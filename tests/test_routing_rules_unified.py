"""Review P0-9: the routing-rule engines are one implementation, not two.

Before the review there were two rule engines with incompatible semantics and
neither knew about the other:

- ``rules.RuleEngine`` understood only ``Tag=value``, so it raised
  ``RuleSyntaxError`` on every rule actually deployed (``modality:CT`` has no
  ``=``).  It was reachable only from the rule tester and these tests — dead
  production code.
- ``Spool._route_targets`` hand-rolled a ``modality:`` matcher that understood
  neither priority nor the general grammar, and unioned every matching rule
  regardless of priority.

So an operator could preview one routing decision and get the other.  These
tests assert the unification: the engine parses both grammars, the spool
delegates to it, and a malformed rule fails open rather than stranding a study.
``tests/test_routing_rules.py`` and ``tests/test_rules.py`` pass *unchanged*
as the backward-compatibility proof.
"""

from __future__ import annotations

import logging

import pytest

from mercure_gateway.config import (
    DICOMDestination,
    ForwardingRule,
    GatewayConfig,
    default_config,
)
from mercure_gateway.rules import RuleEngine, RuleSyntaxError, compile_rule
from mercure_gateway.spool import Spool
from mercure_gateway.spool.db import mem_database


def make_destination(name: str) -> DICOMDestination:
    return DICOMDestination(
        name=name, type="dicom", host="127.0.0.1", port=11112, aet_target="X"
    )


def make_spool(rules: list[ForwardingRule] | None = None) -> Spool:
    cfg: GatewayConfig = default_config()
    cfg.forwarding_rules = rules or []
    return Spool(mem_database(), cfg)


def routes_for(spool: Spool, study_id: int) -> list[str]:
    return [r["target_name"] for r in spool._db.get_routes(study_id)]


# ══════════════════════════════════════════════════════════════════════════
# Both grammars compile through one parser
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("rule_text", "tag"),
    [
        ("modality:CT", "modality"),
        ("Modality:CT", "Modality"),
        ("Modality=CT", "Modality"),
        ("StudyDescription=*chest*", "StudyDescription"),
    ],
)
def test_both_grammars_compile_to_a_tag_and_pattern(rule_text: str, tag: str) -> None:
    parsed = compile_rule(ForwardingRule(rule=rule_text, targets=["pacs"]))
    assert parsed[0] == tag
    assert parsed[2] == ["pacs"]


def test_short_form_matches_via_the_general_matcher() -> None:
    """``modality:CT`` and ``Modality=CT`` route identically — one matcher."""
    engine = RuleEngine([ForwardingRule(rule="modality:CT", targets=["pacs"])])

    matched, matched_any = engine.match({"Modality": "CT"})

    assert matched_any
    assert matched == ["pacs"]


def test_short_form_is_case_insensitive_and_spaced() -> None:
    """The deployed spellings (' modality:ct ') all resolve."""
    engine = RuleEngine([ForwardingRule(rule=" modality:ct ", targets=["pacs"])])

    assert engine.match({"Modality": "CT"}) == (["pacs"], True)
    assert engine.match({"Modality": "MR"}) == ([], False)


@pytest.mark.parametrize("rule_text", ["modality:", ":CT", "   ", "=", "=CT", "modality="])
def test_malformed_rules_are_rejected(rule_text: str) -> None:
    """Parsing a single bad rule raises; the engine skips, it does not raise.

    ``compile_rule`` is the strict boundary — that is what the config lint uses
    to reject a bad rule at save time.
    """
    with pytest.raises(RuleSyntaxError):
        compile_rule(ForwardingRule(rule=rule_text, targets=["pacs"]))


def test_engine_skips_a_malformed_rule_instead_of_failing() -> None:
    """One bad rule does not take the whole config down with it.

    The alternative — failing construction — means a single typo silently
    routes *every* study to *every* destination, which is a worse failure than
    the rule being ignored.  ``test_spool_fails_open_on_a_malformed_rule``
    covers the routing consequence; this covers the engine's own contract.
    """
    engine = RuleEngine(
        [
            ForwardingRule(rule="no-equals-here", targets=["pacs"]),
            ForwardingRule(rule="modality:CT", targets=["hub"]),
        ]
    )

    assert len(engine.skipped) == 1
    assert engine.match({"Modality": "CT"}) == (["hub"], True)


def test_empty_value_after_colon_is_rejected() -> None:
    """``modality:`` names no modality — it must not silently match nothing."""
    with pytest.raises(RuleSyntaxError):
        compile_rule(ForwardingRule(rule="modality:", targets=["pacs"]))


# ══════════════════════════════════════════════════════════════════════════
# Union-at-best-priority — the semantic decision
# ══════════════════════════════════════════════════════════════════════════


def test_matching_rules_at_the_same_priority_union_their_targets() -> None:
    """Two rules at one priority both apply — the deployed shape.

    A study has one Modality, but two rules can both match it (e.g. a
    ``modality:CT`` rule and a ``StudyDescription=*chest*`` rule).  Unioning at
    the best priority preserves what the old hand-rolled router did — it
    ignored priority entirely and unioned every match — for every config where
    all rules share the default priority, which is every deployed one.
    """
    engine = RuleEngine(
        [
            ForwardingRule(rule="modality:CT", targets=["pacs"]),
            ForwardingRule(rule="StudyDescription=*chest*", targets=["archive"]),
        ]
    )

    matched, matched_any = engine.match({"Modality": "CT", "StudyDescription": "CHEST"})

    assert matched_any
    assert set(matched) == {"pacs", "archive"}


def test_a_higher_priority_rule_supersedes_lower_priority_matches() -> None:
    engine = RuleEngine(
        [
            ForwardingRule(rule="modality:CT", targets=["pacs"], priority="normal"),
            ForwardingRule(rule="modality:CT", targets=["archive"], priority="high"),
        ]
    )

    assert engine.match({"Modality": "CT"}) == (["archive"], True)


def test_union_order_follows_config_order() -> None:
    """Deterministic output — the preview must not shuffle between clicks."""
    engine = RuleEngine(
        [
            ForwardingRule(rule="modality:CT", targets=["b", "a"]),
            ForwardingRule(rule="modality:CT", targets=["c", "a"]),
        ]
    )

    first = engine.match({"Modality": "CT"})
    second = engine.match({"Modality": "CT"})

    assert first == second == (["b", "a", "c"], True)


def test_no_match_reports_matched_any_false() -> None:
    """``matched_any`` is what the spool's stale-target guard keys on."""
    engine = RuleEngine([ForwardingRule(rule="modality:CT", targets=["pacs"])])

    assert engine.match({"Modality": "US"}) == ([], False)


def test_an_empty_tag_value_matches_nothing() -> None:
    """A study with no Modality takes the default route, as before.

    The old router short-circuited on an empty modality; the unified one gets
    the same outcome from the matcher itself (an empty value matches no
    pattern), so a study with an unreadable modality still reaches every
    destination.
    """
    engine = RuleEngine([ForwardingRule(rule="modality:CT", targets=["pacs"])])

    assert engine.match({}) == ([], False)


# ══════════════════════════════════════════════════════════════════════════
# The spool delegates — and fails open
# ══════════════════════════════════════════════════════════════════════════


def test_spool_routes_through_the_shared_engine() -> None:
    """The live path and the preview are one implementation now."""
    spool = make_spool(
        [
            ForwardingRule(rule="modality:CT", targets=["pacs"]),
            ForwardingRule(rule="StudyDescription=*chest*", targets=["archive"]),
        ]
    )
    study_id = spool.receive("1.2.3.4.10", modality="CT")

    # The spool only has the Modality tag at enqueue time, so the
    # StudyDescription rule cannot match here — a preview given a full tag set
    # would route to both.  That asymmetry is documented, not a bug.
    spool.enqueue(study_id, [make_destination("pacs"), make_destination("archive")])

    assert routes_for(spool, study_id) == ["pacs"]


def test_spool_unions_two_matching_modality_rules() -> None:
    """The old hand-rolled router unioned; the delegate still does."""
    spool = make_spool(
        [
            ForwardingRule(rule="modality:CT", targets=["pacs"]),
            ForwardingRule(rule="modality:CT", targets=["hub"]),
        ]
    )
    study_id = spool.receive("1.2.3.4.11", modality="CT")
    spool.enqueue(study_id, [make_destination("pacs"), make_destination("hub")])

    assert sorted(routes_for(spool, study_id)) == ["hub", "pacs"]


def test_spool_honours_priority_when_two_rules_conflict() -> None:
    """Priority is no longer ignored on the live path."""
    spool = make_spool(
        [
            ForwardingRule(rule="modality:CT", targets=["pacs"], priority="normal"),
            ForwardingRule(rule="modality:CT", targets=["hub"], priority="high"),
        ]
    )
    study_id = spool.receive("1.2.3.4.12", modality="CT")
    spool.enqueue(study_id, [make_destination("pacs"), make_destination("hub")])

    assert routes_for(spool, study_id) == ["hub"]


def test_spool_fails_open_on_a_malformed_rule(
    spool_with_bad_rule: tuple[Spool, int],
) -> None:
    """A typo'd rule over-delivers rather than stranding the study.

    The alternative — fail closed — leaves the study in RECEIVED, which the
    panel cannot recover (Enqueue renders only for RECEIVED... which is where
    it already is; the operator sees a study that never moves and no reason
    why).  Over-delivery is visible in the audit log and reversible; a stranded
    study in a clinical workflow is not.
    """
    spool, study_id = spool_with_bad_rule
    destinations = [make_destination("pacs"), make_destination("hub")]

    spool.enqueue(study_id, destinations)

    assert sorted(routes_for(spool, study_id)) == ["hub", "pacs"]


def test_spool_logs_the_malformed_rule(caplog: pytest.LogCaptureFixture) -> None:
    """Fail-open is not fail-silent: the operator is told which rule.

    The engine logs at construction (once per rule set, since the spool caches
    it) rather than per study, so a busy gateway does not amplify one typo into
    a log storm — but the message still names the offending rule text.
    """
    with caplog.at_level(logging.ERROR, logger="mercure_gateway.rules"):
        make_spool([ForwardingRule(rule="no-equals-here", targets=["pacs"])])._rule_engine()

    assert any("could not be parsed" in rec.message for rec in caplog.records)
    assert any("no-equals-here" in rec.message for rec in caplog.records)


def test_engine_reports_skipped_rules_for_the_caller() -> None:
    """``skipped`` lets a preview surface the bad rule alongside its result."""
    engine = RuleEngine(
        [
            ForwardingRule(rule="no-equals-here", targets=["pacs"]),
            ForwardingRule(rule="modality:CT", targets=["hub"]),
        ]
    )

    assert len(engine.skipped) == 1
    assert engine.skipped[0][0] == 0  # index into the configured rule list
    assert "no-equals-here" in engine.skipped[0][1]
    # The good rule still works.
    assert engine.match({"Modality": "CT"}) == (["hub"], True)


def test_malformed_rule_does_not_poison_a_following_study(
    spool_with_bad_rule: tuple[Spool, int],
) -> None:
    """The bad compile stays uncached, so each study re-checks the rules.

    A study that matches a *good* rule still routes by it — the malformed one
    only fails open the studies it would have matched.
    """
    spool, _ = spool_with_bad_rule
    # Rule list: [malformed, modality:MR → hub]. This study matches the good
    # rule (MR), the previous study (CT) matched nothing but the broken one.
    study_id = spool.receive("1.2.3.4.14", modality="MR")
    spool.enqueue(study_id, [make_destination("pacs"), make_destination("hub")])

    assert routes_for(spool, study_id) == ["hub"]


@pytest.fixture()
def spool_with_bad_rule() -> tuple[Spool, int]:
    """A spool whose first rule cannot parse, plus a CT study that hits it."""
    spool = make_spool(
        [
            ForwardingRule(rule="no-equals-here", targets=["pacs"]),
            ForwardingRule(rule="modality:MR", targets=["hub"]),
        ]
    )
    study_id = spool.receive("1.2.3.4.13", modality="CT")
    return spool, study_id


def test_rule_engine_is_compiled_once_per_rule_set() -> None:
    """The compile is cached: N studies cost one parse, not N."""
    spool = make_spool([ForwardingRule(rule="modality:CT", targets=["pacs"])])
    first = spool._rule_engine()

    for i in range(5):
        study_id = spool.receive(f"1.2.3.4.{20 + i}", modality="CT")
        spool.enqueue(study_id, [make_destination("pacs")])

    assert spool._rule_engine() is first


def test_swapping_the_rule_list_recompiles() -> None:
    """A config swap (restart, or a test) is picked up, not served stale."""
    spool = make_spool([ForwardingRule(rule="modality:CT", targets=["pacs"])])
    before = spool._rule_engine()

    assert spool._config is not None
    spool._config.forwarding_rules = [ForwardingRule(rule="modality:MR", targets=["hub"])]

    after = spool._rule_engine()
    assert after is not before
    assert after.match({"Modality": "MR"}) == (["hub"], True)
