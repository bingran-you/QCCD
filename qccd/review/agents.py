"""The coding agents a review runs, how each signs in, and the command that runs one.

A reviewer is one of the agent CLIs a person already has -- Claude Code (`claude`) or Codex
(`codex`) -- run from a PROFILE of its own: a folder that is the CLI's whole home for reviews
(CLAUDE_CONFIG_DIR, CODEX_HOME), signed in once with the CLI's own sign-in.  A review then never
sees the person's own instructions, memories, plugins, MCP servers or hooks (their
~/.codex/AGENTS.md and ~/.claude/CLAUDE.md reach every session of their usual home, whatever else
is switched off), and their usual sign-in is untouched.  The profiles live in ~/.qccd/reviewers/
(QCCD_HOME moves it), shared by every workspace and by a development server on this computer:

    reviewers/codex          Codex, on this computer and in Docker: its sign-in is a file (auth.json)
    reviewers/claude         Claude Code: its sign-in is a long-lived subscription token from
                             `claude setup-token` (`oauth-token`, readable by this user only), which
                             works on this computer and in Docker alike (CLAUDE_CODE_OAUTH_TOKEN)
    reviewers/claude-docker  Claude Code's own settings and state in Docker (no sign-in in it)

How a reviewer pays (`mode`): `local`, the profile's own sign-in -- a person's own subscription on
their own computer -- or `api_key`, a key the caller holds (QCCD_REVIEW_ANTHROPIC_API_KEY,
QCCD_REVIEW_OPENAI_API_KEY), given to the agent's process and to nothing else.

ISOLATION: `sandbox` runs the CLI on this computer inside its own OS sandbox (Codex: Seatbelt on
macOS, Landlock on Linux; Claude Code: Seatbelt or bubblewrap): no network for the agent's
commands, writes only in the review's folder, and Claude Code's also reads nothing in the
credential folders.  `docker` runs it in a container that sees only the review's folder and the
profile, and reaches only its model's API (container.py).

A person signs a reviewer in at a terminal (`login`), or from the Studio (`SigninSession`: the
CLI's own sign-in in a pseudo-terminal, with the link and code it prints shown on the page).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

__all__ = ["REVIEWERS", "ISOLATIONS", "DENY_READ", "qccd_home", "find_cli", "profile_dir", "model_of",
           "agent_env", "signed_in", "status", "statuses", "signin_hint", "login_argv", "login", "claude_token",
           "save_claude_token", "agent_command", "SigninSession", "REPO"]

REPO = Path(__file__).resolve().parents[2]

#: the coding agents a review can run, by the id a request names.  `hosts`: the model API's hosts,
#: the only ones a Docker review may reach (and the sign-in's, for a token refreshed mid-review)
REVIEWERS = {
    "claude": {"title": "Claude", "vendor": "Anthropic", "cli": "Claude Code", "bin": "claude",
               "bin_env": "QCCD_REVIEW_CLAUDE_BIN", "model": "claude-opus-5-5", "model_env": "QCCD_REVIEW_CLAUDE_MODEL",
               "key_env": "QCCD_REVIEW_ANTHROPIC_API_KEY", "key_var": "ANTHROPIC_API_KEY",
               "home_env": "QCCD_REVIEW_CLAUDE_CONFIG_DIR", "home_var": "CLAUDE_CONFIG_DIR",
               "docker_home_env": "QCCD_REVIEW_CLAUDE_DOCKER_DIR",
               "hosts": ("api.anthropic.com", "console.anthropic.com", "platform.claude.com", "claude.ai",
                         "claude.com")},
    "codex": {"title": "ChatGPT", "vendor": "OpenAI", "cli": "Codex", "bin": "codex",
              "bin_env": "QCCD_REVIEW_CODEX_BIN", "model": None, "model_env": "QCCD_REVIEW_CODEX_MODEL",
              "key_env": "QCCD_REVIEW_OPENAI_API_KEY", "key_var": "CODEX_API_KEY",
              "home_env": "QCCD_REVIEW_CODEX_HOME", "home_var": "CODEX_HOME", "docker_home_env": None,
              "hosts": ("chatgpt.com", "auth.openai.com", "api.openai.com")},
}
ISOLATIONS = ("sandbox", "docker")

#: what the agent may not read even inside its sandbox: keys and sign-ins, its own profile's too
DENY_READ = ("~/.ssh", "~/.aws", "~/.gnupg", "~/.codex", "~/.claude", "~/.qccd", "~/.config", "~/.netrc",
             "~/.docker", "~/.kube", "~/.pypirc")
#: Codex keeps a review profile's sign-in in its auth.json, never the system keyring: a container
#: reads the same file, and nothing of the review lands in the person's own keychain
CODEX_FILE_STORE = ["-c", 'cli_auth_credentials_store="file"']
#: Claude Code's review sign-in: the token `claude setup-token` prints, kept in its profile
CLAUDE_TOKEN_FILE = "oauth-token"
_CLAUDE_TOKEN = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]{20,}")


def _now() -> float:
    return time.time()


def qccd_home() -> Path:
    """~/.qccd, or QCCD_HOME (toolchain.py's convention)."""
    return Path(os.environ.get("QCCD_HOME") or (Path.home() / ".qccd"))


def find_cli(rid: str) -> str | None:
    """The reviewer's CLI on this computer: QCCD_REVIEW_<X>_BIN, the PATH, or where the installers
    and the desktop apps put it (the workspace's own finders: Homebrew, npm, the native installer,
    the Codex and ChatGPT apps, Claude Code's editor extensions)."""
    spec = REVIEWERS[rid]
    v = os.environ.get(spec["bin_env"])
    if v:
        return v if Path(v).is_file() else None
    found = shutil.which(spec["bin"])
    if found:
        return found
    try:
        if rid == "codex":
            from ..workspace.agents.codex import find_codex as find
        else:
            from ..workspace.agents.claude import find_claude as find
        return find()
    except Exception:            # an optional dependency of the chat bridges missing: the PATH is all there is
        return None


def profile_dir(rid: str, isolation: str = "sandbox") -> Path:
    """The folder reviewer `rid` runs from as its home (module doc)."""
    spec = REVIEWERS[rid]
    if rid == "claude" and isolation == "docker":
        v = os.environ.get(spec["docker_home_env"])
        return Path(v).expanduser() if v else qccd_home() / "reviewers" / "claude-docker"
    v = os.environ.get(spec["home_env"])
    return Path(v).expanduser() if v else qccd_home() / "reviewers" / rid


def _codex_setting(key: str) -> str | None:
    """A setting of the person's own Codex (~/.codex/config.toml): the model and effort they chose,
    which a review keeps although it runs from a profile of its own."""
    try:
        import tomllib
        v = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")).get(key)
        return v if isinstance(v, str) else None
    except Exception:          # no config, or an older Python: the CLI picks its own
        return None


def model_of(rid: str) -> str | None:
    """The model a review asks for; None lets the CLI use its default."""
    spec = REVIEWERS[rid]
    return os.environ.get(spec["model_env"]) or spec["model"] or (_codex_setting("model") if rid == "codex" else None)


def codex_effort() -> str | None:
    e = os.environ.get("QCCD_REVIEW_CODEX_EFFORT") or _codex_setting("model_reasoning_effort")
    return e if e and e.isidentifier() else None


def agent_env(rid: str, mode: str | None, isolation: str = "sandbox", *, create: bool = True) -> dict:
    """The CLI's whole environment on this computer: what it needs to start, its profile, and its key
    in api_key mode -- nothing of the caller's (no server token, no database URL, no other key)."""
    keep = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    home = profile_dir(rid, isolation)
    if create:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env[REVIEWERS[rid]["home_var"]] = str(home)
    if mode == "api_key":
        env[REVIEWERS[rid]["key_var"]] = os.environ[REVIEWERS[rid]["key_env"]]
    elif rid == "claude" and claude_token():
        env["CLAUDE_CODE_OAUTH_TOKEN"] = claude_token()
    return env


def claude_token() -> str | None:
    """The Claude subscription token the review profile was signed in with (`claude setup-token`)."""
    try:
        t = (profile_dir("claude") / CLAUDE_TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return t if _CLAUDE_TOKEN.fullmatch(t) else None


def save_claude_token(token: str) -> None:
    """Keep a Claude subscription token in the review profile, readable by this user only."""
    token = (token or "").strip()
    if not _CLAUDE_TOKEN.fullmatch(token):
        raise ValueError("that is not a Claude subscription token (`claude setup-token` prints one: sk-ant-oat01-...)")
    home = profile_dir("claude")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = home / (CLAUDE_TOKEN_FILE + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.replace(tmp, home / CLAUDE_TOKEN_FILE)
    _STATUS.clear()


def signin_hint(rid: str, isolation: str = "sandbox") -> str:
    """What a person runs at a terminal to sign reviewer `rid` in for reviews (one sign-in serves both
    isolations)."""
    return f"qccd review login {rid}"


def signed_in(rid: str, isolation: str = "sandbox", binp: str | None = None) -> tuple[bool, str | None, str | None]:
    """(signed in, as what, why not), from the CLI itself (or, for Claude Code in Docker, the
    sign-in file the container keeps in its profile)."""
    hint = f"not signed in for reviews: `{signin_hint(rid, isolation)}`, once"
    if rid == "claude" and claude_token():
        return True, "a Claude subscription token (claude setup-token)", None
    home = profile_dir(rid, isolation)
    if not home.is_dir():
        # never signed in: no profile yet (and asking the CLI would make one)
        return False, None, hint
    if rid == "claude" and isolation == "docker":
        # a container reads no Keychain: only a token, or a sign-in made inside a container, will do
        try:
            creds = json.loads((home / ".credentials.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, None, hint
        return (True, "signed in, in Docker", None) if creds else (False, None, hint)
    binp = binp or find_cli(rid)
    if not binp and rid == "codex" and isolation == "docker":
        # no Codex on this computer, only in the image: its sign-in file is all there is to go by
        return ((True, "signed in", None) if (home / "auth.json").is_file() else (False, None, hint))
    if not binp:
        return False, None, f"{REVIEWERS[rid]['cli']} (`{REVIEWERS[rid]['bin']}`) is not installed"
    try:
        if rid == "claude":
            p = subprocess.run([binp, "auth", "status"], capture_output=True, text=True, timeout=20,
                               env=agent_env(rid, None, isolation, create=False), stdin=subprocess.DEVNULL)
            d = json.loads(p.stdout or "{}")
            if isinstance(d, dict) and d.get("loggedIn"):
                parts = [str(d[k]) for k in ("email", "subscriptionType") if d.get(k)]
                return True, " · ".join(parts) or str(d.get("authMethod") or "signed in"), None
            return False, None, hint
        p = subprocess.run([binp, "login", "status", *CODEX_FILE_STORE], capture_output=True, text=True, timeout=20,
                           env=agent_env(rid, None, isolation, create=False), stdin=subprocess.DEVNULL)
        said = " ".join((p.stdout + " " + p.stderr).split())
        if p.returncode == 0 and "logged in" in said.lower() and "not logged in" not in said.lower():
            if isolation == "docker" and not (home / "auth.json").is_file():
                return False, None, ("signed in, but not in a file a container can read: sign in again with "
                                     f"`{signin_hint(rid, isolation)}`")
            return True, said.split("Logged in using", 1)[-1].strip() or "signed in", None
        return False, None, hint
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, None, f"could not ask {REVIEWERS[rid]['bin']} whether it is signed in: {exc}"


_STATUS: dict = {}
_STATUS_TTL_S = 20.0


def status(rid: str, *, isolation: str = "sandbox", allow_api_key: bool = True, allow_local: bool = True,
           fresh: bool = False, here: str = "this computer") -> dict:
    """Whether reviewer `rid` can run here now, how it pays, and if it cannot, why not."""
    spec = REVIEWERS[rid]
    key = (rid, isolation, allow_api_key, allow_local, here)
    hit = _STATUS.get(key)
    if hit and not fresh and _now() - hit[0] < _STATUS_TTL_S:
        return dict(hit[1])
    out = {"id": rid, "title": spec["title"], "vendor": spec["vendor"], "cli": spec["cli"], "model": model_of(rid),
           "isolation": isolation, "available": False, "mode": None, "account": None, "why": None,
           "signin": signin_hint(rid, isolation), "profile": str(profile_dir(rid, isolation))}
    if isolation not in ISOLATIONS:
        out["why"] = f"isolation is one of {', '.join(ISOLATIONS)}"
    elif isolation == "docker" and not (dk := _docker_ready())[0]:
        out["why"] = dk[1]
    elif isolation == "sandbox" and not find_cli(rid):
        out["why"] = f"{spec['cli']} (`{spec['bin']}`) is not installed on {here}"
    elif allow_api_key and os.environ.get(spec["key_env"]):
        out.update(available=True, mode="api_key", account="an API key")
    elif allow_local:
        ok, who, why = signed_in(rid, isolation)
        out.update(available=ok, mode="local" if ok else None, account=who, why=why)
    else:
        out["why"] = f"no {spec['vendor']} API key for reviews here"
    _STATUS[key] = (_now(), out)
    return dict(out)


def statuses(**kw) -> list[dict]:
    return [status(r, **kw) for r in REVIEWERS]


def _docker_ready() -> tuple[bool, str]:
    from .container import ready
    return ready()


# ---------------------------------------------------------------------- signing in

def login_argv(rid: str, isolation: str = "sandbox", *, device: bool = False) -> tuple[list[str], dict]:
    """The CLI's own sign-in, into the reviewer's profile: Codex's `login` (`device`: its
    device-code sign-in, a link and a code, for a sign-in started without a terminal), and Claude
    Code's `setup-token`, whose token serves this computer and Docker alike."""
    binp = find_cli(rid)
    if not binp:
        raise RuntimeError(f"{REVIEWERS[rid]['cli']} (`{REVIEWERS[rid]['bin']}`) is not installed")
    env = {**agent_env(rid, None, "sandbox"), "TERM": os.environ.get("TERM", "xterm-256color")}
    if rid == "claude":
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        return [binp, "setup-token"], env
    return [binp, "login", *CODEX_FILE_STORE, *(["--device-auth"] if device else [])], env


def login(rid: str, isolation: str = "sandbox", *, device: bool = False, token: str | None = None) -> int:
    """Sign reviewer `rid` in at this terminal: the CLI's own sign-in, into its profile.  Claude
    Code: `claude setup-token`, its token kept as it is printed -- or `token`, one it printed before."""
    spec = REVIEWERS[rid]
    if rid == "claude" and token is not None:
        save_claude_token(token)
        print("Claude is signed in for reviews (the token is kept in its review profile).")
        return 0
    argv, env = login_argv(rid, isolation, device=device)
    print(f"Signing {spec['title']} ({spec['cli']}) in for reviews -- on this computer and in Docker alike; its "
          f"profile: {profile_dir(rid)}.\nThis is a sign-in of its own: your usual {spec['bin']} sign-in is not "
          "touched.\n", flush=True)
    if rid != "claude":
        rc = subprocess.call(argv, env=env)
        _STATUS.clear()
        return rc
    rc, said = _tee(argv, env)
    found = _CLAUDE_TOKEN.findall(_ANSI.sub("", said))
    if not found:
        print("\nNo token came back: the sign-in did not finish.", file=sys.stderr)
        return rc or 1
    save_claude_token(found[-1])
    print("\nThe token is kept in Claude's review profile; you need not copy it anywhere.")
    return 0


def _tee(argv: list[str], env: dict) -> tuple[int, str]:
    """Run an interactive sign-in at this terminal, and keep what it printed (a pseudo-terminal in
    between when there is a terminal, so the CLI's own screens work)."""
    if not sys.stdin.isatty() or os.name == "nt":
        p = subprocess.run(argv, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        sys.stdout.write(_CLAUDE_TOKEN.sub("sk-ant-oat01-…", p.stdout))
        return p.returncode, p.stdout
    import pty
    chunks: list[bytes] = []

    def master_read(fd: int) -> bytes:
        data = os.read(fd, 4096)
        chunks.append(data)
        return data
    status = pty.spawn(["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()], *argv], master_read)
    return os.waitstatus_to_exitcode(status), b"".join(chunks).decode("utf-8", "replace")


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


class SigninSession:
    """A reviewer's sign-in for a person without a terminal (the Studio): the CLI's own sign-in in a
    pseudo-terminal, the link it prints (and Codex's one-time code) for the page to show, and a way
    to type the code Claude Code asks for when its browser cannot call back.  POSIX only."""

    def __init__(self, rid: str, isolation: str = "sandbox"):
        self.id = "si_" + secrets.token_hex(6)
        self.rid, self.isolation = rid, isolation
        self.state, self.url, self.code, self.needs_code, self.error = "starting", None, None, False, None
        self.out, self.started, self.finished = "", _now(), None
        self.proc: subprocess.Popen | None = None
        self.master: int | None = None
        self.lock = threading.Lock()

    def start(self) -> "SigninSession":
        if os.name == "nt":
            raise RuntimeError(f"sign in at a terminal: {signin_hint(self.rid, self.isolation)}")
        import fcntl
        import pty
        import struct
        import termios
        argv, env = login_argv(self.rid, self.isolation, device=True)
        master, slave = pty.openpty()
        # a wide terminal: a sign-in link printed in one piece, never wrapped across lines
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 1000, 0, 0))
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True,
                                     close_fds=True)
        os.close(slave)
        self.master = master
        self.state = "waiting"
        threading.Thread(target=self._read, name=f"qccd-signin-{self.id}", daemon=True).start()
        return self

    def _read(self) -> None:
        while True:
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            with self.lock:
                self.out = (self.out + _ANSI.sub("", chunk.decode("utf-8", "replace")))[-20000:]
                self._scan()
        rc = self.proc.wait() if self.proc else None
        try:
            os.close(self.master)
        except OSError:
            pass
        _STATUS.clear()
        ok, who, why = signed_in(self.rid, self.isolation)
        with self.lock:
            if self.state != "cancelled":
                self.state = "signed_in" if ok else "failed"
                if not ok:
                    tail = " ".join(self.out.split())[-300:]
                    self.error = f"the sign-in ended (exit {rc}) without signing in" + (f": {tail}" if tail else "")
            self.finished = _now()

    def _scan(self) -> None:
        # the whole token, up to the end of its line: a read can end in the middle of it
        tok = re.search(_CLAUDE_TOKEN.pattern + r"(?=\s)", self.out)
        if tok and self.rid == "claude":
            # `claude setup-token` printed its token: kept in the profile, and never shown on a page
            save_claude_token(tok.group(0))
            self.out = _CLAUDE_TOKEN.sub("sk-ant-oat01-… (kept in the review profile)", self.out)
        hosts = ("auth.openai.com", "chatgpt.com", "claude.ai", "claude.com", "anthropic.com")
        for u in re.findall(r"https://[^\s\"'<>]+", self.out):
            if any(h in u for h in hosts):
                self.url = u.rstrip(".,)")
        if self.rid == "codex":
            m = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b", self.out)
            if m:
                self.code = m.group(1)
        elif re.search(r"(?i)paste (the )?code|enter (the )?code|authorization code", self.out):
            self.needs_code = True

    def send(self, text: str) -> None:
        """Type `text` and Enter into the sign-in (the code a browser showed)."""
        if self.master is None or self.state not in ("waiting",):
            raise RuntimeError("this sign-in is not waiting for a code")
        os.write(self.master, (str(text).strip()[:400] + "\r").encode("utf-8"))

    def cancel(self) -> None:
        with self.lock:
            self.state = "cancelled"
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass

    def view(self) -> dict:
        with self.lock:
            return {"signin_id": self.id, "reviewer": self.rid, "isolation": self.isolation, "state": self.state,
                    "url": self.url, "code": self.code, "needs_code": self.needs_code and self.state == "waiting",
                    "error": self.error, "started_at": self.started, "finished_at": self.finished,
                    "said": re.sub(r"sk-ant-oat01-\S*", "sk-ant-oat01-…",
                                   "\n".join(ln for ln in self.out.splitlines() if ln.strip()))[-1500:]}


# ---------------------------------------------------------------------- the agent's command

def agent_command(rid: str, job_dir: Path, prompt: str, *, mode: str | None,
                  python: str | None = None) -> tuple[list[str], dict]:
    """The argv and environment that run reviewer `rid` on `job_dir` on this computer, inside its
    CLI's sandbox (a Docker review: container.py)."""
    binp, model = find_cli(rid), model_of(rid)
    if not binp:
        raise RuntimeError(f"{REVIEWERS[rid]['cli']} is not installed")
    env = agent_env(rid, mode)
    # the skill's script, copied into the folder, finds the checkout it reads the rules from
    env["QCCD_REPO"] = str(REPO)
    if rid == "claude":
        deny = list(DENY_READ) + [p for p in os.environ.get("QCCD_REVIEW_DENY_READ", "").split(os.pathsep) if p]
        settings = {"sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                                "failIfUnavailable": True, "network": {"allowedDomains": []},
                                "filesystem": {"denyRead": deny}},
                    "permissions": {"allow": ["Read", "Glob", "Grep", "Bash", "Write(./out/**)", "Edit(./out/**)"],
                                    "deny": ["WebFetch", "WebSearch"] + [f"Read({p}/**)" for p in deny]}}
        argv = [binp, "-p", prompt, "--model", model or "claude-opus-5-5", "--output-format", "stream-json", "--verbose",
                "--permission-mode", "dontAsk", "--setting-sources", "project", "--strict-mcp-config",
                "--no-session-persistence", "--tools", "Bash,Read,Glob,Grep,Write,Edit",
                "--settings", json.dumps(settings)]
        return argv, env
    # none of the person's own Codex setup: no config.toml (features, plugins, MCP servers, hooks),
    # no exec-policy rules, no memories read or written
    argv = [binp, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--cd", str(job_dir), "--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=false",
            "-c", 'approval_policy="never"', "-c", "features.memories=false", "-c", "memories.use_memories=false",
            "-c", "memories.generate_memories=false", *CODEX_FILE_STORE,
            "-o", str(Path(job_dir) / "out" / ".last-message.md")]
    if model:
        argv += ["-m", model]
    if codex_effort():
        argv += ["-c", f'model_reasoning_effort="{codex_effort()}"']
    return argv + [prompt], env
