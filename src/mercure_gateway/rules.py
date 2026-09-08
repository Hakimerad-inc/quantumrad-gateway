"""Forwarding rules engine (PRD §5.5, US-09, S07-T6).

Evaluates routing rules against extracted DICOM tags (the ``*.tags`` files
from S02-T4) to decide which targets receive a study.  Rules use the syntax::

    TagName=value

where ``value`` may use ``*`` as a wildcard prefix/suffix matcher.  Matching is
case-insensitive.  Priority ordering: ``high`` > ``normal`` > ``low``.

When no rule matches, every enabled target receives the study (default route).
"""

from __future__ import annotations

import fnmatch

from mercure_gateway.config import ForwardingRule

__all__ = ["RuleEngine", "RuleSyntaxError"]

_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


class RuleSyntaxError(ValueError):
    """Raised when a forwarding rule cannot be parsed."""


class RuleEngine:
    """Compile and evaluate a set of forwarding rules."""

    def __init__(self, rules: list[ForwardingRule]) -> None:
        self.rules = list(rules)
        self._compiled = [self._compile(rule) for rule in rules]

    @staticmethod
    def _compile(rule: ForwardingRule) -> tuple[str, str, list[str]]:
        """Parse ``rule.rule`` into ``(tag, pattern, targets)``.

        Raises :class:`RuleSyntaxError` on malformed expressions.
        """
        text = rule.rule.strip()
        if "=" not in text:
            raise RuleSyntaxError(f"rule {rule.rule!r} must be 'Tag=value'")
        tag, _, pattern = text.partition("=")
        tag = tag.strip()
        pattern = pattern.strip()
        if not tag or not pattern:
            raise RuleSyntaxError(f"rule {rule.rule!r} has empty tag or value")
        return tag, pattern, list(rule.targets)

    def resolve(
        self,
        tags: dict[str, str],
        *,
        all_targets: list[str],
    ) -> list[str]:
        """Return the targets a study with *tags* should be routed to.

        The highest-priority matching rule wins; when none matches, return
        ``all_targets`` (default route).
        """
        best_rank: int | None = None
        matched: list[str] = []
        tag_index = {k.lower(): v for k, v in tags.items()}
        for rule, (tag, pattern, targets) in zip(self.rules, self._compiled, strict=True):
            value = tag_index.get(tag.lower(), "")
            if value and fnmatch.fnmatch(value.lower(), pattern.lower()):
                rank = _PRIORITY_RANK.get(rule.priority, 1)
                if best_rank is None or rank < best_rank:
                    best_rank = rank
                    matched = targets
        return matched if best_rank is not None else list(all_targets)
