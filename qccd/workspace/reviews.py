"""Reviewing a design on the person's own computer, before it is submitted.

`start_review` takes a graded local submission -- the newest current test of a design on a board,
or one by id -- and starts a `review` job: the design-review skill's rule check and the decision,
made by the same function the official server runs (qccd/review/engine.py), then the person's own
reviewer agent -- Claude Code or Codex, signed in with their own subscription in a review profile
of its own (qccd/review/agents.py), in its CLI's sandbox or in Docker (qccd/review/container.py) --
writes the review: what fails, where, and how to fix it.

Every door starts the same job: the Studio's Review button (and its Submit, which reviews first
when the person has picked a reviewer), `qccd review`, and an agent's `qccd_review`.  The decision
here is a preview: the official server decides again from its own grade, and never from a review
written where it cannot see.

What a review keeps is its row in `reviews` and `.qccd/reviews/<id>/` (the review, the rule check,
the re-cooled programme the rule check tested).  The folder the agent works in is a temporary one
outside the workspace, so the workspace's own CLAUDE.md and AGENTS.md (the co-design skill's)
never reach the reviewer.

The person's choices are settings (`reviewer`, `review_isolation`).  Signing a reviewer in needs
the person: at a terminal (`qccd review login`), or from the Studio (`start_signin`: the CLI's own
sign-in, its link and code shown on the page).
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Mapping

from .store import dumps, loads

__all__ = ["ReviewsMixin", "REVIEW_STATES"]

REVIEW_STATES = ("queued", "checking", "reviewing", "done", "failed")
ACTIVE = ("queued", "checking", "reviewing")


def _now() -> float:
    return time.time()


def _title(rid: str) -> str:
    from ..review.agents import REVIEWERS
    return "No agent" if rid == "none" else REVIEWERS[rid]["title"]


class ReviewsMixin:
    """`Workspace` methods for reviews on this computer (the table is `reviews`, store.py)."""

    # ------------------------------------------------------------------ helpers

    def _review_row(self, rid: str) -> dict:
        from .core import WorkspaceError
        r = self.store.one("SELECT * FROM reviews WHERE id=?", (rid,))
        if r is None:
            raise WorkspaceError("unknown_review", f"no review {rid!r}", status=404)
        d = dict(r)
        for k in ("reasons", "rule_check", "progress", "usage", "files", "egress", "actor"):
            d[k] = loads(d[k]) if d[k] else None
        return d

    def _review_set(self, rid: str, *, emit: bool = False, **fields) -> None:
        for k in ("reasons", "rule_check", "progress", "usage", "files", "egress"):
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = dumps(fields[k])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.store.tx() as db:
            db.execute(f"UPDATE reviews SET {cols} WHERE id=?", (*fields.values(), rid))
            if emit:
                row = db.execute("SELECT id, submission_id, status, decision FROM reviews WHERE id=?", (rid,)).fetchone()
                self._emit(db, "review.updated", {"review_id": rid, "submission_id": row["submission_id"],
                                                  "status": row["status"], "decision": row["decision"]}, None)

    def _keep_dir(self, rid: str) -> Path:
        return self.root / ".qccd" / "reviews" / rid

    def _review_target(self, submission_id: str | None, design: str | None, board: str | None) -> dict:
        """The local submission a review reads: the one named, or the newest current test of the
        design on the board."""
        from .core import WorkspaceError
        if submission_id:
            return self.submission(submission_id)
        if not board:
            raise WorkspaceError("bad_request", "name the submission, or the board (and the design): "
                                 + "; ".join(b["title"] for b in self.boards()), status=422)
        rel = self.board(board)
        branch = self.resolve_draft(design or "main")
        for r in self.store.all("SELECT s.id FROM submissions s JOIN snapshots n ON n.id=s.snapshot_id "
                                "WHERE n.branch=? AND s.task_release=? ORDER BY s.created_at DESC LIMIT 10",
                                (branch, rel.id)):
            s = self.submission(r["id"])
            if not s["stale"]:
                return s
        raise WorkspaceError("not_tested", f"{self.design_title(self.branch(branch))} has no current test on "
                             f"{rel.title}: test it first (qccd_submit_local, or Test on this board), then review it",
                             status=409)

    # ------------------------------------------------------------------ who can review here

    def reviewers_view(self, fresh: bool = False) -> dict:
        """The reviewers on this computer, for each isolation: installed, signed in, and if not, why
        not -- with the person's choices and the sign-ins in progress."""
        from ..review import agents, container
        st = self.settings()
        ok, why = container.ready(fresh=fresh)
        return {"reviewer": st["reviewer"], "isolation": st["review_isolation"],
                "docker": {"ready": ok, "why": why or None},
                "reviewers": {iso: [agents.status(r, isolation=iso, fresh=fresh) for r in agents.REVIEWERS]
                              for iso in agents.ISOLATIONS},
                "signins": [s.view() for s in getattr(self, "_signins", {}).values()
                            if s.state in ("starting", "waiting") or _now() - (s.finished or 0) < 120]}

    def _reviewer_ready(self, reviewer: str, isolation: str) -> dict:
        from ..review import agents
        from .core import WorkspaceError
        st = agents.status(reviewer, isolation=isolation, fresh=True)
        if not st["available"]:
            sign = "sign" in str(st.get("why") or "")
            raise WorkspaceError("needs_signin" if sign else "reviewer_unavailable",
                                 f"{st['title']} cannot review here: {st['why']}"
                                 + (" (the person signs it in once: Sign in, in the Studio's Leaderboard panel, or "
                                    f"`{st['signin']}` at a terminal -- an agent cannot)" if sign else ""),
                                 status=409, detail={"reviewer": reviewer, "isolation": isolation, "why": st["why"],
                                                     "signin": st["signin"]})
        return st

    # ------------------------------------------------------------------ starting one

    def start_review(self, actor: Mapping, *, submission_id: str | None = None, design: str | None = None,
                     board: str | None = None, reviewer: str | None = None, isolation: str | None = None,
                     request_id: str | None = None, origin_prompt_id: str | None = None) -> dict:
        """Review a graded local submission on this computer: the decision at once (no agent), then
        the person's reviewer's written review.  `reviewer`/`isolation` left out: the person's
        choices (no reviewer chosen: the decision and every rule's verdict only)."""
        from ..review.engine import REVIEW_TIMEOUT_S
        from .collab import REVIEW_ISOLATIONS, REVIEWER_CHOICES
        from .core import WorkspaceError, new_id
        st = self.settings()
        who = reviewer or st["reviewer"] or "none"
        iso = isolation or st["review_isolation"]
        if who not in REVIEWER_CHOICES:
            raise WorkspaceError("bad_request", f"reviewer is one of {', '.join(REVIEWER_CHOICES)}", status=422)
        if iso not in REVIEW_ISOLATIONS:
            raise WorkspaceError("bad_request", f"isolation is one of {', '.join(REVIEW_ISOLATIONS)}", status=422)
        s = self._review_target(submission_id, design, board)
        if s["status"] not in ("eligible", "ineligible"):
            raise WorkspaceError("not_graded", f"that test is {s['status']}: review it once it has a verdict", status=409)
        if who != "none":
            self._reviewer_ready(who, iso)
        prior = self.store.one("SELECT id FROM reviews WHERE submission_id=? AND reviewer=? AND isolation=? AND status IN "
                               "('queued','checking','reviewing') ORDER BY created_at DESC LIMIT 1", (s["id"], who, iso))
        if prior:
            return {**self.review(prior["id"], brief=True), "duplicate": True}
        rid = new_id("rv")
        with self.store.tx() as db:
            db.execute("INSERT INTO reviews(id, submission_id, reviewer, isolation, status, actor, origin_prompt_id, "
                       "created_at) VALUES(?,?,?,?, 'queued', ?, ?, ?)",
                       (rid, s["id"], who, iso, dumps(dict(actor)), origin_prompt_id, _now()))
        job = self.start_job(actor, "review", {"review_id": rid, "branch": s["snapshot"]["branch"],
                                               "revision": s["snapshot"]["revision"],
                                               "timeout_s": REVIEW_TIMEOUT_S + 300},
                             request_id=request_id, origin_prompt_id=origin_prompt_id)
        self._review_set(rid, job_id=job["job_id"], emit=True)
        out = self.review(rid, brief=True)
        out["next"] = ("the decision comes within seconds, the written review in a few minutes: wait for it with "
                       f"qccd_review(review_id={rid!r}, wait_s=50), which starts nothing new"
                       if who != "none" else "the decision and every rule's verdict come within seconds")
        return out

    # ------------------------------------------------------------------ the job

    def _job_review(self, jid, params, actor, origin_prompt_id, cancel) -> dict:
        from ..review import agents
        from ..review.engine import LOCAL_WORDS, run_review
        rid = params["review_id"]
        row = self._review_row(rid)
        s = self.submission(row["submission_id"])
        bundle = self.root / s["snapshot"]["dir"] / "bundle"
        rel = self._bundle_board(bundle)
        design = self._design_title_of(s["snapshot"]["branch"])
        self._review_set(rid, status="checking", started_at=_now(), emit=True)
        lines: list = []
        last = [0.0]
        lock = threading.Lock()

        def on_progress(new: list) -> None:
            with lock:
                for ln in new:
                    if ln and (not lines or lines[-1]["text"] != ln):
                        lines.append({"t": round(_now(), 1), "text": ln[:240]})
                del lines[:-120]
                if lines and _now() - last[0] > 1.5:
                    last[0] = _now()
                    self._review_set(rid, progress=list(lines))
                    self._progress(jid, "review", lines[-1]["text"])

        def on_decision(decision: dict, view, md: str) -> None:
            self._review_set(rid, decision=decision["decision"],
                             reasons={"reasons": decision["reasons"], "not_run": decision.get("not_run") or []},
                             rule_check=view, emit=True)

        def on_agent(info: dict) -> None:
            self._review_set(rid, status="reviewing", model=info.get("model"), emit=True)
        mode = None
        if row["reviewer"] != "none":
            mode = agents.status(row["reviewer"], isolation=row["isolation"])["mode"] or "local"
        work = Path(tempfile.gettempdir()) / "qccd-review" / f"{str(self.id)[:8]}-{rid}"
        res = run_review(job=work, bundle_dir=bundle, report=s["report"] or {}, board=rel.id, board_title=rel.title,
                         reviewer=row["reviewer"], isolation=row["isolation"], mode=mode, where="local",
                         keep=self._keep_dir(rid), python=sys.executable, cancel=cancel, on_progress=on_progress,
                         on_decision=on_decision, on_agent=on_agent)
        with lock:
            final = list(lines)
        self._review_set(rid, status=res["status"], review_md=res["review_md"] or None, usage=res["usage"],
                         error=res["error"], files=res["files"], egress=res["egress"], progress=final,
                         finished_at=_now(), emit=True)
        d = res["decision"] or {}
        said = LOCAL_WORDS.get(d.get("decision"), "no decision")
        why = "; ".join((d.get("reasons") or [])[:2])
        summary = (f"{design} on {rel.title}: {said}" + (f" ({why})" if why else "")
                   + ("" if row["reviewer"] == "none" else
                      (f"; {_title(row['reviewer'])}'s review is written" if res["status"] == "done"
                       else f"; the review did not finish: {res['error']}")))
        return {"_status": "succeeded" if d else "failed", "summary": summary, "review": self.review(rid, brief=True)}

    # ------------------------------------------------------------------ reading it back

    def review(self, rid: str, *, brief: bool = False) -> dict:
        """One review, as the Studio, the review page and an agent read it (`brief`: without the
        review's text and every rule's row)."""
        from ..review.engine import LOCAL_WORDS, REVIEW_FILES
        r = self._review_row(rid)
        out = {"review_id": rid, "submission_id": r["submission_id"], "reviewer": r["reviewer"],
               "reviewer_title": _title(r["reviewer"]), "model": r["model"], "isolation": r["isolation"],
               "status": r["status"], "decision": r["decision"],
               "decision_words": LOCAL_WORDS.get(r["decision"] or "", None),
               "reasons": (r["reasons"] or {}).get("reasons") or [], "not_run": (r["reasons"] or {}).get("not_run") or [],
               "error": r["error"], "usage": r["usage"] or {}, "egress": r["egress"], "job_id": r["job_id"],
               "created_at": r["created_at"], "started_at": r["started_at"], "finished_at": r["finished_at"],
               "progress": (r["progress"] or [])[-14 if brief else -120:], "page": f"/reviewview/{rid}",
               "where": "local", "poll_url": f"/api/reviews/{rid}?brief=1",
               "files": [{"name": n, "about": REVIEW_FILES.get(n, ""), "url": f"/api/reviews/{rid}/files/{n}"}
                         for n in (r["files"] or [])]}
        try:
            s = self.submission(r["submission_id"])
            rep = s["report"] or {}
            rel = self._bundle_board(self.root / s["snapshot"]["dir"] / "bundle")
            out["design"] = self._design_title_of(s["snapshot"]["branch"])
            out["board_title"], out["board_id"], out["stale"] = rel.title, rel.id, s["stale"]
            out["submission"] = {"submission_id": s["id"], "task": rel.id, "status": s["status"],
                                 "display_name": out["design"], "revision": s["snapshot"]["revision"],
                                 "report": {"eligible": (rep.get("eligibility") or {}).get("eligible")}}
        except Exception:
            out["submission"] = {"submission_id": r["submission_id"]}
        if not brief:
            out["review_md"] = r["review_md"]
            out["rule_check"] = r["rule_check"]
            md = self._keep_dir(rid) / "rule-check.md"
            out["rule_check_md"] = md.read_text(encoding="utf-8") if md.is_file() else None
            out["reviews"] = [{"review_id": x["id"], "reviewer": x["reviewer"], "status": x["status"],
                               "decision": x["decision"], "created_at": x["created_at"]}
                              for x in self.store.all("SELECT id, reviewer, status, decision, created_at FROM reviews "
                                                      "WHERE submission_id=? ORDER BY created_at DESC LIMIT 12",
                                                      (r["submission_id"],))]
        return out

    def reviews(self, submission_id: str | None = None, limit: int = 20) -> list:
        args: tuple = ()
        where = ""
        if submission_id:
            where, args = "WHERE submission_id=?", (submission_id,)
        rows = self.store.all(f"SELECT id FROM reviews {where} ORDER BY created_at DESC LIMIT ?",
                              (*args, max(1, min(int(limit), 100))))
        return [self.review(r["id"], brief=True) for r in rows]

    def latest_review(self, submission_id: str, *, reviewer: str | None = None, isolation: str | None = None,
                      finished: bool = False) -> dict | None:
        q = "SELECT id FROM reviews WHERE submission_id=?"
        args: list = [submission_id]
        if reviewer:
            q, args = q + " AND reviewer=?", args + [reviewer]
        if isolation:
            q, args = q + " AND isolation=?", args + [isolation]
        if finished:
            q += " AND status IN ('done','failed') AND decision IS NOT NULL"
        r = self.store.one(q + " ORDER BY created_at DESC LIMIT 1", tuple(args))
        return self.review(r["id"], brief=True) if r else None

    def review_file(self, rid: str, name: str) -> bytes:
        from ..review.engine import REVIEW_FILES
        from .core import WorkspaceError
        r = self._review_row(rid)
        if name not in REVIEW_FILES or name not in (r["files"] or []):
            raise WorkspaceError("not_found", "this review kept no such file", status=404)
        return (self._keep_dir(rid) / name).read_bytes()

    def cancel_review(self, actor: Mapping, rid: str) -> dict:
        r = self._review_row(rid)
        if r["job_id"]:
            self.cancel_job(actor, r["job_id"])
        return self.review(rid, brief=True)

    # ------------------------------------------------------------------ signing a reviewer in

    def _signin_table(self) -> dict:
        t = getattr(self, "_signins", None)
        if t is None:
            t = self._signins = {}
        return t

    def start_signin(self, actor: Mapping, reviewer: str, isolation: str | None = None) -> dict:
        """The person signs reviewer `reviewer` in from the Studio: its CLI's own sign-in starts here,
        and the page shows the link (and Codex's code) it prints."""
        from ..review import agents
        from .core import WorkspaceError
        if actor.get("kind") != "human":
            raise WorkspaceError("forbidden", "only the person signs a reviewer in", status=403)
        if reviewer not in agents.REVIEWERS:
            raise WorkspaceError("bad_request", f"reviewer is one of {', '.join(agents.REVIEWERS)}", status=422)
        iso = isolation or self.settings()["review_isolation"]
        # Codex's sign-in is the same file on this computer and in Docker
        iso = "sandbox" if reviewer == "codex" else iso
        for s in self._signin_table().values():
            if s.rid == reviewer and s.isolation == iso and s.state in ("starting", "waiting"):
                return s.view()
        try:
            s = agents.SigninSession(reviewer, iso).start()
        except Exception as exc:
            raise WorkspaceError("signin_failed", f"could not start the sign-in: {exc}", status=409) from None
        self._signin_table()[s.id] = s
        return s.view()

    def signin_view(self, sid: str) -> dict:
        from .core import WorkspaceError
        s = self._signin_table().get(sid)
        if s is None:
            raise WorkspaceError("unknown_signin", "no such sign-in", status=404)
        return s.view()

    def signin_send(self, actor: Mapping, sid: str, code: str) -> dict:
        from .core import WorkspaceError
        if actor.get("kind") != "human":
            raise WorkspaceError("forbidden", "only the person signs a reviewer in", status=403)
        s = self._signin_table().get(sid)
        if s is None:
            raise WorkspaceError("unknown_signin", "no such sign-in", status=404)
        try:
            s.send(code)
        except RuntimeError as exc:
            raise WorkspaceError("bad_state", str(exc), status=409) from None
        return s.view()

    def signin_cancel(self, actor: Mapping, sid: str) -> dict:
        s = self._signin_table().get(sid)
        if s is not None:
            s.cancel()
            return s.view()
        return {"signin_id": sid, "state": "cancelled"}
