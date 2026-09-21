"""S05-T1 (RED): Report query transport — C-FIND SCU (US-05, refinement §2.3).

Queries a PACS by StudyInstanceUID or AccessionNumber, filtering by SOP Class
UID to distinguish DICOM SR (1.2.840.10008.5.1.4.1.1.88.33) from Encapsulated
PDF (1.2.840.10008.5.1.4.1.1.104.2).

Behaviors:
1. C-FIND for SR returns only SR SOP instances
2. C-FIND for PDF returns only PDF SOP instances
3. C-FIND with no match returns empty list
4. Connection error raises ConnectionError
5. SOP class filter works (requesting SR only returns SR, not PDF)
"""

from __future__ import annotations

import socket
import time
from typing import Any

import pytest
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE, evt


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_SR_SOP = "1.2.840.10008.5.1.4.1.1.88.33"
_PDF_SOP = "1.2.840.10008.5.1.4.1.1.104.2"
_IMAGE_FIND_SOP = "1.2.840.10008.5.1.4.1.2.2.1"  # Study Root Q/R FIND


def _find_scp_ae(port: int, *results: Dataset, delay: float = 0.0) -> AE:
    """Create a C-FIND SCP that returns *results* for any IMAGE-level query.

    Instruments the SCP so a test can distinguish a *real* early stop from a
    post-hoc ``[:limit]`` slice — the counters live on the returned AE:

    - ``offered``      — pending results the SCP handed to the wire
    - ``reached_end``  — the SCP reached its terminal 0x0000 Success status
    - ``closed_early`` — the SCU tore the association down mid-generator

    ``delay`` sleeps before each yield, widening the window in which a client
    that stops reading gets its abandonment observed (without it the SCP can
    dump the whole result set into the socket buffer before the SCU breaks).
    """
    ae = AE(ae_title="FINDSCP")
    ae.add_supported_context(_IMAGE_FIND_SOP, ExplicitVRLittleEndian)

    ae.offered = 0  # type: ignore[attr-defined]
    ae.reached_end = False  # type: ignore[attr-defined]
    ae.closed_early = False  # type: ignore[attr-defined]

    def on_c_find(event: evt.Event) -> Any:
        for ds in results:
            ae.offered += 1  # type: ignore[attr-defined]
            if delay:
                time.sleep(delay)
            try:
                yield (0xFF00, ds)
            except GeneratorExit:
                ae.closed_early = True  # type: ignore[attr-defined]
                raise
        ae.reached_end = True  # type: ignore[attr-defined]
        yield (0x0000, None)

    ae.start_server(("", port), evt_handlers=[(evt.EVT_C_FIND, on_c_find)], block=False)
    return ae


def _make_sr_instance(study_uid: str, sop_uid: str | None = None) -> Dataset:
    ds = Dataset()
    ds.QueryRetrieveLevel = "IMAGE"
    ds.StudyInstanceUID = study_uid
    ds.SOPInstanceUID = sop_uid or "1.2.3.4.5.6.7.1"
    ds.SOPClassUID = _SR_SOP
    ds.SeriesInstanceUID = "1.2.3.4.5.6.8"
    ds.NumberOfSeriesRelatedInstances = "1"
    return ds


def _make_pdf_instance(study_uid: str, sop_uid: str | None = None) -> Dataset:
    ds = Dataset()
    ds.QueryRetrieveLevel = "IMAGE"
    ds.StudyInstanceUID = study_uid
    ds.SOPInstanceUID = sop_uid or "1.2.3.4.5.6.7.2"
    ds.SOPClassUID = _PDF_SOP
    ds.SeriesInstanceUID = "1.2.3.4.5.6.9"
    ds.NumberOfSeriesRelatedInstances = "1"
    return ds


def test_find_sr_returns_sr_instances() -> None:
    from mercure_gateway.reports.find import ReportFinder

    sr = _make_sr_instance("1.2.840.1")
    port = free_port()
    scp = _find_scp_ae(port, sr)
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP")
        results = finder.find(study_uid="1.2.840.1", report_types=["sr"])
        assert len(results) == 1
        assert results[0].sop_class_uid == _SR_SOP
        assert results[0].study_uid == "1.2.840.1"
    finally:
        scp.shutdown()


def test_find_pdf_returns_pdf_instances() -> None:
    from mercure_gateway.reports.find import ReportFinder

    pdf = _make_pdf_instance("1.2.840.1")
    port = free_port()
    scp = _find_scp_ae(port, pdf)
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP")
        results = finder.find(study_uid="1.2.840.1", report_types=["pdf"])
        assert len(results) == 1
        assert results[0].sop_class_uid == _PDF_SOP
    finally:
        scp.shutdown()


def test_find_with_no_match_returns_empty() -> None:
    from mercure_gateway.reports.find import ReportFinder

    port = free_port()
    ae = AE(ae_title="FINDSCP")
    ae.add_supported_context(_IMAGE_FIND_SOP, ExplicitVRLittleEndian)

    def on_empty(event: evt.Event) -> Any:
        yield (0x0000, None)  # Success — no matches

    ae.start_server(("", port), evt_handlers=[(evt.EVT_C_FIND, on_empty)], block=False)
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP")
        results = finder.find(study_uid="1.2.3.4", report_types=["sr"])
        assert results == []
    finally:
        ae.shutdown()


def test_find_sop_class_filter_works() -> None:
    """Requesting SR only returns SR instances, even when PDF instances exist."""
    from mercure_gateway.reports.find import ReportFinder

    sr = _make_sr_instance("1.2.840.1", "1.2.3.4.5.6.7.1")
    pdf = _make_pdf_instance("1.2.840.1", "1.2.3.4.5.6.7.2")
    port = free_port()
    scp = _find_scp_ae(port, sr, pdf)
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP")
        sr_only = finder.find(study_uid="1.2.840.1", report_types=["sr"])
        assert len(sr_only) == 1
        assert sr_only[0].sop_class_uid == _SR_SOP

        both = finder.find(study_uid="1.2.840.1", report_types=["sr", "pdf"])
        assert len(both) == 2
    finally:
        scp.shutdown()


def test_connection_error_raises() -> None:
    from mercure_gateway.reports.find import ReportFinder, ReportFinderError

    port = free_port()  # nothing listening on this port
    finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP")
    with pytest.raises(ReportFinderError):
        finder.find(study_uid="1.2.3.4", report_types=["sr"])


# ── Association budget (review P1-12) ───────────────────────────────────


def test_timeouts_are_applied_to_the_ae() -> None:
    """The C-FIND budget is bound explicitly rather than left implicit.

    pynetdicom 3.0.4 has no ``timeout=`` kwarg on ``associate()``; the budget
    is set on the AE instance so a slow PACS can raise it from config.
    """
    from pynetdicom import AE

    from mercure_gateway.reports.find import (
        ReportFinder,
    )

    finder = ReportFinder(host="127.0.0.1", port=11112, aet="PACS", timeout=90.0)
    ae = AE(ae_title="GATEWAY")
    finder._apply_timeouts(ae)

    assert ae.connection_timeout == 90.0
    assert ae.acse_timeout == 90.0
    assert ae.network_timeout == 90.0


def test_timeouts_fall_back_to_the_default() -> None:
    from pynetdicom import AE

    from mercure_gateway.reports.find import (
        _DEFAULT_ASSOCIATE_TIMEOUT_SEC,
        ReportFinder,
    )

    finder = ReportFinder(host="127.0.0.1", port=11112, aet="PACS")
    ae = AE(ae_title="GATEWAY")
    finder._apply_timeouts(ae)

    assert ae.connection_timeout == _DEFAULT_ASSOCIATE_TIMEOUT_SEC
    assert ae.acse_timeout == _DEFAULT_ASSOCIATE_TIMEOUT_SEC
    assert ae.network_timeout == _DEFAULT_ASSOCIATE_TIMEOUT_SEC


# ── C-FIND early stop (review P2-3c) ──────────────────────────────────────
#
# send_c_find returns a lazy iterator; ReportFinder.find() used to drain it
# fully. The sole business consumer (_do_retrieve) keeps retrieved[0], so a
# PACS with N matches was interrogated for all N and then C-MOVE'd all N.
# `limit` abandons the generator instead. These tests have to prove the stop
# actually happened server-side — len() alone cannot tell an early stop from
# a post-hoc [:limit] slice, and that distinction is the whole finding.


def _many_sr_instances(count: int, study_uid: str = "1.2.840.1") -> list[Dataset]:
    """``count`` distinct SR instances, in deterministic ascending UID order."""
    return [_make_sr_instance(study_uid, f"1.2.3.4.5.6.7.{i}") for i in range(count)]


def test_find_limit_returns_at_most_limit_results() -> None:
    """With 50 instances on offer and limit=1, exactly one match comes back."""
    from mercure_gateway.reports.find import ReportFinder

    port = free_port()
    scp = _find_scp_ae(port, *_many_sr_instances(50))
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP", timeout=2.0)
        results = finder.find(study_uid="1.2.840.1", report_types=["sr"], limit=1)
        assert len(results) == 1
    finally:
        scp.shutdown()


def test_find_limit_returns_the_first_match_not_a_later_one() -> None:
    """Ordering matters: a limit that returns the wrong row is worse than none.

    The SCP offers instances in ascending SOPInstanceUID order, so the first
    match must be ``...7.0`` — a post-hoc slice of a shuffled or fully-drained
    set could not be distinguished from this by structure alone.
    """
    from mercure_gateway.reports.find import ReportFinder

    port = free_port()
    scp = _find_scp_ae(port, *_many_sr_instances(50))
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP", timeout=2.0)
        results = finder.find(study_uid="1.2.840.1", report_types=["sr"], limit=1)
        assert results[0].sop_instance_uid == "1.2.3.4.5.6.7.0"
    finally:
        scp.shutdown()


def test_find_limit_stops_the_scp_before_the_success_status() -> None:
    """The limit is an early stop, not a slice: the SCP never reaches Success.

    This is the load-bearing assertion. ``offered`` counts results the SCP
    actually put on the wire inside ``on_c_find``; without a real early stop
    the generator runs to completion and ``reached_end`` is set. With one, the
    SCU abandons the iterator and ``assoc.release()`` closes the SCP's
    generator (``closed_early``) well short of the 50 results it had.
    """
    from mercure_gateway.reports.find import ReportFinder

    port = free_port()
    # delay widens the window so the stop is observable rather than the SCP
    # emptying itself into the socket buffer before the SCU breaks.
    scp = _find_scp_ae(port, *_many_sr_instances(50), delay=0.01)
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP", timeout=2.0)
        start = time.monotonic()
        results = finder.find(study_uid="1.2.840.1", report_types=["sr"], limit=1)
        elapsed = time.monotonic() - start

        assert len(results) == 1
        # The SCP was asked for far fewer results than it had.
        assert scp.offered < 50
        assert scp.reached_end is False
        assert scp.closed_early is True
        # And the association was torn down rather than hanging on the peer.
        assert elapsed < 20.0
    finally:
        scp.shutdown()


def test_find_limit_none_returns_everything() -> None:
    """limit=None (the default) must not change behaviour for existing callers."""
    from mercure_gateway.reports.find import ReportFinder

    port = free_port()
    scp = _find_scp_ae(port, *_many_sr_instances(50))
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP", timeout=2.0)
        results = finder.find(study_uid="1.2.840.1", report_types=["sr"])
        assert len(results) == 50
        assert scp.reached_end is True
    finally:
        scp.shutdown()


def test_find_limit_larger_than_offered_still_terminates_on_success() -> None:
    """The 0x0000 Success break stays authoritative when it precedes the limit.

    A limit above the number of results on offer must not wait for more
    matches: Success arrives, the loop breaks, the association releases.
    """
    from mercure_gateway.reports.find import ReportFinder

    port = free_port()
    scp = _find_scp_ae(port, *_many_sr_instances(5))
    try:
        finder = ReportFinder(host="127.0.0.1", port=port, aet="FINDSCP", timeout=2.0)
        start = time.monotonic()
        results = finder.find(study_uid="1.2.840.1", report_types=["sr"], limit=100)
        elapsed = time.monotonic() - start

        assert len(results) == 5
        assert scp.reached_end is True
        assert elapsed < 10.0
    finally:
        scp.shutdown()


def test_find_limit_rejects_non_positive() -> None:
    """limit=0 would collect one match then break; reject it at the door."""
    from mercure_gateway.reports.find import ReportFinder

    finder = ReportFinder(host="127.0.0.1", port=11112, aet="FINDSCP")
    with pytest.raises(ValueError):
        finder.find(study_uid="1.2.840.1", report_types=["sr"], limit=0)


def test_transport_find_forwards_limit_to_the_finder() -> None:
    """The transport pass-through threads ``limit`` down to the finder.

    The protocol contract and the DICOM implementation must stay symmetric so
    a caller that asks for one match gets one match whichever transport it
    holds.
    """
    from mercure_gateway.reports.transport import DICOMReportTransport

    recorded: list[dict[str, Any]] = []

    class _RecordingFinder:
        def find(self, **kw: Any) -> list[Any]:
            recorded.append(kw)
            return []

    transport = DICOMReportTransport(_RecordingFinder(), retriever=object())
    transport.find(study_uid="1.2.840.1", report_types=["sr"], limit=1)

    assert recorded == [
        {
            "study_uid": "1.2.840.1",
            "accession": None,
            "report_types": ["sr"],
            "limit": 1,
        }
    ]
