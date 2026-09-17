"""S09-T2 (RED): Perf gates (§5.6, K8).

Verifies the gate script measures within budget: forwarding begins within
US-03's 2 s window, and concurrent throughput clears the K8 floor.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _run_gate() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / "check_perf_gates.py")],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_perf_gate_exits_zero() -> None:
    """The perf gate passes (exit 0) within the latency/throughput budgets."""
    result = _run_gate()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_perf_gate_reports_latency() -> None:
    """The gate prints the forwarding latency measurement."""
    result = _run_gate()
    assert "forwarding begins in" in result.stdout


def test_perf_gate_reports_throughput() -> None:
    """The gate prints the concurrent throughput measurement (K8)."""
    result = _run_gate()
    assert "concurrent throughput" in result.stdout
