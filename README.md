# Linq AI Webhook (MVP)

A tiny Flask server that closes one conversational loop:

```
Customer texts your Linq number
        │
        ▼
Linq POSTs  message.received  ──►  /webhook/linq
        │
        ▼
OpenAI writes a short missed-call-recovery reply
        │
        ▼
Server sends it back through the Linq API  ──►  Customer
```

Files:

| File | What it is |
|------|------------|
| `app.py` | The whole app — receive, think, reply |
| `requirements.txt` | Dependencies |
| `render.yaml` | Render Blueprint (one-click deploy config) |
| `test_local.py` | Offline test — no API calls, no credits spent |
| `test_curl.sh` | Fires a fake inbound message at a running server |
| `.env.example` | Template for local env vars |

---

## What you need first

1. **Linq bearer token** + a **phone number** assigned to your account (from your Linq rep / sandbox).
2. **OpenAI API key** (platform.openai.com → API keys).
3. A **GitHub** account and a **Render** account (both free).

The three environment variables the app reads:

- `LINQ_API_TOKEN` — your Linq bearer token
- `LINQ_FROM_NUMBER` — your Linq number in E.164 format, e.g. `+18055551234`
- `OPENAI_API_KEY` — your OpenAI key

Optional: `BUSINESS_NAME` (used in the AI's persona) and `OPENAI_MODEL` (defaults to `gpt-4o-mini`).

---

## Step 1 — Test locally (optional but smart)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Offline test — proves the wiring, spends nothing:
python test_local.py
```

To run the real server locally:

```bash
cp .env.example .env        # then edit .env with your real keys
export $(grep -v '^#' .env | xargs)   # load them into your shell
python app.py               # serves on http://localhost:5000
```

In a second terminal, send it a fake inbound message:

```bash
./test_curl.sh
```

If your keys are real, you'll see the AI reply in the logs and an actual
text go out through Linq to `+15556667777` (change that number in
`test_curl.sh` to your own phone to see it land).

---

## Step 2 — Push to GitHub

From the project folder:

```bash
git init
git add .
git commit -m "Linq AI webhook MVP"
```

Create an empty repo on github.com (no README), then:

```bash
git remote add origin https://github.com/YOUR_USERNAME/linq-ai-webhook.git
git branch -M main
git push -u origin main
```

> `.env` is gitignored, so your secrets never get pushed. Good.

---

## Step 3 — Deploy on Render

You're already in Render, so:

1. **New +** → **Web Service**.
2. Connect GitHub and pick the `linq-ai-webhook` repo.
3. Render auto-detects Python. Confirm:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT`
4. Plan: **Free** is fine for a demo.
5. **Environment** → add the three variables:
   - `LINQ_API_TOKEN`
   - `LINQ_FROM_NUMBER`
   - `OPENAI_API_KEY`
   - (optional) `BUSINESS_NAME`
6. **Create Web Service**. Wait for the build to go green.

> Because `render.yaml` is in the repo, you can instead use **New + → Blueprint**
> and Render reads the config automatically — you'll just fill in the secret values.

When it's live you'll get a public URL like:

```
https://linq-ai-webhook.onrender.com
```

Quick checks in a browser:
- `https://linq-ai-webhook.onrender.com/` → "Linq AI webhook is running."
- `https://linq-ai-webhook.onrender.com/healthz` → shows which env vars are set.

---

## Step 4 — Point Linq at your URL

Your public webhook URL is:

```
https://linq-ai-webhook.onrender.com/webhook/linq
```

Register it as a **webhook subscription** in Linq for the `message.received`
event (via your Linq dashboard or the subscriptions API). Then text your Linq
number from your phone — you should get an AI reply back within a few seconds.

Test the live server without using your phone:

```bash
./test_curl.sh https://linq-ai-webhook.onrender.com
```

---

## Heads-up: Render free tier sleeps

Free web services spin down after ~15 minutes idle and take a few seconds to
wake on the next request. The very first text after a quiet period may be
slightly delayed. Upgrade to a paid instance (or ping `/healthz` on a schedule)
if you need it always-warm for a live demo.

---

## When you outgrow the MVP

- **Reply asynchronously.** Right now we call OpenAI + Linq inline before
  returning 200. That's fine for a demo. For volume, return 200 immediately and
  do the work in a background thread/queue so webhooks never time out.
- **Verify webhook signatures.** Linq signs webhooks; verify the signature so
  only genuine Linq events are processed. See Linq's Webhooks guide.
- **Dedupe with `event_id`.** Linq may retry; skip events whose `event_id`
  you've already handled.
- **Add conversation memory.** This MVP treats each message standalone. Store
  recent messages per chat to make replies context-aware.
- **Swap the model.** Set `OPENAI_MODEL`, or point `generate_reply()` at Claude
  instead — same shape, just a different client.
