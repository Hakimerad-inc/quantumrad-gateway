"""DICOMweb QIDO/WADO report transport (PRD §2.3, Q2, S08-T4).

Retrieves SR/PDF reports from a DICOMweb server over HTTPS:

- **QIDO-RS** (``/studies`` query) locates report instances, filtered by
  StudyInstanceUID or AccessionNumber and by report SOP class (SR / PDF).
  Results are followed across pages with ``offset`` pagination.
- **WADO-RS** (``/studies/{study}/series/{series}/instances/{instance}``)
  fetches each instance's DICOM bytes, which are parsed and saved under
  ``reports/{study_uid}/{sr|pdf}/{sop_uid}.dcm``.

HTTP failures raise :class:`DICOMwebError` — a silent data loss loop is
unacceptable for a clinical gateway (mirrors the S05 C-FIND/C-MOVE behavior).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests

from mercure_gateway.config import ReportQuerySource
from mercure_gateway.reports.find import PDF_SOP_CLASS, SR_SOP_CLASS, ReportMatch
from mercure_gateway.reports.move import RetrievedReport
from mercure_gateway.spool import validate_uid

__all__ = ["DICOMwebError", "DICOMwebReportTransport", "build_dicomweb_transport"]

logger = logging.getLogger(__name__)

# QIDO-RS include fields (report identity + SOP class).
_QIDO_INCLUDE = [
    "SOPClassUID",
    "SOPInstanceUID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
]

# report_type ("sr"/"pdf") → SOP Class UID (mirrors reports.find).
_REPORT_SOP_CLASSES = {
    "sr": SR_SOP_CLASS,
    "pdf": PDF_SOP_CLASS,
}


class DICOMwebError(Exception):
    """Raised when the DICOMweb server returns an error or cannot be reached."""


class DICOMwebReportTransport:
    """Report transport backed by a DICOMweb QIDO-RS/WADO-RS endpoint.

    Satisfies the :class:`~mercure_gateway.reports.transport.ReportTransport`
    protocol: ``find`` locates report instances, ``retrieve`` pulls them to
    disk.
    """

    def __init__(
        self,
        *,
        base_url: str,
        reports_dir: Path,
        verify_tls: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._reports_dir = Path(reports_dir)
        self._verify_tls = verify_tls

    def find(
        self,
        *,
        study_uid: str | None = None,
        accession: str | None = None,
        report_types: list[str] | None = None,
    ) -> list[ReportMatch]:
        """QIDO-RS query for report instances; follows pagination.

        Returns :class:`ReportMatch` records; empty list when the server has
        no matching report instances.  Raises :class:`DICOMwebError` on an
        HTTP failure.
        """
        if not study_uid and not accession:
            raise ValueError("provide study_uid or accession")
        report_types = report_types or list(_REPORT_SOP_CLASSES)
        wanted_sops = {
            _REPORT_SOP_CLASSES[t] for t in report_types if t in _REPORT_SOP_CLASSES
        }

        include = ",".join(_QIDO_INCLUDE)
        params = [f"includefield={include}"]
        if study_uid:
            params.append(f"StudyInstanceUID={study_uid}")
        if accession:
            params.append(f"AccessionNumber={accession}")

        matches: list[ReportMatch] = []
        next_url: str | None = f"{self._base_url}/studies?" + "&".join(params)
        while next_url:
            url = next_url
            resp = self._request("GET", url)
            if not resp.ok:
                raise DICOMwebError(f"QIDO-RS failed (HTTP {resp.status_code})")
            rows = resp.json()
            if not rows:
                break
            for row in rows:
                sop_class = _json_ui(row.get("00080016"))
                if wanted_sops and sop_class not in wanted_sops:
                    continue
                matches.append(
                    ReportMatch(
                        sop_class_uid=sop_class,
                        study_uid=_json_ui(row.get("0020000D")) or (study_uid or ""),
                        series_uid=_json_ui(row.get("0020000E")) or "",
                        sop_instance_uid=_json_ui(row.get("00080018")) or "",
                    )
                )
            next_url = _parse_link_next(resp)

        return matches

    def retrieve(self, matches: list[ReportMatch]) -> list[RetrievedReport]:
        """WADO-RS fetch each match and save it to disk.

        Returns :class:`RetrievedReport` records for the instances fetched.
        Raises :class:`DICOMwebError` when a fetch fails (no silent drop).
        """
        if not matches:
            return []
        self._reports_dir.mkdir(parents=True, exist_ok=True)

        saved: list[RetrievedReport] = []
        for match in matches:
            url = (
                f"{self._base_url}/studies/{match.study_uid}"
                f"/series/{match.series_uid}"
                f"/instances/{match.sop_instance_uid}"
            )
            resp = self._request("GET", url)
            if not resp.ok:
                raise DICOMwebError(
                    f"WADO-RS failed for {match.sop_instance_uid} (HTTP {resp.status_code})"
                )
            ds = self._parse_bytes(resp.content, match)
            path = self._save(ds, match)
            saved.append(
                RetrievedReport(
                    sop_class_uid=match.sop_class_uid,
                    study_uid=match.study_uid,
                    sop_instance_uid=match.sop_instance_uid,
                    file_path=path,
                )
            )
        return saved

    def _request(self, method: str, url: str) -> requests.Response:
        """Issues an HTTPS GET with TLS verification as configured."""
        if not url.startswith("https://") and not url.startswith("http://"):
            url = f"https://{url}"
        return requests.get(url, timeout=30, verify=self._verify_tls)

    @staticmethod
    def _parse_bytes(content: bytes, match: ReportMatch) -> Any:
        """Parse raw DICOM bytes into a pydicom dataset."""
        import io

        from pydicom import dcmread

        try:
            return dcmread(io.BytesIO(content), force=True)
        except Exception as exc:
            raise DICOMwebError(
                f"WADO-RS returned non-DICOM payload for {match.sop_instance_uid}: {exc}"
            ) from exc

    def _save(self, ds: Any, match: ReportMatch) -> Path:
        """Persist *ds* under ``reports/{study_uid}/{sr|pdf}/{sop_uid}.dcm``."""
        # P0-1: the UIDs arrive from the DICOMweb server's JSON and compose the
        # output path, so a malicious or malformed server can write outside
        # reports_dir. Reject anything that is not a bare DICOM UID before it
        # reaches the filesystem (same guard the C-STORE receiver uses).
        study_uid = validate_uid(match.study_uid, what="StudyInstanceUID")
        sop_instance_uid = validate_uid(match.sop_instance_uid, what="SOPInstanceUID")
        sub = "sr" if match.sop_class_uid == SR_SOP_CLASS else "pdf"
        out_dir = self._reports_dir / study_uid / sub
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{sop_instance_uid}.dcm"
        ds.save_as(str(path), enforce_file_format=True)
        return path


def _parse_link_next(resp: requests.Response) -> str | None:
    """Return the ``rel="next"`` page URL from the Link header (or None)."""
    link = resp.headers.get("Link")
    if not link:
        return None
    for part in link.split(","):
        section = part.split(";")
        if len(section) >= 2 and 'rel="next"' in section[1]:
            url = section[0].strip().strip("<>")
            return url or None
    return None


def _json_ui(value: Any) -> str:
    """Extract a UI value from a DICOMweb JSON attribute row."""
    if isinstance(value, dict):
        values = value.get("Value")
        if values:
            return str(values[0])
    return ""


def build_dicomweb_transport(
    source: ReportQuerySource, *, reports_dir: Path, verify_tls: bool = True
) -> DICOMwebReportTransport:
    """Build a DICOMweb transport from a ``dicomweb`` query source."""
    scheme = "https" if verify_tls else "http"
    base_url = f"{scheme}://{source.host}:{source.port}/dicomweb"
    return DICOMwebReportTransport(
        base_url=base_url,
        reports_dir=reports_dir,
        verify_tls=verify_tls,
    )
