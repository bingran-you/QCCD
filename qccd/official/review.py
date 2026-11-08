"""The official server's side of agent review: it decides every submission it is asked to from its
own grade, and runs an agent only when it holds a key for one.

The person's own computer reviews a design before it is submitted -- their own Claude Code or
Codex, signed in with their own subscription (the workspace's `review` job, qccd/review/).  That
review cannot decide anything here: it ran where the server cannot see.  So once the grader has
graded a submission whose upload asked for it (`X-QCCD-Reviewer`), this server:

  1. runs the same RULE CHECK and makes the same DECISION (qccd.review.engine.decide) from its own
     grade -- accepted, not accepted, held for a maintainer, or undetermined here.  No agent, no
     key, no model (`none`, what a Studio asks for);
  2. and, if the upload named an agent this server can run, lets it write the review.

How a server's agent signs in (a reviewer's `mode`):

  api_key   the server's own key (QCCD_REVIEW_ANTHROPIC_API_KEY, QCCD_REVIEW_OPENAI_API_KEY), given
            to the agent's process and nothing else.  What a public server would use.
  local     the agent CLI's own sign-in in its review profile on THIS computer -- only on a
            development server (QCCD_OFFICIAL_DEV=1, which serves loopback only) that asks for it
            (QCCD_REVIEW_LOCAL_LOGINS=1).  A public server must not offer people's claude.ai logins
            ("Unless previously approved, Anthropic does not allow third party developers to offer
            claude.ai login or rate limits for their products", the Agent SDK's terms), and runs on
            ChatGPT plans only through OpenAI's Sign in with ChatGPT program.

QCCD_REVIEW_ISOLATION=docker runs the server's agents in containers (qccd/review/container.py)
instead of their CLIs' own sandboxes.

    python -m qccd.official.worker reviewer --db ... --artifacts ... --work DIR
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

from ..review import agents
from ..review.agents import REVIEWERS, agent_command
from ..review.engine import (DECISION_WORDS, NO_AGENT, REVIEW_FILES, REVIEW_TIMEOUT_S, decide, parse_event,
                             review_prompt, run_review, run_streaming)

# the engine's names a caller of this module used before it moved (qccd/review/) are kept here
__all__ = ["REVIEWERS", "REVIEW_STATES", "NO_AGENT", "REVIEW_FILES", "DECISION_WORDS", "reviews_enabled",
           "local_logins_allowed", "reviewer_status", "reviewer_statuses", "decide", "process_review", "run_reviewer",
           "claim_review", "isolation", "agent_command", "parse_event", "review_prompt", "run_streaming"]

REVIEW_STATES = ("queued", "checking", "reviewing", "done", "failed")
#: a reviewer that stops sending heartbeats for this long has died: its review is queued again
STALE_S = 900
MAX_ATTEMPTS = 2
MAX_PROGRESS = 120


def _now() -> float:
    return time.time()


def reviews_enabled() -> bool:
    return os.environ.get("QCCD_REVIEW", "") == "1"


def local_logins_allowed() -> bool:
    """A person's own CLI sign-in, on their own computer: a development server only."""
    return os.environ.get("QCCD_OFFICIAL_DEV") == "1" and os.environ.get("QCCD_REVIEW_LOCAL_LOGINS") == "1"


def isolation() -> str:
    v = os.environ.get("QCCD_REVIEW_ISOLATION", "sandbox")
    return v if v in agents.ISOLATIONS else "sandbox"


def reviewer_status(rid: str, *, fresh: bool = False) -> dict:
    """Whether this server can run reviewer `rid` now, how it signs in, and if not, why not."""
    out = agents.status(rid, isolation=isolation(), allow_api_key=True, allow_local=local_logins_allowed(),
                        fresh=fresh, here="this server")
    if not reviews_enabled():
        out.update(available=False, mode=None, account=None, why="agent reviews are off on this server")
    return out


def reviewer_statuses(*, fresh: bool = False) -> list[dict]:
    return [reviewer_status(r, fresh=fresh) for r in REVIEWERS]


# ---------------------------------------------------------------------- one review

class _Progress:
    """The agent's progress lines into the review row, at most every couple of seconds, and the
    heartbeat with them."""

    def __init__(self, svc, rid: str):
        self.svc, self.rid, self.lines, self.last, self.lock = svc, rid, [], 0.0, threading.Lock()

    def add(self, lines: list[str], *, force: bool = False) -> None:
        with self.lock:
            for ln in lines:
                if ln and (not self.lines or self.lines[-1]["text"] != ln):
                    self.lines.append({"t": round(_now(), 1), "text": ln[:240]})
            del self.lines[:-MAX_PROGRESS]
            if force or _now() - self.last > 2.0:
                self.last = _now()
                self.svc.db.execute("UPDATE reviews SET progress=?, heartbeat=? WHERE id=?",
                                    (json.dumps(self.lines), _now(), self.rid))


def _set(svc, rid: str, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    svc.db.execute(f"UPDATE reviews SET {cols} WHERE id=?", (*fields.values(), rid))


def process_review(svc, row: dict, work_root: Path, *, python: str = sys.executable,
                   timeout: float = REVIEW_TIMEOUT_S, cancel: threading.Event | None = None) -> str:
    """Run one claimed review to the end: the rule check and the decision, then the agent.  The
    review's final status ('done' or 'failed')."""
    rid = row["id"]
    sub = svc.db.one("SELECT * FROM submissions WHERE id=?", (row["submission"],))
    rep = svc.db.one("SELECT report FROM reports WHERE submission=? AND superseded_by IS NULL "
                     "ORDER BY created_at DESC LIMIT 1", (row["submission"],))
    if sub is None or rep is None:
        _set(svc, rid, status="failed", error="the submission has no grade to review", finished_at=_now())
        return "failed"
    report = json.loads(rep["report"])
    rel = svc.releases.get(sub["task"])
    prog = _Progress(svc, rid)
    # an agent this server cannot run now: the decision is still made, and the page says why there
    # is no review
    st = reviewer_status(row["reviewer"], fresh=True) if row["reviewer"] != NO_AGENT else None
    reviewer = row["reviewer"] if st is None or st["available"] else NO_AGENT

    def on_decision(decision: dict, view, md: str) -> None:
        _set(svc, rid, decision=decision["decision"],
             reasons=json.dumps({"reasons": decision["reasons"], "not_run": decision.get("not_run") or []}),
             rule_check=json.dumps(view) if view else None, rule_check_md=md or None, heartbeat=_now())

    def on_agent(info: dict) -> None:
        _set(svc, rid, status="reviewing", model=info.get("model"), heartbeat=_now())
    prog.add(["Waiting for the rule check"], force=True)
    res = run_review(job=Path(work_root) / rid, archive=(svc.artifacts / f"{sub['archive_sha256']}.zip").read_bytes(),
                     report=report, board=sub["task"], board_title=rel.title if rel is not None else sub["task"],
                     reviewer=reviewer, isolation=isolation(), mode=(st or {}).get("mode"), where="server",
                     keep=svc.artifacts / "reviews" / rid, python=python, timeout=timeout, cancel=cancel,
                     on_progress=prog.add, on_decision=on_decision, on_agent=on_agent,
                     beat=lambda: svc.db.execute("UPDATE reviews SET heartbeat=? WHERE id=?", (_now(), rid)))
    prog.add([], force=True)
    if st is not None and not st["available"] and res["decision"]:
        res.update(status="failed", error=f"{st['title']} cannot review here: {st['why']}")
    _set(svc, rid, review_md=res["review_md"] or None, usage=json.dumps(res["usage"]), status=res["status"],
         error=res["error"], finished_at=_now(), heartbeat=_now())
    return res["status"]


# ---------------------------------------------------------------------- the reviewer

def claim_review(svc) -> dict | None:
    db = svc.db
    with db.tx() as t:
        row = t.one(db.claim_review_sql())
        if row is None:
            return None
        t.execute("UPDATE reviews SET status='checking', attempts=attempts+1, locked_by=?, locked_at=?, heartbeat=?, "
                  "started_at=? WHERE id=? AND status='queued'",
                  (f"{socket.gethostname()}:{os.getpid()}", _now(), _now(), _now(), row["id"]))
    return db.one("SELECT * FROM reviews WHERE id=?", (row["id"],))


def _sweep(svc) -> None:
    """Reviews whose reviewer died go back in the queue (or fail after MAX_ATTEMPTS); reviews of a
    submission the grader gave up on fail."""
    for r in svc.db.all("SELECT id, attempts FROM reviews WHERE status IN ('checking', 'reviewing') AND heartbeat<?",
                        (_now() - STALE_S,)):
        svc.db.execute("UPDATE reviews SET status=?, locked_by=NULL, error=? WHERE id=?",
                       ("queued" if r["attempts"] < MAX_ATTEMPTS else "failed", "the reviewer holding it stopped", r["id"]))
    svc.db.execute("UPDATE reviews SET status='failed', error='the grader could not grade the submission', finished_at=? "
                   "WHERE status='queued' AND submission IN (SELECT id FROM submissions WHERE status='grading_failed')",
                   (_now(),))


def run_reviewer(svc, work: Path, *, python: str = sys.executable, once: bool = False,
                 stop: threading.Event | None = None) -> None:
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    while stop is None or not stop.is_set():
        _sweep(svc)
        row = claim_review(svc)
        if row is not None:
            status = process_review(svc, row, work, python=python, cancel=stop)
            print(f"[reviewer] {row['id']} ({row['reviewer']}) on {row['submission']}: {status}", flush=True)
        if once:
            return
        if row is None:
            time.sleep(1.0)
