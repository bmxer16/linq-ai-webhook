"""
Offline test. Validates the webhook parsing + routing WITHOUT calling
OpenAI or Linq (so it costs nothing and needs no real keys).

Run:  python test_local.py
"""
import os
os.environ.setdefault("LINQ_FROM_NUMBER", "+12025551234")
os.environ.setdefault("LINQ_API_TOKEN", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

import app as a

new_inbound = {
    "event_type": "message.received",
    "data": {
        "direction": "inbound",
        "sender_handle": {"handle": "+15556667777", "is_me": False},
        "parts": [{"type": "text", "value": "Hey, I called earlier about a quote"}],
    },
}
old_inbound = {
    "event_type": "message.received",
    "data": {
        "from": "+15556667777",
        "is_from_me": False,
        "message": {"parts": [{"type": "text", "value": "Are you open Saturday?"}]},
    },
}
my_own = {"event_type": "message.sent",
          "data": {"direction": "outbound",
                   "sender_handle": {"handle": "+12025551234", "is_me": True},
                   "parts": [{"type": "text", "value": "hi"}]}}
delivered = {"event_type": "message.delivered", "data": {"chat_id": "abc"}}

assert a.extract_inbound(new_inbound) == ("+15556667777", "Hey, I called earlier about a quote")
assert a.extract_inbound(old_inbound) == ("+15556667777", "Are you open Saturday?")
assert a.extract_inbound(my_own) is None          # never reply to ourselves
assert a.extract_inbound(delivered) is None        # ignore receipts
print("Parser OK ✅")

# Mock the two outbound calls so no credits are spent
a.generate_reply = lambda t: "Sorry we missed you! Want me to set up a callback?"
captured = {}
a.send_linq_message = lambda to, text: captured.update(to=to, text=text) or {"ok": True}

client = a.app.test_client()
r = client.post("/webhook/linq", json=new_inbound)
assert r.status_code == 200 and r.get_json()["status"] == "replied"
assert captured["to"] == "+15556667777"
print("Route OK ✅  (would send to", captured["to"], "->", repr(captured["text"]), ")")
print("\nAll tests passed.")
