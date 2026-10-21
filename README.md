# Hindi Thriller Bot

This bot posts a daily 5-minute Hindi thriller story video on YouTube at **10:00**, plus 3 Shorts from it at **13:00, 18:00 and 21:00 IST**. You review everything on **Telegram** the evening before it goes out.

| Part | Tool |
|---|---|
| Story, scenes, image prompts, titles | Gemini 2.5 Pro / Flash |
| ~88 images in 2D Indian graphic-novel style (+ vertical images for Shorts) | Gemini 2.5 Flash Image ("Nano Banana") with the Batch API (50% off) |
| Female Hindi narration | Sarvam AI Bulbul |
| Video: zoom/pan on images, Hindi subtitles, music | FFmpeg |
| Runs every day | GitHub Actions (free) |
| Review and approval | Telegram bot |

## Daily timeline (IST, D = the day the videos are published)

| When | Workflow | What happens |
|---|---|---|
| D-1 06:00 | `1 Prepare` | Writes the story and character sheets, then submits the image batch |
| D-1 15:00 | `2 Produce` | Collects the images, records the voice, renders the video and 3 Shorts, and **sends you a review on Telegram** |
| D-1 19:25 → 01:00 | `3 Review listener` | Answers your Telegram commands (approve, fix, new story) |
| D 08:00 | `4 Auto-publish` | **Deadline.** If you haven't replied, everything not on hold is uploaded and scheduled |
| D 10:00 / 13:00 / 18:00 / 21:00 | YouTube | YouTube publishes each video at its scheduled time |

## Telegram commands

You can send these as soon as the review message arrives.

```
✅ button / /approve      upload and schedule everything not on hold
🛑 button / /hold v s2     don't publish these        /unhold s2
/title v New title        /desc s1 New description
/redo 12 37 54            regenerate these image numbers (shown on the image grids), then re-render
/recut s2                 use the next-best clip for Short 2
/clip s2 6-9              Short 2 = scenes 6 to 9
🔄 button / /newstory [idea]  throw the story away and make a new one (~45-60 min)
/status  /help
```

Put things on hold **before** you approve. Approving uploads everything else immediately.

---

## Setup (one time, ~1-2 hours)

### 1. GitHub repository
1. Create a new repository on GitHub and push this folder to it (see "Push this code" at the end).
2. Make it **public**. Public repositories get unlimited free GitHub Actions minutes. The review listener runs about 5.5 hours a day, which is more than a private repository's free 2,000 minutes a month allows. Your API keys stay secret either way, because they're stored as GitHub Secrets. The story text in `state/` will be visible to anyone.
3. Go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions**.

### 2. Gemini API key
1. Go to https://aistudio.google.com/apikey and create an API key.
2. Image generation through the API needs **billing turned on** for the Google Cloud project behind the key. Your free credits apply after that. Check them under AI Studio → Usage & Billing.
3. Cost per day is about 88 + ~45 images × ~$0.039, halved by the Batch API: **~$2.6/day**. The story text costs a few cents.

### 3. Sarvam AI key
Get it from https://dashboard.sarvam.ai → API keys. Each day uses about 4,000 characters of text-to-speech.

### 4. Telegram bot
1. In Telegram, open **@BotFather**, send `/newbot`, and follow the steps. Copy the **bot token**.
2. Open your new bot and send it "hi".
3. Run `python tools/telegram_chat_id.py <BOT_TOKEN>` and copy the **chat id** it prints.

### 5. YouTube API
1. Go to https://console.cloud.google.com, create a project (or use the Gemini one), and enable the **YouTube Data API v3**.
2. Go to **APIs & Services → OAuth consent screen**. Choose External, fill in the app name and your email, then click **Publish app** so it's **"In production"**. If you skip this, the login token expires every 7 days and the uploads stop.
3. Go to **Credentials → Create credentials → OAuth client ID → Desktop app**. Download the JSON file and save it as `tools/client_secret.json`. Don't commit it; `.gitignore` already blocks it.
4. On your own computer, run:
   ```
   pip install google-auth-oauthlib
   python tools/get_youtube_token.py
   ```
   Log in with the Google account that owns the channel. Copy the 3 values it prints.
5. **Apply for the API audit now:** https://support.google.com/youtube/contact/yt_api_form. Until Google approves it, videos uploaded through the API stay private. In the meantime the bot uploads them as private and sends you the times to schedule them by hand in YouTube Studio (about 2 minutes a day). After approval, set `api_audited: true` in `config.yaml`.
6. Verify your channel by phone at https://www.youtube.com/verify. Without this, custom thumbnails won't upload.

### 6. GitHub Secrets
Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `GEMINI_API_KEY` | from step 2 |
| `SARVAM_API_KEY` | from step 3 |
| `TELEGRAM_BOT_TOKEN` | from step 4 |
| `TELEGRAM_CHAT_ID` | from step 4 |
| `YT_CLIENT_ID` | from step 5 |
| `YT_CLIENT_SECRET` | from step 5 |
| `YT_REFRESH_TOKEN` | from step 5 |

### 7. Background music (optional but recommended)
Put a few dark or suspense instrumental `.mp3` files in `assets/music/`. Free sources: YouTube Studio → Audio Library, or Pixabay Music. Check that each track's license allows YouTube use. The bot picks one per day.

### 8. First test run
Go to **Actions** and run these one after another, each with date = `tomorrow`:
1. **1 Prepare story + images** (~3 min)
2. **2 Produce videos + send review** (~15-30 min; the batch may still be running, in which case the remaining images use the normal API)
3. Check your Telegram for the preview videos, image grids, thumbnail and buttons.
4. **3 Review listener** to try the commands. Use `/hold v s1 s2 s3` while testing so nothing gets published.

After that, everything runs on schedule by itself.

---

## Series mode (Season 1: जासूस विक्रम)
`series.enabled: true` in `config.yaml` makes the bot follow:
- `series/bible.yaml`: setting, tone, rules and recurring characters (Vikram, the Black Lotus).
- `series/episodes.yaml`: one episode per day, in order. Episode 1 (आधी रात की सीटी) is fully specified. Episodes 2-30 list only the public-domain Doyle source story, and Gemini adapts each one by itself. Add `title`, `setting`, `characters`, `beats`, `shorts` and `thumbnail` to any episode to control it fully, the way Episode 1 does.

How it works:
- **Vikram's face stays the same in every episode.** His reference image is made once and saved to `state/series_refs/vikram.jpg`. To use a face you like better, replace that file and commit it.
- **Continuity:** each new episode gets a summary of all earlier ones, so the Black Lotus thread builds up.
- **Titles:** the format is `<title> | जासूस विक्रम Ep N`. Every description gets a credit line to Doyle's original story.
- **Thumbnail:** the bot makes a dedicated thumbnail image from your thumbnail prompt.
- `/newstory` remakes the **same** episode. Running Prepare again for the same date also reuses that day's episode.
- **To restart the season**, delete `state/series_progress.json`.

## Settings (`config.yaml`)
- `channel.name`: once you choose it, it's shown on the Shorts end card.
- `schedule`: publish times.
- `story.theme_hint`: e.g. "haunted places in Rajasthan" for a themed week. You can also run Prepare by hand with an idea using `python pipeline.py prepare --idea "..."`.
- `images.style_prompt`: the art style. Add 1-2 style reference images later if you want an even more consistent look.
- `voice.speaker`: female voices are `anushka`, `manisha`, `vidya`, `arya`. Try each in the Sarvam playground.
- `images.vertical_for_shorts: false` uses blurred-background frames for Shorts instead of extra vertical images, which is about 30% cheaper.

## Run locally (optional)
You need Python 3.11+, ffmpeg and the Noto Sans Devanagari font.
```
pip install -r requirements.txt
export GEMINI_API_KEY=... SARVAM_API_KEY=...
python pipeline.py local --date tomorrow --no-telegram   # writes everything to work/<date>/
```

## Good to know
- **Monetization:** YouTube demonetizes mass-produced, repetitive content. Your daily review, a consistent narrator and characters, and real variety in stories all help. The bot sends Gemini a list of past plots so stories don't repeat. Uploads are marked as AI-generated (synthetic content).
- **GitHub schedules can start 5-20 minutes late.** The timeline has buffers for this.
- **YouTube quota:** about 1,600 units per upload out of 10,000 a day, so 4 uploads a day fit. Fixes don't use quota because nothing is uploaded until you approve or the deadline passes.
- **If something fails**, the bot sends you a Telegram message with the error. Full logs are in GitHub → Actions.

## Push this code
```
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```
