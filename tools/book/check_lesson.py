#!/usr/bin/env python3
"""Check one Lean lesson of the tutorial natively, the way tests/test_book.py checks the published
ones -- for an author, before a lesson is published.

    python tools/book/check_lesson.py F2            (a lesson id from book/book.json)
    python tools/book/check_lesson.py book/chapters/F2-specification.lean --id F2

It runs the checker package's Lean (`Compiler/lean/checker`, `lake env lean --json`):

* SOLVED: the lesson file as written plus the grader -- no errors, no `sorry`, every exercise
  resting only on propext / Quot.sound / Classical.choice;
* READER: the lesson as a reader gets it (each exercise proof `sorry`) plus the grader -- no
  errors, every exercise open, every restated statement still type-checking;
* FIGURES: every {{figure NAME}} / {{studio NAME}} names a value the lesson defines;
* WORDS: the prose never says book, volume or chapter (the lessons are the tutorial's).

It prints one line per check and exits non-zero on the first failure.  The browser runs a newer
Lean (the WebAssembly build); check a lesson there too before publishing it."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from qccd.site import book  # noqa: E402

CHECKER = ROOT / "Compiler" / "lean" / "checker"
SEV = {"error": "error", "warning": "warning", "information": "info"}


def lean(text: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "Lesson.lean"
        f.write_text(text, encoding="utf-8")
        r = subprocess.run(["lake", "env", "lean", "--json", str(f)], cwd=CHECKER, capture_output=True, timeout=1800)
    out = []
    for ln in r.stdout.decode("utf-8", "replace").splitlines():
        if ln.strip().startswith("{"):
            m = json.loads(ln)
            out.append({"line": m["pos"]["line"], "sev": SEV.get(m["severity"], m["severity"]), "text": m["data"]})
    return out


def axioms(msgs: list[dict], name: str) -> list[str] | None:
    for m in msgs:
        if m["text"].startswith(f"'{name}' does not depend on any axioms"):
            return []
        mm = re.match(rf"^'{re.escape(name)}' depends on axioms: \[(.*?)\]", m["text"], flags=re.S)
        if mm:
            return [a.strip() for a in mm.group(1).split(",") if a.strip()]
    return None


def fail(msg: str) -> None:
    print("FAIL", msg)
    raise SystemExit(1)


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")          # stars and Lean's symbols on any console
    except (AttributeError, ValueError):
        pass
    if not argv:
        print(__doc__)
        return 2
    if shutil.which("lake") is None:
        fail("no `lake` on PATH")
    lesson = None
    for part in book.outline()["parts"]:
        for les in part["lessons"]:
            if les["id"] == argv[0] or ("--id" in argv and les["id"] == argv[argv.index("--id") + 1]):
                lesson = les
    if argv[0].endswith(".lean"):
        src_path = (ROOT / argv[0]) if not Path(argv[0]).is_absolute() else Path(argv[0])
    elif lesson and lesson.get("source"):
        src_path = book.BOOK / lesson["source"]
    else:
        fail(f"no lesson {argv[0]} with a source in book/book.json")
    slug = (lesson or {}).get("slug") or src_path.stem
    ch = book.parse_chapter(src_path, slug, (lesson or {}).get("store"), (lesson or {}).get("ids"))
    if not ch.exercises:
        fail("the lesson has no exercises")
    print(f"lesson {(lesson or {}).get('id', '?')} {ch.title!r}: {len(ch.exercises)} exercises "
          f"({', '.join('★' * e['stars'] + ' ' + e['name'] for e in ch.exercises)})")

    src = src_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    prose = " ".join(b.text for b in ch.blocks if b.kind == "prose")
    bad = re.findall(r"(?i)\bbook\b|\bvolume\b|\bchapter\b", prose)
    if bad:
        fail(f"WORDS: the prose says {sorted(set(bad))}")
    print("ok   WORDS")
    names = re.findall(r"\{\{(?:figure|studio)\s+([A-Za-z_][A-Za-z0-9_'.]*)", src)
    for n in names:
        if not re.search(rf"(?m)^(?:def|abbrev|theorem)\s+{re.escape(n)}\b", src):
            fail(f"FIGURES: {{{{... {n}}}}} names no `def {n}` in the lesson")
    print(f"ok   FIGURES ({len(names)})")

    msgs = lean(src + book.grader(ch))
    errs = [m for m in msgs if m["sev"] == "error"]
    if errs:
        fail("SOLVED: " + "; ".join(f'{m["line"]}: {m["text"][:300]}' for m in errs[:5]))
    if any("declaration uses 'sorry'" in m["text"] for m in msgs):
        fail("SOLVED: a declaration uses sorry")
    for e in ch.exercises:
        ax = axioms(msgs, e["name"])
        if ax is None:
            fail(f"SOLVED: no axioms line for {e['name']}")
        extra = [a for a in ax if a not in book.ALLOWED_AXIOMS]
        if extra:
            fail(f"SOLVED: {e['name']} rests on {extra}")
    print("ok   SOLVED (every exercise proved, allowed axioms only)")

    reader = "\n\n".join(book.reader_code(b) for b in ch.blocks if b.kind == "code") + "\n"
    msgs = lean(reader + book.grader(ch))
    errs = [m for m in msgs if m["sev"] == "error"]
    if errs:
        fail("READER: " + "; ".join(f'{m["line"]}: {m["text"][:300]}' for m in errs[:5]))
    for e in ch.exercises:
        ax = axioms(msgs, e["name"])
        if ax is None or "sorryAx" not in ax:
            fail(f"READER: {e['name']} is not open in the reader's version (axioms {ax})")
    print("ok   READER (every exercise open, statements restate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
