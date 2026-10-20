import QCCDC.Cert.Sound

/-!
# Every verdict, reproduced

*Part H, lesson H4. You need lessons H1 and H3.*

Lesson H1 checked the O1 half of R10 on files the site publishes. This lesson does the whole of
R10 from nothing but the repository: the circuit, the device, the published compiler, the
cooling pass, the rules, the checker input, the proved checker, and `check_cert.py`, which ties
them together and decides R10. You run it on the Compilation page's GHZ chain, compare what you
get with what the page published, and then state in Lean what a verdict of `passed` requires.
-/

open QCCDC.Cert

/-!
## 1. Once: the repository, the compiler, the checker

From a terminal. When this lesson was written, the Windows blocks below were run as written in
PowerShell from a fresh clone, and the commands of the macOS and Linux blocks in Git Bash on the
same Windows machine (with the venv's `Scripts` folder in place of `bin`); they were not run on
macOS or Linux.

macOS or Linux:

```
git clone -b compiler https://github.com/yezhuoyang/QCCD
cd QCCD
python3.13 -m venv .venv && source .venv/bin/activate   # any Python 3.10 or newer
pip install -e ".[agent]"                     # includes qiskit, stim and numpy, which check_cert.py uses
qccd toolchain install                        # the compiler (on a Mac, see below)
(cd Compiler/lean/checker && lake build)      # the checker (lesson H1)
QCCDC=$(python -c "from qccd.workspace.evaluator import Toolchain; print(Toolchain.discover().qccdc)")
```

Windows (PowerShell):

```
git clone -b compiler https://github.com/yezhuoyang/QCCD
cd QCCD
py -m venv .venv
.venv\Scripts\activate
pip install -e ".[agent]"
qccd toolchain install
cd Compiler\lean\checker; lake build; cd ..\..\..
$QCCDC = python -c "from qccd.workspace.evaluator import Toolchain; print(Toolchain.discover().qccdc)"
```

`qccd toolchain install` downloads the compiler `qccdc_cli` that the repository pins
(`qccd/workspace/toolchain.json`: a version, and for each platform a URL, a size and a SHA-256),
refuses it unless the size and the hash match, installs it under `~/.qccd/toolchain/`, and runs it
once on a test circuit. The pinned version has prebuilt compilers for Windows and Linux on x86-64;
when this lesson was written there was none for macOS, and on a Mac the command says so and says
how to build one: OCaml 5.3 with opam (`brew install opam`, `opam init`,
`opam switch create 5.3.0`, `opam install dune yojson.2.2.2`), then `dune build bin/qccdc_cli.exe`
in `Compiler/ocaml`. The last line of each block asks the workspace which compiler it would use --
`QCCD_QCCDC` if you set it, else the one built in the checkout, else the installed one -- so it
works either way. `qccd toolchain status` shows the same.

## 2. The GHZ chain, from scratch

The circuit is in the repository as the GHZ starter board's task circuit; it is byte for byte the
Compilation page's `algo_ghz` (lesson H3). The device is made by the same generator call the page
used for `chain4`. Run everything **from the repository root**: three of the scripts
(`insert_cooling.py`, `check_tsir.py`, `check_cert.py`) read a relative `--arch` path against the
repository root, not against the current folder.

macOS or Linux:

```
mkdir -p work/ghz
cp qccd/workspace/releases/ghz4@1/circuit.qasm work/ghz/circuit.qasm
python -c "import json; from qccd.api import Machine; m = Machine.chain(n=4, name='chain4'); open('work/ghz/chain4.arch.json', 'w', newline='\n').write(json.dumps(m.arch.to_json(), indent=1) + '\n')"
python Compiler/bridge/export_arch.py work/ghz/chain4.arch.json -o work/ghz/chain4.expanded.json
"$QCCDC" compile work/ghz/circuit.qasm --arch work/ghz/chain4.expanded.json -o work/ghz/prog
python Compiler/bridge/insert_cooling.py work/ghz/prog.tsir.json --arch work/ghz/chain4.arch.json -o work/ghz/prog.cooled.tsir.json
python Compiler/bridge/check_tsir.py work/ghz/prog.cooled.tsir.json --arch work/ghz/chain4.arch.json --model corrected
python Compiler/bridge/mk_qcheck_input.py work/ghz/prog --arch work/ghz/chain4.expanded.json -o work/ghz/prog.qcheck.json
Compiler/lean/checker/.lake/build/bin/qcheck work/ghz/prog.qcheck.json
python Compiler/bridge/check_cert.py work/ghz/prog --qasm work/ghz/circuit.qasm --arch work/ghz/chain4.arch.json --qcheck work/ghz/prog.qcheck.json
```

Windows (PowerShell):

```
mkdir work\ghz
Copy-Item "qccd\workspace\releases\ghz4@1\circuit.qasm" work\ghz\circuit.qasm
python -c "import json; from qccd.api import Machine; m = Machine.chain(n=4, name='chain4'); open('work/ghz/chain4.arch.json', 'w', newline='\n').write(json.dumps(m.arch.to_json(), indent=1) + '\n')"
python Compiler\bridge\export_arch.py work\ghz\chain4.arch.json -o work\ghz\chain4.expanded.json
& $QCCDC compile work\ghz\circuit.qasm --arch work\ghz\chain4.expanded.json -o work\ghz\prog
python Compiler\bridge\insert_cooling.py work\ghz\prog.tsir.json --arch work\ghz\chain4.arch.json -o work\ghz\prog.cooled.tsir.json
python Compiler\bridge\check_tsir.py work\ghz\prog.cooled.tsir.json --arch work\ghz\chain4.arch.json --model corrected
python Compiler\bridge\mk_qcheck_input.py work\ghz\prog --arch work\ghz\chain4.expanded.json -o work\ghz\prog.qcheck.json
Compiler\lean\checker\.lake\build\bin\qcheck.exe work\ghz\prog.qcheck.json
python Compiler\bridge\check_cert.py work\ghz\prog --qasm work\ghz\circuit.qasm --arch work\ghz\chain4.arch.json --qcheck work\ghz\prog.qcheck.json
```

What each step is:

| step | writes | what it does |
|---|---|---|
| `Machine.chain(n=4, name='chain4')` | `chain4.arch.json` | the device document, from the generator call the Compilation page used |
| `export_arch.py` | `chain4.expanded.json` | the device expanded: every node's capacities and abilities, the rails, the SIMD classes; what the compiler and the checker input read |
| `qccdc_cli compile` | `prog.tsir.json`, `prog.qcert.json` | the programme and its certificate. It prints `self-check OK`: the compiler's own pre-flight check, which nothing trusts |
| `insert_cooling.py` | `prog.cooled.tsir.json` | cooling wherever a gate would start on ions too hot to gate (R7) |
| `check_tsir.py` | | replays the cooled programme and evaluates the rules. It lists R10 as skipped: R10 is not decided by the replay |
| `mk_qcheck_input.py` | `prog.qcheck.json` | the certificate plus the device's gate sites, hops and loops |
| `qcheck` | | O1, by the proved checker |
| `check_cert.py --qcheck` | | the whole of R10 |

`check_cert.py` reads the programme the certificate speaks about, `prog.tsir.json`, and the
circuit, and checks, in one run: that the qcheck input was made from **this** certificate on
**this** device (it rebuilds the input and refuses a file that differs); the binding (the
certificate's `circuit_ops` are the QASM op by op, its map covers the QASM's qubits); O1,
including that each witness names a real instruction of the right kind and that the
programme's own transport is the certificate's moves; each qubit's ops in the circuit's order
in the programme; `qcheck`'s verdict; and O2 -- the pulses composed and compared with the
circuit, each measurement and reset in its place, as a stabilizer tableau or, outside the
Clifford gates and up to 10 qubits counting mid-circuit readouts, as an exact unitary. R10 is
`passed` only when all of them are; section 4 states the rule. On the GHZ chain it ends:

```
  -> R10 passed: O1 by the proved Lean checker (QCCDC.Cert.check_sound); O2 by stabilizer
     tableau, composed from the emitted pulses, against the source QASM
```

and exits with status 0 (status 1 for `FAILED`).

## 3. Compare with what the page published

Save [algo_ghz.qcheck.json](data/algo_ghz.qcheck.json) and
[algo_ghz.qcert.json](data/algo_ghz.qcert.json) into `work/ghz`, and compare them with yours as
JSON:

```
python -c "import json, sys; a, b = (json.load(open(f)) for f in sys.argv[1:]); print(a == b)" work/ghz/prog.qcheck.json work/ghz/algo_ghz.qcheck.json
python -c "import json, sys; a, b = (json.load(open(f)) for f in sys.argv[1:]); print(a == b)" work/ghz/prog.qcert.json work/ghz/algo_ghz.qcert.json
```

Both print `True`. When this lesson was written the same held for all 19 examples of the
Compilation page: from a fresh clone, with the pinned compiler (version `f88754a2fa71`), each
one's programme, certificate and checker input came out equal to the files the page was built
from -- on Windows byte for byte -- and its R10 verdict the same. (Compare as JSON on other
systems: `mk_qcheck_input.py` ends its file with the system's line ending, and the page's files
were made on Windows.) `algo_rot` uses the rotation pass, `qccdc_cli rotate` in place of
`compile`; the rest use `compile`. So the proofs of lesson H1 are proofs about the files you just
made.

The same pipeline reproduces a board entry whose circuit and device are in the repository. Lesson
H2's repetition-code entry: `Compiler/examples/rep9_esm.qasm` on `arch/stationary_chain.arch.json`
gives a checker input equal to the one that lesson proves.

## 4. The verdict, in Lean

`check_cert.py --json` writes the verdict as JSON. Yours, for the GHZ chain:

```
{ "program": "circuit", "arch": "chain4",
  "circuit_sha256": "a6c1803b8301e79ceba044278137397890d40a37eb2eaaa6057b8e96a49bdd00",
  "binding": "ok", "transport": "ok", "o1_transport": "ok", "order": "ok",
  "lean": "accepted", "unrealised_ops": 0,
  "readout": "ok: 0 measurement(s)/reset(s) compared in place, final measurements at the end",
  "o2_semantics": "ok",
  "method": "stabilizer tableau, composed from the emitted pulses, against the source QASM",
  "R10": "passed",
  "R10_reason": "O1 by the proved Lean checker (QCCDC.Cert.check_sound); O2 by stabilizer tableau, ..." }
```

(The verdicts the page was built with have no `"transport"` field: that check was added to
`check_cert.py` after they were made. Its result is also folded into `"o1_transport"`.)

`R10` is decided from these fields by one chain of tests at the end of `check_cert.py`'s `main`.
Here it is, with the fields as Lean data. A list of problems is empty where the JSON says `"ok"`;
`lean` is `some true` for `accepted`, `some false` for `REJECTED`, and `none` when `--qcheck`
was not given or `qcheck` is not built; `same` is O2: `some true` for `ok`, `some false` for a
mismatch, `none` when it could not be established.
-/

/-- R10's three verdicts. `partial` is a word Lean reserves, so that one is written `«partial»`. -/
inductive R10 where
  | passed | «partial» | failed
  deriving DecidableEq, Repr

/-- What `check_cert.py` decides R10 from. -/
structure Verdict where
  unbound : Bool          -- the qcheck input was not made from this certificate on this device
  lean : Option Bool      -- qcheck's verdict
  binding : List String   -- the certificate against the QASM
  o1 : List String        -- co-location, the witnesses against the programme, its transport
  unrealised : Nat        -- `unrealised_ops`
  order : List String     -- each qubit's ops in the circuit's order, in the programme
  same : Option Bool      -- O2
  deriving Repr

/-- The end of `check_cert.py`'s `main`, test for test. -/
def r10 (v : Verdict) : R10 :=
  if v.unbound then .failed
  else if v.lean = some false then .failed
  else if v.binding ≠ [] then .failed
  else if v.o1 ≠ [] ∨ v.unrealised ≠ 0 then .failed
  else if v.order ≠ [] then .failed
  else if v.same = some false then .failed
  else if v.same = some true ∧ v.lean = some true then .passed
  else .«partial»

def ghzVerdict : Verdict :=
  { unbound := false, lean := some true, binding := [], o1 := [], unrealised := 0, order := [],
    same := some true }

/-!
#### Exercise 1 (★) `ghz_verdict_passed`
Prove that your verdict, read through the rule, is `passed`.
-/

theorem ghz_verdict_passed : r10 ghzVerdict = .passed := by
  decide

/-!
#### Exercise 2 (★★) `refused_input_fails`
A checker input that `check_cert.py` cannot rebuild from the certificate and the device is not
run, and R10 fails, whatever else holds. Prove it for every verdict.
-/

theorem refused_input_fails {v : Verdict} (h : v.unbound = true) : r10 v = .failed := by
  simp [r10, h]

/-!
#### Exercise 3 (★★) `no_qcheck_no_pass`
Without the proved checker's verdict, R10 is at best `partial` -- which is why the official
grader (lesson H3) runs `qcheck` as a stage of its own and runs `check_cert.py` without it.
Prove that a verdict with `lean = none` is never `passed`. `unfold r10 at hp`, then
`repeat' split at hp` takes the chain apart; `simp_all` closes each piece.
-/

theorem no_qcheck_no_pass {v : Verdict} (h : v.lean = none) : r10 v ≠ .passed := by
  intro hp
  unfold r10 at hp
  repeat' split at hp
  all_goals simp_all

/-!
#### Exercise 4 (★★) `order_fails`
A reorder fails R10 whatever O2 says -- even when the two ops commute and the tableaux agree.
Prove it.
-/

theorem order_fails {v : Verdict} (h : v.order ≠ []) : r10 v = .failed := by
  unfold r10
  repeat' split
  all_goals simp_all

/-!
#### Exercise 5 (★★★) `passed_iff`
State exactly what `passed` requires of the fields, and prove it: no refused input, `qcheck`
accepted, no binding problem, no O1 problem, nothing unrealised, no order problem, and O2
established. `unfold r10`, `repeat' split`, then on each goal `simp_all`; a few goals are left as
implications, which `intros` followed by `simp_all` closes.
-/

theorem passed_iff (v : Verdict) :
    r10 v = .passed ↔ v.unbound = false ∧ v.lean = some true ∧ v.binding = [] ∧ v.o1 = [] ∧
      v.unrealised = 0 ∧ v.order = [] ∧ v.same = some true := by
  unfold r10
  repeat' split
  all_goals (simp_all; try (intros; simp_all))

/-!
## 5. A doctored input

The checker input is a file, and you can change it. Here is your GHZ chain's, with one change:
the first move is stamped cycle 9 instead of 3, so `q0` leaves `C3` after the CNOT that needs it
in `C2` at cycle 8.
-/

def ghz : Input :=
  { circuit := "circuit", arch := "chain4", nQubits := 4,
    qmap := [(0, "q0"), (1, "q1"), (2, "q2"), (3, "q3")],
    init := [("q0", "C3"), ("q1", "C2"), ("q2", "C1"), ("q3", "C0")],
    ops := [⟨0, "h", [0]⟩, ⟨1, "cx", [0, 1]⟩, ⟨2, "cx", [1, 2]⟩, ⟨3, "cx", [2, 3]⟩, ⟨4, "measure", [0]⟩, ⟨5, "measure", [1]⟩, ⟨6, "measure", [2]⟩, ⟨7, "measure", [3]⟩],
    moves := [⟨3, "q0", "C3", "C2"⟩, ⟨9, "q1", "C2", "C1"⟩, ⟨16, "q2", "C1", "C0"⟩],
    rots := [],
    gates := [⟨0, 2, "C3", ["q0"]⟩, ⟨1, 8, "C2", ["q0", "q1"]⟩, ⟨2, 14, "C1", ["q1", "q2"]⟩, ⟨3, 21, "C0", ["q2", "q3"]⟩],
    unrealised := [],
    gateSites := ["C0", "C1", "C2", "C3"],
    hops := [("C0", "C1"), ("C1", "C0"), ("C1", "C2"), ("C2", "C1"), ("C2", "C3"), ("C3", "C2")],
    loops := [] }

def ghzLate : Input :=
  { ghz with moves := [⟨9, "q0", "C3", "C2"⟩, ⟨9, "q1", "C2", "C1"⟩, ⟨16, "q2", "C1", "C0"⟩] }

/-!
{{figure ghzLate at=8}}

`qcheck` on that file says

```
  REJECTED
    - a gate witness's ions are not together at a gate-capable trap
```

with exit status 1. And `check_cert.py`, given the doctored file with `--qcheck`, does not run
the checker at all: the file is not the one it rebuilds from the certificate, so the Lean verdict
would be about something else.

```
  proved Lean checker        : REFUSED: not this certificate's input
  -> R10 FAILED: the qcheck input was not made from this certificate on this device, so the
     Lean checker's verdict on it is not this certificate's
```

That is Exercise 2's case: `unbound`.

#### Exercise 6 (★★) `ghz_late_rejected`
Prove the doctored input refused, with `rejects_not_colocated`, on the witness of the first CNOT
and the ion that is not there -- the way lesson F1 refused its late move: membership by `simp`,
the position by computation.
-/

theorem ghz_late_rejected : check ghzLate = false :=
  rejects_not_colocated (g := ⟨1, 8, "C2", ["q0", "q1"]⟩) (i := "q0")
    (by simp [ghzLate, ghz]) (by simp) (by decide +kernel)

/-!
## 6. What `qccd toolchain install` asks of a checker

When it installs a prebuilt `qcheck` (on macOS), `qccd toolchain install` runs it twice before
calling it installed (`qccd/workspace/toolchain.py`): on an input it must accept and one it must
reject. The first is `{"circuit": "smoke", "arch": "none", "n_qubits": 0}`: every other field is
missing, and `Input.ofJson` reads a missing field as empty. The second has two qubits on ions
`a` and `b`, both starting in `S0`, and one op, `cx` on both -- and no witness. In Lean:
-/

def smokeOk : Input :=
  { circuit := "smoke", arch := "none", nQubits := 0, qmap := [], init := [], ops := [],
    moves := [], rots := [], gates := [], unrealised := [], gateSites := [], hops := [],
    loops := [] }

def smokeBad : Input :=
  { smokeOk with nQubits := 2, qmap := [(0, "a"), (1, "b")], init := [("a", "S0"), ("b", "S0")],
                 ops := [⟨0, "cx", [0, 1]⟩] }

/-!
#### Exercise 7 (★) `smoke_ok_accepted`
Prove the checker accepts the empty certificate: a circuit of no ops on no qubits holds.
-/

theorem smoke_ok_accepted : check smokeOk = true := by
  decide +kernel

/-!
#### Exercise 8 (★★) `smoke_bad_rejected`
Prove the checker rejects the second, with `rejects_dropped_gate`: the `cx` is one of the ops
(`simp`), it needs a witness, and it has none (both by computation).
-/

theorem smoke_bad_rejected : check smokeBad = false :=
  rejects_dropped_gate (o := ⟨0, "cx", [0, 1]⟩) (by simp [smokeBad]) (by decide +kernel)
    (by decide +kernel)

/-!
## 7. The whole part, in one table

| what the site shows | what you can check | how |
|---|---|---|
| the Compilation page's R10, 19 examples | O1 in the page; all of R10 from the repository | lesson H1's panel; sections 1-3 here |
| a board's R10 mark | the one board certificate in lesson H2; any entry whose circuit and device are in the repository, by compiling it yourself | lesson H2; sections 1-3 here |
| an official entry's eligibility | the grader's code and your own bundle's report; another person's certificate only with their bundle | lesson H3 |

Every check above runs the same proved `check`, and every R10 the same `check_cert.py`. What
that leaves you trusting is what Part F lists: Lean's kernel for what you prove in the page, and
Lean's compiler too for `qcheck`; and the computed half -- `check_cert.py`, the
`mk_qcheck_input.py` and `export_arch.py` that build the checker's input, and the qiskit and stim
they use -- which is computed, not proved.
-/
