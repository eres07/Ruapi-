# ruapi v1 — Hindi + English AI API with credits

Simple first version. Free and cheap. Improve later.

General-purpose API for anyone. Hindi + English. 1 credit per chat call.
New keys get **100 free credits**.

## Endpoints

| Method | Path | Auth | What it does |
|---|---|---|---|
| GET | `/health` | — | `{"status": "ok", ...}` |
| POST | `/v1/keys` | — | Create a demo key with 100 credits |
| GET | `/v1/credits` | Bearer key | Credits remaining |
| POST | `/v1/chat` | Bearer key | Chat in Hindi/English, costs 1 credit |
| POST | `/v1/chat/completions` | Bearer key | OpenAI-compatible alias for `/v1/chat` |

Out of credits → HTTP `402`. Missing/invalid key → HTTP `401`.

## Run locally

```bash
cd ruapi
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000 for the docs/playground page
```

Credits are stored in a tiny SQLite DB (`ruapi.db` locally, `$DATA_DIR/ruapi.db` on Embarko).

## curl examples

Replace `KEY` with your key, and `http://localhost:8000` with the live URL when deployed.

```bash
# Health
curl http://localhost:8000/health

# Get a free key (100 credits)
curl -X POST http://localhost:8000/v1/keys

# Chat in Hindi
curl -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer KEY" -H "Content-Type: application/json" \
  -d '{"message": "नमस्ते, AI kya hai samjhao", "language": "hi"}'

# Chat in English
curl -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer KEY" -H "Content-Type: application/json" \
  -d '{"message": "translate hello to Hindi", "language": "en"}'

# Credits left
curl http://localhost:8000/v1/credits -H "Authorization: Bearer KEY"
```

## How the chat works (v1)

No external API key needed. Provider order:

1. `GROQ_API_KEY` (if set) → Groq `llama-3.1-8b-instant`
2. `SARVAM_API_KEY` (if set) → Sarvam `sarvam-m`
3. **Pollinations** (no key, no signup, free) → `gpt-oss-20b`.
   Verified live 2026-11-21 — real Hindi/Hinglish answers.
   Best effort, no SLA, rate-limited; set `POLLINATIONS_ENABLED=0`
   to skip it. Prompts go to a third party, so don't send personal data.
4. Built-in **demo brain** (rule-based Hindi/English replies for
   greetings, translation of common phrases, writing help, explaining
   things, and simple maths) — always there as the final fallback.

So out of the box, with no keys at all, chat answers real questions
via Pollinations and falls back to the demo brain if that is slow or
rate-limited.

### Plug in a real model later (no API rewrite)

Set an env var on the host — never hardcode a key:

```bash
GROQ_API_KEY=...    # tries Groq first
SARVAM_API_KEY=...  # then Sarvam
```

The API shape stays the same; only the model behind it changes.
Upgrade path: free Groq key → Groq GPT-OSS 20B (same model family) →
paid upstream only after paying users exist.

## Deploy (Embarko, free, no card)

```bash
cd ~/workspace/ruapi
tar -czf /tmp/ruapi.tar.gz --exclude=.venv --exclude=__pycache__ --exclude='*.db' --exclude=.git --exclude=TEST_PACK.md -C ~/workspace/ruapi .
curl -sS -X POST 'https://ship.embarko.ai/apps' \
  -H 'X-App-Name: ruapi' -H 'X-Agent-Name: Muse' \
  -H 'X-App-Id: cmwqarwz8008sek4ijqz0oqhj' \
  -F 'source=@/tmp/ruapi.tar.gz'
curl -sS 'https://ship.embarko.ai/apps/ruapi/status'
# live at https://ruapi.embarko.app (temporary 24h unless claimed)
# claim: https://embarko.ai/claim?app=ruapi&code=OhsWOBX7vkHGckcV-bwegX1q-gw_Vxov7vvGePFoIio
```

Embarko detects Python from `requirements.txt` + `main.py`, which starts
uvicorn on `$PORT` / `0.0.0.0`. `Procfile` runs `/app/.venv/bin/python
/app/main.py` (absolute paths — the runtime cwd is not `/app`).
`.python-version` pins Python 3.12 (the version this was tested on).

### Deploy status, 21 Nov 2026 — API NOT live yet, docs ARE live

- Docs/playground page: **live** at https://ruapi-docs.embarko.app
  (app id `cmwqbv9h800taek4i2980hhlp`, expires 2026-11-22T11:31:39Z unless claimed;
  claim: https://embarko.ai/claim?app=ruapi-docs&code=Inuaws7n2SYnFoblUYktyyM56uZxIQdNWci_q5ymLt0)
- API at https://ruapi.embarko.app: **down**. 5 deploys, all built fine but
  the container exits 3x in a row right after starting (exit 1 with no
  Procfile, exit 2 with Procfile, ~35–100 ms after start, no output
  captured). A trivial plain-FastAPI test app (`ruapi-min`) crashes the
  exact same way, so this looks like an Embarko Python-runtime problem,
  not a bug in this code — the same code passes every local test.
  Next step: report it via the deploy feedback link
  (https://ship.embarko.ai/apps/ruapi/customer-query) or retry the deploy
  later with the command above; the code itself needs no change.
- The API is fully tested locally — run it with the commands at the top
  of this README and it works at http://localhost:8000.
