"""One-time local OAuth helper. Prints the refresh token for GitHub Secrets."""
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
]

flow = InstalledAppFlow.from_client_config({"installed": {
    "client_id": os.environ["YOUTUBE_CLIENT_ID"],
    "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["http://localhost"],
}}, SCOPES)
credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
print("\nYOUTUBE_REFRESH_TOKEN=" + str(credentials.refresh_token))

