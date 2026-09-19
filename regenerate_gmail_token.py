"""Run this locally (needs a real browser) to re-authorize Gmail sending
after the refresh token expires/gets revoked. It overwrites gmail_token.json
with a fresh one, then prints the base64 string to paste into Render's
GMAIL_TOKEN_JSON env var on BOTH mototyre-customer and mototyre-admin
(they share the same Gmail account, so one fresh token covers both)."""

import base64
import os

from google_auth_oauthlib.flow import InstalledAppFlow

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
GMAIL_CREDS_FILE = "credentials.json"
GMAIL_TOKEN_FILE = "gmail_token.json"

if not os.path.exists(GMAIL_CREDS_FILE):
    raise SystemExit(f"Missing {GMAIL_CREDS_FILE} — need the OAuth client secret file first.")

print("Opening your browser to sign in and re-authorize Gmail sending...")
flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDS_FILE, GMAIL_SCOPES)
creds = flow.run_local_server(port=0)

with open(GMAIL_TOKEN_FILE, "w") as f:
    f.write(creds.to_json())
print(f"\nSaved fresh token to {GMAIL_TOKEN_FILE}.")

with open(GMAIL_TOKEN_FILE, "rb") as f:
    encoded = base64.b64encode(f.read()).decode()

print("\n" + "=" * 70)
print("COPY THE LINE BELOW AND PASTE IT AS GMAIL_TOKEN_JSON ON RENDER")
print("(both mototyre-customer AND mototyre-admin services)")
print("=" * 70)
print(encoded)
print("=" * 70)
