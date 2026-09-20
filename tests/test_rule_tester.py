"""S07-T7 (RED): Rule tester — preview routing for a tag set (US-09).

The rule tester lets an operator run a synthetic DICOM tag-set against the
configured rules and see which targets would receive the study, before a real
study arrives.

Behaviors:
1. A matching tag-set previews the rule's targets
2. A non-matching tag-set previews the default (all targets)
3. Priority ordering is reflected in the preview
4. Output is deterministic (same input → same preview)
"""

from __future__ import annotations

import pytest

from mercure_gateway.config import ForwardingRule
from mercure_gateway.rules_tester import preview_routing


def _rules() -> list[ForwardingRule]:
    return [
        ForwardingRule(rule="Modality=CT", targets=["pacs"], priority="normal"),
        ForwardingRule(rule="Modality=CT", targets=["archive"], priority="high"),
        ForwardingRule(rule="Modality=MR", targets=["archive"]),
    ]


def test_preview_matching_tag_set() -> None:
    result = preview_routing(_rules(), {"Modality": "MR"}, all_targets=["pacs", "archive", "hub"])
    assert result.targets == ["archive"]
    assert result.matched_any is True


def test_preview_no_match_returns_all() -> None:
    result = preview_routing(_rules(), {"Modality": "US"}, all_targets=["pacs", "archive", "hub"])
    assert result.targets == ["pacs", "archive", "hub"]
    # No rule matched US — this is the default route, not a rule's targets.
    assert result.matched_any is False


def test_preview_priority_high_wins() -> None:
    result = preview_routing(_rules(), {"Modality": "CT"}, all_targets=["pacs", "archive", "hub"])
    assert result.targets == ["archive"]
    assert result.matched_any is True


def test_preview_is_deterministic() -> None:
    a = preview_routing(_rules(), {"Modality": "CT"}, all_targets=["pacs", "archive", "hub"])
    b = preview_routing(_rules(), {"Modality": "CT"}, all_targets=["pacs", "archive", "hub"])
    assert a == b


def test_preview_accepts_any_tag() -> None:
    rules = [ForwardingRule(rule="StudyDescription=*chest*", targets=["pacs"])]
    result = preview_routing(
        rules, {"StudyDescription": "CHEST X-RAY"}, all_targets=["pacs", "hub"]
    )
    assert result.targets == ["pacs"]


def test_preview_rejects_invalid_rule() -> None:
    from mercure_gateway.rules import RuleSyntaxError

    with pytest.raises(RuleSyntaxError):
        preview_routing(
            [ForwardingRule(rule="malformed", targets=["pacs"])],
            {"Modality": "CT"},
            all_targets=["pacs"],
        )
