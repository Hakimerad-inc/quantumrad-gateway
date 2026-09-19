#!/usr/bin/env python
"""Emit a CycloneDX SBOM for the frozen Python sidecar (review P1-13).

The release pipeline signs installers and ships ``latest.json``, but the
*dependency* story of the frozen backend was never published with the release
— ``uv.lock`` is committed yet never attached, so an operator (or a scanner)
asking "exactly which versions are in this appliance?" had to diff the repo
at the tag and reconstruct it.

This script resolves the same lock the sidecar was frozen from
(``uv export --frozen --no-dev --all-extras``) and writes a CycloneDX 1.5
document: the root project as an ``application`` component plus one
``library`` component per pinned dependency, each with its sha256 digests from
the lock and a ``purl``.

Deliberately stdlib-only and a fixed-format input: an SBOM generator in the
signing pipeline is the wrong place to take on a new third-party dependency,
and parsing ``uv export``'s requirements-text format is ~40 lines of
straightforward grammar.

Honest scope note (documented in the runbook): this is the *resolved build
input*, not an inventory of the bundle. PyInstaller collects the subset of
these that the backend actually imports — including its build-time deps
(``altgraph``, ``macholib``, …) which never ship. The SBOM says "the release
was built against this pinned set"; a byte-exact contents list is a
PyInstaller ``--report`` follow-up.

Usage:
    uv run python scripts/export_sbom.py --output dist/sbom.json
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

# `uv export --frozen` requirements-text grammar. A requirement begins on an
# unindented line and its continuation lines (hashes, markers, `# via`) are
# indented, so a logical entry is one unindented line plus everything after it
# that starts with whitespace.
_REQ_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)(?:\[(?P<extras>[^\]]*)\])?==(?P<version>[^ ;]+)")
_HASH_RE = re.compile(r"--hash=sha256:(?P<hash>[0-9a-f]{64})")
_MARKER_RE = re.compile(r";\s*(?P<marker>.+)$")


def _uv_binary() -> str:
    """Locate uv — a standalone binary, not an importable module."""
    found = shutil.which("uv")
    if found is None:
        raise SystemExit(
            "uv is not on PATH. The SBOM is derived from the frozen lock; "
            "run this from a checkout with uv installed (CI runs setup-uv)."
        )
    return found


def _export_requirements() -> str:
    """The frozen, resolved dependency set the sidecar is built from."""
    proc = subprocess.run(
        [
            _uv_binary(),
            "export",
            "--frozen",
            "--no-dev",
            "--all-extras",
            "--format",
            "requirements-txt",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def parse_requirements(text: str) -> list[dict[str, Any]]:
    """Turn requirements-text into ``{name, version, hashes, scope}`` records."""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        if not raw[0].isspace():
            # New logical requirement. `-e .` (the root project) has no `==`.
            if current is not None:
                entries.append(current)
            if raw.startswith("-e "):
                current = {
                    "name": _root_project_name(),
                    "version": None,
                    "hashes": [],
                    "scope": None,
                }
                continue
            match = _REQ_RE.match(raw)
            if match is None:
                # Anything we cannot parse is reported rather than silently
                # dropped from a document people audit against.
                current = {
                    "name": raw.split(" ")[0],
                    "version": None,
                    "hashes": [],
                    "scope": f"unparsed: {raw}",
                }
                continue
            # The remainder of the line is ` ; <marker> \` — a trailing
            # backslash continues onto the hash lines, so strip it before the
            # marker is recorded as a property.
            rest = raw[match.end() :].rstrip().rstrip("\\").rstrip()
            marker = _MARKER_RE.search(rest)
            current = {
                "name": match.group("name"),
                "version": match.group("version"),
                "hashes": [],
                "scope": marker.group("marker").strip() if marker else None,
            }
        elif current is not None:
            for h in _HASH_RE.findall(raw):
                current["hashes"].append(h)
    if current is not None:
        entries.append(current)
    return entries


def _purl(name: str, version: str | None) -> str:
    v = f"@{version}" if version else ""
    return f"pkg:pypi/{name.lower().replace('_', '-')}{v}"


def _root_project_name() -> str:
    """The distribution name (``[project] name``), not the checkout's dir."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^name\s*=\s*"([^"]+)"', text, re.M)
    if match is None:
        raise SystemExit("cannot parse `name` from pyproject.toml")
    return match.group(1)


def build_sbom(entries: list[dict[str, Any]], *, source_version: dict[str, str]) -> dict[str, Any]:
    """Assemble a CycloneDX 1.5 document from the parsed requirements."""
    components: list[dict[str, Any]] = []
    for entry in entries:
        is_root = entry["version"] is None and entry["scope"] is None
        component = {
            "type": "application" if is_root else "library",
            "name": entry["name"],
            "purl": _purl(entry["name"], entry["version"]),
        }
        if entry["version"]:
            component["version"] = entry["version"]
        if entry["scope"]:
            component["scope"] = "optional"
            component["properties"] = [{"name": "marker", "value": entry["scope"]}]
        if entry["hashes"]:
            component["hashes"] = [
                {"alg": "SHA-256", "content": h} for h in sorted(set(entry["hashes"]))
            ]
        components.append(component)

    # The `-e .` entry carries no version; the release version is what the
    # document's own metadata records for it.
    for component in components:
        if component["type"] == "application":
            component["version"] = source_version["version"]
            component["purl"] = _purl(component["name"], source_version["version"])

    # The document's identity is deterministic given the tree: a UUID-5 over
    # the component set, so two builds of the same release produce the same
    # serial (a fixed zero UUID is what CycloneDX validators reject).
    serial = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"mercure-gateway-sbom-{source_version['version']}-"
        f"{hashlib.sha256(json.dumps(components, sort_keys=True).encode()).hexdigest()}",
    )
    return {
        "$schema": "http://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": source_version.get("built_at_utc", ""),
            "component": {
                "type": "application",
                "name": "mercure-gateway",
                "version": source_version["version"],
            },
            "properties": [
                {"name": "source", "value": "uv export --frozen --no-dev --all-extras"},
                {"name": "built_from_commit", "value": source_version.get("commit", "")},
            ],
        },
        "components": components,
    }


def _source_version() -> dict[str, str]:
    """Version + provenance of the tree this SBOM describes."""
    init = REPO / "src" / "mercure_gateway" / "__init__.py"
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"), re.M)
    if match is None:
        raise SystemExit(f"cannot parse __version__ from {init}")
    commit = ""
    with contextlib.suppress(FileNotFoundError):
        # git absent — the field stays empty rather than failing the SBOM.
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=False
        ).stdout.strip()
    return {
        "version": match.group(1),
        "commit": commit,
        "built_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="path to write the SBOM JSON")
    args = parser.parse_args(argv)

    text = _export_requirements()
    entries = parse_requirements(text)
    if not entries:
        raise SystemExit("parsed no dependencies from `uv export` — refusing to ship an empty SBOM")
    bom = build_sbom(entries, source_version=_source_version())

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")
    sha = hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(
        f"wrote {args.output}: {len(bom['components'])} components "
        f"(sha256 {sha[:16]}…, version {bom['metadata']['component']['version']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
