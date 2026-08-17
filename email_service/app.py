"""AppPilot Email Relay - minimal server-side email API.

Isolated micro-service that accepts an already-generated AppPilot test report
and sends it from ONE fixed, service-owned sender via Azure Communication
Services (ACS) Email.

It also exposes a tiny best-effort telemetry endpoint (`/telemetry`) that records
one row per real suite run in Azure Table Storage (four non-identifying fields
only: suite, test_cases, host_os, target_platform).

Security model:
  * The caller can NEVER choose the From address - it is fixed server-side via
    environment configuration. The recipient may be supplied per request (and is
    validated); when omitted the configured default recipient is used.
  * Privileged ACS and Storage credentials live only on the server (Container App
    secrets), never in the distributed AppPilot CLI.
  * Callers authenticate with a scoped API key (constant-time compared).
  * Secrets are never logged and never returned in responses.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import uuid

from azure.communication.email import EmailClient
from azure.data.tables import TableClient
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("apppilot-email-relay")

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _parse_recipients(raw: str) -> list[str]:
    return [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]


# Server-side configuration. The sender and API key are mandatory; the default
# recipient is optional (used only when a request omits `to`).
_SENDER = _required("ACS_SENDER_ADDRESS")
_RECIPIENTS = _parse_recipients(os.environ.get("APPPILOT_EMAIL_TO", ""))
_API_KEY = _required("APPPILOT_API_KEY")
_client = EmailClient.from_connection_string(_required("ACS_CONNECTION_STRING"))

# Telemetry storage is optional/isolated: a missing connection string must never
# break email. When unset, /telemetry returns 500 while /send-report keeps working.
_TELEMETRY_CONN = os.environ.get("TELEMETRY_TABLE_CONNECTION_STRING", "").strip()
_TELEMETRY_TABLE = "SuiteRuns"
_HOST_OS_VALUES = {"macOS", "Windows"}
_TARGET_PLATFORM_VALUES = {"Android", "iOS"}

app = FastAPI(title="AppPilot Email Relay", version="1.0.0")


class ReportIn(BaseModel):
    subject: str = Field(
        default="AppPilot Deeplink Suite Report", max_length=300
    )
    body: str = Field(..., min_length=1, max_length=200_000)
    # Optional caller-chosen recipient. The SENDER stays fixed server-side; only
    # the destination may be supplied. Omitted -> the configured default.
    to: str | None = Field(default=None, max_length=320)
    # Optional HTML rendering of the report. When present it is sent as the
    # email's HTML content (with `body` as the plain-text fallback).
    html: str | None = Field(default=None, max_length=400_000)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/send-report")
def send_report(payload: ReportIn, x_api_key: str = Header(default="")) -> dict:
    # Compare bytes: a non-ASCII header would make str compare_digest raise
    # (TypeError -> 500) for an unauthenticated caller. Bytes keeps it 401.
    if not hmac.compare_digest(
        x_api_key.encode("utf-8", "replace"), _API_KEY.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="unauthorized")

    if payload.to is not None:
        recipient = payload.to.strip()
        if not _EMAIL_RE.match(recipient):
            raise HTTPException(status_code=422, detail="invalid recipient address")
        recipients = [recipient]
    else:
        recipients = _RECIPIENTS
    if not recipients:
        raise HTTPException(status_code=500, detail="no recipients configured")

    content = {"subject": payload.subject, "plainText": payload.body}
    if payload.html:
        content["html"] = payload.html
    message = {
        # Sender is fixed server-side; the request can only choose the recipient.
        "senderAddress": _SENDER,
        "content": content,
        "recipients": {"to": [{"address": a} for a in recipients]},
    }
    try:
        poller = _client.begin_send(message)
        result = poller.result()
        message_id = result.get("id") if isinstance(result, dict) else None
    except Exception:  # never leak provider/credential details to the caller
        _log.exception("ACS send failed")
        raise HTTPException(status_code=502, detail="email send failed")

    return {"status": "sent", "id": message_id}


class TelemetryIn(BaseModel):
    # extra=forbid rejects any field beyond the four approved ones (422), so the
    # client can never smuggle extra/identifying data into telemetry.
    model_config = ConfigDict(extra="forbid")

    suite: str = Field(..., min_length=1, max_length=100)
    test_cases: int = Field(..., ge=0, le=1_000_000)
    host_os: str
    target_platform: str


@app.post("/telemetry")
def telemetry(payload: TelemetryIn, x_api_key: str = Header(default="")) -> dict:
    if not hmac.compare_digest(
        x_api_key.encode("utf-8", "replace"), _API_KEY.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="unauthorized")
    if (
        payload.host_os not in _HOST_OS_VALUES
        or payload.target_platform not in _TARGET_PLATFORM_VALUES
    ):
        raise HTTPException(status_code=422, detail="invalid host_os/target_platform")
    if not _TELEMETRY_CONN:
        raise HTTPException(status_code=500, detail="telemetry storage not configured")

    # PartitionKey/RowKey are Azure Table mechanics, not telemetry data: partition
    # by suite for cheap aggregation; a random RowKey keeps each run a distinct
    # row (identical runs must never overwrite). Only the four fields are stored.
    entity = {
        "PartitionKey": payload.suite,
        "RowKey": uuid.uuid4().hex,
        "suite": payload.suite,
        "test_cases": payload.test_cases,
        "host_os": payload.host_os,
        "target_platform": payload.target_platform,
    }
    try:
        with TableClient.from_connection_string(
            _TELEMETRY_CONN, table_name=_TELEMETRY_TABLE
        ) as table:
            table.create_entity(entity)
    except Exception:  # never leak storage/credential details to the caller
        _log.exception("telemetry insert failed")
        raise HTTPException(status_code=502, detail="telemetry store failed")

    return {"status": "recorded"}
