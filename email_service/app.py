"""AppPilot Email Relay - minimal server-side email API.

Isolated micro-service that accepts an already-generated AppPilot test report
and sends it from ONE fixed, service-owned sender via Azure Communication
Services (ACS) Email.

Security model:
  * The caller can NEVER choose the From address - it is fixed server-side via
    environment configuration. The recipient may be supplied per request (and is
    validated); when omitted the configured default recipient is used.
  * Privileged ACS credentials live only on the server (Container App secret),
    never in the distributed AppPilot CLI.
  * Callers authenticate with a scoped API key (constant-time compared).
  * Secrets are never logged and never returned in responses.
"""

from __future__ import annotations

import hmac
import logging
import os
import re

from azure.communication.email import EmailClient
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

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
