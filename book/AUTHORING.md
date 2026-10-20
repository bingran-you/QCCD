# Writing a Lean lesson of the tutorial

The tutorial's Parts E–I are lessons whose code is Lean that runs in the reader's browser, in
the style of *Software Foundations*: the lesson is its source file, every code block is editable,
and every exercise is checked by Lean as the reader types. F1 (`chapters/2.1-certificates.lean`)
is the model; read it first, in the browser and as source.

## Words

These lessons are part of **the tutorial**, beside Parts A–D. A reader never sees "book",
"volume" or "chapter" (a test fails if a lesson page says them): it is "Part F", "lesson F2",
"this lesson". Write in the site's voice: plain, precise, short paragraphs; "programme" for a
hardware programme, "circuit" for the source. Explain Lean the first time a lesson uses a piece
of it; Part E assumes no Lean, Part F builds on E, G-I assume E-F.

## The file

`book/chapters/<slug>.lean` (the slug and the progress ids are already in `book/book.json`; do
not edit that file -- the maintainer publishes a lesson by setting its status to `live`).

```lean
import QCCDC.Cert.Sound

/-!
# The lesson's title

*Part F, lesson F2. You need lesson F1. ...*

Prose, Markdown: paragraphs, `code`, **bold**, *italic*, lists, tables, ``` fences,
[links](https://...). Headings: `## 1. Section`.
-/

open QCCDC.Cert

def something := ...

/-!
#### Exercise 1 (★) `name`
What to prove, and any hint.
-/

theorem name : statement := by
  the solution
```

* **The import line** is the first line. Natively it imports the proved checker; in the browser
  it is replaced by the engine's prelude, which provides the same names. Nothing else can be
  imported: only Lean core and `QCCDC.Cert.*` (`Syntax`, `Check`, `Perm`, `Sound`, `Commute`,
  with `Lean.Data.Json`). **No Mathlib, no Batteries.**
* **Prose** is every `/-! ... -/` that starts a line; everything else is Lean, shown in order.
* **An exercise** is a heading line `#### Exercise N (★|★★|★★★) \`name\`` in prose, followed by a
  code block holding `theorem name <binders> : <statement> := <proof>`. The file holds the
  SOLUTION. The reader gets the statement verbatim and `by sorry`; a hidden grader then requires
  the statement unchanged and the proof to rest only on `propext`, `Quot.sound`,
  `Classical.choice` -- so no `sorry`, no `native_decide`. Declarations after the theorem in the
  same block (an `#eval`, a `#print axioms`) stay visible to the reader.
* **6-10 exercises per lesson**, numbered from 1, names unique. Mix: ★ computation or one tactic
  (`rfl`, `decide +kernel`, `simp`); ★★ apply a named lemma or a short proof; ★★★ a general
  theorem (all certificates, all programmes), induction, a new lemma.
* **A setup block** is a code block whose first line is `-- setup`: run, never shown. Use it to
  restate definitions the lesson builds on from an earlier lesson (each page is its own Lean
  document; it cannot import another lesson). Keep it short.

## Figures: show what is proved

Wherever a statement is about a concrete device, programme or certificate, put a figure beside it
-- the reader must see what they are proving. A figure is a line of its own in prose:

* `{{figure NAME [at=CYCLE]}}` -- `NAME : QCCDC.Cert.Input` (a certificate). The page asks Lean to
  replay it (`QCCDBook.figure`: the checker's own `posAt`, `look`, `ionOf`, `check`), plays its
  moves and gates in the platform's studio, and lists every condition on every move and witness
  as Lean evaluated it. `arch` must be a device below.
* `{{studio NAME [at=STEP]}}` -- `NAME` is any value with fields `arch : String` and
  `src : String`: a studio programme the lesson builds in Lean (compute `src` from your own
  definitions where you can, so the figure follows the reader's edits). The studio plays it and
  says what its own rule checker thinks.
* `{{certificates}}` -- the panel proving the Compilation page's 19 published certificates.

Devices with a studio page: `chain4` (C0-C1-C2-C3, every trap gates), `grid2x3` (traps
T0_0h ... T1_1v between junctions J0_0 ... J1_2), `race8` (a closed loop S0..S7, every trap
gates), `ring6d` (loop L0 = S0..S5 that cannot gate, docks A0 off S0 and A3 off S3 that can).
Their documents: `qccd/site/compiled/<name>.arch.json`.

The studio's programme language (`qccd/api.py`, class `Program`): `p.init({"q0": "C1", ...})`,
`p.shuttle("q0", ["C1", "C0"])` (a path of adjacent nodes), `p.gate("CX", [["q0", "q1"]])` (a
pair in one trap), `p.gate("H", [], ["C1"])` (a one-qubit gate at a site), `p.cool()`,
`p.measure(["q0"])`, `p.reset([...])`, `p.rotate(delta, loop="L0")`, `p.barrier()`; one statement
per line.

## Two Leans

The page runs Lean 4.33 (WebAssembly, every file a module); the native check runs the
checker's Lean 4.29. A lesson must pass both. Practical rules:

* prefer tactics that are stable across versions (`decide +kernel`, `simp`, `omega`, `induction`,
  `cases`, `rfl`) over naming core library lemmas, whose names drift;
* plain `decide` gets stuck on `String` comparisons: use `decide +kernel`;
* the browser's interpreter has about 550 frames of stack: no deep non-tail recursion in an
  `#eval`; keep `#eval` output short;
* the whole lesson should check in a few seconds in the browser: keep kernel computations small
  (a certificate of tens of events, not thousands).

## Accuracy

Every claim about the QCCD platform must be true of the code as it is. Check it there:

* the proved checker: `Compiler/lean/QCCDC/Cert/{Syntax,Check,Perm,Sound,Commute}.lean`,
  `Compiler/lean/Main.lean` (`qcheck`);
* what feeds it: `Compiler/bridge/mk_qcheck_input.py` (hops, gate sites, loops from the device);
* the semantic half of R10: `Compiler/bridge/check_cert.py` (binding, witnesses, strict order,
  transport, readout, tableau / unitary against the source QASM);
* the pulse identities: `Compiler/lean/QCCDC/Pulse/{Native,Decompose}.lean` (with Mathlib);
  the compiler's lowering: `Compiler/ocaml/lib/gateset.ml`, `gateset_composites.ml`;
* the compiler: `Compiler/ocaml/lib/{place,route,compile,rotate_pipeline,cert}.ml`, `Compiler/README.md`;
* the rules: `qccd/verify/rules.py`, the rule language `qccd/ruleset/` and `docs/rule-language.md`;
* the official grader: `qccd/workspace/evaluator.py`, `docs/workspace.md`; the Compiler board:
  `qccd/bench/{contract,check,harness}.py`; the toolchain: `qccd/workspace/toolchain.py`,
  `deploy/toolchain/README.md`;
* the published certificates: `qccd/site/compiled/<id>/`.

Never invent a number; measure it or leave it out.

## Checking a lesson

    python tools/book/check_lesson.py F2

runs the solved file and the reader's version natively and checks the words and figures. Then in
the browser (one Chrome at a time on the maintainer's machine; the script waits its turn):

    bash <scratchpad>/leanweb/lesson-check.sh <out-dir> <port> F2

builds the lesson as a draft, serves it, and reports: loads clean, every exercise starts open,
every figure draws, every solution solves.
