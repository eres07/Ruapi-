"""
ruapi v1 — a simple, free, API-first Indian AI (Hindi + English).

MVP endpoints:
  GET  /health      — no auth
  POST /v1/keys     — create a demo API key with 100 free credits
  GET  /v1/credits  — remaining credits for your key (Bearer auth)
  POST /v1/chat     — 1 credit per call (Bearer auth)

Chat works out of the box with NO external API keys. Provider order:
  1. GROQ_API_KEY (if set) -> Groq llama-3.1-8b-instant
  2. SARVAM_API_KEY (if set) -> Sarvam sarvam-m
  3. Pollinations (no key, no signup, free) -> gpt-oss-20b, unless
     POLLINATIONS_ENABLED=0. Best effort, no SLA, rate-limited; prompts
     go to a third party, so do not send personal data through it.
  4. Built-in demo brain (rule-based Hindi/English replies) — always
     available as the final fallback, so the API never goes down because
     of a provider. No keys are hardcoded anywhere in this file.

To plug in a real model later, just set an env var — no API rewrite.

Credits live in a tiny SQLite DB. On Embarko the DB is stored under
DATA_DIR (survives redeploys); locally it is ./ruapi.db.
Run:  uvicorn main:app --host 0.0.0.0 --port 8000
"""

import json
import os
import re
import secrets
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

APP_NAME = "ruapi"
VERSION = "1.0.0"
FREE_CREDITS = 100

BASE_DIR = Path(__file__).parent
DATA_DIR = os.environ.get("DATA_DIR")
if DATA_DIR:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
DB_PATH = Path(DATA_DIR) / "ruapi.db" if DATA_DIR else BASE_DIR / "ruapi.db"

app = FastAPI(title="ruapi", version=VERSION, description="Simple Hindi + English AI API with credits (v1)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- DB / keys
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS keys "
        "(api_key TEXT PRIMARY KEY, credits INTEGER NOT NULL, created_at TEXT NOT NULL)"
    )
    return conn


def create_key() -> str:
    key = "ruapi_" + secrets.token_urlsafe(24)
    conn = db()
    conn.execute(
        "INSERT INTO keys (api_key, credits, created_at) VALUES (?, ?, ?)",
        (key, FREE_CREDITS, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return key


def get_credits(key: str):
    conn = db()
    row = conn.execute("SELECT credits FROM keys WHERE api_key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def spend_credit(key: str) -> int:
    """Deduct 1 credit. Returns the new balance, or None if key/credits missing."""
    conn = db()
    row = conn.execute("SELECT credits FROM keys WHERE api_key = ?", (key,)).fetchone()
    if not row or row[0] <= 0:
        conn.close()
        return None
    new_balance = row[0] - 1
    conn.execute("UPDATE keys SET credits = ? WHERE api_key = ?", (new_balance, key))
    conn.commit()
    conn.close()
    return new_balance


def require_key(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing API key. Send: Authorization: Bearer <your-key>")
    key = authorization.removeprefix("Bearer ").strip()
    if get_credits(key) is None:
        raise HTTPException(status_code=401, detail="Invalid API key. Create one at POST /v1/keys")
    return key


# ---------------------------------------------------------------- language
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
HINDI_WORDS = {
    "namaste", "namaskar", "kya", "kaise", "hain", "hai", "mujhe", "aap",
    "mera", "meri", "karo", "batao", "samjhao", "likho", "dhanyavad",
    "shukriya", "kahan", "kab", "kyun", "kaun", "kitna", "accha",
}


def detect_language(message: str, requested: str | None) -> str:
    if requested in ("hi", "en"):
        return requested
    if DEVANAGARI.search(message):
        return "hi"
    words = set(re.findall(r"[a-z]+", message.lower()))
    if words & HINDI_WORDS:
        return "hi"
    return "en"


# ---------------------------------------------------------------- demo brain
PHRASES_EN_TO_HI = {
    "hello": "नमस्ते", "hi": "नमस्ते", "good morning": "शुभ प्रभात",
    "good night": "शुभ रात्रि", "thank you": "धन्यवाद", "thanks": "धन्यवाद",
    "how are you": "आप कैसे हैं?", "goodbye": "अलविदा",
    "i love you": "मैं तुमसे प्यार करता हूँ", "welcome": "स्वागत है",
    "please": "कृपया", "sorry": "माफ़ कीजिए", "yes": "हाँ", "no": "नहीं",
    "friend": "दोस्त", "water": "पानी", "food": "खाना", "help": "मदद",
}
PHRASES_HI_TO_EN = {v: k for k, v in PHRASES_EN_TO_HI.items()}


def demo_reply(message: str, lang: str) -> str:
    """Rule-based demo brain for common asks. Small, honest, and free."""
    text = message.strip()
    low = text.lower()

    # -- maths: "2 + 3", "12 * 4" -----------------------------------------
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([+\-*/x])\s*(\d+(?:\.\d+)?)\s*\??\s*", low)
    if m:
        a, op, b = float(m.group(1)), m.group(2), float(m.group(3))
        ops = {"+": a + b, "-": a - b, "*": a * b, "x": a * b, "/": (a / b if b else None)}
        if ops[op] is None:
            return "0 se bhaag nahi sakte. / Cannot divide by zero." if lang == "hi" else "Cannot divide by zero."
        val = ops[op]
        val = int(val) if val == int(val) else round(val, 2)
        return f"{a:g} {op} {b:g} = {val}" if lang == "en" else f"{a:g} {op} {b:g} = {val} — jawab yahi hai."

    # -- translation (before greetings: "translate hello to Hindi" has "hello" in it)
    tr = re.search(r"translate\s+(.+?)\s+to\s+(hindi|english)", low)
    if tr:
        phrase, target = tr.group(1).strip().strip("'\""), tr.group(2)
        table = PHRASES_EN_TO_HI if target == "hindi" else PHRASES_HI_TO_EN
        out = table.get(phrase)
        if out:
            return f"'{phrase}' in {target.title()}: {out}" if lang == "en" else f"'{phrase}' ka {target} mein matlab: {out}"
        return (
            f"Demo brain mein '{phrase}' ka anuvaad abhi nahi hai — v1 mein sirf aam vakya hain. "
            f"Asli model judne par koi bhi anuvaad ho jayega."
            if lang == "hi"
            else f"I don't have a translation for '{phrase}' in the demo brain yet — v1 only knows common phrases. "
                 f"Once a real model is plugged in, any translation will work."
        )

    # -- greetings (only when the message is basically just a greeting) -----
    if re.fullmatch(r"(hi|hello|hey|namaste|namaskar|pranam)[!.,? ]*", low):
        return (
            "Namaste! Main ruapi hoon — aap Hindi ya English mein kuch bhi pooch sakte hain. "
            "Jaise: 'translate hello to Hindi', 'mujhe ek email likh do', ya 'AI kya hai samjhao'."
            if lang == "hi"
            else "Hello! I'm ruapi — ask me anything in Hindi or English. Try: 'translate hello to Hindi', "
                 "'write me an email', or 'explain what AI is'."
        )

    # -- writing help ------------------------------------------------------
    if any(w in low for w in ("write", "likh", "letter", "email", "essay", "caption", "bio", "application")):
        if lang == "hi":
            return (
                "Zaroor! Yeh ek chhota sa template hai — aap apni details bhar lein:\n\n"
                "Subject: [Apna kaam yahan likhein]\n\nNamaste,\nMera naam [aapka naam] hai. Main [kaam / request] ke baare mein likh raha/rahi hoon. "
                "Kripya meri madad karein.\n\nDhanyavad,\n[Naam] | [Phone]"
            )
        return (
            "Sure! Here is a short template — just fill in your details:\n\n"
            "Subject: [Your topic here]\n\nHello,\nMy name is [your name]. I am writing about [your request]. "
            "Could you please help me with this?\n\nThank you,\n[Name] | [Phone]"
        )

    # -- explain -----------------------------------------------------------
    m = re.search(r"(?:explain|what is|what are|tell me about)\s+(.+)", low) or \
        re.search(r"(.+?)\s+(kya hai|samjhao|matlab kya hai)", low)
    if m:
        topic = m.group(1).strip().strip("?") or "yeh vishay"
        # strip a leading greeting, e.g. "namaste, ai kya hai" -> topic "ai"
        topic = re.sub(r"^(नमस्ते|नमस्कार|namaste|namaskar|hello|hi|hey)[, ]+", "", topic).strip() or topic
        if lang == "hi":
            return (
                f"'{topic}' ke baare mein seedhe shabdon mein: yeh ek aisa vishay hai jise chhote-chhote "
                f"hisson mein samajhna aasan hota hai. Demo brain (v1) mein iska poora jawab abhi nahi hai, "
                f"lekin asli model judne par main iska vistaar se jawab de paunga. "
                f"Tab tak — aap '{topic}' ke baare mein kya khaas jaanna chahte hain?"
            )
        return (
            f"In simple words, '{topic}' is best understood in small parts. The demo brain (v1) doesn't "
            f"have a full answer for this yet, but once a real model is plugged in I will be able to "
            f"explain it in detail. What part of '{topic}' would you like to know first?"
        )

    # -- default -----------------------------------------------------------
    if lang == "hi":
        return (
            f"Aapne poocha: \"{text}\"\n\nMain ruapi hoon — Hindi aur English mein madad karta hoon. "
            f"Main abhi demo mode (v1) mein hoon, isliye aam sawalon ka jawab de sakta hoon: namaste, "
            f"anuvaad, likhne mein madad, ya kisi cheez ko samjhana. Asli AI model judne par main "
            f"kisi bhi sawal ka jawab de paunga."
        )
    return (
        f"You asked: \"{text}\"\n\nI'm ruapi — I help in Hindi and English. I'm in demo mode (v1) right now, "
        f"so I can answer common asks: greetings, translation, writing help, or explaining things. "
        f"Once a real AI model is plugged in, I'll be able to answer any question."
    )


# ------------------------------------------------------- provider hook (v2)
def _post_json(url: str, payload: dict, headers: dict, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def provider_reply(message: str, lang: str):
    """Try a real model if a key is set. Returns (reply, model) or None.

    GROQ_API_KEY first, then SARVAM_API_KEY. No keys are hardcoded;
    set them as env vars on the host. Any failure -> None (demo brain).
    """
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        try:
            data = _post_json(
                "https://api.groq.com/openai/v1/chat/completions",
                {"model": "llama-3.1-8b-instant",
                 "messages": [
                     {"role": "system", "content": "You are ruapi, a helpful Indian AI. Answer in Hindi if the user writes Hindi, else English. Keep answers short."},
                     {"role": "user", "content": message},
                 ]},
                {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            )
            return data["choices"][0]["message"]["content"].strip(), "groq/llama-3.1-8b-instant"
        except Exception:
            pass
    sarvam_key = os.environ.get("SARVAM_API_KEY")
    if sarvam_key:
        try:
            data = _post_json(
                "https://api.sarvam.ai/v1/chat/completions",
                {"model": "sarvam-m",
                 "messages": [
                     {"role": "system", "content": "You are ruapi, a helpful Indian AI. Answer in Hindi if the user writes Hindi, else English. Keep answers short."},
                     {"role": "user", "content": message},
                 ]},
                {"api-subscription-key": sarvam_key, "Content-Type": "application/json"},
            )
            return data["choices"][0]["message"]["content"].strip(), "sarvam/sarvam-m"
        except Exception:
            pass
    # No-key upstream: Pollinations, free, no signup. Verified live
    # 2026-11-21 (Hindi + Hinglish answers, model gpt-oss-20b). It can be
    # slow on a cold start and is rate-limited, so any failure falls
    # through to the demo brain below. Set POLLINATIONS_ENABLED=0 to skip.
    if os.environ.get("POLLINATIONS_ENABLED", "1") != "0":
        try:
            data = _post_json(
                "https://text.pollinations.ai/openai/chat/completions",
                {"model": "gpt-oss-20b",
                 "messages": [
                     {"role": "system", "content": "You are ruapi, a helpful Indian AI. Answer in the user's language — Hindi, English or Hinglish (Roman Hindi). Keep answers short. Never invent live facts (weather, train times, prices) — say you don't know them."},
                     {"role": "user", "content": message},
                 ]},
                {"Content-Type": "application/json"},
                timeout=25,
            )
            reply = data["choices"][0]["message"]["content"].strip()
            if reply:
                return reply, "pollinations/gpt-oss-20b"
        except Exception:
            pass
    return None


# ------------------------------------------------------------------ models
class ChatIn(BaseModel):
    message: str
    language: str | None = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionsIn(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    language: str | None = None


# ----------------------------------------------------------------- routes
@app.get("/health")
def health():
    return {"status": "ok", "service": APP_NAME, "version": VERSION,
            "model": "demo-brain-v1 (plug in GROQ_API_KEY / SARVAM_API_KEY for a real model)"}


@app.post("/v1/keys")
def make_key():
    key = create_key()
    return {"api_key": key, "credits": FREE_CREDITS,
            "note": f"100 free credits. 1 credit per chat call. Send it as: Authorization: Bearer {key}"}


@app.get("/v1/credits")
def credits(authorization: str | None = Header(default=None)):
    key = require_key(authorization)
    return {"api_key": key[:10] + "...", "credits": get_credits(key)}


def _answer(key: str, message: str, language: str | None):
    """Shared chat logic: auth/402 already checked by caller. Returns (reply, lang, model, balance)."""
    lang = detect_language(message, language)
    got = provider_reply(message, lang)
    if got:
        reply, model = got
    else:
        reply, model = demo_reply(message, lang), "demo-brain-v1"
    balance = spend_credit(key)
    return reply, lang, model, balance


@app.post("/v1/chat")
def chat(body: ChatIn, response: Response, authorization: str | None = Header(default=None)):
    key = require_key(authorization)
    if get_credits(key) <= 0:
        raise HTTPException(status_code=402, detail="Out of credits. Create a new key at POST /v1/keys")
    reply, lang, model, balance = _answer(key, body.message, body.language)
    response.headers["X-Credits-Remaining"] = str(balance)
    return {"reply": reply, "language": lang, "model": model, "credits_remaining": balance}


@app.post("/v1/chat/completions")
def chat_completions(body: ChatCompletionsIn, response: Response, authorization: str | None = Header(default=None)):
    """OpenAI-compatible alias for /v1/chat, so standard clients work.
    Takes {"messages": [{"role": "user", "content": "..."}]} and answers
    the last user message. Same credits: 1 per call, 401/402 as above."""
    key = require_key(authorization)
    if get_credits(key) <= 0:
        raise HTTPException(status_code=402, detail="Out of credits. Create a new key at POST /v1/keys")
    user_msgs = [m.content for m in body.messages if m.role == "user"]
    if not user_msgs:
        raise HTTPException(status_code=400, detail="messages must include at least one user message")
    reply, lang, model, balance = _answer(key, user_msgs[-1], body.language)
    response.headers["X-Credits-Remaining"] = str(balance)
    return {
        "id": "ruapi-" + secrets.token_hex(8),
        "object": "chat.completion",
        "created": int(datetime.now(timezone.utc).timestamp()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": max(1, len(user_msgs[-1]) // 4),
                  "completion_tokens": max(1, len(reply) // 4),
                  "total_tokens": max(2, (len(user_msgs[-1]) + len(reply)) // 4)},
        "language": lang,
        "credits_remaining": balance,
    }


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError:
        port = 8000
    uvicorn.run(app, host="0.0.0.0", port=port)
