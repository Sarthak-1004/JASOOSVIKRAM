"""Series mode: fixed bible, ordered episode list, recurring characters, continuity."""
import json
import shutil
from pathlib import Path

import yaml

from .common import CFG, ROOT, STATE_DIR, log

SERIES_DIR = ROOT / "series"
PROGRESS = STATE_DIR / "series_progress.json"
REFS = STATE_DIR / "series_refs"


def enabled() -> bool:
    return bool(CFG.get("series", {}).get("enabled"))


def bible() -> dict:
    return yaml.safe_load((SERIES_DIR / "bible.yaml").read_text(encoding="utf-8"))


def episodes() -> list[dict]:
    """episodes.yaml, with hand-written scripts merged in from series/scripts/epNN.yaml."""
    eps = yaml.safe_load((SERIES_DIR / "episodes.yaml").read_text(encoding="utf-8"))
    for ep in eps:
        path = SERIES_DIR / "scripts" / f"ep{ep['n']:02d}.yaml"
        if path.exists():
            extra = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for k in ("script", "clips"):
                if extra.get(k):
                    ep[k] = extra[k]
    return eps


def _progress() -> dict:
    return json.loads(PROGRESS.read_text(encoding="utf-8")) if PROGRESS.exists() else \
        {"next": 1, "done": []}


def take_next_episode() -> dict | None:
    """Next episode in the list; advances the counter. None when the season is over."""
    prog = _progress()
    eps = {e["n"]: e for e in episodes()}
    ep = eps.get(prog["next"])
    if not ep:
        return None
    prog["next"] += 1
    PROGRESS.write_text(json.dumps(prog, ensure_ascii=False, indent=1), encoding="utf-8")
    return ep


def record_episode(n: int, title: str, summary: str) -> None:
    prog = _progress()
    prog["done"] = [d for d in prog["done"] if d["n"] != n] + [{"n": n, "title": title, "summary": summary}]
    PROGRESS.write_text(json.dumps(prog, ensure_ascii=False, indent=1), encoding="utf-8")


def recurring_ids() -> set[str]:
    return {c["id"] for c in bible().get("recurring_characters", [])}


def prompt_block(ep: dict) -> str:
    b = bible()
    rec = "\n".join(f"- id `{c['id']}`, {c['name']} (from Ep {c.get('from_episode', 1)}): "
                    f"{c['personality']} LOOK: {c['look']}"
                    for c in b["recurring_characters"] if c.get("from_episode", 1) <= ep["n"])
    prev = "\n".join(f"- Ep {d['n']} \"{d['title']}\": {d['summary']}"
                     for d in sorted(_progress()["done"], key=lambda d: d["n"])[-29:]) or "- (this is the first episode)"
    lines = [
        f"SERIES: {b['series_name']}. Episode {ep['n']} of {len(episodes())}.",
        f"Setting: {b['setting']}", f"Tone: {b['tone']}", f"Narrator: {b['narrator']}",
        "Series rules:", *[f"- {r}" for r in b["rules"]],
        "Viral storytelling rules (from what works on Hindi story YouTube):",
        *[f"- {r}" for r in b.get("viral_rules", [])],
        "SECRET season arc - for the writer only. Plant clues fairly but NEVER state or hint the "
        "identity in a way that gives it away before the finale:", b.get("secret_arc", "").strip(),
        "Recurring characters (reuse these exact ids and looks whenever they appear):", rec,
        "Previous episodes (keep continuity, especially the Black Lotus thread):", prev,
        "",
        f"THIS EPISODE adapts Arthur Conan Doyle's public-domain story \"{ep['source']}\" into the "
        "series world: Indian names and places, 1920s India, Vikram as the detective. Keep the "
        "original's core puzzle and solution, rewrite everything else in your own words.",
    ]
    if ep.get("title"):
        lines.append(f"Episode title (use exactly): {ep['title']}")
    if ep.get("setting"):
        lines.append(f"Episode setting: {ep['setting']}")
    if ep.get("note"):
        lines.append(f"Producer note: {ep['note']}")
    if ep.get("characters"):
        lines += ["Episode characters:", *[f"- {c}" for c in ep["characters"]]]
    if ep.get("beats"):
        lines += ["Follow these beats closely (timings are approximate):", *[f"- {x}" for x in ep["beats"]]]
    if ep.get("black_lotus"):
        lines.append(f"Black Lotus thread for this episode (plant it subtly, never reveal the identity): "
                     f"{ep['black_lotus']}")
    if ep.get("shorts"):
        lines += ["The FIRST clip candidates must be exactly these Shorts, in this order "
                  "(hook_text and the clip's last scene must match the described ending):",
                  *[f"{i}. {s}" for i, s in enumerate(ep["shorts"], 1)]]
    if ep.get("thumbnail"):
        t = ep["thumbnail"]
        lines.append(f"Thumbnail: thumbnail_text = \"{t.get('text', '')}\"; thumbnail_prompt = {t.get('prompt', '')}")
    lines.append(f"YouTube title format: {b['title_format'].format(episode_title='<title>', n=ep['n'])}")
    return "\n".join(lines)


def finish_metadata(story: dict, ep: dict) -> None:
    """Force the series title format and the source credit line."""
    b = bible()
    title = ep.get("title") or story["title"].split("|")[0].strip()
    story["episode_title"] = title
    story["title"] = b["title_format"].format(episode_title=title, n=ep["n"])[:100]
    credit = b["credit_line"].format(source=ep["source"])
    if credit not in story["description"]:
        story["description"] = f"{story['description']}\n\n{credit}"
    for c in story["clips"]:
        if b["series_name"] not in c["title"]:
            c["title"] = f"{c['title']} | {b['series_name']}"[:90]


# ---------------------------------------------------------------- recurring faces

def restore_ref(cid: str, dest: Path) -> bool:
    src = REFS / f"{cid}.jpg"
    if src.exists() and not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
    return dest.exists()


def keep_ref(cid: str, path: Path) -> None:
    if cid in recurring_ids() and not (REFS / f"{cid}.jpg").exists():
        REFS.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, REFS / f"{cid}.jpg")
        log.info("Saved permanent reference for recurring character %s", cid)
