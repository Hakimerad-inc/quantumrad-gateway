"""S07-T6 (RED): Forwarding rules engine (PRD §5.5, US-09).

Evaluates routing rules against extracted DICOM tags (the ``*.tags`` files
from S02-T4) to decide which targets receive a study.  Rules use a simple
``tag=value`` expression syntax with priority ordering and multi-target
expansion.

Behaviors:
1. A matching rule routes to its named targets only
2. No match → default route (all targets)
3. Priority ordering: high wins over normal over low
4. Multi-target expansion per rule
5. Invalid rule syntax is rejected
"""

from __future__ import annotations

import pytest

from mercure_gateway.rules import RuleEngine, RuleSyntaxError


def _engine(rules: list[dict]) -> RuleEngine:
    from mercure_gateway.config import ForwardingRule

    return RuleEngine([ForwardingRule(**r) for r in rules])


# ══════════════════════════════════════════════════════════════════════
# Basic matching
# ══════════════════════════════════════════════════════════════════════

def test_matching_rule_routes_to_named_targets() -> None:
    engine = _engine([{"rule": "Modality=CT", "targets": ["pacs"]}])
    result = engine.resolve({"Modality": "CT"}, all_targets=["pacs", "hub"])
    assert result == ["pacs"]


def test_no_match_defaults_to_all() -> None:
    engine = _engine([{"rule": "Modality=CT", "targets": ["pacs"]}])
    result = engine.resolve({"Modality": "MR"}, all_targets=["pacs", "hub"])
    assert result == ["pacs", "hub"]


def test_match_is_case_insensitive() -> None:
    engine = _engine([{"rule": "modality=ct", "targets": ["pacs"]}])
    result = engine.resolve({"Modality": "CT"}, all_targets=["pacs", "hub"])
    assert result == ["pacs"]


# ══════════════════════════════════════════════════════════════════════
# Priority ordering
# ══════════════════════════════════════════════════════════════════════

def test_high_priority_rule_wins() -> None:
    engine = _engine(
        [
            {"rule": "Modality=CT", "targets": ["pacs"], "priority": "normal"},
            {"rule": "Modality=CT", "targets": ["archive"], "priority": "high"},
        ]
    )
    result = engine.resolve({"Modality": "CT"}, all_targets=["pacs", "archive", "hub"])
    assert result == ["archive"]


def test_normal_priority_falls_through_when_no_high_match() -> None:
    engine = _engine(
        [
            {"rule": "Modality=CT", "targets": ["pacs"], "priority": "normal"},
            {"rule": "Modality=CT", "targets": ["archive"], "priority": "high"},
        ]
    )
    result = engine.resolve({"Modality": "MR"}, all_targets=["pacs", "archive", "hub"])
    assert result == ["pacs", "archive", "hub"]  # no match → default all


# ══════════════════════════════════════════════════════════════════════
# Multi-target expansion
# ══════════════════════════════════════════════════════════════════════

def test_rule_expands_to_multiple_targets() -> None:
    engine = _engine([{"rule": "Modality=MR", "targets": ["pacs", "archive"]}])
    result = engine.resolve({"Modality": "MR"}, all_targets=["pacs", "archive", "hub"])
    assert result == ["pacs", "archive"]


# ══════════════════════════════════════════════════════════════════════
# Tag matching beyond Modality
# ══════════════════════════════════════════════════════════════════════

def test_matches_any_tag_not_just_modality() -> None:
    engine = _engine([{"rule": "StudyDescription=*chest*", "targets": ["pacs"]}])
    result = engine.resolve(
        {"Modality": "CR", "StudyDescription": "CHEST X-RAY"}, all_targets=["pacs", "hub"]
    )
    assert result == ["pacs"]


def test_wildcard_prefix_suffix() -> None:
    engine = _engine([{"rule": "Modality=*", "targets": ["pacs"]}])
    result = engine.resolve({"Modality": "US"}, all_targets=["pacs", "hub"])
    assert result == ["pacs"]


# ══════════════════════════════════════════════════════════════════════
# Invalid rules
# ══════════════════════════════════════════════════════════════════════

def test_invalid_rule_expression_rejected() -> None:
    with pytest.raises(RuleSyntaxError):
        _engine([{"rule": "no-equals-here", "targets": ["pacs"]}])


def test_empty_rule_rejected() -> None:
    with pytest.raises(RuleSyntaxError):
        _engine([{"rule": "   ", "targets": ["pacs"]}])
