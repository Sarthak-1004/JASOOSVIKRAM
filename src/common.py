"""Shared helpers: config, paths, per-day state, git sync."""
import datetime as dt
import json
import logging
import os
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
WORK_DIR = ROOT / "work"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
TZ = ZoneInfo(CFG["channel"]["timezone"])

ITEMS = ["v", "s1", "s2", "s3"]  # long video + 3 shorts


def env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        raise SystemExit(f"Missing environment variable / GitHub secret: {name}")
    return val


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def resolve_date(arg: str) -> str:
    """'today' / 'tomorrow' (IST) or an explicit YYYY-MM-DD."""
    if arg == "today":
        return now().date().isoformat()
    if arg == "tomorrow":
        return (now().date() + dt.timedelta(days=1)).isoformat()
    dt.date.fromisoformat(arg)
    return arg


def work_dir(date: str) -> Path:
    d = WORK_DIR / date
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(date: str) -> Path:
    return STATE_DIR / f"{date}.json"


def load_state(date: str) -> dict:
    p = state_path(date)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"date": date, "status": "new", "items": {}}


def save_state(state: dict, push: bool = True, msg: str = "") -> None:
    STATE_DIR.mkdir(exist_ok=True)
    state_path(state["date"]).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if push:
        git_push(msg or f"state {state['date']}: {state['status']}")


def git_push(msg: str) -> None:
    """Commit state/ back to the repo when running in GitHub Actions."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    def run(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True)
    run("config", "user.name", "thriller-bot")
    run("config", "user.email", "thriller-bot@users.noreply.github.com")
    run("add", "state", "-f")
    if run("diff", "--cached", "--quiet").returncode == 0:
        return
    run("commit", "-m", msg)
    for _ in range(4):
        run("pull", "--rebase", "-X", "theirs")
        if run("push").returncode == 0:
            return
    log.warning("git push of state failed")


def git_pull() -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        subprocess.run(["git", "pull", "--rebase"], cwd=ROOT, capture_output=True)


def publish_times(date: str) -> dict:
    """item -> aware datetime (IST) when it should go public."""
    d = dt.date.fromisoformat(date)
    times = [CFG["schedule"]["long_video"], *CFG["schedule"]["shorts"]]
    out = {}
    for item, t in zip(ITEMS, times):
        h, m = map(int, t.split(":"))
        out[item] = dt.datetime.combine(d, dt.time(h, m), tzinfo=TZ)
    return out


def ffmpeg(*args: str) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd)}\n{r.stderr[-2000:]}")


def media_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    return float(r.stdout.strip())
