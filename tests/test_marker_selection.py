"""Tests for the ``-m`` marker-selection guard in ``tests/conftest.py``.

``--strict-markers`` validates the markers applied *to* a test but not the
names inside a ``-m`` expression. These tests pin the guard that closes that
gap; see ``_reject_undeclared_markers`` in ``conftest.py`` for why.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# A real test file to point collection at. The guard fires at configure time,
# before anything is collected, so this is only consulted by the passing cases.
_A_TEST_FILE = "tests/test_config.py"


@pytest.mark.timeout(90)
def test_typo_in_m_expression_fails_loudly() -> None:
    """A typo'd marker name must abort the run, not select everything.

    This is the regression the guard exists for: ``-m "not sloww and not
    integration"`` is valid Python-with-marker-grammar, matches no test, and a
    naive runner then silently collects the whole suite. Without the guard the
    exit code is 0 and the only symptom is a deselected count nobody reads.
    """
    result = _run_pytest(
        "-m", "not sloww and not integration", "--collect-only", "-q", _A_TEST_FILE
    )

    assert result.returncode != 0, (
        "a typo'd -m selector must not pass; without the guard this run "
        "collects the full suite and exits 0"
    )
    assert "sloww" in (result.stdout + result.stderr), (
        f"the failing run should name the unknown marker; got:\n{result.stdout}{result.stderr}"
    )
    assert "Unknown marker" in (result.stdout + result.stderr), (
        f"the failing run should say what is wrong; got:\n{result.stdout}{result.stderr}"
    )


@pytest.mark.timeout(90)
def test_declared_marker_expression_is_accepted() -> None:
    """The guard must not reject a selector that only names declared markers.

    Distinguishes the guard from a blunt ``-m is forbidden`` rule: the CI
    test-fast job's real selector has to keep working.
    """
    result = _run_pytest("-m", "not slow and not integration", "--collect-only", "-q", _A_TEST_FILE)

    assert result.returncode == 0, (
        f"the shipped selector must still pass; got:\n{result.stdout}{result.stderr}"
    )


@pytest.mark.timeout(90)
def test_builtin_marker_names_are_accepted() -> None:
    """``-m`` selectors naming pytest's own markers are not false positives.

    ``config.getini("markers")`` lists only the project's declared markers, so
    the guard has to let pytest's builtins through or ``-m "not xfail"`` would
    be rejected for a name pytest itself accepts.
    """
    result = _run_pytest("-m", "not xfail and not skip", "--collect-only", "-q", _A_TEST_FILE)

    assert result.returncode == 0, (
        f"builtin marker names must pass; got:\n{result.stdout}{result.stderr}"
    )


def _run_pytest(*args: str) -> subprocess.CompletedProcess[str]:
    """Run this suite's pytest in a real child, with this conftest in scope.

    The guard fires during ``pytest_configure``, so a child is the only honest
    way to exercise it: an in-process re-run would need this conftest
    re-configured under the already-running interpreter.

    ``COVERAGE_PROCESS_START`` is stripped so a ``--cov`` parent does not get
    stray coverage files written into the tree by a collection that never runs.
    """
    import os

    env = {k: v for k, v in os.environ.items() if k != "COVERAGE_PROCESS_START"}
    repo_root = Path(__file__).resolve().parent.parent
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--rootdir", str(repo_root), *args],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
