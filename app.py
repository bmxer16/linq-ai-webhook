"""
Linq <-> OpenAI conversational webhook — MVP

One loop:
  1. Linq POSTs an inbound `message.received` event to /webhook/linq
  2. We pull the customer's text out of the payload
  3. We ask OpenAI for a short missed-call-recovery reply
  4. We send that reply back to the customer through the Linq API

Keep it simple. Everything runs inline so the logs read top-to-bottom
when you're testing. See README.md for the production upgrade notes.
"""

import os
import logging

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

LINQ_BASE_URL = "https://api.linqapp.com/api/partner/v3"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("linq-ai")

app = Flask(__name__)

# Lazily create the OpenAI client so the app still boots (and /healthz works)
# even if the key isn't set yet.
_openai_client = None


def openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


# ----------------------------------------------------------------------
# Step 2: pull the customer + text out of a Linq webhook payload
# ----------------------------------------------------------------------
def extract_inbound(payload):
    """
    Return (customer_number, text) for a genuine inbound customer text,
    or None for anything we should NOT reply to (our own messages,
    delivery receipts, reactions, typing indicators, etc.).

    Handles both Linq webhook payload versions: 2026-02-03 and 2025-01-01.
    """
    if not isinstance(payload, dict):
        return None

    if payload.get("event_type") != "message.received":
        return None  # ignore message.sent / delivered / read / reactions / etc.

    data = payload.get("data", {}) or {}

    # --- 2026-02-03 shape ---------------------------------------------
    # data.direction == "inbound", data.sender_handle.handle, data.parts
    if "sender_handle" in data or "direction" in data:
        if data.get("direction") == "outbound":
            return None
        sender = data.get("sender_handle", {}) or {}
        if sender.get("is_me"):
            return None
        customer = sender.get("handle")
        parts = data.get("parts", []) or []

    # --- 2025-01-01 shape ---------------------------------------------
    # data.is_from_me == false, data.from, data.message.parts
    else:
        if data.get("is_from_me"):
            return None
        customer = data.get("from")
        parts = (data.get("message", {}) or {}).get("parts", []) or []

    # Belt-and-suspenders: never reply to our own number (no self-loops)
    if not customer or customer == LINQ_FROM_NUMBER:
        return None

    # Join all text parts; ignore media-only messages for this MVP
    text = " ".join(
        p.get("value", "") for p in parts if p.get("type") == "text"
    ).strip()
    if not text:
        return None

    return customer, text


# ----------------------------------------------------------------------
# Step 3: generate a short conversational reply
# ----------------------------------------------------------------------
def generate_reply(customer_text):
    system_prompt = (
        f"You are a friendly missed-call recovery assistant texting on behalf of "
        f"{BUSINESS_NAME}. A customer called and we missed it, so we're following up "
        f"by text. Be warm, brief, and human. Keep replies to 1-2 short sentences. "
        f"Acknowledge them, answer simply, and gently move toward booking or a callback. "
        f"Never invent prices, hours, or promises you can't keep — if you don't know, "
        f"offer to have someone follow up. Plain text only, no markdown."
    )
    resp = openai_client().chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": customer_text},
        ],
        max_tokens=150,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()


# ----------------------------------------------------------------------
# Step 4: send the reply back through Linq
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
    # Quick visibility into which env vars are wired up (booleans only — no secret leakage)
    return jsonify(
        ok=True,
        linq_token_set=bool(LINQ_API_TOKEN),
        linq_from_set=bool(LINQ_FROM_NUMBER),
        openai_key_set=bool(OPENAI_API_KEY),
        model=OPENAI_MODEL,
    ), 200


@app.get("/webhook/linq")
def webhook_probe():
    # So you can open the URL in a browser and confirm it's live
    return "Linq webhook endpoint is live. Send a POST here.", 200


@app.post("/webhook/linq")
def webhook_linq():
    payload = request.get_json(silent=True) or {}
    log.info("Inbound webhook: event_type=%s", payload.get("event_type"))

    # Always 200 the webhook fast so Linq doesn't retry-storm us,
    # even if our own processing hits a snag.
    try:
        result = extract_inbound(payload)
        if not result:
            return jsonify(status="ignored"), 200

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
    # Local dev only. Render uses gunicorn (see render.yaml / start command).
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
