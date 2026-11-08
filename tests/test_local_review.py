"""A design reviewed on the person's own computer before it is submitted (qccd/workspace/reviews.py,
on the engine in qccd/review/).

The decision is made at once, from the local grade and a check of every rule -- the same decision
the official server makes again from its own grade -- and the person's own reviewer (Claude Code or
Codex, signed in with their subscription in a review profile of its own) then writes the review, in
its CLI's sandbox or in Docker.  Every door starts the same job: the Studio, `qccd review`, an
agent's `qccd_review`; and the Studio's Submit reviews first.  A reviewer that is not signed in is
refused with what the person does; only the person signs one in.  A Docker review reaches only its
model's API, through the egress proxy; one that leaves the computer must be a Docker review with no
credential in it, and the server keeps it beside its own decision, marked as the submitter's.

The stand-in agent of tests/test_agent_review.py plays both CLIs; local tests are frozen from the
example bundles and graded with the reference evaluator, so nothing here compiles or reaches a model.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from qccd.official.review_pages import example_archive, review_page
from qccd.review import agents as A
from qccd.review import container as C
from test_agent_review import FAKE

REPO = Path(__file__).resolve().parents[1]
PERSON = {"kind": "human", "id": "cli", "label": "terminal"}
AGENT = {"kind": "agent", "id": "agent:mcp"}


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    """Both reviewers are the stand-in, signed in on this computer; Docker is absent."""
    d = tmp_path / "bin"
    d.mkdir()
    p = d / "fake-agent"
    p.write_text(FAKE.replace("{python}", sys.executable), encoding="utf-8")
    p.chmod(0o755)
    for k in ("QCCD_REVIEW_CLAUDE_BIN", "QCCD_REVIEW_CODEX_BIN"):
        monkeypatch.setenv(k, str(p))
    monkeypatch.setenv("QCCD_HOME", str(tmp_path / "qccd"))
    monkeypatch.setenv("QCCD_DOCKER", "none")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for k in ("QCCD_REVIEW_ANTHROPIC_API_KEY", "QCCD_REVIEW_OPENAI_API_KEY", "QCCD_REVIEW_CODEX_HOME",
              "QCCD_REVIEW_CLAUDE_CONFIG_DIR", "QCCD_REVIEW_CLAUDE_DOCKER_DIR", "QCCD_REVIEW_CODEX_MODEL"):
        monkeypatch.delenv(k, raising=False)
    for r in ("codex", "claude"):
        (tmp_path / "qccd" / "reviewers" / r).mkdir(parents=True)
    A._STATUS.clear()
    C._READY.clear()

    def mode(m: str) -> None:
        (d / "mode").write_text(m, encoding="utf-8")
        A._STATUS.clear()
    mode("ok")
    yield {"mode": mode, "seen": lambda: json.loads((d / "argv.json").read_text(encoding="utf-8"))}
    A._STATUS.clear()
    C._READY.clear()


@pytest.fixture
def ws(tmp_path):
    from qccd.workspace.app import Workspace
    w = Workspace.init(tmp_path / "ws", "ghz4@1")
    yield w
    w.close()


def _local_test(ws, example: str, *, eligible: bool | None = None) -> str:
    """A graded local submission of an example bundle, recorded as `submit_design` leaves one (a
    frozen snapshot of the design's head, its reference report) -- graded here without compiling.
    `eligible` overrides the grade's verdict (a machine without the Lean checker cannot pass)."""
    from qccd.workspace.bundle import extract_archive, read_bundle
    from qccd.workspace.core import new_id
    from qccd.workspace.evaluator import grade
    from qccd.workspace.jsonsafe import digest
    from qccd.workspace.store import dumps
    task, archive = example_archive(example)
    sid = new_id("snap")
    d = ws.snapshots_dir / sid
    extract_archive(archive, d / "bundle")
    b = read_bundle(d / "bundle")
    rel = ws._bundle_board(d / "bundle")
    report = grade(rel, d / "bundle", "reference", workdir=ws.root / ".qccd" / "runs" / sid)
    if eligible is not None:
        report["eligibility"] = {"eligible": eligible, "reasons": [] if eligible else ["not eligible"]}
        for st in report["stages"]:
            st["status"] = "passed" if eligible and st.get("required") else st["status"]
    (d / "report.json").write_text(json.dumps(report), encoding="utf-8")
    head = ws.head("main")
    jid, sub = new_id("job"), new_id("sub")
    now = time.time()
    with ws.store.tx() as db:
        db.execute("INSERT INTO snapshots(id, branch, revision, input_digest, bundle_digest, dir, created_at, task_release, "
                   "task_digest) VALUES(?,?,?,?,?,?,?,?,?)", (sid, "main", head.revision, head.input_digest, b.digest,
                                                             str(d.relative_to(ws.root)), now, rel.id, rel.digest))
        db.execute("INSERT INTO jobs(id, kind, snapshot_id, profile, status, actor, created_at, result) "
                   "VALUES(?, 'evaluate', ?, 'reference', 'succeeded', ?, ?, ?)",
                   (jid, sid, dumps(PERSON), now, dumps({"report_path": str((d / "report.json").relative_to(ws.root))})))
        db.execute("INSERT INTO submissions(id, snapshot_id, job_id, profile, task_release, task_digest, status, report_digest, "
                   "actor, created_at) VALUES(?,?,?, 'reference', ?, ?, ?, ?, ?, ?)",
                   (sub, sid, jid, rel.id, rel.digest, "eligible" if report["eligibility"]["eligible"] else "ineligible",
                    digest(report), dumps(PERSON), now))
    return sub


def _wait(ws, job_id: str, timeout: float = 60) -> dict:
    t0 = time.time()
    while ws.job(job_id)["status"] in ("queued", "running"):
        assert time.time() - t0 < timeout, "the job did not finish"
        time.sleep(0.2)
    return ws.job(job_id)


# ---------------------------------------------------------------------- the review job

@pytest.mark.parametrize("reviewer", ["codex", "claude"])
def test_a_review_here_decides_then_explains(stand_in, ws, reviewer):
    sub = _local_test(ws, "ghz-chain4-hot")
    r = ws.start_review(PERSON, submission_id=sub, reviewer=reviewer)
    assert r["status"] == "queued" and r["reviewer"] == reviewer and r["isolation"] == "sandbox"
    job = _wait(ws, r["job_id"])
    rv = ws.review(r["review_id"])
    assert job["status"] == "succeeded" and rv["status"] == "done" and rv["decision"] == "not_accepted"
    assert any(x.startswith("breaks R7") for x in rv["reasons"]) and rv["review_md"].startswith("# Review")
    assert "Not accepted" in job["result"]["summary"] and "review is written" in job["result"]["summary"]
    # the fix the rule check tested, the review and the rule check, kept with the workspace
    assert [f["name"] for f in rv["files"]] == ["recooled.tsir.json"]
    keep = ws.root / ".qccd" / "reviews" / r["review_id"]
    assert {"review.md", "rule-check.md", "rule-check.json", "recooled.tsir.json"} <= {p.name for p in keep.iterdir()}
    assert sum(1 for i in json.loads(ws.review_file(r["review_id"], "recooled.tsir.json"))["instructions"]
               if i["type"] == "cool") > 0
    # the agent: in its profile, outside the workspace (whose CLAUDE.md and AGENTS.md are the co-design skill's),
    # with nothing of the person's environment but where the skill's script finds the checkout
    seen = stand_in["seen"]()
    assert [k for k in seen["env"] if k.startswith("QCCD_")] == ["QCCD_REPO"]
    if reviewer == "codex":
        cd = Path(seen["argv"][seen["argv"].index("--cd") + 1])
        assert ws.root not in cd.parents and not cd.exists()      # a temporary folder, gone afterwards
    page = review_page(ws.review(r["review_id"]), "n0nce")
    assert "Back to the Studio" in page and "Decided on this computer" in page and "/api/reviews/" in page


def test_without_a_reviewer_the_decision_still_comes(stand_in, ws):
    """Nobody chosen (the person never picked a reviewer): the decision and every rule's verdict, no model."""
    sub = _local_test(ws, "bell-on-ghz")
    r = ws.start_review(AGENT, submission_id=sub)
    _wait(ws, r["job_id"])
    rv = ws.review(r["review_id"])
    assert rv["reviewer"] == "none" and rv["status"] == "done" and rv["review_md"] is None
    assert rv["decision"] == "not_accepted" and "2-qubit circuit" in rv["reasons"][0] and rv["rule_check"]["rules"]
    # the person's choices are theirs: an agent cannot change who reviews
    from qccd.workspace.core import WorkspaceError
    with pytest.raises(WorkspaceError):
        ws.set_settings({"reviewer": "codex"}, AGENT)
    assert ws.set_settings({"reviewer": "codex", "review_isolation": "docker"}, PERSON)["review_isolation"] == "docker"


def test_a_reviewer_not_signed_in_is_refused_with_what_the_person_does(stand_in, ws, tmp_path):
    from qccd.workspace.core import WorkspaceError
    sub = _local_test(ws, "ghz-chain4")
    stand_in["mode"]("loggedout")
    with pytest.raises(WorkspaceError) as e:
        ws.start_review(AGENT, submission_id=sub, reviewer="codex")
    assert e.value.code == "needs_signin" and e.value.detail["signin"] == "qccd review login codex"
    assert "an agent cannot" in str(e.value)
    # Docker is not here at all; when it is, Claude Code in a container needs the token one sign-in keeps
    # (`claude setup-token`: a container reads no Keychain), and that token serves both isolations
    st = A.status("claude", isolation="docker", fresh=True)
    assert not st["available"] and "Docker is not installed" in st["why"]
    C._READY["v"] = (time.time() + 3600, (True, ""))
    st = A.status("claude", isolation="docker", fresh=True)
    assert not st["available"] and st["signin"] == "qccd review login claude"
    A.save_claude_token("sk-ant-oat01-" + "x" * 95)
    assert A.status("claude", isolation="docker", fresh=True)["available"]
    assert A.status("claude", fresh=True)["account"].startswith("a Claude subscription token")
    with pytest.raises(ValueError):
        A.save_claude_token("not a token")
    # a profile never signed in is not made by asking (the Studio asks on every panel refresh)
    A._STATUS.clear()
    os.rename(tmp_path / "qccd" / "reviewers" / "codex", tmp_path / "codex-away")
    assert not A.status("codex", fresh=True)["available"] and not (tmp_path / "qccd" / "reviewers" / "codex").exists()


# ---------------------------------------------------------------------- the doors

def test_the_studio_the_agent_and_the_cli_start_the_same_review(stand_in, ws):
    from starlette.testclient import TestClient
    from qccd.workspace import features as F
    from qccd.workspace.service import ServiceState, create_app
    sub = _local_test(ws, "ghz-chain4-hot")
    state = ServiceState(ws, {"port": 8765, "agent_token": "agent-tok", "owner_token": "owner-tok", "pid": os.getpid()})
    app = create_app(state)
    http = TestClient(app, base_url="http://127.0.0.1:8765")
    agent = {"Authorization": "Bearer agent-tok"}
    owner = {"Authorization": "Bearer owner-tok"}
    # an agent's qccd_review, waiting for the answer: the decision, the reasons, the review, every failing rule
    class Backend:
        def call(self, method, path, body=None, timeout=60.0):
            r = http.request(method, path, json=body, headers=agent)
            assert r.status_code < 400, r.text
            return r.json()
    out = F.BY_NAME["qccd_review"].call(Backend(), {"submission_id": sub, "reviewer": "claude", "wait_s": 30})
    assert out["status"] == "done" and out["decision"] == "not_accepted" and out["review_md"].startswith("# Review")
    assert {x["id"] for x in out["failing_rules"]} >= {"R7"}
    # the reviewers and the person's choices; signing a reviewer in is the person's alone
    v = http.get("/api/reviewers", headers=agent).json()
    assert [r["id"] for r in v["reviewers"]["sandbox"]] == ["claude", "codex"] and not v["docker"]["ready"]
    assert http.post("/api/reviewers/codex/signin", json={}, headers=agent).status_code == 403
    # the page: the paired person's (here the terminal's token), under its own nonce'd policy
    page = http.get(f"/reviewview/{out['review_id']}", headers=owner)
    assert page.status_code == 200 and "nonce-" in page.headers["content-security-policy"] and "Not accepted" in page.text
    assert http.get(f"/reviewview/{out['review_id']}").status_code == 401
    got = http.get(f"/api/reviews/{out['review_id']}/files/recooled.tsir.json", headers=owner)
    assert got.status_code == 200 and "attachment" in got.headers["content-disposition"]
    # the same job from the CLI's door (the terminal's token), one at a time per reviewer
    a = http.post("/api/reviews", json={"submission_id": sub, "reviewer": "none"}, headers=owner).json()
    _wait(ws, a["job_id"])
    assert ws.review(a["review_id"])["decision"] == "not_accepted"
    assert [r["reviewer"] for r in ws.reviews(sub)] == ["none", "claude"]


def test_submit_reviews_first_then_sends_and_the_server_decides(stand_in, ws, tmp_path, monkeypatch):
    """The Studio's Submit with a reviewer chosen: reviewed here first (the request waits, `reviewing`),
    then sent with `X-QCCD-Reviewer: none` -- the server's own decision -- and the card shows both.
    A review written outside Docker stays on this computer even when the person asked to send it."""
    import qccd.workspace.account as account
    server = "http://127.0.0.1:9"
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({server: {"token": "qccdk_x", "account": {"id": "7", "name": "Ada"}}}), encoding="utf-8")
    monkeypatch.setenv("QCCD_CREDENTIALS", str(creds))
    monkeypatch.setenv("QCCD_OFFICIAL_URL", server)
    sent: list = []

    def official(method, path, body=None, *, raw=None, token=None, server=None, headers=None, timeout=None, **kw):
        sent.append((method, path, dict(headers or {}), body))
        if method == "POST" and path == "/v1/submissions":
            return {"submission_id": "os_y", "by": "Ada", "status": "queued"}
        if path == "/v1/reviewers":
            return {"enabled": True, "reviewers": []}
        if path == "/v1/submissions/os_y":
            return {"submission_id": "os_y", "status": "eligible", "by": "Ada", "report": {"eligible": True},
                    "review": {"id": "rv_s", "reviewer": "none", "status": "done", "decision": "accepted",
                               "page": "/review/os_y"}}
        return {"tasks": [{"id": "ghz4@1"}], "rows": []}
    monkeypatch.setattr(account, "official_call", official)
    ws.set_settings({"reviewer": "codex"}, PERSON)
    sub = _local_test(ws, "ghz-chain4", eligible=True)
    p = ws.request_publication(PERSON, board="ghz4", submission_id=sub, attach_review=True)
    assert p["state"] == "reviewing" and p["review"]["reviewer"] == "codex"
    t0 = time.time()
    while ws.publication(p["publication_id"])["state"] in ("reviewing", "uploading"):
        assert time.time() - t0 < 60
        time.sleep(0.2)
    p = ws._pub_json(ws._fetch_official(ws._pub_row(p["publication_id"])))
    assert p["state"] == "uploaded" and p["review"]["decision"] == "accepted" and p["review"]["status"] == "done"
    post = next(h for m, path, h, b in sent if m == "POST" and path == "/v1/submissions")
    assert post["X-QCCD-Reviewer"] == "none"
    assert p["official"]["review"]["decision"] == "accepted"
    assert p["official"]["attached"] == {"sent": False, "why": "only a review written in Docker goes with a submission"}
    assert not any(path.endswith("/submitter-review") for m, path, h, b in sent)
    # asked again, the same graded design is not sent twice
    assert ws.request_publication(PERSON, board="ghz4", submission_id=sub)["duplicate"]


def test_a_review_leaves_the_computer_only_from_docker_and_without_credentials(stand_in, ws):
    sub = _local_test(ws, "ghz-chain4")
    now = time.time()
    with ws.store.tx() as db:
        for rid, iso, text in (("rv_doc", "docker", "# Review\n\nAll good.\n" * 20),
                               ("rv_leak", "docker", "# Review\n\nkey sk-ant-api03-" + "x" * 40 + "\n" * 20),
                               ("rv_sbx", "sandbox", "# Review\n\nAll good.\n" * 20)):
            db.execute("INSERT INTO reviews(id, submission_id, reviewer, isolation, status, decision, review_md, actor, "
                       "created_at) VALUES(?,?, 'claude', ?, 'done', 'accepted', ?, '{}', ?)", (rid, sub, iso, text, now))
    assert ws._attachable("rv_doc") == (True, "")
    ok, why = ws._attachable("rv_leak")
    assert not ok and "credential" in why
    assert ws._attachable("rv_sbx")[0] is False


def test_a_review_cut_off_by_a_restart_ends_failed_with_its_decision(stand_in, ws):
    """The service stopped while a review ran (a restart onto new code, a crash): when it starts
    again the review is `failed`, not `reviewing` forever, and the decision it made stands."""
    from qccd.workspace.app import Workspace
    sub = _local_test(ws, "ghz-chain4-hot")
    now = time.time()
    with ws.store.tx() as db:
        db.execute("INSERT INTO jobs(id, kind, status, pid, actor, created_at) VALUES('job_cut', 'review', 'running', "
                   "999999, '{}', ?)", (now,))
        db.execute("INSERT INTO reviews(id, submission_id, job_id, reviewer, isolation, status, decision, actor, "
                   "created_at) VALUES('rv_cut', ?, 'job_cut', 'codex', 'docker', 'reviewing', 'not_accepted', '{}', ?)",
                   (sub, now))
    root = ws.root
    ws.close()
    again = Workspace(root, recover=True)
    try:
        r = again.review("rv_cut")
        assert r["status"] == "failed" and r["decision"] == "not_accepted" and "stopped" in r["error"]
    finally:
        again.close()


def test_what_a_dead_review_left_in_docker_is_swept_and_nothing_else(monkeypatch):
    calls: list = []
    listing = {("ps", "-a"): "qccd-rv-a\t999999\nqccd-rv-b\t" + str(os.getpid()) + "\n",
               ("network", "ls"): "qccd-rv-a-net\t999999\nother-net\t\n"}

    class P:
        def __init__(self, out):
            self.stdout, self.returncode = out, 0

    def docker(*args, check=True, timeout=60):
        calls.append(args)
        return P(listing.get(args[:2], ""))
    monkeypatch.setattr(C, "_docker", docker)
    monkeypatch.setattr(C, "_alive", lambda pid: pid == os.getpid())
    assert C.sweep() == ["qccd-rv-a", "qccd-rv-a-net"]
    assert ("rm", "-f", "qccd-rv-a") in calls and ("network", "rm", "qccd-rv-a-net") in calls
    assert not any("qccd-rv-b" in c or "other-net" in c for c in calls if c[0] in ("rm", "network") and c[1] in ("-f", "rm"))


# ---------------------------------------------------------------------- Docker

def test_a_docker_review_runs_walled_in(stand_in, tmp_path, monkeypatch):
    """The container: the review's own --internal network (its only way out is the egress proxy),
    a read-only root, no capabilities, the person's uid, the review's folder and profile only; the
    CLI's own sandbox off (the container is the sandbox); a key, when there is one, by name only."""
    box = C.Isolated("codex", tmp_path / "job", mode="local")
    box.image = "qccd-reviewer:test"
    monkeypatch.setattr(C, "docker_bin", lambda: "/usr/bin/docker")
    argv, env = box.command("review it")
    i = argv.index("qccd-reviewer:test")
    opts, cli = argv[:i], argv[i + 1:]
    assert opts[opts.index("--network") + 1] == box.net and "--read-only" in opts
    assert opts[opts.index("--cap-drop") + 1] == "ALL" and "no-new-privileges" in opts
    assert f"{tmp_path / 'job'}:/job" in opts and any(o.endswith(":/profile") for o in opts)
    assert "HTTPS_PROXY=http://egress:3128" in opts and "CODEX_HOME=/profile" in opts
    assert cli[cli.index("--sandbox") + 1] == "danger-full-access" and cli[-1] == "review it"
    assert not any("QCCD_OFFICIAL" in o or "TOKEN" in o for o in opts)
    monkeypatch.setenv("QCCD_REVIEW_ANTHROPIC_API_KEY", "sk-ant-secret")
    box = C.Isolated("claude", tmp_path / "job", mode="api_key")
    box.image = "qccd-reviewer:test"
    argv, env = box.command("review it")
    assert "sk-ant-secret" not in " ".join(argv) and env["ANTHROPIC_API_KEY"] == "sk-ant-secret"
    assert argv[argv.index("ANTHROPIC_API_KEY") - 1] == "-e"
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["sandbox"]["enabled"] is False and "Read(/profile/**)" in settings["permissions"]["deny"]
    # the subscription token goes in the same way: by name, never in an argv
    monkeypatch.delenv("QCCD_REVIEW_ANTHROPIC_API_KEY")
    A.save_claude_token("sk-ant-oat01-" + "z" * 95)
    box = C.Isolated("claude", tmp_path / "job", mode="local")
    box.image = "qccd-reviewer:test"
    argv, env = box.command("review it")
    assert "z" * 20 not in " ".join(argv) and env["CLAUDE_CODE_OAUTH_TOKEN"].endswith("z" * 95)
    assert argv[argv.index("CLAUDE_CODE_OAUTH_TOKEN") - 1] == "-e"
    assert C._hosts("codex") == ["chatgpt.com", "auth.openai.com", "api.openai.com"]


def test_the_egress_proxy_lets_through_only_the_allowed_hosts(tmp_path):
    """deploy/review/egress_proxy.py: CONNECT to an allowed host (and port) goes through; anything else
    -- another host, another port, plain HTTP -- is refused, and every request is logged."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("egress_proxy", REPO / "deploy" / "review" / "egress_proxy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(4)
    up_port = upstream.getsockname()[1]

    def echo():
        while True:
            try:
                c, _ = upstream.accept()
            except OSError:
                return
            c.sendall(b"hello from upstream")
            c.close()
    threading.Thread(target=echo, daemon=True).start()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    port: list = []

    def serve():
        asyncio.set_event_loop(loop)

        async def go():
            srv = await asyncio.start_server(lambda r, w: mod.handle(r, w, ["localhost", "127.0.0.1"], {str(up_port)}),
                                             "127.0.0.1", 0)
            port.append(srv.sockets[0].getsockname()[1])
            ready.set()
            async with srv:
                await srv.serve_forever()
        try:
            loop.run_until_complete(go())
        except RuntimeError:
            pass
    threading.Thread(target=serve, daemon=True).start()
    assert ready.wait(10)

    def ask(line: bytes) -> bytes:
        s = socket.create_connection(("127.0.0.1", port[0]), timeout=10)
        s.sendall(line)
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        return data
    ok = ask(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert ok.startswith(b"HTTP/1.1 200") and b"hello from upstream" in ok
    assert ask(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert ask(b"CONNECT 127.0.0.1:22 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert ask(b"GET http://127.0.0.1/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    assert mod.allowed("api.anthropic.com", "443", ["anthropic.com"], {"443"})
    assert not mod.allowed("anthropic.com.evil.example", "443", ["anthropic.com"], {"443"})
    loop.call_soon_threadsafe(loop.stop)
    upstream.close()


# ---------------------------------------------------------------------- signing in from the Studio

SIGNIN = r'''#!{python}
import json, os, sys, pathlib
a = sys.argv[1:]
if a[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": False})); sys.exit(0)
if a[:1] == ["setup-token"]:
    print("Browser didn't open? Use the url below to sign in:\n\nhttps://claude.ai/oauth/authorize?code=true&state=abc\n", flush=True)
    print("Paste code here if prompted > ", end="", flush=True)
    code = sys.stdin.readline().strip()
    if code == "the-code":
        print("\nYour OAuth token (valid for 1 year):\n\nsk-ant-oat01-" + "Q" * 95 + "\n\nStore this token securely.", flush=True)
        sys.exit(0)
    sys.exit(1)
'''


@pytest.mark.skipif(os.name == "nt", reason="a pseudo-terminal: POSIX")
def test_a_studio_sign_in_shows_the_link_and_takes_the_code(stand_in, tmp_path, monkeypatch):
    p = tmp_path / "bin" / "fake-signin"
    p.write_text(SIGNIN.replace("{python}", sys.executable), encoding="utf-8")
    p.chmod(0o755)
    monkeypatch.setenv("QCCD_REVIEW_CLAUDE_BIN", str(p))
    s = A.SigninSession("claude").start()
    t0 = time.time()
    while not (s.view()["url"] and s.view()["needs_code"]):
        assert time.time() - t0 < 15, s.view()
        time.sleep(0.1)
    assert s.view()["url"] == "https://claude.ai/oauth/authorize?code=true&state=abc"
    s.send("the-code")
    while s.view()["state"] == "waiting":
        assert time.time() - t0 < 30, s.view()
        time.sleep(0.1)
    # `claude setup-token`'s token is kept in the profile, and the page never shows it
    assert s.view()["state"] == "signed_in" and A.claude_token() == "sk-ant-oat01-" + "Q" * 95
    assert "Q" * 20 not in s.view()["said"] and A.status("claude", fresh=True)["available"]


# ---------------------------------------------------------------------- the server keeps it, unchecked

def test_the_server_keeps_the_submitters_review_beside_its_own_decision(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from qccd.official.service import OfficialError, OfficialService, create_app
    from qccd.official.worker import process_one
    from qccd.workspace.tasks import RELEASES_DIR
    from qccd.official import review as rv
    for k, v in (("QCCD_OFFICIAL_DEV", "1"), ("QCCD_REVIEW", "1"), ("QCCD_HOME", str(tmp_path / "qccd")),
                 ("QCCD_DOCKER", "none")):
        monkeypatch.setenv(k, v)
    svc = OfficialService(f"sqlite:///{tmp_path / 'o.db'}", RELEASES_DIR, tmp_path / "artifacts")
    task, archive = example_archive("ghz-chain4-hot")
    up = svc.dev_uploader()
    sid = svc.submit(up, archive, task=task, visibility="private", reviewer="none")["submission_id"]
    while process_one(svc, tmp_path / "spool", inline=True, releases=RELEASES_DIR):
        pass
    rv.run_reviewer(svc, tmp_path / "reviews", once=True)
    text = "# Review\n\n**Verdict:** not accepted: R7.\n\nRe-cool it.\n"
    assert svc.attach_submitter_review(sid, up, {"reviewer": "claude", "model": "claude-opus-5-5", "isolation": "docker",
                                                 "decision": "not_accepted", "review_md": text})["kept"]
    r = svc.review(sid, up)
    assert r["decision"] == "not_accepted" and r["submitter_review"]["review_md"] == text
    page = review_page(r, "n0nce")
    assert "The submitter's own review" in page and "did not check it" in page and "Re-cool it." in page
    with pytest.raises(OfficialError):
        svc.attach_submitter_review(sid, {**up, "id": "u_other", "kind": None}, {"reviewer": "claude", "review_md": text})
    with pytest.raises(OfficialError):
        svc.attach_submitter_review(sid, up, {"reviewer": "claude", "review_md": "token sk-ant-api03-" + "y" * 40})
    http = TestClient(create_app(svc), base_url="http://127.0.0.1:8300", client=("127.0.0.1", 50000))
    assert http.post(f"/v1/submissions/{sid}/submitter-review", json={"reviewer": "codex", "review_md": text}).status_code == 401
