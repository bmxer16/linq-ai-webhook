"""
Linq <-> OpenAI conversational webhook — MVP

One loop:
  1. Linq POSTs an inbound `message.received` event to /webhook/linq
  2. We pull the customer's text out of the payload
  3. We ask OpenAI for a short missed-call-recovery reply
  4. We send that reply back to the customer through the Linq API
"""

import os
import logging
from collections import deque
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify
from openai import OpenAI

# ----------------------------------------------------------------------
# Config (all from environment variables — never hard-code secrets)
# ----------------------------------------------------------------------
LINQ_API_TOKEN = os.environ.get("LINQ_API_TOKEN", "")
LINQ_FROM_NUMBER = os.environ.get("LINQ_FROM_NUMBER", "")   # your Linq number, E.164: +18055551234
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "the business")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# Ignore messages older than this many seconds. Stops a backlog of retried/
# queued webhooks (e.g. after the server was asleep) from triggering a flood
# of replies to stale messages.
MAX_MESSAGE_AGE_SECONDS = int(os.environ.get("MAX_MESSAGE_AGE_SECONDS", "300"))

LINQ_BASE_URL = "https://api.linqapp.com/api/partner/v3"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("linq-ai")

app = Flask(__name__)

_openai_client = None


def openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


# ----------------------------------------------------------------------
# Dedup: remember event IDs we've already handled so Linq retries don't
# cause duplicate replies. In-memory + bounded (fine for a single-worker MVP).
# ----------------------------------------------------------------------
_seen_ids = set()
_seen_order = deque(maxlen=2000)


def already_handled(event_id):
    if not event_id:
        return False
    if event_id in _seen_ids:
        return True
    _seen_ids.add(event_id)
    _seen_order.append(event_id)
    # Keep the set bounded to match the deque
    while len(_seen_ids) > _seen_order.maxlen:
        _seen_ids.discard(_seen_order.popleft())
    return False


# ----------------------------------------------------------------------
# Pull the customer + text out of a Linq webhook payload
# Handles both payload versions: 2026-02-03 and 2025-01-01.
# ----------------------------------------------------------------------
def extract_inbound(payload):
    if not isinstance(payload, dict):
        return None
    if payload.get("event_type") != "message.received":
        return None

    data = payload.get("data", {}) or {}

    if "sender_handle" in data or "direction" in data:          # 2026-02-03
        if data.get("direction") == "outbound":
            return None
        sender = data.get("sender_handle", {}) or {}
        if sender.get("is_me"):
            return None
        customer = sender.get("handle")
        parts = data.get("parts", []) or []
    else:                                                        # 2025-01-01
        if data.get("is_from_me"):
            return None
        customer = data.get("from")
        parts = (data.get("message", {}) or {}).get("parts", []) or []

    if not customer or customer == LINQ_FROM_NUMBER:
        return None

    text = " ".join(
        p.get("value", "") for p in parts if p.get("type") == "text"
    ).strip()
    if not text:
        return None

    return customer, text


def message_too_old(payload):
    """True if the message's sent_at is older than MAX_MESSAGE_AGE_SECONDS."""
    data = payload.get("data", {}) or {}
    sent_at = data.get("sent_at") or (data.get("message", {}) or {}).get("sent_at")
    if not sent_at:
        return False  # can't tell — don't block
    try:
        ts = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > MAX_MESSAGE_AGE_SECONDS
    except Exception:
        return False


# ----------------------------------------------------------------------
# Generate a short, warm, human reply
# ----------------------------------------------------------------------
def generate_reply(customer_text):
    system_prompt = (
        f"You're texting a customer back on behalf of {BUSINESS_NAME}, a local business. "
        f"The customer called, you missed it, and now you're following up by text. "
        f"Sound like a real, warm human dashing off a quick text — NOT a corporate auto-reply.\n\n"
        f"Rules:\n"
        f"- 1-2 short sentences. Casual and friendly, use contractions.\n"
        f"- React to what they actually said — acknowledge their specific situation in a human way.\n"
        f"- Then move it forward concretely: ask for the best number and a good time to reach them, "
        f"or offer to get them on the schedule. Never just say 'someone will reach out.'\n"
        f"- Never invent prices, availability, or guarantees. If unsure, offer to have someone confirm.\n"
        f"- Plain text only. No markdown. An emoji is fine only if it feels natural."
    )
    resp = openai_client().chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": customer_text},
        ],
        max_tokens=150,
        temperature=0.8,
    )
    return resp.choices[0].message.content.strip()


# ----------------------------------------------------------------------
# Send the reply back through Linq
# ----------------------------------------------------------------------
def send_linq_message(to_number, text):
    url = f"{LINQ_BASE_URL}/chats"
    headers = {
        "Authorization": f"Bearer {LINQ_API_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "from": LINQ_FROM_NUMBER,
        "to": [to_number],
        "message": {"parts": [{"type": "text", "value": text}]},
    }
    r = requests.post(url, headers=headers, json=body, timeout=20)
    log.info("Linq send -> %s | status=%s | body=%s", to_number, r.status_code, r.text[:300])
    r.raise_for_status()
    return r.json()


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.get("/")
def home():
    return "Linq AI webhook is running. POST events to /webhook/linq", 200


@app.get("/healthz")
def healthz():
    return jsonify(
        ok=True,
        linq_token_set=bool(LINQ_API_TOKEN),
        linq_from_set=bool(LINQ_FROM_NUMBER),
        openai_key_set=bool(OPENAI_API_KEY),
        model=OPENAI_MODEL,
    ), 200


@app.get("/webhook/linq")
def webhook_probe():
    return "Linq webhook endpoint is live. Send a POST here.", 200


@app.post("/webhook/linq")
def webhook_linq():
    payload = request.get_json(silent=True) or {}
    event_id = payload.get("event_id")
    log.info("Inbound webhook: event_type=%s event_id=%s",
             payload.get("event_type"), event_id)

    try:
        # 1) Skip retries / duplicates of an event we've already handled
        if already_handled(event_id):
            log.info("Duplicate event %s — skipping", event_id)
            return jsonify(status="duplicate"), 200

        # 2) Only real inbound customer texts
        result = extract_inbound(payload)
        if not result:
            return jsonify(status="ignored"), 200

        # 3) Don't reply to a stale backlog
        if message_too_old(payload):
            log.info("Message too old — skipping to avoid backlog flood")
            return jsonify(status="too_old"), 200

        customer, text = result
        log.info("Customer %s said: %s", customer, text)

        reply = generate_reply(text)
        log.info("AI reply: %s", reply)

        send_linq_message(customer, reply)
        return jsonify(status="replied", reply=reply), 200

    except Exception as e:  # noqa: BLE001 — never let the webhook 500
        log.exception("Error handling webhook: %s", e)
        return jsonify(status="error", detail=str(e)), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
