"""R10: does the compiled program implement the input circuit?

`qccd/verify/__init__.py` lists R10 as UNCHECKABLE with the reason *"needs symbolic
permutation + Pauli-frame tracking against a QASM DAG"*.  This is the check that reason
describes.  It has two halves, and neither of them asks the compiler what it intended.

**O1 -- the certificate's witnesses: transport and co-location.**  Ion positions are
recomputed by replaying the certificate's move and rotation witnesses from its initial
placement.  Every GATE WITNESS -- the certificate's claim that circuit op `dag` ran at
cycle `t` in trap `site` -- must find its ions already together in that trap, and the trap
must be gate-capable; the proved Lean checker (`--qcheck`, `QCCDC.Cert.check_sound`)
decides the same question of the same witnesses -- on an input that must equal the one
rebuilt here from this certificate and this device (`qcheck_binding`), so its verdict is
this certificate's and not some other's.  This half is about the witnesses, not
the emitted program: it never reads the TSIR's gate instructions.  Positions the
certificate also claims are cross-checked, never believed -- the discipline
`qccd/ir/import_deck.py` applies to the shipped artifact, for the same reason.

The witnesses index `circuit_ops`, the certificate's own copy of the (lowered) circuit, so
O1 -- and the Lean verdict -- are only about the source circuit if that list IS the source
circuit.  `check_binding` checks it against the QASM op by op, together with the qubit
count and the qubit->ion map, and a disagreement fails R10.

**O2/O3 -- semantics.**  The compiled program's meaning is recovered *from the emitted
hardware program itself*: the native pulses (`R`, `VZ`, `MS`) are read out of the TSIR,
composed over the logical qubits through the certificate's qubit->ion map, and compared
with the SOURCE circuit as parsed from the QASM (`load_source`) -- never with
`circuit_ops`, which is the compiler's account of what it compiled.  For a Clifford
circuit (decision D1) both sides are stabilizer tableaux.  Outside the Clifford fragment,
for a circuit of at most `--max-unitary-qubits` qubits, both are exact unitaries compared
up to one global phase, and a composite such as `ccx` enters at its defining matrix, so
the compiler's lowering table is checked rather than trusted.  Nothing about the
compiler's reasoning enters.  A swapped operand, a dropped gate, a wrong rotation angle or
a mis-tracked frame all move the tableau, or the unitary.

This subsumes Pauli-frame tracking rather than implementing it separately, which is the
whole argument for D1: a frame error IS a tableau difference.  Both routes compare the
unitary part only: `measure`, `reset` and `barrier` are skipped on both sides.

    python Compiler/bridge/check_cert.py build/out/ghz8_grid9x9 \
        --qasm bench/ghz8.qasm --arch arch/grid9x9.arch.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qccd.arch import Architecture  # noqa: E402
from qccd.ir.tsir import TSIR  # noqa: E402

PI = math.pi
#: the pulses the hardware executes, and so the only gates the semantic half can compose
NATIVE = ("R", "VZ", "MS")
#: what both semantic routes skip: readout, re-preparation and fences are not unitaries
NON_UNITARY = ("measure", "reset", "barrier")

_BRIDGE_MODULES: dict = {}


def _bridge(name: str):
    """A script beside this file, loaded by path: this module is imported by path too
    (`qccd/bench/check.py`, `qccd/workspace/evaluator.py`), when its directory need not be
    on `sys.path`."""
    if name not in _BRIDGE_MODULES:
        spec = importlib.util.spec_from_file_location(f"qccd_check_cert_{name}", HERE / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _BRIDGE_MODULES[name] = mod
    return _BRIDGE_MODULES[name]


def _unitary():
    return _bridge("unitary")


def find_qcheck(lean: Path) -> Path | None:
    """The built Lean checker: `QCCD_QCHECK`, else the standalone `lean/checker` package's
    build, else the full package's; `qcheck.exe` on Windows, `qcheck` elsewhere."""
    if os.environ.get("QCCD_QCHECK"):
        return Path(os.environ["QCCD_QCHECK"])
    for d in (lean / "checker" / ".lake" / "build" / "bin", lean / ".lake" / "build" / "bin"):
        for name in ("qcheck.exe", "qcheck"):
            if (d / name).is_file():
                return d / name
    return None


def qcheck_binding(qcheck_input: Path, cert: dict, arch: Architecture) -> list[str]:
    """Was `qcheck_input` made from THIS certificate on THIS device?  The problems, empty
    if so.

    The Lean checker decides whatever input it is given, and its verdict is reported as
    this certificate's.  So the input is rebuilt here -- `mk_qcheck_input.build` over the
    certificate and `export_arch.expand` of the device this check was given -- and the file
    must equal it.  An input made from another program, or from another device, is refused
    rather than believed.
    """
    want = json.loads(json.dumps(_bridge("mk_qcheck_input").build(cert, _bridge("export_arch").expand(arch))))
    try:
        got = json.loads(Path(qcheck_input).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"the qcheck input does not load: {exc}"]
    if got == want:
        return []
    if not isinstance(got, dict):
        return ["the qcheck input is not a qcheck input"]
    fields = sorted(k for k in set(want) | set(got) if want.get(k) != got.get(k))
    return [f"the qcheck input was not made from this certificate on this device: it differs in "
            f"{', '.join(fields)}"]


# ------------------------------------------------------------------ O1


def replay_positions(cert: dict, arch: Architecture) -> tuple[dict, list[str]]:
    """Recompute where every ion is at every cycle, from init + the movement witnesses.

    Movement is of two kinds. A `move` carries one ion across one segment. A `rotation`
    turns a whole loop: every ion standing ON it advances by `delta` slots, and every ion
    that is not -- a docked one -- stays where it is. That asymmetry is why the rotation
    compiler must never turn the loop while a dock is occupied, and modelling it here is
    what makes the replay able to catch it if it ever did.

    The loop's node order is read from `arch`, never from the certificate: a compiler that
    supplied its own could rotate ions to wherever its gates needed them.
    """
    problems: list[str] = []
    pos = dict(cert["init"])
    loops = {
        lid: list(l.nodes)
        for lid, l in getattr(arch.device, "loops", {}).items()
        if l.closed
    }

    by_cycle: dict[int, list] = {}
    for m in cert["moves"]:
        by_cycle.setdefault(m["t"], []).append(m)
    rots_at: dict[int, list] = {}
    for r in cert.get("rotations", []):
        rots_at.setdefault(r["t"], []).append(r)

    last = max([0] + [m["t"] for m in cert["moves"]] + [g["t"] for g in cert["gates"]]
               + [r["t"] + abs(r["delta"]) for r in cert.get("rotations", [])])
    snapshots: dict[int, dict] = {}
    for t in range(last + 1):
        snapshots[t] = dict(pos)
        for m in by_cycle.get(t, []):
            if pos.get(m["ion"]) != m["from"]:
                problems.append(
                    f"t={t}: {m['ion']} departs {m['from']} but is at {pos.get(m['ion'])}")
            pos[m["ion"]] = m["to"]
        for r in rots_at.get(t, []):
            nodes = loops.get(r["loop"])
            if nodes is None:
                problems.append(
                    f"t={t}: rotation of {r['loop']}, which the architecture does not have")
                continue
            slot = {s: i for i, s in enumerate(nodes)}
            n = len(nodes)
            for ion, site in list(pos.items()):
                if site in slot:
                    pos[ion] = nodes[(slot[site] + r["delta"]) % n]
    return snapshots, problems


def check_o1(cert: dict, arch: Architecture) -> list[str]:
    snapshots, problems = replay_positions(cert, arch)
    for g in cert["gates"]:
        at = snapshots.get(g["t"], {})
        sites = {ion: at.get(ion) for ion in g["ions"]}
        if len(set(sites.values())) != 1 or None in sites.values():
            problems.append(
                f"op {g['dag']}: operands not co-located at t={g['t']}: {sites}")
            continue
        site = next(iter(sites.values()))
        if site != g["site"]:
            problems.append(
                f"op {g['dag']}: witness says {g['site']}, replay says {site}")
        if not arch.can(site, "gate"):
            zt = arch.device.nodes[site].zone_type
            problems.append(
                f"op {g['dag']}: gate at {site}, whose zone {zt!r} cannot gate")
    return problems


# ------------------------------------------------------------------ O2 / O3


def is_right_angle(x: float, eps: float = 1e-9) -> bool:
    return abs(x / (PI / 2) - round(x / (PI / 2))) < eps


def tableau_from_program(prog: TSIR, cert: dict, n: int):
    """Compose the EMITTED pulses into a stabilizer tableau over the logical qubits.

    Reads the hardware program, not the circuit and not the compiler's intent.
    """
    import stim

    qubit_of = {ion: int(q) for q, ion in cert["map"].items()}
    circ = stim.Circuit()
    for instr in prog.instructions:
        if instr.type != "gate":
            continue
        params = list(instr.params)
        if instr.gate == "R":
            for k, ion in enumerate(instr.ions):
                th, ph = params[k] if k < len(params) else (0.0, 0.0)
                if not (is_right_angle(th) and is_right_angle(ph)):
                    raise NotImplementedError(f"non-Clifford pulse R({th},{ph})")
                _emit_r(circ, qubit_of[ion], th, ph)
        elif instr.gate == "VZ":
            for k, ion in enumerate(instr.ions):
                (lam,) = params[k] if k < len(params) else (0.0,)
                if not is_right_angle(lam):
                    raise NotImplementedError(f"non-Clifford frame VZ({lam})")
                # VZ(lam) = diag(1, e^{i lam}) -- S applied `lam / (pi/2)` times
                for _ in range(round(lam / (PI / 2)) % 4):
                    circ.append("S", [qubit_of[ion]])
        elif instr.gate == "MS":
            for k, (a, b) in enumerate(instr.pairs):
                (th,) = params[k] if k < len(params) else (0.0,)
                if not is_right_angle(th):
                    raise NotImplementedError(f"non-Clifford MS({th})")
                _emit_ms(circ, qubit_of[a], qubit_of[b], th)
        else:
            # skipping it would compare a program with that gate taken out
            raise NotImplementedError(f"gate {instr.gate!r} is not a native pulse {NATIVE}")
    circ_padded = stim.Circuit()
    circ_padded.append("I", list(range(n)))
    circ_padded += circ
    return stim.Tableau.from_circuit(circ_padded)


def _emit_r(circ, q: int, theta: float, phi: float) -> None:
    """R(theta, phi) = exp(-i theta/2 (cos phi X + sin phi Y)), for theta a multiple of pi/2."""
    k = round(theta / (PI / 2)) % 4          # quarter turns
    axis = round(phi / (PI / 2)) % 4         # 0 = X, 1 = Y, 2 = -X, 3 = -Y
    if k == 0:
        return
    # rotate about +X or +Y; a negative axis is the same axis with the opposite sense
    if axis in (0, 2):
        base, sense = "X", (1 if axis == 0 else -1)
    else:
        base, sense = "Y", (1 if axis == 1 else -1)
    turns = (k * sense) % 4
    if turns == 0:
        return
    # stim's SQRT_X is exp(-i pi/4 X) up to a global phase, i.e. R(+pi/2, 0) -- NOT its
    # dagger.  Getting this backwards is invisible on a single gate (a tableau ignores
    # global phase) and shows up only as a whole-program mismatch, which is what this
    # checker is for.
    if turns == 2:
        circ.append(base, [q])               # a pi rotation is the Pauli itself
    elif turns == 1:
        circ.append("SQRT_X" if base == "X" else "SQRT_Y", [q])
    else:
        circ.append("SQRT_X_DAG" if base == "X" else "SQRT_Y_DAG", [q])


def _emit_ms(circ, a: int, b: int, theta: float) -> None:
    """MS(theta) = exp(-i theta/2 X⊗X), for theta a multiple of pi/2."""
    k = round(theta / (PI / 2)) % 4
    if k == 0:
        return
    if k == 2:
        circ.append("X", [a])
        circ.append("X", [b])
        return
    circ.append("SQRT_XX" if k == 1 else "SQRT_XX_DAG", [a, b])


def program_unitary(prog: TSIR, cert: dict, n: int):
    """Compose the EMITTED pulses into the exact `2^n x 2^n` unitary over the logical qubits,
    through the same qubit->ion map as `tableau_from_program`."""
    unknown = sorted({str(i.gate) for i in prog.instructions
                      if i.type == "gate" and i.gate not in NATIVE})
    if unknown:
        # `unitary.program_unitary` would skip them, comparing a program without them
        raise NotImplementedError(f"gate(s) {unknown} are not native pulses {NATIVE}")
    qubit_of = {ion: int(q) for q, ion in cert["map"].items()}
    return _unitary().program_unitary(prog.instructions, qubit_of, n)


# ------------------------------------------------------------------ the source circuit


def load_source(qasm: Path):
    """The source circuit, parsed by qiskit: the ONE reading of the QASM that both semantic
    routes and `check_binding` use."""
    from qiskit import qasm2

    return qasm2.load(str(qasm), custom_instructions=qasm2.LEGACY_CUSTOM_INSTRUCTIONS)


def source_ops(qc) -> list[dict]:
    """The source circuit's operations in program order, measure/reset/barrier included.

    Qubits are numbered `qc.find_bit(q).index` -- declaration order, registers in the order
    their `qreg`s appear -- which is the numbering the compiler gives them and the
    certificate's qubit->ion map is written in.  A classically conditioned op
    (`if (c == v) g ...;`) is unwrapped into its body and flagged `cond`: neither semantic
    route can compare it, and the certificate does not record the condition.
    """
    out: list[dict] = []
    for inst in qc.data:
        op = inst.operation
        qs = [qc.find_bit(q).index for q in inst.qubits]
        if op.name == "if_else":
            body = op.blocks[0]
            for b in body.data:
                out.append(_source_op(b.operation, [qs[body.find_bit(q).index] for q in b.qubits],
                                      cond=True))
            continue
        out.append(_source_op(op, qs, cond=False))
    return out


def _source_op(op, qubits: list[int], cond: bool) -> dict:
    params = []
    for p in op.params:
        try:
            params.append(float(p))
        except (TypeError, ValueError):
            params.append(p)
    return {"name": op.name, "qubits": qubits, "params": params, "cond": cond, "op": op}


def source_matrix(o: dict, cache: dict | None = None):
    """The defining unitary of one SOURCE op, in `unitary.py`'s bit order (the op's first
    qubit is the most significant).

    Read from qiskit's own definition of the gate it parsed -- a qelib1 gate, or the body of
    a `gate` the QASM declares -- never from the compiler's lowering: a `ccx` enters as the
    Toffoli, not as the CNOTs the compiler turned it into.  qiskit orders an operator's
    qubits the other way round (its first qubit least significant), hence `reverse_qargs`;
    `tests/test_check_cert_source.py` pins that against `unitary.gate_matrix`.
    """
    import numpy as np
    from qiskit.quantum_info import Operator

    key = (o["name"], tuple(map(str, o["params"])), len(o["qubits"]))
    if cache is not None and key in cache:
        return cache[key]
    try:
        m = np.asarray(Operator(o["op"]).reverse_qargs().data, dtype=complex)
    except Exception as exc:  # noqa: BLE001 -- qiskit has no matrix for it: say so
        raise NotImplementedError(f"no defining unitary for source gate {o['name']}: {exc}") from None
    if cache is not None:
        cache[key] = m
    return m


def source_unitary(ops: list[dict], n: int):
    """The SOURCE circuit's exact unitary: every unitary op of the QASM at its defining
    matrix, composites included; measure/reset/barrier skipped, as on the program side."""
    import numpy as np

    U = _unitary()
    M = np.eye(1 << n, dtype=complex)
    cache: dict = {}
    for o in ops:
        if o["name"] in NON_UNITARY:
            continue
        if o["cond"]:
            raise NotImplementedError(f"classically conditioned source gate {o['name']}")
        M = U.apply_gate(M, source_matrix(o, cache), o["qubits"], n)
    return M


def circuit_tableau(qasm: Path, n: int, ops: list[dict] | None = None):
    """The source circuit's tableau, via qiskit -> stim, for the Clifford fragment."""
    import stim

    if ops is None:
        ops = source_ops(load_source(qasm))
    circ = stim.Circuit()
    circ.append("I", list(range(n)))
    simple = {"x": "X", "y": "Y", "z": "Z", "h": "H", "s": "S", "sdg": "S_DAG",
              "sx": "SQRT_X", "sxdg": "SQRT_X_DAG", "id": "I",
              "cx": "CX", "cy": "CY", "cz": "CZ", "swap": "SWAP"}
    for o in ops:
        name, qs = o["name"], o["qubits"]
        if name in NON_UNITARY:
            continue
        if o["cond"]:
            raise NotImplementedError(f"classically conditioned source gate {name}")
        if name in simple:
            circ.append(simple[name], qs)
        elif name in ("rz", "u1", "p") and is_right_angle(float(o["params"][0])):
            k = round(float(o["params"][0]) / (PI / 2)) % 4
            for _ in range(k):
                circ.append("S", qs)
        else:
            raise NotImplementedError(f"non-Clifford source gate {name}")
    return stim.Tableau.from_circuit(circ)


# ------------------------------------------------------------------ the binding

#: OpenQASM 2's two built-ins, which qiskit reads as the qelib1 gates of the same meaning
_ALIAS = {"U": "u", "CX": "cx"}
#: the longest run of circuit_ops one source gate may be lowered to
MAX_LOWERING = 4096


def check_binding(cert: dict, qc, src: list[dict]) -> list[str]:
    """Does the certificate describe THIS circuit?  The problems, empty if it does.

    The witnesses -- and so O1 and the Lean checker's verdict -- are about `circuit_ops`,
    the certificate's own copy of the circuit, and the semantic half reads the qubit->ion
    map.  Both are checked against the QASM here, never believed:

    * the certificate's `n_qubits` is the QASM's, and its map sends each of those qubits
      to its own ion;
    * `circuit_ops` is numbered 0..N-1, and is the source circuit op by op: an op on one
      or two qubits (and every measure, reset and barrier) appears as itself, on the same
      qubits in the same order, with the same name and angles or the same defining
      unitary; an op on three or more qubits appears as a run of ops on its qubits whose
      product is its defining unitary up to a global phase -- which is how the lowering of
      `ccx` and `cswap` is checked where it is used.

    One small matrix per op at most, so this runs at every size, Clifford or not.
    """
    problems: list[str] = []
    n = cert.get("n_qubits")
    if qc.num_qubits != n:
        problems.append(f"the QASM declares {qc.num_qubits} qubits, the certificate {n}")
    qmap = cert.get("map", {})
    if sorted(qmap) != sorted(str(q) for q in range(qc.num_qubits)):
        problems.append(f"map covers qubits {sorted(qmap, key=lambda k: (len(k), k))[:8]}, "
                        f"not 0..{qc.num_qubits - 1}")
    if len(set(qmap.values())) != len(qmap):
        problems.append("map sends two qubits to one ion")
    cops = cert.get("circuit_ops", [])
    if [o.get("i") for o in cops] != list(range(len(cops))):
        problems.append("circuit_ops is not numbered 0..N-1 in order")
    return problems + _match_ops(src, cops)


def _match_ops(src: list[dict], cops: list[dict]) -> list[str]:
    problems: list[str] = []
    cache: dict = {}
    j = 0
    for k, s in enumerate(src):
        where = f"source op {k} `{_show(s)}`"
        if j >= len(cops):
            problems.append(f"{where}: circuit_ops ends after {len(cops)} ops")
            return problems
        if s["name"] in NON_UNITARY or len(s["qubits"]) <= 2:
            why = _same_op(s, cops[j], cache)
            if why:
                problems.append(f"{where} vs circuit_ops[{j}] `{_show(cops[j])}`: {why}")
                if len(problems) >= 6:
                    return problems
            j += 1
        else:
            end, why = _lowered_run(s, cops, j, cache)
            if why:
                problems.append(f"{where} vs circuit_ops[{j}..]: {why}")
                return problems          # where its lowering ends is unknown: stop here
            j = end
    if j < len(cops):
        problems.append(f"circuit_ops has {len(cops) - j} op(s) beyond the source circuit, "
                        f"from circuit_ops[{j}] `{_show(cops[j])}`")
    return problems


def _show(o: dict) -> str:
    ps = o.get("params") or []
    args = "(" + ",".join(f"{float(p):.6g}" if isinstance(p, (int, float)) else str(p)
                          for p in ps) + ")" if ps else ""
    return f"{o['name']}{args} {list(o['qubits'])}"


def _cert_matrix(c: dict):
    got = _unitary().gate_matrix(_ALIAS.get(c["name"], c["name"]), [float(p) for p in c.get("params", [])])
    if got is None or got[1] != len(c["qubits"]):
        return None
    return got[0]


def _same_op(s: dict, c: dict, cache: dict) -> str | None:
    """Why the certificate's op `c` is not the source op `s`, or None if it is."""
    sname, cname = _ALIAS.get(s["name"], s["name"]), _ALIAS.get(c["name"], c["name"])
    if sname in NON_UNITARY or cname in NON_UNITARY:
        if sname != cname:
            return f"it is a {c['name']}, the source a {s['name']}"
        if sname == "barrier":
            return None if sorted(c["qubits"]) == sorted(s["qubits"]) else "a fence over other qubits"
        return None if list(c["qubits"]) == s["qubits"] else "on other qubits"
    if list(c["qubits"]) != s["qubits"]:
        return "on other qubits, or in another order"
    cp = [float(p) for p in c.get("params", [])]
    if (sname == cname and len(cp) == len(s["params"])
            and all(isinstance(b, float) and abs(a - b) <= 1e-9 for a, b in zip(cp, s["params"]))):
        return None
    mc = _cert_matrix(c)
    if mc is None:
        return "another gate, and one with no defining unitary to compare"
    try:
        ms = source_matrix(s, cache)
    except NotImplementedError as exc:
        return f"another gate name, and {exc}"
    if mc.shape != ms.shape or not _unitary().same_up_to_phase(mc, ms)[0]:
        return "another gate"
    return None


def _lowered_run(s: dict, cops: list[dict], j: int, cache: dict) -> tuple[int, str | None]:
    """The shortest run `cops[j:end]` on the qubits of `s` whose product is `s`'s defining
    unitary up to a global phase: (end, None), or (j, why there is none)."""
    import numpy as np

    U = _unitary()
    try:
        want = source_matrix(s, cache)
    except NotImplementedError as exc:
        return j, f"its lowering cannot be checked: {exc}"
    pos = {q: t for t, q in enumerate(s["qubits"])}
    k = len(s["qubits"])
    M = np.eye(1 << k, dtype=complex)
    for m in range(j, min(len(cops), j + MAX_LOWERING)):
        c = cops[m]
        if c["name"] in NON_UNITARY or any(q not in pos for q in c["qubits"]):
            break
        g = _cert_matrix(c)
        if g is None:
            return j, f"circuit_ops[{m}] `{_show(c)}` has no defining unitary to compose"
        M = U.apply_gate(M, g, [pos[q] for q in c["qubits"]], k)
        if U.same_up_to_phase(M, want)[0]:
            return m + 1, None
    return j, "no run of ops on its qubits composes to it: the lowering is not the gate"


# ------------------------------------------------------------------ driver


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prefix", help="the compiler's -o prefix (.tsir.json / .qcert.json)")
    ap.add_argument("--qasm", required=True)
    ap.add_argument("--arch", required=True)
    ap.add_argument("--qcheck", default=None,
                    help="a qcheck input from mk_qcheck_input.py; runs the PROVED Lean "
                         "checker and lets R10 be reported as `passed`")
    ap.add_argument("--max-unitary-qubits", type=int, default=10,
                    help="fall back to an exact unitary comparison up to this size; "
                         "2^n x 2^n, so 10 is ~16 MB")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cert = json.loads(Path(args.prefix + ".qcert.json").read_text(encoding="utf-8"))
    prog = TSIR.load(args.prefix + ".tsir.json")
    arch_path = ROOT / args.arch if not Path(args.arch).is_absolute() else Path(args.arch)
    arch = Architecture.from_json(json.loads(arch_path.read_text(encoding="utf-8")))

    verdict: dict = {"program": prog.name, "arch": arch.name}

    # the binding: a certificate with no binding is a certificate for anything
    qasm_sha = hashlib.sha256(Path(args.qasm).read_bytes()).hexdigest()
    verdict["circuit_sha256"] = qasm_sha

    # ...and the certificate's own copy of the circuit, which the witnesses index, must be
    # that circuit.  A disagreement is reported under O1 as well: witnesses for another
    # circuit's ops say nothing about this one's, and callers that read only
    # `o1_transport` (the workspace evaluator) must see it.
    qc = load_source(Path(args.qasm))
    src = source_ops(qc)
    binding = check_binding(cert, qc, src)
    verdict["binding"] = "ok" if not binding else binding[:6]

    o1 = check_o1(cert, arch)
    o1_all = [f"certificate vs QASM: {b}" for b in binding] + o1
    verdict["o1_transport"] = "ok" if not o1_all else o1_all[:6]

    # The proved checker.  Until C6 this did not exist and R10 was capped at `partial`
    # by decision D3; now that it does, its verdict is what lifts the cap.
    lean_ok: bool | None = None
    unbound: list[str] = []
    if args.qcheck:
        exe = find_qcheck(ROOT / "Compiler" / "lean")
        unbound = qcheck_binding(Path(args.qcheck), cert, arch)
        if unbound:
            # its verdict would be about some other certificate: not run, not believed
            lean_ok = False
            verdict["lean"] = "REFUSED: not this certificate's input"
            verdict["lean_why"] = unbound
        elif exe is None:
            verdict["lean"] = "qcheck not built"
        else:
            r = subprocess.run([str(exe), args.qcheck], capture_output=True, text=True)
            lean_ok = "ACCEPTED" in r.stdout
            verdict["lean"] = "accepted" if lean_ok else "REJECTED"
            if not lean_ok:
                verdict["lean_why"] = [
                    ln.strip()[2:] for ln in r.stdout.splitlines()
                    if ln.strip().startswith("- ")]

    unrealised = cert.get("unrealised", [])
    verdict["unrealised_ops"] = len(unrealised)

    n = cert["n_qubits"]
    same: bool | None = None
    if qc.num_qubits != n or any(b.startswith("map ") for b in binding):
        # the pulses cannot be read over the circuit's qubits; R10 fails on the binding
        verdict["o2_semantics"] = "not checked: the certificate's qubits are not the QASM's"
        verdict["method"] = "none"
    else:
        try:
            got = tableau_from_program(prog, cert, n)
            want = circuit_tableau(Path(args.qasm), n, src)
            same = got == want
            verdict["o2_semantics"] = "ok" if same else "TABLEAU MISMATCH"
            verdict["method"] = ("stabilizer tableau, composed from the emitted pulses, "
                                 "against the source QASM")
        except NotImplementedError as exc:
            # Outside the Clifford fragment the tableau says nothing.  For a circuit small
            # enough to hold a full operator, compare the unitaries instead -- which is a
            # STRONGER check (it sees global phase and every amplitude), just one that costs
            # 2^n x 2^n and so cannot scale.  The reference is the SOURCE circuit, read from
            # the QASM with its composites at their defining matrices -- never the
            # certificate's `circuit_ops`, which is the compiler's own lowering of it: a
            # comparison against that would pass a `ccx` compiled from `t q[0];`.
            same = None
            verdict["o2_semantics"] = f"not checked: {exc}"
            verdict["method"] = "outside the Clifford fragment (decision D1)"
            if n <= args.max_unitary_qubits:
                try:
                    U = _unitary()
                    a = program_unitary(prog, cert, n)
                    b = source_unitary(src, n)
                    ok_u, alpha, worst = U.same_up_to_phase(a, b)
                    same = ok_u
                    verdict["o2_semantics"] = (
                        f"ok (max entry error {worst:.2e})" if ok_u
                        else f"UNITARY MISMATCH (max entry error {worst:.2e})")
                    verdict["method"] = (
                        f"exact {1 << n}x{1 << n} unitary, built from the emitted pulses, "
                        f"against the source QASM; global phase {alpha:+.4f}")
                except NotImplementedError as exc2:
                    verdict["o2_semantics"] = f"not checked: {exc2}"
                except MemoryError:
                    verdict["o2_semantics"] = f"not checked: {n} qubits is too large"

    structural_ok = (not o1) and not unrealised
    if unbound:
        verdict["R10"] = "FAILED"
        verdict["R10_reason"] = (
            "the qcheck input was not made from this certificate on this device, so the "
            "Lean checker's verdict on it is not this certificate's")
    elif lean_ok is False:
        verdict["R10"] = "FAILED"
        verdict["R10_reason"] = "the proved Lean checker rejected the certificate"
    elif binding:
        verdict["R10"] = "FAILED"
        verdict["R10_reason"] = (
            "the certificate does not describe the source circuit: its circuit_ops, qubit "
            "count or qubit map disagree with the QASM, so its witnesses are not this "
            "circuit's")
    elif not structural_ok:
        verdict["R10"] = "FAILED"
        verdict["R10_reason"] = "transport, co-location or coverage did not check out"
    elif same is False:
        verdict["R10"] = "FAILED"
        verdict["R10_reason"] = "the compiled program does not implement the circuit"
    elif same is True and lean_ok:
        # Both halves, and the O1 half by a checker proved sound in Lean
        # (`QCCDC.Cert.check_sound`).  This is the condition D3 set for `passed`.
        verdict["R10"] = "passed"
        verdict["R10_reason"] = (
            "O1 by the proved Lean checker (QCCDC.Cert.check_sound); O2 by "
            + verdict["method"])
    elif same is True:
        verdict["R10"] = "partial"
        verdict["R10_reason"] = (
            "O1 and O2 both check out, but only the fast OCaml/Python checker ran; "
            "pass --qcheck to have the proved Lean checker decide it")
    else:
        # Outside the Clifford fragment the stabilizer route says nothing, and saying
        # nothing is not the same as failing.  D1 defers these to the symbolic route.
        # Even with the Lean checker accepting, O2 is unestablished, so this stays
        # `partial` -- O1 alone is not R10.
        verdict["R10"] = "partial"
        verdict["R10_reason"] = (
            "O1 checks out; O2 not established -- the circuit is outside the Clifford "
            "fragment, where decision D1 defers to the symbolic route")
    ok = verdict["R10"] in ("partial", "passed")

    if args.json:
        print(json.dumps(verdict, indent=1))
    else:
        print(f"R10 for {prog.name} on {arch.name}")
        print(f"  certificate vs QASM        : {verdict['binding']}")
        print(f"  O1 transport + co-location : {verdict['o1_transport']}")
        print(f"  O2 semantics               : {verdict['o2_semantics']}")
        if "lean" in verdict:
            print(f"  proved Lean checker        : {verdict['lean']}")
        print(f"     ({verdict['method']})")
        if unrealised:
            print(f"  UNREALISED circuit ops     : {len(unrealised)} {unrealised[:8]}")
        print(f"  -> R10 {verdict['R10']}: {verdict['R10_reason']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
