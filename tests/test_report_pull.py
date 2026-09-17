"""TDD (S01-T6, RED): Report pull C-FIND SCU for the demo chain.

Behaviors:
1. ``find_study`` issues a C-FIND to a remote SCP and returns matching
   study-level attributes (StudyInstanceUID, AccessionNumber, Modality, etc.).
2. ``find_study`` returns an empty list when no study matches.
3. The C-FIND SCU can be run against an in-process pynetdicom C-FIND SCP.
"""

from __future__ import annotations

import socket
from typing import Any

from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE, evt


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _cfind_sop() -> str:
    """Return the Study Root Query/Retrieve Information Model - FIND SOP class."""
    return "1.2.840.10008.5.1.4.1.2.2.1"


# ── Minimal C-FIND SCP for testing ────────────────────────────────────


def _make_cfind_scp(port: int, result_dataset: Dataset | None = None) -> AE:
    """Create a C-FIND SCP that returns *result_dataset* for any query.

    Caller must start (``start_server``) and stop (``shutdown``) the AE.
    """
    ae = AE(ae_title="CFINDSCP")
    ae.add_supported_context(_cfind_sop(), ExplicitVRLittleEndian)

    if result_dataset is None:
        result_dataset = Dataset()
        result_dataset.StudyInstanceUID = "1.2.840.10008.1.1"
        result_dataset.AccessionNumber = "ACC-001"
        result_dataset.Modality = "CT"
        result_dataset.StudyDescription = "Test study"

    def on_c_find(event: evt.Event) -> Any:
        # Generator: yield (status, dataset) pairs; 0xFF00 = Pending,
        # then a final 0x0000 (Success) terminates the C-FIND.
        yield (0xFF00, result_dataset)
        yield (0x0000, None)

    handlers = [(evt.EVT_C_FIND, on_c_find)]
    ae.start_server(("", port), evt_handlers=handlers, block=False)
    return ae


# ── Test 1: find_study returns matching study attributes ──────────────


def test_find_study_returns_matching_attributes() -> None:
    from demo.report_pull import find_study

    expected = Dataset()
    expected.StudyInstanceUID = "1.2.840.10008.1.1"
    expected.AccessionNumber = "ACC-001"
    expected.Modality = "CT"
    expected.StudyDescription = "Test study"

    port = free_port()
    scp = _make_cfind_scp(port, expected)
    try:
        results = find_study(
            host="127.0.0.1",
            port=port,
            aet="CFINDSCP",
            study_uid="1.2.840.10008.1.1",
        )
        assert len(results) >= 1
        result = results[0]
        assert str(result.StudyInstanceUID) == "1.2.840.10008.1.1"
        assert str(result.AccessionNumber) == "ACC-001"
    finally:
        scp.shutdown()


# ── Test 2: find_study returns empty when no match ────────────────────


def test_find_study_returns_empty_for_no_match() -> None:
    from demo.report_pull import find_study

    port = free_port()

    # SCP that returns no results
    ae = AE(ae_title="CFINDSCP")
    ae.add_supported_context(_cfind_sop(), ExplicitVRLittleEndian)

    def on_c_find_empty(event: evt.Event) -> Any:
        yield (0x0000, None)  # Success — no matches

    ae.start_server(("", port), evt_handlers=[(evt.EVT_C_FIND, on_c_find_empty)], block=False)
    try:
        import time
        time.sleep(0.2)
        results = find_study(
            host="127.0.0.1",
            port=port,
            aet="CFINDSCP",
            accession="NONEXISTENT",
        )
        assert results == []
    finally:
        ae.shutdown()
