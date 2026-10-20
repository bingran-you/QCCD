"""Shor's algorithm, fault-tolerant, on its own floorplan (docs/ARCHITECTURE.md §8).

The order of 7 modulo 15: 157 logical instructions after the standard gates expand, 15
magic states, 29 π/4 gadgets, scheduled in parallel on examples/gadgets/floorplans/shor.floor
with memories that correct what they hold.  The whole run is checked in parts, because no
flat simulation can enumerate its branches:

* the floorplan passes F1-F9 and the schedule G1-G14, with nothing refused;
* no block waits past its coherence budget: the refresh pass adds the rounds that need it;
* the flat sign-off says why it cannot run, rather than pass or fail;
* the compositional sign-off holds -- places, gadgets, gates, composition, semantics -- and
  the semantics are Shor's: m1 = 0 and the phase s/4 uniform;
* the factories produce while the computation runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qccd.gadget.algorithm import parse_algorithm_file
from qccd.gadget.floorplan import FLOORPLANS_DIR, load_file
from qccd.gadget.judge import judge_floorplan, judge_schedule, traffic
from qccd.gadget.library import LeafLibrary
from qccd.gadget.logic.compose import compositional_sign_off
from qccd.gadget.logic.flat import sign_off
from qccd.gadget.place_checks import check_places
from qccd.gadget.refresh import gaps, schedule_with_refresh

ROOT = Path(__file__).resolve().parents[1]
SHOR = ROOT / "examples" / "gadgets" / "algorithms" / "shor15.alg"


@pytest.fixture(scope="module")
def leaves():
    return LeafLibrary(ROOT / "out" / "gadgets" / "cache", log=lambda *a, **k: None)


@pytest.fixture(scope="module")
def machine(leaves):
    alg = parse_algorithm_file(SHOR)
    assert alg.floorplan == "shor"
    lib, fp = load_file(FLOORPLANS_DIR / "shor.floor", leaves)
    alg2, gir = schedule_with_refresh(alg, lib, leaves)
    rules = judge_schedule(lib, leaves, gir, regions=fp.regions)
    return alg, alg2, gir, lib, fp, rules


def test_the_shor_floorplan_passes_every_floorplan_rule(leaves):
    lib, fp = load_file(FLOORPLANS_DIR / "shor.floor", leaves)
    rep = judge_floorplan(lib, leaves, regions=fp.regions)
    assert rep["ok"], rep["failed"]
    fams = [lib[i.master].family for i in lib[lib.top].instances]
    assert fams.count("memory") >= 5 and fams.count("surgery") >= 2 and fams.count("factory") >= 2


def test_shor_schedules_with_nothing_refused_and_every_rule_holding(machine, leaves):
    alg, alg2, gir, lib, fp, rules = machine
    assert not gir["refused"]
    assert not rules["failed"] and not rules["unchecked"], rules["failed"]
    assert {f"G{i}" for i in range(1, 15)} | {f"F{i}" for i in range(1, 10)} <= set(rules["passed"])
    assert not check_places(gir, lib, leaves)["failed"]
    # 157 instructions as written, plus the refresh rounds the pass added
    assert len(alg.instructions) == 157
    added = [i for i in alg2.instructions if i.refresh]
    assert len(alg2.instructions) == 157 + len(added) and added
    assert all(i.kind == "se" and i.not_before is not None for i in added)


def test_no_block_waits_past_its_budget(machine):
    _, _, gir, *_ = machine
    worst = max((z - a for rows in gaps(gir).values() for a, z in rows), default=0.0)
    assert worst <= 1e6


def test_the_flat_sign_off_says_why_it_does_not_apply(machine, leaves):
    alg, alg2, gir, lib, *_ = machine
    so = sign_off(alg2, gir, lib, leaves)
    assert so["passed"] is None and "compositionally" in so["skipped"]


def test_shor_is_signed_off_compositionally_with_shors_outcomes(machine, leaves):
    alg, alg2, gir, lib, fp, rules = machine
    so = sign_off(alg2, gir, lib, leaves)
    cs = compositional_sign_off(alg2, gir, rules, flat=so, outcomes=["m1", "m2", "m3"])
    assert cs["passed"], {k: v["passed"] for k, v in cs["parts"].items()}
    assert {r["gate"] for r in cs["parts"]["gates"]["rows"]} == {"sdg", "h", "t", "tdg", "ccx",
                                                                "cswap"}
    dist = {tuple(r["outcomes"].values()): r["p"] for r in cs["parts"]["semantics"]["distribution"]}
    assert dist == {(0, a, b): 0.25 for a in (0, 1) for b in (0, 1)}


def test_the_factories_work_while_the_computation_runs(machine):
    _, _, gir, lib, *_ = machine
    fam = {p: lib[v[0]].family for p, v in gir["leaves"].items()}
    produce = [e for e in gir["events"] if fam[e[3]] == "factory"]
    surgery = [e for e in gir["events"] if fam[e[3]] == "surgery"]
    assert len(produce) == 15
    assert sum(1 for p in produce if any(s[1] < p[2] and p[1] < s[2] and s[6] != p[6]
                                        for s in surgery)) >= 5
    assert len({e[3] for e in produce}) == 2           # both factories take work
    t = traffic(gir, lib)
    assert t["peak_parallel_ops"] >= 3 and t["ion_sites"] > 0
