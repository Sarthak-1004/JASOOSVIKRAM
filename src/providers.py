"""Image providers, tried in order for every image (config images.providers).

gemini       - Nano Banana via the Gemini API. Needs a billed key (free tier has no image quota).
cloudflare   - Workers AI FLUX.2 [klein]. Free: 10,000 neurons/day (~90 images at 1024x576),
               supports up to 4 reference images for consistent faces.
               Secrets: CF_ACCOUNT_ID, CF_API_TOKEN.
pollinations - image.pollinations.ai FLUX. Free, no signup, slow (rate limited). Text-only.
               Optional secret POLLINATIONS_TOKEN raises the limits.

A provider that is out of quota or not configured is switched off for the rest of the run.
"""
import base64
import io
import os
import threading
import time
import urllib.parse

import requests
from google.genai import types
from PIL import Image

from . import models
from .common import CFG, env, gh_annotate, log

SIZES = {"16:9": (1024, 576), "9:16": (576, 1024), "1:1": (1024, 1024)}
_dead: dict[str, str] = {}
stats: dict[str, int] = {}
_lock = threading.Lock()
_poll_lock = threading.Lock()
_poll_last = [0.0]


class ProviderDead(Exception):
    """Provider unusable for the rest of this run (no key, no quota)."""


def _order() -> list[str]:
    return list(CFG["images"].get("providers") or ["gemini"])


def all_dead() -> bool:
    return all(p in _dead for p in _order())


def alive(name: str) -> bool:
    return name in _order() and name not in _dead


def summary() -> str:
    used = ", ".join(f"{k} {v}" for k, v in stats.items()) or "none yet"
    off = "; ".join(f"{k}: {_short(v)}" for k, v in _dead.items())
    return used + (f" (not used - {off})" if off else "")


def _short(why: str) -> str:
    low = why.lower()
    if "free tier" in low or "free_tier" in low:
        return "free-tier key, no image quota"
    if "allowance" in low:
        return "free daily allowance used up"
    if "not set" in low:
        return "secrets not set"
    return why[:70]


def _kill(name: str, why: str) -> None:
    with _lock:
        if name not in _dead:
            _dead[name] = why[:160]
            log.warning("Image provider %s switched off: %s", name, why[:300])
            gh_annotate("warning", f"image provider {name} off", why[:1000])


def generate(prompt: str, refs: list[bytes], aspect: str) -> bytes | None:
    """First provider that returns an image wins. None if every live provider failed this one."""
    for name in _order():
        if name in _dead:
            continue
        try:
            data = {"gemini": _gemini, "cloudflare": _cloudflare,
                    "pollinations": _pollinations}[name](prompt, refs, aspect)
        except ProviderDead as e:
            _kill(name, str(e))
            continue
        except Exception as e:  # noqa: BLE001 - one bad image must not stop the run
            log.warning("%s image failed: %s", name, str(e)[:300])
            continue
        if data:
            with _lock:
                stats[name] = stats.get(name, 0) + 1
            return data
    return None


# ---------------------------------------------------------------- gemini

_gclient = None


def gemini_client():
    global _gclient
    if _gclient is None:
        from google import genai
        _gclient = genai.Client(api_key=env("GEMINI_API_KEY"))
    return _gclient


def _gemini(prompt: str, refs: list[bytes], aspect: str) -> bytes | None:
    if not os.environ.get("GEMINI_API_KEY"):
        raise ProviderDead("GEMINI_API_KEY not set")
    contents = [prompt, *[types.Part.from_bytes(data=r, mime_type="image/jpeg") for r in refs]]
    try:
        resp = models.run("image", gemini_client(), lambda m: gemini_client().models.generate_content(
            model=m, contents=contents,
            config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"],
                                               image_config=types.ImageConfig(aspect_ratio=aspect))))
    except RuntimeError as e:  # no usable model (free tier / retired models)
        raise ProviderDead(str(e)) from e
    except Exception as e:  # noqa: BLE001
        if "resource_exhausted" in str(e).lower() or "429" in str(e):
            raise ProviderDead(f"quota: {e}") from e
        raise
    for cand in resp.candidates or []:
        for part in (cand.content.parts if cand.content else []) or []:
            if part.inline_data and part.inline_data.data:
                return part.inline_data.data
    return None


# ---------------------------------------------------------------- cloudflare

def _small_ref(data: bytes) -> bytes:
    """Cloudflare FLUX.2 wants reference images smaller than 512x512."""
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((508, 508))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _cloudflare(prompt: str, refs: list[bytes], aspect: str) -> bytes | None:
    acct, token = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_API_TOKEN")
    if not acct or not token:
        raise ProviderDead("CF_ACCOUNT_ID / CF_API_TOKEN secrets not set")
    w, h = SIZES.get(aspect, SIZES["16:9"])
    form = {"prompt": (None, prompt[:2000]), "width": (None, str(w)), "height": (None, str(h))}
    for i, r in enumerate(refs[:4]):
        form[f"input_image_{i}"] = (f"ref{i}.jpg", _small_ref(r), "image/jpeg")
    url = (f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/"
           f"{CFG['images'].get('cloudflare_model', '@cf/black-forest-labs/flux-2-klein-4b')}")
    for attempt in range(3):
        r = requests.post(url, headers={"Authorization": f"Bearer {token}"}, files=form, timeout=180)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.ok and body.get("success", True) and (body.get("result") or {}).get("image"):
            return base64.b64decode(body["result"]["image"])
        text = str(body.get("errors") or r.text)[:400]
        low = text.lower()
        if r.status_code in (401, 403) or "authentication" in low or "not entitled" in low:
            raise ProviderDead(f"Cloudflare rejected the API token/account ({r.status_code}): {text}")
        if "4006" in low or "allocation" in low or "neurons" in low:
            raise ProviderDead("Cloudflare free daily allowance used up (resets 05:30 IST)")
        if "3030" in low or "flagged" in low or "nsfw" in low:
            log.warning("Cloudflare refused this prompt (moderation): %s", text)
            return None
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(10 * (attempt + 1))
            continue
        log.warning("Cloudflare image error %s: %s", r.status_code, text)
        return None
    return None


# ---------------------------------------------------------------- pollinations

def _pollinations(prompt: str, refs: list[bytes], aspect: str) -> bytes | None:
    w, h = SIZES.get(aspect, SIZES["16:9"])
    q = {"width": w, "height": h, "model": "flux", "nologo": "true", "seed": abs(hash(prompt)) % 10**6}
    tok = os.environ.get("POLLINATIONS_TOKEN")
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:1500])
           + "?" + urllib.parse.urlencode(q))
    gap = float(CFG["images"].get("pollinations_interval", 46))
    with _poll_lock:  # one request at a time, spaced out, to respect the free rate limit
        for attempt in range(3):
            wait = _poll_last[0] + gap - time.time()
            if wait > 0:
                time.sleep(wait)
            _poll_last[0] = time.time()
            try:
                r = requests.get(url, headers=headers, timeout=240)
            except requests.RequestException as e:
                log.warning("Pollinations error: %s", e)
                continue
            if r.ok and r.headers.get("content-type", "").startswith("image/"):
                im = Image.open(io.BytesIO(r.content))
                if im.size == (w, h):  # a rate-limit banner comes back at a different size
                    return r.content
                log.warning("Pollinations returned %s instead of %sx%s (rate-limit banner?)", im.size, w, h)
            elif r.status_code in (401, 403):
                raise ProviderDead(f"Pollinations refused ({r.status_code})")
            else:
                log.warning("Pollinations %s: %s", r.status_code, r.text[:200])
    return None
