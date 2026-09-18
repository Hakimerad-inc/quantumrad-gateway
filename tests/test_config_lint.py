"""Config lint tests — non-fatal misconfiguration warnings (refinement 2026-09-17).

These pin the contract the panel's warnings banner depends on: lint never rejects
a config, it reports findings with a path the editor can highlight.
"""

from __future__ import annotations

import pytest

from mercure_gateway.config import (
    DICOMDestination,
    ForwardingRule,
    GatewayConfig,
)
from mercure_gateway.config.lint import lint_config


def _cfg(**overrides: object) -> GatewayConfig:
    base = GatewayConfig().model_dump()
    base.update(overrides)
    return GatewayConfig.model_validate(base)


@pytest.mark.parametrize(
    "check",
    [
        "lint_forwarding_rules",
        "lint_destinations",
        "lint_config_version",
    ],
)
def test_lint_clean_config_has_no_warnings(check: str) -> None:
    """A default config with one healthy destination lints clean."""
    from mercure_gateway.config import lint as lint_module

    cfg = _cfg(destinations=[DICOMDestination(name="pacs", host="h", port=104, aet_target="A")])
    fn = getattr(lint_module, check)
    assert fn(cfg) == []


def test_stale_forwarding_rule_target_is_flagged() -> None:
    """A rule naming a removed destination narrows routing — surface it."""
    cfg = _cfg(
        destinations=[DICOMDestination(name="pacs-a", host="h", port=104, aet_target="A")],
        forwarding_rules=[
            ForwardingRule(rule="StudyDescription=*CHEST*", targets=["pacs-a", "pacs-gone"])
        ],
    )
    (warning,) = [w for w in lint_config(cfg) if w.path.startswith("forwarding_rules")]
    assert "pacs-gone" in warning.message
    assert warning.path == "forwarding_rules[0].targets"


def test_malformed_forwarding_rule_is_flagged() -> None:
    """An unparsable rule makes routing fail open — so it must be flagged.

    The spool ignores a rule it cannot parse and routes the study to every
    destination. That is the right call at runtime (a stranded clinical study
    is worse than an over-delivered one), but it is invisible in operation:
    a study routed everywhere looks like a config with no rules. Lint is what
    turns it into something the operator sees at save time, in the panel
    (review P0-9).
    """
    cfg = _cfg(
        destinations=[DICOMDestination(name="pacs", host="h", port=104, aet_target="A")],
        forwarding_rules=[ForwardingRule(rule="no-equals-here", targets=["pacs"])],
    )
    (warning,) = [w for w in lint_config(cfg) if w.path == "forwarding_rules[0].rule"]
    assert "cannot be parsed" in warning.message
    assert "no-equals-here" in warning.message
    assert warning.severity == "warning"


def test_malformed_rule_does_not_mask_its_stale_targets() -> None:
    """An unparsable rule's targets are not trustworthy — skip that check.

    ``compile_rule`` raising means the rule text was never understood, so
    reporting its targets as stale would be advice about a rule that will not
    run at all. The syntax finding is the one that matters.
    """
    cfg = _cfg(
        destinations=[DICOMDestination(name="pacs", host="h", port=104, aet_target="A")],
        forwarding_rules=[ForwardingRule(rule="no-equals-here", targets=["ghost"])],
    )
    findings = [w for w in lint_config(cfg) if w.path.startswith("forwarding_rules")]
    assert len(findings) == 1
    assert findings[0].path == "forwarding_rules[0].rule"


def test_both_rule_grammars_lint_clean() -> None:
    """The short form and the general form are both first-class now."""
    cfg = _cfg(
        destinations=[DICOMDestination(name="pacs", host="h", port=104, aet_target="A")],
        forwarding_rules=[
            ForwardingRule(rule="modality:CT", targets=["pacs"]),
            ForwardingRule(rule="StudyDescription=*chest*", targets=["pacs"]),
        ],
    )
    assert [w for w in lint_config(cfg) if w.path.startswith("forwarding_rules")] == []


def test_all_disabled_destinations_warn() -> None:
    """A queue that can never drain is worth a nudge, not a rejection."""
    cfg = _cfg(
        destinations=[
            DICOMDestination(name="pacs", host="h", port=104, aet_target="A", enabled=False)
        ],
    )
    (warning,) = [w for w in lint_config(cfg) if w.path == "destinations"]
    assert "disabled" in warning.message


def test_no_destinations_at_all_is_not_a_warning() -> None:
    """An empty destination list is the pre-wizard state, not a misconfiguration."""
    assert lint_config(GatewayConfig()) == []


def test_duplicate_destination_names_are_flagged() -> None:
    """Names key the routing tables; a duplicate silently collapses."""
    cfg = _cfg(
        destinations=[
            DICOMDestination(name="pacs", host="a", port=104, aet_target="A"),
            DICOMDestination(name="pacs", host="b", port=104, aet_target="B"),
        ],
    )
    dup = [w for w in lint_config(cfg) if "already used" in w.message]
    assert len(dup) == 1
    assert dup[0].path == "destinations[1].name"


def test_future_config_version_is_informational() -> None:
    """A version other than 1.0 is noted, not treated as a fault."""
    cfg = _cfg(config_version="2.0")
    (warning,) = [w for w in lint_config(cfg) if w.path == "config_version"]
    assert warning.severity == "info"
    assert "2.0" in warning.message


def test_warnings_serialize_for_the_api() -> None:
    """The panel consumes the dict form; pin its shape."""
    cfg = _cfg(
        destinations=[
            DICOMDestination(name="pacs", host="h", port=104, aet_target="A", enabled=False)
        ],
    )
    warnings = lint_config(cfg)
    assert all(set(w.as_dict()) == {"path", "message", "severity"} for w in warnings)
