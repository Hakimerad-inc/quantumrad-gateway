"""S3 handler — boto3 object-store delivery (PRD §2.3 v1.1, S07-T4).

Satisfies the :class:`~mercure_gateway.forwarder.DestinationHandler` protocol.

Delivery uploads every DICOM file from the spooled study to an S3-compatible
bucket using boto3.  The operation is a **copy** — spool files remain.
"""

from __future__ import annotations

from pathlib import Path

from mercure_gateway.config import S3Destination
from mercure_gateway.forwarder import DeliveryResult
from mercure_gateway.spool import Spool

__all__ = ["S3Handler"]


class S3Handler:
    def __init__(self, destination: S3Destination, spool: Spool) -> None:
        self.destination = destination
        self.spool = spool

    def deliver(self, task: object, spool_dir: Path) -> DeliveryResult:
        from mercure_gateway.spool import ClaimedTask

        assert isinstance(task, ClaimedTask)
        try:
            study_uid = self.spool.study_uid(task.study_id)
        except KeyError:
            return DeliveryResult(ok=False, error="study not found")

        files = self.spool.study_files(study_uid)
        if not files:
            return DeliveryResult(ok=False, error="no DICOM files found for study")

        try:
            import boto3  # type: ignore[import-untyped]
        except ImportError:
            return DeliveryResult(ok=False, error="boto3 not installed")

        try:
            kwargs = {}
            if self.destination.endpoint_url:
                kwargs["endpoint_url"] = self.destination.endpoint_url
            if self.destination.region:
                kwargs["region_name"] = self.destination.region
            if self.destination.access_key_id:
                kwargs["aws_access_key_id"] = self.destination.access_key_id
            if self.destination.secret_access_key:
                kwargs["aws_secret_access_key"] = self.destination.secret_access_key
            s3 = boto3.client("s3", **kwargs)
            for f in files:
                key = f"{self.destination.remote_prefix}{f.name}"
                s3.upload_file(str(f), self.destination.bucket, key)
        except Exception as exc:  # noqa: BLE001 — boundary: map to DeliveryResult
            return DeliveryResult(ok=False, error=str(exc))

        return DeliveryResult(ok=True)