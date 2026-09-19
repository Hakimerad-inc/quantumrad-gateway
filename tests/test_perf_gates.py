"""S09-T2 (RED): Perf gates (§5.6, K8).

Verifies the gate script measures within budget: forwarding begins within
US-03's 2 s window, and concurrent throughput clears the K8 floor.

The synthetic gate (default) runs on every push. The real-handler gate
(P1-7) is ``slow`` + ``integration`` — CI deselects ``integration`` and it
runs on the nightly / integration path instead.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _run_gate(real: bool = False) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(_SCRIPTS / "check_perf_gates.py")]
    if real:
        args.append("--real")
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        # The real mode opens ~100 real associations; it takes ~15 s on a
        # quiet box, and the synthetic mode is sub-second — one budget
        # covers both without being generous about a hang.
        timeout=180,
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


# ══════════════════════════════════════════════════════════════════════
# Real-handler mode — the throughput gate stops measuring a fake (P1-7)
#
# The default gate's handler increments a counter and returns ok=True with
# no I/O, so its "throughput" measures a while loop rather than the
# forwarder. --real delivers the same batch through an actual socket to a
# local C-STORE SCP, reading real DICOM files off a temp spool. On a quiet
# box the two numbers differ by ~300×, and only the real one means
# anything. Marked integration so CI's per-push matrix skips it (the
# socket-bound measurement flaps on a loaded shared runner) and slow
# because the 100-association batch takes ~15 s.
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.slow
@pytest.mark.integration
def test_real_perf_gate_exits_zero() -> None:
    """The real-handler gate also passes within the same budgets."""
    result = _run_gate(real=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


@pytest.mark.slow
@pytest.mark.integration
def test_real_perf_gate_measures_a_socket_not_a_counter() -> None:
    """Real mode reports itself distinctly, so a CI default cannot silently
    regress to the fake number and still look like the real measurement."""
    result = _run_gate(real=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "real concurrent throughput" in result.stdout
    assert "real forwarding begins in" in result.stdout


@pytest.mark.integration
def test_real_mode_is_marked_integration() -> None:
    """The integration marker exists so CI can deselect this path.

    The marker was declared but unused (zero tests carried it); wiring it
    here is what actually puts --real off the per-push matrix.
    """
    markers = {m.name for m in test_real_perf_gate_exits_zero.pytestmark}
    assert "integration" in markers
