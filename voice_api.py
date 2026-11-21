"""
ruapi voice API — Kokoro-82M text-to-speech as an API, same keys/credits as main.py.

Endpoints:
  GET  /health             — no auth
  GET  /v1/voices         — list the 6 Kokoro presets (no auth)
  POST /v1/keys            — create a demo API key with 100 free credits
  GET  /v1/credits         — remaining credits for your key (Bearer auth)
  POST /v1/audio/speech    — 1 credit per call (Bearer auth)
        {"text": "...", "voice": "hf_alpha"} -> audio/wav (24 kHz)

Rules learned from testing (2026-11-21):
  - Hindi presets (hf_alpha, hf_beta, hm_omega, hm_psi) speak Devanagari
    Hindi + English well.
  - af_heart / af_bella speak English + Roman-letter Hinglish only.
    Devanagari text sent to them comes out broken (read letter by
    letter), so the API rejects it with a clear 400 instead of
    generating garbage.
  - Kokoro-82M is Apache-2.0, commercial OK. Needs espeak-ng installed
    on the host (apt install espeak-ng) and runs on plain CPU.

Run:  uvicorn voice_api:app --host 0.0.0.0 --port 8001
(Keep this OUT of requirements.txt: the Vercel text API must not try
to install torch/kokoro. Use requirements-voice.txt on the voice host.)
"""

import io
import os
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Reuse the credits DB + key helpers from the text API (same ruapi.db
# file when both apps run from the same directory).
sys.path.insert(0, str(Path(__file__).parent))
from main import (  # noqa: E402
    APP_NAME,
    DEVANAGARI,
    FREE_CREDITS,
    create_key,
    get_credits,
    require_key,
    spend_credit,
)

VERSION = "1.0.0"
MAX_TEXT_CHARS = 500

app = FastAPI(title="ruapi-voice", version=VERSION, description="Kokoro TTS voice API with credits (v1)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# voice_id -> (lang_code, gender, language, style, notes)
VOICES = {
    "hf_alpha": ("h", "female", "Hindi", "warm, clear", "Hindi + English"),
    "hf_beta": ("h", "female", "Hindi", "bright", "Hindi + English"),
    "hm_omega": ("h", "male", "Hindi", "warm, deep", "Hindi + English"),
    "hm_psi": ("h", "male", "Hindi", "calm", "Hindi + English"),
    "af_heart": ("a", "female", "American English", "warm, flagship", "English + Roman Hinglish only — no Devanagari"),
    "af_bella": ("a", "female", "American English", "energetic", "English + Roman Hinglish only — no Devanagari"),
}

_pipelines = {}


def get_pipeline(lang_code: str):
    """Lazy-load one KPipeline per language (first call downloads the model)."""
    if lang_code not in _pipelines:
        from kokoro import KPipeline

        _pipelines[lang_code] = KPipeline(lang_code=lang_code)
    return _pipelines[lang_code]


class SpeechIn(BaseModel):
    text: str
    voice: str = "hf_alpha"


@app.get("/health")
def health():
    return {"status": "ok", "service": APP_NAME + "-voice", "version": VERSION,
            "voices": sorted(VOICES), "engine": "kokoro-82m"}


@app.get("/v1/voices")
def list_voices():
    return {"voices": [
        {"id": vid, "lang_code": v[0], "gender": v[1], "language": v[2],
         "style": v[3], "notes": v[4]}
        for vid, v in sorted(VOICES.items())
    ]}


@app.post("/v1/keys")
def make_key():
    key = create_key()
    return {"api_key": key, "credits": FREE_CREDITS,
            "note": f"100 free credits. 1 credit per speech call. Send it as: Authorization: Bearer {key}"}


@app.get("/v1/credits")
def credits(authorization: str | None = Header(default=None)):
    key = require_key(authorization)
    return {"api_key": key[:10] + "...", "credits": get_credits(key)}


@app.post("/v1/audio/speech")
def speech(body: SpeechIn, response: Response, authorization: str | None = Header(default=None)):
    key = require_key(authorization)
    if get_credits(key) <= 0:
        raise HTTPException(status_code=402, detail="Out of credits. Create a new key at POST /v1/keys")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(status_code=400, detail=f"text too long (max {MAX_TEXT_CHARS} characters)")
    voice = body.voice or "hf_alpha"
    if voice not in VOICES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown voice '{voice}'. See GET /v1/voices. Available: {', '.join(sorted(VOICES))}",
        )
    lang_code = VOICES[voice][0]
    if lang_code == "a" and DEVANAGARI.search(text):
        raise HTTPException(
            status_code=400,
            detail=f"Voice '{voice}' cannot speak Devanagari Hindi — it reads it letter by letter. "
                   f"Send Hindi in Roman letters (Hinglish) or use a Hindi voice (hf_alpha, hf_beta, hm_omega, hm_psi).",
        )
    try:
        pipe = get_pipeline(lang_code)
        chunks = []
        for _, _, audio in pipe(text, voice=voice):
            chunks.append(audio)
        if not chunks:
            raise RuntimeError("Kokoro returned no audio")
        audio = np.concatenate(chunks)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Speech generation failed: {e}")
    buf = io.BytesIO()
    sf.write(buf, audio, 24000, format="WAV")
    balance = spend_credit(key)
    response.headers["X-Credits-Remaining"] = str(balance)
    response.headers["X-Voice"] = voice
    return Response(content=buf.getvalue(), media_type="audio/wav",
                    headers={"Content-Disposition": f'attachment; filename="ruapi_{voice}.wav"'})


@app.get("/")
def home():
    return {"service": APP_NAME + "-voice", "version": VERSION,
            "voices": sorted(VOICES),
            "usage": 'POST /v1/audio/speech with {"text": "...", "voice": "hf_alpha"} and Authorization: Bearer <key>'}


if __name__ == "__main__":
    import uvicorn

    try:
        port = int(os.environ.get("VOICE_PORT", os.environ.get("PORT", "8001")))
    except ValueError:
        port = 8001
    uvicorn.run(app, host="0.0.0.0", port=port)
