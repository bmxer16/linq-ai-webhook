"""
Linq <-> OpenAI conversational webhook — MVP
Memory + per-business knowledge (reads the company's website).
"""

import os
import re
import time
import random
import logging
import threading
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
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "12"))

# Business knowledge: set BUSINESS_URL to the company's website (the bot reads it),
# or paste BUSINESS_PROFILE directly to skip scraping. BUSINESS_PROFILE wins if both set.
BUSINESS_URL = os.environ.get("BUSINESS_URL", "")
BUSINESS_PROFILE = os.environ.get("BUSINESS_PROFILE", "")
BUSINESS_NICHE = os.environ.get("BUSINESS_NICHE", "")        # e.g. "plumbing", "HVAC"
AI_DISCLOSURE = os.environ.get("AI_DISCLOSURE", "false").lower() in ("1", "true", "yes")

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
# Business knowledge: read the website once, summarize, cache.
# ----------------------------------------------------------------------
_biz_context = None


def _html_to_text(html):
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"&[a-z]+;", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def business_context():
    """Return a short profile of the business, or '' if none configured."""
    global _biz_context
    if _biz_context is not None:
        return _biz_context

    if BUSINESS_PROFILE.strip():
        _biz_context = BUSINESS_PROFILE.strip()
        return _biz_context

    if BUSINESS_URL.strip():
        try:
            r = requests.get(BUSINESS_URL, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0 (LinqAI)"})
            text = _html_to_text(r.text)[:6000]
            summary = openai_client().chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content":
                        "Summarize this business's website into a tight profile a phone/text "
                        "agent can use: what they do, services offered, service area, hours if "
                        "stated, how to book or contact them, and anything notable. 120 words max. "
                        "Only use what's actually on the page — never invent details."},
                    {"role": "user", "content": text},
                ],
                max_tokens=300, temperature=0.2,
            ).choices[0].message.content.strip()
            _biz_context = summary
            log.info("Loaded business profile from %s (%d chars)", BUSINESS_URL, len(summary))
            return _biz_context
        except Exception as e:  # noqa: BLE001
            log.warning("Could not load BUSINESS_URL (%s): %s", BUSINESS_URL, e)

    _biz_context = ""
    return _biz_context


# ----------------------------------------------------------------------
# Dedup (Linq retries)
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
# Conversation memory (per customer)
# ----------------------------------------------------------------------
_history = {}
_MAX_CUSTOMERS = 500


def get_history(number):
    if number not in _history and len(_history) >= _MAX_CUSTOMERS:
        _history.pop(next(iter(_history)))
    return _history.setdefault(number, deque(maxlen=HISTORY_TURNS))


# ----------------------------------------------------------------------
# Parse inbound payloads
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
# Generate reply (memory + business knowledge)
# ----------------------------------------------------------------------
_BASE_RULES = """You are the text-back assistant for {biz}{niche_clause}. A customer either called and it was missed, or texted this number directly, and you're handling the conversation.

{disclosure}YOUR #1 JOB: capture the lead. Every reply should move toward getting (1) their name, (2) what they need, and (3) the best number to reach them — then confirming a clear next step. You are warm, but you do not let a lead quietly slip away.

HOW TO SOUND — match the human on the other end:
- Like a real, caring teammate texting. 1-2 short messages. Contractions. An emoji only when it fits the mood.
- READ THEIR EMOTION and mirror it. Stressed or urgent (a leak, no heat or AC, an emergency) -> lead with genuine empathy and real urgency. Excited or casual -> be upbeat and easygoing. Skeptical, annoyed, or hesitant -> stay calm, reassuring, and low-pressure. Sound like you actually feel it with them.
- Professional warmth and a little happiness — glad to help, never robotic, never cold.
- Apologize for the missed call only ONCE, in your first reply. Never re-greet after that.

CAPTURING THE LEAD — warm but firm:
- Don't let them drift. If they go quiet or hesitate, offer an easy, specific next step.
- If they say no or "just looking" — that's okay, don't push hard. But still warmly try to get a name and number so the team can check in later, and leave the door wide open. A soft no is not the end of the conversation.
- Once you have their name, their need, and a number, confirm someone will reach out and what happens next.

HARD RULES:
- NEVER give prices, quotes, or cost estimates. If asked, say a team member will confirm pricing — you can't quote it.
- Speak generally and confidently to common {niche_word} problems and reassure them you handle it, but never invent availability, names, guarantees, or specifics you don't actually know.
- Never re-ask for info they already gave."""


def system_prompt():
    niche_clause = f", a {BUSINESS_NICHE} business" if BUSINESS_NICHE else ""
    niche_word = BUSINESS_NICHE if BUSINESS_NICHE else "service"
    disclosure = ""
    if AI_DISCLOSURE:
        disclosure = (
            "Early on, lightly and confidently let them know you're an AI assistant helping the team "
            "respond instantly, and they're welcome to try you out — keep it friendly, not apologetic. "
        )
    base = _BASE_RULES.format(biz=BUSINESS_NAME, niche_clause=niche_clause,
                              niche_word=niche_word, disclosure=disclosure)
    biz = business_context()
    if biz:
        base += (f"\n\nWhat you know about {BUSINESS_NAME} (use this to answer accurately; "
                 f"don't contradict it or invent beyond it):\n{biz}")
    return base


def generate_reply(customer_text, history):
    history.append({"role": "user", "content": customer_text})
    messages = [{"role": "system", "content": system_prompt()}]
    messages.extend(history)
    resp = openai_client().chat.completions.create(
        model=OPENAI_MODEL, messages=messages, max_tokens=150, temperature=0.8,
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
# Human-like reply timing: reply after a natural pause (read + type),
# done in a background thread so the webhook still returns 200 instantly.
# ----------------------------------------------------------------------
def human_delay_seconds(reply):
    words = max(1, len(reply.split()))
    read = random.uniform(2.0, 4.0)              # "saw it and read it"
    typing = min(words * 0.35, 10.0)             # "typed it out"
    return read + typing


def handle_and_reply(customer, text):
    try:
        t0 = time.monotonic()
        reply = generate_reply(text, get_history(customer))
        log.info("AI reply (queued): %s", reply)
        remaining = human_delay_seconds(reply) - (time.monotonic() - t0)
        if remaining > 0:
            time.sleep(remaining)
        send_linq_message(customer, reply)
    except Exception as e:  # noqa: BLE001
        log.exception("Background reply failed: %s", e)


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
                   openai_key_set=bool(OPENAI_API_KEY), model=OPENAI_MODEL,
                   business_knowledge=bool(business_context())), 200


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
            log.info("Message too old — skipping")
            return jsonify(status="too_old"), 200
        customer, text = result
        log.info("Customer %s said: %s", customer, text)
        # Ack Linq immediately; reply after a human-like pause in the background.
        threading.Thread(target=handle_and_reply, args=(customer, text), daemon=True).start()
        return jsonify(status="accepted"), 200
    except Exception as e:  # noqa: BLE001
        log.exception("Error handling webhook: %s", e)
        return jsonify(status="error", detail=str(e)), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
