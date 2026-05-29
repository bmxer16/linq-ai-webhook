"""
Linq <-> OpenAI conversational webhook — MVP with short-term memory

Loop:
  1. Linq POSTs an inbound `message.received` event to /webhook/linq
  2. We pull the customer's text out of the payload
  3. We ask OpenAI for a reply, WITH the recent conversation as context
  4. We send that reply back through the Linq API and remember it
"""

import os
import logging
from collections import deque
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify
from openai import OpenAI

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
LINQ_API_TOKEN = os.environ.get("LINQ_API_TOKEN", "")
LINQ_FROM_NUMBER = os.environ.get("LINQ_FROM_NUMBER", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "the business")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
MAX_MESSAGE_AGE_SECONDS = int(os.environ.get("MAX_MESSAGE_AGE_SECONDS", "300"))

# How many past turns (user + assistant messages) to keep per customer.
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "12"))

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
# Dedup (Linq retries) — in-memory, bounded
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
    while len(_seen_ids) > _seen_order.maxlen:
        _seen_ids.discard(_seen_order.popleft())
    return False


# ----------------------------------------------------------------------
# Conversation memory — per customer number, in-memory, bounded.
# (Resets on redeploy/restart. Fine for a demo; use Redis/DB for production.)
# ----------------------------------------------------------------------
_history = {}          # number -> deque[{"role","content"}]
_MAX_CUSTOMERS = 500


def get_history(number):
    if number not in _history and len(_history) >= _MAX_CUSTOMERS:
        _history.pop(next(iter(_history)))   # drop oldest customer
    return _history.setdefault(number, deque(maxlen=HISTORY_TURNS))


# ----------------------------------------------------------------------
# Parse inbound payloads (both 2026-02-03 and 2025-01-01 shapes)
# ----------------------------------------------------------------------
def extract_inbound(payload):
    if not isinstance(payload, dict):
        return None
    if payload.get("event_type") != "message.received":
        return None
    data = payload.get("data", {}) or {}

    if "sender_handle" in data or "direction" in data:
        if data.get("direction") == "outbound":
            return None
        sender = data.get("sender_handle", {}) or {}
        if sender.get("is_me"):
            return None
        customer = sender.get("handle")
        parts = data.get("parts", []) or []
    else:
        if data.get("is_from_me"):
            return None
        customer = data.get("from")
        parts = (data.get("message", {}) or {}).get("parts", []) or []

    if not customer or customer == LINQ_FROM_NUMBER:
        return None

    text = " ".join(p.get("value", "") for p in parts if p.get("type") == "text").strip()
    if not text:
        return None
    return customer, text


def message_too_old(payload):
    data = payload.get("data", {}) or {}
    sent_at = data.get("sent_at") or (data.get("message", {}) or {}).get("sent_at")
    if not sent_at:
        return False
    try:
        ts = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() > MAX_MESSAGE_AGE_SECONDS
    except Exception:
        return False


# ----------------------------------------------------------------------
# Generate a reply WITH conversation context
# ----------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You're texting a customer back on behalf of {biz}, a local business, after missing "
    "their call. This is an ongoing text conversation — the earlier messages are included, "
    "so read them and continue naturally.\n\n"
    "Rules:\n"
    "- Sound like a real, warm human texting. Casual, contractions, 1-2 short sentences.\n"
    "- Apologize for missing their call ONLY in your very first reply. After that, never "
    "re-introduce yourself, never re-apologize, and never repeat a greeting.\n"
    "- NEVER re-ask for anything they've already told you (their number, the problem, timing). "
    "Use what they've already said.\n"
    "- Always push the conversation one concrete step forward. If they want help now, tell them "
    "you're getting someone to call them right away. If you have their number and a time, confirm "
    "it and tell them what happens next. Don't stall by asking more questions than you need.\n"
    "- Never invent prices, names, or guarantees. Plain text only. An emoji is fine if it fits."
)


def generate_reply(customer_text, history):
    history.append({"role": "user", "content": customer_text})
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(biz=BUSINESS_NAME)}]
    messages.extend(history)
    resp = openai_client().chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        max_tokens=150,
        temperature=0.8,
    )
    reply = resp.choices[0].message.content.strip()
    history.append({"role": "assistant", "content": reply})
    return reply


# ----------------------------------------------------------------------
# Send via Linq
# ----------------------------------------------------------------------
def send_linq_message(to_number, text):
    url = f"{LINQ_BASE_URL}/chats"
    headers = {"Authorization": f"Bearer {LINQ_API_TOKEN}", "Content-Type": "application/json"}
    body = {"from": LINQ_FROM_NUMBER, "to": [to_number],
            "message": {"parts": [{"type": "text", "value": text}]}}
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
    return jsonify(ok=True, linq_token_set=bool(LINQ_API_TOKEN),
                   linq_from_set=bool(LINQ_FROM_NUMBER),
                   openai_key_set=bool(OPENAI_API_KEY), model=OPENAI_MODEL), 200


@app.get("/webhook/linq")
def webhook_probe():
    return "Linq webhook endpoint is live. Send a POST here.", 200


@app.post("/webhook/linq")
def webhook_linq():
    payload = request.get_json(silent=True) or {}
    event_id = payload.get("event_id")
    log.info("Inbound webhook: event_type=%s event_id=%s", payload.get("event_type"), event_id)
    try:
        if already_handled(event_id):
            log.info("Duplicate event %s — skipping", event_id)
            return jsonify(status="duplicate"), 200

        result = extract_inbound(payload)
        if not result:
            return jsonify(status="ignored"), 200

        if message_too_old(payload):
            log.info("Message too old — skipping to avoid backlog flood")
            return jsonify(status="too_old"), 200

        customer, text = result
        log.info("Customer %s said: %s", customer, text)

        reply = generate_reply(text, get_history(customer))
        log.info("AI reply: %s", reply)

        send_linq_message(customer, reply)
        return jsonify(status="replied", reply=reply), 200

    except Exception as e:  # noqa: BLE001
        log.exception("Error handling webhook: %s", e)
        return jsonify(status="error", detail=str(e)), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
