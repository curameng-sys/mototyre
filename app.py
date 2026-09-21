from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, redirect, url_for, flash, request, session, jsonify, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, date, time
from sqlalchemy import func
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from io import BytesIO
from security import (clean_str, clean_int, clean_float, is_valid_email, is_valid_phone, validate_otp_purpose,
    validate_return_kind, RETURN_OUTCOMES_BY_KIND, RETURN_REASONS, OPEN_RETURN_STATUSES)
import json as _json
from order_notifications import order_status_message, with_stamp, shipping_destination
from service_duration import (
    DEFAULT_DURATION_MIN, combine_services, combined_service_name, split_service_names,
    format_duration, minutes_to_ampm, minutes_to_hhmm, hhmm_to_minutes, slot_statuses,
    all_slot_starts, SHOP_CLOSE_MIN, compute_finish_minutes, mechanic_overlaps,
    mechanic_origin_note, validate_booking, MAX_BOOKINGS_PER_DAY, SLOT_GRANULARITY_MIN,
    add_working_days, MULTIDAY_MIN_DAYS, MULTIDAY_MAX_DAYS,
)
from gmail_helper import send_gmail_html as _send_gmail, send_otp_email
import os, uuid, random, string, base64, requests
from urllib.parse import quote, urlparse
import pymysql
import threading

# One source of truth for the database location — every other place that
# needs host/user/password (like the local-dev auto-create below) derives it
# from this instead of keeping a second, separately-maintained copy.
DATABASE_URL = os.getenv('DATABASE_URL', 'mysql+pymysql://root:@localhost:3306/mototyre').strip()

def _ensure_database():
    """Local-dev convenience only: creates the database if it doesn't exist
    yet. Skipped entirely for anything that isn't localhost — a hosted
    database (Aiven, etc.) already has its database created, requires SSL
    this quick connection doesn't bother with, and may not even grant
    CREATE DATABASE; there's nothing useful for this to do there, and
    trying would just cost a slow, doomed connection attempt on every
    startup. Also silently skipped if it fails for any other reason —
    this must never block startup."""
    parsed = urlparse(DATABASE_URL.replace('mysql+pymysql://', 'mysql://', 1))
    host = parsed.hostname or 'localhost'
    if host not in ('localhost', '127.0.0.1'):
        return
    try:
        conn = pymysql.connect(host=host, port=parsed.port or 3306,
                                user=parsed.username or 'root', password=parsed.password or '')
        try:
            db_name = (parsed.path or '/mototyre').lstrip('/')
            conn.cursor().execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

_ensure_database()

app = Flask(__name__)
_secure_cookies = os.getenv('SESSION_COOKIE_SECURE', 'false').lower() == 'true'
# A hosted database (Aiven, etc.) requires an SSL connection; a local XAMPP
# one typically isn't even configured for it — so this is opt-in by host,
# never something that has to be remembered as a separate setting.
_db_host = urlparse(DATABASE_URL.replace('mysql+pymysql://', 'mysql://', 1)).hostname or ''
_engine_options = {} if _db_host in ('localhost', '127.0.0.1', '') else {'connect_args': {'ssl': {'ssl': {}}}}
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY', 'mototyre-fixed-secret-key-xK9mP2qL7rZ3wN8vB4'),
    SESSION_COOKIE_NAME='mototyre_customer_session',
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=_secure_cookies,
    SESSION_COOKIE_DOMAIN=None,
    REMEMBER_COOKIE_SAMESITE='Lax',
    REMEMBER_COOKIE_SECURE=_secure_cookies,
    SQLALCHEMY_DATABASE_URI=DATABASE_URL,
    SQLALCHEMY_ENGINE_OPTIONS=_engine_options,
    SQLALCHEMY_TRACK_MODIFICATIONS=False
)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ── Gmail config ────────────────────────────────────────────────────────────
# Handled by gmail_helper.py (SMTP App Password — no OAuth, no re-auth needed)
# Set GMAIL_SENDER and GMAIL_APP_PASSWORD in your .env file.
OTP_EXPIRY_MINS = 2

# ── PayMongo config ──────────────────────────────────────────────────────────
PAYMONGO_SECRET_KEY = os.getenv("PAYMONGO_SECRET_KEY", "sk_test_qzA2hw8wmbB6AR46TSWYjKPV")
PAYMONGO_API_URL = "https://api.paymongo.com/v1"
# Only set BASE_URL in .env if PayMongo needs a fixed public host (e.g. a persistent
# tunnel). Otherwise leave it unset — create_gcash_payment() falls back to whatever
# whitelisted origin the customer is actually browsing from, so "Return to Merchant"
# doesn't break every time a dev tunnel URL changes or expires.
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

# The shop's real Facebook Page, linked from the landing page's contact
# section. Set via env var so it can be corrected without a code change.
FACEBOOK_PAGE_URL = os.getenv("FACEBOOK_PAGE_URL", "https://www.facebook.com/share/19Uyin8Xay/")

# Hosts the customer is allowed to be redirected back to after payment.
# ALLOWED_ORIGIN lets a deployed host (Render, etc.) add itself without a
# code change — set it in that service's environment to its own public URL.
ALLOWED_RETURN_ORIGINS = [
    "http://127.0.0.1:5000",
    "http://localhost:5000",
    "https://h4fjzg66-5000.jpe1.devtunnels.ms",
    "https://mototyre-customer.onrender.com",
]
_extra_origin = os.getenv("ALLOWED_ORIGIN", "").rstrip("/")
if _extra_origin and _extra_origin not in ALLOWED_RETURN_ORIGINS:
    ALLOWED_RETURN_ORIGINS.append(_extra_origin)

def safe_return_origin(origin):
    """Return a whitelisted origin for the post-payment redirect, or a safe default."""
    if origin:
        origin = origin.rstrip("/")
        if origin in ALLOWED_RETURN_ORIGINS:
            return origin
    return "http://127.0.0.1:5000"

def create_gcash_payment(amount, description, order_id=None, booking_id=None, origin=None):
    headers = {
        "Authorization": f"Basic {base64.b64encode(f'{PAYMONGO_SECRET_KEY}:'.encode()).decode()}",
        "Content-Type": "application/json"
    }

    # PayMongo's "Return to Merchant" button on the checkout page goes to this URL.
    # If BASE_URL isn't pinned in .env, use the whitelisted origin the customer is
    # actually browsing from right now instead of a stale/fixed host.
    return_origin = safe_return_origin(origin)
    base_url = BASE_URL or return_origin

    payload = {
        "data": {
            "attributes": {
                "line_items": [
                    {
                        "name": description,
                        "quantity": 1,
                        "amount": int(amount * 100),
                        "currency": "PHP"
                    }
                ],
                "payment_method_types": ["gcash"],
                "success_url": f"{base_url}/payment/success?order_id={order_id or ''}&booking_id={booking_id or ''}&origin={quote(return_origin, safe='')}&checkout_id={{CHECKOUT_SESSION_ID}}",
                "cancel_url": f"{base_url}/payment/failed?order_id={order_id or ''}&booking_id={booking_id or ''}&origin={quote(return_origin, safe='')}"
            }
        }
    }

    response = requests.post(
        f"{PAYMONGO_API_URL}/checkout_sessions",
        json=payload,
        headers=headers
    )

    if response.status_code == 200:
        data = response.json()
        return {
            "success": True,
            "checkout_url": data["data"]["attributes"]["checkout_url"],
            "checkout_id": data["data"]["id"]
        }
    else:
        print(f"PayMongo Error: {response.json()}")
        return {"success": False, "error": response.json()}


def send_order_receipt_email(order, override_email=None):
    try:
        if order.receipt_sent:
            return
        user = User.query.get(order.user_id)
        if not user:
            return

        recipient_email = override_email or user.email
        if not recipient_email:
            return

        items_rows = ""
        for item in order.items:
            product_name = item.product.name if item.product else "Product"
            subtotal = item.unit_price * item.quantity
            items_rows += f"""
            <tr>
              <td style="padding:10px 16px;border-bottom:1px solid #f3f4f6;color:#111827;font-size:14px;">{product_name}</td>
              <td style="padding:10px 16px;border-bottom:1px solid #f3f4f6;color:#6b7280;font-size:14px;text-align:center;">{item.quantity}</td>
              <td style="padding:10px 16px;border-bottom:1px solid #f3f4f6;color:#6b7280;font-size:14px;text-align:right;">&#8369;{item.unit_price:.2f}</td>
              <td style="padding:10px 16px;border-bottom:1px solid #f3f4f6;color:#111827;font-size:14px;text-align:right;font-weight:600;">&#8369;{subtotal:.2f}</td>
            </tr>"""

        delivery_label = "Shop Pickup — MotoTyre North Caloocan" if order.delivery_method == 'pickup' else "Ship to Address"
        delivery_detail = ""
        if order.delivery_method == 'ship' and order.ship_address:
            addr = order.ship_address.replace('\n', '<br>')
            delivery_detail = f'<p style="margin:4px 0 0;color:#6b7280;font-size:13px;">{addr}</p>'

        order_date = order.created_at.strftime("%B %d, %Y at %I:%M %p") if order.created_at else "—"
        order_num  = f"ORD-{order.id:03d}"

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;">
          <div style="background:#0c0d0f;padding:28px 32px;text-align:center;">
            <div style="font-family:Georgia,serif;font-size:26px;font-weight:900;letter-spacing:4px;color:#ffffff;">MOTO<span style="color:#ff0f0f;">TYRE</span></div>
            <div style="color:#6b7280;font-size:12px;letter-spacing:2px;text-transform:uppercase;margin-top:4px;">Order Receipt</div>
          </div>
          <div style="background:#f0fdf4;border-bottom:1px solid #bbf7d0;padding:16px 32px;display:flex;align-items:center;gap:12px;">
            <div style="width:36px;height:36px;background:#16a34a;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
              <span style="color:#fff;font-size:18px;line-height:1;">&#10003;</span>
            </div>
            <div>
              <div style="font-weight:700;color:#15803d;font-size:15px;">Payment Confirmed!</div>
              <div style="color:#4b5563;font-size:13px;margin-top:2px;">Your GCash payment was received successfully.</div>
            </div>
          </div>
          <div style="padding:24px 32px 0;">
            <table style="width:100%;border-collapse:collapse;">
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">Order Number</td>
                <td style="padding:6px 0;color:#111827;font-size:13px;font-weight:700;text-align:right;">{order_num}</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">Date</td>
                <td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">{order_date}</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">Customer</td>
                <td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">{user.fullname}</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">Payment</td>
                <td style="padding:6px 0;text-align:right;"><span style="background:#eff6ff;color:#1d4ed8;font-size:12px;font-weight:700;padding:3px 10px;border-radius:20px;border:1px solid #bfdbfe;">&#128241; GCash</span></td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">Delivery</td>
                <td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">
                  {delivery_label}
                  {delivery_detail}
                </td>
              </tr>
            </table>
          </div>
          <div style="padding:20px 32px 0;">
            <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#9ca3af;margin-bottom:8px;">Items Ordered</div>
            <table style="width:100%;border-collapse:collapse;border:1px solid #f3f4f6;border-radius:8px;overflow:hidden;">
              <thead>
                <tr style="background:#f9fafb;">
                  <th style="padding:10px 16px;text-align:left;font-size:11px;font-weight:700;color:#6b7280;letter-spacing:1px;text-transform:uppercase;">Product</th>
                  <th style="padding:10px 16px;text-align:center;font-size:11px;font-weight:700;color:#6b7280;letter-spacing:1px;text-transform:uppercase;">Qty</th>
                  <th style="padding:10px 16px;text-align:right;font-size:11px;font-weight:700;color:#6b7280;letter-spacing:1px;text-transform:uppercase;">Price</th>
                  <th style="padding:10px 16px;text-align:right;font-size:11px;font-weight:700;color:#6b7280;letter-spacing:1px;text-transform:uppercase;">Subtotal</th>
                </tr>
              </thead>
              <tbody>{items_rows}</tbody>
            </table>
          </div>
          <div style="padding:16px 32px;">
            <table style="width:100%;border-collapse:collapse;">
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">Subtotal</td>
                <td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">&#8369;{order.total_amount:.2f}</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">Shipping</td>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;text-align:right;">To be arranged</td>
              </tr>
              <tr style="border-top:2px solid #111827;">
                <td style="padding:12px 0 6px;color:#111827;font-size:16px;font-weight:900;letter-spacing:1px;">TOTAL</td>
                <td style="padding:12px 0 6px;color:#ff0f0f;font-size:22px;font-weight:900;text-align:right;">&#8369;{order.total_amount:.2f}</td>
              </tr>
            </table>
          </div>
          <div style="background:#f9fafb;border-top:1px solid #f3f4f6;padding:20px 32px;text-align:center;">
            <div style="color:#6b7280;font-size:12px;line-height:1.7;">
              <strong style="color:#111827;">MotoTyre North Caloocan</strong><br>
              Saranay Rd, Brgy. 171 Bagumbong, Caloocan City<br>
              &#128222; 0915 269 8366 &nbsp;|&nbsp; Mon–Sat 8:00 AM – 7:00 PM
            </div>
            <div style="margin-top:12px;color:#9ca3af;font-size:11px;">Thank you for choosing MotoTyre! &#127947;</div>
          </div>
        </div>"""

        _send_gmail(recipient_email, f"Your MotoTyre Receipt — {order_num}", html)
        order.receipt_sent = True
        db.session.commit()
        print(f"[receipt email] sent to {recipient_email} for {order_num}")
    except Exception as e:
        print(f"[receipt email] failed for order {order.id}: {e}")


def _booking_confirmation_facts(booking):
    """The facts a booking confirmation states, computed once so the email
    and the bell notification can never disagree: reference, date, the time
    set aside, the expected finish (a range for a drop-off — never an exact
    time), the mechanic and where they came from, services, and motorcycle."""
    ref = f'BKG-{booking.id:03d}'
    date_str = booking.date.strftime('%B %d, %Y')
    date_short = booking.date.strftime('%b %d')
    time_str = booking.time.strftime('%I:%M %p')

    if booking.is_multiday:
        time_label = f'{time_str} (drop-off intake)'
        release_from = add_working_days(booking.date, MULTIDAY_MIN_DAYS)
        release_to = add_working_days(booking.date, MULTIDAY_MAX_DAYS)
        finish_label = f"ready between {release_from.strftime('%b %d')} and {release_to.strftime('%b %d')} — we will call you"
    else:
        time_label = time_str
        finish_label = f"Done by {booking.end_time.strftime('%I:%M %p')}" if booking.end_time else 'To be confirmed'

    origin = mechanic_origin_note(booking.assigned_mechanic_name, booking.preferred_mechanic_name)
    mechanic_label = f'{booking.assigned_mechanic_name} ({origin})' if booking.assigned_mechanic_name \
        else "We'll assign one closer to your appointment"

    motorcycle = (booking.motorcycle_model or '').strip()
    if booking.motorcycle_plate:
        motorcycle = f'{motorcycle} ({booking.motorcycle_plate})' if motorcycle else booking.motorcycle_plate
    motorcycle = motorcycle or 'On file'

    return {
        'ref': ref, 'date_str': date_str, 'date_short': date_short, 'time_str': time_str,
        'time_label': time_label, 'finish_label': finish_label, 'mechanic_label': mechanic_label,
        'services': booking.service, 'motorcycle': motorcycle,
    }


def send_booking_confirmation_email(booking):
    """States the mechanic and where they came from, using the same phrasing
    the admin-side reassignment email uses (mechanic_origin_note), so a
    customer never sees two different explanations for the same fact."""
    try:
        user = User.query.get(booking.user_id)
        if not user or not user.email:
            return

        f = _booking_confirmation_facts(booking)

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;">
          <div style="background:#0c0d0f;padding:28px 32px;text-align:center;">
            <div style="font-family:Georgia,serif;font-size:26px;font-weight:900;letter-spacing:4px;color:#ffffff;">MOTO<span style="color:#ff0f0f;">TYRE</span></div>
            <div style="color:#6b7280;font-size:12px;letter-spacing:2px;text-transform:uppercase;margin-top:4px;">Booking Confirmed</div>
          </div>
          <div style="background:#f0fdf4;border-bottom:1px solid #bbf7d0;padding:16px 32px;display:flex;align-items:center;gap:12px;">
            <div style="width:36px;height:36px;background:#16a34a;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
              <span style="color:#fff;font-size:18px;line-height:1;">&#10003;</span>
            </div>
            <div>
              <div style="font-weight:700;color:#15803d;font-size:15px;">Appointment Confirmed!</div>
              <div style="color:#4b5563;font-size:13px;margin-top:2px;">We'll see you and your bike at the shop.</div>
            </div>
          </div>
          <div style="padding:24px 32px;">
            <table style="width:100%;border-collapse:collapse;">
              <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;">Reference</td><td style="padding:6px 0;color:#111827;font-size:13px;font-weight:700;text-align:right;">{f['ref']}</td></tr>
              <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;">Date</td><td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">{f['date_str']}</td></tr>
              <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;">Time set aside</td><td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">{f['time_label']}</td></tr>
              <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;">Expected finish</td><td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">{f['finish_label']}</td></tr>
              <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;">Mechanic</td><td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">{f['mechanic_label']}</td></tr>
              <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;">Services</td><td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">{f['services']}</td></tr>
              <tr><td style="padding:6px 0;color:#6b7280;font-size:13px;">Motorcycle</td><td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">{f['motorcycle']}</td></tr>
            </table>
            <div style="margin-top:16px;padding-top:16px;border-top:1px dashed #e5e7eb;color:#374151;font-size:13px;">
              Please arrive 10 minutes early so we can start on time.
            </div>
          </div>
          <div style="background:#f9fafb;border-top:1px solid #f3f4f6;padding:20px 32px;text-align:center;">
            <div style="color:#6b7280;font-size:12px;line-height:1.7;">
              <strong style="color:#111827;">MotoTyre North Caloocan</strong><br>
              Saranay Rd, Brgy. 171 Bagumbong, Caloocan City<br>
              &#128222; 0915 269 8366 &nbsp;|&nbsp; Mon–Sat 8:00 AM – 6:30 PM
            </div>
          </div>
        </div>"""

        _send_gmail(user.email, f"Booking Confirmed — {f['ref']}", html)
        print(f"[booking confirmation email] sent to {user.email} for booking {booking.id}")
    except Exception as e:
        print(f"[booking confirmation email] failed for booking {booking.id}: {e}")
        db.session.rollback()


# ── Helpers ──────────────────────────────────────────────────────────────────

def ph_now():
    return datetime.utcnow() + timedelta(hours=8)


def minutes_to_time(minute_of_day):
    """510 -> time(8, 30) — wraps past midnight defensively, shouldn't happen given shop hours."""
    h, m = divmod(int(minute_of_day) % 1440, 60)
    return time(h, m)


# ── Models ───────────────────────────────────────────────────────────────────

class User(db.Model, UserMixin):
    id               = db.Column(db.Integer, primary_key=True)
    fullname         = db.Column(db.String(100), nullable=False)
    email            = db.Column(db.String(100), unique=True, nullable=False)
    phone            = db.Column(db.String(20), nullable=False)
    password_hash    = db.Column(db.String(255), nullable=False)
    role             = db.Column(db.String(20), default='customer')
    address          = db.Column(db.String(255))
    motorcycle_model = db.Column(db.String(100))
    profile_pic      = db.Column(db.String(255))
    email_verified   = db.Column(db.Boolean, default=False)
    account_status   = db.Column(db.String(20), default='active')   # active | deactivated | banned
    is_flagged       = db.Column(db.Boolean, default=False)
    bookings         = db.relationship('Booking', backref='customer', lazy=True)
    orders           = db.relationship('Order', backref='customer', lazy=True)

    def set_password(self, pw):   self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)


class OTPRecord(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    email      = db.Column(db.String(100), nullable=False, index=True)
    otp        = db.Column(db.String(10), nullable=False)
    purpose    = db.Column(db.String(10), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used       = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=ph_now)


class Booking(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    service          = db.Column(db.String(300), nullable=False)  # combined names, comma-separated
    date             = db.Column(db.Date, nullable=False)
    time             = db.Column(db.Time, nullable=False)
    motorcycle_model = db.Column(db.String(100))
    motorcycle_plate = db.Column(db.String(20))
    notes            = db.Column(db.Text)
    status           = db.Column(db.String(20), default='pending')
    payment_method   = db.Column(db.String(20), default='cash')
    created_at       = db.Column(db.DateTime, default=ph_now)
    reminder_sent    = db.Column(db.Boolean, default=False)
    day_before_reminder_sent = db.Column(db.Boolean, default=False)
    assigned_mechanic_name           = db.Column(db.String(100))  # who is actually doing the work — shop changes this freely
    assigned_mechanic_specialization = db.Column(db.String(100))
    preferred_mechanic_name           = db.Column(db.String(100))  # who the customer asked for, or None — never overwritten after booking creation
    preferred_mechanic_specialization = db.Column(db.String(100))
    contact_name   = db.Column(db.String(100))
    contact_mobile = db.Column(db.String(20))
    odometer       = db.Column(db.Integer)
    is_archived    = db.Column(db.Boolean, default=False)
    total_amount   = db.Column(db.Float, default=0)
    booking_batch  = db.Column(db.String(36), nullable=True)
    duration_minutes = db.Column(db.Integer, default=60)  # total estimated job length
    end_time         = db.Column(db.Time, nullable=True)  # computed: time + duration
    is_multiday      = db.Column(db.Boolean, default=False)
    overrun_minutes  = db.Column(db.Integer, default=0)  # counter-staff-recorded extra time on top of duration_minutes
    was_rescheduled  = db.Column(db.Boolean, default=False)  # set once, first time this booking's date/time changes after creation
    completed_at     = db.Column(db.DateTime, nullable=True)  # when status actually reached completed — the warranty window's start


class BlockedSlot(db.Model):
    """An admin-blocked start time — removes that slot from the customer
    booking flow immediately. Admin-managed; customer side only ever reads it."""
    id         = db.Column(db.Integer, primary_key=True)
    date       = db.Column(db.Date, nullable=False)
    time       = db.Column(db.Time, nullable=False)
    reason     = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=ph_now)
    __table_args__ = (db.UniqueConstraint('date', 'time', name='uq_blocked_slot'),)


class Mechanic(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(100), nullable=False)
    specialization = db.Column(db.String(100), nullable=False)
    status         = db.Column(db.String(20), default='available')
    phone          = db.Column(db.String(20))
    note           = db.Column(db.Text)
    created_at     = db.Column(db.DateTime, default=ph_now)


class DailyCapacity(db.Model):
    """Per-date overrides for the shop's capacity controls. A missing row for
    a date means "use the defaults" — full roster, standard daily cap."""
    id             = db.Column(db.Integer, primary_key=True)
    date           = db.Column(db.Date, nullable=False, unique=True)
    mechanic_count = db.Column(db.Integer, nullable=True)  # None = whole roster is rostered
    daily_cap      = db.Column(db.Integer, nullable=True)  # None = MAX_BOOKINGS_PER_DAY default


class Product(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    barcode     = db.Column(db.String(100))
    name        = db.Column(db.String(150), nullable=False)
    category    = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    price       = db.Column(db.Float, nullable=False)
    stock       = db.Column(db.Integer, default=0)
    created_at  = db.Column(db.DateTime, default=ph_now)
    order_items = db.relationship('OrderItem', backref='product', lazy=True)


class Order(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total_amount    = db.Column(db.Float, nullable=False)
    status          = db.Column(db.String(20), default='pending')
    payment_method  = db.Column(db.String(20), default='cash')
    delivery_method = db.Column(db.String(20), default='pickup')
    ship_address    = db.Column(db.Text)
    created_at      = db.Column(db.DateTime, default=ph_now)
    items           = db.relationship('OrderItem', backref='order', lazy=True)
    is_archived     = db.Column(db.Boolean, default=False)
    receipt_sent    = db.Column(db.Boolean, default=False)
    delivered_at    = db.Column(db.DateTime, nullable=True)  # when status actually reached delivered/completed — the return window's start


class OrderItem(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    order_id   = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity   = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)


class Service(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.String(200), default='')
    price       = db.Column(db.Float, default=0.0)
    is_active   = db.Column(db.Boolean, default=True)
    duration_minutes = db.Column(db.Integer, default=60)      # estimated job length
    is_multiday      = db.Column(db.Boolean, default=False)   # e.g. Full/Top Overhaul
    duration_label   = db.Column(db.String(30))                # override text, e.g. "3-5 days"
    created_at  = db.Column(db.DateTime, default=ph_now)


class Feedback(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), default='')
    email      = db.Column(db.String(150), default='')
    service    = db.Column(db.String(100), default='')
    rating     = db.Column(db.Integer, default=0)
    message    = db.Column(db.Text, default='')
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=ph_now)


class Notification(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title      = db.Column(db.String(120), nullable=False)
    message    = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(30), default='update')
    status     = db.Column(db.String(30))
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=ph_now)
    priority    = db.Column(db.Boolean, default=False)  # needs a customer decision — stays pinned to the top, unread, until opened
    booking_id  = db.Column(db.Integer, nullable=True)  # lets "open" land on the specific booking, not a list
    return_request_id = db.Column(db.Integer, nullable=True)  # same, for a return/warranty claim


class ReturnRequest(db.Model):
    """A customer reporting a spare part that arrived wrong or a service that
    didn't hold, and what they want done about it (an RMA). kind picks
    whether this claim is against an order (product) or a booking (service)
    — exactly one of order_id/booking_id is ever set. Only one open claim
    (submitted/under_review/approved) may exist per order or booking at a
    time; the next one can only be filed once this is resolved, denied, or
    cancelled."""
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    kind            = db.Column(db.String(10), nullable=False)  # 'product' | 'service'
    order_id        = db.Column(db.Integer, nullable=True)
    booking_id      = db.Column(db.Integer, nullable=True)
    reasons         = db.Column(db.String(300), nullable=False)  # comma-separated reason codes, multi-select
    other_reason_text = db.Column(db.Text, nullable=True)  # required when 'other' is among reasons
    desired_outcome = db.Column(db.String(20), nullable=False)
    requested_mechanic_name = db.Column(db.String(100), nullable=True)  # back-job only — a request, never a guarantee
    requested_refund_amount = db.Column(db.Float, nullable=True)  # what the customer's own math added up to at submission
    photos          = db.Column(db.String(500))  # comma-separated filenames under static/return_evidence/ — permanent, never stripped
    status          = db.Column(db.String(20), default='submitted')  # submitted|under_review|approved|denied|resolved|cancelled
    decision_reason = db.Column(db.Text)
    resolution      = db.Column(db.String(20))  # refund|replacement|redo_service
    refund_amount   = db.Column(db.Float)  # the shop's final approved amount — may differ from requested_refund_amount
    created_at      = db.Column(db.DateTime, default=ph_now)
    decided_at      = db.Column(db.DateTime)
    resolved_at     = db.Column(db.DateTime)
    cancelled_at    = db.Column(db.DateTime)
    # Two things a claim can be blocked on after it's filed, each with its
    # own "stays pinned until actually done" notification, not just "until
    # opened": the shop needing more from the customer, or (for a product
    # claim) the part needing to come back before the remedy is carried out.
    awaiting_customer_info = db.Column(db.Boolean, default=False)
    info_request_note      = db.Column(db.Text)          # admin's own words — becomes the notification title verbatim
    info_provided_at       = db.Column(db.DateTime)
    info_provided_text     = db.Column(db.Text)
    item_returned          = db.Column(db.Boolean, default=False)
    item_returned_at       = db.Column(db.DateTime)
    # Back-job scheduling — a request recorded here, not an entry on the real
    # shop calendar; approving and scheduling are two separate state changes,
    # each raising its own single notification.
    redo_date          = db.Column(db.Date)
    redo_time          = db.Column(db.Time)
    redo_mechanic_name = db.Column(db.String(100))
    redo_booking_id    = db.Column(db.Integer, nullable=True)  # the real, zero-charge Booking this back job writes into the shop calendar
    internal_notes     = db.Column(db.Text)  # shop-only — never surfaced to the customer


class ReturnRequestItem(db.Model):
    """One line item within a product return — which order line, and how
    many of it (capped at what was actually bought on that line), since only
    one of several identical parts might be the bad one."""
    id                = db.Column(db.Integer, primary_key=True)
    return_request_id = db.Column(db.Integer, db.ForeignKey('return_request.id'), nullable=False)
    order_item_id     = db.Column(db.Integer, nullable=False)
    quantity          = db.Column(db.Integer, nullable=False)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def send_notification(user_id, title, message, type='update', status=None, booking_id=None, priority=False, return_request_id=None):
    db.session.add(Notification(user_id=user_id, title=title, message=message, type=type,
                                 status=status, booking_id=booking_id, priority=priority,
                                 return_request_id=return_request_id))
    db.session.commit()


def require_active_account(f):
    """Decorator: block booking/ordering for banned accounts (deactivated accounts
    already can't log in, but this also covers an already-open session)."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        status = getattr(current_user, 'account_status', 'active')
        if status == 'banned':
            msg = 'Your account has been banned. You are no longer allowed to book appointments or place orders.'
        elif status == 'deactivated':
            msg = 'Your account has been deactivated. Please contact support.'
        else:
            return f(*args, **kwargs)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
            return jsonify({'success': False, 'error': msg}), 403
        flash(msg, 'danger')
        return redirect(url_for('customer_dashboard'))
    return decorated


def check_upcoming_bookings():
    """Every minute: find bookings starting in ~15 min and send in-app reminder."""
    with app.app_context():
        now = datetime.now()
        target_start = now + timedelta(minutes=14)
        target_end   = now + timedelta(minutes=16)

        upcoming = Booking.query.filter(
            Booking.status.in_(['confirmed', 'in_progress', 'inprogress']),
            Booking.reminder_sent == False,
            Booking.date == now.date()
        ).all()

        for b in upcoming:
            booking_dt = datetime.combine(b.date, b.time)
            if target_start <= booking_dt <= target_end:
                send_notification(
                    b.user_id,
                    'Upcoming Appointment Reminder',
                    f'Your {b.service} booking is in 15 minutes at {b.time.strftime("%I:%M %p")}. Please arrive on time.',
                    type='reminder',
                    status='reminder'
                )
                b.reminder_sent = True
                db.session.commit()
                print(f'[REMINDER] Sent for booking #{b.id} to user {b.user_id}')


def check_day_before_reminders():
    """Once daily-scale (checked every 30 min): find bookings happening
    tomorrow and send a short reminder — no marketing, no upsell, just what
    they need to show up: date, time, mechanic, services, motorcycle, and
    how to reply if they need to move it."""
    with app.app_context():
        tomorrow = (datetime.now() + timedelta(days=1)).date()
        upcoming = Booking.query.filter(
            Booking.status.in_(['confirmed', 'in_progress', 'inprogress']),
            Booking.day_before_reminder_sent == False,
            Booking.date == tomorrow,
        ).all()

        for b in upcoming:
            f = _booking_confirmation_facts(b)
            send_notification(
                b.user_id, f"Tomorrow — {f['time_str']}",
                f"Reminder: your {f['services']} appointment ({f['ref']}) is tomorrow, {f['date_str']} at {f['time_label']}. "
                f"Mechanic: {f['mechanic_label']}. Motorcycle: {f['motorcycle']}. "
                f"Reply to this if you need to move it.",
                type='booking', status=b.status, booking_id=b.id,
            )
            b.day_before_reminder_sent = True
            db.session.commit()

            user = User.query.get(b.user_id)
            if user and user.email:
                html = f"""
                <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;">
                  <p>Hi {user.fullname},</p>
                  <p>Reminder: your <strong>{f['services']}</strong> appointment ({f['ref']}) is tomorrow, <strong>{f['date_str']} at {f['time_label']}</strong>.</p>
                  <p>Mechanic: {f['mechanic_label']}<br>Motorcycle: {f['motorcycle']}</p>
                  <p>Reply to this email if you need to move it.</p>
                  <p>— MotoTyre North Caloocan</p>
                </div>"""
                _send_gmail(user.email, f"Reminder — tomorrow at {f['time_str']} ({f['ref']})", html)
            print(f'[DAY-BEFORE REMINDER] Sent for booking #{b.id} to user {b.user_id}')


def _generate_otp(length=6):
    return "".join(random.choices(string.digits, k=length))


ALLOWED_EMAIL_DOMAINS = {
    'gmail.com',
    'yahoo.com', 'yahoo.com.ph', 'ymail.com',
    'outlook.com', 'hotmail.com', 'live.com', 'msn.com',
    'icloud.com', 'me.com', 'mac.com',
    'proton.me', 'protonmail.com',
    'zoho.com', 'aol.com', 'mail.com',
}

def is_public_email(email: str) -> bool:
    try:
        domain = email.strip().lower().split('@')[1]
        return domain in ALLOWED_EMAIL_DOMAINS
    except IndexError:
        return False


def _save_otp(email, purpose):
    OTPRecord.query.filter_by(email=email, purpose=purpose, used=False).update({"used": True})
    db.session.flush()
    otp = _generate_otp()
    db.session.add(OTPRecord(
        email=email, otp=otp, purpose=purpose,
        expires_at=ph_now() + timedelta(minutes=OTP_EXPIRY_MINS)
    ))
    db.session.commit()
    return otp


def _verify_otp(email, otp_input, purpose):
    record = OTPRecord.query.filter_by(email=email, purpose=purpose, used=False)\
                            .order_by(OTPRecord.created_at.desc()).first()
    if not record:
        return {"valid": False, "message": "No OTP found. Please request a new one."}
    if ph_now() > record.expires_at:
        record.used = True
        db.session.commit()
        return {"valid": False, "message": "OTP has expired. Please request a new one."}
    if record.otp != otp_input.strip():
        return {"valid": False, "message": "Invalid OTP. Please try again."}
    record.used = True
    db.session.commit()
    return {"valid": True, "message": "OTP verified."}


ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def cleanup_abandoned_gcash_orders():
    cutoff = ph_now() - timedelta(minutes=30)
    abandoned = Order.query.filter(
        Order.status == 'awaiting_payment',
        Order.payment_method == 'gcash',
        Order.created_at < cutoff
    ).all()
    for order in abandoned:
        Notification.query.filter(
            Notification.user_id == order.user_id,
            Notification.type == 'order',
            Notification.created_at >= order.created_at
        ).delete(synchronize_session=False)
        for item in order.items:
            product = Product.query.get(item.product_id)
            if product:
                product.stock += item.quantity
        OrderItem.query.filter_by(order_id=order.id).delete()
        db.session.delete(order)
    db.session.commit()
    return len(abandoned)


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return render_template('landing.html', FACEBOOK_PAGE_URL=FACEBOOK_PAGE_URL)

@app.route('/products')
def products():
    return render_template('Products.html')


@app.route('/api/feedback', methods=['POST'])
def submit_feedback():
    data = request.get_json(silent=True) or {}
    name    = clean_str(data.get('name', ''), max_len=100)
    email   = clean_str(data.get('email', ''), max_len=150)
    service = clean_str(data.get('service', ''), max_len=100)
    message = clean_str(data.get('message', ''), max_len=2000)
    rating  = clean_int(data.get('rating'), default=0)

    if email and not is_valid_email(email):
        return jsonify({'success': False, 'error': 'Please enter a valid email address.'}), 400
    if rating < 1 or rating > 5:
        return jsonify({'success': False, 'error': 'Please pick a star rating.'}), 400
    if not message:
        return jsonify({'success': False, 'error': 'Please write your feedback.'}), 400

    fb = Feedback(name=name, email=email, service=service, rating=rating, message=message)
    db.session.add(fb)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        if current_user.role in ['admin', 'staff']:
            logout_user()
            flash('Please use the Admin Portal to log in.', 'warning')
            return redirect(url_for('login'))
        return redirect(url_for('customer_dashboard'))

    if request.method == 'POST':
        email    = clean_str(request.form.get('email', ''), max_len=254).lower()
        password = request.form.get('password', '')
        if not is_valid_email(email):
            flash('Invalid email address.', 'danger')
            return redirect(url_for('login'))
        user = User.query.filter_by(email=email).first()

        if user and user.role in ['admin', 'staff']:
            flash('Admin and staff accounts must use the Admin Portal.', 'danger')
            return redirect(url_for('login'))

        if user and user.check_password(password):
            if not user.email_verified:
                flash('Please verify your email before logging in.', 'warning')
                return redirect(url_for('login'))
            if user.account_status == 'deactivated':
                flash('Your account has been deactivated. Please contact support.', 'danger')
                return redirect(url_for('login'))
            # Banned accounts can still log in — they need to see the notification
            # explaining they can no longer book appointments or place orders.
            otp = _save_otp(email, purpose="login")
            try:
                send_otp_email(email, otp, purpose="login")
            except Exception as e:
                flash(f'Could not send OTP: {e}', 'danger')
                return redirect(url_for('login'))
            session['pending_login_email'] = email
            return redirect(url_for('login'))

        flash('Invalid email or password.', 'danger')
    return render_template('login.html')


@app.route('/verify-login-otp', methods=['GET', 'POST'])
def verify_login_otp():
    email = session.get('pending_login_email')
    if not email:
        return redirect(url_for('login'))

    if request.method == 'POST':
        result = _verify_otp(email, request.form.get('otp', ''), purpose="login")
        if result['valid']:
            session.pop('pending_login_email', None)
            user = User.query.filter_by(email=email).first()
            if user and user.role in ['admin', 'staff']:
                flash('Admin and staff accounts must use the Admin Portal.', 'danger')
                return redirect(url_for('login'))
            login_user(user, remember=True)
            next_page = request.args.get('next') or session.pop('next', None)
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for('customer_dashboard'))
        flash(result['message'], 'danger')

    return render_template('verify_otp.html', purpose='login', email=email)


@app.route('/resend-otp/<purpose>')
def resend_otp(purpose):
    validate_otp_purpose(purpose)
    key_map      = {'login': 'pending_login_email', 'verify': 'pending_verify_email', 'reset': 'pending_reset_email'}
    redirect_map = {'login': 'login', 'reset': 'forgot_password_verify', 'verify': 'register'}
    email = session.get(key_map.get(purpose))
    if not email:
        flash('Session expired. Please start again.', 'danger')
        return redirect(url_for('login'))
    otp = _save_otp(email, purpose=purpose)
    try:
        send_otp_email(email, otp, purpose=purpose)
        flash('A new OTP has been sent to your email.', 'info')
    except Exception as e:
        flash(f'Could not resend OTP: {e}', 'danger')
    return redirect(url_for(redirect_map.get(purpose, 'login')))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = clean_str(request.form.get('email', ''), max_len=254).lower()
        phone = clean_str(request.form.get('phone', ''), max_len=11)

        if not is_valid_email(email):
            flash('Invalid email address.', 'danger')
            return redirect(url_for('register'))
        if not is_public_email(email):
            flash('Please use a public email address (e.g. Gmail, Yahoo, Outlook, iCloud).', 'danger')
            return redirect(url_for('register'))
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
            return redirect(url_for('register'))
        if not is_valid_phone(phone):
            flash('Invalid phone number.', 'danger')
            return redirect(url_for('register'))

        password         = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        if password != confirm_password:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('register'))

        firstname = clean_str(request.form.get('firstname', ''), max_len=50)
        lastname  = clean_str(request.form.get('lastname', ''), max_len=50)
        suffix    = clean_str(request.form.get('suffix', ''), max_len=10)
        fullname  = f"{firstname} {lastname}" + (f" {suffix}" if suffix else "")

        user = User(
            fullname=fullname, email=email, phone=phone,
            address=clean_str(request.form.get('address', ''), max_len=255),
            motorcycle_model=clean_str(request.form.get('model', ''), max_len=100),
            email_verified=False
        )
        user.set_password(request.form['password'])
        db.session.add(user)
        db.session.commit()

        otp = _save_otp(email, purpose="verify")
        try:
            send_otp_email(email, otp, purpose="verify")
        except Exception as e:
            flash(f'Account created but could not send OTP: {e}', 'warning')
            return redirect(url_for('login'))

        session['pending_verify_email'] = email
        return redirect(url_for('register'))

    return render_template('register.html')


@app.route('/verify-email-otp', methods=['GET', 'POST'])
def verify_email_otp():
    email = session.get('pending_verify_email')
    if not email:
        return redirect(url_for('register'))

    if request.method == 'POST':
        result = _verify_otp(email, request.form.get('otp', ''), purpose="verify")
        if result['valid']:
            user = User.query.filter_by(email=email).first()
            if user:
                user.email_verified = True
                db.session.commit()
            session.pop('pending_verify_email', None)
            flash('Email verified! You can now log in.', 'success')
            return redirect(url_for('login'))
        flash(result['message'], 'danger')

    return render_template('verify_otp.html', purpose='verify', email=email)


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user  = User.query.filter_by(email=email).first()
        if user:
            otp = _save_otp(email, purpose="reset")
            try:
                send_otp_email(email, otp, purpose="reset")
                session['pending_reset_email'] = email
            except Exception as e:
                flash(f'Could not send reset code: {e}', 'danger')
                return redirect(url_for('forgot_password'))
        flash('If that email is registered, a reset code has been sent.', 'info')
        return redirect(url_for('forgot_password_verify'))
    return render_template('forgot_password.html')


@app.route('/forgot-password/verify', methods=['GET', 'POST'])
def forgot_password_verify():
    email = session.get('pending_reset_email')
    if not email:
        flash('Session expired. Please try again.', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        result = _verify_otp(email, request.form.get('otp', ''), purpose="reset")
        if result['valid']:
            session['reset_otp_verified'] = True
            return redirect(url_for('forgot_password_reset'))
        flash(result['message'], 'danger')

    return render_template('verify_otp.html', purpose='reset', email=email)


@app.route('/forgot-password/reset', methods=['GET', 'POST'])
def forgot_password_reset():
    email, verified = session.get('pending_reset_email'), session.get('reset_otp_verified')
    if not email or not verified:
        flash('Session expired. Please start again.', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        pw, cpw = request.form.get('password', ''), request.form.get('confirm_password', '')
        if len(pw) < 8:
            flash('Password must be at least 8 characters.', 'danger')
            return redirect(url_for('forgot_password_reset'))
        if pw != cpw:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('forgot_password_reset'))
        user = User.query.filter_by(email=email).first()
        if user:
            user.set_password(pw)
            db.session.commit()
        session.pop('pending_reset_email', None)
        session.pop('reset_otp_verified', None)
        flash('Password reset successful! You can now log in.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', email=email)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ── Customer routes ───────────────────────────────────────────────────────────

@app.route('/customer/dashboard')
@login_required
def customer_dashboard():
    if current_user.role in ['admin', 'staff']:
        logout_user()
        flash('Please use the Admin Portal.', 'warning')
        return redirect(url_for('login'))

    Notification.query.filter_by(
        user_id=current_user.id, type='order', status='pending'
    ).delete(synchronize_session=False)
    db.session.commit()

    bookings = Booking.query.filter_by(user_id=current_user.id).order_by(Booking.created_at.desc()).all()
    orders   = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    products = Product.query.filter(Product.stock > 0).all()
    services = Service.query.filter_by(is_active=True).order_by(Service.name).all()
    returns  = ReturnRequest.query.filter_by(user_id=current_user.id).order_by(ReturnRequest.created_at.desc()).all()
    for r in returns:
        r.subject_label = _return_subject_label(r)
        r.origin_label = _return_origin_label(r)

    open_claims = ReturnRequest.query.filter(
        ReturnRequest.user_id == current_user.id, ReturnRequest.status.in_(OPEN_RETURN_STATUSES)
    ).all()
    open_by_order = {r.order_id: r for r in open_claims if r.order_id}
    open_by_booking = {r.booking_id: r for r in open_claims if r.booking_id}

    for o in orders:
        o.return_window = return_window_info('product', o.delivered_at) if o.status in ('delivered', 'completed') else None
        o.open_claim = open_by_order.get(o.id)
    for b in bookings:
        b.return_window = return_window_info('service', b.completed_at) if b.status == 'completed' else None
        b.open_claim = open_by_booking.get(b.id)
    return render_template('customer_dashboard.html', bookings=bookings, orders=orders,
                           products=products, services=services, returns=returns)


def _return_subject_label(rr):
    """Human-readable 'what' a return/warranty claim is about — the specific
    line items, or the service."""
    if rr.kind == 'product':
        rows = ReturnRequestItem.query.filter_by(return_request_id=rr.id).all()
        if rows:
            names = []
            for row in rows:
                item = OrderItem.query.get(row.order_item_id)
                if item and item.product:
                    names.append(f'{item.product.name} ×{row.quantity}')
            if names:
                return ', '.join(names)
        return f'ORD-{rr.order_id:03d}' if rr.order_id else 'an order'
    booking = Booking.query.get(rr.booking_id) if rr.booking_id else None
    return booking.service if booking else 'a service'


def _return_origin_label(rr):
    """'The order or service it came from' — the originating record's own
    reference, distinct from the claim's own RMA reference."""
    if rr.kind == 'product':
        return f'ORD-{rr.order_id:03d}' if rr.order_id else '—'
    booking = Booking.query.get(rr.booking_id) if rr.booking_id else None
    return f"{booking.service} — {booking.date.strftime('%b %d, %Y')}" if booking else '—'


def _resolve_service_combo(names):
    """names: requested service names. Looks them up against the active Service
    catalog and returns service_duration.combine_services()'s dict — unmatched
    names still count (at the default duration) so a booking never silently loses
    a service the customer picked."""
    names = [n for n in (names or []) if n]
    if not names:
        return None
    found = {s.name: s for s in Service.query.filter(Service.name.in_(names), Service.is_active == True).all()}
    rows = []
    for n in names:
        svc = found.get(n)
        if svc:
            rows.append(svc)
        else:
            rows.append(type('Adhoc', (), {
                'name': n, 'duration_minutes': DEFAULT_DURATION_MIN,
                'is_multiday': False, 'duration_label': None,
            })())
    return combine_services(rows)


def get_capacity_row(query_date):
    return DailyCapacity.query.filter_by(date=query_date).first()


def get_on_duty_mechanics(query_date=None):
    """Who's actually working a given day: the first N by roster order (N
    from that date's capacity override, or the whole roster if none is set),
    further cut down to whoever isn't individually marked off duty in their
    own profile — that override always applies, however high N is set. Same
    routine the admin side uses, so the two can't ever disagree about who's on."""
    query_date = query_date or ph_now().date()
    roster = Mechanic.query.order_by(Mechanic.id).all()
    cap_row = get_capacity_row(query_date)
    n = cap_row.mechanic_count if (cap_row and cap_row.mechanic_count is not None) else len(roster)
    rostered_today = roster[:max(n, 0)]
    on_duty = [m for m in rostered_today if m.status == 'available']
    return roster, rostered_today, on_duty


def get_daily_cap(query_date=None):
    query_date = query_date or ph_now().date()
    cap_row = get_capacity_row(query_date)
    if cap_row and cap_row.daily_cap is not None:
        return cap_row.daily_cap
    return MAX_BOOKINGS_PER_DAY


def _gather_blocked_intervals(booking_date):
    """Admin-blocked start times (staff meeting, parts delivery) for one day,
    as full-hour (start, start+60) windows — folded into the shop's queue
    intervals so a blocked slot reads exactly like an already-booked one:
    'Fully booked', gone from the grid, refused if requested directly."""
    return [
        (t.hour * 60 + t.minute, t.hour * 60 + t.minute + SLOT_GRANULARITY_MIN)
        for t in (bs.time for bs in BlockedSlot.query.filter_by(date=booking_date).all())
    ]


def _check_booking_request(mechanic_id, booking_date, start_minutes, duration_minutes, exclude_id=None):
    """THE call every booking-creation path in this app makes before touching
    the schedule. Gathers what service_duration.validate_booking() needs from
    the DB — the day's existing bookings, the requested mechanic's roster
    status and their own day — and hands the actual decision to that one
    shared routine, so this app and the admin side can never enforce the shop
    rules differently. Returns (mechanic_or_None, error_message_or_None); a
    booking with no mechanic requested still gets fully validated, it just
    passes mechanic_name=None through."""
    q = Booking.query.filter(Booking.date == booking_date, Booking.status != 'cancelled')
    if exclude_id:
        q = q.filter(Booking.id != exclude_id)
    shop_intervals = []
    for b in q.all():
        b_start = b.time.hour * 60 + b.time.minute
        shop_intervals.append((b_start, compute_finish_minutes(b_start, b.duration_minutes or DEFAULT_DURATION_MIN)))
    shop_intervals += _gather_blocked_intervals(booking_date)

    mechanic = None
    mechanic_status = None
    mechanic_intervals = None
    if mechanic_id:
        mechanic = Mechanic.query.get(int(mechanic_id)) if str(mechanic_id).isdigit() else None
        if not mechanic:
            return None, 'That mechanic could not be found. Please choose someone else.'
        # On the roster AND individually available — a mechanic dialed out by
        # today's capacity slider reads the same as one marked off duty.
        _, _, on_duty = get_on_duty_mechanics(booking_date)
        mechanic_status = 'available' if any(m.name == mechanic.name for m in on_duty) else 'off duty'
        mq = Booking.query.filter(
            Booking.assigned_mechanic_name == mechanic.name,
            Booking.date == booking_date,
            Booking.status != 'cancelled',
        )
        if exclude_id:
            mq = mq.filter(Booking.id != exclude_id)
        mechanic_intervals = []
        for b in mq.all():
            b_start = b.time.hour * 60 + b.time.minute
            mechanic_intervals.append((b_start, compute_finish_minutes(b_start, b.duration_minutes or DEFAULT_DURATION_MIN)))

    ok, error = validate_booking(
        start_minutes, duration_minutes, shop_intervals, daily_count=len(shop_intervals),
        daily_cap=get_daily_cap(booking_date),
        mechanic_name=(mechanic.name if mechanic else None),
        mechanic_status=mechanic_status, mechanic_intervals=mechanic_intervals,
    )
    if not ok:
        return None, error
    return mechanic, None


@app.route('/customer/book', methods=['POST'])
@login_required
@require_active_account
def book_service():
    service_names = request.form.getlist('services') or (
        [clean_str(request.form.get('service', ''), max_len=100)]
        if request.form.get('service') else []
    )
    combo = _resolve_service_combo(service_names)
    if not combo:
        flash('Please choose at least one service.', 'danger')
        return redirect(url_for('customer_dashboard'))

    try:
        booking_date = datetime.strptime(clean_str(request.form.get('date', ''), max_len=10), '%Y-%m-%d').date()
        booking_time = datetime.strptime(clean_str(request.form.get('time', ''), max_len=5), '%H:%M').time()
    except ValueError:
        flash('Invalid date or time format.', 'danger')
        return redirect(url_for('customer_dashboard'))

    booking_datetime = datetime.combine(booking_date, booking_time)
    if booking_datetime <= ph_now():
        flash('Cannot book a past or current time slot. Please choose a future time.', 'danger')
        return redirect(url_for('customer_dashboard'))

    start_min = booking_time.hour * 60 + booking_time.minute
    end_min   = compute_finish_minutes(start_min, combo['total_minutes'])
    mechanic_id = request.form.get('mechanic_id', '')
    requested_mechanic, request_error = _check_booking_request(mechanic_id, booking_date, start_min, combo['total_minutes'])
    if request_error:
        flash(request_error, 'danger')
        return redirect(url_for('customer_dashboard'))

    service = combined_service_name(combo['names'])
    odo_raw = clean_str(request.form.get('odometer', ''), max_len=10)
    booking = Booking(
        user_id=current_user.id,
        service=service,
        date=booking_date,
        time=booking_time,
        end_time=minutes_to_time(end_min),
        duration_minutes=combo['total_minutes'],
        is_multiday=combo['is_multiday'],
        status='confirmed',
        motorcycle_model=clean_str(request.form.get('motorcycle_model', ''), max_len=100),
        motorcycle_plate=clean_str(request.form.get('motorcycle_plate', ''), max_len=20),
        notes=clean_str(request.form.get('notes', ''), max_len=500),
        contact_name=clean_str(request.form.get('contact_name', ''), max_len=100),
        contact_mobile=clean_str(request.form.get('contact_mobile', ''), max_len=13),
        odometer=int(odo_raw) if odo_raw.isdigit() else None,
    )
    if requested_mechanic:
        # What the customer asked for — recorded once, never changed again —
        # and it's also the initial assignment, since that's who's on it now.
        booking.preferred_mechanic_name = requested_mechanic.name
        booking.preferred_mechanic_specialization = requested_mechanic.specialization
        booking.assigned_mechanic_name = requested_mechanic.name
        booking.assigned_mechanic_specialization = requested_mechanic.specialization
    db.session.add(booking)
    db.session.commit()
    send_booking_confirmation_email(booking)

    cf = _booking_confirmation_facts(booking)
    send_notification(
        current_user.id, f"Confirmed — {cf['date_short']} at {cf['time_str']}",
        f"Your {cf['services']} appointment ({cf['ref']}) on {cf['date_str']} — {cf['time_label']}. "
        f"{cf['finish_label']}. Mechanic: {cf['mechanic_label']}. Motorcycle: {cf['motorcycle']}. "
        f"Please arrive 10 minutes early so we can start on time.",
        type='booking', status='confirmed', booking_id=booking.id,
    )
    for admin in User.query.filter_by(role='admin').all():
        send_notification(
            admin.id, 'New Booking Confirmed',
            f'{current_user.fullname} booked {service} on {booking.date.strftime("%b %d, %Y")} at {booking.time.strftime("%I:%M %p")}.',
            type='booking', status='confirmed'
        )

    flash('Appointment booked successfully!', 'success')
    return redirect(url_for('customer_dashboard'))


@app.route('/customer/book-multiple', methods=['POST'])
@login_required
@require_active_account
def book_multiple_services():
    import json as _json
    data = request.get_json()
    if not data or not data.get('bookings'):
        return jsonify({'success': False, 'error': 'No bookings provided'}), 400

    created = []
    created_bookings = []
    errors  = []
    # Only bookings submitted together as a batch of 2+ get a shared batch id, so the
    # admin's Manage Booking page compresses them into one row — single bookings never
    # get grouped with anything else, even ones made later the same day.
    batch_id = str(uuid.uuid4()) if len(data['bookings']) > 1 else None

    for idx, b in enumerate(data['bookings']):
        try:
            booking_date = datetime.strptime(b.get('date', ''), '%Y-%m-%d').date()
            booking_time = datetime.strptime(b.get('time', ''), '%H:%M').time()
        except ValueError:
            errors.append(f'Booking {idx+1}: invalid date/time')
            continue

        booking_dt = datetime.combine(booking_date, booking_time)
        if booking_dt <= ph_now():
            errors.append(f'Booking {idx+1}: cannot book a past time slot')
            continue

        service_names = b.get('services') or ([b.get('service')] if b.get('service') else [])
        combo = _resolve_service_combo(service_names)
        if not combo:
            errors.append(f'Booking {idx+1}: at least one service is required')
            continue
        service = combined_service_name(combo['names'])

        start_min = booking_time.hour * 60 + booking_time.minute
        end_min   = compute_finish_minutes(start_min, combo['total_minutes'])
        mechanic_id = b.get('mechanic_id', '')
        # Every prior booking in this same batch is already flushed to the DB
        # by the time we get here, so this one query also catches a collision
        # with an earlier item in the same submission — no separate bookkeeping needed.
        requested_mechanic, request_error = _check_booking_request(mechanic_id, booking_date, start_min, combo['total_minutes'])
        if request_error:
            errors.append(f'Booking {idx+1}: {request_error}')
            continue

        odo_raw = clean_str(str(b.get('odometer', '')), max_len=10)
        booking = Booking(
            user_id=current_user.id,
            service=service,
            date=booking_date,
            time=booking_time,
            end_time=minutes_to_time(end_min),
            duration_minutes=combo['total_minutes'],
            is_multiday=combo['is_multiday'],
            status='confirmed',
            motorcycle_model=clean_str(b.get('motorcycle_model', ''), max_len=100),
            motorcycle_plate=clean_str(b.get('motorcycle_plate', ''), max_len=20),
            notes=clean_str(b.get('notes', ''), max_len=500),
            contact_name=clean_str(b.get('contact_name', ''), max_len=100),
            contact_mobile=clean_str(b.get('contact_mobile', ''), max_len=13),
            odometer=int(odo_raw) if odo_raw.isdigit() else None,
            booking_batch=batch_id,
        )
        if requested_mechanic:
            booking.preferred_mechanic_name = requested_mechanic.name
            booking.preferred_mechanic_specialization = requested_mechanic.specialization
            booking.assigned_mechanic_name = requested_mechanic.name
            booking.assigned_mechanic_specialization = requested_mechanic.specialization

        db.session.add(booking)
        db.session.flush()
        created.append(booking.id)
        created_bookings.append(booking)  # facts computed once each below, after IDs are final

    if created:
        db.session.commit()
        facts = [_booking_confirmation_facts(b) for b in created_bookings]
        for booking in created_bookings:
            send_booking_confirmation_email(booking)

        # One combined notification instead of one per booking, so a multi-service
        # queue doesn't stack several near-identical notifications for the customer.
        if len(facts) == 1:
            cf = facts[0]
            send_notification(
                current_user.id, f"Confirmed — {cf['date_short']} at {cf['time_str']}",
                f"Your {cf['services']} appointment ({cf['ref']}) on {cf['date_str']} — {cf['time_label']}. "
                f"{cf['finish_label']}. Mechanic: {cf['mechanic_label']}. Motorcycle: {cf['motorcycle']}. "
                f"Please arrive 10 minutes early so we can start on time.",
                type='booking', status='confirmed', booking_id=created_bookings[0].id,
            )
        else:
            lines = '\n'.join(
                f"• {cf['services']} ({cf['ref']}) — {cf['date_str']} at {cf['time_label']}, {cf['finish_label']}, mechanic: {cf['mechanic_label']}"
                for cf in facts
            )
            send_notification(
                current_user.id, f'{len(facts)} bookings confirmed',
                f"Your appointments are confirmed:\n{lines}\nPlease arrive 10 minutes early for each.",
                type='booking', status='confirmed',
            )

        for admin in User.query.filter_by(role='admin').all():
            send_notification(
                admin.id, f'{len(created)} New Booking(s) Confirmed',
                f'{current_user.fullname} booked {len(created)} service(s).',
                type='booking', status='confirmed'
            )

    return jsonify({'success': len(created) > 0, 'created': len(created), 'errors': errors})


@app.route('/customer/cart/checkout', methods=['POST'])
@login_required
@require_active_account
def cart_checkout():
    data = request.get_json()
    if not data or not data.get('items'):
        return jsonify({'success': False, 'error': 'Cart is empty'}), 400

    items_data      = data['items']
    delivery_method = data.get('delivery_method', 'pickup')
    payment_method  = data.get('payment_method', 'cash')
    ship_address    = data.get('ship_address', '')

    resolved = []
    for item in items_data:
        product = Product.query.get(item['product_id'])
        if not product:
            return jsonify({'success': False, 'error': 'Product not found'}), 400
        qty = int(item['quantity'])
        if product.stock < qty:
            return jsonify({'success': False, 'error': f'Not enough stock for {product.name}'}), 400
        resolved.append((product, qty))

    total        = sum(p.price * q for p, q in resolved)
    order_status = 'awaiting_payment' if payment_method.lower() == 'gcash' else 'pending'

    order = Order(user_id=current_user.id, total_amount=total,
                  payment_method=payment_method, delivery_method=delivery_method,
                  ship_address=ship_address, status=order_status)
    db.session.add(order)
    db.session.flush()

    for product, qty in resolved:
        db.session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=qty, unit_price=product.price))
        product.stock -= qty

    db.session.commit()

    if payment_method.lower() == 'gcash':
        desc   = f"MotoTyre Order #{order.id:03d}: " + ', '.join(f"{p.name} x{q}" for p, q in resolved)
        result = create_gcash_payment(amount=total, description=desc[:100], order_id=order.id, origin=request.host_url)
        if result['success']:
            return jsonify({'success': True, 'gcash': True, 'checkout_url': result['checkout_url']})
        return jsonify({'success': False, 'error': 'Could not create GCash payment'}), 500

    items_desc = ', '.join(f"{p.name} x{q}" for p, q in resolved)
    _is_ship   = delivery_method == 'ship'
    _dest      = shipping_destination(ship_address)
    _dest_text = f' We will ship it to {_dest} once confirmed.' if _is_ship and _dest else ''
    send_notification(current_user.id, 'Order Placed!',
        with_stamp(
            f'Your order ORD-{order.id:03d} for {items_desc[:80]} worth ₱{total:.2f} '
            f'is now pending.{_dest_text}',
            'pending', is_ship=_is_ship),
        type='order', status='pending')
    for admin in User.query.filter_by(role='admin').all():
        send_notification(admin.id, 'New Order Received',
            f'{current_user.fullname} placed an order for ₱{total:.2f}.', type='order', status='pending')

    # submitCartOrder() reloads the page on success, so a flash here is picked
    # up by that reload the same way the profile-update toasts are.
    flash('Order placed successfully!', 'success')
    return jsonify({'success': True, 'gcash': False})


@app.route('/customer/order', methods=['POST'])
@login_required
@require_active_account
def place_order():
    product  = Product.query.get_or_404(clean_int(request.form.get('product_id', 0), default=0))
    quantity = clean_int(request.form.get('quantity', 1), default=1, min_val=1, max_val=9999)
    if product.stock < quantity:
        flash('Not enough stock available.', 'danger')
        return redirect(url_for('customer_dashboard'))

    total           = product.price * quantity
    payment_method  = clean_str(request.form.get('payment_method', 'cash'), max_len=20)
    delivery_method = clean_str(request.form.get('delivery_method', 'pickup'), max_len=20)

    ship_address = ''
    if delivery_method == 'ship':
        ship_name     = clean_str(request.form.get('ship_name', ''), max_len=100)
        ship_mobile   = clean_str(request.form.get('ship_mobile', ''), max_len=13)
        ship_street   = clean_str(request.form.get('ship_street', ''), max_len=200)
        ship_city     = clean_str(request.form.get('ship_city', ''), max_len=100)
        ship_province = clean_str(request.form.get('ship_province', ''), max_len=100)
        ship_zip      = clean_str(request.form.get('ship_zip', ''), max_len=10)
        ship_address  = f"{ship_name}\n{ship_mobile}\n{ship_street}, {ship_city}, {ship_province} {ship_zip}"

    if payment_method.lower() == 'gcash':
        order_status = 'awaiting_payment'
    else:
        # Cash orders (pickup or ship) start as pending — payment is collected later
        # (at the counter via the Billing page for pickup).
        order_status = 'pending'

    order = Order(
        user_id=current_user.id, total_amount=total,
        payment_method=payment_method, delivery_method=delivery_method,
        ship_address=ship_address, status=order_status
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=quantity, unit_price=product.price))
    product.stock -= quantity
    db.session.commit()

    if payment_method.lower() != 'gcash':
        _is_ship      = delivery_method == 'ship'
        delivery_text = "for delivery" if _is_ship else "for pickup"
        _dest         = shipping_destination(ship_address)
        _dest_text    = f' We will ship it to {_dest} once confirmed.' if _is_ship and _dest else ''
        send_notification(
            current_user.id, 'Order Placed!',
            with_stamp(
                f'Your order ORD-{order.id:03d} for {product.name} (x{quantity}) worth ₱{total:.2f} '
                f'is now pending {delivery_text}.{_dest_text}',
                'pending', is_ship=_is_ship),
            type='order', status='pending'
        )

    if payment_method.lower() == 'gcash':
        description = f"MotoTyre Order #{order.id:03d}: {product.name} x{quantity}"
        result = create_gcash_payment(amount=total, description=description, order_id=order.id, origin=request.host_url)
        if result["success"]:
            return redirect(result["checkout_url"])
        else:
            flash('Could not create GCash payment. Please try again or pay in-store.', 'warning')
            return redirect(url_for('customer_dashboard'))

    flash('Order placed successfully!', 'success')
    return redirect(url_for('customer_dashboard'))


@app.route('/customer/profile', methods=['GET', 'POST'])
@login_required
def customer_profile():
    if request.method == 'POST':
        phone = clean_str(request.form.get('phone', ''), max_len=11)
        if not is_valid_phone(phone):
            flash('Invalid phone number.', 'danger')
            return redirect(url_for('customer_dashboard'))
        current_user.fullname         = clean_str(request.form.get('fullname', ''), max_len=100)
        current_user.phone            = phone
        current_user.address          = clean_str(request.form.get('address', ''), max_len=255)
        current_user.motorcycle_model = clean_str(request.form.get('motorcycle_model', ''), max_len=100)
        db.session.commit()
        flash('Profile updated successfully!', 'success')
    return redirect(url_for('customer_dashboard'))


@app.route('/profile/upload-pic', methods=['POST'])
@login_required
def upload_profile_pic():
    file = request.files.get('profile_pic')
    if not file or file.filename == '':
        flash('No file selected.', 'danger')
    elif allowed_file(file.filename):
        ext      = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{current_user.id}_{uuid.uuid4().hex}.{ext}"
        folder   = os.path.join(app.root_path, 'static', 'profile_pics')
        os.makedirs(folder, exist_ok=True)
        file.save(os.path.join(folder, filename))
        current_user.profile_pic = filename
        db.session.commit()
        db.session.refresh(current_user)
        flash('Profile picture updated!', 'success')
    else:
        flash('Invalid file type.', 'danger')
    return redirect(url_for('customer_dashboard'))


RETURN_PHOTOS_MAX = 5
RETURN_PHOTO_MAX_BYTES = 5 * 1024 * 1024
RETURN_OUTCOME_LABELS = {
    'product': {'replacement': 'Return and replacement', 'refund': 'Return and refund'},
    'service': {'redo_service': 'Back job', 'refund': 'Refund'},
}
RETURN_REASON_LABELS = {kind: {code: label for code, label, _ in opts} for kind, opts in RETURN_REASONS.items()}
RETURN_REASON_NEEDS_PHOTO = {kind: {code: np for code, _, np in opts} for kind, opts in RETURN_REASONS.items()}

# Eligibility windows: a spare part is returnable within 7 days of delivery;
# a service carries a 15-day warranty from the day the job was completed.
RETURN_WINDOW_DAYS = {'product': 7, 'service': 15}


def return_window_info(kind, reference_dt):
    """Days left (or expired) in a claim's eligibility window, measured from
    when the order was actually delivered / the booking actually completed —
    not when it was placed/booked. None if that hasn't happened yet, so
    there's no window to speak of."""
    if not reference_dt:
        return None
    window_days = RETURN_WINDOW_DAYS[kind]
    deadline = reference_dt + timedelta(days=window_days)
    now = ph_now()
    expired = now > deadline
    days_left = max((deadline.date() - now.date()).days, 0)
    if expired:
        label = 'Return window closed' if kind == 'product' else 'Warranty expired'
    else:
        unit = 'day' if days_left == 1 else 'days'
        label = f"{days_left} {unit} left to return" if kind == 'product' else f"{days_left} {unit} of warranty left"
    return {'expired': expired, 'days_left': days_left, 'deadline': deadline, 'label': label}


def _save_return_photos(files):
    """Images only, up to RETURN_PHOTOS_MAX, each under RETURN_PHOTO_MAX_BYTES.
    Returns (saved_filenames, skipped_count) — never raises on a bad file,
    just leaves it out, since the client already told the customer which
    ones didn't make it."""
    folder = os.path.join(app.root_path, 'static', 'return_evidence')
    saved, skipped = [], 0
    for file in files[:RETURN_PHOTOS_MAX]:
        if not (file and file.filename):
            continue
        if not allowed_file(file.filename):
            skipped += 1
            continue
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(0)
        if size > RETURN_PHOTO_MAX_BYTES:
            skipped += 1
            continue
        ext = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{current_user.id}_{uuid.uuid4().hex}.{ext}"
        os.makedirs(folder, exist_ok=True)
        file.save(os.path.join(folder, filename))
        saved.append(filename)
    return saved, skipped


@app.route('/returns/new', methods=['POST'])
@login_required
@require_active_account
def create_return_request():
    """A customer reporting a spare part that arrived wrong, or a service
    that didn't hold, and what they want done about it — an RMA. Validates
    everything the dialog itself checks, again, since the dialog's checks
    are a courtesy, not the rule: eligibility + window, one open claim per
    order/booking, the outcome pairing, at least one reason (with the typed
    text if 'other' is among them), and a photo if any picked reason needs
    one. Every problem is collected and returned together, not one at a
    time, so the customer sees the whole list at once."""
    errors = []

    kind = request.form.get('kind', '')
    if kind not in ('product', 'service'):
        return jsonify({'success': False, 'errors': ['Choose a spare part order or a completed service.']}), 400
    validate_return_kind(kind)

    desired_outcome = clean_str(request.form.get('desired_outcome', ''), max_len=20)
    if desired_outcome not in RETURN_OUTCOMES_BY_KIND[kind]:
        errors.append('Choose either to have it made right, or a refund.')

    reason_meta = RETURN_REASON_NEEDS_PHOTO[kind]
    reasons = [clean_str(r, max_len=30) for r in request.form.getlist('reasons')]
    reasons = [r for r in reasons if r in reason_meta]
    if not reasons:
        errors.append('Please select at least one reason.')

    other_text = clean_str(request.form.get('other_reason_text', ''), max_len=500)
    if 'other' in reasons and not other_text:
        errors.append('You ticked Other — please type what went wrong.')

    needs_photo = any(reason_meta.get(r, False) for r in reasons)

    incoming_photos = [f for f in request.files.getlist('photos') if f and f.filename]
    if len(incoming_photos) > RETURN_PHOTOS_MAX:
        errors.append(f'Only {RETURN_PHOTOS_MAX} photos may be attached — the rest were left out.')
        incoming_photos = incoming_photos[:RETURN_PHOTOS_MAX]
    valid_photo_count = 0
    for f in incoming_photos:
        if not allowed_file(f.filename):
            continue
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(0)
        if size <= RETURN_PHOTO_MAX_BYTES:
            valid_photo_count += 1
    if needs_photo and valid_photo_count == 0:
        errors.append('The reason you picked needs at least one photo of the problem.')

    order, booking = None, None
    item_rows = []  # [(OrderItem, quantity)]
    requested_mechanic_name = None

    if kind == 'product':
        oid = clean_int(request.form.get('order_id', ''))
        order = Order.query.filter_by(id=oid, user_id=current_user.id).first() if oid else None
        if not order or order.status not in ('delivered', 'completed'):
            errors.append('That order has to be delivered before you can return anything from it.')
        else:
            window = return_window_info('product', order.delivered_at)
            if window is None or window['expired']:
                errors.append("The 7-day return window for this order has closed. Message the shop "
                              "directly and we'll look at it case by case.")
            existing_open = ReturnRequest.query.filter(
                ReturnRequest.order_id == order.id, ReturnRequest.status.in_(OPEN_RETURN_STATUSES)
            ).first()
            if existing_open:
                errors.append(f'Request RMA-{existing_open.id:03d} is already open for this order.')

            try:
                raw_items = _json.loads(request.form.get('items_json', '[]'))
            except ValueError:
                raw_items = []
            order_items_by_id = {i.id: i for i in OrderItem.query.filter_by(order_id=order.id).all()}
            for ri in (raw_items if isinstance(raw_items, list) else []):
                oiid = clean_int(ri.get('order_item_id', ''))
                qty = clean_int(ri.get('quantity', ''))
                oi = order_items_by_id.get(oiid)
                if oi and 1 <= qty <= oi.quantity:
                    item_rows.append((oi, qty))
            if not item_rows:
                errors.append('Please select at least one item, with how many, to return.')
        what = ', '.join(f'{oi.product.name} ×{qty}' for oi, qty in item_rows) if item_rows else \
            (f'your order (ORD-{order.id:03d})' if order else 'your order')
    else:
        bid = clean_int(request.form.get('booking_id', ''))
        booking = Booking.query.filter_by(id=bid, user_id=current_user.id).first() if bid else None
        if not booking or booking.status != 'completed':
            errors.append('That appointment has to be completed before you can file a warranty claim on it.')
        else:
            window = return_window_info('service', booking.completed_at)
            if window is None or window['expired']:
                errors.append("The 15-day warranty window for this service has closed. Message the shop "
                              "directly and we'll look at it case by case.")
            existing_open = ReturnRequest.query.filter(
                ReturnRequest.booking_id == booking.id, ReturnRequest.status.in_(OPEN_RETURN_STATUSES)
            ).first()
            if existing_open:
                errors.append(f'Request RMA-{existing_open.id:03d} is already open for this appointment.')
            if desired_outcome == 'redo_service' and request.form.get('requested_mechanic') == 'yes' and booking.assigned_mechanic_name:
                requested_mechanic_name = booking.assigned_mechanic_name
        what = booking.service if booking else 'your appointment'

    if errors:
        return jsonify({'success': False, 'errors': errors}), 400

    saved_filenames, _skipped = _save_return_photos(incoming_photos)

    requested_refund_amount = None
    if desired_outcome == 'refund':
        if kind == 'product':
            requested_refund_amount = sum(oi.unit_price * qty for oi, qty in item_rows)
        else:
            requested_refund_amount = booking.total_amount or 0

    rr = ReturnRequest(
        user_id=current_user.id, kind=kind,
        order_id=order.id if order else None, booking_id=booking.id if booking else None,
        reasons=','.join(reasons), other_reason_text=other_text if 'other' in reasons else None,
        desired_outcome=desired_outcome, requested_mechanic_name=requested_mechanic_name,
        requested_refund_amount=requested_refund_amount, photos=','.join(saved_filenames) or None,
    )
    db.session.add(rr)
    db.session.flush()
    for oi, qty in item_rows:
        db.session.add(ReturnRequestItem(return_request_id=rr.id, order_item_id=oi.id, quantity=qty))
    db.session.commit()

    ref = f'RMA-{rr.id:03d}'
    reason_labels = [RETURN_REASON_LABELS[kind].get(r, r) for r in reasons if r != 'other']
    if other_text:
        reason_labels.append(other_text)
    reasons_text = '; '.join(reason_labels)
    outcome_label = RETURN_OUTCOME_LABELS[kind][desired_outcome]
    # The bell and the email must always say the same thing — one body, two
    # deliveries, never two different tellings of the same event.
    filed_title = f'We received your return request ({ref})'
    filed_body = (f"We got your report about {what} ({ref}) — {reasons_text}. You asked for: {outcome_label}. "
                  f"We'll review the evidence and get back to you with a decision.")
    send_notification(
        current_user.id, filed_title, filed_body,
        type='booking' if kind == 'service' else 'order', status='submitted', return_request_id=rr.id,
    )
    if current_user.email:
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;">
          <p>Hi {current_user.fullname},</p>
          <p>{filed_body}</p>
          <p>— MotoTyre North Caloocan</p>
        </div>"""
        _send_gmail(current_user.email, f'{filed_title} — MotoTyre', html)

    for admin in User.query.filter_by(role='admin').all():
        send_notification(
            admin.id, f'New return request ({ref})',
            f'{current_user.fullname} reported {what} — {reasons_text}. Wants: {outcome_label}.',
            type='booking' if kind == 'service' else 'order', status='submitted',
        )

    return jsonify({'success': True, 'ref': ref, 'id': rr.id})


@app.route('/returns/<int:rid>/cancel', methods=['POST'])
@login_required
def cancel_return_request(rid):
    """The customer withdraws their own claim — but only while it's still
    Submitted or Under review. Once the shop has approved it, money or parts
    are already moving, so cancelling from here stops being a self-service
    click and has to be a conversation instead — the button doesn't even
    show past that point, and this is the rule behind it, not the button."""
    rr = ReturnRequest.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
    if rr.status not in ('submitted', 'under_review'):
        return jsonify({'success': False, 'error': 'This request has already been approved — contact the shop directly to change it.'}), 400
    rr.status = 'cancelled'
    rr.cancelled_at = ph_now()
    db.session.commit()
    return jsonify({'success': True})


@app.route('/returns/<int:rid>/add-info', methods=['POST'])
@login_required
def add_return_info(rid):
    """The customer's reply to a 'we need more from you' request — clears
    the pending flag (what actually clears the pinned notification, not
    just opening it) and tells the shop what came in."""
    rr = ReturnRequest.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
    if not rr.awaiting_customer_info:
        return jsonify({'success': False, 'error': 'This request is not waiting on anything from you.'}), 400

    text = clean_str(request.form.get('text', ''), max_len=500)
    new_photos, _skipped = _save_return_photos([f for f in request.files.getlist('photos') if f and f.filename])
    if not text and not new_photos:
        return jsonify({'success': False, 'error': 'Add a note or a photo before sending.'}), 400

    rr.awaiting_customer_info = False
    rr.info_provided_at = ph_now()
    rr.info_provided_text = text or None
    if new_photos:
        existing = [p for p in (rr.photos or '').split(',') if p]
        rr.photos = ','.join((existing + new_photos)[:RETURN_PHOTOS_MAX])
    db.session.commit()

    ref = f'RMA-{rr.id:03d}'
    for admin in User.query.filter_by(role='admin').all():
        send_notification(
            admin.id, f'Customer replied on {ref}',
            f'{current_user.fullname} added more info on {ref}: {text or "(photo only)"}',
            type='booking' if rr.kind == 'service' else 'order', status=rr.status,
        )
    return jsonify({'success': True})


@app.route('/api/booked-slots')
@login_required
def get_booked_slots():
    year  = request.args.get('year',  type=int)
    month = request.args.get('month', type=int)
    if not year or not month:
        return jsonify({})
    start    = date(year, month, 1)
    end      = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    bookings = Booking.query.filter(Booking.date >= start, Booking.date < end, Booking.status != 'cancelled').all()
    result   = {}
    for b in bookings:
        result.setdefault(b.date.strftime('%Y-%m-%d'), []).append(b.time.strftime('%H:%M'))
    return jsonify(result)


@app.route('/api/time-slots')
@login_required
def api_time_slots():
    """Given a date and the services the customer picked, returns every start time
    that fits the combined job length without overlapping an existing booking —
    the shop is a single queue, so this is duration-aware, not just hourly marks."""
    date_str = request.args.get('date', '').strip()
    services_param = request.args.get('services', '').strip()
    if not date_str or not services_param:
        return jsonify({'slots': [], 'total_minutes': 0, 'duration_label': '', 'is_multiday': False})
    try:
        slot_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400

    names = split_service_names(services_param)
    combo = _resolve_service_combo(names)
    if not combo:
        return jsonify({'slots': [], 'total_minutes': 0, 'duration_label': '', 'is_multiday': False})

    existing = Booking.query.filter(Booking.date == slot_date, Booking.status != 'cancelled').all()
    intervals = []
    for b in existing:
        b_start = b.time.hour * 60 + b.time.minute
        intervals.append((b_start, compute_finish_minutes(b_start, b.duration_minutes or DEFAULT_DURATION_MIN)))
    intervals += _gather_blocked_intervals(slot_date)

    now_minutes = None
    if slot_date == ph_now().date():
        now = ph_now()
        now_minutes = now.hour * 60 + now.minute

    # Every fixed slot comes back, available or not — the customer sees why a
    # time is off instead of it just not being there.
    slots = slot_statuses(combo['total_minutes'], intervals, now_minutes)
    return jsonify({
        'slots': [{
            'time':      minutes_to_hhmm(s['start']),
            'end_time':  minutes_to_hhmm(s['end']),
            'label':     minutes_to_ampm(s['start']),
            'end_label': minutes_to_ampm(s['end']),
            'available': s['available'],
            'reason':    s['reason'],
        } for s in slots],
        'total_minutes':  combo['total_minutes'],
        'duration_label': combo['duration_label'],
        'is_multiday':    combo['is_multiday'],
        'min_days':       combo['min_days'],
        'max_days':       combo['max_days'],
    })


@app.route('/api/mechanics')
@login_required
def get_mechanics():
    date_str = request.args.get('date', '').strip()
    time_str = request.args.get('time', '').strip()
    duration = request.args.get('duration', type=int) or DEFAULT_DURATION_MIN

    # Ordered by id (hire/creation order) — a stable stand-in for a rotation
    # queue. Who's actually "on duty" is the roster call every scheduling
    # check in both apps uses: the first N by that order (from today's — or
    # the picked date's — capacity setting), minus anyone individually marked
    # off duty on their own profile.
    try:
        roster_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else None
    except ValueError:
        roster_date = None
    _, _, mechanics = get_on_duty_mechanics(roster_date)

    # A mechanic is busy only if their existing job's time window overlaps the
    # slot being picked — not for every booking they've ever had. Busy mechanics
    # are still returned (marked busy) rather than dropped, so the customer sees
    # the whole crew and why someone isn't available right now.
    busy_names = set()
    if date_str and time_str:
        try:
            slot_date  = datetime.strptime(date_str, '%Y-%m-%d').date()
            start_min  = hhmm_to_minutes(time_str)
            end_min    = compute_finish_minutes(start_min, duration)
            for b in Booking.query.filter(
                Booking.assigned_mechanic_name.isnot(None),
                Booking.date == slot_date,
                Booking.status != 'cancelled'
            ).all():
                b_start = b.time.hour * 60 + b.time.minute
                b_end   = compute_finish_minutes(b_start, b.duration_minutes or DEFAULT_DURATION_MIN)
                if mechanic_overlaps(start_min, end_min, b_start, b_end):
                    busy_names.add(b.assigned_mechanic_name)
        except ValueError:
            pass

    return jsonify([
        {'id': m.id, 'name': m.name, 'specialization': m.specialization, 'busy': m.name in busy_names}
        for m in mechanics
    ])


# ── Notification routes ───────────────────────────────────────────────────────

@app.route('/api/notifications')
@login_required
def get_notifications():
    notifs = Notification.query.filter_by(user_id=current_user.id)\
                               .order_by(Notification.created_at.desc()).limit(50).all()

    return_ids = {n.return_request_id for n in notifs if n.return_request_id}
    claims = {r.id: r for r in ReturnRequest.query.filter(ReturnRequest.id.in_(return_ids)).all()} if return_ids else {}

    def still_pending(n):
        """Read is enough to clear most notifications, but two return
        situations need the real-world thing done, not just a glance: the
        shop waiting on more from the customer, or (for a product claim) the
        part needing to come back before its remedy is carried out."""
        rr = claims.get(n.return_request_id) if n.return_request_id else None
        if rr:
            if rr.awaiting_customer_info:
                return True
            if rr.kind == 'product' and rr.status == 'approved' and not rr.item_returned:
                return True
        return not n.is_read

    return jsonify([{
        'id': n.id, 'title': n.title, 'message': n.message,
        'type': n.type, 'status': n.status, 'is_read': n.is_read,
        'priority': n.priority, 'booking_id': n.booking_id,
        'return_request_id': n.return_request_id, 'still_pending': still_pending(n),
        'created_at': n.created_at.strftime('%Y-%m-%dT%H:%M:%S+08:00')
    } for n in notifs])


@app.route('/api/notifications/<int:nid>/read', methods=['POST'])
@login_required
def read_notification(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    n.is_read = True
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/notifications/read-all', methods=['POST'])
@login_required
def read_all_notifications():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/notifications/<int:nid>/delete', methods=['POST'])
@login_required
def delete_notification(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    db.session.delete(n)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/customer/poll')
@login_required
def customer_poll():
    bookings = Booking.query.filter_by(user_id=current_user.id)\
        .order_by(Booking.created_at.desc()).all()
    orders = Order.query.filter_by(user_id=current_user.id)\
        .order_by(Order.created_at.desc()).all()
    unread_count = Notification.query.filter_by(
        user_id=current_user.id, is_read=False).count()
    return jsonify({
        'bookings': [{'id': b.id, 'status': b.status} for b in bookings],
        'orders':   [{'id': o.id, 'status': o.status} for o in orders],
        'unread_notifications': unread_count
    })


# ── Order confirm received ────────────────────────────────────────────────────

@app.route('/customer/order/<int:oid>/confirm-received', methods=['POST'])
@login_required
def confirm_order_received(oid):
    order = Order.query.get_or_404(oid)
    if order.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Unauthorized.'}), 403
    if order.status != 'shipped':
        return jsonify({'success': False, 'message': 'Order is not in shipped status.'}), 400
    if order.payment_method == 'cash' and order.delivery_method == 'pickup':
        return jsonify({'success': False,
                        'message': 'Please settle the cash payment at the counter — staff will mark this order complete.'}), 400
    order.status = 'completed'
    if not order.delivered_at:
        order.delivered_at = ph_now()
    db.session.commit()
    _is_ship = order.delivery_method == 'ship'
    send_notification(current_user.id, 'Order Received!',
        with_stamp(
            f'You confirmed receipt of your order ORD-{order.id:03d}. Thank you!',
            'completed', is_ship=_is_ship),
        type='order', status='completed')
    for admin in User.query.filter_by(role='admin').all():
        send_notification(admin.id, 'Order Received ✅',
            f'{current_user.fullname} confirmed receipt of ORD-{order.id:03d}.',
            type='order', status='completed')
    return jsonify({'success': True, 'new_status': 'completed'})


# ── Payment routes ────────────────────────────────────────────────────────────

@app.route('/pay/order/<int:oid>')
@login_required
def pay_order(oid):
    order = Order.query.get_or_404(oid)
    if order.user_id != current_user.id:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('customer_dashboard'))
    if order.status not in ['pending']:
        flash('This order cannot be paid online.', 'warning')
        return redirect(url_for('customer_dashboard'))
    items_desc  = ", ".join([f"{item.product.name} x{item.quantity}" for item in order.items])
    description = f"MotoTyre Order #{order.id:03d}: {items_desc[:100]}"
    result = create_gcash_payment(amount=order.total_amount, description=description, order_id=order.id, origin=request.host_url)
    if result["success"]:
        order.payment_method = 'gcash'
        db.session.commit()
        return redirect(result["checkout_url"])
    flash('Could not create payment. Please try again.', 'danger')
    return redirect(url_for('customer_dashboard'))


@app.route('/pay/booking/<int:bid>')
@login_required
def pay_booking(bid):
    booking = Booking.query.get_or_404(bid)
    if booking.user_id != current_user.id:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('customer_dashboard'))
    if booking.status not in ['pending', 'confirmed']:
        flash('This booking cannot be paid online.', 'warning')
        return redirect(url_for('customer_dashboard'))
    SERVICE_PRICES = {
        'Oil Change': 350, 'Tire Change – Front': 150, 'Tire Change – Rear': 150,
        'Tire Change – Both': 250, 'Brake Inspection': 100, 'Brake Pad Replacement': 300,
        'Chain Cleaning & Lube': 150, 'Chain Replacement': 400, 'Spark Plug Replacement': 200,
        'Battery Check & Replacement': 250, 'General Checkup': 200, 'Full Tune-up': 800,
        'CVT Cleaning': 500, 'FI Cleaning': 600, 'ECU Remapping': 1500,
        'Full Overhaul': 3000, 'Overhaul': 2500,
    }
    amount = 500
    for service_name, price in SERVICE_PRICES.items():
        if service_name.lower() in booking.service.lower():
            amount = price
            break
    description = f"MotoTyre Booking #{booking.id}: {booking.service}"
    result = create_gcash_payment(amount=amount, description=description, booking_id=booking.id, origin=request.host_url)
    if result["success"]:
        booking.payment_method = 'gcash'
        db.session.commit()
        return redirect(result["checkout_url"])
    flash('Could not create payment. Please try again.', 'danger')
    return redirect(url_for('customer_dashboard'))


@app.route('/payment/success')
def payment_success():
    order_id    = request.args.get('order_id', '').strip()
    booking_id  = request.args.get('booking_id', '').strip()
    checkout_id = request.args.get('checkout_id', '').strip()
    gcash_email = None

    if checkout_id:
        try:
            headers = {"Authorization": f"Basic {base64.b64encode(f'{PAYMONGO_SECRET_KEY}:'.encode()).decode()}"}
            res = requests.get(f"{PAYMONGO_API_URL}/checkout_sessions/{checkout_id}", headers=headers)
            if res.status_code == 200:
                attrs = res.json()['data']['attributes']
                gcash_email = attrs.get('email') or (attrs.get('billing') or {}).get('email')
        except Exception as e:
            print(f"[paymongo] could not fetch checkout session: {e}")

    if order_id.isdigit():
        try:
            order = Order.query.get(int(order_id))
            if order and order.status == 'awaiting_payment':
                order.status = 'confirmed'
                db.session.commit()
                items_desc = ", ".join([f"{item.product.name} x{item.quantity}" for item in order.items])
                _is_ship   = order.delivery_method == 'ship'
                _dest      = shipping_destination(order.ship_address)
                _next_step = (f' We are getting it ready to ship to {_dest}.' if _is_ship and _dest
                              else ' We are getting it ready to ship.' if _is_ship
                              else '')
                send_notification(order.user_id, 'Order Confirmed! ✅',
                    with_stamp(
                        f'Your payment for Order ORD-{order.id:03d} ({items_desc}) worth '
                        f'₱{order.total_amount:.2f} has been confirmed.{_next_step}',
                        'confirmed', is_ship=_is_ship),
                    type='order', status='confirmed')
            if order and order.status == 'confirmed':
                send_order_receipt_email(order, override_email=gcash_email)
        except Exception as e:
            print(f"[payment/success] order error: {e}")

    if booking_id.isdigit():
        try:
            booking = Booking.query.get(int(booking_id))
            if booking and booking.status == 'pending':
                booking.status = 'confirmed'
                db.session.commit()
        except Exception as e:
            print(f"[payment/success] booking error: {e}")

    return redirect(safe_return_origin(request.args.get('origin')) + '/customer/dashboard')


@app.route('/payment/failed')
def payment_failed():
    order_id   = request.args.get('order_id')
    booking_id = request.args.get('booking_id')
    if order_id:
        try:
            order = Order.query.get(int(order_id))
            if order and order.status == 'awaiting_payment':
                order.status = 'confirmed'
                db.session.commit()
            if order and order.status == 'awaiting_payment':
                Notification.query.filter(
                    Notification.user_id == order.user_id,
                    Notification.type == 'order',
                    Notification.created_at >= order.created_at
                ).delete(synchronize_session=False)
                for item in order.items:
                    product = Product.query.get(item.product_id)
                    if product:
                        product.stock += item.quantity
                OrderItem.query.filter_by(order_id=order.id).delete()
                db.session.delete(order)
                db.session.commit()
        except:
            db.session.rollback()
    if booking_id:
        try:
            booking = Booking.query.get(int(booking_id))
            if booking and booking.status == 'pending':
                Notification.query.filter(
                    Notification.user_id == booking.user_id,
                    Notification.type == 'booking',
                    Notification.created_at >= booking.created_at
                ).delete(synchronize_session=False)
                db.session.delete(booking)
                db.session.commit()
        except:
            db.session.rollback()
    return redirect(safe_return_origin(request.args.get('origin')) + '/customer/dashboard')


@app.route('/webhook/paymongo', methods=['POST'])
def paymongo_webhook():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400
    event_type = data.get('data', {}).get('attributes', {}).get('type')
    resource   = data.get('data', {}).get('attributes', {}).get('data', {})
    if event_type == 'link.payment.paid':
        metadata   = resource.get('attributes', {}).get('metadata', {})
        order_id   = metadata.get('order_id')
        booking_id = metadata.get('booking_id')
        if order_id:
            order = Order.query.get(int(order_id))
            if order and order.status in ['pending', 'awaiting_payment']:
                order.status = 'confirmed'
                order.payment_method = 'gcash'
                db.session.commit()
                _is_ship = order.delivery_method == 'ship'
                send_notification(order.user_id, 'Payment Received!',
                    with_stamp(
                        f'Your payment for Order ORD-{order.id:03d} has been confirmed.'
                        + (' We are getting it ready to ship.' if _is_ship else ''),
                        'confirmed', is_ship=_is_ship),
                    type='order', status='confirmed')
                send_order_receipt_email(order)
        if booking_id:
            booking = Booking.query.get(int(booking_id))
            if booking and booking.status == 'pending':
                booking.status = 'confirmed'
                booking.payment_method = 'gcash'
                db.session.commit()
                send_notification(booking.user_id, 'Booking Payment Received!',
                    f'Your payment for {booking.service} on {booking.date.strftime("%b %d")} has been confirmed.',
                    type='booking', status='confirmed')
    return jsonify({'success': True})


@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/api/services')
def api_services():
    try:
        svcs = Service.query.filter_by(is_active=True).order_by(Service.name).all()
        return jsonify([{
            'name': s.name, 'price': s.price,
            'duration_minutes': s.duration_minutes or DEFAULT_DURATION_MIN,
            'is_multiday': bool(s.is_multiday),
            'duration_label': s.duration_label or format_duration(s.duration_minutes or DEFAULT_DURATION_MIN),
        } for s in svcs])
    except Exception:
        return jsonify([])


# ── Scheduler ─────────────────────────────────────────────────────────────────

from apscheduler.schedulers.background import BackgroundScheduler
import atexit

scheduler = BackgroundScheduler(timezone='Asia/Manila')
scheduler.add_job(func=check_upcoming_bookings, trigger='interval', minutes=1,
                  id='booking_reminder_job', replace_existing=True)
scheduler.add_job(func=check_day_before_reminders, trigger='interval', minutes=30,
                  id='day_before_reminder_job', replace_existing=True)
scheduler.start()
print('[SCHEDULER] Booking reminder service started — checking every 1 minute (15-min) / 30 minutes (day-before)')
atexit.register(lambda: scheduler.shutdown())


# ── Migrations ────────────────────────────────────────────────────────────────

with app.app_context():
    for _stmt in [
        "ALTER TABLE `order` ADD COLUMN receipt_sent TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE service ADD COLUMN description VARCHAR(200) NOT NULL DEFAULT ''",
        "ALTER TABLE service ADD COLUMN price FLOAT NOT NULL DEFAULT 0",
        "UPDATE service SET is_active=1 WHERE is_active IS NULL",
        "ALTER TABLE booking ADD COLUMN odometer INT DEFAULT NULL",
        "ALTER TABLE user ADD COLUMN address VARCHAR(255) DEFAULT NULL",
        "ALTER TABLE user ADD COLUMN motorcycle_model VARCHAR(100) DEFAULT NULL",
        "ALTER TABLE user ADD COLUMN profile_pic VARCHAR(255) DEFAULT NULL",
        "ALTER TABLE user ADD COLUMN email_verified TINYINT(1) NOT NULL DEFAULT 0",
        "ALTER TABLE booking ADD COLUMN booking_batch VARCHAR(36) DEFAULT NULL",
    ]:
        try:
            from sqlalchemy import text as _t
            db.session.execute(_t(_stmt))
            db.session.commit()
        except Exception:
            db.session.rollback()

with app.app_context():
    try:
        db.create_all()
        _default_services = [
            ('Ball Race Installation',      'Steering ball race replacement',        350),
            ('Brake Cleaning',              'Brake system cleaning',                 200),
            ('Change Brake Pad',            'Brake pad replacement',                 300),
            ('Change Oil',                  'Engine oil replacement',                350),
            ('CVT Cleaning',                'CVT belt & pulley cleaning',            500),
            ('CVT Upgrade',                 'CVT performance upgrade',               700),
            ('Diagnostic (API Tech / MST)', 'Electronic diagnostic scan',            300),
            ('FI Cleaning',                 'Fuel injection system cleaning',        600),
            ('Full Maintenance Package',    'Complete maintenance service',         1200),
            ('General Rewiring',            'Full electrical rewiring',              500),
            ('Horn Installation',           'Horn install & wiring',                 150),
            ('Overhaul',                    'Full engine overhaul',                 2500),
            ('Remap',                       'ECU remapping & tuning',               1500),
            ('Rubber Link Stopper',         'Rubber link stopper replacement',       100),
            ('Suspension Tuning',           'Front & rear suspension setup',         400),
            ('Throttle Body Cleaning',      'Clean throttle body assembly',          400),
            ('Top Overhaul',                'Top-end engine rebuild',               1500),
            ('Tune-Up',                     'Spark plug, filters & adjustment',      800),
        ]
        for name, desc, price in _default_services:
            svc = Service.query.filter_by(name=name).first()
            if not svc:
                db.session.add(Service(name=name, description=desc, price=price))
            else:
                if not svc.description: svc.description = desc
                if not svc.price:       svc.price = price
        db.session.commit()
        print('[MIGRATION] Service table ready')
    except Exception as e:
        db.session.rollback()
        print('[MIGRATION] Service table error:', e)


if __name__ == '__main__':
    app.run(debug=True, port=5000, use_reloader=False)