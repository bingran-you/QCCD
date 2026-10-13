"""Memory-X probes use the identical CX layers of the memory-Z sources."""
from pathlib import Path

import pytest
import stim


@pytest.mark.parametrize("distance", [2, 3, 6, 12])
def test_generated_memory_x_has_identical_source_cx_layers(distance):
    root = Path(__file__).resolve().parents[1] / "Reproduce/jones2025/circuits"
    generated = stim.Circuit.generated("surface_code:rotated_memory_x",
                                       distance=distance, rounds=1)
    z = stim.Circuit((root / f"rotated_memory_z_d{distance}_r1.stim").read_text())
    x = stim.Circuit((root / f"rotated_memory_x_d{distance}_r1.stim").read_text())
    assert [ins for ins in generated if ins.name == "CX"] == [ins for ins in z if ins.name == "CX"]
    assert x == generated
