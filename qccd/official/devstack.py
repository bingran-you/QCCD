"""A review stack on this computer: the official server, its grader and the agent reviewer, with
reviews signed in as the person at this keyboard (their own `claude` and `codex` sign-in).

    python -m qccd.official.devstack                      # then open http://127.0.0.1:8300/review
    python -m qccd.official.devstack --grader-python ~/venvs/qccd/bin/python --port 8301
    python -m qccd.official.devstack login codex          # sign a reviewer in, once (also: login claude)

Each reviewer runs from the review profile every workspace on this computer uses too
(~/.qccd/reviewers/, qccd/review/agents.py), signed in once with `login` (= `qccd review login`):
a review never sees the person's own agent instructions, memories, plugins, MCP servers or hooks,
and their usual CLI sign-in is untouched.  `--isolation docker` runs the server's agents in
containers (qccd/review/container.py).

It starts three children, and stops them on Ctrl+C:

    server     python -m qccd.official.service serve          QCCD_OFFICIAL_DEV=1: loopback only
    worker     python -m qccd.official.worker worker --inline  grades each submission
    reviewer   python -m qccd.official.worker reviewer        the rule check and decision, then the agent

Everything lives under --root (default ~/.qccd/devstack): the database, the stored bundles, the
grader's spool, each review's job folder (outside any repository, so the agent's project is that
folder and nothing else), the logs, and a credentials file a Studio can use to submit here:

    QCCD_OFFICIAL_URL=http://127.0.0.1:8300 QCCD_CREDENTIALS=~/.qccd/devstack/credentials.json qccd studio

The server is the same code qccd.academy runs; only the sign-ins differ (review.py, `local`).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _has(python: str, modules: str) -> bool:
    return subprocess.run([python, "-c", f"import {modules}"], capture_output=True).returncode == 0


def _qcheck(python: str) -> str | None:
    """The Lean checker the grader in `python` will use (evaluator.Toolchain.discover), if any."""
    cp = subprocess.run([python, "-c", "from qccd.workspace.evaluator import Toolchain; "
                         "print(Toolchain.discover().qcheck or '')"], capture_output=True, text=True, cwd=str(REPO))
    return (cp.stdout.strip() or None) if cp.returncode == 0 else None


def _get(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _write_credentials(path: Path, server: str, token: str, name: str) -> None:
    """The Studio's sign-in to this server (account.py's format), readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"token": token, "account": {"id": "dev", "name": name}, "label": "qccd devstack",
             "signed_in_at": time.time()}
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({server: entry}, f, indent=1)
    os.replace(tmp, path)


def login(rid: str, root: Path | None = None) -> int:
    """Sign reviewer `rid` in, into its review profile: the CLI's own sign-in, in this terminal (the
    same profile `qccd review login` signs in; `root` is not where it lives)."""
    from ..review import agents
    if rid not in agents.REVIEWERS:
        print(f"login takes one of: {', '.join(agents.REVIEWERS)}", file=sys.stderr)
        return 2
    try:
        return agents.login(rid)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["login"]:
        lp = argparse.ArgumentParser(prog="python -m qccd.official.devstack login")
        lp.add_argument("reviewer", choices=["codex", "claude"])
        lp.add_argument("--root", default=None, help=argparse.SUPPRESS)
        la = lp.parse_args(argv[1:])
        return login(la.reviewer)
    ap = argparse.ArgumentParser(prog="python -m qccd.official.devstack", description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8300)
    ap.add_argument("--root", default=os.environ.get("QCCD_DEVSTACK_ROOT", str(Path.home() / ".qccd" / "devstack")))
    ap.add_argument("--grader-python", default=sys.executable,
                    help="the Python that grades (one with qiskit, stim and pymatching runs every stage that needs "
                         "them; the default is this one)")
    ap.add_argument("--name", default=os.environ.get("USER", "you"), help="who the Studio submits as, on this server")
    ap.add_argument("--no-open", action="store_true", help="do not open the review page in a browser")
    ap.add_argument("--isolation", choices=["sandbox", "docker"], default=os.environ.get("QCCD_REVIEW_ISOLATION", "sandbox"),
                    help="where the server's agents run: their CLIs' own sandboxes, or Docker containers")
    a = ap.parse_args(argv)

    if not _has(sys.executable, "fastapi, uvicorn"):
        print("the server needs fastapi and uvicorn in this Python (pip install fastapi uvicorn)", file=sys.stderr)
        return 2
    root = Path(a.root).expanduser().resolve()
    if root == REPO or REPO in root.parents:
        print("--root must be outside the repository: each review's job folder is its agent's whole project",
              file=sys.stderr)
        return 2
    for d in ("artifacts", "spool", "reviews", "logs"):
        (root / d).mkdir(parents=True, exist_ok=True)
    server = f"http://127.0.0.1:{a.port}"
    db = f"sqlite:///{root / 'official.db'}"
    token = "qccd_dev_" + secrets.token_urlsafe(24)
    env = {**os.environ, "QCCD_OFFICIAL_DEV": "1", "QCCD_OFFICIAL_DEV_TOKEN": token, "QCCD_REVIEW": "1",
           "QCCD_REVIEW_LOCAL_LOGINS": "1", "QCCD_OFFICIAL_DB": db, "QCCD_OFFICIAL_ARTIFACTS": str(root / "artifacts"),
           "QCCD_OFFICIAL_SPOOL": str(root / "spool"), "QCCD_REVIEW_WORK": str(root / "reviews"),
           "QCCD_REVIEW_ISOLATION": a.isolation, "PYTHONUNBUFFERED": "1"}
    common = ["--db", db, "--artifacts", str(root / "artifacts")]
    specs = {
        "server": [sys.executable, "-m", "qccd.official.service", "serve", "--port", str(a.port), *common],
        "worker": [a.grader_python, "-m", "qccd.official.worker", "worker", "--inline", *common,
                   "--spool", str(root / "spool")],
        "reviewer": [sys.executable, "-m", "qccd.official.worker", "reviewer", *common, "--work", str(root / "reviews"),
                     "--python", a.grader_python],
    }
    procs: dict = {}
    logs: dict = {}

    # Ctrl+C, `kill` and a closed terminal all stop the stack and its children (a shell starts a
    # background job with SIGINT ignored: put it back)
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, signal.default_int_handler)
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, stop)
    def start(name: str) -> None:
        logs[name] = open(root / "logs" / f"{name}.log", "ab", buffering=0)
        procs[name] = subprocess.Popen(specs[name], cwd=str(REPO), env=env, stdout=logs[name], stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL)
    try:
        # the server first: it makes the database; then the two that work from it
        start("server")
        for _ in range(120):
            if procs["server"].poll() is not None:
                break
            try:
                _get(f"{server}/v1/health")
                break
            except OSError:
                time.sleep(0.25)
        else:
            print(f"the server did not come up: see {root / 'logs' / 'server.log'}", file=sys.stderr)
            return 1
        if procs["server"].poll() is not None:
            print(f"the server stopped at start: see {root / 'logs' / 'server.log'}", file=sys.stderr)
            return 1
        start("worker")
        start("reviewer")
        creds = root / "credentials.json"
        _write_credentials(creds, server, token, f"{a.name} (local)")
        rv = _get(f"{server}/v1/reviewers", timeout=60)
        print(f"QCCD review stack on {server}  (everything under {root})\n")
        print(f"  Submit a design for review:  {server}/review")
        for r in rv.get("reviewers", []):
            mark = "ready" if r.get("available") else "not ready"
            print(f"  {r['title']:8s} {r.get('model') or '':18s} {mark}: "
                  + (f"signed in for reviews ({r.get('account')})" if r.get("available") else (r.get("why") or "")))
        if not _has(a.grader_python, "qiskit, stim, pymatching"):
            print("\n  The grader's Python lacks qiskit, stim or pymatching: the certificate binding, semantics and LER "
                  "stages are `unsupported`,\n  so no decision can be final here. Pass --grader-python with the agent "
                  "extras (pip install -e \".[agent]\") for a fuller grade.")
        if not _qcheck(a.grader_python):
            print("\n  No Lean checker (qcheck) here: `lean_certificate` is unsupported, so a design that passes everything "
                  "else is\n  \"not decided on this server\". Build it once with `python -m qccd.workspace toolchain "
                  "build-checker` (it needs Lean:\n  `brew install elan-init` on a Mac); on Windows and Linux, `python -m "
                  "qccd.workspace toolchain install` fetches it.")
        print(f"\n  A Studio can submit here too:\n    QCCD_OFFICIAL_URL={server} QCCD_CREDENTIALS={creds} "
              f"python -m qccd.workspace studio --root <a workspace>\n")
        print(f"  Logs: {root / 'logs'}   ·   Ctrl+C stops the stack\n", flush=True)
        if not a.no_open:
            webbrowser.open(f"{server}/review")
        while True:
            for name, p in procs.items():
                if p.poll() is not None:
                    print(f"{name} stopped (exit {p.returncode}): see {root / 'logs' / (name + '.log')}", file=sys.stderr)
                    return 1
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopping…", flush=True)
        return 0
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in logs.values():
            f.close()


if __name__ == "__main__":
    raise SystemExit(main())
