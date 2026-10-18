"""R10's semantic half compares the emitted pulses with the SOURCE circuit, on both routes.

Outside the Clifford fragment `Compiler/bridge/check_cert.py` used to build its reference
unitary from the certificate's `circuit_ops` -- the compiler's own lowering of the circuit --
and only hashed the QASM.  So a compiled Toffoli, checked with `--qasm` replaced by
`t q[0];` or by a Toffoli on other operands, reported "R10 passed", and the composite
lowering table was never checked against the source of any non-Clifford circuit (audit,
2026-10-17).  Now the reference is the QASM as qiskit parses it, composites at their
defining matrices, and `circuit_ops` is itself checked against the QASM (`check_binding`),
because the witnesses -- and so O1 and the Lean verdict -- index it.

The planted faults below FAIL on the old checker.  To see that, point the tests at another
copy of it:

    QCCD_CHECK_CERT=/path/to/old/check_cert.py python -m pytest tests/test_check_cert_source.py
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("stim")
pytest.importorskip("qiskit")

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "Compiler" / "bridge"
CHECK_CERT = Path(os.environ.get("QCCD_CHECK_CERT") or BRIDGE / "check_cert.py")

HEADER = 'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[{n}];\ncreg c[{n}];\n'


@contextlib.contextmanager
def _bridge_on_path():
    """The checker imports `unitary` from its directory when run as a script; in-process it
    needs that directory on `sys.path` (the old checker did) -- for the call only."""
    sys.path.insert(0, str(BRIDGE))
    try:
        yield
    finally:
        sys.path.remove(str(BRIDGE))


@pytest.fixture(scope="module")
def cc():
    spec = importlib.util.spec_from_file_location("check_cert_under_test", CHECK_CERT)
    mod = importlib.util.module_from_spec(spec)
    with _bridge_on_path():
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def U():
    spec = importlib.util.spec_from_file_location("unitary_for_test", BRIDGE / "unitary.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _qasm(tmp: Path, body: str, n: int = 3, name: str = "c.qasm") -> Path:
    p = tmp / name
    p.write_text(HEADER.format(n=n) + body + "\n", encoding="utf-8", newline="\n")
    return p


def r10(cc, prefix: Path, qasm: Path, arch: Path, qcheck: Path | None = None) -> tuple[int, dict]:
    """`check_cert.py <prefix> --qasm <qasm> --arch <arch> [--qcheck <input>] --json`, in-process."""
    out = io.StringIO()
    argv = [str(prefix), "--qasm", str(qasm), "--arch", str(arch), "--json"]
    if qcheck is not None:
        argv += ["--qcheck", str(qcheck)]
    with _bridge_on_path(), contextlib.redirect_stdout(out):
        rc = cc.main(argv)
    return rc, json.loads(out.getvalue())


# ------------------------------------------------------------------ compiled examples


def _toolchain():
    from qccd.workspace.evaluator import Toolchain
    return Toolchain.discover().qccdc


def _compile(tmp: Path, body: str, n: int, device) -> dict:
    """Compile `body` on `device` with the built compiler, as qccd/site/compile_examples.py
    does (the Compilation page's examples are these circuits on these devices)."""
    qccdc = _toolchain()
    if qccdc is None:
        pytest.skip("qccdc_cli is not built")
    arch = tmp / "device.arch.json"
    arch.write_text(json.dumps(device.arch.to_json()), encoding="utf-8")
    exp = tmp / "device.expanded.json"
    subprocess.run([sys.executable, str(BRIDGE / "export_arch.py"), str(arch), "-o", str(exp)],
                   check=True, capture_output=True)
    qasm = _qasm(tmp, body, n, "circuit.qasm")
    cp = subprocess.run([str(qccdc), "compile", str(qasm), "--arch", str(exp), "-o", str(tmp / "prog")],
                        capture_output=True, text=True, timeout=600)
    assert cp.returncode == 0, cp.stdout[-800:] + cp.stderr[-800:]
    cert = json.loads((tmp / "prog.qcert.json").read_text(encoding="utf-8"))
    qcheck = tmp / "prog.qcheck.json"
    subprocess.run([sys.executable, str(BRIDGE / "mk_qcheck_input.py"), str(tmp / "prog"), "--arch", str(exp),
                    "-o", str(qcheck)], check=True, capture_output=True)
    return {"dir": tmp, "prefix": tmp / "prog", "qasm": qasm, "arch": arch, "cert": cert, "qcheck": qcheck}


@pytest.fixture(scope="module")
def toffoli(tmp_path_factory):
    from qccd.api import Machine
    return _compile(tmp_path_factory.mktemp("toffoli"), "ccx q[0],q[1],q[2];", 3,
                    Machine.grid(a=2, b=3, name="grid2x3"))


@pytest.fixture(scope="module")
def other_on_grid(tmp_path_factory):
    """Another program on the Toffoli's device: a certificate the Lean checker accepts, for
    a different circuit."""
    from qccd.api import Machine
    return _compile(tmp_path_factory.mktemp("other"), "cx q[0],q[1];\ncx q[1],q[2];", 3,
                    Machine.grid(a=2, b=3, name="grid2x3"))


@pytest.fixture(scope="module")
def bell(tmp_path_factory):
    from qccd.api import Machine
    return _compile(tmp_path_factory.mktemp("bell"), "h q[0];\ncx q[0],q[1];\nmeasure q -> c;", 2,
                    Machine.chain(n=4, name="chain4"))


def test_the_compiled_toffoli_passes_against_its_own_qasm(cc, toffoli):
    assert [o["name"] for o in toffoli["cert"]["circuit_ops"]].count("cx") == 6, "the Toffoli was not lowered"
    rc, v = r10(cc, toffoli["prefix"], toffoli["qasm"], toffoli["arch"])
    assert rc == 0, v
    assert v["R10"] == "partial", v               # no --qcheck here: the fast checkers only
    assert v["o2_semantics"].startswith("ok"), v
    assert "unitary" in v["method"], v
    assert v.get("binding", "ok") == "ok" and v["o1_transport"] == "ok", v


#: QASM bodies the compiled Toffoli does NOT implement
PLANTED = {
    "a T gate": ("t q[0];", 3),
    "the Toffoli with control and target swapped": ("ccx q[2],q[1],q[0];", 3),
    "the Toffoli onto another target": ("ccx q[0],q[2],q[1];", 3),
    "the Toffoli onto another qubit": ("ccx q[0],q[1],q[3];", 4),
}


@pytest.mark.parametrize("fault", list(PLANTED))
def test_the_compiled_toffoli_fails_against_a_planted_fault(cc, toffoli, fault, tmp_path):
    body, n = PLANTED[fault]
    rc, v = r10(cc, toffoli["prefix"], _qasm(tmp_path, body, n), toffoli["arch"])
    assert rc == 1 and v["R10"] == "FAILED", v
    if n == 3:
        # the SEMANTIC half refuses it on its own: the pulses are not this circuit
        assert "UNITARY MISMATCH" in v["o2_semantics"], v
    else:
        assert any("qubits" in b for b in v["binding"]), v
    # ...and so does the binding: circuit_ops is not this circuit either
    assert v["binding"] != "ok" and v["o1_transport"] != "ok", v


def test_a_clifford_example_still_goes_the_stabilizer_route(cc, bell, tmp_path):
    """As before the fix: the right QASM passes, a wrong one is a tableau mismatch."""
    rc, v = r10(cc, bell["prefix"], bell["qasm"], bell["arch"])
    assert rc == 0 and v["R10"] == "partial", v
    assert v["o2_semantics"] == "ok" and v["method"].startswith("stabilizer tableau"), v
    assert v.get("binding", "ok") == "ok", v
    wrong = _qasm(tmp_path, "h q[1];\ncx q[1],q[0];\nmeasure q -> c;", 2)
    rc, v = r10(cc, bell["prefix"], wrong, bell["arch"])
    assert rc == 1 and v["R10"] == "FAILED", v
    assert v["o2_semantics"] == "TABLEAU MISMATCH", v


def _swap_qubits(ops, name):
    k = next(i for i, o in enumerate(ops) if o["name"] == name)
    ops[k]["qubits"] = ops[k]["qubits"][::-1]


def _rename(ops, old, new):
    next(o for o in ops if o["name"] == old)["name"] = new


#: edits to a certificate's circuit_ops; the program and the QASM are left right
LIES = {
    "toffoli: one T of the lowering made a T-dagger": ("toffoli", lambda c: _rename(c["circuit_ops"], "t", "tdg")),
    "toffoli: one CNOT of the lowering reversed": ("toffoli", lambda c: _swap_qubits(c["circuit_ops"], "cx")),
    "toffoli: the lowering's last op dropped": ("toffoli", lambda c: c["circuit_ops"].pop()),
    "toffoli: an op the circuit does not have": ("toffoli", lambda c: c["circuit_ops"].append(
        {"i": len(c["circuit_ops"]), "name": "x", "qubits": [0], "params": [], "line": 5})),
    "bell: the CNOT reversed": ("bell", lambda c: _swap_qubits(c["circuit_ops"], "cx")),
    "bell: the CNOT made a CZ": ("bell", lambda c: _rename(c["circuit_ops"], "cx", "cz")),
    "bell: the Hadamard dropped": ("bell", lambda c: c["circuit_ops"].__setitem__(
        slice(None), [dict(o, i=i) for i, o in enumerate(o for o in c["circuit_ops"] if o["name"] != "h")])),
    "bell: two qubits sent to one ion": ("bell", lambda c: c["map"].__setitem__("1", c["map"]["0"])),
}


@pytest.mark.parametrize("lie", list(LIES))
def test_a_certificate_whose_circuit_ops_is_not_the_qasm_is_refused(cc, toffoli, bell, lie, tmp_path):
    which, edit = LIES[lie]
    ex = {"toffoli": toffoli, "bell": bell}[which]
    cert = copy.deepcopy(ex["cert"])
    edit(cert)
    shutil.copy(str(ex["prefix"]) + ".tsir.json", tmp_path / "p.tsir.json")
    (tmp_path / "p.qcert.json").write_text(json.dumps(cert), encoding="utf-8")
    rc, v = r10(cc, tmp_path / "p", ex["qasm"], ex["arch"])
    assert rc == 1 and v["R10"] == "FAILED", v
    assert v.get("binding", "ok") != "ok", v
    assert v["o1_transport"] != "ok", v           # where the workspace evaluator looks
    assert "does not describe the source circuit" in v["R10_reason"], v


# ------------------------------------------------------------------ the qcheck input is this certificate's


def _qcheck_built(cc):
    return cc.find_qcheck(REPO / "Compiler" / "lean") is not None


def test_the_honest_qcheck_input_lets_the_proved_checker_decide(cc, toffoli):
    if not _qcheck_built(cc):
        pytest.skip("the Lean checker is not built")
    rc, v = r10(cc, toffoli["prefix"], toffoli["qasm"], toffoli["arch"], toffoli["qcheck"])
    assert rc == 0 and v["R10"] == "passed" and v["lean"] == "accepted", v


def test_a_qcheck_input_for_another_program_on_the_same_device_is_refused(cc, toffoli, other_on_grid):
    """The Lean checker accepts the other program's input -- it is a good certificate, for
    another circuit -- so the old checker reported R10 passed for the Toffoli on it."""
    rc, v = r10(cc, toffoli["prefix"], toffoli["qasm"], toffoli["arch"], other_on_grid["qcheck"])
    assert rc == 1 and v["R10"] == "FAILED", v
    assert v["lean"].startswith("REFUSED"), v
    assert "not made from this certificate" in v["R10_reason"], v


def test_a_qcheck_input_with_a_widened_device_is_refused(cc, toffoli, tmp_path):
    """The device facts are what the compiler must not get to assert: one more hop in the
    input is a device the architecture does not describe."""
    doc = json.loads(toffoli["qcheck"].read_text(encoding="utf-8"))
    doc["hops"].append({"from": doc["gate_sites"][0], "to": doc["gate_sites"][-1]})
    wide = tmp_path / "wide.qcheck.json"
    wide.write_text(json.dumps(doc), encoding="utf-8")
    rc, v = r10(cc, toffoli["prefix"], toffoli["qasm"], toffoli["arch"], wide)
    assert rc == 1 and v["R10"] == "FAILED" and v["lean"].startswith("REFUSED"), v
    assert "hops" in " ".join(v["lean_why"]), v


# ------------------------------------------------------------------ the pieces, no compiler


#: qelib1 gates `unitary.gate_matrix` defines, with the qubits and angles they are tried on
TABLE = [("x", 1, []), ("y", 1, []), ("z", 1, []), ("h", 1, []), ("s", 1, []), ("sdg", 1, []),
         ("t", 1, []), ("tdg", 1, []), ("sx", 1, []), ("rx", 1, [0.3]), ("ry", 1, [0.4]),
         ("rz", 1, [0.5]), ("u1", 1, [0.6]), ("u2", 1, [0.1, 0.2]), ("u3", 1, [0.1, 0.2, 0.3]),
         ("cx", 2, []), ("cy", 2, []), ("cz", 2, []), ("ch", 2, []), ("swap", 2, []),
         ("crz", 2, [0.7]), ("cu1", 2, [0.8]), ("rzz", 2, [0.9]), ("ccx", 3, []), ("cswap", 3, [])]


def _src(cc, tmp: Path, body: str, n: int = 3):
    qc = cc.load_source(_qasm(tmp, body, n))
    return qc, cc.source_ops(qc)


@pytest.mark.parametrize("name,k,params", TABLE)
def test_the_source_matrix_is_in_the_bridges_bit_order(cc, U, tmp_path, name, k, params):
    """qiskit orders an operator's qubits least-significant first, `unitary.py` most
    significant first; an asymmetric gate on reversed operands would hide a mix-up."""
    ps = "(" + ",".join(map(str, params)) + ")" if params else ""
    qs = ",".join(f"q[{q}]" for q in range(k))
    _, ops = _src(cc, tmp_path, f"{name}{ps} {qs};", k)
    ok, _alpha, worst = U.same_up_to_phase(cc.source_matrix(ops[0]), U.gate_matrix(name, params)[0])
    assert ok, (name, worst)


#: `Gateset_composites.lower "ccx" [] [a; b; c]`, Compiler/ocaml/lib/gateset_composites.ml
def _ccx_lowering(a, b, c):
    seq = [("h", [c]), ("cx", [b, c]), ("tdg", [c]), ("cx", [a, c]), ("t", [c]), ("cx", [b, c]),
           ("tdg", [c]), ("cx", [a, c]), ("t", [b]), ("t", [c]), ("h", [c]), ("cx", [a, b]),
           ("t", [a]), ("tdg", [b]), ("cx", [a, b])]
    return [{"name": n, "qubits": qs, "params": []} for n, qs in seq]


def _cert(n, ops):
    return {"n_qubits": n, "map": {str(q): f"ion{q}" for q in range(n)},
            "circuit_ops": [dict(o, i=i) for i, o in enumerate(ops)]}


def test_the_binding_accepts_the_compilers_lowerings(cc, tmp_path):
    qc, src = _src(cc, tmp_path, "h q[0];\nccx q[0],q[1],q[2];\ncswap q[2],q[0],q[1];\nmeasure q -> c;")
    a, b, c = 2, 0, 1
    cswap = [{"name": "cx", "qubits": [c, b], "params": []}] + _ccx_lowering(a, b, c) + \
            [{"name": "cx", "qubits": [c, b], "params": []}]
    ops = [{"name": "h", "qubits": [0], "params": []}] + _ccx_lowering(0, 1, 2) + cswap + \
          [{"name": "measure", "qubits": [q], "params": []} for q in range(3)]
    assert cc.check_binding(_cert(3, ops), qc, src) == []


@pytest.mark.parametrize("k", range(15))
def test_the_binding_refuses_a_lowering_with_any_one_op_changed(cc, tmp_path, k):
    qc, src = _src(cc, tmp_path, "ccx q[0],q[1],q[2];")
    ops = _ccx_lowering(0, 1, 2)
    swap = {"h": "s", "cx": "cz", "t": "tdg", "tdg": "t"}
    ops[k] = dict(ops[k], name=swap[ops[k]["name"]])
    assert cc.check_binding(_cert(3, ops), qc, src), f"op {k} changed and the binding still holds"


def test_a_conditioned_gate_is_never_compared_as_if_it_always_ran(cc, tmp_path):
    _, src = _src(cc, tmp_path, "if (c == 1) x q[0];\nt q[1];")
    assert [o["cond"] for o in src] == [True, False]
    with pytest.raises(NotImplementedError):
        cc.source_unitary(src, 3)
    with pytest.raises(NotImplementedError):
        cc.circuit_tableau(None, 3, src)


def test_a_pulse_the_checker_cannot_model_is_never_skipped(cc):
    from qccd.ir.tsir import TSIR
    prog = TSIR.from_json({"name": "p", "arch_spec": "a", "instructions": [
        {"type": "gate", "id": 0, "mode": "intra", "gate": "CX", "arity": 2, "params": [],
         "ions": ["ion0", "ion1"], "sites": ["T0"]}]})
    cert = {"map": {"0": "ion0", "1": "ion1"}}
    with pytest.raises(NotImplementedError):
        cc.tableau_from_program(prog, cert, 2)
    with pytest.raises(NotImplementedError):
        cc.program_unitary(prog, cert, 2)
