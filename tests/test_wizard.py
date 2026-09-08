"""S06-T5 (RED): Web setup wizard state machine (PRD §2.2 Flow A, US-08).

The guided wizard walks a non-technical user through initial configuration:
receiver → destinations → reports/hub → summary/save, with per-step validation
gates (US-08 AC: step-by-step setup with connectivity validation).

Behaviors:
1. The wizard starts on the first step with all steps incomplete
2. ``next`` advances; ``back`` returns; boundaries are guarded
3. A step can only be completed when its validation gate passes
4. The final step can only be saved when every step is complete
"""

from __future__ import annotations

import pytest

from mercure_gateway.web.wizard import SetupWizard


@pytest.fixture()
def wizard() -> SetupWizard:
    return SetupWizard()


# ══════════════════════════════════════════════════════════════════════
# Step model
# ══════════════════════════════════════════════════════════════════════

def test_starts_on_first_step(wizard: SetupWizard) -> None:
    assert wizard.current_step() == "receiver"
    assert not wizard.is_complete()


def test_step_order() -> None:
    assert SetupWizard().steps == ["receiver", "destinations", "reports", "summary"]


def test_next_advances(wizard: SetupWizard) -> None:
    wizard.next()
    assert wizard.current_step() == "destinations"


def test_next_boundary_at_last_step(wizard: SetupWizard) -> None:
    for _ in range(len(wizard.steps) - 1):
        wizard.next()
    assert wizard.current_step() == "summary"
    wizard.next()
    assert wizard.current_step() == "summary"  # cannot advance past the end


def test_back_returns(wizard: SetupWizard) -> None:
    wizard.next()
    wizard.next()
    wizard.back()
    assert wizard.current_step() == "destinations"


def test_back_boundary_at_first_step(wizard: SetupWizard) -> None:
    wizard.back()
    assert wizard.current_step() == "receiver"


# ══════════════════════════════════════════════════════════════════════
# Validation gates
# ══════════════════════════════════════════════════════════════════════

def test_receiver_gate_valid(wizard: SetupWizard) -> None:
    errors = wizard.validate_step(
        "receiver", {"ae_title": "GATEWAY", "port": 11112}
    )
    assert errors == []
    wizard.complete_step("receiver", {"ae_title": "GATEWAY", "port": 11112})
    assert wizard.is_step_complete("receiver")


def test_receiver_gate_rejects_bad_port(wizard: SetupWizard) -> None:
    errors = wizard.validate_step("receiver", {"ae_title": "GATEWAY", "port": 0})
    assert any("port" in e for e in errors)


def test_receiver_gate_rejects_blank_ae(wizard: SetupWizard) -> None:
    errors = wizard.validate_step("receiver", {"ae_title": "", "port": 11112})
    assert any("ae_title" in e for e in errors)


def test_destinations_gate_requires_at_least_one(wizard: SetupWizard) -> None:
    errors = wizard.validate_step("destinations", {"destinations": []})
    assert errors, "no destinations should fail validation"


def test_destinations_gate_valid(wizard: SetupWizard) -> None:
    errors = wizard.validate_step(
        "destinations",
        {
            "destinations": [
                {"name": "hub", "host": "hub.local", "port": 11112, "aet": "MERCURE"}
            ]
        },
    )
    assert errors == []


def test_reports_gate_optional(wizard: SetupWizard) -> None:
    errors = wizard.validate_step("reports", {"enabled": False})
    assert errors == []


def test_cannot_complete_unknown_step(wizard: SetupWizard) -> None:
    with pytest.raises(KeyError):
        wizard.validate_step("nope", {})


# ══════════════════════════════════════════════════════════════════════
# Completion / save gate
# ══════════════════════════════════════════════════════════════════════

def test_not_complete_until_all_steps_done(wizard: SetupWizard) -> None:
    wizard.complete_step("receiver", {"ae_title": "GATEWAY", "port": 11112})
    assert not wizard.is_complete()


def test_complete_when_all_steps_done(wizard: SetupWizard) -> None:
    wizard.complete_step("receiver", {"ae_title": "GATEWAY", "port": 11112})
    wizard.complete_step(
        "destinations",
        {"destinations": [{"name": "hub", "host": "h", "port": 11112, "aet": "M"}]},
    )
    wizard.complete_step("reports", {"enabled": False})
    wizard.complete_step("summary", {})
    assert wizard.is_complete()


def test_summary_requires_all(wizard: SetupWizard) -> None:
    errors = wizard.validate_step(
        "summary", {}
    )
    assert errors  # cannot reach summary with incomplete steps


def test_wizard_validate_api_endpoint() -> None:
    """POST /api/wizard/validate/{step} returns errors via the HTTP API."""
    from conftest import FakeForwarder, FakeReceiver
    from fastapi.testclient import TestClient

    from mercure_gateway.config import default_config
    from mercure_gateway.spool import Spool
    from mercure_gateway.spool.db import mem_database
    from mercure_gateway.web import create_app

    spool = Spool(mem_database())
    app = create_app(default_config(), spool)
    app.state.receiver = FakeReceiver()
    app.state.forwarder = FakeForwarder()
    client = TestClient(app)

    r = client.post("/api/wizard/validate/receiver", json={"ae_title": "GATEWAY", "port": 11112})
    assert r.status_code == 200
    assert r.json()["errors"] == []

    r_bad = client.post("/api/wizard/validate/receiver", json={"ae_title": "", "port": 0})
    assert r_bad.status_code == 200
    assert r_bad.json()["errors"]

    r_unknown = client.post("/api/wizard/validate/nope", json={})
    assert r_unknown.status_code == 400
