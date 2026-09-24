"""Audit event vocabulary (PRD §5.4, K5 — 100% of events audited).

Single source of truth for every audit event name the gateway emits.  Tests
assert against these constants so a typo'd event name cannot silently break
coverage (K5).  Extend this list when a new lifecycle transition is added.
"""

from __future__ import annotations

# Study lifecycle (emitted by the Spool / receiver path)
STUDY_RECEIVED = "STUDY_RECEIVED"
STUDY_QUEUED = "STUDY_QUEUED"
STUDY_SENT = "STUDY_SENT"
STUDY_FAILED = "STUDY_FAILED"

# Forwarding (emitted by the Forwarder)
FORWARD_START = "FORWARD_START"
FORWARD_COMPLETE = "FORWARD_COMPLETE"
FORWARD_ERROR = "FORWARD_ERROR"

# Operator actions
RETRY_MANUAL = "RETRY_MANUAL"

# Retention maintenance (emitted by AuditLog.prune)
PRUNE_AUDIT = "PRUNE_AUDIT"

# Scheduled integrity check (emitted by the composition root's chain verifier)
AUDIT_CHAIN_FAILED = "AUDIT_CHAIN_FAILED"

# Scheduled authenticity check (emitted by the composition root's anchor verifier)
AUDIT_ANCHOR_FAILED = "AUDIT_ANCHOR_FAILED"

# Report retrieval (emitted by the ReportRetriever, Sprint 05)
REPORT_REQUESTED = "REPORT_REQUESTED"
REPORT_RETRIEVING = "REPORT_RETRIEVING"
REPORT_RETRIEVED = "REPORT_RETRIEVED"
REPORT_RETRIEVAL_FAILED = "REPORT_RETRIEVAL_FAILED"
REPORT_SLA_EXPIRED = "REPORT_SLA_EXPIRED"

# Everything the gateway can emit — used by the coverage sweep to detect
# orphan/renamed event names (K5).
ALL_EVENTS = (
    STUDY_RECEIVED,
    STUDY_QUEUED,
    STUDY_SENT,
    STUDY_FAILED,
    FORWARD_START,
    FORWARD_COMPLETE,
    FORWARD_ERROR,
    RETRY_MANUAL,
    PRUNE_AUDIT,
    AUDIT_CHAIN_FAILED,
    AUDIT_ANCHOR_FAILED,
    REPORT_REQUESTED,
    REPORT_RETRIEVING,
    REPORT_RETRIEVED,
    REPORT_RETRIEVAL_FAILED,
    REPORT_SLA_EXPIRED,
)

__all__ = [
    "STUDY_RECEIVED",
    "STUDY_QUEUED",
    "STUDY_SENT",
    "STUDY_FAILED",
    "FORWARD_START",
    "FORWARD_COMPLETE",
    "FORWARD_ERROR",
    "RETRY_MANUAL",
    "PRUNE_AUDIT",
    "AUDIT_CHAIN_FAILED",
    "AUDIT_ANCHOR_FAILED",
    "REPORT_REQUESTED",
    "REPORT_RETRIEVING",
    "REPORT_RETRIEVED",
    "REPORT_RETRIEVAL_FAILED",
    "REPORT_SLA_EXPIRED",
    "ALL_EVENTS",
]
