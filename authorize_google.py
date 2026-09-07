"""
One-time local Google Calendar authorization
---------------------------------------------
Run this ONCE, on your own computer (not on Railway), as whoever's Google
account already has edit access to the team calendars. It opens your
browser for a normal Google sign-in/consent screen, then prints a JSON blob
you paste into Railway as the GOOGLE_OAUTH_TOKEN_JSON variable.

This authorizes the bot to act as YOUR Google account (not a separate
service-account identity), so it only needs whatever access you already
have — no Google Workspace admin console access required.

Setup (one time):
1. Go to https://console.cloud.google.com/ -> create/select a project.
2. APIs & Services -> Library -> search "Google Calendar API" -> Enable.
3. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID.
   - If prompted, configure the OAuth consent screen first: User Type
     "External" is fine, fill in the required fields, and add yourself as a
     test user (this keeps the app private/unpublished, which is fine for
     this use case).
   - Application type: "Desktop app". Name it anything.
4. Download the resulting JSON file (the "Download JSON" button on the
   credential) and save it next to this script as client_secret.json.
5. pip install google-auth-oauthlib (already in requirements.txt)
6. Run: python authorize_google.py
7. A browser window opens -> sign in as the account that has edit access
   to your team calendars -> Allow.
8. Copy the printed JSON (one line) and paste it as the GOOGLE_OAUTH_TOKEN_JSON
   variable on Railway.

You only need to redo this if the token is ever revoked or stops working.
"""

import json
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CLIENT_SECRET_FILE = os.path.join(os.path.dirname(__file__), "client_secret.json")


def main():
    if not os.path.exists(CLIENT_SECRET_FILE):
        raise SystemExit(
            f"Couldn't find {CLIENT_SECRET_FILE}.\n"
            "Download your OAuth client credentials from Google Cloud Console "
            "(APIs & Services -> Credentials) and save them as client_secret.json "
            "next to this script."
        )

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    print("\nSuccess! Paste this entire line as the GOOGLE_OAUTH_TOKEN_JSON variable on Railway:\n")
    print(creds.to_json())


if __name__ == "__main__":
    main()
