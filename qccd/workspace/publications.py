"""Putting a design on the OFFICIAL leaderboard at qccd.academy, credited to the signed-in person.

A publication is one request to publish one graded local submission on one board:

    testing          the agent asked before the design was graded: the local grade is running
    test_failed      that grade was not eligible (or the review here did not accept it): nothing to publish
    reviewing        graded eligible, and the person's reviewer is reviewing it on this computer first
                     (reviews.py): the decision in seconds, the written review in a few minutes
    awaiting_person  graded (and reviewed); the person's Studio shows "Submit ... as <name>?"
    uploading        the person pressed it (or asked for it themselves): approved and being sent
    uploaded         the server has it; its own grade, its own decision and the leaderboard place
                     follow (`official`)
    declined | failed

What makes it safe to let an agent ask:
  * only a PERSON approves, and the approval is the existing one (approve_publish binds the
    bundle digest and the parameters, and publish re-hashes the bundle before one byte goes);
  * nothing is sent unless this computer is signed in with a qccd.academy account (account.py),
    and the leaderboard names that account as the one who submitted it;
  * the design must have passed the test first: a current, eligible local reference grade of
    exactly this design on exactly this board -- under the rule set it is entered under (the
    board's, or one the request names: `rules`), which the upload carries as it was tested.

The server regrades everything itself; the local grade is what lets the person see beforehand
that it will pass, and the review on this computer what tells them why and how to do better.  The
server decides again from its own grade (an upload asks it to, `X-QCCD-Reviewer: none`), never from
the review here; a review written in Docker may go with the upload, marked as the submitter's.  The
official status is read from the server when asked (cached a few seconds), never pushed, so a closed
browser or a stopped service loses nothing.
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from typing import Mapping

from .store import dumps, loads

__all__ = ["PublicationsMixin", "PUBLICATION_STATES"]

PUBLICATION_STATES = ("testing", "test_failed", "reviewing", "awaiting_person", "uploading", "uploaded", "declined",
                      "failed")
ACTIVE = ("testing", "reviewing", "awaiting_person", "uploading", "uploaded")
_REFRESH_S = 5.0


def _now() -> float:
    return time.time()


class PublicationsMixin:
    """`Workspace` methods for the official leaderboard (the table is `publications`, store.py)."""

    # ------------------------------------------------------------------ helpers

    def _accounts(self):
        from .account import Accounts
        acc = getattr(self, "_accounts_obj", None)
        if acc is None:
            acc = self._accounts_obj = Accounts()
        return acc

    def account(self, verify: bool = False) -> dict:
        """Who this computer submits as (the name only, never the key)."""
        return self._accounts().status(verify=verify)

    def _pub_row(self, pid: str) -> dict:
        from .core import WorkspaceError
        r = self.store.one("SELECT * FROM publications WHERE id=?", (pid,))
        if r is None:
            raise WorkspaceError("unknown_publication", f"no publication {pid!r}", status=404)
        d = dict(r)
        for k in ("requested_by", "approved_by", "official"):
            d[k] = loads(d[k]) if d[k] else None
        return d

    def _pub_move(self, pid: str, from_state: str, **fields) -> bool:
        """`_pub_set`, only if the request is still in `from_state`: two readers that see a review
        finish at once never both start the upload."""
        fields["updated_at"] = _now()
        for k in ("requested_by", "approved_by", "official"):
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = dumps(fields[k])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.store.tx() as db:
            cur = db.execute(f"UPDATE publications SET {cols} WHERE id=? AND state=?", (*fields.values(), pid, from_state))
            if cur.rowcount != 1:
                return False
            row = db.execute("SELECT id, state, board, branch FROM publications WHERE id=?", (pid,)).fetchone()
            self._emit(db, "publication.updated", {"publication_id": pid, "state": row["state"],
                                                   "board": row["board"]}, row["branch"])
        return True

    def _pub_set(self, pid: str, **fields) -> None:
        fields["updated_at"] = _now()
        for k in ("requested_by", "approved_by", "official"):
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = dumps(fields[k])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.store.tx() as db:
            db.execute(f"UPDATE publications SET {cols} WHERE id=?", (*fields.values(), pid))
            row = db.execute("SELECT id, state, board, branch FROM publications WHERE id=?", (pid,)).fetchone()
            self._emit(db, "publication.updated", {"publication_id": pid, "state": row["state"],
                                                   "board": row["board"]}, row["branch"])

    def _passing_submission(self, branch: str, rel, rules: Mapping | None | bool = False) -> dict | None:
        """The newest local grade of this design on this board that is still current (the design
        has not changed since), and whether it passed.  `rules` (a `rules_choice`, None for the
        board's) asks for one tested under that rule set; left out, any."""
        import hashlib
        want = None if rules is False else (
            "sha256:" + hashlib.sha256(rules["text"].encode()).hexdigest() if rules else "board")
        rows = self.store.all("SELECT s.id FROM submissions s JOIN snapshots n ON n.id=s.snapshot_id "
                              "WHERE n.branch=? AND s.task_release=? AND s.profile='reference' "
                              "ORDER BY s.created_at DESC LIMIT 10", (branch, rel.id))
        for r in rows:
            s = self.submission(r["id"])
            if s["stale"]:
                continue
            if want is not None and want != ((s.get("rules") or {}).get("digest") or "board"):
                continue
            return s
        return None

    # ------------------------------------------------------------------ requests

    def request_publication(self, actor: Mapping, *, board: str | None, design: str | None = None,
                            submission_id: str | None = None, display_name: str | None = None,
                            visibility: str = "public", origin_prompt_id: str | None = None,
                            rules=None, reviewer: str | None = None, attach_review: bool = False) -> dict:
        """Ask to put a design on the official leaderboard.  From a PERSON (the Studio's Submit button,
        `qccd publish`) this is the approval and it uploads now -- after the review on this computer
        when they have picked a reviewer; from an AGENT it waits for the person, after testing the
        design first when it has no current passing grade on that board, and reviewing it.  `rules`:
        the rule set it is entered under ("board", a shipped set, a .rules file); left out, whichever
        its newest current test used.  `reviewer` ("claude", "codex", "none"; left out, the person's
        choice): who reviews it here before it is sent (reviews.py).  `attach_review`: send that review
        with it, marked as the submitter's (a Docker review with no credential in it, only)."""
        from .collab import REVIEWER_CHOICES
        from .core import WorkspaceError, new_id
        if visibility not in ("public", "private"):
            raise WorkspaceError("bad_request", "visibility must be public (on the leaderboard, with your name) "
                                 "or private (only you see it)", status=422)
        if reviewer is not None and reviewer not in REVIEWER_CHOICES:
            raise WorkspaceError("bad_request", f"reviewer is one of {', '.join(REVIEWER_CHOICES)}", status=422)
        who = reviewer or self.settings()["reviewer"]
        human = actor.get("kind") == "human"
        chosen = False
        if submission_id:
            s = self.submission(submission_id)
            rel = self._bundle_board(self.root / s["snapshot"]["dir"] / "bundle")
            if board and self.board(board).id != rel.id:
                raise WorkspaceError("bad_request", f"that submission was graded for {rel.title}", status=422)
            branch = s["snapshot"]["branch"]
        else:
            rel = self.board(board)
            if not board:
                raise WorkspaceError("bad_request", "name the board: " + "; ".join(b["title"] for b in self.boards()),
                                     status=422)
            branch = self.resolve_draft(design or "main")
            chosen = False if rules is None else self.rules_choice(rules)
            s = self._passing_submission(branch, rel, chosen)
        title = self.design_title(self.branch(branch))
        name = " ".join(str(display_name or title).split())[:80] or title
        acct = self.account()
        if human and not acct["signed_in"]:
            raise WorkspaceError("not_signed_in", "sign in to qccd.academy first (Sign in, in the Leaderboard panel, "
                                 "or `qccd login`): the leaderboard shows who submitted each design", status=409)
        # one request per graded submission at a time: asking again returns it
        if s is not None:
            prior = self.store.one("SELECT id FROM publications WHERE submission_id=? AND state IN "
                                   "('reviewing','awaiting_person','uploading','uploaded') ORDER BY created_at DESC LIMIT 1",
                                   (s["id"],))
            if prior:
                out = self.publication(prior["id"])
                if human and out["state"] in ("awaiting_person", "reviewing"):
                    return self.confirm_publication(actor, prior["id"], display_name=display_name,
                                                    visibility=visibility, reviewer=reviewer,
                                                    attach_review=attach_review)
                return {**out, "duplicate": True}
        state, sub_id, job = None, None, None
        status = s["status"] if s is not None else None
        if status == "ineligible":
            reasons = ((s["report"] or {}).get("eligibility") or {}).get("reasons") or []
            under = (s.get("rules") or {}).get("name")
            raise WorkspaceError("not_eligible", f"{title} did not pass on {rel.title}"
                                 + (f" under its rules ({under})" if under else "") + ": " + "; ".join(reasons[:3])
                                 + ". Change the design, or the rules it is entered under, and test it again "
                                 "(a review -- qccd_review, or Review in the Studio -- says where it fails and how "
                                 "to fix it).", status=409)
        if status == "grading":
            state, sub_id = "testing", s["id"]
        elif status == "eligible":
            state, sub_id = ("uploading" if human else "awaiting_person"), s["id"]
        elif human:
            under = f" under {chosen['name']}" if chosen else ""
            raise WorkspaceError("not_tested", f"test {title} on {rel.title}{under} first (Test on this board): only a "
                                 "design that passes the reference checks can be submitted", status=409)
        else:
            # the agent's "submit it" on an untested design: test it now, and ask the person when it passes
            job = self.submit_design(actor, design=branch, board=rel.id, origin_prompt_id=origin_prompt_id,
                                     rules=None if rules is None else chosen and chosen["text"])
            state = "testing"
        # the review on this computer comes first, when the person picked a reviewer: a finished
        # review of exactly this graded design is used as it is
        review_id = None
        if state in ("uploading", "awaiting_person") and who:
            review_id, finished = self._review_for_publication(actor, s["id"], who)
            if not finished:
                state = "reviewing"
        pid = new_id("pb")
        now = _now()
        with self.store.tx() as db:
            db.execute("INSERT INTO publications(id, branch, board, submission_id, submit_job, display_name, visibility, "
                       "state, requested_by, approved_by, server, origin_prompt_id, created_at, updated_at, reviewer, "
                       "review_id, attach_review) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, branch, rel.id, sub_id, (job or {}).get("job_id"), name, visibility, state,
                        dumps(dict(actor)), dumps(dict(actor)) if human else None, self._accounts().server,
                        origin_prompt_id, now, now, who, review_id, 1 if attach_review else 0))
            self._emit(db, "publication.updated", {"publication_id": pid, "state": state, "board": rel.id}, branch)
            prompt = origin_prompt_id or (self._working_prompt(db, actor) if not human else None)
            if prompt:
                self._link_prompt(db, prompt, "publication", pid, actor)
        if state == "uploading":
            self._start_upload(pid)
        elif state == "testing":
            self._watch_test(pid)
        elif state == "reviewing":
            self._watch_review(pid)
        out = self.publication(pid)
        if not human:
            out["next"] = ("the person's Studio shows a card asking them to submit it as "
                           f"{acct['name']}; it uploads when they press it" if acct["signed_in"] else
                           "nobody is signed in on this computer: the card asks the person to sign in to qccd.academy "
                           "first (an agent cannot); it uploads when they have and press Submit")
            if state == "testing":
                out["next"] = ("testing it on the board first (the local reference grade, usually under a minute); "
                               "then " + out["next"])
            elif state == "reviewing":
                out["next"] = (f"{self._reviewer_said(who)} is reviewing it on this computer first (the decision in "
                               "seconds, the written review in a few minutes); then " + out["next"])
            out["next"] += ". Tell them in one line, and stop: the card follows it to the leaderboard."
        return out

    def confirm_publication(self, actor: Mapping, pid: str, *, display_name: str | None = None,
                            visibility: str | None = None, reviewer: str | None = None,
                            attach_review: bool | None = None) -> dict:
        """The person's Submit on a request an agent made: it uploads now, or -- with a reviewer that
        has not reviewed this graded design yet, or one still reviewing it -- once the review here is
        done."""
        from .collab import REVIEWER_CHOICES
        from .core import WorkspaceError
        if actor.get("kind") != "human":
            raise WorkspaceError("forbidden", "only the person submits to the official leaderboard", status=403)
        p = self._refresh_review(self._refresh_test(self._pub_row(pid)))
        if p["state"] not in ("awaiting_person", "reviewing"):
            raise WorkspaceError("bad_state", f"this request is {p['state'].replace('_', ' ')}", status=409)
        if not self.account()["signed_in"]:
            raise WorkspaceError("not_signed_in", "sign in to qccd.academy first", status=409)
        s = self.submission(p["submission_id"])
        if s["stale"]:
            raise WorkspaceError("stale", "the design changed after it was tested: test it again", status=409)
        fields: dict = {"approved_by": dict(actor), "error": None}
        if display_name is not None and " ".join(str(display_name).split()):
            fields["display_name"] = " ".join(str(display_name).split())[:80]
        if visibility in ("public", "private"):
            fields["visibility"] = visibility
        if attach_review is not None:
            fields["attach_review"] = 1 if attach_review else 0
        if reviewer is not None and reviewer not in REVIEWER_CHOICES:
            raise WorkspaceError("bad_request", f"reviewer is one of {', '.join(REVIEWER_CHOICES)}", status=422)
        who = reviewer or p.get("reviewer") or self.settings()["reviewer"]
        if p["state"] == "reviewing":
            # the person pressed Submit while the review runs: it goes when the review is done
            self._pub_set(pid, **fields)
            return self.publication(pid)
        if who:
            done = self.latest_review(s["id"], reviewer=who, isolation=self.settings()["review_isolation"], finished=True)
            if done is None:
                rid, finished = self._review_for_publication(actor, s["id"], who)
                if not finished:
                    if self._pub_move(pid, "awaiting_person", state="reviewing", reviewer=who, review_id=rid, **fields):
                        self._watch_review(pid)
                    return self.publication(pid)
                fields["review_id"] = rid
            else:
                fields["review_id"] = done["review_id"]
            fields["reviewer"] = who
        if self._pub_move(pid, "awaiting_person", state="uploading", **fields):
            self._start_upload(pid)
        return self.publication(pid)

    def decline_publication(self, actor: Mapping, pid: str) -> dict:
        from .core import WorkspaceError
        if actor.get("kind") != "human":
            raise WorkspaceError("forbidden", "only the person declines a submission", status=403)
        p = self._pub_row(pid)
        if p["state"] not in ("testing", "awaiting_person", "test_failed", "failed"):
            raise WorkspaceError("bad_state", f"this request is {p['state']}", status=409)
        self._pub_set(pid, state="declined")
        return self.publication(pid)

    # ------------------------------------------------------------------ the local test, then the upload

    def _refresh_test(self, p: dict) -> dict:
        """A request that is testing moves on when its local grade has a verdict."""
        if p["state"] != "testing":
            return p
        sub_id = p["submission_id"]
        if not sub_id and p["submit_job"]:
            j = self.job(p["submit_job"])
            if j["status"] in ("queued", "running"):
                return p
            sub_id = (j.get("result") or {}).get("submission_id")
            if j["status"] != "succeeded" or not sub_id:
                why = j.get("error") or (j.get("result") or {}).get("summary") or j["status"]
                self._pub_set(p["id"], state="test_failed", error=str(why).split(": ", 1)[-1][:300])
                return self._pub_row(p["id"])
            self._pub_set(p["id"], submission_id=sub_id)
        s = self.submission(sub_id)
        if s["status"] == "grading":
            return self._pub_row(p["id"])
        if s["status"] == "eligible":
            who = p.get("reviewer")
            if who:
                try:
                    rid, finished = self._review_for_publication(p["requested_by"] or {"kind": "system"}, s["id"], who)
                except Exception as exc:          # a reviewer the person has not signed in: they still decide
                    self._pub_move(p["id"], "testing", state="awaiting_person", error=f"not reviewed here: {exc}"[:300])
                    return self._pub_row(p["id"])
                if not finished and self._pub_move(p["id"], "testing", state="reviewing", review_id=rid):
                    self._watch_review(p["id"])
                    return self._pub_row(p["id"])
                self._pub_move(p["id"], "testing", state="awaiting_person", review_id=rid)
            else:
                self._pub_move(p["id"], "testing", state="awaiting_person")
        else:
            reasons = ((s["report"] or {}).get("eligibility") or {}).get("reasons") or [s["status"]]
            self._pub_set(p["id"], state="test_failed", error="; ".join(reasons[:3])[:300])
        return self._pub_row(p["id"])

    def _watch_test(self, pid: str) -> None:
        def loop():
            deadline = _now() + 3 * 3600
            while not getattr(self, "_closed", False) and _now() < deadline:
                try:
                    if self._refresh_test(self._pub_row(pid))["state"] != "testing":
                        return
                except Exception:
                    return
                time.sleep(1.0)
        threading.Thread(target=loop, name=f"qccd-pub-test-{pid}", daemon=True).start()

    # ------------------------------------------------------------------ the review here, before it is sent

    def _reviewer_said(self, who: str | None) -> str:
        from .reviews import _title
        return _title(who) if who else "nobody"

    def _review_for_publication(self, actor: Mapping, sub_id: str, who: str) -> tuple[str, bool]:
        """(the review of this graded design by `who`, and whether it is finished): one already
        finished is used as it is; otherwise one starts now (reviews.py -- a reviewer that is not
        signed in is refused, with what to do)."""
        iso = self.settings()["review_isolation"]
        done = self.latest_review(sub_id, reviewer=who, isolation=iso, finished=True)
        if done is not None:
            return done["review_id"], True
        rv = self.start_review(actor, submission_id=sub_id, reviewer=who, isolation=iso)
        return rv["review_id"], rv["status"] in ("done", "failed") and rv.get("decision") is not None

    def _refresh_review(self, p: dict) -> dict:
        """A request that waits for the review here moves on when the review has its decision and
        the agent is done: a person who already pressed Submit sends it now; otherwise they decide."""
        if p["state"] != "reviewing" or not p.get("review_id"):
            return p
        r = self._review_row(p["review_id"])
        if r["status"] in ("queued", "checking", "reviewing"):
            return p
        if r["decision"] == "not_accepted":
            why = "; ".join(((r["reasons"] or {}).get("reasons") or [])[:3])
            self._pub_move(p["id"], "reviewing", state="test_failed", error=f"not accepted on this computer: {why}"[:300])
        elif p.get("approved_by"):
            if self._pub_move(p["id"], "reviewing", state="uploading", error=None):
                self._start_upload(p["id"])
        else:
            self._pub_move(p["id"], "reviewing", state="awaiting_person")
        return self._pub_row(p["id"])

    def _watch_review(self, pid: str) -> None:
        def loop():
            deadline = _now() + 3 * 3600
            while not getattr(self, "_closed", False) and _now() < deadline:
                try:
                    if self._refresh_review(self._pub_row(pid))["state"] != "reviewing":
                        return
                except Exception:
                    return
                time.sleep(1.0)
        threading.Thread(target=loop, name=f"qccd-pub-review-{pid}", daemon=True).start()

    def _attachable(self, review_id: str | None) -> tuple[bool, str]:
        """Whether the review here may go with the upload: written in Docker (where the agent saw
        nothing of this computer but the review's own folder), finished, and with nothing in it that
        looks like a credential."""
        from ..review.engine import find_secrets
        if not review_id:
            return False, "there is no review to send"
        r = self._review_row(review_id)
        if r["isolation"] != "docker":
            return False, "only a review written in Docker goes with a submission"
        if r["status"] != "done" or not r["review_md"]:
            return False, "the review did not finish"
        hits = find_secrets(r["review_md"])
        if hits:
            return False, f"the review holds what looks like a credential ({hits[0]}): it stays on this computer"
        return True, ""

    def _start_upload(self, pid: str) -> None:
        threading.Thread(target=self._upload, args=(pid,), name=f"qccd-pub-{pid}", daemon=True).start()

    def _upload(self, pid: str) -> None:
        """Approve exactly this bundle as the person, then send it with this computer's key."""
        from .account import OfficialHTTPError, credential, official_call
        p = self._pub_row(pid)
        try:
            cred = credential(p["server"])
            if not cred:
                raise RuntimeError("not signed in to qccd.academy any more: sign in and press Submit again")
            review = self.prepare_publish(p["approved_by"], p["submission_id"], visibility=p["visibility"],
                                          display_name=p["display_name"])
            appr = self.approve_publish(p["approved_by"], p["submission_id"], review["bundle_digest"],
                                        {"visibility": p["visibility"], "display_name": p["display_name"]})
            self._pub_set(pid, approval_id=appr["approval_id"])

            def uploader(archive: bytes, meta: dict) -> dict:
                h = {"Content-Type": "application/zip", "X-QCCD-Task": meta["task"],
                     "X-QCCD-Visibility": meta.get("visibility", "public")}
                if meta.get("display_name"):
                    # a header carries latin-1 only: the name goes percent-encoded, and the server decodes it
                    h["X-QCCD-Display-Name"] = urllib.parse.quote(meta["display_name"], safe=" ")
                if self._server_decides(p["server"]):
                    # the server's own decision from its own grade, no agent of its own (`none`): the
                    # review that explains it was written here
                    h["X-QCCD-Reviewer"] = "none"
                return official_call("POST", "/v1/submissions", raw=archive, token=cred["token"],
                                     server=p["server"], headers=h, timeout=180)
            res = self.publish(appr["approval_id"], uploader)["server"]
            official = {**res, "fetched_at": _now()}
            if p.get("attach_review"):
                official["attached"] = self._attach_review(p, res.get("submission_id"), cred["token"])
            self._pub_set(pid, state="uploaded", official_id=res.get("submission_id"),
                          credit=res.get("by") or ((cred.get("account") or {}).get("name")), official=official)
        except OfficialHTTPError as e:
            msg = str(e)
            if e.status == 401:
                msg = "this computer's key was revoked or signed out on the site: sign in again, then Submit"
            elif e.code == "unknown_task" or e.code == "wrong_task":
                msg = f"the official server does not grade this edition of the board yet ({e})"
            self._pub_set(pid, state="failed", error=msg[:400])
        except Exception as e:                                  # say why on the card; never hang in 'uploading'
            self._pub_set(pid, state="failed", error=str(e)[:400])

    def _server_decides(self, server: str) -> bool:
        """The server makes its own decision when asked (it runs reviews); an older one does not."""
        from .account import OfficialHTTPError, official_call
        try:
            return bool(official_call("GET", "/v1/reviewers", server=server, timeout=8).get("enabled"))
        except OfficialHTTPError:
            return False

    def _attach_review(self, p: dict, official_id: str | None, token: str) -> dict:
        """Send the review written here with the submission, marked as the submitter's (when it may go:
        `_attachable`): {"sent": bool, "why"}."""
        from .account import OfficialHTTPError, official_call
        ok, why = self._attachable(p.get("review_id"))
        if not ok or not official_id:
            return {"sent": False, "why": why or "the server gave no submission id"}
        r = self._review_row(p["review_id"])
        try:
            official_call("POST", f"/v1/submissions/{official_id}/submitter-review",
                          {"reviewer": r["reviewer"], "model": r["model"], "isolation": r["isolation"],
                           "decision": r["decision"], "review_md": r["review_md"], "written_at": r["finished_at"]},
                          token=token, server=p["server"], timeout=30)
            return {"sent": True, "why": None}
        except OfficialHTTPError as e:
            return {"sent": False, "why": f"the server did not take it: {e}"[:300]}

    # ------------------------------------------------------------------ reading it back

    def _refresh_official(self, p: dict, force: bool = False) -> dict:
        """The server's own verdict and the leaderboard place, re-read at most every few seconds --
        in the background, so a slow or unreachable qccd.academy never holds up the chat or the
        panel (they show the last answer, and the next read has the new one).  `force` reads now."""
        if p["state"] != "uploaded" or not p["official_id"]:
            return p
        o = p["official"] or {}
        # while the server grades, every few seconds; once it has a verdict, a minute (others' entries
        # can still move its place)
        age = _now() - o.get("fetched_at", 0)
        # an agent review still running (qccd/official/review.py) keeps the reads frequent too
        settled = o.get("status") in ("eligible", "ineligible") and \
            (o.get("review") or {}).get("status") in (None, "done", "failed")
        if not force and age < (60 if settled else _REFRESH_S):
            return p
        if force:
            return self._fetch_official(p)
        busy = getattr(self, "_pub_refreshing", None)
        if busy is None:
            busy = self._pub_refreshing = set()
        if p["id"] not in busy:
            busy.add(p["id"])

            def run():
                try:
                    self._fetch_official(p)
                except Exception:
                    pass
                finally:
                    busy.discard(p["id"])
            threading.Thread(target=run, name=f"qccd-pub-read-{p['id']}", daemon=True).start()
        return p

    def _fetch_official(self, p: dict) -> dict:
        from .account import OfficialHTTPError, credential, official_call
        o = p["official"] or {}
        cred = credential(p["server"])
        try:
            s = official_call("GET", f"/v1/submissions/{p['official_id']}", token=(cred or {}).get("token"),
                              server=p["server"], timeout=15)
            o = {k: s.get(k) for k in ("submission_id", "status", "visibility", "display_name", "by", "created_at")}
            rep = s.get("report") or {}
            o["eligible"] = rep.get("eligible")
            o["metrics"] = rep.get("metrics")
            o["reasons"] = ((rep.get("eligibility") or {}).get("reasons") or [])[:3]
            o["job"] = s.get("job")
            # the server's own decision (and its agent's review, if it ran one), and where to read it
            rvw = s.get("review") or None
            if rvw:
                o["review"] = {**{k: rvw.get(k) for k in ("id", "reviewer", "model", "status", "decision")},
                               "page": f"{p['server']}{rvw.get('page') or ''}" if rvw.get("page") else None}
            if (p["official"] or {}).get("attached"):
                o["attached"] = p["official"]["attached"]
            # an entry that keeps the board's rules is ranked among those; one under rules of its own is
            # listed with every entry, behind the board's filter (`conformance`)
            o["conforms"] = (rep.get("conformance") or {}).get("board", True)
            if o["status"] == "eligible" and p["visibility"] == "public":
                lb = official_call("GET", f"/v1/leaderboard/{p['board']}"
                                   + ("" if o["conforms"] else "?rules=all"), server=p["server"], timeout=15)
                ids = [r["id"] for r in lb.get("rows") or []]
                if p["official_id"] in ids:
                    o["rank"], o["of"] = ids.index(p["official_id"]) + 1, len(ids)
                o["rank_by"] = lb.get("rank_by")
                o["rank_value"] = next((r.get("rank_value") for r in lb.get("rows") or [] if r["id"] == p["official_id"]),
                                       None)
            o["fetched_at"] = _now()
        except OfficialHTTPError as e:
            o = {**o, "fetched_at": _now(), "unreachable": str(e)}
        self._pub_set(p["id"], official=o)
        return self._pub_row(p["id"])

    def publication(self, pid: str, refresh: bool = True) -> dict:
        p = self._pub_row(pid)
        if refresh:
            p = self._refresh_official(self._refresh_review(self._refresh_test(p)))
        return self._pub_json(p)

    def publications(self, board: str | None = None, limit: int = 30, refresh: bool = True) -> list:
        args: list = []
        where = ""
        if board:
            where, args = "WHERE board=?", [self.board(board).id]
        rows = self.store.all(f"SELECT id FROM publications {where} ORDER BY created_at DESC LIMIT ?",
                              (*args, max(1, min(int(limit), 100))))
        return [self.publication(r["id"], refresh=refresh) for r in rows]

    # ------------------------------------------------------------------ one board, as the Studio's panel shows it

    def _official_cached(self, path: str, ttl: float = 10.0):
        """A public read of the official server, cached briefly (the panel re-reads while it is open)."""
        from .account import OfficialHTTPError, official_call
        cache = getattr(self, "_official_cache", None)
        if cache is None:
            cache = self._official_cache = {}
        server = self._accounts().server
        hit = cache.get((server, path))
        if hit and _now() - hit[0] < ttl:
            return hit[1]
        try:
            val = {"ok": official_call("GET", path, server=server, timeout=8)}
        except OfficialHTTPError as e:
            val = {"error": {"status": e.status, "code": e.code, "message": str(e)}}
        cache[(server, path)] = (_now(), val)
        return val

    def board_view(self, board: str | None, design: str | None = None, rules: str | None = None) -> dict:
        """Everything the Leaderboard panel shows for one board: what it ranks, the official ranking
        (live from qccd.academy, with who submitted each design; `rules` "all" lists the entries under
        rule sets of their own too), this design's test on it, its requests to publish, and who this
        computer submits as."""
        view = "all" if rules == "all" else "board"
        rows_of = lambda lb: [{k: r.get(k) for k in ("id", "display_name", "by", "rank_value", "created_at", "conformance")}
                              for r in lb.get("rows") or []]
        rel = self.board(board or None) if board else None
        tasks = self._official_cached("/v1/tasks", ttl=60.0)
        on_server = {t["id"]: t.get("digest") for t in ((tasks.get("ok") or {}).get("tasks") or [])}
        boards = [{"id": b["id"], "title": b["title"], "rank_by": b["rank_by"],
                   "on_server": b["id"] in on_server} for b in self.boards()]
        out: dict = {"boards": boards, "account": self.account(verify=True), "server": self._accounts().server,
                     "server_error": (tasks.get("error") or {}).get("message")}
        # who reviews a design on this computer before it is sent, and whether the server makes its own
        # decision when asked (an older server does not)
        out["reviewers"] = self.reviewers_view()
        rv = self._official_cached("/v1/reviewers", ttl=20.0).get("ok") or {}
        out["server_decides"] = bool(rv.get("enabled"))
        if rel is None:
            return out
        m = rel.manifest
        rank = m.get("rank_by")
        met = next((x for x in m.get("metrics", []) if x.get("name") == rank), {})
        out["board"] = {"id": rel.id, "title": rel.title, "description": m.get("description"), "rank_by": rank,
                        "better": met.get("better", "low"), "unit": met.get("unit"), "about_metric": met.get("about"),
                        "starter": m.get("starter"), "memory": bool(m.get("qec")),
                        "on_server": rel.id in on_server,
                        "same_edition": on_server.get(rel.id) in (None, rel.digest)}
        lb_path = f"/v1/leaderboard/{urllib.parse.quote(rel.id, safe='@')}" + ("?rules=all" if view == "all" else "")
        lb = self._official_cached(lb_path)
        if lb.get("ok"):
            out["official"] = {"rows": rows_of(lb["ok"]), "rules": view, "counts": lb["ok"].get("counts"),
                               "rank_by": lb["ok"].get("rank_by"), "better": lb["ok"].get("better")}
        else:
            out["official"] = {"rows": [], "error": (lb.get("error") or {}).get("message")}
        branch = self.resolve_draft(design or "main")
        out["design"] = {"name": self.design_title(self.branch(branch)), "branch": branch,
                         "revision": self.head(branch).revision}
        s = self._passing_submission(branch, rel)
        test = None
        for j in self.active_jobs():
            if j["kind"] != "submit":
                continue
            prm = (self.job(j["job_id"]).get("result") or {}).get("params") or {}   # a job keeps what it was given
            if prm.get("branch") == branch and prm.get("board") == rel.id:
                test = {"state": "running", "job_id": j["job_id"], "progress": (j.get("progress") or {}).get("message")}
                break
        if test is None and s is not None:
            rep = s["report"] or {}
            mm = (rep.get("metrics") or {}).get(rank) or {}
            test = {"state": {"grading": "running"}.get(s["status"], s["status"]), "submission_id": s["id"],
                    "eligible": (rep.get("eligibility") or {}).get("eligible"),
                    "reasons": ((rep.get("eligibility") or {}).get("reasons") or [])[:3],
                    "value": mm.get("value"), "unit": mm.get("unit"), "revision": s["snapshot"]["revision"],
                    "rules": s.get("rules"), "conformance": rep.get("conformance")}
            if s["status"] == "grading":
                jid = s.get("job_id")
                if jid:
                    test["progress"] = ((self.job(jid).get("progress") or {}) or {}).get("message")
        if test and test.get("submission_id"):
            # the newest review of this very test, whoever wrote it
            test["review"] = self.latest_review(test["submission_id"])
        out["test"] = test
        out["publications"] = self.publications(rel.id, limit=10)
        # the server has placed one of this computer's designs, but the (cached) ranking does not list it
        # yet: read the ranking again now, so the table shows it at the moment the request does
        listed = {r["id"] for r in out["official"]["rows"]}
        if any((p.get("official") or {}).get("rank") and (p.get("official") or {}).get("submission_id") not in listed
               for p in out["publications"]):
            lb = self._official_cached(lb_path, ttl=0.0)
            if lb.get("ok"):
                out["official"]["rows"] = rows_of(lb["ok"])
        # this very graded design, already sent (or waiting for the person): Submit has nothing new to send
        if test and test.get("submission_id"):
            for p in out["publications"]:
                if p["submission_id"] == test["submission_id"] and p["state"] in ("reviewing", "awaiting_person", "uploading",
                                                                                  "uploaded"):
                    test["publication"] = {"publication_id": p["publication_id"], "state": p["state"]}
                    break
        # where this design's result would rank among the official ones (the server's grade decides);
        # its own entry, if it is already there, is not counted against it
        # (an entry under rules of its own is ranked with every entry, not among the board's)
        if test and not ((test.get("conformance") or {}).get("board", True)):
            test["listed_with"] = "all"
        if test and test.get("eligible") and isinstance(test.get("value"), (int, float)) and \
                (test.get("listed_with") != "all" or view == "all"):
            own = {(p.get("official") or {}).get("submission_id") for p in out["publications"]
                   if p["submission_id"] == test.get("submission_id")}
            vals = [r["rank_value"] for r in out["official"]["rows"]
                    if isinstance(r.get("rank_value"), (int, float)) and r["id"] not in own]
            better = out["board"]["better"]
            out["test"]["would_rank"] = 1 + sum(1 for v in vals if (v < test["value"] if better == "low"
                                                                     else v > test["value"]))
            out["test"]["of"] = len(vals) + 1
        return out

    def _pub_json(self, p: dict) -> dict:
        o = p["official"] or {}
        acct = self.account()
        out = {"publication_id": p["id"], "state": p["state"], "design": self._design_title_of(p["branch"]),
               "board": self._board_title(p["board"]), "board_id": p["board"], "display_name": p["display_name"],
               "visibility": p["visibility"], "submission_id": p["submission_id"], "error": p["error"],
               "requested_by": (p["requested_by"] or {}).get("kind"), "created_at": p["created_at"],
               "updated_at": p["updated_at"],
               # who it is (or will be) credited to: the person signed in on this computer
               "credit": p["credit"] or (acct["name"] if acct["signed_in"] else None),
               "signed_in": acct["signed_in"]}
        if p["submission_id"]:
            try:                                  # the rule set it was tested, and is entered, under
                out["rules"] = self.submission(p["submission_id"]).get("rules")
            except Exception:
                out["rules"] = None
        out["reviewer"] = p.get("reviewer")
        out["attach_review"] = bool(p.get("attach_review"))
        if p.get("review_id"):
            try:                                  # the review written here before it was sent
                out["review"] = self.review(p["review_id"], brief=True)
            except Exception:
                out["review"] = None
        if p["state"] == "uploaded":
            out["official"] = {k: o.get(k) for k in ("submission_id", "status", "by", "eligible", "metrics", "reasons",
                                                     "rank", "of", "rank_by", "rank_value", "unreachable", "conforms",
                                                     "review", "attached")}
            out["official"]["report_url"] = f"{p['server']}/v1/submissions/{p['official_id']}/report"
            try:
                rel = self.board(p["board"])
                rk = rel.manifest.get("rank_by")
                out["official"]["unit"] = next((x.get("unit") for x in rel.manifest.get("metrics", [])
                                                if x.get("name") == rk), None)
            except Exception:
                pass
        if p["submission_id"] and p["state"] in ("awaiting_person", "testing", "test_failed", "reviewing"):
            try:
                rep = self.report_for(p["submission_id"]) or {}
                rank = rep.get("rank_by")
                m = (rep.get("metrics") or {}).get(rank) if rank else None
                if m:
                    out["local"] = {"metric": rank, "value": m.get("value"), "unit": m.get("unit"),
                                    "eligible": (rep.get("eligibility") or {}).get("eligible")}
            except Exception:
                pass
        return out
