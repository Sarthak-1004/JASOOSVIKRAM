"""Hindi narration with Sarvam AI Bulbul. One WAV per scene (gives exact scene timing)."""
import base64
import re
import time
import wave
from pathlib import Path

import requests

from .common import CFG, env, log

URL = "https://api.sarvam.ai/text-to-speech"
MAX_CHARS = 450  # stay well under the per-request limit


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[।!?\.])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _chunks(text: str) -> list[str]:
    out, cur = [], ""
    for s in split_sentences(text):
        if cur and len(cur) + len(s) + 1 > MAX_CHARS:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def _speak(text: str) -> bytes:
    v = CFG["voice"]
    # only the fields bulbul:v3 accepts; older pitch/loudness options were removed
    body = {"text": text, "target_language_code": "hi-IN", "speaker": v["speaker"],
            "model": v["model"], "pace": v["pace"]}
    if v.get("temperature") is not None:
        body["temperature"] = v["temperature"]
    last = ""
    for attempt in range(5):
        r = requests.post(URL, json=body, timeout=120,
                          headers={"api-subscription-key": env("SARVAM_API_KEY").strip()})
        if r.ok:
            return base64.b64decode(r.json()["audios"][0])
        last = f"HTTP {r.status_code}: {r.text[:500]}"
        log.warning("Sarvam TTS %s", last)
        if r.status_code in (400, 401, 403, 404, 422):  # bad key/request: retrying will not help
            break
        time.sleep(3 * 2 ** attempt)
    raise RuntimeError(f"Sarvam TTS failed: {last}")


def _pcm(wav_bytes: bytes, path: Path):
    path.write_bytes(wav_bytes)
    with wave.open(str(path)) as w:
        return w.readframes(w.getnframes()), w.getparams()


def scene_audio(scenes: list[dict], wd: Path) -> list[float]:
    """Writes audio/scene_XX.wav (speech + trailing gap). Returns speech durations (s)."""
    adir = wd / "audio"
    adir.mkdir(parents=True, exist_ok=True)
    gap = CFG["voice"]["scene_gap"]
    speech = []
    for i, sc in enumerate(scenes, 1):
        out = adir / f"scene_{i:02d}.wav"
        dur_file = adir / f"scene_{i:02d}.dur"
        if out.exists() and dur_file.exists():
            speech.append(float(dur_file.read_text()))
            continue
        frames, params = b"", None
        for j, ch in enumerate(_chunks(sc["narration"])):
            f, p = _pcm(_speak(ch), adir / f"tmp_{i}_{j}.wav")
            params = params or p
            frames += f
        rate, width, nch = params.framerate, params.sampwidth, params.nchannels
        dur = len(frames) / (rate * width * nch)
        frames += b"\x00" * int(gap * rate) * width * nch
        with wave.open(str(out), "wb") as w:
            w.setparams(params)
            w.writeframes(frames)
        dur_file.write_text(f"{dur:.3f}")
        speech.append(dur)
        log.info("Scene %d narration: %.1fs", i, dur)
    for t in adir.glob("tmp_*.wav"):
        t.unlink()
    return speech


def concat_wavs(paths: list[Path], out: Path) -> None:
    params, frames = None, b""
    for p in paths:
        with wave.open(str(p)) as w:
            params = params or w.getparams()
            frames += w.readframes(w.getnframes())
    with wave.open(str(out), "wb") as w:
        w.setparams(params)
        w.writeframes(frames)
