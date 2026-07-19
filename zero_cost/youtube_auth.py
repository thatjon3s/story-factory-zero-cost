"""One-time local OAuth helper. Prints the refresh token for GitHub Secrets."""
import os
from contextlib import redirect_stdout
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
]

flow = InstalledAppFlow.from_client_config({"installed": {
    "client_id": os.getenv("YOUTUBE_CLIENT_ID", "310904108207-dekcvaqh4jvfrsbrc8g3ukoa6gstp279.apps.googleusercontent.com"),
    "client_secret": os.getenv("YOUTUBE_CLIENT_SECRET", ""),
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["http://localhost"],
}}, SCOPES)
work = Path(__file__).resolve().parents[1] / "work"
work.mkdir(exist_ok=True)
with (work / "oauth-url.txt").open("w", encoding="utf-8", buffering=1) as output, redirect_stdout(output):
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent", open_browser=False)
(work / "oauth-token.txt").write_text(str(credentials.refresh_token), encoding="utf-8")
