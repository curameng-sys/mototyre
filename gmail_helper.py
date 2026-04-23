# gmail_helper.py  — Google OAuth (Gmail API), replaces SMTP version

import os, base64, threading, random, string
from datetime import timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

GMAIL_SCOPES     = ["https://www.googleapis.com/auth/gmail.send"]
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_CREDS_FILE = "credentials.json"
GMAIL_SENDER     = os.getenv("GMAIL_SENDER", "mototyre0505@gmail.com")
OTP_EXPIRY_MINS  = 2

_lock   = threading.Lock()
_svc_cache = None

def _get_service():
    global _svc_cache
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request(timeout=15))
        else:
            flow  = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        _svc_cache = None
    with _lock:
        if _svc_cache is None:
            _svc_cache = build("gmail", "v1", credentials=creds)
    return _svc_cache

def send_gmail_html(to: str, subject: str, html_body: str):
    """Send an HTML email via Gmail API (OAuth). Fire-and-forget thread."""
    msg = MIMEMultipart("alternative")
    msg["to"]      = to
    msg["from"]    = GMAIL_SENDER
    msg["subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    def _do():
        try:
            _get_service().users().messages().send(
                userId="me", body={"raw": raw}
            ).execute()
            print(f"[GMAIL] Sent to {to}: {subject}")
        except Exception as e:
            print(f"[GMAIL] Failed to {to}: {e}")

    threading.Thread(target=_do, daemon=True).start()

def send_otp_email(email: str, otp: str, purpose: str = "login"):
    configs = {
        "login":  ("Your MotoTyre login code",          "Sign-In Verification",    "complete your login"),
        "verify": ("Verify your MotoTyre email",         "Email Verification",      "verify your email address"),
        "reset":  ("Your MotoTyre password reset code",  "Password Reset",          "reset your password"),
    }
    subject, heading, action = configs.get(purpose, configs["login"])

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:32px;
                border:1px solid #e5e7eb;border-radius:8px;background:#ffffff;">
      <div style="text-align:center;margin-bottom:24px;">
        <span style="font-family:Georgia,serif;font-size:22px;font-weight:900;
                     letter-spacing:4px;color:#0c0d0f;">MOTO<span style="color:#ff0f0f;">TYRE</span></span>
      </div>
      <h2 style="color:#111827;font-size:18px;margin-bottom:8px;">{heading}</h2>
      <p style="color:#6b7280;font-size:14px;margin-bottom:20px;">
        Use the code below to {action}. It expires in <strong>{OTP_EXPIRY_MINS} minutes</strong>.
      </p>
      <div style="font-size:40px;font-weight:bold;letter-spacing:14px;color:#111827;
                  background:#f3f4f6;padding:24px;border-radius:8px;
                  text-align:center;margin:0 0 20px;">
        {otp}
      </div>
      <p style="color:#9ca3af;font-size:12px;margin:0;">
        If you didn't request this, you can safely ignore this email.
      </p>
    </div>"""

    send_gmail_html(email, subject, html)