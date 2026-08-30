# mercure-gateway Sprint 01 test rig (S01-T2) — Orthanc + demo chain
#
# Targets:
#   make up        start Orthanc (docker compose up -d)
#   make down      stop Orthanc
#   make send      send a synthetic study to the gateway receiver
#   make find      C-FIND report pull against Orthanc
#   make demo      full 15-minute demo chain (send → forward → report pull)
#   make test      run the full pytest suite
#   make lint      ruff + mypy

ORTHANC_DICOM_PORT := 4242
ORTHANC_AET        := ORTHANC
GATEWAY_PORT       := 11112
GATEWAY_AET        := GATEWAY
SPOOL_DIR          := .spool
STUDY_UID          := 1.2.840.10008.99.1

.PHONY: up down send find demo test lint

up:
	docker compose up -d

down:
	docker compose down

# Send a synthetic study to the gateway (receiver must be running).
send:
	uv run python -m demo.fake_modality --host 127.0.0.1 --port $(GATEWAY_PORT) \
		--aet $(GATEWAY_AET) --study-uid $(STUDY_UID)

# C-FIND the PACS for the study (report pull demo step).
find:
	uv run python -m demo.report_pull --host 127.0.0.1 --port $(ORTHANC_DICOM_PORT) \
		--aet $(ORTHANC_AET) --study-uid $(STUDY_UID)

# Full demo: gateway (receiver+forwarder) → hub, then report pull via C-FIND.
demo:
	uv run python -m demo.demo_chain --hub-host 127.0.0.1 --hub-port $(ORTHANC_DICOM_PORT) \
		--hub-aet $(ORTHANC_AET) --study-uid $(STUDY_UID) --spool-dir $(SPOOL_DIR) \
		--receiver-port $(GATEWAY_PORT) --report-host 127.0.0.1 \
		--report-port $(ORTHANC_DICOM_PORT) --report-aet $(ORTHANC_AET)

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run mypy .
