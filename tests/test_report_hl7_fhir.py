"""S08-T5 (smoke): HL7/FHIR experimental transport (PRD §2.3).

A smoke test only — the transport is experimental and feature-flagged off
by default (``ENABLED = False``).  Verifies the class exists, is registered
under ``"fhir"`` and raises ``NotImplementedError`` when used.
"""

from __future__ import annotations

import pytest


def test_hl7_fhir_transport_raises_on_use() -> None:
    """The experimental transport raises NotImplementedError when called."""
    from mercure_gateway.reports.hl7_fhir import HL7FHIRTransport

    transport = HL7FHIRTransport()
    with pytest.raises(NotImplementedError, match="experimental"):
        transport.find(study_uid="1.2.3")
    with pytest.raises(NotImplementedError, match="experimental"):
        transport.retrieve([])


def test_hl7_fhir_is_registered() -> None:
    """The FHIR transport is registered under ``"fhir"``."""
    from mercure_gateway.reports.transport import transport_for_type

    cls = transport_for_type("fhir")
    from mercure_gateway.reports.hl7_fhir import HL7FHIRTransport

    assert cls is HL7FHIRTransport


def test_hl7_fhir_flag_off_by_default() -> None:
    """The ENABLED flag is ``False`` — experimental, off by default."""
    from mercure_gateway.reports.hl7_fhir import ENABLED

    assert ENABLED is False
