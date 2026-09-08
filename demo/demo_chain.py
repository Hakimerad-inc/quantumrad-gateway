"""Demo chain runner (S01-T6, PRD §9 Phase 0).

Orchestrates the 15-minute demo: a fake modality sends a synthetic study to
the gateway receiver, which spools it and forwards it to a hub (Orthanc or the
mercure hub) via the DICOM handler, then the report-pull step queries the hub.

Designed to run with the Sprint 01 test rig (``docs/dev/test-rig.md``) and
standalone over localhost for tests — the hub can be a real Orthanc container
or an in-process :class:`~mercure_gateway.receiver.Receiver`.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from demo.fake_modality import FakeModality
from mercure_gateway.config import DICOMDestination, GatewayConfig, default_config
from mercure_gateway.receiver import Receiver
from mercure_gateway.spool import Spool, StudyState

__all__ = ["DemoChain", "main"]

_POLL_SEC = 0.1


class DemoChain:
    """Send a synthetic study through the gateway and confirm the hub got it."""

    def __init__(
        self,
        gateway_config: GatewayConfig | None = None,
        receiver: Receiver | None = None,
        forwarder: Any | None = None,
        study_uid: str | None = None,
        *,
        timeout_sec: float = 30.0,
        hub_spool: Spool | None = None,
    ) -> None:
        self.gateway_config = gateway_config
        self.receiver = receiver
        self.forwarder = forwarder
        self.study_uid = study_uid
        self.timeout_sec = timeout_sec
        self.spool = receiver.spool if receiver is not None else None
        self.hub_spool = hub_spool

    @staticmethod
    def build_hub_config(
        host: str,
        port: int,
        aet: str,
        base: GatewayConfig | None = None,
    ) -> GatewayConfig:
        """Return *base* (or a fresh config) wired to an Orthanc-as-hub
        DICOM destination named ``"hub"``."""
        if base is None:
            base = default_config()
        base.destinations = [
            DICOMDestination(
                name="hub",
                type="dicom",
                host=host,
                port=port,
                aet_target=aet,
            )
        ]
        return base

    def send_study(self) -> dict[str, int]:
        """Send the synthetic study to the receiver and enqueue it.

        Returns ``{"sent": n, "failure": n}`` from the fake-modality send.
        The study is enqueued for every enabled destination in
        ``gateway_config`` so the forwarder can pick it up.
        """
        if self.receiver is None or self.study_uid is None:
            raise RuntimeError("receiver and study_uid are required to run the demo")
        if self.spool is None:
            raise RuntimeError("receiver has no spool wired")

        fake = FakeModality()
        datasets = fake.create_synthetic_study(self.study_uid)
        result = fake.send_study(
            datasets,
            host="127.0.0.1",
            port=self.receiver.port,
            aet_target=self.receiver.config.ae_title,
        )

        if result["failure"] == 0:
            study_row = self.spool._db.get_study_by_uid(self.study_uid)
            if study_row is not None and self.gateway_config is not None:
                self.spool.enqueue(int(study_row["id"]), self.gateway_config.destinations)
        return result

    def run(self) -> dict[str, int]:
        """Send the synthetic study into the gateway and wait for forwarding.

        Returns ``{"sent": n, "failure": n}`` from the fake-modality send.
        Raises ``TimeoutError`` when the study is not forwarded in time.
        """
        result = self.send_study()

        if result["failure"] == 0:
            state = self.wait_for_forwarded(self.study_uid or "")
            if state == StudyState.FAILED:
                raise RuntimeError(f"study {self.study_uid} failed to forward")

        return {"sent": result["success"], "failure": result["failure"]}

    def retry(self) -> dict[str, Any]:
        """Run the retry demo failure phase (S03-T4).

        The forwarder must be wired WITHOUT a handler for ``dicom`` so the
        first dispatch fails (study → FAILED). Sends the study and waits for
        that failure. The caller then registers the real handler and calls
        ``reforward_study`` (emitting ``RETRY_MANUAL``) to complete the flow.
        """
        if self.study_uid is None:
            raise RuntimeError("study_uid is required to run the retry demo")

        result = self.send_study()
        if result["failure"]:
            return {"sent": result["success"], "failure": result["failure"], "retried": 0}

        state = self.wait_for_forwarded(self.study_uid)
        return {
            "sent": result["success"],
            "failure": result["failure"],
            "state": str(state),
        }

    def wait_for_forwarded(self, study_uid: str) -> StudyState:
        """Poll the gateway spool until *study_uid* reaches a terminal state.

        Returns ``SENT`` or ``FAILED``; raises ``TimeoutError`` if neither is
        reached within ``timeout_sec``.
        """
        if self.spool is None:
            raise RuntimeError("gateway spool is not wired")
        deadline = time.monotonic() + self.timeout_sec
        while time.monotonic() < deadline:
            row = self.spool._db.get_study_by_uid(study_uid)
            if row is not None:
                state = StudyState(str(row["state"]))
                if state in (StudyState.SENT, StudyState.FAILED):
                    return state
            time.sleep(_POLL_SEC)
        raise TimeoutError(f"study {study_uid} not forwarded within {self.timeout_sec}s")

    def hub_received(self, study_uid: str) -> Any | None:
        """Poll the hub spool until *study_uid* appears; return its row."""
        if self.hub_spool is None:
            raise RuntimeError("hub spool is not wired")
        deadline = time.monotonic() + self.timeout_sec
        while time.monotonic() < deadline:
            row = self.hub_spool._db.get_study_by_uid(study_uid)
            if row is not None:
                return row
            time.sleep(_POLL_SEC)
        return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demo-chain",
        description="Run the 15-minute demo: fake modality → gateway → hub, then report pull.",
    )
    parser.add_argument("--hub-host", required=True, help="Hub / Orthanc host")
    parser.add_argument("--hub-port", type=int, required=True, help="Hub DICOM port")
    parser.add_argument("--hub-aet", required=True, help="Hub called AE title")
    parser.add_argument(
        "--study-uid",
        default="1.2.840.10008.99.1",
        help="Study Instance UID for the synthetic study",
    )
    parser.add_argument("--spool-dir", required=True, help="Gateway spool directory")
    parser.add_argument(
        "--receiver-port",
        type=int,
        default=0,
        help="Gateway receiver port (0 = ephemeral)",
    )
    parser.add_argument("--report-host", default=None, help="PACS host for C-FIND report pull")
    parser.add_argument("--report-port", type=int, default=None, help="PACS port for C-FIND")
    parser.add_argument("--report-aet", default=None, help="PACS AE title for C-FIND")
    parser.add_argument(
        "--retry-mode",
        action="store_true",
        help="Simulate a delivery failure then manual re-forward (RETRY_MANUAL audit)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the full demo chain: start gateway, send study, wait for forward,
    then (optionally) pull a report via C-FIND.

    With ``--retry-mode``, the first delivery fails (no handler), then the
    handler is registered and ``reforward_study`` is called manually, emitting
    ``RETRY_MANUAL`` to the audit log.
    """
    args = _build_parser().parse_args(argv)

    cfg = default_config()
    cfg.storage.spool_dir = args.spool_dir
    if args.receiver_port:
        cfg.receiver.port = args.receiver_port
    cfg = DemoChain.build_hub_config(args.hub_host, args.hub_port, args.hub_aet, base=cfg)

    from mercure_gateway.audit import AuditLog
    from mercure_gateway.forwarder import Forwarder, RetryPolicy
    from mercure_gateway.forwarder.handlers.dicom import DICOMHandler
    from mercure_gateway.spool.db import open_database

    hub_dest = cfg.destinations[0]
    assert isinstance(hub_dest, DICOMDestination)

    spool_dir = Path(args.spool_dir)
    spool_dir.mkdir(parents=True, exist_ok=True)
    db = open_database(spool_dir / "mercure-gateway.db")
    audit = AuditLog(db)
    spool = Spool(db, cfg, audit=audit)

    retry_policy = RetryPolicy(base_delay_sec=0, max_attempts=1) if args.retry_mode else None
    forwarder = Forwarder(cfg, spool, retry=retry_policy, audit=audit)

    if not args.retry_mode:
        forwarder.register_handler("dicom", DICOMHandler(hub_dest, spool))

    receiver = Receiver(cfg.receiver, spool)
    receiver.start()
    forwarder.start()

    try:
        chain = DemoChain(
            gateway_config=cfg,
            receiver=receiver,
            forwarder=forwarder,
            study_uid=args.study_uid,
        )

        if args.retry_mode:
            # Failure phase: no handler registered → first dispatch fails.
            result = chain.retry()
            print(f"sent: {result['sent']} instance(s), {result['failure']} failed")
            state = result.get("state")
            if state is None:
                print("first attempt: not attempted (send failed)")
            else:
                print(f"first attempt: {state}")

            # Register the handler and re-forward manually (RETRY_MANUAL audit).
            forwarder.register_handler("dicom", DICOMHandler(hub_dest, spool))
            study_row = spool._db.get_study_by_uid(args.study_uid)
            if study_row is not None:
                requeued = spool.reforward_study(int(study_row["id"]))
                print(f"retried: {requeued} route(s) re-queued (RETRY_MANUAL audited)")
            state = chain.wait_for_forwarded(args.study_uid)
            print(f"final: {state}")
        else:
            result = chain.run()
            state = chain.wait_for_forwarded(args.study_uid)
            print(f"sent: {result['sent']} instance(s), {result['failure']} failed")
            print(f"forwarded: {state}")

        if args.report_host and args.report_port and args.report_aet:
            from demo.report_pull import find_study

            matches = find_study(
                host=args.report_host,
                port=args.report_port,
                aet=args.report_aet,
                study_uid=args.study_uid,
            )
            print(f"report: {len(matches)} study(ies) matched via C-FIND")
        else:
            print("report: skipped (pass --report-host/--report-port/--report-aet)")
        return 0
    finally:
        forwarder.stop()
        receiver.stop()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
