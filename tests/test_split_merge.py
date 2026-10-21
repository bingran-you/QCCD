"""#16, #17: split and merge are charged for what a move does to the chains, not for what its
class is called.  Each test is a programme that used to dodge the charge."""

from __future__ import annotations

import json
from pathlib import Path

from qccd.api import Machine
from qccd.arch import Architecture
from qccd.cost import corrected_model
from qccd.ir.tsir import TSIR
from qccd.verify import verify

COMPILED = Path(__file__).resolve().parents[1] / "qccd" / "site" / "compiled"


def _replay(arch, prog):
    rep = verify(prog, arch, corrected_model(), check_metrics=False)
    return rep.result, rep.rules.summary()


def _point(arch, which):
    return arch.primitives.curve(which).pick(corrected_model().policy)


def _chain_sites(m):
    return sorted((n for n, v in m.arch.device.nodes.items() if v.kind == "site"),
                  key=lambda s: m.arch.device.nodes[s].pos)


def test_a_shuttle_out_of_a_chain_splits_and_into_one_merges():
    m = Machine.chain(4)
    s = _chain_sites(m)
    split, merge = _point(m.arch, "split"), _point(m.arch, "merge")
    alone = m.program("alone", provenance="off")
    alone.init({"a": s[0]})
    alone.simd("shuttle", [["a", s[0], s[1]]])
    alone.simd("shuttle", [["a", s[1], s[2]]])
    out = m.program("out", provenance="off")
    out.init({"a": s[0], "b": s[0], "c": s[2]})
    out.simd("shuttle", [["a", s[0], s[1]]])          # leaves b behind: a split
    out.simd("shuttle", [["a", s[1], s[2]]])          # joins c: a merge
    r0, _ = _replay(m.arch, alone.build())
    r1, _ = _replay(m.arch, out.build())
    assert r0.quanta_components.get("split_merge", 0.0) == 0.0
    assert r1.total_us == r0.total_us + split.us + merge.us
    assert r1.quanta_components["split_merge"] == split.quanta + merge.quanta


def test_a_chain_moving_whole_is_neither_split_nor_merged():
    m = Machine.chain(4)
    s = _chain_sites(m)
    p = m.program("whole", provenance="off")
    p.init({"a": s[0], "b": s[0]})
    p.simd("shuttle", [["a", s[0], s[1]], ["b", s[0], s[1]]])
    r, _ = _replay(m.arch, p.build())
    assert r.quanta_components.get("split_merge", 0.0) == 0.0


def test_a_rotation_still_costs_neither():
    m = Machine.ring(3, 2, 2)
    p = m.program("t", provenance="off")
    p.fill()
    p.rotate(2)
    r, s = _replay(m.arch, p.build())
    assert r.quanta_components.get("split_merge", 0.0) == 0.0 and not s.get("failed")


def test_deleting_the_dock_classes_buys_nothing():
    # four hillclimb agents found that a device without its `dock`/`undock` classes docked
    # through the entail-free `shuttle` and dropped the split, the merge and their heat
    doc = json.loads((COMPILED / "ring6d.arch.json").read_text(encoding="utf-8"))
    prog = TSIR.from_json(json.loads((COMPILED / "algo_rot" / "prog.cooled.tsir.json").read_text(encoding="utf-8")))
    honest = Architecture.from_json(doc)
    assert honest.entails("dock") == ("split", "merge")
    for e in doc["control"]["classes"]["extra"]:
        if e.get("id") in ("dock", "undock"):
            e["entails"] = []
    stripped = Architecture.from_json(doc)
    assert stripped.entails("dock") == ()
    r0, _ = _replay(honest, prog)
    r1, _ = _replay(stripped, prog)
    assert r0.quanta_components["split_merge"] > 0
    assert (r1.total_us, r1.quanta_components["split_merge"]) == (r0.total_us, r0.quanta_components["split_merge"])
