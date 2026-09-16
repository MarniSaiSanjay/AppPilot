"""AppPilot Email Relay API."""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import uuid

from azure.communication.email import EmailClient
from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential
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


def _validate_api_key_separation(
    email_api_key: str, telemetry_api_key: str
) -> None:
    if telemetry_api_key and secrets.compare_digest(
        email_api_key, telemetry_api_key
    ):
        raise RuntimeError(
            "APPPILOT_API_KEY and APPPILOT_TELEMETRY_API_KEY "
            "must be different"
        )


_SENDER = _required("ACS_SENDER_ADDRESS")
_RECIPIENTS = _parse_recipients(os.environ.get("APPPILOT_EMAIL_TO", ""))
_API_KEY = _required("APPPILOT_API_KEY")
_TELEMETRY_API_KEY = os.environ.get(
    "APPPILOT_TELEMETRY_API_KEY", ""
).strip()
_validate_api_key_separation(_API_KEY, _TELEMETRY_API_KEY)
_client = EmailClient.from_connection_string(_required("ACS_CONNECTION_STRING"))

_TELEMETRY_ENDPOINT = os.environ.get("TELEMETRY_TABLE_ENDPOINT", "").strip()
_TELEMETRY_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "").strip() or None
_TELEMETRY_CREDENTIAL = (
    DefaultAzureCredential(managed_identity_client_id=_TELEMETRY_CLIENT_ID)
    if _TELEMETRY_ENDPOINT
    else None
)
_TELEMETRY_TABLE = "SuiteRuns"
_HOST_OS_VALUES = {"macOS", "Windows"}
_TARGET_PLATFORM_VALUES = {"Android", "iOS"}

app = FastAPI(title="AppPilot Email Relay", version="1.0.0")


class ReportIn(BaseModel):
    subject: str = Field(
        default="AppPilot Deeplink Suite Report", max_length=300
    )
    body: str = Field(..., min_length=1, max_length=200_000)
    to: str | None = Field(default=None, max_length=320)
    html: str | None = Field(default=None, max_length=400_000)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/send-report")
def send_report(payload: ReportIn, x_api_key: str = Header(default="")) -> dict:
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
        "senderAddress": _SENDER,
        "content": content,
        "recipients": {"to": [{"address": a} for a in recipients]},
    }
    try:
        poller = _client.begin_send(message)
        result = poller.result()
        message_id = result.get("id") if isinstance(result, dict) else None
    except Exception:
        _log.error("ACS send failed")
        raise HTTPException(status_code=502, detail="email send failed")

    return {"status": "sent", "id": message_id}


class TelemetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: str = Field(..., min_length=1, max_length=100)
    test_cases: int = Field(..., ge=0, le=1_000_000)
    host_os: str
    target_platform: str


@app.post("/telemetry")
def telemetry(payload: TelemetryIn, x_api_key: str = Header(default="")) -> dict:
    if not _TELEMETRY_API_KEY:
        raise HTTPException(
            status_code=500, detail="telemetry authentication not configured"
        )
    if not hmac.compare_digest(
        x_api_key.encode("utf-8", "replace"),
        _TELEMETRY_API_KEY.encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="unauthorized")
    if (
        payload.host_os not in _HOST_OS_VALUES
        or payload.target_platform not in _TARGET_PLATFORM_VALUES
    ):
        raise HTTPException(status_code=422, detail="invalid host_os/target_platform")
    if not _TELEMETRY_ENDPOINT or _TELEMETRY_CREDENTIAL is None:
        raise HTTPException(status_code=500, detail="telemetry storage not configured")

    entity = {
        "PartitionKey": payload.suite,
        "RowKey": uuid.uuid4().hex,
        "suite": payload.suite,
        "test_cases": payload.test_cases,
        "host_os": payload.host_os,
        "target_platform": payload.target_platform,
    }
    try:
        with TableClient(
            endpoint=_TELEMETRY_ENDPOINT,
            table_name=_TELEMETRY_TABLE,
            credential=_TELEMETRY_CREDENTIAL,
        ) as table:
            table.create_entity(entity)
    except Exception:
        _log.error("telemetry insert failed")
        raise HTTPException(status_code=502, detail="telemetry store failed")

    return {"status": "recorded"}
