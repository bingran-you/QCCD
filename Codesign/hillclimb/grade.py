"""Grade device files on a leaderboard with the reference evaluator, as `qccd submit --local` does.

Each device is imported into a throwaway workspace, the board's circuit is compiled for it by the
reference compiler (qccdc, compiler "auto"), and the result is frozen and graded under the reference
profile. Run it from the root of a checkout of yezhuoyang/QCCD's `compiler` branch at 972d8bf with
this folder copied in, with compiler dbd13d8952f3 and a qcheck built from that checkout (README.md
has the steps):

    python Codesign/hillclimb/grade.py rep9@1 Codesign/hillclimb/rep9/designs/*.arch.json

Prints one line per device: the board's ranked metric, eligibility, DACs, traps and junctions.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qccd.arch.device import Architecture  # noqa: E402
from qccd.cost.hardware import hardware_report  # noqa: E402
from qccd.workspace.app import Workspace  # noqa: E402
from qccd.workspace.design import base_from_arch  # noqa: E402

USER = {"kind": "human", "id": "cli"}
DONE = ("succeeded", "failed", "cancelled", "timeout", "internal_error")


def wait(ws: Workspace, job_id: str, timeout: float = 3600.0) -> dict:
    t0 = time.time()
    while (job := ws.job(job_id))["status"] not in DONE:
        if time.time() - t0 > timeout:
            raise SystemExit(f"timed out waiting for {job_id}")
        time.sleep(0.3)
    return job


def grade(board: str, path: Path) -> str:
    arch = Architecture.from_json(json.loads(path.read_text()))
    base = base_from_arch(arch)
    with tempfile.TemporaryDirectory() as tmp:
        ws = Workspace.init(Path(tmp) / "ws", board)
        try:
            sync = {"type": "studio_sync", "base": {"geom": base.geom, "seed": base.seed, "post": base.post}}
            change = ws.apply_change_set({"expected_revision": ws.head().revision, "request_id": "import",
                                          "mode": "apply", "summary": "import", "operations": [sync]}, USER)
            if change.get("status") != "committed":
                return f"import refused: {change}"
            job = wait(ws, ws.submit_design(USER, board=board, profile="reference")["job_id"])
            result = job.get("result") or {}
            if job["status"] != "succeeded":
                return f"submit {job['status']}: {str(result.get('log') or job.get('error'))[-600:]}"
            graded = wait(ws, result["grading_job"])
            if graded["status"] != "succeeded":
                return f"grading {graded['status']}: {graded.get('error')}"
            report = ws.submission(result["submission_id"])["report"] or {}
        finally:
            ws.close()
    metric = report.get("rank_by")
    value = ((report.get("metrics") or {}).get(metric) or {}).get("value")
    eligibility = report.get("eligibility") or {}
    hw = hardware_report(arch)
    verdict = "eligible" if eligibility.get("eligible") else f"NOT eligible {eligibility.get('reasons')}"
    return f"{metric} {value}  {verdict}  dacs {hw.dacs} traps {hw.n_traps} junctions {hw.n_junctions}"


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    for device in sys.argv[2:]:
        print(f"{device}: {grade(sys.argv[1], Path(device))}", flush=True)
