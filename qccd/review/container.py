"""Docker isolation for a review: the agent's CLI in a container that sees only the review's
folder and its profile, and reaches only its model's API.

    image    qccd-reviewer:<digest of deploy/review/>, built on this computer the first time a review
             asks for Docker (`ensure_image`): Node with the two CLIs at the versions pinned there,
             Python with numpy for the design-review skill's script, and the egress proxy
    network  a Docker network of the review's own, --internal: nothing on it has a route out
    egress   a proxy container on that network and on the default one: CONNECT goes through to the
             reviewer's model API hosts only (agents.REVIEWERS[...]["hosts"], plus
             QCCD_REVIEW_EGRESS_EXTRA), port 443 only; every request, let through or refused, is
             logged, and the review keeps the hosts (`Isolated.egress`)
    agent    a read-only root, no capabilities, no new privileges, pid, memory and CPU limits, the
             person's own uid; /job (the review's folder) and /profile (its sign-in, so a token
             refreshed mid-review is kept) read-write, the checkout's qccd/ and arch/ read-only for
             the skill's script.  Inside, the CLI's own sandbox is off: the container is the sandbox
             (Codex's and Claude Code's sandboxes need kernel features a container does not give).
    sign-in  Codex's auth.json is in its profile; Claude Code's token (`claude setup-token`) goes in by
             name (CLAUDE_CODE_OAUTH_TOKEN), as a key does: one sign-in serves both isolations

What the agent can still read is the review's folder, the code, and its own profile (the CLI must
sign its requests): a review that leaves this computer is checked for credentials first
(engine.find_secrets).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import agents

__all__ = ["docker_bin", "ready", "image_tag", "ensure_image", "Isolated", "sweep", "IMAGE_DIR"]

IMAGE_DIR = agents.REPO / "deploy" / "review"
LABEL = "qccd.review=1"
PROXY_PORT = 3128
#: the process that runs a review: what it leaves behind when it dies is swept (`sweep`)
OWNER = "qccd.review.pid"

_READY: dict = {}
_BUILD = threading.Lock()


def docker_bin() -> str | None:
    """The docker CLI: QCCD_DOCKER, the PATH, or Docker Desktop's own (which a service started by a
    desktop app does not have on its PATH)."""
    v = os.environ.get("QCCD_DOCKER")
    if v:
        return v if v != "none" and Path(v).is_file() else None
    found = shutil.which("docker")
    if found:
        return found
    for cand in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                 "/Applications/Docker.app/Contents/Resources/bin/docker"):
        if Path(cand).is_file():
            return cand
    return None


def _client_env() -> dict:
    keep = ("PATH", "HOME", "USER", "LANG", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
            "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")
    env = {k: v for k, v in os.environ.items() if k in keep}
    # Docker Desktop's credential helpers live beside its CLI, which a desktop app's PATH lacks
    d = docker_bin()
    if d:
        env["PATH"] = os.pathsep.join([str(Path(d).parent), env.get("PATH", "")])
    return env


def _docker(*args: str, timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess:
    d = docker_bin()
    if not d:
        raise RuntimeError("Docker is not installed")
    p = subprocess.run([d, *args], capture_output=True, text=True, timeout=timeout, env=_client_env(),
                       stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {(p.stderr or p.stdout).strip()[-600:]}")
    return p


def ready(*, fresh: bool = False) -> tuple[bool, str]:
    """(Docker can run a review now, and if not, why not), cached for a few seconds."""
    hit = _READY.get("v")
    if hit and not fresh and time.time() - hit[0] < 10.0:
        return hit[1]
    if os.name == "nt":
        out = (False, "Docker isolation runs on macOS and Linux")
    elif not docker_bin():
        out = (False, "Docker is not installed (Docker Desktop, or the docker engine)")
    else:
        try:
            p = _docker("version", "--format", "{{.Server.Version}}", timeout=10, check=False)
            out = (True, "") if p.returncode == 0 and p.stdout.strip() else \
                (False, "Docker is not running: start Docker Desktop (or the docker service), then try again")
        except (OSError, subprocess.SubprocessError) as exc:
            out = (False, f"could not reach Docker: {exc}")
    _READY["v"] = (time.time(), out)
    return out


def image_tag() -> str:
    """qccd-reviewer:<the digest of deploy/review/>: a change to the image's recipe is a new image."""
    h = hashlib.sha256()
    for p in sorted(IMAGE_DIR.iterdir()):
        if p.is_file():
            h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return f"qccd-reviewer:{h.hexdigest()[:12]}"


def ensure_image(say=None) -> str:
    """The reviewer image, built now if this computer does not have it (a few minutes, once)."""
    tag = image_tag()
    with _BUILD:
        if _docker("image", "inspect", tag, check=False, timeout=30).returncode == 0:
            return tag
        if say:
            say([f"Building the reviewer image {tag} (once, a few minutes)"])
        d = docker_bin()
        proc = subprocess.Popen([d, "build", "--label", LABEL, "-t", tag, str(IMAGE_DIR)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=_client_env(), stdin=subprocess.DEVNULL)
        tail: list[str] = []
        for ln in proc.stdout:
            tail.append(ln.rstrip())
            del tail[:-40]
            if say and ln.strip().startswith(("#", "Step")):
                say([ln.strip()[:200]])
        if proc.wait() != 0:
            raise RuntimeError("building the reviewer image failed: " + " | ".join(tail[-8:])[-800:])
        return tag


def _hosts(rid: str) -> list[str]:
    extra = [h.strip().lower() for h in os.environ.get("QCCD_REVIEW_EGRESS_EXTRA", "").split(",") if h.strip()]
    return list(agents.REVIEWERS[rid]["hosts"]) + extra


def _cli_argv(rid: str, prompt: str) -> list[str]:
    """The CLI's own command inside the container (its sandbox off: the container is the sandbox)."""
    model = agents.model_of(rid)
    if rid == "claude":
        settings = {"sandbox": {"enabled": False},
                    "permissions": {"allow": ["Read", "Glob", "Grep", "Bash", "Write(./out/**)", "Edit(./out/**)"],
                                    "deny": ["WebFetch", "WebSearch", "Read(/profile/**)"]}}
        return ["claude", "-p", prompt, "--model", model or "claude-opus-5-5", "--output-format", "stream-json",
                "--verbose", "--permission-mode", "dontAsk", "--setting-sources", "project", "--strict-mcp-config",
                "--no-session-persistence", "--tools", "Bash,Read,Glob,Grep,Write,Edit", "--settings", json.dumps(settings)]
    argv = ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--cd", "/job", "--sandbox", "danger-full-access", "-c", 'approval_policy="never"',
            "-c", "features.memories=false", "-c", "memories.use_memories=false",
            "-c", "memories.generate_memories=false", *agents.CODEX_FILE_STORE, "-o", "/job/out/.last-message.md"]
    if model:
        argv += ["-m", model]
    if agents.codex_effort():
        argv += ["-c", f'model_reasoning_effort="{agents.codex_effort()}"']
    return argv + [prompt]


def _owner() -> str:
    return f"{OWNER}={os.getpid()}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                     # someone else's process: alive
        return True
    return True


def sweep() -> list[str]:
    """Remove what a review left behind when the process that ran it died before tearing it down
    (its containers and network carry that process's pid): never another live review's.  The
    containers go first: a network in use cannot be removed."""
    gone = []
    for listing, field, remove in ((("ps", "-a"), "{{.Names}}", ("rm", "-f")),
                                   (("network", "ls"), "{{.Name}}", ("network", "rm"))):
        p = _docker(*listing, "--filter", "label=" + LABEL, "--format", field + '\t{{.Label "' + OWNER + '"}}',
                    check=False, timeout=30)
        for line in (p.stdout or "").splitlines():
            name, _, pid = line.partition("\t")
            if name and pid.strip().isdigit() and not _alive(int(pid)):
                _docker(*remove, name, check=False, timeout=30)
                gone.append(name)
    return gone


def _profile(rid: str) -> Path:
    """The reviewer's Docker profile, readable by this user only (it holds the sign-in)."""
    profile = agents.profile_dir(rid, "docker")
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        profile.chmod(0o700)
    except OSError:
        pass
    return profile


def _run_as() -> list[str]:
    return ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []


#: a container's HOME is on its tmpfs: made, then the command
_WITH_HOME = ["sh", "-c", 'mkdir -p "$HOME" && exec "$@"', "sh"]


class Isolated:
    """`with Isolated(rid, job, mode=...) as box: argv, env = box.command(prompt)` -- the network and
    the egress proxy for one review, torn down (with the agent's container) on the way out."""

    def __init__(self, rid: str, job: Path, *, mode: str | None = "local"):
        self.rid, self.job, self.mode = rid, Path(job), mode
        self.name = "qccd-rv-" + secrets.token_hex(5)
        self.net, self.proxy = self.name + "-net", self.name + "-egress"
        self.image: str | None = None
        self.egress: dict | None = None

    def __enter__(self) -> "Isolated":
        ok, why = ready(fresh=True)
        if not ok:
            raise RuntimeError(why)
        self.image = ensure_image()
        sweep()
        try:
            _docker("network", "create", "--internal", "--label", LABEL, "--label", _owner(), self.net)
            _docker("run", "-d", "--rm", "--name", self.proxy, "--label", LABEL, "--label", _owner(),
                    "--network", "bridge", "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "128m",
                    "--user", "65534:65534", self.image, "python3", "/opt/qccd/egress_proxy.py", "--port", str(PROXY_PORT),
                    "--allow", ",".join(_hosts(self.rid)))
            _docker("network", "connect", "--alias", "egress", self.net, self.proxy)
        except Exception:
            self._teardown()
            raise
        return self

    def command(self, prompt: str) -> tuple[list[str], dict]:
        spec = agents.REVIEWERS[self.rid]
        profile = _profile(self.rid)
        proxy = f"http://egress:{PROXY_PORT}"
        envs = {"HOME": "/tmp/home", "HTTPS_PROXY": proxy, "https_proxy": proxy, "HTTP_PROXY": proxy,
                "http_proxy": proxy, "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1",
                "QCCD_REPO": "/opt/qccd-repo", "PYTHONDONTWRITEBYTECODE": "1", spec["home_var"]: "/profile",
                "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        client = _client_env()
        keyflag: list[str] = []
        # a key or a token goes by name: its value is in the docker client's environment, never in an argv
        if self.mode == "api_key":
            client[spec["key_var"]] = os.environ[spec["key_env"]]
            keyflag = ["-e", spec["key_var"]]
        elif self.rid == "claude" and agents.claude_token():
            client["CLAUDE_CODE_OAUTH_TOKEN"] = agents.claude_token()
            keyflag = ["-e", "CLAUDE_CODE_OAUTH_TOKEN"]
        argv = [docker_bin(), "run", "--rm", "--name", self.name, "--label", LABEL, "--label", _owner(), "--network", self.net,
                "--read-only", "--tmpfs", "/tmp:rw,exec,size=1g", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "512", "--memory", "4g", "--cpus", "2",
                *_run_as(), "-w", "/job",
                "-v", f"{self.job}:/job", "-v", f"{profile}:/profile",
                "-v", f"{agents.REPO / 'qccd'}:/opt/qccd-repo/qccd:ro", "-v", f"{agents.REPO / 'arch'}:/opt/qccd-repo/arch:ro",
                *[x for k, v in envs.items() for x in ("-e", f"{k}={v}")], *keyflag,
                self.image, *_WITH_HOME, *_cli_argv(self.rid, prompt)]
        return argv, client

    def __exit__(self, *exc) -> bool:
        self._teardown()
        return False

    def _teardown(self) -> None:
        try:
            p = _docker("logs", self.proxy, check=False, timeout=20)
            allowed, refused = set(), set()
            for ln in (p.stdout or "").splitlines():
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                (allowed if e.get("allowed") else refused).add(f"{e.get('host')}:{e.get('port')}")
            self.egress = {"allowed": sorted(allowed), "refused": sorted(refused)}
        except Exception:
            pass
        for args in (("rm", "-f", self.name), ("rm", "-f", self.proxy), ("network", "rm", self.net)):
            try:
                _docker(*args, check=False, timeout=30)
            except Exception:
                pass
