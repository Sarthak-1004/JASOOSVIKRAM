"""Story writing with Gemini: Hindi narration, scenes, image prompts, metadata."""
import json

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from . import series
from .common import CFG, STATE_DIR, env, log


class Character(BaseModel):
    id: str = Field(description="short latin id, e.g. 'meera'")
    name: str = Field(description="name in Devanagari")
    look: str = Field(description="English visual description: age, gender, face, hair, "
                                  "clothes, distinctive features. Same in every image.")


class Shot(BaseModel):
    prompt: str = Field(description="English image prompt: subject, action, setting, camera "
                                    "angle, lighting. Do not include style words.")
    characters: list[str] = Field(description="ids of characters visible in this shot")


class Scene(BaseModel):
    narration: str = Field(description="Hindi narration in Devanagari for this scene")
    shots: list[Shot]


class Clip(BaseModel):
    start_scene: int = Field(description="1-based scene number where the Short starts")
    end_scene: int = Field(description="1-based scene number where the Short ends (inclusive)")
    hook_text: str = Field(description="2-6 word Hindi hook shown on top of the Short")
    title: str = Field(description="YouTube Shorts title, Hindi, under 80 chars, no hashtags")
    end_question: str = Field(default="", description="short Hindi question shown on the Short's "
                              "last seconds, e.g. 'क्या आप पहचान पाए?'")


class Story(BaseModel):
    title: str = Field(description="YouTube title, Hindi with a few English keywords, under 90 chars")
    description: str = Field(description="YouTube description in Hindi, 3-5 lines, with 5 hashtags at the end")
    tags: list[str]
    logline: str = Field(description="one-line plot summary in English, used to avoid repeats")
    thumbnail_text: str = Field(description="2-4 word Hindi thumbnail text")
    thumbnail_shot: int = Field(description="1-based index (across all shots) of the best thumbnail image")
    thumbnail_prompt: str = Field(default="", description="English prompt for a dedicated, eye-catching "
                                  "thumbnail illustration (one strong face/emotion + one mystery element)")
    characters: list[Character]
    scenes: list[Scene]
    clips: list[Clip]


SYSTEM = """You are the head writer of a hugely popular Hindi thriller storytelling YouTube channel.
You write original, gripping thriller / suspense / mystery stories set in India, narrated by a
female storyteller. Rules:
- Narration language: natural spoken Hindi (Hindustani) in Devanagari script, the way a great
  audio-story narrator speaks. Short sentences. Numbers written as words. No English script.
- Structure: a shocking hook in the first two sentences, rising tension, at least one misdirection,
  a strong twist, and a satisfying but chilling ending. Every scene ends with tension.
- Original story only. No real people, no real crimes, no brands. Suspense over violence:
  danger is implied, never gory. Nothing sexual. No hate towards any community.
- Indian settings with texture: havelis, small towns, Mumbai chawls, highways, hill stations,
  railway stations, villages, old bungalows, hospitals, monsoon nights, etc.
- Image prompts are in English, describe one clear frame each (subject, action, setting,
  camera angle, lighting), vary shot types (wide, close-up, over-the-shoulder, object detail,
  silhouette), and never ask for text, signs, letters or gore.
- Clips: pick self-contained, high-tension stretches of 45-75 seconds (about 3-5 scenes) that
  end on a cliffhanger, ranked best first, not overlapping where possible.
"""


def _prompt(history: list[str], idea: str = "", episode: dict | None = None) -> str:
    s = CFG["story"]
    past = "\n".join(f"- {h}" for h in history[-s["history_size"]:]) or "- (none yet)"
    extra = idea or ("" if episode else s.get("theme_hint")) or ""
    if episode and episode.get("script"):
        scenes = "\n".join(f"Scene {i}: {t.strip()}" for i, t in enumerate(episode["script"], 1))
        return f"""{series.prompt_block(episode)}

THE NARRATION IS ALREADY WRITTEN. Do not rewrite it. Return exactly {len(episode['script'])} scenes,
copying each scene's narration below verbatim, and write the shots, characters, clips and metadata.
- Exactly {s['images_per_scene']} shots per scene that illustrate that scene's narration.
- Exactly {s['clip_candidates']} clip candidates.

{scenes}
"""
    if episode:
        return f"""{series.prompt_block(episode)}

Length and format:
- Total narration: about {s['target_words']} Hindi words (about 5 minutes spoken).
- Exactly {s['scenes']} scenes, about {s['target_words'] // s['scenes']} words each.
- Exactly {s['images_per_scene']} shots per scene.
- Exactly {s['clip_candidates']} clip candidates.
{f'- Extra direction from the producer for this version: {extra}' if extra else ''}
"""
    return f"""Write today's story.
- Total narration: about {s['target_words']} Hindi words (about 5 minutes spoken).
- Exactly {s['scenes']} scenes, about {s['target_words'] // s['scenes']} words each.
- Exactly {s['images_per_scene']} shots per scene.
- 2 to 4 main characters.
- Exactly {s['clip_candidates']} clip candidates.
{f'- Story idea / direction from the producer: {extra}' if extra else ''}

Do NOT repeat or closely resemble these past plots:
{past}
"""


def _history() -> list[str]:
    p = STATE_DIR / "history.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def add_history(logline: str) -> None:
    h = _history() + [logline]
    (STATE_DIR / "history.json").write_text(
        json.dumps(h[-200:], ensure_ascii=False, indent=1), encoding="utf-8")


def _validate(story: Story, episode: dict | None = None) -> list[str]:
    s = CFG["story"]
    problems = []
    if episode and episode.get("script") and len(story.scenes) != len(episode["script"]):
        problems.append(f"expected {len(episode['script'])} scenes, got {len(story.scenes)}")
    words = sum(len(sc.narration.split()) for sc in story.scenes)
    fixed = bool(episode and episode.get("script"))  # narration gets replaced anyway
    if not fixed and words < s["target_words"] * 0.8:
        problems.append(f"narration too short ({words} words)")
    if len(story.scenes) < s["scenes"] - 3:
        problems.append(f"too few scenes ({len(story.scenes)})")
    if len(story.clips) < 3:
        problems.append("fewer than 3 clips")
    ids = {c.id for c in story.characters}
    for sc in story.scenes:
        if not sc.shots:
            problems.append("scene without shots")
        for sh in sc.shots:
            sh.characters = [c for c in sh.characters if c in ids]
    return problems


def write_story(idea: str = "", episode: dict | None = None) -> dict:
    client = genai.Client(api_key=env("GEMINI_API_KEY"))
    models = [CFG["story"]["text_model"], CFG["story"]["fallback_model"]]
    last_err = None
    for attempt in range(4):
        model = models[min(attempt // 2, 1)]
        try:
            log.info("Writing story with %s (attempt %d)", model, attempt + 1)
            resp = client.models.generate_content(
                model=model,
                contents=_prompt(_history(), idea, episode),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM,
                    response_mime_type="application/json",
                    response_schema=Story,
                    temperature=1.0,
                ),
            )
            story = resp.parsed if isinstance(resp.parsed, Story) else Story.model_validate_json(resp.text)
            problems = _validate(story, episode)
            if problems and attempt < 3:
                log.warning("Story rejected: %s", problems)
                last_err = problems
                continue
            data = story.model_dump()
            data["clips"] = [c for c in data["clips"]
                             if 1 <= c["start_scene"] <= c["end_scene"] <= len(data["scenes"])]
            if episode:
                _apply_fixed(data, episode)
                data["episode"] = episode["n"]
                series.finish_metadata(data, episode)
            return data
        except Exception as e:  # noqa: BLE001
            log.warning("Story generation error: %s", e)
            last_err = e
    raise RuntimeError(f"Could not write a story: {last_err}")


def _apply_fixed(data: dict, episode: dict) -> None:
    """Hand-written script and Shorts from series/episodes.yaml always win over Gemini."""
    script = episode.get("script")
    if script:
        shots = [sh for sc in data["scenes"] for sh in sc["shots"]] or [{"prompt": "", "characters": []}]
        per = CFG["story"]["images_per_scene"]
        scenes = data["scenes"]
        if len(scenes) != len(script):  # last resort: keep Gemini's shots, our text
            scenes = [{"narration": "", "shots": [shots[(i * per + j) % len(shots)] for j in range(per)]}
                      for i in range(len(script))]
        for sc, text in zip(scenes, script):
            sc["narration"] = " ".join(text.split())
        data["scenes"] = scenes
    fixed = [c for c in episode.get("clips") or [] if c.get("start_scene")]
    for i, c in enumerate(fixed):
        base = data["clips"][i] if i < len(data["clips"]) else {"hook_text": "", "title": "", "end_question": ""}
        merged = {**base, **{k: v for k, v in c.items() if v}}
        if i < len(data["clips"]):
            data["clips"][i] = merged
        else:
            data["clips"].append(merged)
    t = episode.get("thumbnail") or {}
    if t.get("text"):
        data["thumbnail_text"] = t["text"]
    if t.get("prompt"):
        data["thumbnail_prompt"] = t["prompt"]


def all_shots(story: dict) -> list[dict]:
    """Flat list of shots with global 1-based numbers (the numbers you use in /redo)."""
    out, n = [], 0
    for si, sc in enumerate(story["scenes"]):
        for sh in sc["shots"]:
            n += 1
            out.append({"n": n, "scene": si + 1, **sh})
    return out
