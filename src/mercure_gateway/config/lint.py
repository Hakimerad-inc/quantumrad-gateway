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

from dataclasses import dataclass

from mercure_gateway.config import GatewayConfig

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
        *lint_config_version(cfg),
    ]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def lint_forwarding_rules(cfg: GatewayConfig) -> list[ConfigWarning]:
    """A rule target that names no configured destination narrows routing.

    The config loader already logs this (``_warn_on_stale_forwarding_rule_targets``),
    but a log line is invisible to an operator working in the panel — this is
    the same finding, surfaced where the mistake is made.
    """
    if not cfg.forwarding_rules:
        return []
    known = {d.name for d in cfg.destinations}
    warnings: list[ConfigWarning] = []
    for i, rule in enumerate(cfg.forwarding_rules):
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

    - A *disabled* destination is fine (operator intent), but one that is the
      *only* destination and disabled means nothing can be delivered, which is
      worth a nudge: the queue will fill with RECEIVED studies.
    - Duplicate names silently collapse in routing tables keyed by name.
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

    seen: dict[str, int] = {}
    for i, d in enumerate(cfg.destinations):
        if d.name in seen:
            warnings.append(
                ConfigWarning(
                    path=f"destinations[{i}].name",
                    message=(
                        f"Destination name {d.name!r} is already used at "
                        f"destinations[{seen[d.name]}]. Names must be unique — "
                        "routing and forwarding rules address destinations by name."
                    ),
                )
            )
        else:
            seen[d.name] = i
    return warnings


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
