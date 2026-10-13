"""WISE must enable the existing broadcast checker without relaxing Jones rules."""
from dataclasses import replace
import gzip
import json
from pathlib import Path

import pytest

from qccd.repro.importers import jones_device
from qccd.repro.models import JONES, JONES_WISE, jones_duration
from qccd.repro.sched_jones import stim_program
from qccd.repro.sched_wise import build
from qccd.repro.timed import Event, PortGraph, TimedSchedule, check


def test_wise_profile_and_broadcast_faults():
    assert replace(JONES_WISE, name=JONES.name, broadcast=()) == JONES
    dev = PortGraph(
        sites={f"T{k}": {"capacity": 2, "ports": {f"C{k}": "R"}} for k in range(2)},
        nodes={f"J{k}": {} for k in range(2)},
        segments={f"C{k}": (f"T{k}", f"J{k}") for k in range(2)},
    )
    events = [Event(k, "split", 0, 80, (str(k),), at=f"T{k}", seg=f"C{k}",
                    meta={"class": "split:R", "locks":
                          [f"ManipulationTrap:{k}", f"Junction:{k}", f"Crossing:{k}"]})
              for k in range(2)]
    returns = [replace(e, id=e.id + 2, kind="merge", t0=100, t1=180,
                       meta={**e.meta, "class": "merge:R"}) for e in events]
    sched = TimedSchedule("broadcast probe", dev, {"T0": ["0"], "T1": ["1"]}, events + returns)
    report = check(sched, JONES_WISE, duration=jones_duration, strict=True)
    assert report.ok, report.summary()
    assert report.checks["broadcast"] == "passed"
    for bad in (
        replace(events[1], meta={**events[1].meta, "class": "split:L"}),
        replace(events[1], t1=81),
        replace(events[1], t0=1, t1=81),
        replace(events[1], meta={"locks": events[1].meta["locks"]}),
    ):
        broken = replace(sched, events=[events[0], bad] + returns)
        assert check(broken, JONES_WISE, duration=jones_duration, strict=True).failed("broadcast")


def _source():
    root = Path(__file__).resolve().parents[1] / "Reproduce/jones2025"
    with gzip.open(root / "artifact/grid_rot_d3_c2.schedule.json.gz", "rt") as f:
        raw = json.load(f)
    chains = {f"T{t}": [str(i) for i in ch] for t, ch in raw["initial_layout"].items()}
    return jones_device(raw["device"]), chains, raw["qubit_to_ion"], root / "circuits/rotated_memory_z_d3_r1.stim"


def test_wise_first_step_and_planted_faults():
    dev, chains, qubits, stim = _source()
    sched = build(dev, chains, qubits, stim, steps=1)
    cx, _, _ = stim_program(stim)
    report = check(sched, JONES_WISE, duration=jones_duration, circuit=cx[:6], strict=True)
    assert report.ok, report.summary()
    assert report.checks["broadcast"] == "passed"
    assert report.metrics["makespan_us"] == 660
    assert sched.claims["broadcast_cycles"] == 12
    assert sched.claims["transport_us_total"] == 540
    event = next(e for e in sched.events if e.kind == "j_exit")
    bad = replace(event, meta={**event.meta, "class": "j_exit:W"})
    broken = replace(sched, events=[bad if e.id == event.id else e for e in sched.events])
    assert check(broken, JONES_WISE, duration=jones_duration, circuit=cx[:6], strict=True).failed("broadcast")
    dropped = replace(sched, events=[e for e in sched.events if e.kind != "gate"])
    assert check(dropped, JONES_WISE, duration=jones_duration, circuit=cx[:6], strict=True).failed("circuit")
    with pytest.raises(ValueError, match="occupied zones would sit out"):
        build(dev, chains, qubits, stim, steps=1, zone_opt_out=False)
