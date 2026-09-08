"""Report pull C-FIND SCU for the demo chain (S01-T6, PRD §5.2 step 5).

Queries a PACS (Orthanc or the mercure hub) for study-level report metadata
using the Study Root Query/Retrieve Information Model - FIND. In the MVP this
is the "throwaway SCU" used by the demo to prove report retrieval works before
Sprint 05 wires the full ``ReportRetriever.retrieve`` transport.

Usage (CLI)::

    python -m demo.report_pull --host 127.0.0.1 --port 4242 --aet ORTHANC \\
        --study-uid 1.2.840.10008.99.1
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE

__all__ = ["find_study", "main"]

# Study Root Query/Retrieve Information Model - FIND
_STUDY_ROOT_FIND_SOP = "1.2.840.10008.5.1.4.1.2.2.1"

_MAX_PDU_SIZE = 131072


def find_study(
    *,
    host: str,
    port: int,
    aet: str,
    study_uid: str | None = None,
    accession: str | None = None,
    ae_title: str = "GATEWAY-DEMO",
) -> list[Any]:
    """C-FIND the remote SCP for study-level attributes.

    Query keys on StudyInstanceUID (when given) or AccessionNumber; returns
    every matching study as a pydicom ``Dataset`` (empty list for no match).
    """
    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    if study_uid:
        query.StudyInstanceUID = study_uid
    if accession:
        query.AccessionNumber = accession
    query.PatientName = ""
    query.PatientID = ""
    query.Modality = ""
    query.StudyDescription = ""

    ae = AE(ae_title=ae_title)
    ae.maximum_pdu_size = _MAX_PDU_SIZE
    ae.add_requested_context(_STUDY_ROOT_FIND_SOP, ExplicitVRLittleEndian)

    assoc = ae.associate(host, port, ae_title=aet)
    if not assoc.is_established:
        raise ConnectionError("C-FIND association rejected by remote SCP")

    results: list[Any] = []
    try:
        statuses = assoc.send_c_find(query, _STUDY_ROOT_FIND_SOP)
        for status, dataset in statuses:
            if status and status.Status == 0x0000:
                break  # final Success status — no more pending results
            if dataset is not None:
                results.append(dataset)
    finally:
        assoc.release()
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="report-pull",
        description="C-FIND a PACS for study-level report metadata.",
    )
    parser.add_argument("--host", required=True, help="PACS host")
    parser.add_argument("--port", type=int, required=True, help="PACS DICOM port")
    parser.add_argument("--aet", required=True, help="PACS called AE title")
    parser.add_argument("--study-uid", default=None, help="Study Instance UID to find")
    parser.add_argument("--accession", default=None, help="Accession number to find")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the report-pull C-FIND."""
    args = _build_parser().parse_args(argv)
    if not args.study_uid and not args.accession:
        print("provide --study-uid or --accession", file=sys.stderr)
        return 2
    try:
        results = find_study(
            host=args.host,
            port=args.port,
            aet=args.aet,
            study_uid=args.study_uid,
            accession=args.accession,
        )
    except ConnectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"{len(results)} study(ies) matched")
    for ds in results:
        print(
            f"  StudyInstanceUID={getattr(ds, 'StudyInstanceUID', '')} "
            f"Accession={getattr(ds, 'AccessionNumber', '')} "
            f"Modality={getattr(ds, 'Modality', '')} "
            f"Description={getattr(ds, 'StudyDescription', '')}"
        )
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
