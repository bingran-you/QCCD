"""The book: chapters written as Lean files, read and run in the browser.

A chapter is ONE solved Lean file (`book/chapters/<slug>.lean`), in the style of Software
Foundations: prose in `/-! ... -/` blocks, Lean between them.  An exercise is a prose block
whose first line is a heading

    #### Exercise 3 (★★) `name`

followed by a code block holding the theorem `name` with its proof.  From the solved file this
module derives everything a reader sees:

* the page, prose rendered and every code block an editable cell;
* the reader's version of each exercise: the statement kept verbatim, the proof `sorry`;
* a hidden grader, appended after the reader's text when the page runs it, that for every
  exercise states the ORIGINAL statement again (`example : <statement> := @name`, so an edited
  statement fails) and prints the axioms the proof depends on (so `sorry`, or a compiler
  trusted through `native_decide`, is seen).

The solved file is checked by Lean as it is, so every exercise is known to be solvable, and
the reader's version is the same file with proofs removed, so it is known to be well formed
apart from them.  Nothing here runs Lean; the page does, in the reader's browser.

    python -m qccd.site.book OUT_DIR [--packs DIR]
        writes OUT_DIR/book/: index.html, the chapters, book-page.js, data/ (the certificates the
        chapters prove) and engine/ (the runtime scripts; with --packs, also engine/site/: the
        Lean engine and library packs that tools/book/build_packs.py writes -- 200 MB, never in git)
"""

from __future__ import annotations

import html
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOOK = ROOT / "book"
HERE = Path(__file__).resolve().parent
ASSETS = HERE / "book"

#: axioms an exercise may rest on: propositional extensionality, quotient soundness and choice
#: are the three every Lean proof about logic may assume.  `sorryAx` (an unproved step) and the
#: auxiliary axioms `native_decide` adds (the compiler trusted) are not on the list.
ALLOWED_AXIOMS = ("propext", "Quot.sound", "Classical.choice")

EXERCISE_RE = re.compile(r"^#{2,4}\s*Exercise\s+(\d+)\s*\(([★]+)\)\s*`([A-Za-z_][A-Za-z0-9_'.]*)`\s*$")


# ----------------------------------------------------------------------------- parsing

@dataclass
class Block:
    kind: str                 # "prose" | "code"
    text: str                 # prose: the markdown; code: the Lean, solved
    exercise: dict | None = None   # on a code block that is an exercise's
    hidden: bool = False      # the import line: run, not shown


@dataclass
class Chapter:
    slug: str
    title: str
    blocks: list[Block] = field(default_factory=list)
    exercises: list[dict] = field(default_factory=list)


def split_blocks(src: str) -> list[Block]:
    """Prose is every `/-! ... -/` at the start of a line; everything else is Lean.  Nested
    block comments inside prose are not supported (the book never writes one)."""
    src = src.replace("\r\n", "\n")
    out: list[Block] = []
    i = 0
    for m in re.finditer(r"(?m)^/-!(.*?)-/[ \t]*\n?", src, flags=re.S):
        code = src[i:m.start()]
        if code.strip():
            out.append(Block("code", code.strip("\n")))
        out.append(Block("prose", _dedent(m.group(1))))
        i = m.end()
    tail = src[i:]
    if tail.strip():
        out.append(Block("code", tail.strip("\n")))
    return out


def _dedent(s: str) -> str:
    lines = s.strip("\n").splitlines()
    return "\n".join(l.rstrip() for l in lines).strip()


def _scan_to(s: str, i: int, stop: str) -> int:
    """Index of the first `stop` at bracket depth 0 at or after `i`, or -1.  `stop` may be
    two characters (`:=`)."""
    depth = 0
    opens, closes = "([{⟨", ")]}⟩"
    while i < len(s):
        c = s[i]
        if c in opens:
            depth += 1
        elif c in closes:
            depth -= 1
        elif depth == 0 and s.startswith(stop, i):
            # `:` must not be the start of `:=`
            if stop == ":" and s.startswith(":=", i):
                i += 2
                continue
            return i
        i += 1
    return -1


def exercise_parts(code: str, name: str) -> dict:
    """Split `theorem name BINDERS : TYPE := PROOF` into its pieces.  The statement is
    everything up to and including `:=`; the reader gets it verbatim followed by `by sorry`."""
    m = re.search(rf"(?m)^theorem\s+{re.escape(name)}\b", code)
    if not m:
        raise ValueError(f"exercise `{name}`: no `theorem {name}` in its code block")
    head_start, after_name = m.start(), m.end()
    assign = _scan_to(code, after_name, ":=")
    if assign < 0:
        raise ValueError(f"exercise `{name}`: no `:=` after the statement")
    colon = _scan_to(code, after_name, ":")
    if colon < 0 or colon > assign:
        raise ValueError(f"exercise `{name}`: no `:` separating binders from the type")
    binders = code[after_name:colon].strip()
    typ = code[colon + 1:assign].strip()
    before = code[:head_start]
    statement = code[head_start:assign + 2]
    proof = code[assign + 2:]
    after = ""
    # the proof runs to the end of the block, or to the first top-level command after it
    nxt = re.search(r"(?m)^(theorem|def|example|#\w+|structure|instance|abbrev)\b", proof)
    if nxt:
        proof, after = proof[:nxt.start()], proof[nxt.start():]
    expected = f"∀ {binders}, {typ}" if binders else typ
    return {"before": before, "statement": statement, "proof": proof.rstrip(),
            "after": after, "expected": " ".join(expected.split()), "binders": binders, "type": typ}


def parse_chapter(path: Path, slug: str) -> Chapter:
    blocks = split_blocks(path.read_text(encoding="utf-8"))
    title = ""
    ch = Chapter(slug=slug, title="")
    pending = None
    for b in blocks:
        if b.kind == "prose":
            first = b.text.splitlines()[0] if b.text else ""
            if not title and first.startswith("# "):
                title = first[2:].strip()
            for line in b.text.splitlines():
                m = EXERCISE_RE.match(line.strip())
                if m:
                    pending = {"n": int(m.group(1)), "stars": len(m.group(2)), "name": m.group(3)}
        elif pending is not None:
            parts = exercise_parts(b.text, pending["name"])
            ex = dict(pending, **{k: parts[k] for k in ("statement", "expected", "before", "after")},
                      solution=parts["proof"])
            ex["id"] = f"book:V{slug.split('-')[0]}.ex{ex['n']}"
            b.exercise = ex
            ch.exercises.append(ex)
            pending = None
        if b.kind == "code" and b.text.lstrip().startswith("import ") and not ch.blocks:
            b.hidden = True
        ch.blocks.append(b)
    if pending is not None:
        raise ValueError(f"exercise `{pending['name']}` has no code block after its heading")
    ch.title = title or slug
    names = [e["name"] for e in ch.exercises]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate exercise names in {path.name}: {names}")
    return ch


def reader_code(b: Block) -> str:
    """What the reader's cell starts as: an exercise's proof replaced by `sorry`."""
    if not b.exercise:
        return b.text
    e = b.exercise
    return (e["before"] + e["statement"] + " by\n  sorry\n" + (e["after"] if e["after"].strip() else "")).rstrip()


def grader(ch: Chapter) -> str:
    """The hidden block run after the reader's text.  One `example` and one `#print axioms`
    per exercise, each on its own line, so the page reads every verdict off its line."""
    out = ["", "-- the book's grader: not part of the chapter"]
    for e in ch.exercises:
        # a restated statement names its binders and uses none of them: not the reader's lint
        out.append(f"set_option linter.unusedVariables false in example : {e['expected']} := @{e['name']}")
        out.append(f"#print axioms {e['name']}")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------- prose

def _inline(s: str) -> str:
    out, i = [], 0
    for m in re.finditer(r"`([^`]+)`", s):
        out.append(_emph(html.escape(s[i:m.start()], quote=False)))
        out.append("<code>" + html.escape(m.group(1), quote=False) + "</code>")
        i = m.end()
    out.append(_emph(html.escape(s[i:], quote=False)))
    return "".join(out)


def _emph(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    return s


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"<[^>]+>", "", s).lower()).strip("-")


def prose_html(md: str, ex: dict | None = None) -> str:
    """The subset the book writes: headings, paragraphs, bullet and numbered lists, tables,
    fenced code.  Anything else is a paragraph."""
    lines = md.splitlines()
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        st = ln.strip()
        if not st:
            i += 1
            continue
        if st.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            out.append('<pre class="lean static"><code>' + html.escape("\n".join(lines[i + 1:j])) + "</code></pre>")
            i = j + 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", st)
        if m:
            level = len(m.group(1))
            em = EXERCISE_RE.match(st)
            if em:
                stars = '<span class="stars" aria-label="difficulty">' + em.group(2) + "</span>"
                out.append(f'<h4 class="exercise" id="ex-{em.group(3)}">Exercise {em.group(1)} {stars} '
                           f'<code>{html.escape(em.group(3))}</code> <span class="exstate" data-ex="{em.group(3)}">open</span></h4>')
            else:
                tag = "h1" if level == 1 else f"h{level}"
                text = _inline(m.group(2))
                out.append(f'<{tag} id="{_slug(text)}">{text}</{tag}>')
            i += 1
            continue
        if st.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            head, body = rows[0], rows[1:]
            out.append("<table class=\"tbl\"><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</tbody></table>")
            continue
        if re.match(r"^([*-]|\d+\.)\s+", st):
            ordered = bool(re.match(r"^\d+\.", st))
            items = []
            while i < len(lines) and lines[i].strip():
                s2 = lines[i].strip()
                mm = re.match(r"^([*-]|\d+\.)\s+(.*)$", s2)
                if mm:
                    items.append(mm.group(2))
                elif items:
                    items[-1] += " " + s2           # a continuation line of the last item
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(t)}</li>" for t in items) + f"</{tag}>")
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,4}\s|```|\||[*-]\s|\d+\.\s)", lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(out)


# ----------------------------------------------------------------------------- published cases

COMPILED = HERE / "compiled"        # the Compilation page's examples (compile_examples.py)


def _s(x) -> str:
    return json.dumps(x, ensure_ascii=False)       # a Lean string literal is a JSON one here


def lean_input(ident: str, d: dict) -> str:
    """A qcheck input document as the Lean value `Input.ofJson` would read from it: the same
    fields in the same order, so a theorem about it is a theorem about the published file."""
    qmap = sorted(((int(k), v) for k, v in d.get("map", {}).items()), key=lambda kv: kv[0])
    strs = lambda xs: "[" + ", ".join(_s(x) for x in xs) + "]"
    nats = lambda xs: "[" + ", ".join(str(int(x)) for x in xs) + "]"
    return "\n".join([
        f"def {ident} : Input :=",
        f"  {{ circuit := {_s(d.get('circuit', '?'))}, arch := {_s(d.get('arch', '?'))}, nQubits := {int(d.get('n_qubits', 0))},",
        "    qmap := [" + ", ".join(f"({q}, {_s(i)})" for q, i in qmap) + "],",
        "    init := [" + ", ".join(f"({_s(k)}, {_s(v)})" for k, v in d.get("init", {}).items()) + "],",
        "    ops := [" + ", ".join(f"⟨{o['i']}, {_s(o['name'])}, {nats(o['qubits'])}⟩" for o in d.get("circuit_ops", [])) + "],",
        "    moves := [" + ", ".join(f"⟨{m['t']}, {_s(m['ion'])}, {_s(m['from'])}, {_s(m['to'])}⟩" for m in d.get("moves", [])) + "],",
        "    rots := [" + ", ".join(f"⟨{r['t']}, {_s(r['loop'])}, {int(r['delta'])}⟩" for r in d.get("rotations", [])) + "],",
        "    gates := [" + ", ".join(f"⟨{g['dag']}, {g['t']}, {_s(g['site'])}, {strs(g['ions'])}⟩" for g in d.get("gates", [])) + "],",
        f"    unrealised := {nats(d.get('unrealised', []))},",
        f"    gateSites := {strs(d.get('gate_sites', []))},",
        "    hops := [" + ", ".join(f"({_s(h['from'])}, {_s(h['to'])})" for h in d.get("hops", [])) + "],",
        "    loops := [" + ", ".join(f"({_s(k)}, {strs(v)})" for k, v in (d.get("loops") or {}).items()) + "] }",
    ])


def published_cases() -> list[dict]:
    """Every Compilation-page example with a checker input: what the page offers to prove."""
    out = []
    for qc in sorted(COMPILED.glob("*/prog.qcheck.json")):
        eid = qc.parent.name
        d = json.loads(qc.read_text(encoding="utf-8"))
        ident = "case_" + re.sub(r"[^A-Za-z0-9_]", "_", eid)
        out.append({"id": eid, "arch": d.get("arch"), "ops": len(d.get("circuit_ops", [])),
                    "moves": len(d.get("moves", [])), "rots": len(d.get("rotations", [])),
                    "witnesses": len(d.get("gates", [])), "ident": ident,
                    "lean": lean_input(ident, d) + f"\n\ntheorem {ident}_ok : Implements {ident} := check_sound (by decide +kernel)\n"
                            f"#print axioms {ident}_ok\n",
                    "files": {"qcheck": f"data/{eid}.qcheck.json", "qcert": f"data/{eid}.qcert.json"},
                    "_dir": qc.parent})
    return out


def devices() -> dict:
    """The geometry of every device a figure may draw, from its own document: node positions
    in model units, rails, loops, and which sites can gate."""
    out = {}
    for f in sorted(COMPILED.glob("*.arch.json")):
        if f.name.endswith(".expanded.json"):
            continue
        a = json.loads(f.read_text(encoding="utf-8"))
        g, zt = a["geometry"], a.get("zone_types", {})
        out[a["name"]] = {
            "nodes": [[n["id"], n["pos"][0], n["pos"][1], n["kind"],
                       bool(zt.get(n.get("zone_type") or "", {}).get("gate"))] for n in g["nodes"]],
            "segs": [s["ends"] for s in g.get("segments", [])],
            "loops": [{"id": l["id"], "nodes": l["nodes"], "closed": bool(l.get("closed"))} for l in g.get("loops", [])],
        }
    return out


FIGURE_RE = re.compile(r"^\{\{figure\s+([A-Za-z_][A-Za-z0-9_']*)((?:\s+[a-z]+=[^\s}]+)*)\s*\}\}$")


def figure_html(name: str, params: str) -> str:
    kv = dict(p.split("=", 1) for p in params.split())
    attrs = "".join(f' data-{k}="{html.escape(v)}"' for k, v in kv.items() if k in ("at", "show"))
    return (f'<figure class="leanfig" data-name="{html.escape(name)}"{attrs}>'
            '<div class="figwait">This figure is drawn from Lean\'s own replay of '
            f'<code>{html.escape(name)}</code>; it appears when Lean has checked the chapter.</div></figure>')


CERTS_PANEL = ('<div class="certs" id="certs"><div class="act"><button type="button" id="certsgo" disabled>'
               'Prove all of them</button><span id="certsum">Lean is loading.</span></div><div id="certrows"></div></div>')


# ----------------------------------------------------------------------------- pages

def chapter_body(ch: Chapter, vol: int, n: int, book: dict) -> str:
    cells, k = [], 0
    for b in ch.blocks:
        if b.kind == "prose":
            figs, lines = [], []
            for line in b.text.replace("{{certificates}}", "\n\n@@CERTS@@\n\n").splitlines():
                m = FIGURE_RE.match(line.strip())
                if m:
                    figs.append(figure_html(m.group(1), m.group(2)))
                    lines += ["", f"@@FIG{len(figs) - 1}@@", ""]
                else:
                    lines.append(line)
            body = prose_html("\n".join(lines)).replace("<p>@@CERTS@@</p>", CERTS_PANEL)
            for i, f in enumerate(figs):
                body = body.replace(f"<p>@@FIG{i}@@</p>", f)
            cells.append('<div class="prose">' + body + "</div>")
            continue
        code = reader_code(b)
        attrs = f' data-cell="{k}"'
        if b.hidden:
            attrs += ' data-hidden="1"'
        if b.exercise:
            attrs += f' data-ex="{html.escape(b.exercise["name"])}"'
        cells.append(f'<div class="cell"{attrs}><textarea rows="1" spellcheck="false" autocapitalize="off" autocomplete="off"'
                     f' aria-label="Lean code">{html.escape(code)}</textarea><div class="msgs" aria-live="polite"></div></div>')
        k += 1
    data = {"slug": ch.slug, "vol": vol, "chapter": n, "title": ch.title,
            "exercises": [{"n": e["n"], "stars": e["stars"], "name": e["name"], "id": e["id"]} for e in ch.exercises],
            "grader": grader(ch), "allowed": list(ALLOWED_AXIOMS),
            # what the browser's Lean puts in front of the reader's text, in place of the
            # chapter's own `import` line (its engine compiles every file as a module)
            "prelude": (ASSETS / "engine" / "prelude.lean").read_text(encoding="utf-8"),
            # the figures: Lean's replay of a named certificate, drawn on its device
            "figureHelper": (ASSETS / "engine" / "figure.lean").read_text(encoding="utf-8"),
            "devices": devices(),
            "cases": ([{k: v for k, v in c.items() if not k.startswith("_")} for c in published_cases()]
                      if any("{{certificates}}" in b.text for b in ch.blocks if b.kind == "prose") else [])}
    head = (f'<p class="crumbs"><a href="index.html">{html.escape(book["title"])}</a> &rsaquo; '
            f'Volume {vol} &rsaquo; Chapter {n}</p>')
    bar = ('<div id="leanbar" role="status"><span class="dot"></span><span class="what">Lean is loading'
           '</span><span class="prog"></span><span class="score"></span></div>')
    return (head + bar + '<article id="chapter">' + "\n".join(cells) + "</article>"
            + '<aside id="goals" hidden><div class="gh">Goals at the cursor</div><pre></pre></aside>'
            + f'<script type="application/json" id="bookdata">{script_json(data)}</script>')


def script_json(data) -> str:
    """JSON for a `<script type="application/json">`, whose text the browser does NOT decode as
    HTML: entity-escaping it (`=>` as `=&gt;`) corrupts the Lean inside.  Only a closing tag or a
    comment opener can end the element early, so only those are escaped, as JSON escapes."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\u0021--")


def index_body(book: dict) -> str:
    vols = []
    for v in book["volumes"]:
        rows = []
        for c in v["chapters"]:
            live = c.get("status") == "live"
            name = html.escape(c["title"])
            link = f'<a href="{c["slug"]}.html">{name}</a>' if live else name
            state = '<span class="cstate live">ready</span>' if live else '<span class="cstate">planned</span>'
            prog = f' <span class="cprog" data-chapter="{c.get("slug", "")}"></span>' if live else ""
            rows.append(f'<li><span class="cn">{v["n"]}.{c["n"]}</span> {link} {state}{prog}</li>')
        vols.append(f'<section class="vol"><h2 id="v{v["n"]}">Volume {v["n"]}: {html.escape(v["title"])}</h2>'
                    f'<p>{html.escape(v["about"])}</p><ul class="chapters">{"".join(rows)}</ul></section>')
    intro = ('<p>A book in the style of <i>Software Foundations</i>: every chapter is a Lean file, '
             'and the Lean runs here, in your browser. Read a chapter, edit any of its code, and do its '
             'exercises; Lean checks each one as you type. Nothing you write leaves your computer.</p>'
             '<p>The first time a chapter opens, your browser downloads Lean itself (about 57&nbsp;MB, '
             'kept for next time). A desktop browser is needed; on a phone the chapters read but may not run.</p>')
    engine = ('<p class="sub" style="margin-top:28px">The Lean in these pages is Lean&nbsp;4 '
              '(Apache-2.0), compiled to WebAssembly by the '
              '<a href="https://github.com/cauli/lean4-wasm-in-browser">cauli/lean4</a> browser build '
              '(Lean 4.33, Apache-2.0), hosted here byte for byte. The proved checker it loads is the '
              'repository\'s <code>QCCDC.Cert</code>, carried to that Lean by a mechanical port '
              '(<code>tools/book/port_checker.py</code>): module syntax, and two tail-recursive '
              'functions each proved equal to the one it replaces. The port gives the same verdict as the '
              'repository\'s checker on every published certificate.</p>')
    return (f'<h1>{html.escape(book["title"])}</h1><p class="sub">{html.escape(book["subtitle"])}</p>'
            + intro + "".join(vols) + engine)


def build(out: Path, packs: Path | None = None) -> list[Path]:
    from .build import PAGE, STYLE            # the site's own document page and stylesheet
    book = json.loads((BOOK / "book.json").read_text(encoding="utf-8"))
    css = (ASSETS / "book.css").read_text(encoding="utf-8")
    dst = out / "book"
    dst.mkdir(parents=True, exist_ok=True)
    written = []
    page = PAGE.format(title=f'{html.escape(book["title"])} - QCCD studio', style=STYLE, extra_css=css,
                       body=index_body(book))
    (dst / "index.html").write_text(page.replace("__FOOTER__", ""), encoding="utf-8")
    written.append(dst / "index.html")
    for v in book["volumes"]:
        for c in v["chapters"]:
            if c.get("status") != "live":
                continue
            ch = parse_chapter(BOOK / c["source"], c["slug"])
            body = chapter_body(ch, v["n"], c["n"], book)
            page = PAGE.format(title=f'{html.escape(ch.title)} - {html.escape(book["title"])}', style=STYLE,
                               extra_css=css, body=body)
            # the Lean runtime first (it defines window.Book), the figures, then the page
            page = page.replace("</body>", '<script src="engine/book-runtime.js" defer></script>'
                                           '<script src="book-figure.js" defer></script>'
                                           '<script src="book-page.js" defer></script></body>')
            (dst / f'{c["slug"]}.html').write_text(page.replace("__FOOTER__", ""), encoding="utf-8")
            written.append(dst / f'{c["slug"]}.html')
    for name in ("book-page.js", "book-figure.js"):
        (dst / name).write_bytes((ASSETS / name).read_bytes())
        written.append(dst / name)
    eng = dst / "engine"
    eng.mkdir(exist_ok=True)
    for name in ("book-runtime.js", "lean-worker.js"):
        (eng / name).write_bytes((ASSETS / "engine" / name).read_bytes())
        written.append(eng / name)
    if packs is not None:
        site = packs / "site" if (packs / "site" / "manifest.json").exists() else packs
        if not (site / "manifest.json").exists():
            raise FileNotFoundError(f"{packs}: no manifest.json (run tools/book/build_packs.py)")
        (eng / "site").mkdir(exist_ok=True)
        for f in sorted(site.iterdir()):
            if f.is_file():
                shutil.copyfile(f, eng / "site" / f.name)
                written.append(eng / "site" / f.name)
    # the certificates the chapters prove, published beside them: what the page checks is a
    # file the reader can download and check again with the command-line checker
    (dst / "data").mkdir(exist_ok=True)
    for c in published_cases():
        for kind, fname in (("qcheck", "prog.qcheck.json"), ("qcert", "prog.qcert.json")):
            src = c["_dir"] / fname
            if src.exists():
                (dst / c["files"][kind]).write_bytes(src.read_bytes())
                written.append(dst / c["files"][kind])
    return written


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    packs = Path(argv[argv.index("--packs") + 1]) if "--packs" in argv else None
    written = build(Path(argv[0]), packs)
    print(f"wrote {len(written)} files under {Path(argv[0]) / 'book'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
