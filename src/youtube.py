"""YouTube upload + scheduling via the YouTube Data API v3."""
import datetime as dt
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .common import CFG, env, log

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]


def service():
    creds = Credentials(
        token=None, refresh_token=env("YT_REFRESH_TOKEN"),
        client_id=env("YT_CLIENT_ID"), client_secret=env("YT_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(path: Path, title: str, description: str, tags: list[str],
           publish_at: dt.datetime | None, thumb: Path | None = None) -> str:
    yt = service()
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
              "containsSyntheticMedia": bool(CFG["youtube"]["synthetic_media"])}
    if publish_at and CFG["youtube"]["api_audited"]:
        earliest = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)
        when = max(publish_at.astimezone(dt.timezone.utc), earliest)
        status["publishAt"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "snippet": {"title": title[:100], "description": description[:4900],
                    "tags": tags[:30], "categoryId": CFG["youtube"]["category_id"],
                    "defaultLanguage": "hi", "defaultAudioLanguage": "hi"},
        "status": status,
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), chunksize=16 * 1024 * 1024,
                                                        resumable=True, mimetype="video/mp4"))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    log.info("Uploaded %s -> https://youtu.be/%s (%s)", path.name, vid, status.get("publishAt", "private"))
    if thumb and thumb.exists():
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb))).execute()
        except Exception as e:  # noqa: BLE001 - needs a phone-verified channel
            log.warning("Thumbnail upload failed (verify your channel by phone): %s", e)
    return vid
