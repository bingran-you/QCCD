"""The book's chapters, checked by Lean as their readers will meet them.

Every live chapter is run twice through the checker package's Lean (`Compiler/lean/checker`,
the toolchain the proved checker is built with):

* SOLVED -- the chapter file as written, with the book's grader appended: no errors, no
  `sorry`, and every exercise rests only on the allowed axioms.  This is the proof that every
  exercise can be done.
* READER -- the chapter as the page hands it out, every exercise proof replaced by `sorry`,
  with the same grader: no errors, and every exercise reported OPEN (its axioms include
  `sorryAx`) while its statement still type-checks.  An exercise the reader's version already
  solves, or a statement the grader cannot restate, fails here.

The browser runs a newer Lean (the WebAssembly build); `tests/book.mjs` is the in-browser
counterpart.  This test needs `lake` on PATH and the checker package built
(`cd Compiler/lean/checker && lake build`); without them it skips, saying so.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from qccd.site import book

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "Compiler" / "lean" / "checker"
LIVE = [(v["n"], c) for v in json.loads((book.BOOK / "book.json").read_text(encoding="utf-8"))["volumes"]
        for c in v["chapters"] if c.get("status") == "live"]

def _lean_available() -> bool:
    return shutil.which("lake") is not None and (CHECKER / ".lake" / "build" / "lib" / "lean" / "QCCDC").is_dir()


SEVERITY = {"error": "error", "warning": "warning", "information": "info"}


def run_lean(text: str, tmp: Path) -> list[dict]:
    """Every message Lean prints for `text`, as {line, sev, text}.  `--json` because the plain
    output prints an information message (an `#eval` result, `#print axioms`) with no position."""
    f = tmp / "Chapter.lean"
    f.write_text(text, encoding="utf-8")
    r = subprocess.run(["lake", "env", "lean", "--json", str(f)], cwd=CHECKER, capture_output=True, timeout=900)
    msgs = []
    for ln in r.stdout.decode("utf-8", "replace").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        m = json.loads(ln)
        msgs.append({"line": m["pos"]["line"], "sev": SEVERITY.get(m["severity"], m["severity"]), "text": m["data"]})
    if r.returncode not in (0, 1):
        raise RuntimeError(f"lean exited {r.returncode}: {r.stderr.decode('utf-8', 'replace')[:400]}")
    return msgs


def axioms_of(msgs: list[dict], name: str) -> list[str] | None:
    for m in msgs:
        t = m["text"]
        if t.startswith(f"'{name}' does not depend on any axioms"):
            return []
        mm = re.match(rf"^'{re.escape(name)}' depends on axioms: \[(.*?)\]", t, flags=re.S)
        if mm:
            return [a.strip() for a in mm.group(1).split(",") if a.strip()]
    return None


def grader_lines(text: str) -> int:
    """The 1-based line the grader starts on, when appended to `text`."""
    return text.count("\n") + 2


@pytest.fixture(scope="module")
def lean():
    if not _lean_available():
        pytest.skip("no `lake` on PATH or the checker package is not built (cd Compiler/lean/checker && lake build)")


@pytest.mark.parametrize("vol,c", LIVE, ids=[c["slug"] for _, c in LIVE])
def test_the_solved_chapter_passes_every_exercise(lean, vol, c, tmp_path):
    ch = book.parse_chapter(book.BOOK / c["source"], c["slug"])
    assert ch.exercises, "a live chapter without exercises"
    src = (book.BOOK / c["source"]).read_text(encoding="utf-8").replace("\r\n", "\n")
    msgs = run_lean(src + book.grader(ch), tmp_path)
    errors = [m for m in msgs if m["sev"] == "error"]
    assert not errors, "\n".join(f'{m["line"]}: {m["text"]}' for m in errors)
    sorries = [m for m in msgs if "declaration uses 'sorry'" in m["text"]]
    assert not sorries, sorries
    for e in ch.exercises:
        ax = axioms_of(msgs, e["name"])
        assert ax is not None, f'no axioms line for {e["name"]}'
        bad = [a for a in ax if a not in book.ALLOWED_AXIOMS]
        assert not bad, f'{e["name"]} rests on {bad}'


@pytest.mark.parametrize("vol,c", LIVE, ids=[c["slug"] for _, c in LIVE])
def test_the_readers_chapter_leaves_every_exercise_open(lean, vol, c, tmp_path):
    ch = book.parse_chapter(book.BOOK / c["source"], c["slug"])
    reader = "\n\n".join(book.reader_code(b) for b in ch.blocks if b.kind == "code") + "\n"
    msgs = run_lean(reader + book.grader(ch), tmp_path)
    start = grader_lines(reader)
    errors = [m for m in msgs if m["sev"] == "error"]
    assert not errors, "\n".join(f'{m["line"]}: {m["text"]}' for m in errors)
    for e in ch.exercises:
        ax = axioms_of(msgs, e["name"])
        assert ax is not None, f'no axioms line for {e["name"]}'
        assert "sorryAx" in ax, f'{e["name"]} is already solved in the reader\'s version: {ax}'
    # and the grader itself raised nothing (the statements restate cleanly)
    assert not [m for m in msgs if m["line"] >= start and m["sev"] == "error"]


def test_the_page_carries_the_lean_byte_for_byte(tmp_path):
    """The page reads its grader, prelude and certificate Lean out of a JSON <script>, whose text
    the browser never entity-decodes: an HTML-escaped `=>` (`=&gt;`) once made every restated
    statement with a `>` fail to parse, and an engine that dropped parse errors then counted the
    exercise solved with its statement unchecked."""
    book.build(tmp_path)
    for vol, c in LIVE:
        ch = book.parse_chapter(book.BOOK / c["source"], c["slug"])
        page = (tmp_path / "book" / f'{c["slug"]}.html').read_text(encoding="utf-8")
        blob = re.search(r'<script type="application/json" id="bookdata">(.*?)</script>', page, re.S).group(1)
        data = json.loads(blob)
        assert data["grader"] == book.grader(ch)
        assert data["prelude"] == (book.ASSETS / "engine" / "prelude.lean").read_text(encoding="utf-8")
        for case in data["cases"]:
            assert "&gt;" not in case["lean"] and "&lt;" not in case["lean"] and "&amp;" not in case["lean"]


def test_every_exercise_has_a_progress_id_the_server_accepts():
    """qccd-86's progress API widens to `^(?:[A-Z][0-9]{1,2}|[a-z]+:[A-Za-z0-9._-]{1,48})$`."""
    rx = re.compile(r"^(?:[A-Z][0-9]{1,2}|[a-z]+:[A-Za-z0-9._-]{1,48})$")
    for _, c in LIVE:
        ch = book.parse_chapter(book.BOOK / c["source"], c["slug"])
        for e in ch.exercises:
            assert rx.match(e["id"]), e["id"]


def test_a_tampered_statement_is_caught_by_the_grader(lean, tmp_path):
    """Control: weaken one exercise's statement in the reader's text and give it a real proof;
    the grader's restatement must fail, so a changed statement is never counted as solved."""
    vol, c = LIVE[0]
    ch = book.parse_chapter(book.BOOK / c["source"], c["slug"])
    e = ch.exercises[0]
    reader = "\n\n".join(book.reader_code(b) for b in ch.blocks if b.kind == "code") + "\n"
    tampered = reader.replace(e["statement"] + " by\n  sorry", f"theorem {e['name']} : True := by\n  trivial", 1)
    assert tampered != reader
    msgs = run_lean(tampered + book.grader(ch), tmp_path)
    start = grader_lines(tampered)
    assert [m for m in msgs if m["line"] >= start and m["sev"] == "error"], "the grader accepted a changed statement"
