"""S05-T6 (RED): Report rendering service (US-05, §2.2, refinement §2.3).

``RenderService`` converts DICOM SR content into structured text suitable for
display in the web admin (S06 UI surface), and extracts the raw PDF from an
Encapsulated PDF DICOM object.  Malformed/missing data raises ``RenderError``
instead of crashing.

Behaviors:
1. A valid DICOM SR dataset produces readable structured text
2. A valid Encapsulated PDF dataset yields the raw PDF bytes
3. An SR dataset with no content raises ``RenderError``
4. A dataset with no EncapsulatedDocument raises ``RenderError``
"""

from __future__ import annotations

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from mercure_gateway.reports.render import RenderError, RenderService

_SR_SOP = "1.2.840.10008.5.1.4.1.1.88.33"
_PDF_SOP = "1.2.840.10008.5.1.4.1.1.104.2"


def _make_sr_dataset(text_items: list[tuple[str, str]]) -> Dataset:
    """Build a minimal DICOM SR with nested CONTAINER → TEXT items."""
    from pydicom.dataset import Dataset as SrItem

    ds = Dataset()
    ds.SOPClassUID = _SR_SOP
    ds.SOPInstanceUID = "1.2.3.4.5.6.7.1"
    ds.StudyInstanceUID = "1.2.840.1"
    ds.SeriesInstanceUID = "1.2.3.4.5.6.100"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = _SR_SOP
    ds.file_meta.MediaStorageSOPInstanceUID = "1.2.3.4.5.6.7.1"
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.PatientName = "TEST^PATIENT"
    ds.PatientID = "P001"
    ds.StudyDescription = "Chest X-ray"
    ds.Modality = "SR"
    ds.ConversionType = "SYN"
    ds.VerificationFlag = "UNVERIFIED"

    items = []
    for concept, text in text_items:
        parts = concept.split(":")
        code_ds = SrItem()
        code_ds.CodeValue = parts[1]
        code_ds.CodingSchemeDesignator = parts[0]
        code_ds.CodeMeaning = parts[2] if len(parts) >= 3 else parts[1]

        item = SrItem()
        item.RelationshipType = "CONTAINS"
        item.ValueType = "TEXT"
        item.ConceptNameCodeSequence = [code_ds]
        item.TextValue = text
        items.append(item)

    root_code = SrItem()
    root_code.CodeValue = "121113"
    root_code.CodingSchemeDesignator = "DCM"
    root_code.CodeMeaning = "Report"

    root = SrItem()
    root.RelationshipType = "CONTAINS"
    root.ValueType = "CONTAINER"
    root.ConceptNameCodeSequence = [root_code]
    root.ContinuityOfContent = "SEPARATE"
    root.ContentSequence = items

    ds.ContentSequence = [root]
    return ds


def _make_pdf_dataset(pdf_bytes: bytes = b"%PDF-1.4 fake pdf content\n%%EOF\n") -> Dataset:
    """Build a minimal Encapsulated PDF DICOM dataset."""
    ds = Dataset()
    ds.SOPClassUID = _PDF_SOP
    ds.SOPInstanceUID = "1.2.3.4.5.6.7.2"
    ds.StudyInstanceUID = "1.2.840.2"
    ds.SeriesInstanceUID = "1.2.3.4.5.6.101"
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = _PDF_SOP
    ds.file_meta.MediaStorageSOPInstanceUID = "1.2.3.4.5.6.7.2"
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.PatientName = "TEST^PATIENT"
    ds.PatientID = "P001"
    ds.StudyDescription = "Chest X-ray Report"
    ds.Modality = "DOC"
    ds.DocumentTitle = "Radiology Report"
    ds.EncapsulatedDocument = pdf_bytes
    ds.MIMETypeOfEncapsulatedDocument = "application/pdf"
    return ds


def test_render_sr_returns_structured_text() -> None:
    """A valid SR dataset produces readable text."""
    ds = _make_sr_dataset([
        ("DCM:121113:Report", "Findings: Normal chest X-ray"),
        ("DCM:121071:Finding", "No acute cardiopulmonary abnormality"),
    ])
    result = RenderService.render_sr(ds)
    assert "Findings" in result
    assert "Normal chest X-ray" in result
    assert "No acute cardiopulmonary abnormality" in result


def test_render_sr_handles_empty_content() -> None:
    """SR with no content items returns empty-ish text."""
    ds = _make_sr_dataset([])
    result = RenderService.render_sr(ds)
    assert isinstance(result, str)


def test_render_sr_missing_content_raises() -> None:
    """SR dataset without ContentSequence raises RenderError."""
    ds = _make_sr_dataset([])
    del ds.ContentSequence
    with pytest.raises(RenderError):
        RenderService.render_sr(ds)


def test_extract_pdf_returns_bytes() -> None:
    """Extract PDF from an Encapsulated PDF dataset."""
    expected = b"%PDF-1.4 real content\n%%EOF\n"
    ds = _make_pdf_dataset(expected)
    result = RenderService.extract_pdf(ds)
    assert result == expected


def test_extract_pdf_missing_encapsulated_document_raises() -> None:
    """Dataset without EncapsulatedDocument raises RenderError."""
    ds = _make_pdf_dataset()
    del ds.EncapsulatedDocument
    with pytest.raises(RenderError):
        RenderService.extract_pdf(ds)


def test_extract_pdf_invalid_bytes_is_still_returned() -> None:
    """The service returns whatever bytes are in the tag — interpretation is
    the caller's responsibility."""
    ds = _make_pdf_dataset(b"not-a-real-pdf")
    result = RenderService.extract_pdf(ds)
    assert result == b"not-a-real-pdf"