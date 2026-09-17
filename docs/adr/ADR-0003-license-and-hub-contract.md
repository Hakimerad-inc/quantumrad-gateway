# ADR-0003: License and Hub Bookkeeper Contract

**Status:** Partially accepted — Q6 decided, Q7 deferred to the hub team
**Date:** 2026-09-18
**Deciders:** Product + Engineering
**Relates to:** PRD §13 (Q6, Q7), S01-T5, product-refinement-spec §2.6,
`test-rig/bookkeeper/`, `tests/test_hub_bookkeeper_interop.py`

## Context

S01-T5 asked for two external-facing answers before the gateway's
integration surface could be considered settled:

- **Q6** — which open-source license the gateway ships under.
- **Q7** — the shape of the bookkeeper API the mercure hub exposes, which the
  gateway must call to register anchors and confirm delivery.

Both were recorded as blocking on the hub team. Q6 is now decided; Q7 is not,
and this ADR exists so the gap in the sequence (0002 → 0004) does not read as
a decision that was made and lost — it is a decision that is only half made.

## Decision

### Q6 — MIT (accepted)

The gateway is licensed **MIT**, consistent with
[mercure](https://github.com/mercure-imaging/mercure). See `LICENSE`.

MIT over a copyleft licence (GPL) or a weak-copyleft middle ground (LGPL,
MPL-2.0) because the gateway is a satellite to a hub, not a hub itself:
permissive terms keep the integration story simple for vendor PACS work and
site deployments that bundle the gateway alongside other software, which is
the deployment model the product brief assumes.

### Q7 — bookkeeper contract: deferred, proven against a stub (not accepted)

The live hub API shape is **not yet confirmed**. Rather than block, the
gateway side is proven against a contract stub, so that only the live-hub
integration remains:

- `test-rig/bookkeeper/bookkeeper.py` — a stand-in bookkeeper exercising the
  assumed contract.
- `tests/test_hub_bookkeeper_interop.py` — the gateway's side of that
  contract, green against the stub.

The stub encodes the assumption the gateway code rests on: anchors are
registered per study and delivery is confirmable by anchor id. When the hub
team answers Q7, either the contract matches and this ADR closes, or it does
not and the divergence is confined to the hub client boundary.

## Consequences

- License headers/`LICENSE` should stay MIT; a change here is a policy
  decision, not a mechanical one.
- Nothing downstream should assume bookkeeper semantics beyond "register an
  anchor, confirm by id" until Q7 closes. Code that goes further is building
  on the stub, not on an agreement.
- When Q7 lands, update this ADR's status to fully accepted and record the
  confirmed shape — do not open an ADR-0008 for it, so the sequence keeps
  pointing at this gap.
