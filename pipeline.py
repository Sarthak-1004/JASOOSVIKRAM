"""Hindi Thriller Bot - daily pipeline.

Stages (all times IST, D = publish day):
  prepare  D-1 06:00  write story, character sheets, submit image batch
  produce  D-1 15:00  collect images, narration, render video + 3 Shorts, send review to Telegram
  listen   D-1 19:25  answer your Telegram commands until 01:00 (approve / fix / new story)
  publish  D   08:00  deadline: upload everything not on hold, scheduled for D
  local    any        prepare + produce in one go without batch (for testing)

Usage: python pipeline.py <stage> [--date today|tomorrow|YYYY-MM-DD] [--no-telegram]
"""
import argparse
import datetime as dt
import os
import shutil
import sys
import time
import traceback

from src import images, models, providers, render, series, telegram as tg, tts
from src.common import CFG, ITEMS, STATE_DIR, gh_annotate, git_pull, load_state, log, now, publish_times, resolve_date, \
    save_state, work_dir
from src.story import add_history, all_shots, write_story

NO_TG = False
LABEL = {"v": "Main video", "s1": "Short 1", "s2": "Short 2", "s3": "Short 3"}


def notify(text: str, buttons=None) -> None:
    log.info("TELEGRAM: %s", text)
    if not NO_TG:
        tg.send(text, buttons)


# ================================================================ prepare

def stage_prepare(date: str, use_batch: bool, idea: str = "", force: bool = False) -> dict:
    state = load_state(date)
    wd = work_dir(date)
    if not force and state.get("story") and state.get("status") != "new":
        log.info("%s already prepared (status %s); skipping. Use --force to redo.", date, state["status"])
        return state
    _prune_refs(date)
    episode = None
    if series.enabled():
        # /newstory re-makes the same episode; a normal run takes the next one
        episode = next((e for e in series.episodes() if e["n"] == state.get("story", {}).get("episode")),
                       None) or series.take_next_episode()
        if episode is None:
            notify("🏁 All episodes in series/episodes.yaml are done. Add more episodes or set "
                   "series.enabled: false in config.yaml.")
            raise SystemExit("season finished")
    story = write_story(idea, episode)
    add_history(story["logline"])
    if episode:
        series.record_episode(episode["n"], story.get("episode_title", story["title"]), story["logline"])
    state.update({"story": story, "active_clips": [0, 1, 2], "next_clip": 3, "items": {},
                  "batch": None, "status": "story_ready"})
    log.info("Story: %s | %s", story["title"], story["logline"])
    images.make_characters(story, wd)
    keys = images.wanted_keys(story, state["active_clips"])
    if use_batch and images.batch_possible():
        try:
            images.submit_batch(story, state, wd, keys)
            state["status"] = "images_submitted"
        except Exception as e:  # noqa: BLE001 - produce makes the images with the provider chain
            log.error(traceback.format_exc())
            notify(f"⚠️ Gemini image batch not available ({str(e)[:200]}). "
                   "Images will be made at 15:00 with the next image provider instead.")
    save_state(state, msg=f"story for {date}")
    used = models.chosen()
    ep = f"Ep {story['episode']} " if story.get("episode") else ""
    warn = ""
    if "pro" in (used.get("image") or "") and providers.stats.get("gemini"):
        warn = ("\n⚠️ Only an expensive 'pro' image model worked with this key. Check its price "
                "before the next run, or change images.models in config.yaml.")
    notify(f"📝 {ep}story ready for {date}: {story['title']}\n"
           f"Text model: {used.get('text')}\nImages so far: {providers.summary()}{warn}\n"
           f"{sum(len(sc['shots']) for sc in story['scenes'])} images + thumbnail will be made at 15:00, "
           f"then the videos come here for review.")
    if not NO_TG:
        for c in story["characters"]:
            p = images.char_path(wd, c["id"])
            if p.exists():
                tg.send_photo(p, f"Character sheet: {c['name']} ({c['id']})")
    return state


def _prune_refs(date: str) -> None:
    """Keep committed character sheets for the last week only."""
    cutoff = (dt.date.fromisoformat(date) - dt.timedelta(days=7)).isoformat()
    for d in (STATE_DIR / "refs").glob("*"):
        if d.is_dir() and d.name < cutoff:
            shutil.rmtree(d)


# ================================================================ produce

def _item_meta(story: dict, clip: dict) -> dict:
    return {"title": f"{clip['title']} #shorts", "description":
            f"{clip['title']}\n\nपूरी कहानी देखें: {{LONG_URL}}\n\n#shorts #hindistory #thriller",
            "tags": story["tags"][:15] + ["shorts"]}


def render_all(state: dict, wd, items_to_render=ITEMS) -> None:
    story = state["story"]
    speech = tts.scene_audio(story["scenes"], wd)
    if "v" in items_to_render:
        render.render_long(story, speech, wd, state["date"])
    for i, item in enumerate(ITEMS[1:]):
        if item in items_to_render:
            clip = story["clips"][state["active_clips"][i]]
            render.render_short(story, speech, wd, state["date"], item, clip)
    for item in items_to_render:
        cur = state["items"].get(item, {})
        if item == "v":
            meta = {"title": story["title"], "description": story["description"], "tags": story["tags"]}
        else:
            meta = _item_meta(story, story["clips"][state["active_clips"][ITEMS.index(item) - 1]])
        # keep your manual title / description edits
        for k in ("title", "description"):
            if cur.get(f"{k}_edited"):
                meta[k] = cur[k]
        state["items"][item] = {**cur, **meta, "status": cur.get("status", "pending")}
    render.thumbnail(story, wd)


def stage_produce(date: str, force: bool = False) -> dict:
    state = load_state(date)
    if "story" not in state:
        raise SystemExit(f"No story for {date}; run prepare first")
    if not force and state.get("status") in ("review", "published"):
        log.info("%s already produced (status %s); skipping. Use --force to redo.", date, state["status"])
        return state
    wd = work_dir(date)
    story = state["story"]
    images.make_characters(story, wd)  # no-op if they exist; regenerates on a fresh runner
    images.make_thumbnail_art(story, wd)
    keys = images.wanted_keys(story, state["active_clips"])
    problems = images.ensure_images(story, state, wd, keys)
    render_all(state, wd)
    state["status"] = "review"
    state["media_run_id"] = os.environ.get("GITHUB_RUN_ID")
    save_state(state, msg=f"rendered {date}")
    send_review(state, wd, problems)
    return state


# ================================================================ review messages

def status_text(state: dict) -> str:
    times = publish_times(state["date"])
    icon = {"pending": "⏳", "held": "🛑", "uploaded": "✅"}
    lines = [f"🎬 {state['date']} — {state['story']['title']}",
             f"📖 {state['story']['logline']}", ""]
    for item in ITEMS:
        it = state["items"].get(item, {})
        lines.append(f"{icon.get(it.get('status'), '?')} {item} {LABEL[item]} @ {times[item]:%H:%M} — "
                     f"{it.get('title', '')}")
    return "\n".join(lines)


HELP = """Commands:
/approve — upload & schedule everything not on hold
/hold v s2 — don't publish these   ·  /unhold s2
/title v New title   ·  /desc s1 New description
/redo 12 37 — regenerate image numbers (see the grids), re-render
/recut s2 — use the next clip candidate for Short 2
/clip s2 6-9 — Short 2 = scenes 6 to 9
/newstory [idea] — throw away and make a new story (~45 min)
/status · /help
No reply by 08:00 → everything not on hold is auto-published."""


def review_buttons():
    return [[("✅ Approve all", "approve"), ("🛑 Hold all", "hold_all")],
            [("🔄 New story", "newstory"), ("ℹ️ Status", "status")]]


def send_review(state: dict, wd, problems=None, only=ITEMS) -> None:
    if NO_TG:
        log.info(status_text(state))
        return
    for item in only:
        src = wd / f"{item}.mp4"
        if src.exists():
            prev = render.preview(src, wd / f"{item}_preview.mp4", 480 if item == "v" else 854)
            tg.send_video(prev, f"{LABEL[item]}: {state['items'][item]['title']}")
    if "v" in only:
        for i, sheet in enumerate(render.contact_sheets(state["story"], wd), 1):
            tg.send_photo(sheet, f"Image grid {i} — use /redo <numbers>")
        thumb = wd / "thumb.jpg"
        if thumb.exists():
            tg.send_photo(thumb, "Thumbnail")
    warn = f"\n⚠️ Images reused from neighbours: {', '.join(problems)}" if problems else ""
    warn += f"\n🖼️ Image sources: {providers.summary()}"
    notify(status_text(state) + warn + "\n\n" + HELP, review_buttons())


# ================================================================ uploading

def upload_pending(state: dict, wd) -> None:
    from src import youtube
    times = publish_times(state["date"])
    long_url = ""
    if state["items"].get("v", {}).get("youtube_id"):
        long_url = f"https://youtu.be/{state['items']['v']['youtube_id']}"
    for item in ITEMS:
        it = state["items"].get(item)
        if not it or it["status"] != "pending":
            continue
        desc = it["description"].replace("{LONG_URL}", long_url or "चैनल पर")
        if CFG["channel"].get("description_footer"):
            desc += "\n\n" + CFG["channel"]["description_footer"]
        vid = youtube.upload(wd / f"{item}.mp4", it["title"], desc, it["tags"], times[item],
                             wd / "thumb.jpg" if item == "v" else None)
        it.update(status="uploaded", youtube_id=vid)
        if item == "v":
            long_url = f"https://youtu.be/{vid}"
        save_state(state, msg=f"uploaded {state['date']} {item}")
    lines = [f"{LABEL[i]}: https://youtu.be/{state['items'][i]['youtube_id']}"
             for i in ITEMS if state["items"].get(i, {}).get("youtube_id")]
    if CFG["youtube"]["api_audited"]:
        notify("📅 Scheduled on YouTube:\n" + "\n".join(lines))
    else:
        sched = "\n".join(f"{LABEL[i]} → {times[i]:%d %b %H:%M}" for i in ITEMS
                          if state["items"].get(i, {}).get("youtube_id"))
        notify("⬆️ Uploaded as PRIVATE (API audit not approved yet).\nPlease schedule in YouTube "
               f"Studio:\n{sched}\n\n" + "\n".join(lines))
    if all(state["items"][i]["status"] != "pending" for i in state["items"]):
        state["status"] = "published"
        save_state(state, msg=f"published {state['date']}")


# ================================================================ commands

def _items(args: list[str]) -> list[str]:
    return [a for a in args if a in ITEMS] or []


def handle(cmd: str, args: list[str], rest: str, state: dict, wd, allow_heavy: bool) -> bool:
    """Returns True when the listener can stop (everything uploaded)."""
    story = state["story"]
    if cmd in ("help", "start"):
        notify(HELP)
    elif cmd == "status":
        notify(status_text(state))
    elif cmd == "approve":
        notify("⬆️ Uploading to YouTube…")
        upload_pending(state, wd)
        return state["status"] == "published"
    elif cmd in ("hold", "hold_all", "unhold"):
        targets = ITEMS if cmd == "hold_all" else _items(args)
        for i in targets:
            it = state["items"][i]
            if it["status"] != "uploaded":
                it["status"] = "pending" if cmd == "unhold" else "held"
        save_state(state)
        notify(status_text(state))
    elif cmd in ("title", "desc"):
        if not args or args[0] not in ITEMS:
            notify("Usage: /title v New title")
            return False
        field = "title" if cmd == "title" else "description"
        text = (rest.split(maxsplit=2) + ["", ""])[2]
        state["items"][args[0]].update({field: text, f"{field}_edited": True})
        save_state(state)
        notify(f"✏️ {LABEL[args[0]]} {field} updated.")
    elif not allow_heavy:
        notify(f"⏰ Too late for /{cmd} — publishing deadline reached.")
    elif cmd == "redo":
        nums = sorted({int(a) for a in args if a.isdigit()})
        total = len(all_shots(story))
        nums = [n for n in nums if 1 <= n <= total]
        if not nums:
            notify(f"Usage: /redo 12 37  (1-{total})")
            return False
        notify(f"🖼️ Regenerating images {nums}… (~15-25 min)")
        keys = []
        for n in nums:
            for kind in ("h", "v"):
                p = images.img_path(wd, images.key(kind, n))
                if kind == "h" or p.exists():
                    p.unlink(missing_ok=True)
                    keys.append(images.key(kind, n))
        failed = images.generate_sync(story, wd, keys)
        images.fill_missing(story, wd)
        scenes = {all_shots(story)[n - 1]["scene"] for n in nums}
        redo = ["v"] + [ITEMS[i + 1] for i, ci in enumerate(state["active_clips"])
                        if scenes & set(range(story["clips"][ci]["start_scene"],
                                              story["clips"][ci]["end_scene"] + 1))]
        redo = [i for i in redo if state["items"][i]["status"] != "uploaded"]
        _rerender(state, wd, redo, failed)
    elif cmd in ("recut", "clip"):
        if not args or args[0] not in ITEMS[1:]:
            notify("Usage: /recut s2   or   /clip s2 6-9")
            return False
        slot = ITEMS.index(args[0]) - 1
        if cmd == "recut":
            if state["next_clip"] >= len(story["clips"]):
                notify("No more clip candidates. Use /clip s2 <start>-<end> with scene numbers.")
                return False
            state["active_clips"][slot] = state["next_clip"]
            state["next_clip"] += 1
        else:
            try:
                a, b = map(int, args[1].split("-"))
                assert 1 <= a <= b <= len(story["scenes"])
            except Exception:  # noqa: BLE001
                notify(f"Usage: /clip s2 6-9 (scenes 1-{len(story['scenes'])})")
                return False
            old = story["clips"][state["active_clips"][slot]]
            story["clips"].append({**old, "start_scene": a, "end_scene": b})
            state["active_clips"][slot] = len(story["clips"]) - 1
        notify(f"✂️ Re-cutting {LABEL[args[0]]}…")
        if CFG["images"]["vertical_for_shorts"]:
            want = images.wanted_keys(story, [state["active_clips"][slot]])
            images.generate_sync(story, wd, [k for k in want if k.startswith("v")
                                             and not images.img_path(wd, k).exists()])
        state["items"][args[0]].pop("title_edited", None)
        _rerender(state, wd, [args[0]], [])
    elif cmd == "newstory":
        notify("🔄 Writing a brand new story… (~45-60 min)")
        shutil.rmtree(wd, ignore_errors=True)
        wd.mkdir(parents=True)
        shutil.rmtree(images.chars_dir(wd), ignore_errors=True)
        new = stage_prepare(state["date"], use_batch=False, idea=rest.partition(" ")[2], force=True)
        state.clear()
        state.update(new)
        images.ensure_images(state["story"], state, wd,
                             images.wanted_keys(state["story"], state["active_clips"]))
        render_all(state, wd)
        state["status"] = "review"
        _mark_media(state)
        send_review(state, wd)
    else:
        notify("Unknown command.\n" + HELP)
    return False


def _rerender(state: dict, wd, items_to_render: list[str], failed: list[str]) -> None:
    render_all(state, wd, items_to_render)
    _mark_media(state)
    send_review(state, wd, failed, only=items_to_render)


def _mark_media(state: dict) -> None:
    state["media_run_id"] = os.environ.get("GITHUB_RUN_ID") or state.get("media_run_id")
    save_state(state, msg=f"re-rendered {state['date']}")


def _drain(state: dict, wd, offset, allow_heavy: bool, timeout: int):
    """Process one long-poll worth of Telegram updates. Returns (offset, done)."""
    for u in tg.updates(offset, timeout=timeout):
        offset = u["update_id"] + 1
        if "callback_query" in u:
            cb = u["callback_query"]
            if str(cb["message"]["chat"]["id"]) != tg.chat_id():
                continue
            tg.answer_callback(cb["id"], "OK")
            text = "/" + cb["data"]
        else:
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != tg.chat_id():
                continue
            text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        parts = text[1:].split()
        cmd = parts[0].split("@")[0].lower()
        log.info("Command: %s", text)
        try:
            if handle(cmd, parts[1:], text[1:], state, wd, allow_heavy):
                tg.updates(offset, timeout=0)  # acknowledge
                return offset, True
        except Exception as e:  # noqa: BLE001
            log.error(traceback.format_exc())
            notify(f"❌ /{cmd} failed: {e}")
    return offset, False


# ================================================================ listen / publish

def stage_listen(date: str) -> None:
    git_pull()
    state = load_state(date)
    wd = work_dir(date)
    if state.get("status") not in ("review",):
        notify(f"⚠️ Nothing to review for {date} (status: {state.get('status')}).")
        return
    hh, mm = map(int, CFG["review"]["listen_until"].split(":"))
    start = now()
    deadline = start.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if deadline <= start:
        deadline += dt.timedelta(days=1)
    deadline = min(deadline, start + dt.timedelta(hours=5, minutes=20))
    notify(f"👀 Review window open until {deadline:%H:%M}. Send /help for commands.")
    offset, done = None, False
    while not done and now() < deadline:
        offset, done = _drain(state, wd, offset, allow_heavy=True, timeout=50)
    if not done:
        notify(f"🌙 Review window closed. Auto-publish runs at 08:00 for anything not on hold.\n"
               + status_text(state))


def stage_publish(date: str) -> None:
    git_pull()
    state = load_state(date)
    wd = work_dir(date)
    if state.get("status") == "published":
        log.info("Already published")
        return
    if state.get("status") != "review" or not (wd / "v.mp4").exists():
        notify(f"❌ Auto-publish for {date}: no rendered videos found (status {state.get('status')}). "
               "Check the GitHub Actions logs.")
        sys.exit(1)
    offset, done = None, False
    for _ in range(3):  # pick up quick commands sent after the review window
        offset, done = _drain(state, wd, offset, allow_heavy=False, timeout=1)
        if done:
            return
    if CFG["review"]["auto_publish"]:
        notify("⏰ Deadline reached — auto-publishing everything not on hold.")
        upload_pending(state, wd)


# ================================================================ check

def stage_check() -> None:
    """Test every key/service once and report on Telegram + the GitHub run page."""
    from google import genai
    lines, files = [], []
    wd = work_dir("check")

    def step(name, fn):
        try:
            lines.append(f"✅ {name}: {fn()}")
        except (Exception, SystemExit) as e:  # noqa: BLE001 - SystemExit = missing secret
            log.error(traceback.format_exc())
            lines.append(f"❌ {name}: {type(e).__name__}: {str(e)[:400]}")

    def gemini_text():
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        r = models.run("text", client, lambda m: client.models.generate_content(
            model=m, contents="Reply with exactly one word: OK"))
        return f"{models.chosen()['text']} replied '{(r.text or '').strip()[:20]}'"
    step("Gemini text", gemini_text)

    prompt = (CFG["images"]["style_prompt"].strip() + " " + series.bible().get("style_extra", "") +
              "\n\nFrame: a crumbling Banaras haveli at midnight, one oil lantern glowing in a window, "
              "river mist. No text.")
    for name in providers._order():
        def one(name=name):
            fn = {"gemini": providers._gemini, "cloudflare": providers._cloudflare,
                  "pollinations": providers._pollinations}[name]
            data = fn(prompt, [], "16:9")
            if not data:
                raise RuntimeError("no image returned")
            out = wd / f"{name}.jpg"
            images._save(data, out)
            files.append((out, f"Test image from {name}"))
            return "image OK"
        step(f"Images via {name}", one)

    def sarvam():
        out = wd / "voice.wav"
        out.write_bytes(tts._speak("नमस्ते, मैं सावित्री हूँ। ये कहानी है जासूस विक्रम की।"))
        files.append((out, "Voice test (Sarvam)"))
        return f"voice OK ({CFG['voice']['speaker']})"
    step("Sarvam voice", sarvam)

    def youtube():
        from src import youtube as yt
        items = yt.service().channels().list(part="snippet", mine=True).execute().get("items", [])
        if not items:
            raise RuntimeError("login works but this Google account has no YouTube channel")
        sn = items[0]["snippet"]
        return f"logged in to channel '{sn.get('title')}' ({sn.get('customUrl', 'no handle')})"
    step("YouTube", youtube)

    report = "🔧 Setup check\n" + "\n".join(lines)
    gh_annotate("notice", "setup check", report)
    notify(report)
    if not NO_TG:
        for path, cap in files:
            (tg.send_audio if path.suffix == ".wav" else tg.send_photo)(path, cap)


# ================================================================ main

def main() -> None:
    global NO_TG
    p = argparse.ArgumentParser()
    p.add_argument("stage", choices=["prepare", "produce", "listen", "publish", "local", "check"])
    p.add_argument("--date", default="tomorrow")
    p.add_argument("--idea", default="")
    p.add_argument("--no-telegram", action="store_true")
    p.add_argument("--force", action="store_true", help="redo a stage that already ran for this date")
    a = p.parse_args()
    NO_TG = a.no_telegram or not os.environ.get("TELEGRAM_BOT_TOKEN")
    date = resolve_date(a.date)
    log.info("Stage %s for %s", a.stage, date)
    t0 = time.time()
    try:
        if a.stage == "prepare":
            stage_prepare(date, CFG["images"]["use_batch"], a.idea, force=a.force)
        elif a.stage == "produce":
            stage_produce(date, force=a.force)
        elif a.stage == "listen":
            stage_listen(date)
        elif a.stage == "publish":
            stage_publish(date)
        elif a.stage == "check":
            stage_check()
        elif a.stage == "local":
            stage_prepare(date, use_batch=False, idea=a.idea, force=True)
            stage_produce(date, force=True)
    except Exception as e:
        log.error(traceback.format_exc())
        gh_annotate("error", f"{a.stage} failed", f"{e}\n\n{traceback.format_exc()[-2500:]}")
        notify(f"❌ {a.stage} failed for {date}: {str(e)[:900]}")
        raise
    log.info("Done in %.1f min", (time.time() - t0) / 60)


if __name__ == "__main__":
    main()
