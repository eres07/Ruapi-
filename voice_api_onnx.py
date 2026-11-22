"""
ruapi voice API v2 — Kokoro-ONNX text-to-speech, same keys/credits as main.py.

Replaces the torch Kokoro setup (voice_api.py), which took ~15.6 min per
2-second line on VPS 1 once torch's MKLDNN fast path had to be disabled
(it crashed Kokoro's LSTM with SIGFPE). This version has NO torch at all:
Kokoro-ONNX runs the same Kokoro-82M model and the same 6 voices through
ONNX Runtime.

Benchmark on VPS 1 (2026-11-22, nproc 1, AMD EPYC 9354P, int8 model,
1 thread — the winner): Hindi 1.8 s, Hinglish 3.3 s, English 1.7 s per
short line, ~427 MB RAM. ~500x faster than the torch setup. 2 threads
was slower on this box, so synthesis is serialised with one thread and
one request at a time (a lock around create()).

Endpoints (same shape as the torch version, so clients don't change):
  GET  /health             — no auth
  GET  /v1/voices         — list the 6 Kokoro presets (no auth)
  POST /v1/keys            — create a demo API key with 100 free credits
  GET  /v1/credits         — remaining credits for your key (Bearer auth)
  POST /v1/audio/speech    — 1 credit per call (Bearer auth)
        {"text": "...", "voice": "hf_alpha"} -> audio/wav (24 kHz)

Rules (same as v1, verified 2026-11-21):
  - Hindi presets (hf_alpha, hf_beta, hm_omega, hm_psi) speak Devanagari
    Hindi + English well, and Roman Hinglish through the Hindi pipeline.
  - af_heart / af_bella speak English + Roman-letter Hinglish only.
    Devanagari text sent to them comes out broken, so the API rejects
    it with a clear 400 instead of generating garbage.
  - Kokoro-82M is Apache-2.0, commercial OK. Needs espeak-ng installed
    on the host (apt install espeak-ng) and the 3 model files in the
    models dir (kokoro-v1.0.int8.onnx, voices-v1.0.bin).

Routing (v1, simple): the voice picks the pipeline — Hindi voices use
lang "hi", American voices use "en-us" — plus the Devanagari guard
above. A transliteration lexicon (Roman Hindi -> Devanagari) is a later
step, not this version.

Disk cache: identical requests (same voice + text) reuse the cached wav,
keyed by backend/model/threads/speed/voice/text.

Run:  uvicorn voice_api_onnx:app --host 0.0.0.0 --port 8001
(Keep this OUT of requirements.txt: the Vercel text API must not try
to install kokoro-onnx. Use requirements-voice-onnx.txt on the voice host.)
"""

import hashlib
import io
import os
import re
import sys
import threading
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

VERSION = "2.0.0"
MAX_TEXT_CHARS = 500
ENGINE = "kokoro-onnx-int8"
MODEL_FILE = "kokoro-v1.0.int8.onnx"
VOICES_FILE = "voices-v1.0.bin"
THREADS = 1  # benchmark winner on VPS 1 (2 threads was slower there)
SPEED = 1.0

BASE_DIR = Path(__file__).parent
MODEL_DIR = Path(os.environ.get("VOICE_MODEL_DIR", BASE_DIR / "models"))
CACHE_DIR = Path(os.environ.get("VOICE_CACHE_DIR", BASE_DIR / "voice_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ruapi-voice", version=VERSION, description="Kokoro-ONNX voice API with credits (v2)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# voice_id -> (onnx lang, gender, language, style, notes)
VOICES = {
    "hf_alpha": ("hi", "female", "Hindi", "warm, clear", "Hindi + English"),
    "hf_beta": ("hi", "female", "Hindi", "bright", "Hindi + English"),
    "hm_omega": ("hi", "male", "Hindi", "warm, deep", "Hindi + English"),
    "hm_psi": ("hi", "male", "Hindi", "calm", "Hindi + English"),
    "af_heart": ("en-us", "female", "American English", "warm, flagship", "English + Roman Hinglish only — no Devanagari"),
    "af_bella": ("en-us", "female", "American English", "energetic", "English + Roman Hinglish only — no Devanagari"),
}

_kokoro = None
_kokoro_lock = threading.Lock()
_synth_lock = threading.Lock()  # one synthesis at a time on this small CPU


def get_kokoro():
    """Lazy-load the ONNX session once (first call loads the model)."""
    global _kokoro
    if _kokoro is None:
        with _kokoro_lock:
            if _kokoro is None:
                import onnxruntime as ort
                from kokoro_onnx import Kokoro

                model_path = MODEL_DIR / MODEL_FILE
                voices_path = MODEL_DIR / VOICES_FILE
                if not model_path.exists() or not voices_path.exists():
                    raise RuntimeError(
                        f"Model files missing in {MODEL_DIR} — need {MODEL_FILE} and {VOICES_FILE}"
                    )
                so = ort.SessionOptions()
                so.intra_op_num_threads = THREADS
                so.inter_op_num_threads = 1
                session = ort.InferenceSession(
                    str(model_path), sess_options=so, providers=["CPUExecutionProvider"]
                )
                _kokoro = Kokoro.from_session(session, str(voices_path))
    return _kokoro


def split_sentences(text: str) -> list[str]:
    """Split on sentence ends, including the Devanagari danda (U+0964)."""
    parts = re.split(r"(?<=[.!?।।])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def cache_key(voice: str, text: str) -> str:
    raw = f"{ENGINE}|threads={THREADS}|speed={SPEED}|{voice}|{text}".encode()
    return hashlib.sha256(raw).hexdigest()


def synthesize(voice: str, lang: str, text: str) -> tuple[np.ndarray, int]:
    """Synthesise (with disk cache). Returns (audio, sample_rate)."""
    key = cache_key(voice, text)
    cached = CACHE_DIR / f"{key}.wav"
    if cached.exists():
        audio, sr = sf.read(str(cached))
        return audio, sr
    kokoro = get_kokoro()
    with _synth_lock:
        chunks = []
        for sentence in split_sentences(text):
            samples, sr = kokoro.create(sentence, voice=voice, speed=SPEED, lang=lang)
            chunks.append(np.asarray(samples))
    if not chunks:
        raise RuntimeError("Kokoro returned no audio")
    audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    sf.write(str(cached), audio, sr)
    return audio, sr


class SpeechIn(BaseModel):
    text: str
    voice: str = "hf_alpha"


@app.get("/health")
def health():
    return {"status": "ok", "service": APP_NAME + "-voice", "version": VERSION,
            "voices": sorted(VOICES), "engine": ENGINE, "threads": THREADS}


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
    lang = VOICES[voice][0]
    if lang == "en-us" and DEVANAGARI.search(text):
        raise HTTPException(
            status_code=400,
            detail=f"Voice '{voice}' cannot speak Devanagari Hindi — it reads it letter by letter. "
                   f"Send Hindi in Roman letters (Hinglish) or use a Hindi voice (hf_alpha, hf_beta, hm_omega, hm_psi).",
        )
    try:
        audio, sr = synthesize(voice, lang, text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Speech generation failed: {e}")
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV")
    balance = spend_credit(key)
    response.headers["X-Credits-Remaining"] = str(balance)
    response.headers["X-Voice"] = voice
    return Response(content=buf.getvalue(), media_type="audio/wav",
                    headers={"Content-Disposition": f'attachment; filename="ruapi_{voice}.wav"'})


@app.get("/")
def home():
    return {"service": APP_NAME + "-voice", "version": VERSION,
            "voices": sorted(VOICES), "engine": ENGINE,
            "usage": 'POST /v1/audio/speech with {"text": "...", "voice": "hf_alpha"} and Authorization: Bearer <key>'}


if __name__ == "__main__":
    import uvicorn

    try:
        port = int(os.environ.get("VOICE_PORT", os.environ.get("PORT", "8001")))
    except ValueError:
        port = 8001
    uvicorn.run(app, host="0.0.0.0", port=port)
