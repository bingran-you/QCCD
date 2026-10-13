"""WISE must enable the existing broadcast checker without relaxing Jones rules."""
from dataclasses import replace

from qccd.repro.models import JONES, JONES_WISE, jones_duration
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
