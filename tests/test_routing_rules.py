"""TDD (S03-T8, RED): Basic modality include/exclude routing (MVP, PRD §3.1).

Behaviors:
1. A CT study is routed to "hub" only when a ``modality:CT`` rule targets it
2. An MR study is routed to "pacs" only when a ``modality:MR`` rule targets it
3. A study whose modality matches no rule goes to ALL enabled destinations
   (default: all studies to all destinations)
4. Rule targets that name a destination not in the config are ignored
5. Matching is case-insensitive on modality
"""

from __future__ import annotations

from mercure_gateway.config import (
    DICOMDestination,
    ForwardingRule,
    GatewayConfig,
    default_config,
)
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


def receive(spool: Spool, uid: str, modality: str) -> int:
    return spool.receive(uid, modality=modality)


def routes_for(spool: Spool, study_id: int) -> list[str]:
    return [r["target_name"] for r in spool._db.get_routes(study_id)]


# ── Test 1: CT routed to hub only ─────────────────────────────────────


def test_ct_study_routed_to_hub_only() -> None:
    spool = make_spool(
        [
            ForwardingRule(rule="modality:CT", targets=["hub"]),
        ]
    )
    study_id = receive(spool, "1.2.3.4.1", "CT")
    spool.enqueue(study_id, [make_destination("hub"), make_destination("pacs")])

    assert routes_for(spool, study_id) == ["hub"]


# ── Test 2: MR routed to pacs only ────────────────────────────────────


def test_mr_study_routed_to_pacs_only() -> None:
    spool = make_spool(
        [
            ForwardingRule(rule="modality:MR", targets=["pacs"]),
        ]
    )
    study_id = receive(spool, "1.2.3.4.2", "MR")
    spool.enqueue(study_id, [make_destination("hub"), make_destination("pacs")])

    assert routes_for(spool, study_id) == ["pacs"]


# ── Test 3: unmatched modality → all destinations ─────────────────────


def test_unmatched_modality_routes_to_all() -> None:
    spool = make_spool(
        [
            ForwardingRule(rule="modality:CT", targets=["hub"]),
        ]
    )
    study_id = receive(spool, "1.2.3.4.3", "US")  # US not covered by a rule
    spool.enqueue(study_id, [make_destination("hub"), make_destination("pacs")])

    assert routes_for(spool, study_id) == ["hub", "pacs"]


# ── Test 4: no rules at all → all destinations ────────────────────────


def test_no_rules_routes_to_all() -> None:
    spool = make_spool()
    study_id = receive(spool, "1.2.3.4.4", "CT")
    spool.enqueue(study_id, [make_destination("hub"), make_destination("pacs")])

    assert routes_for(spool, study_id) == ["hub", "pacs"]


# ── Test 5: rule names unknown destination → ignored ──────────────────


def test_rule_target_not_in_config_ignored() -> None:
    spool = make_spool(
        [
            ForwardingRule(rule="modality:CT", targets=["nowhere"]),
        ]
    )
    study_id = receive(spool, "1.2.3.4.5", "CT")
    spool.enqueue(study_id, [make_destination("hub"), make_destination("pacs")])

    # "nowhere" doesn't exist → no route created; nothing to route to.
    assert routes_for(spool, study_id) == []


# ── Test 6: case-insensitive modality matching ────────────────────────


def test_modality_matching_is_case_insensitive() -> None:
    spool = make_spool(
        [
            ForwardingRule(rule="modality:ct", targets=["hub"]),
        ]
    )
    study_id = receive(spool, "1.2.3.4.6", "CT")
    spool.enqueue(study_id, [make_destination("hub"), make_destination("pacs")])

    assert routes_for(spool, study_id) == ["hub"]
