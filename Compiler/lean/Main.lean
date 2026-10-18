/-
`qcheck` -- the trusted checker, as a program.

`Compiler/PLAN.md` §8 argues for this shape: the checker is written in Lean, proved sound
there, and COMPILED, so there is no extraction step and nothing has to be trusted twice.
The OCaml checker in `Compiler/ocaml/lib/cert.ml` exists only as a fast pre-flight during
compilation; this is the one whose verdict the transport-and-order half of R10 is allowed
to rest on.

It is not the whole of R10.  A witness names an op, a cycle, a trap and ions, never a gate,
so a program whose certificate is ACCEPTED can still apply the wrong one: a two-qubit
`barrier` or a docked `cz` compiled as a CX (compiler releases up to c7c535380bfb) was
accepted here and refused by the other half, `Compiler/bridge/check_cert.py`, which
composes the emitted pulses and compares them with the source circuit.  R10 passes only
when both do.
-/
import QCCDC.Cert.Sound
import Lean.Data.Json

open QCCDC.Cert Lean

def main (args : List String) : IO UInt32 := do
  match args with
  | [path] =>
    let text ← IO.FS.readFile path
    match Json.parse text with
    | .error e =>
      IO.eprintln s!"qcheck: {path}: not JSON: {e}"
      pure 2
    | .ok j =>
      let inp := Input.ofJson j
      IO.println s!"qcheck {inp.circuit} on {inp.arch}"
      IO.println s!"  {inp.ops.length} circuit ops, {inp.gates.length} gate witnesses, \
{inp.moves.length} moves, {inp.rots.length} rotations, {inp.nQubits} qubits"
      if check inp then
        IO.println "  ACCEPTED -- the program follows its certificate: mapping, transport, co-location, order"
        IO.println "  (QCCDC.Cert.check_sound).  It names no gate: which gate fires is decided by comparing"
        IO.println "  the emitted pulses with the circuit (bridge/check_cert.py), and R10 needs both."
        pure 0
      else
        IO.println "  REJECTED"
        for d in diagnose inp do
          IO.println s!"    - {d}"
        pure 1
  | _ =>
    IO.eprintln "usage: qcheck <qcheck-input.json>"
    pure 2
