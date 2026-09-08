"""``*.tags`` sidecar extraction — mercure ``getdcmtags`` convention.

For every received DICOM instance the gateway writes a flat JSON sidecar
(``<instance_uid>.tags`` next to the ``.dcm`` file) carrying the tags used
for routing rules (S07-T6), display, and report correlation (PRD §5.2-2,
§8.3). Values are stringified; binary elements (e.g. PixelData) are never
included.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

__all__ = ["TAG_KEYWORDS", "extract_tags", "write_tags_file"]

logger = logging.getLogger(__name__)

# The tag set written to every sidecar (mercure getdcmtags baseline plus the
# routing/display tags the gateway itself uses).
TAG_KEYWORDS: tuple[str, ...] = (
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "SOPClassUID",
    "Modality",
    "AccessionNumber",
    "PatientID",
    "PatientName",
    "PatientBirthDate",
    "PatientSex",
    "StudyDate",
    "StudyTime",
    "StudyDescription",
    "SeriesDescription",
    "StationName",
    "InstitutionName",
)


def extract_tags(dataset: Any) -> dict[str, str]:
    """Extract the flat ``{keyword: string value}`` tag summary.

    Optional keywords missing from *dataset* are omitted; values are
    stringified and trimmed of DICOM padding.
    """
    tags: dict[str, str] = {}
    for keyword in TAG_KEYWORDS:
        value = getattr(dataset, keyword, None)
        if value is None:
            continue
        text = str(value).strip().strip("\x00").strip()
        if text:
            tags[keyword] = text
    return tags


def write_tags_file(dataset: Any, path: Path) -> Path:
    """Write the sidecar JSON for *dataset* to *path* (``*.tags``)."""
    tags = extract_tags(dataset)
    sidecar = path.with_suffix(".tags")
    sidecar.write_text(json.dumps(tags, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return sidecar
