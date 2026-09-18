"""Rule tester — preview routing for a tag set (PRD §5.5, US-09, S07-T7).

Lets an operator run a synthetic DICOM tag-set against the configured rules
and preview which targets would receive a study before one arrives.  Thin
wrapper over :class:`~mercure_gateway.rules.RuleEngine`.

Strict where the spool is lenient: the engine skips a rule it cannot parse so
that one typo does not silently route every study to every destination, and
the live path inherits that.  *Preview* is an explicit "check my rules" action,
so it raises instead — the operator asked to be told.
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

    Raises :class:`RuleSyntaxError` when any rule is malformed. The live router
    would skip it and route by the rest; preview is the operator's validation
    action, so it reports the problem instead of quietly working around it.
    """
    engine = RuleEngine(rules)
    if engine.skipped:
        index, message = engine.skipped[0]
        raise RuleSyntaxError(f"forwarding_rules[{index}]: {message}")
    return engine.resolve(tags, all_targets=all_targets)
