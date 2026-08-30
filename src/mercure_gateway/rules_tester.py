"""Rule tester — preview routing for a tag set (PRD §5.5, US-09, S07-T7).

Lets an operator run a synthetic DICOM tag-set against the configured rules
and preview which targets would receive a study before one arrives.  Thin
wrapper over :class:`~mercure_gateway.rules.RuleEngine`.
"""

from __future__ import annotations

from mercure_gateway.config import ForwardingRule
from mercure_gateway.rules import RuleEngine, RuleSyntaxError

__all__ = ["preview_routing", "RuleSyntaxError"]


def preview_routing(
    rules: list[ForwardingRule],
    tags: dict[str, str],
    *,
    all_targets: list[str],
) -> list[str]:
    """Return the targets that would receive a study with *tags*.

    Raises :class:`RuleSyntaxError` when any rule is malformed.
    """
    engine = RuleEngine(rules)
    return engine.resolve(tags, all_targets=all_targets)
