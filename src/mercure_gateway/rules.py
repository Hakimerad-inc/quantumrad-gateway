"""Forwarding rules engine (PRD §5.5, US-09, S07-T6).

Evaluates routing rules against extracted DICOM tags (the ``*.tags`` files
from S02-T4) to decide which targets receive a study.  Two spellings are
accepted, both compiled to the same ``(tag, pattern, targets)`` triple::

    modality:CT       short form, matches the study's Modality tag
    TagName=value     general form; ``value`` may use ``*`` as a wildcard
                      prefix/suffix matcher

Matching is case-insensitive.  Priority ordering: ``high`` > ``normal`` >
``low``.  Among the rules that match, the targets of every rule at the *best*
matching priority are unioned in config order.  When no rule matches, every
enabled target receives the study (default route).

One engine, two callers
-----------------------
Before review P0-9 there were two rule engines with incompatible semantics,
and neither knew about the other.  This one understood only ``Tag=value`` and
raised :class:`RuleSyntaxError` on every rule actually deployed (``modality:CT``
has no ``=``), so it was reachable only from the rule tester and its tests —
dead production code.  The live router (``Spool._route_targets``) hand-rolled a
``modality:`` prefix matcher that understood neither priority nor the general
grammar, and unioned every matching rule regardless of priority.  An operator
could preview one routing decision and get the other, and neither discrepancy
was visible anywhere.

The spool now delegates here, so preview and production are one implementation.
"""

from __future__ import annotations

import fnmatch
import logging

from mercure_gateway.config import ForwardingRule

__all__ = ["RuleEngine", "RuleSyntaxError", "compile_rule"]

logger = logging.getLogger(__name__)

_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


class RuleSyntaxError(ValueError):
    """Raised when a forwarding rule cannot be parsed."""


class RuleEngine:
    """Compile and evaluate a set of forwarding rules.

    A rule that cannot be parsed is *skipped*, not fatal: one typo'd rule
    silently sending every study to every destination would be a worse
    failure than the rule being ignored, and the good rules in the same
    config are still the operator's intent.  The mistake is surfaced three
    ways instead — the config lint flags it at save time, the engine logs it
    at construction, and :attr:`skipped` carries it for a caller to report.
    """

    def __init__(self, rules: list[ForwardingRule]) -> None:
        self.rules = list(rules)
        self._compiled: list[tuple[ForwardingRule, tuple[str, str, list[str]]]] = []
        self.skipped: list[tuple[int, str]] = []
        for index, rule in enumerate(self.rules):
            try:
                self._compiled.append((rule, compile_rule(rule)))
            except RuleSyntaxError as exc:
                self.skipped.append((index, str(exc)))
        if self.skipped:
            logger.error(
                "%d of %d forwarding rule(s) could not be parsed and are ignored: %s",
                len(self.skipped),
                len(self.rules),
                "; ".join(f"[{i}] {msg}" for i, msg in self.skipped),
            )

    def match(self, tags: dict[str, str]) -> tuple[list[str], bool]:
        """Return ``(targets, matched_any)`` for a study carrying *tags*.

        ``matched_any`` is False when no rule matched — the caller's default
        route.  The spool needs that flag to tell "a rule narrowed this study
        to destinations that no longer exist" (a misconfiguration: the study
        must not advance) from "the caller passed only disabled destinations"
        (a deliberate no-op).  It is not derivable by comparing target sets.
        """
        best_rank: int | None = None
        matched: list[str] = []
        seen: set[str] = set()
        tag_index = {k.lower(): v for k, v in tags.items()}
        for rule, (tag, pattern, targets) in self._compiled:
            value = tag_index.get(tag.lower(), "")
            if not value or not fnmatch.fnmatch(value.lower(), pattern.lower()):
                continue
            rank = _PRIORITY_RANK.get(rule.priority, 1)
            if best_rank is None or rank < best_rank:
                # A higher-priority match supersedes everything collected at a
                # worse priority — rules are not unioned across priorities.
                best_rank, matched, seen = rank, [], set()
            if rank == best_rank:
                for target in targets:
                    if target not in seen:
                        seen.add(target)
                        matched.append(target)
        return matched, best_rank is not None

    def resolve(
        self,
        tags: dict[str, str],
        *,
        all_targets: list[str],
    ) -> list[str]:
        """Return the targets a study with *tags* should be routed to.

        Among matching rules the targets at the best priority are unioned; when
        none matches, return ``all_targets`` (default route).
        """
        matched, matched_any = self.match(tags)
        return matched if matched_any else list(all_targets)


def compile_rule(rule: ForwardingRule) -> tuple[str, str, list[str]]:
    """Parse ``rule.rule`` into ``(tag, pattern, targets)``.

    Raises :class:`RuleSyntaxError` on malformed expressions.  Public so the
    config lint can reject a rule that would make routing fail open *at save
    time*, in the panel, instead of discovering it from a log after a study is
    stranded.
    """
    text = rule.rule.strip()
    if not text:
        raise RuleSyntaxError("rule is empty")
    # ``modality:CT`` short form → the general form on the Modality tag.  The
    # separator is unambiguous: a general rule always has ``=``, and a DICOM
    # tag name cannot contain ``:``.
    if "=" not in text and ":" in text:
        tag, _, pattern = text.partition(":")
    else:
        tag, _, pattern = text.partition("=")
    tag = tag.strip()
    pattern = pattern.strip()
    if not tag or not pattern:
        raise RuleSyntaxError(
            f"rule {rule.rule!r} has empty tag or value "
            "(expected 'modality:CT' or 'TagName=value')"
        )
    return tag, pattern, list(rule.targets)
