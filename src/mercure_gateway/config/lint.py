"""Config lint — non-fatal misconfiguration warnings (refinement 2026-09-17).

Validation *rejects* a config that cannot load; lint *accepts* one that loads
but is likely misconfigured, and says why. The distinction matters: a rule
naming a removed destination does not break the gateway, it silently narrows
what gets routed — exactly the class of thing an operator wants surfaced in the
panel rather than in a log file they may never open.

The checks run against the already-validated model (they are called after
``model_validate``), so they can assume well-typed fields and concentrate on
cross-field and semantic mistakes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from mercure_gateway.config import GatewayConfig
from mercure_gateway.rules import RuleSyntaxError, compile_rule

__all__ = ["ConfigWarning", "lint_config"]

# Severity: "warning" = likely wrong, investigate; "info" = intentional-but-noted.
# The panel renders only warnings by default and folds info away.


@dataclass(frozen=True)
class ConfigWarning:
    """One lint finding.

    ``path`` is a dotted pointer into the config document
    (``destinations[1].host``) so the editor can highlight the offending field
    when one exists; some findings are structural and carry an empty path.
    """

    path: str
    message: str
    severity: str = "warning"

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message, "severity": self.severity}


def lint_config(cfg: GatewayConfig) -> list[ConfigWarning]:
    """Return non-fatal warnings about *cfg*; empty means nothing to flag."""
    return [
        *lint_forwarding_rules(cfg),
        *lint_destinations(cfg),
        *lint_secrets(cfg),
        *lint_config_version(cfg),
    ]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def lint_forwarding_rules(cfg: GatewayConfig) -> list[ConfigWarning]:
    """A rule that cannot parse, or that targets no configured destination.

    The config loader already logs the stale-target case
    (``_warn_on_stale_forwarding_rule_targets``), but a log line is invisible
    to an operator working in the panel — this is the same finding, surfaced
    where the mistake is made.

    The syntax check is here because of how routing handles a bad rule: the
    spool fails *open* (every destination receives the study, plus an error
    log) rather than stranding it in RECEIVED.  That is the right call for a
    running appliance, but it means a typo is invisible unless something
    compiles the rule — a study silently over-delivered looks like normal
    operation.  Lint runs at save time, so the operator is told before any
    study is affected (review P0-9).
    """
    if not cfg.forwarding_rules:
        return []
    known = {d.name for d in cfg.destinations}
    warnings: list[ConfigWarning] = []
    for i, rule in enumerate(cfg.forwarding_rules):
        try:
            compile_rule(rule)
        except RuleSyntaxError as exc:
            warnings.append(
                ConfigWarning(
                    path=f"forwarding_rules[{i}].rule",
                    message=(
                        f"Rule cannot be parsed ({exc}) and is ignored at routing "
                        "time — studies matching its intent are sent to every "
                        "destination. Use 'modality:CT' or 'TagName=value'."
                    ),
                )
            )
            # An unparsed rule has no trustworthy targets to check.
            continue
        stale = [t for t in rule.targets if t not in known]
        if stale:
            warnings.append(
                ConfigWarning(
                    path=f"forwarding_rules[{i}].targets",
                    message=(
                        f"Rule targets unknown destination(s) {', '.join(sorted(stale))}. "
                        "Rename or remove the target, or add the destination — studies "
                        "matching this rule will not route to the missing target."
                    ),
                )
            )
    return warnings


def lint_destinations(cfg: GatewayConfig) -> list[ConfigWarning]:
    """Structural destination mistakes that pass validation but bite later.

    A *disabled* destination is fine (operator intent), but one that is the
    *only* destination and disabled means nothing can be delivered, which is
    worth a nudge: the queue will fill with RECEIVED studies.

    Duplicate names used to be a lint warning here; they are now a validation
    error on ``GatewayConfig`` (review P1-2) — a duplicate collapses the
    by-name lookup the web API's '***' restore depends on, which can persist
    the wrong destination's secret.
    """
    warnings: list[ConfigWarning] = []
    enabled = [d for d in cfg.destinations if d.enabled]

    if cfg.destinations and not enabled:
        warnings.append(
            ConfigWarning(
                path="destinations",
                message=(
                    "Every destination is disabled. Studies will be received and "
                    "persisted but never forwarded — enable at least one destination "
                    "or pause the receiver if this is intentional."
                ),
            )
        )
    return warnings


def lint_secrets(cfg: GatewayConfig) -> list[ConfigWarning]:
    """Secrets that will be written to disk in cleartext (review P1-1).

    The appliance encrypts at rest by default and generates its own key on
    first boot. Both opt-outs are legitimate for a sealed read-only appliance
    or a dev box, and neither is a validation error — but they are exactly the
    kind of thing an operator wants to see stated in the panel rather than
    have to reason about from an environment variable they did not set.
    """
    warnings: list[ConfigWarning] = []
    if not cfg.credentials.encrypted:
        warnings.append(
            ConfigWarning(
                path="credentials.encrypted",
                message=(
                    "Encryption at rest is disabled in this config — destination "
                    "passwords and the admin password hash are stored in cleartext "
                    "in the config file."
                ),
            )
        )
    if os.environ.get(_ALLOW_PLAINTEXT_ENV, "") == "1":
        warnings.append(
            ConfigWarning(
                path="credentials",
                message=(
                    "MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS=1 is set in this "
                    "process's environment, so secrets are written in cleartext "
                    "regardless of the setting above."
                ),
                severity="info",
            )
        )
    return warnings


_ALLOW_PLAINTEXT_ENV = "MERCURE_GATEWAY_ALLOW_PLAINTEXT_SECRETS"


def lint_config_version(cfg: GatewayConfig) -> list[ConfigWarning]:
    """A version the loader does not recognise today.

    Validation only checks the field is a string; a future-tense version
    (``"2.0"``) will load but may be missing migrations, so note it. This is
    informational, not a fault.
    """
    if cfg.config_version != "1.0":
        return [
            ConfigWarning(
                path="config_version",
                message=(
                    f"Config version is {cfg.config_version!r}; this build expects "
                    "'1.0'. Fields may be interpreted differently than intended."
                ),
                severity="info",
            )
        ]
    return []
