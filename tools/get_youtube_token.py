"""Run ONCE on your own computer to get the YouTube refresh token.

1. Download your OAuth client JSON (type "Desktop app") from Google Cloud Console
   and save it next to this file as client_secret.json
2. pip install google-auth-oauthlib
3. python tools/get_youtube_token.py
4. Log in with the Google account that owns the YouTube channel and allow access.
5. Copy the three printed values into GitHub Secrets.
"""
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]

secret = Path(__file__).with_name("client_secret.json")
flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
print("\nAdd these as GitHub Secrets:\n")
print("YT_CLIENT_ID     =", creds.client_id)
print("YT_CLIENT_SECRET =", creds.client_secret)
print("YT_REFRESH_TOKEN =", creds.refresh_token)
