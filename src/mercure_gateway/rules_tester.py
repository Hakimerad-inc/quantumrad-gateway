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

from dataclasses import dataclass

from mercure_gateway.config import ForwardingRule
from mercure_gateway.rules import RuleEngine, RuleSyntaxError

__all__ = ["preview_routing", "RulePreview", "RuleSyntaxError"]


@dataclass(frozen=True)
class RulePreview:
    """Where a study carrying a tag set would be routed.

    ``matched_any`` is False when no rule matched, meaning ``targets`` is the
    default route (every enabled destination) rather than anything a rule
    selected. The two look identical in a target list alone, and they are not
    the same statement: one says "my rules decided this", the other says "my
    rules said nothing about this". The panel shows the difference so an
    operator does not read a silent default as an intended match.
    """

    targets: list[str]
    matched_any: bool


def preview_routing(
    rules: list[ForwardingRule],
    tags: dict[str, str],
    *,
    all_targets: list[str],
) -> RulePreview:
    """Preview the targets that would receive a study with *tags*.

    Raises :class:`RuleSyntaxError` when any rule is malformed. The live router
    would skip it and route by the rest; preview is the operator's validation
    action, so it reports the problem instead of quietly working around it.
    """
    engine = RuleEngine(rules)
    if engine.skipped:
        index, message = engine.skipped[0]
        raise RuleSyntaxError(f"forwarding_rules[{index}]: {message}")
    matched, matched_any = engine.match(tags)
    if not matched_any:
        return RulePreview(list(all_targets), False)
    return RulePreview(matched, True)
