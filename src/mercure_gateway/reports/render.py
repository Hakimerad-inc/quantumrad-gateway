"""Report rendering service (US-05, §2.2 Flow D, refinement §2.3, S05-T6).

Two conversions:
- :meth:`RenderService.render_sr` — walk a DICOM SR content tree and produce
  structured text suitable for the web admin viewer (S06 UI surface).
- :meth:`RenderService.extract_pdf` — pull the raw PDF bytes out of an
  Encapsulated PDF DICOM object.

Malformed or missing content raises :class:`RenderError` instead of crashing
the viewer.
"""

from __future__ import annotations

from typing import Any

__all__ = ["RenderError", "RenderService"]


class RenderError(Exception):
    """Raised when a report object cannot be rendered/extracted."""


class RenderService:
    """Stateless converters from DICOM report objects to displayable data."""

    @staticmethod
    def render_sr(dataset: Any) -> str:
        """Render a DICOM SR dataset as structured text.

        Walks the ContentSequence, emitting each content item's concept name
        and value on its own line.  Nested containers are flattened in order.
        """
        content = getattr(dataset, "ContentSequence", None)
        if content is None:
            raise RenderError("SR dataset has no ContentSequence")
        lines = RenderService._walk(content)
        if not lines:
            return ""
        return "\n".join(lines)

    @staticmethod
    def _walk(content_seq: Any) -> list[str]:
        """Flatten a DICOM SR ContentSequence into displayable lines."""
        lines: list[str] = []
        for item in content_seq:
            concept = RenderService._concept_name(item)
            value_type = str(getattr(item, "ValueType", "TEXT"))
            value = RenderService._value(item, value_type)
            if value:
                lines.append(f"{concept}: {value}" if concept else str(value))
            else:
                lines.append(concept or "")
            nested = getattr(item, "ContentSequence", None)
            if nested:
                lines.extend(RenderService._walk(nested))
        return lines

    @staticmethod
    def _concept_name(item: Any) -> str:
        """Return the concept name text for an SR content item."""
        code = getattr(item, "ConceptNameCodeSequence", None)
        if code:
            first = code[0]
            text = getattr(first, "CodeMeaning", None)
            if text:
                return str(text)
        return ""

    @staticmethod
    def _value(item: Any, value_type: str) -> str:
        """Return the displayable value of an SR content item by ValueType."""
        if value_type == "TEXT":
            return str(getattr(item, "TextValue", "") or "")
        if value_type in ("NUM", "NUMERIC"):
            num = getattr(item, "NumericValue", None)
            unit = getattr(item, "MeasurementUnitsCodeSequence", None)
            unit_text = ""
            if unit and hasattr(unit[0], "CodeMeaning"):
                unit_text = f" {unit[0].CodeMeaning}"
            return f"{num}{unit_text}" if num is not None else ""
        if value_type == "CODE":
            code = getattr(item, "ConceptCodeSequence", None)
            if code and hasattr(code[0], "CodeMeaning"):
                return str(code[0].CodeMeaning)
            return ""
        if value_type in ("DATETIME", "DATE", "TIME"):
            tag = {"DATETIME": "DateTime", "DATE": "Date", "TIME": "Time"}[value_type]
            return str(getattr(item, tag, "") or "")
        if value_type == "PNAME":
            return str(getattr(item, "PersonName", "") or "")
        if value_type == "UIDREF":
            return str(getattr(item, "UID", "") or "")
        return ""

    @staticmethod
    def extract_pdf(dataset: Any) -> bytes:
        """Extract the raw PDF bytes from an Encapsulated PDF DICOM object."""
        doc = getattr(dataset, "EncapsulatedDocument", None)
        if doc is None:
            raise RenderError("PDF dataset has no EncapsulatedDocument")
        return bytes(doc)
