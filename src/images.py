"""Image generation with Gemini "Nano Banana" image models: batch + sync fallback."""
import base64
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image, ImageEnhance, ImageFilter

from . import models, series
from .common import CFG, STATE_DIR, env, log
from .story import all_shots

ASPECT = {"h": "16:9", "v": "9:16"}
_client = None


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=env("GEMINI_API_KEY"))
    return _client


def img_path(wd: Path, key: str) -> Path:
    return wd / "img" / f"{key}.jpg"


def chars_dir(wd: Path) -> Path:
    """Character sheets live in state/ (committed) so every later stage reuses the same faces."""
    d = STATE_DIR / "refs" / wd.name
    d.mkdir(parents=True, exist_ok=True)
    return d


def char_path(wd: Path, cid: str) -> Path:
    return chars_dir(wd) / f"{cid}.jpg"


def key(kind: str, n: int) -> str:
    return f"{kind}-{n:03d}"


def clip_scene_numbers(story: dict, clip_ids: list[int]) -> set[int]:
    scenes = set()
    for ci in clip_ids:
        c = story["clips"][ci]
        scenes.update(range(c["start_scene"], c["end_scene"] + 1))
    return scenes


def wanted_keys(story: dict, clip_ids: list[int]) -> list[str]:
    shots = all_shots(story)
    keys = [key("h", s["n"]) for s in shots]
    if CFG["images"]["vertical_for_shorts"]:
        vs = clip_scene_numbers(story, clip_ids)
        keys += [key("v", s["n"]) for s in shots if s["scene"] in vs]
    return keys


def _shot_by_key(story: dict, k: str) -> tuple[dict, str]:
    kind, n = k.split("-")
    return all_shots(story)[int(n) - 1], ASPECT[kind]


def _text(story: dict, shot: dict, aspect: str, soften: bool = False) -> str:
    chars = {c["id"]: c for c in story["characters"]}
    style = CFG["images"]["style_prompt"].strip()
    if series.enabled():
        style += " " + series.bible().get("style_extra", "").strip()
    lines = [style, "", f"Frame: {shot['prompt']}"]
    present = [chars[c] for c in shot["characters"] if c in chars]
    if present:
        lines.append("Characters in this frame - match the attached reference images exactly "
                     "(same face, hair, clothes), in the same art style:")
        for i, c in enumerate(present, 1):
            lines.append(f"- {c['id']}: {c['look']} (reference image {i})")
    if soften:
        lines.append("Keep it suspenseful but calm: no weapons pointed, no injuries, no blood.")
    lines.append(f"Aspect ratio {aspect}. Single illustration, full bleed, no borders, no text.")
    return "\n".join(lines)


def _save(data: bytes, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.open(io.BytesIO(data)).convert("RGB").save(out, "JPEG", quality=92)


def _generate(contents, aspect: str):
    return models.run("image", client(), lambda m: client().models.generate_content(
        model=m, contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect))))


def _image_from_response(resp) -> bytes | None:
    for cand in resp.candidates or []:
        for part in (cand.content.parts if cand.content else []) or []:
            if part.inline_data and part.inline_data.data:
                return part.inline_data.data
    return None


# ---------------------------------------------------------------- characters

def make_characters(story: dict, wd: Path) -> None:
    """One reference sheet per main character (sync, ~3 images)."""
    for c in story["characters"]:
        out = char_path(wd, c["id"])
        if out.exists() or series.restore_ref(c["id"], out):
            continue
        prompt = (f"{CFG['images']['style_prompt'].strip()}\n\nCharacter reference sheet: one person, "
                  f"full body standing front view on the left and a head-and-shoulders close-up on the "
                  f"right, plain light grey background. {c['look']}")
        for attempt in range(3):
            try:
                resp = _generate(prompt, "16:9")
                data = _image_from_response(resp)
                if data:
                    Image.open(io.BytesIO(data)).convert("RGB").save(out, "JPEG", quality=90)
                    series.keep_ref(c["id"], out)
                    log.info("Character sheet: %s", c["id"])
                    break
            except Exception as e:  # noqa: BLE001
                if str(e) == models.FREE_TIER_HELP:
                    raise
                log.warning("Character %s attempt %d failed: %s", c["id"], attempt + 1, e)
                time.sleep(5 * (attempt + 1))


def _ref_parts_bytes(story: dict, shot: dict, wd: Path) -> list:
    parts = []
    for cid in shot["characters"]:
        p = char_path(wd, cid)
        if p.exists():
            parts.append(types.Part.from_bytes(data=p.read_bytes(), mime_type="image/jpeg"))
    return parts


def make_thumbnail_art(story: dict, wd: Path) -> None:
    """Dedicated 16:9 thumbnail illustration from story['thumbnail_prompt'] (sync, 1 image)."""
    out = wd / "img" / "thumb_art.jpg"
    if out.exists() or not story.get("thumbnail_prompt"):
        return
    shot = {"prompt": story["thumbnail_prompt"] + " Extremely high contrast, one clear focal point, "
            "leave empty dark space on the left for a title.",
            "characters": [c["id"] for c in story["characters"]
                           if c["id"].lower() in story["thumbnail_prompt"].lower()
                           or c["name"] in story["thumbnail_prompt"]]}
    for attempt in range(3):
        try:
            resp = _generate([_text(story, shot, "16:9"), *_ref_parts_bytes(story, shot, wd)], "16:9")
            data = _image_from_response(resp)
            if data:
                _save(data, out)
                return
        except Exception as e:  # noqa: BLE001
            if str(e) == models.FREE_TIER_HELP:
                raise
            log.warning("Thumbnail art attempt %d: %s", attempt + 1, e)
            time.sleep(5)


# ---------------------------------------------------------------- sync

def generate_one(story: dict, wd: Path, k: str) -> bool:
    shot, aspect = _shot_by_key(story, k)
    for attempt in range(4):
        try:
            resp = _generate([_text(story, shot, aspect, soften=attempt >= 2),
                              *_ref_parts_bytes(story, shot, wd)], aspect)
            data = _image_from_response(resp)
            if data:
                _save(data, img_path(wd, k))
                return True
            log.warning("%s: no image returned (likely safety block), retrying", k)
        except Exception as e:  # noqa: BLE001
            if str(e) == models.FREE_TIER_HELP:
                raise
            log.warning("%s attempt %d: %s", k, attempt + 1, e)
            time.sleep(min(60, 5 * 2 ** attempt))
    return False


def generate_sync(story: dict, wd: Path, keys: list[str]) -> list[str]:
    """Generate keys in parallel; returns keys that still failed."""
    if not keys:
        return []
    log.info("Generating %d images (standard API)", len(keys))
    with ThreadPoolExecutor(CFG["images"]["sync_workers"]) as ex:
        ok = list(ex.map(lambda k: generate_one(story, wd, k), keys))
    return [k for k, good in zip(keys, ok) if not good]


# ---------------------------------------------------------------- batch

def submit_batch(story: dict, state: dict, wd: Path, keys: list[str]) -> None:
    """Upload character refs + a JSONL of requests; start a 50%-price batch job."""
    uris = {}
    for c in story["characters"]:
        p = char_path(wd, c["id"])
        if p.exists():
            f = client().files.upload(file=str(p), config={"mime_type": "image/jpeg"})
            uris[c["id"]] = f.uri
    lines = []
    for k in keys:
        shot, aspect = _shot_by_key(story, k)
        parts = [{"text": _text(story, shot, aspect)}]
        parts += [{"fileData": {"fileUri": uris[c], "mimeType": "image/jpeg"}}
                  for c in shot["characters"] if c in uris]
        lines.append(json.dumps({"key": k, "request": {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"],
                                 "imageConfig": {"aspectRatio": aspect}}}}))
    jsonl = wd / "batch_requests.jsonl"
    jsonl.write_text("\n".join(lines), encoding="utf-8")
    up = client().files.upload(file=str(jsonl),
                               config={"mime_type": "jsonl", "display_name": f"imgs-{state['date']}"})
    job = models.run("image", client(), lambda m: client().batches.create(
        model=m, src=up.name, config={"display_name": f"thriller-{state['date']}"}))
    state["batch"] = {"name": job.name, "keys": keys}
    log.info("Batch submitted: %s (%d images)", job.name, len(keys))


def collect_batch(state: dict, wd: Path) -> None:
    b = state.get("batch")
    if not b:
        return
    job = client().batches.get(name=b["name"])
    st = job.state.name if job.state else "UNKNOWN"
    log.info("Batch %s state: %s", b["name"], st)
    if st not in ("JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"):
        if st in ("JOB_STATE_PENDING", "JOB_STATE_RUNNING", "JOB_STATE_QUEUED"):
            client().batches.cancel(name=b["name"])  # leftovers go through the standard API
        return
    if not (job.dest and job.dest.file_name):
        return
    raw = client().files.download(file=job.dest.file_name).decode("utf-8")
    saved = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        k = row.get("key")
        for cand in (row.get("response") or {}).get("candidates", []):
            for part in (cand.get("content") or {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    _save(base64.b64decode(inline["data"]), img_path(wd, k))
                    saved += 1
                    break
    log.info("Batch delivered %d images", saved)


# ---------------------------------------------------------------- fallbacks

def blur_vertical(src: Path, out: Path, size=(1080, 1920)) -> None:
    """9:16 frame from a 16:9 image: blurred background + sharp image in the middle."""
    im = Image.open(src).convert("RGB")
    w, h = size
    scale = max(w / im.width, h / im.height)
    bg = im.resize((int(im.width * scale) + 1, int(im.height * scale) + 1))
    bg = bg.crop(((bg.width - w) // 2, (bg.height - h) // 2, (bg.width - w) // 2 + w,
                  (bg.height - h) // 2 + h)).filter(ImageFilter.GaussianBlur(40))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    fg = im.resize((w, int(im.height * w / im.width)))
    bg.paste(fg, (0, (h - fg.height) // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.save(out, "JPEG", quality=92)


def fill_missing(story: dict, wd: Path) -> list[str]:
    """Last resort so a render never fails: reuse a neighbouring image. Returns patched keys."""
    shots = all_shots(story)
    patched = []
    for s in shots:
        p = img_path(wd, key("h", s["n"]))
        if p.exists():
            continue
        for d in range(1, len(shots)):
            for n in (s["n"] - d, s["n"] + d):
                q = img_path(wd, key("h", n))
                if 1 <= n <= len(shots) and q.exists():
                    p.write_bytes(q.read_bytes())
                    patched.append(key("h", s["n"]))
                    break
            if p.exists():
                break
    if patched:
        log.warning("Reused neighbouring images for: %s", patched)
    return patched


def ensure_images(story: dict, state: dict, wd: Path, keys: list[str]) -> list[str]:
    collect_batch(state, wd)
    missing = [k for k in keys if not img_path(wd, k).exists()]
    # verticals are optional (blur fallback); horizontals first
    missing.sort(key=lambda k: k[0] != "h")
    failed = generate_sync(story, wd, missing)
    return fill_missing(story, wd) + failed
