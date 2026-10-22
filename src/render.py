"""Video rendering: Ken Burns image segments, burned Hindi subtitles, music, Shorts, previews."""
import hashlib
import os
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .common import CFG, ROOT, ffmpeg, log, media_duration
from .images import blur_vertical, img_path, key
from .story import all_shots
from .tts import concat_wavs, split_sentences

FPS = CFG["video"]["fps"]
FONT = CFG["video"]["font"]
FONTS_DIR = ROOT / "assets" / "fonts"
MOVES = ["zin", "zout", "pan_r", "pan_l", "zin", "pan_up"]


# ---------------------------------------------------------------- timing

def scene_spans(speech: list[float], scene_nums: list[int]) -> list[tuple[int, float, float, float]]:
    """(scene, start, speech_end, end) on a timeline that starts at 0 for the given scenes."""
    gap, t, out = CFG["voice"]["scene_gap"], 0.0, []
    for s in scene_nums:
        d = speech[s - 1]
        out.append((s, t, t + d, t + d + gap))
        t += d + gap
    return out


def image_timeline(story: dict, spans, kind: str, wd: Path) -> list[tuple[Path, float, float]]:
    shots_by_scene: dict[int, list[dict]] = {}
    for sh in all_shots(story):
        shots_by_scene.setdefault(sh["scene"], []).append(sh)
    tl = []
    for scene, start, _, end in spans:
        shots = shots_by_scene[scene]
        step = (end - start) / len(shots)
        for i, sh in enumerate(shots):
            tl.append((_frame_image(wd, kind, sh["n"]), start + i * step, start + (i + 1) * step))
    return tl


def _frame_image(wd: Path, kind: str, n: int) -> Path:
    if kind == "h":
        return img_path(wd, key("h", n))
    v = img_path(wd, key("v", n))
    if v.exists():
        return v
    vb = wd / "img" / f"vb-{n:03d}.jpg"
    if not vb.exists():
        blur_vertical(img_path(wd, key("h", n)), vb)
    return vb


def subtitle_events(story: dict, spans, max_words: int) -> list[tuple[float, float, str]]:
    ev = []
    for scene, start, speech_end, _ in spans:
        text = story["scenes"][scene - 1]["narration"]
        pieces = []
        for s in split_sentences(text):
            words = s.split()
            for i in range(0, len(words), max_words):
                pieces.append(" ".join(words[i:i + max_words]))
        total = sum(len(p) for p in pieces) or 1
        t = start
        for p in pieces:
            d = (speech_end - start) * len(p) / total
            ev.append((t, t + d, p))
            t += d
    return ev


# ---------------------------------------------------------------- ASS subtitles

def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def write_ass(path: Path, size: tuple[int, int], styles: list[str],
              events: list[tuple[str, float, float, str]]) -> None:
    w, h = size
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
""" + "\n".join(styles) + """

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 0,{_ts(a)},{_ts(b)},{st},,0,0,0,,{txt.replace(chr(10), r'\N')}"
             for st, a, b, txt in events]
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def _style(name, size, align, margin_v, primary="&H00FFFFFF", outline=4, box=False):
    border, back = (3, "&H96000000") if box else (1, "&H64000000")
    return (f"Style: {name},{FONT},{size},{primary},&H000000FF,&H00000000,{back},-1,0,0,0,"
            f"100,100,0,0,{border},{outline},1,{align},60,60,{margin_v},1")


def _subs_filter(ass: Path) -> str:
    p = str(ass).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    # complex shaping is required for Devanagari conjuncts and the ि matra
    f = f"ass=filename='{p}':shaping=complex"
    if FONTS_DIR.exists() and any(FONTS_DIR.iterdir()):
        f += f":fontsdir='{FONTS_DIR}'"
    return f


# ---------------------------------------------------------------- segments

def _zoompan(move: str, frames: int, size: tuple[int, int]) -> str:
    n = max(frames - 1, 1)
    c = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    z = {
        "zin": f"z='1.0+0.12*on/{n}':{c}",
        "zout": f"z='1.12-0.12*on/{n}':{c}",
        "pan_r": f"z='1.12':x='(iw-iw/zoom)*on/{n}':y='ih/2-(ih/zoom/2)'",
        "pan_l": f"z='1.12':x='(iw-iw/zoom)*(1-on/{n})':y='ih/2-(ih/zoom/2)'",
        "pan_up": f"z='1.12':x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*(1-on/{n})'",
    }[move]
    w, h = size
    return (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
            f"zoompan={z}:d={frames}:s={w}x{h}:fps={FPS},format=yuv420p")


def _segment(img: Path, out: Path, frames: int, size, move: str) -> None:
    ffmpeg("-i", str(img), "-vf", _zoompan(move, frames, size), "-frames:v", str(frames),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-threads", "2", str(out))


def render_slideshow(tl, size, out: Path, tag: str) -> None:
    sdir = out.parent / f"seg_{tag}"
    sdir.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(tag)
    jobs = []
    for i, (img, a, b) in enumerate(tl):
        frames = max(round(b * FPS) - round(a * FPS), 1)
        jobs.append((img, sdir / f"{i:03d}.mp4", frames, size, rnd.choice(MOVES)))
    workers = max(1, (os.cpu_count() or 2) // 2)
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(lambda j: _segment(*j), jobs))
    lst = sdir / "list.txt"
    lst.write_text("".join(f"file '{j[1].name}'\n" for j in jobs))
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out))


def _music(date: str) -> Path | None:
    mdir = ROOT / "assets" / "music"
    files = sorted(p for p in mdir.iterdir() if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg"))
    if not files:
        return None
    return files[int(hashlib.md5(date.encode()).hexdigest(), 16) % len(files)]


def finalize(video: Path, voice: Path, ass: Path, out: Path, date: str, crf: int) -> None:
    music = _music(date)
    args = ["-i", str(video), "-i", str(voice)]
    vf = f"[0:v]{_subs_filter(ass)}[v]"
    if music:
        args += ["-stream_loop", "-1", "-i", str(music)]
        af = (f"[1:a]aresample=48000[nar];[2:a]aresample=48000,volume={CFG['video']['music_volume']}[mus];"
              f"[nar][mus]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    else:
        af = "[1:a]aresample=48000[a]"
    ffmpeg(*args, "-filter_complex", f"{vf};{af}", "-map", "[v]", "-map", "[a]",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
           "-t", f"{media_duration(voice):.3f}", str(out))


# ---------------------------------------------------------------- public API

def render_long(story: dict, speech: list[float], wd: Path, date: str) -> Path:
    spans = scene_spans(speech, list(range(1, len(story["scenes"]) + 1)))
    size = (1920, 1080)
    tl = image_timeline(story, spans, "h", wd)
    raw = wd / "long_raw.mp4"
    render_slideshow(tl, size, raw, "long")
    voice = wd / "audio" / "long.wav"
    concat_wavs([wd / "audio" / f"scene_{s:02d}.wav" for s, *_ in spans], voice)
    ass = wd / "long.ass"
    write_ass(ass, size, [_style("Sub", 58, 2, 60)],
              [("Sub", a, b, t) for a, b, t in subtitle_events(story, spans, 9)])
    out = wd / "v.mp4"
    finalize(raw, voice, ass, out, date, CFG["video"]["long_crf"])
    log.info("Long video: %s (%.0fs)", out, media_duration(out))
    return out


def render_short(story: dict, speech: list[float], wd: Path, date: str, item: str, clip: dict) -> Path:
    scenes = list(range(clip["start_scene"], clip["end_scene"] + 1))
    spans = scene_spans(speech, scenes)
    total = spans[-1][3]
    size = (1080, 1920)
    tl = image_timeline(story, spans, "v", wd)
    raw = wd / f"{item}_raw.mp4"
    render_slideshow(tl, size, raw, item)
    voice = wd / "audio" / f"{item}.wav"
    concat_wavs([wd / "audio" / f"scene_{s:02d}.wav" for s in scenes], voice)
    end_text = CFG["video"]["shorts_end_text"]
    if clip.get("end_question"):
        end_text = clip["end_question"] + "\n" + end_text
    handle = CFG["channel"].get("handle") or ("@" + CFG["channel"]["name"].replace(" ", ""))
    if CFG["channel"]["name"]:
        end_text += "\n" + handle
    end_at = max(total - 3.0, total * 0.7)
    events = [("Hook", 0, end_at, clip["hook_text"]), ("End", end_at, total + 1, end_text)]
    events += [("Cap", a, b, t) for a, b, t in subtitle_events(story, spans, 4)]
    ass = wd / f"{item}.ass"
    write_ass(ass, size, [
        _style("Cap", 78, 5, 0, outline=6),
        _style("Hook", 66, 8, 230, primary="&H0000E5FF", box=True, outline=14),
        _style("End", 66, 8, 230, primary="&H0000E5FF", box=True, outline=14),
    ], events)
    out = wd / f"{item}.mp4"
    finalize(raw, voice, ass, out, date, 21)
    log.info("Short %s: %s (%.0fs)", item, out, media_duration(out))
    return out


def preview(src: Path, out: Path, height: int) -> Path:
    """Small copy that fits Telegram's 50 MB bot upload limit."""
    dur = media_duration(src)
    kbps = int(min(1500, (40 * 8192) / dur - 64))
    ffmpeg("-i", str(src), "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-preset", "veryfast",
           "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
           "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(out))
    return out


def fit_for_telegram(src: Path, out: Path, max_mb: float = 47.0) -> Path:
    """Same resolution, re-encoded just enough to fit Telegram's 50 MB bot upload limit."""
    if src.stat().st_size <= max_mb * 1024 * 1024:
        return src
    dur = media_duration(src)
    kbps = max(300, int((max_mb - 1.5) * 8192 / dur - 128))
    ffmpeg("-i", str(src), "-c:v", "libx264", "-preset", "medium", "-b:v", f"{kbps}k",
           "-maxrate:v", f"{int(kbps * 1.4)}k", "-bufsize:v", f"{kbps * 2}k", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out))
    return out


def contact_sheets(story: dict, wd: Path, per_sheet: int = 45, cols: int = 9) -> list[Path]:
    """Numbered grids of all images so you can say '/redo 12 37'."""
    shots = all_shots(story)
    tw, th = 320, 180
    font = ImageFont.load_default(size=34)
    out = []
    for si in range(0, len(shots), per_sheet):
        chunk = shots[si:si + per_sheet]
        rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * tw, rows * th), "black")
        d = ImageDraw.Draw(sheet)
        for i, sh in enumerate(chunk):
            p = img_path(wd, key("h", sh["n"]))
            x, y = (i % cols) * tw, (i // cols) * th
            if p.exists():
                sheet.paste(Image.open(p).convert("RGB").resize((tw - 4, th - 4)), (x + 2, y + 2))
            d.rectangle((x + 2, y + 2, x + 70, y + 44), fill="black")
            d.text((x + 8, y + 4), str(sh["n"]), fill="yellow", font=font)
        path = wd / f"sheet_{si // per_sheet + 1}.jpg"
        sheet.save(path, "JPEG", quality=85)
        out.append(path)
    return out


def thumbnail(story: dict, wd: Path) -> Path | None:
    n = min(max(int(story.get("thumbnail_shot") or 1), 1), len(all_shots(story)))
    src = wd / "img" / "thumb_art.jpg"
    if not src.exists():
        src = img_path(wd, key("h", n))
    if not src.exists():
        return None
    ass = wd / "thumb.ass"
    write_ass(ass, (1280, 720), [_style("T", 110, 1, 40, primary="&H0000E5FF", outline=8)],
              [("T", 0, 5, story["thumbnail_text"])])
    out = wd / "thumb.jpg"
    ffmpeg("-i", str(src), "-vf", f"scale=1280:720:force_original_aspect_ratio=increase,"
           f"crop=1280:720,{_subs_filter(ass)}", "-frames:v", "1", "-q:v", "3", str(out))
    return out
