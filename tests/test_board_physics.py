"""A design entered on a board takes the board's physics unless it set its own (`board_device`).

The fixed blocks (primitives, heating, species, budget) are not design variables.  Every new
design starts from the template's, which every board before `qccd-physics@2` shares; the Goal 1
board (surface5xz_mem@1) has physics of its own, so a design with the template's must take the
board's to be an entry, and a design whose physics was edited must stay exploratory.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qccd.workspace.tasks import FIXED_BLOCKS, board_device, find_release, physics_mismatch  # noqa: E402

GRID = json.loads((ROOT / "arch" / "grid9x9.arch.json").read_text(encoding="utf-8"))


def test_a_board_with_the_templates_physics_gets_the_design_unchanged():
    for board in ("surface3_mem@1", "rep5_mem@1", "ghz4@1"):
        rel = find_release(board)
        assert board_device(GRID, rel) == GRID
        assert physics_mismatch(board_device(GRID, rel), rel) == []


def test_a_design_with_the_template_physics_takes_the_goal_1_boards():
    rel = find_release("surface5xz_mem@1")
    assert physics_mismatch(GRID, rel)              # as drawn, it is not an entry
    doc = board_device(GRID, rel)
    assert physics_mismatch(doc, rel) == []          # as entered, it is
    assert doc["geometry"] == GRID["geometry"] and doc["name"] == GRID["name"]
    assert {b: doc[b] for b in FIXED_BLOCKS} == {b: rel.physics()[b] for b in FIXED_BLOCKS}


def test_edited_physics_stays_the_designs_own_and_exploratory():
    rel = find_release("surface5xz_mem@1")
    mine = copy.deepcopy(GRID)
    mine["primitives"]["ms_gate"]["fidelity_at_n0"] = 0.9999
    out = board_device(mine, rel)
    assert out == mine and "primitives" in physics_mismatch(out, rel)


def test_the_goal_1_release_pins_noise_at_two_and_its_table():
    rel = find_release("surface5xz_mem@1")
    m = rel.manifest
    assert m["qec"]["noise"] == "qccd-noise@2" and m["rank_by"] == "ler_per_round"
    assert m["physics"]["rank_table"] == "quantinuum" and m["physics"]["tables"] == ["quantinuum"]
    phys = json.loads((ROOT / "qccd" / "workspace" / "physics" / "qccd-physics@2.json").read_text(encoding="utf-8"))
    assert {b: rel.physics()[b] for b in FIXED_BLOCKS} == {b: phys[b] for b in FIXED_BLOCKS}
    assert m["qec"]["experiment"] == "surface5xz_r5"
