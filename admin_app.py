from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, redirect, url_for, flash, request, session, jsonify, make_response, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, date
from sqlalchemy import func, and_, or_, not_
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from io import BytesIO
from security import (clean_str, clean_int, clean_float, is_valid_email, is_valid_phone, validate_booking_status, validate_order_status,
    ALLOWED_RETURN_OUTCOMES, RETURN_OUTCOMES_BY_KIND, RETURN_REASONS, OPEN_RETURN_STATUSES)
from order_notifications import order_status_message, with_stamp, shipping_destination, ship_notice, ship_email_extra, build_order_email, ship_on_the_way_email, booking_receipt_email
from delivery_zones import format_delivery_address
from cloud_storage import upload_image
from service_duration import (split_service_names, DEFAULT_DURATION_MIN, MULTIDAY_INTAKE_MIN,
    compute_finish_time, mechanic_origin_note, compute_finish_minutes, all_slot_starts,
    SHOP_CLOSE_MIN, mechanic_overlaps, minutes_to_ampm, hhmm_to_minutes, minutes_to_hhmm,
    validate_booking, MAX_BOOKINGS_PER_DAY, max_jobs_one_mechanic, capacity_bottleneck,
    SLOT_GRANULARITY_MIN, SHOP_OPEN_MIN, real_end_minutes, multiday_progress, format_duration,
    MULTIDAY_MIN_DAYS, MULTIDAY_MAX_DAYS, is_working_day, _overlaps, working_days_between,
    slot_statuses, MECHANIC_SPECIALIZATIONS)
import json
import re
import os, uuid, random, string, base64, requests
from urllib.parse import urlparse
import threading

# App setup

# Same DATABASE_URL env var and same default as app.py — one database, read
# identically by both apps, never two separately-maintained connection strings.
DATABASE_URL = os.getenv('DATABASE_URL', 'mysql+pymysql://root:@localhost:3306/mototyre').strip()

admin_app = Flask(__name__, template_folder='templates', static_folder='static')
# A hosted database (Aiven, etc.) requires an SSL connection; a local XAMPP
# one typically isn't even configured for it — so this is opt-in by host,
# never something that has to be remembered as a separate setting.
_db_host = urlparse(DATABASE_URL.replace('mysql+pymysql://', 'mysql://', 1)).hostname or ''
_engine_options = {} if _db_host in ('localhost', '127.0.0.1', '') else {'connect_args': {'ssl': {'ssl': {}}}}
admin_app.config.update(
    SECRET_KEY=os.getenv('ADMIN_SECRET_KEY', 'mototyre-admin-secret-key-aP5nQ9vX2kR8mT6yW1'),
    SESSION_COOKIE_NAME='mototyre_admin_session',
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', 'false').lower() == 'true',
    SQLALCHEMY_DATABASE_URI=DATABASE_URL,
    SQLALCHEMY_ENGINE_OPTIONS=_engine_options,
    SQLALCHEMY_TRACK_MODIFICATIONS=False
)

db = SQLAlchemy(admin_app)
login_manager = LoginManager(admin_app)
login_manager.login_view = 'admin_login'

@admin_app.context_processor
def _inject_current_year():
    """Makes {{ current_year }} available in every template — the footer
    copyright line reads the real year instead of whatever was hardcoded
    when that page was last touched."""
    return {'current_year': datetime.now().year,
            'SHOP_ADDRESS': os.getenv('SHOP_ADDRESS', 'Saranay Rd, Brgy 171 Bagumbong, Caloocan City, Metro Manila 1421')}

# Gmail config

GMAIL_SCOPES     = ["https://www.googleapis.com/auth/gmail.send"]
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_CREDS_FILE = "credentials.json"
GMAIL_SENDER     = os.getenv("GMAIL_SENDER", "mototyre0505@gmail.com")
OTP_EXPIRY_MINS  = 2

def _write_gmail_secret_from_env(path, env_var):
    """Both Gmail files are correctly gitignored, so a fresh deploy never has
    them on disk. If the matching env var is set, write it out once at
    startup so the existing file-based OAuth flow below needs no other
    changes. The env var is expected to be base64 of the file's exact
    contents, not the raw JSON — raw JSON has quotes and newlines that are
    exactly the kind of thing a web form's env-var box mangles on paste;
    base64 is one line of plain characters with nothing in it to corrupt.
    Local dev already has the real files, no env var is set, and this is a
    no-op."""
    if not os.path.exists(path):
        contents = os.getenv(env_var)
        if contents:
            try:
                decoded = base64.b64decode(contents.strip()).decode('utf-8')
            except Exception:
                decoded = contents  # fall back to raw, in case it wasn't base64
            with open(path, "w") as f:
                f.write(decoded)

_write_gmail_secret_from_env(GMAIL_TOKEN_FILE, "GMAIL_TOKEN_JSON")
_write_gmail_secret_from_env(GMAIL_CREDS_FILE, "GMAIL_CREDENTIALS_JSON")

# PayMongo config

PAYMONGO_SECRET_KEY = os.getenv("PAYMONGO_SECRET_KEY", "sk_test_qzA2hw8wmbB6AR46TSWYjKPV")
PAYMONGO_API_URL = "https://api.paymongo.com/v1"
# /payment/success and /payment/failed only exist on the customer app, never
# this one — so this always has to point there, not at admin_app's own host.
BASE_URL = os.getenv("BASE_URL", "https://mototyre-customer.onrender.com")

def create_gcash_payment(amount, description, order_id=None, booking_id=None):
    headers = {
        "Authorization": f"Basic {base64.b64encode(f'{PAYMONGO_SECRET_KEY}:'.encode()).decode()}",
        "Content-Type": "application/json"
    }
    payload = {
        "data": {
            "attributes": {
                "line_items": [{"name": description, "quantity": 1, "amount": int(amount * 100), "currency": "PHP"}],
                "payment_method_types": ["gcash"],
                "success_url": f"{BASE_URL}/payment/success?order_id={order_id or ''}&booking_id={booking_id or ''}",
                "cancel_url": f"{BASE_URL}/payment/failed?order_id={order_id or ''}&booking_id={booking_id or ''}"
            }
        }
    }
    response = requests.post(f"{PAYMONGO_API_URL}/checkout_sessions", json=payload, headers=headers)
    if response.status_code == 200:
        data = response.json()
        return {"success": True, "checkout_url": data["data"]["attributes"]["checkout_url"], "checkout_id": data["data"]["id"]}
    return {"success": False, "error": response.json()}

# Gmail helpers

_gmail_service_lock = threading.Lock()
_gmail_service_cache = None

def _get_gmail_service():
    global _gmail_service_cache
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow  = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        _gmail_service_cache = None
    with _gmail_service_lock:
        if _gmail_service_cache is None:
            _gmail_service_cache = build("gmail", "v1", credentials=creds)
    return _gmail_service_cache

def _send_gmail(to, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["to"], msg["from"], msg["subject"] = to, GMAIL_SENDER, subject
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    def _do_send():
        try:
            _get_gmail_service().users().messages().send(userId="me", body={"raw": raw}).execute()
            print(f"[GMAIL ADMIN] Sent to {to}: {subject}", flush=True)
        except Exception as e:
            print(f"[GMAIL ADMIN] Send failed to {to}: {e}", flush=True)
    threading.Thread(target=_do_send, daemon=True).start()

def send_otp_email(email, otp, purpose="login"):
    configs = {
        "login": ("Your MotoTyres Admin login code", "Admin Login Verification", "complete your admin login"),
        "reset": ("Your MotoTyres Admin password reset code", "Admin Password Reset", "reset your admin password"),
    }
    subject, heading, action = configs.get(purpose, configs["login"])
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:32px;
                border:1px solid #e5e7eb;border-radius:8px;">
      <h2 style="color:#111827;">{heading}</h2>
      <p style="color:#6b7280;">Use the code below to {action}. Expires in {OTP_EXPIRY_MINS} minutes.</p>
      <div style="font-size:36px;font-weight:bold;letter-spacing:12px;color:#111827;
                  background:#f3f4f6;padding:20px;border-radius:6px;text-align:center;margin:24px 0;">{otp}</div>
      <p style="color:#9ca3af;font-size:13px;">If you didn't request this, ignore this email.</p>
    </div>"""
    _send_gmail(email, subject, html)

# Models

class User(db.Model, UserMixin):
    id               = db.Column(db.Integer, primary_key=True)
    fullname         = db.Column(db.String(100), nullable=False)
    email            = db.Column(db.String(100), unique=True, nullable=False)
    phone            = db.Column(db.String(20), nullable=False)
    password_hash    = db.Column(db.String(255), nullable=False)
    role             = db.Column(db.String(20), default='customer')
    billing_seen_at  = db.Column(db.DateTime, nullable=True)  # admin: when Billing History was last opened — anything paid after this is NEW
    address          = db.Column(db.String(255))
    motorcycle_model = db.Column(db.String(100))
    profile_pic      = db.Column(db.String(255))
    profile_pic_blob = db.Column(db.LargeBinary(length=2**24 - 1), nullable=True)  # the picture itself, when no cloud storage is configured — disk files are lost on redeploy
    profile_pic_mime = db.Column(db.String(40), nullable=True)
    email_verified   = db.Column(db.Boolean, default=False)
    account_status   = db.Column(db.String(20), default='active')   # active | deactivated | banned
    is_flagged       = db.Column(db.Boolean, default=False)
    bookings         = db.relationship('Booking', backref='customer', lazy=True)
    orders           = db.relationship('Order', backref='customer', lazy=True)
    default_delivery_name     = db.Column(db.String(100))
    default_delivery_mobile   = db.Column(db.String(20))
    default_delivery_city     = db.Column(db.String(50))
    default_delivery_barangay = db.Column(db.String(50))
    default_delivery_street   = db.Column(db.String(255))
    default_delivery_zip      = db.Column(db.String(10))

    def set_password(self, pw):   self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)


class OTPRecord(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    email      = db.Column(db.String(100), nullable=False, index=True)
    otp        = db.Column(db.String(10), nullable=False)
    purpose    = db.Column(db.String(10), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used       = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))


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
    created_at       = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    reminder_sent    = db.Column(db.Boolean, default=False)
    day_before_reminder_sent = db.Column(db.Boolean, default=False)
    assigned_mechanic_name           = db.Column(db.String(100))  # who is actually doing the work — shop changes this freely
    assigned_mechanic_specialization = db.Column(db.String(100))
    preferred_mechanic_name           = db.Column(db.String(100))  # who the customer asked for, or None — never overwritten after booking creation
    preferred_mechanic_specialization = db.Column(db.String(100))
    contact_name    = db.Column(db.String(100))
    contact_mobile  = db.Column(db.String(20))
    odometer        = db.Column(db.Integer)
    is_archived     = db.Column(db.Boolean, default=False)
    total_amount    = db.Column(db.Float, default=0)
    walkin_customer_id = db.Column(db.Integer, nullable=True)
    booking_batch   = db.Column(db.String(36), nullable=True)
    duration_minutes = db.Column(db.Integer, default=60)  # total estimated job length
    end_time         = db.Column(db.Time, nullable=True)  # computed: time + duration
    is_multiday      = db.Column(db.Boolean, default=False)
    overrun_minutes  = db.Column(db.Integer, default=0)  # counter-staff-recorded extra time on top of duration_minutes
    was_rescheduled  = db.Column(db.Boolean, default=False)  # set once, first time this booking's date/time changes after creation
    completed_at     = db.Column(db.DateTime, nullable=True)  # when status actually reached completed — the warranty window's start
    invoice_payment_method = db.Column(db.String(10), nullable=True)  # customer's pick on the post-completion invoice: 'cash' or 'gcash'
    invoice_paid     = db.Column(db.Boolean, default=False)   # true once that invoice is actually settled (cash confirmed, or GCash succeeded)
    invoice_paid_at  = db.Column(db.DateTime, nullable=True)
    invoice_cash_received = db.Column(db.Float, nullable=True)
    invoice_change        = db.Column(db.Float, nullable=True)
    invoice_receipt_no    = db.Column(db.String(20), nullable=True)
    invoice_gcash_ref     = db.Column(db.String(50), nullable=True)
    invoice_received_by   = db.Column(db.String(100), nullable=True)   # staff member who took the cash at the counter
    actual_start_at       = db.Column(db.DateTime, nullable=True)  # when the shop really started the job (may be earlier than the booked time)
    inspection_at    = db.Column(db.DateTime, nullable=True)  # when the booking reached Inspection & Estimation
    in_progress_at   = db.Column(db.DateTime, nullable=True)  # when work started (In Progress)
    late_notice_at   = db.Column(db.DateTime, nullable=True)   # when the customer was last told about running late / back on time
    late_notice_text = db.Column(db.String(120), nullable=True) # e.g. 'New finish 11:30 AM' — shown on the Day Schedule card
    late_notice_emailed = db.Column(db.Boolean, default=False)
    assign_notice_at    = db.Column(db.DateTime, nullable=True)   # when the customer was last told who their mechanic is
    assign_notice_kind  = db.Column(db.String(10), nullable=True)  # 'assigned' | 'changed'
    assign_notice_emailed = db.Column(db.Boolean, default=False)


class BlockedSlot(db.Model):
    """An admin-blocked start time — staff meeting, parts delivery — that
    removes that slot from the customer booking flow immediately without
    touching any real booking."""
    id         = db.Column(db.Integer, primary_key=True)
    date       = db.Column(db.Date, nullable=False)
    time       = db.Column(db.Time, nullable=False)
    reason     = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    __table_args__ = (db.UniqueConstraint('date', 'time', name='uq_blocked_slot'),)


class Mechanic(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(100), nullable=False)
    specialization = db.Column(db.String(100), nullable=False)
    status         = db.Column(db.String(20), default='available')
    phone          = db.Column(db.String(20))
    note           = db.Column(db.Text)  # a short profile note shown under their name — not customer-visible
    manual_customer = db.Column(db.String(200))  # fallback only — shown when there is no real booking, never overrides one
    manual_service  = db.Column(db.String(300))  # comma-separated service names, same fallback rule
    created_at     = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))


class MechanicAttendance(db.Model):
    """One row per mechanic per day — kept forever so past days can still be
    viewed. A missing row just means "not timed in yet"."""
    id          = db.Column(db.Integer, primary_key=True)
    mechanic_id = db.Column(db.Integer, db.ForeignKey('mechanic.id'), nullable=False)
    date        = db.Column(db.Date, nullable=False)
    time_in     = db.Column(db.Time, nullable=True)
    time_out    = db.Column(db.Time, nullable=True)
    is_absent   = db.Column(db.Boolean, default=False)
    __table_args__ = (db.UniqueConstraint('mechanic_id', 'date', name='uq_mechanic_attendance_day'),)


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
    image_url   = db.Column(db.String(500))
    created_at  = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    order_items = db.relationship('OrderItem', backref='product', lazy=True)


class Order(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total_amount    = db.Column(db.Float, nullable=False)
    status          = db.Column(db.String(20), default='pending')
    payment_method  = db.Column(db.String(20), default='cash')
    payment_ref     = db.Column(db.String(80), nullable=True)  # PayMongo checkout id for GCash orders
    delivery_method = db.Column(db.String(20), default='pickup')
    ship_address    = db.Column(db.Text)
    created_at      = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    items           = db.relationship('OrderItem', backref='order', lazy=True)
    is_archived     = db.Column(db.Boolean, default=False)
    walkin_customer_id = db.Column(db.Integer, nullable=True)
    delivered_at    = db.Column(db.DateTime, nullable=True)  # when status actually reached delivered/completed — the return window's start
    # ── Ship to Address (Lalamove) ──
    paid_at                 = db.Column(db.DateTime, nullable=True)
    shipping_vehicle        = db.Column(db.String(40), nullable=True)
    shipping_reason         = db.Column(db.String(200), nullable=True)
    shipping_prices         = db.Column(db.Text, nullable=True)
    shipping_options_sent_at= db.Column(db.DateTime, nullable=True)
    shipping_deadline_at    = db.Column(db.DateTime, nullable=True)
    shipping_reminder_at    = db.Column(db.DateTime, nullable=True)
    shipping_option         = db.Column(db.String(20), nullable=True)
    shipping_fee            = db.Column(db.Float, nullable=True)
    lalamove_fare           = db.Column(db.Float, nullable=True)
    paymongo_payment_id     = db.Column(db.String(80), nullable=True)
    rider_type              = db.Column(db.String(12), nullable=True)
    rider_name              = db.Column(db.String(100), nullable=True)
    rider_phone             = db.Column(db.String(20), nullable=True)
    rider_plate             = db.Column(db.String(30), nullable=True)
    rider_model = db.Column(db.String(60), nullable=True)
    pay_started_at = db.Column(db.DateTime, nullable=True)
    lalamove_link           = db.Column(db.String(500), nullable=True)
    shipping_started_at     = db.Column(db.DateTime, nullable=True)
    received_at             = db.Column(db.DateTime, nullable=True)
    received_reminder_at    = db.Column(db.DateTime, nullable=True)
    completed_by_shop       = db.Column(db.Boolean, default=False)
    completion_proof        = db.Column(db.String(500), nullable=True)
    cancelled_by            = db.Column(db.String(12), nullable=True)
    cancel_reason           = db.Column(db.String(300), nullable=True)
    refunded_at             = db.Column(db.DateTime, nullable=True)
    refund_amount           = db.Column(db.Float, nullable=True)
    refund_reason           = db.Column(db.String(300), nullable=True)
    stock_restocked         = db.Column(db.Boolean, nullable=True)
    contact_name    = db.Column(db.String(100), nullable=True)  # typed name for an in-store sale with no real account
    is_instore      = db.Column(db.Boolean, default=False)      # created from In-Store Parts Purchase — gets a SALE- ref, not ORD-
    instore_seen    = db.Column(db.Boolean, default=True)       # False only for a fresh in-store sale, until Billing History is opened


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
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


class Feedback(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(100), default='')
    email      = db.Column(db.String(150), default='')
    service    = db.Column(db.String(100), default='')
    rating     = db.Column(db.Integer, default=0)
    message    = db.Column(db.Text, default='')
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))


class AppState(db.Model):
    """Tiny key/value store for single global values that don't belong to any
    one row — e.g. when the admin last looked at Transaction History, used to
    mark everything since then as new."""
    __tablename__ = 'app_state'
    key   = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(200))


class WalkInCustomer(db.Model):
    __tablename__ = 'walkin_customer'
    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(100), nullable=False)
    phone            = db.Column(db.String(20), nullable=False)
    motorcycle_model = db.Column(db.String(100))
    motorcycle_plate = db.Column(db.String(20))
    notes            = db.Column(db.Text)
    created_at       = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))


class Quotation(db.Model):
    __tablename__ = 'quotation'
    id               = db.Column(db.Integer, primary_key=True)
    customer_name    = db.Column(db.String(100), nullable=False)
    customer_phone   = db.Column(db.String(20), nullable=False)
    motorcycle_model = db.Column(db.String(100))
    motorcycle_plate = db.Column(db.String(20))
    notes            = db.Column(db.Text)
    total_amount     = db.Column(db.Float, default=0)
    # pending/accepted/rejected: the walk-in counter flow (admin decides).
    # awaiting_customer/approved/declined: a booking-linked inspection quote
    # sent remotely — the CUSTOMER decides, via the booking details modal.
    status           = db.Column(db.String(20), default='pending')
    booking_id       = db.Column(db.Integer, nullable=True)  # set only for an Inspection & Estimation quote
    inspected_by     = db.Column(db.String(100), nullable=True)  # mechanic name from the Inspection tab
    created_by       = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at       = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    decided_at       = db.Column(db.DateTime, nullable=True)  # when approved/declined — set once, never on conversion
    not_covered         = db.Column(db.Boolean, default=False)  # back job only: the mechanic found the problem is NOT from the shop's work
    not_covered_reason  = db.Column(db.Text, nullable=True)
    not_covered_photos  = db.Column(db.String(500), nullable=True)  # comma-separated filenames under static/return_evidence/
    items            = db.relationship('QuotationItem', backref='quotation', lazy=True, cascade='all, delete-orphan')


class QuotationItem(db.Model):
    __tablename__ = 'quotation_item'
    id           = db.Column(db.Integer, primary_key=True)
    quotation_id = db.Column(db.Integer, db.ForeignKey('quotation.id'), nullable=False)
    item_type    = db.Column(db.String(10), default='service')  # 'service', 'product', or 'finding'
    name         = db.Column(db.String(150), nullable=False)
    quantity     = db.Column(db.Integer, default=1)
    unit_price   = db.Column(db.Float, nullable=False)
    locked       = db.Column(db.Boolean, default=False)  # the booked service row — can't be removed client-side


class JobOrder(db.Model):
    __tablename__ = 'job_order'
    id               = db.Column(db.Integer, primary_key=True)
    quotation_id     = db.Column(db.Integer, db.ForeignKey('quotation.id'), nullable=True)
    customer_name    = db.Column(db.String(100), nullable=False)
    customer_phone   = db.Column(db.String(20), nullable=False)
    motorcycle_model = db.Column(db.String(100))
    motorcycle_plate = db.Column(db.String(20))
    notes            = db.Column(db.Text)
    total_amount     = db.Column(db.Float, default=0)
    status           = db.Column(db.String(20), default='pending')  # pending, in_progress, completed, cancelled
    mechanic_name    = db.Column(db.String(100))
    start_at         = db.Column(db.DateTime, nullable=True)   # when the mechanic started on it (walk-in: when it was created)
    est_minutes      = db.Column(db.Integer, nullable=True)    # how long the mechanic is tied up — +30m / +1h extend it
    mechanic_done_at = db.Column(db.DateTime, nullable=True)   # "Mark Done": frees the mechanic right away
    created_by       = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at       = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    completed_at     = db.Column(db.DateTime, nullable=True)
    items            = db.relationship('JobOrderItem', backref='job_order', lazy=True, cascade='all, delete-orphan')


class JobOrderItem(db.Model):
    __tablename__ = 'job_order_item'
    id           = db.Column(db.Integer, primary_key=True)
    job_order_id = db.Column(db.Integer, db.ForeignKey('job_order.id'), nullable=False)
    item_type    = db.Column(db.String(10), default='service')
    name         = db.Column(db.String(150), nullable=False)
    quantity     = db.Column(db.Integer, default=1)
    unit_price   = db.Column(db.Float, nullable=False)


class Payment(db.Model):
    __tablename__ = 'payment'
    id             = db.Column(db.Integer, primary_key=True)
    job_order_id   = db.Column(db.Integer, db.ForeignKey('job_order.id'), nullable=False)
    amount         = db.Column(db.Float, nullable=False)
    payment_method = db.Column(db.String(20), nullable=False, default='cash')  # cash, gcash
    reference_no   = db.Column(db.String(50), nullable=True)   # GCash ref number
    paid_at        = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    created_by     = db.Column(db.Integer, db.ForeignKey('user.id'))
    job_order      = db.relationship('JobOrder', backref=db.backref('payment', uselist=False))


class Notification(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title      = db.Column(db.String(120), nullable=False)
    message    = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(30), default='update')
    status     = db.Column(db.String(30))
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    priority    = db.Column(db.Boolean, default=False)  # needs a customer decision — stays pinned to the top, unread, until opened
    booking_id  = db.Column(db.Integer, nullable=True)  # lets "open" land on the specific booking, not a list
    return_request_id = db.Column(db.Integer, nullable=True)  # same, for a return/warranty claim
    order_id    = db.Column(db.Integer, nullable=True)  # lets "View order" open the order's details


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
    created_at      = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    decided_at      = db.Column(db.DateTime)
    resolved_at     = db.Column(db.DateTime)
    cancelled_at    = db.Column(db.DateTime)
    awaiting_customer_info = db.Column(db.Boolean, default=False)
    info_request_note      = db.Column(db.Text)
    info_provided_at       = db.Column(db.DateTime)
    info_provided_text     = db.Column(db.Text)
    item_returned          = db.Column(db.Boolean, default=False)
    item_returned_at       = db.Column(db.DateTime)
    redo_date          = db.Column(db.Date)
    redo_time          = db.Column(db.Time)
    redo_mechanic_name = db.Column(db.String(100))
    redo_booking_id    = db.Column(db.Integer, nullable=True)  # the real, zero-charge Booking this back job writes into the shop calendar
    internal_notes     = db.Column(db.Text)  # shop-only — the customer never sees this
    replacement_shipped_at  = db.Column(db.DateTime)  # replacement left the shop, on its way to the customer
    replacement_arrived_at  = db.Column(db.DateTime)  # replacement delivered to the address below
    completed_at            = db.Column(db.DateTime)  # customer confirmed receipt — the actual end of a replacement claim
    delivery_name      = db.Column(db.String(100))
    delivery_mobile    = db.Column(db.String(20))
    delivery_city       = db.Column(db.String(50))
    delivery_barangay  = db.Column(db.String(50))
    delivery_street     = db.Column(db.String(255))
    delivery_zip        = db.Column(db.String(10))
    is_pickup           = db.Column(db.Boolean, default=False)  # customer picks the replacement up at the shop — no delivery_* fields set
    refund_method          = db.Column(db.String(10), nullable=True)    # 'cash' (claim at the shop counter) | 'gcash' — refund requests only
    gcash_number           = db.Column(db.String(20), nullable=True)    # where to send it when refund_method == 'gcash'
    gcash_account_name     = db.Column(db.String(100), nullable=True)
    refund_receipt         = db.Column(db.String(500), nullable=True)   # GCash receipt screenshot (URL, or a filename under static/return_evidence/) uploaded when the refund was sent
    cash_ready_at          = db.Column(db.DateTime, nullable=True)      # cash refunds: when the shop told the customer it's ready to claim at the counter
    info_requested_at      = db.Column(db.DateTime, nullable=True)      # when the shop last asked the customer for more ("Shop asked: …" in History)
    refund_given_by        = db.Column(db.String(100), nullable=True)   # staff member who handed over a cash refund
    replacement_out_of_stock = db.Column(db.Boolean, default=False)   # the replacement part was out of stock when the request was filed
    customer_at_shop         = db.Column(db.Boolean, default=False)   # the customer (or the person they sent) is at the counter right now
    replacement_reserved     = db.Column(db.Boolean, default=False)   # a replacement has been set aside for this claim (taken out of stock)
    customer_arrived_at      = db.Column(db.DateTime, nullable=True)  # first visit: the customer arrived to hand in the item
    customer_back_at         = db.Column(db.DateTime, nullable=True)  # second visit: back to pick up the replacement that was set aside
    completed_by_shop        = db.Column(db.Boolean, default=False)   # the shop completed it (the customer did not press I Received My Replacement)


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

# Helpers

def ph_now():
    return datetime.utcnow() + timedelta(hours=8)

def send_notification(user_id, title, message, type='update', status=None, booking_id=None, priority=False,
                       return_request_id=None, collapse_minutes=None, order_id=None):
    """The one place a customer notification gets written. Email and the bell
    always carry the exact same title/message — see each call site, which
    builds one (title, message) pair and reuses it for both channels.
    priority=True pins it unread at the top of the customer's bell (and the
    admin outbox) until they actually open it — reserved for changes a
    customer might want to push back on: a delay, a mechanic swap, a
    reschedule, or any shop-initiated cancellation.

    collapse_minutes: if set, and this exact user+booking+type already has a
    notification from within that many minutes, it's overwritten in place
    (title, message, re-marked unread) instead of inserting a new row — a
    flurry of admin changes to the same booking within the window reads as
    one notification carrying the latest state, not one per edit."""
    if collapse_minutes and booking_id:
        cutoff = ph_now() - timedelta(minutes=collapse_minutes)
        existing = Notification.query.filter(
            Notification.user_id == user_id, Notification.booking_id == booking_id,
            Notification.type == type, Notification.created_at >= cutoff,
        ).order_by(Notification.created_at.desc()).first()
        if existing:
            existing.title = title
            existing.message = message
            existing.status = status
            existing.priority = priority
            existing.is_read = False
            existing.created_at = ph_now()
            db.session.commit()
            return
    db.session.add(Notification(user_id=user_id, title=title, message=message, type=type,
                                 status=status, booking_id=booking_id, priority=priority,
                                 return_request_id=return_request_id, order_id=order_id))
    db.session.commit()


def _notify_order_customer(order, title, body, status=None, extra_html='', email=None):
    """Writes the order's bell notification and — unless this is a walk-in/
    in-store sale with no real account — emails the exact same title/body to
    the customer's Gmail, mirroring _notify_customer()'s pattern for bookings."""
    send_notification(order.user_id, title, body, type='order', status=status, order_id=order.id)
    if order.is_instore or order.walkin_customer_id:
        return False
    customer = User.query.get(order.user_id)
    if not (customer and customer.email):
        return False
    if email:
        _send_gmail(customer.email, email[0], email[1])
        return True
    html = build_order_email(customer.fullname, body, extra_html, ph_now())
    _send_gmail(customer.email, f'{title} — MotoTyres North Caloocan', html)
    return True


def booking_service_price(service_str):
    """Sum the catalog price for every service in a booking's (possibly combined,
    comma-separated) service string — a plain name lookup misses combo bookings."""
    names = split_service_names(service_str)
    if not names:
        return 0
    found = {s.name: s.price for s in Service.query.filter(Service.name.in_(names), Service.is_active == True).all()}
    return sum(found.get(n, 0) for n in names)


admin_app.jinja_env.globals['booking_service_price'] = booking_service_price


def booking_finish_time(b):
    """A booking's finish time, break-aware. Prefers the value stored at creation
    time; falls back to computing it via the SAME shared helper the customer-side
    booking flow uses, for legacy bookings made before duration tracking existed —
    so the admin view and the customer view can never disagree."""
    if b.end_time:
        return b.end_time
    return compute_finish_time(b.time, b.duration_minutes or DEFAULT_DURATION_MIN)


admin_app.jinja_env.globals['booking_finish_time'] = booking_finish_time


def is_booking_overdue(b, now):
    """A booking is overdue when its window has fully passed but nobody ever
    marked it completed or cancelled — the shop-side signal that a no-show
    or a forgotten status update needs a human look. Multi-day jobs (Full/Top
    Overhaul) run on a different, days-long lifecycle, so they're excluded
    here rather than flagged the moment their drop-off slot passes."""
    if b.is_multiday:
        return False
    if b.status not in ('confirmed', 'in_progress', 'inprogress'):
        return False
    finish = booking_finish_time(b)
    return datetime.combine(b.date, finish) < now


admin_app.jinja_env.globals['is_booking_overdue'] = is_booking_overdue


EARLY_START_WINDOW_MIN = 30


def start_eligibility(b, now):
    """Can this confirmed booking be started right now? Returns a dict:
    state — 'ready' (scheduled time reached), 'early_ok' (inside the 30-minute
    early window and the mechanic is free), 'early_busy' (inside the window
    but the mechanic is still on another job), 'too_early' (outside the window).
    Also carries minutes_early, opens_at (when 'early_ok' becomes possible)
    and, for 'early_busy', the message to show."""
    scheduled = datetime.combine(b.date, b.time)
    if scheduled <= now:
        return {'state': 'ready', 'minutes_early': 0}
    mins_early = int((scheduled - now).total_seconds() // 60) + (1 if (scheduled - now).total_seconds() % 60 else 0)
    opens_at = scheduled - timedelta(minutes=EARLY_START_WINDOW_MIN)
    info = {
        'minutes_early': mins_early,
        'opens_at': opens_at.strftime('%b %d, %I:%M %p'),
        'scheduled': scheduled.strftime('%b %d, %Y %I:%M %p'),
    }
    if now < opens_at:
        return {**info, 'state': 'too_early'}
    mech = b.assigned_mechanic_name
    if mech:
        others = Booking.query.filter(
            Booking.assigned_mechanic_name == mech, Booking.id != b.id,
            Booking.status.in_(['inspection', 'in_progress', 'inprogress']),
        ).all()
        for o in others:
            # Only a job that has really been started and hasn't hit its end counts.
            began = o.actual_start_at or datetime.combine(o.date, o.time)
            end = began + timedelta(minutes=(o.duration_minutes or DEFAULT_DURATION_MIN) + (o.overrun_minutes or 0))
            if not o.is_multiday and began <= now < end:
                return {**info, 'state': 'early_busy', 'message': f'{mech} is still on another job.'}
    return {**info, 'state': 'early_ok'}


admin_app.jinja_env.globals['start_eligibility'] = start_eligibility


def _quotation_breakdown(items):
    """Splits a quotation into the three things a customer actually decides
    over: the service they already booked (locked=True, always charged),
    the materials the shop physically used on their bike (item_type='product',
    not locked — a part that's already installed can't be un-charged just
    because the customer declines the rest), and the extra inspection
    findings (everything else — labor/diagnosis beyond the booked service,
    the only thing an actual decline removes). Works on either real
    QuotationItem rows or the raw item dicts save_quotation() gets before
    they're written to the DB."""
    def amt(i):
        price = i.unit_price if hasattr(i, 'unit_price') else i['unit_price']
        qty   = i.quantity   if hasattr(i, 'quantity')   else i['quantity']
        return float(price) * int(qty)
    def is_locked(i):
        return i.locked if hasattr(i, 'locked') else bool(i.get('locked'))
    def item_type(i):
        return i.item_type if hasattr(i, 'item_type') else i.get('item_type', 'service')

    booked    = sum(amt(i) for i in items if is_locked(i))
    materials = sum(amt(i) for i in items if not is_locked(i) and item_type(i) == 'product')
    extra     = sum(amt(i) for i in items if not is_locked(i) and item_type(i) != 'product')
    return {
        'booked': booked, 'materials': materials, 'extra': extra,
        'approve_total': booked + materials + extra,
        'decline_total': booked + materials,
    }


def _mechanic_initials(name):
    parts = (name or '').split()
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _generate_otp(length=6):
    return "".join(random.choices(string.digits, k=length))

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


# Auth routes

@admin_app.route('/')
def admin_home():
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard'))
    return redirect(url_for('admin_login'))


@admin_app.route('/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        if current_user.role in ['admin', 'staff']:
            return redirect(url_for('admin_dashboard'))
        # Customer somehow got here — boot them out
        logout_user()
        flash('This portal is for admin and staff only.', 'danger')

    if request.method == 'POST':
        email    = clean_str(request.form.get('email', ''), max_len=254).lower()
        password = request.form.get('password', '')
        if not is_valid_email(email):
            flash('Invalid email address.', 'danger')
            return redirect(url_for('admin_login'))
        user = User.query.filter_by(email=email).first()

        # Only allow admin/staff roles
        if user and user.role not in ['admin', 'staff']:
            flash('Access denied. This portal is for admin and staff only.', 'danger')
            return redirect(url_for('admin_login'))

        if user and user.check_password(password):
            otp = _save_otp(email, purpose="login")
            send_otp_email(email, otp, purpose="login")
            session['admin_pending_login_email'] = email
            return redirect(url_for('admin_login'))

        flash('Invalid email or password.', 'danger')

    return render_template('admin_login.html')


@admin_app.route('/verify-otp', methods=['GET', 'POST'])
def admin_verify_otp():
    email = session.get('admin_pending_login_email')
    if not email:
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        result = _verify_otp(email, request.form.get('otp', ''), purpose="login")
        if result['valid']:
            session.pop('admin_pending_login_email', None)
            user = User.query.filter_by(email=email).first()
            login_user(user, remember=False)
            return redirect(url_for('admin_dashboard'))
        flash(result['message'], 'danger')

    return redirect(url_for('admin_login'))


@admin_app.route('/resend-otp')
def admin_resend_otp():
    email = session.get('admin_pending_login_email')
    if not email:
        flash('Session expired. Please log in again.', 'danger')
        return redirect(url_for('admin_login'))
    otp = _save_otp(email, purpose="login")
    try:
        send_otp_email(email, otp, purpose="login")
        flash('A new OTP has been sent to your email.', 'info')
    except Exception as e:
        flash(f'Could not resend OTP: {e}', 'danger')
    return redirect(url_for('admin_login'))

@admin_app.route('/forgot-password', methods=['GET', 'POST'])
def admin_forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user  = User.query.filter_by(email=email).first()
        if user and user.role in ['admin', 'staff']:
            otp = _save_otp(email, purpose="reset")
            try:
                send_otp_email(email, otp, purpose="reset")
                session['admin_pending_reset_email'] = email
            except Exception as e:
                flash(f'Could not send reset code: {e}', 'danger')
                return redirect(url_for('admin_forgot_password'))
        flash('If that admin email is registered, a reset code has been sent.', 'info')
        return redirect(url_for('admin_forgot_verify'))
    return render_template('admin_forgot_password.html')


@admin_app.route('/forgot-password/verify', methods=['GET', 'POST'])
def admin_forgot_verify():
    email = session.get('admin_pending_reset_email')
    if not email:
        flash('Session expired.', 'danger')
        return redirect(url_for('admin_forgot_password'))
    if request.method == 'POST':
        result = _verify_otp(email, request.form.get('otp', ''), purpose="reset")
        if result['valid']:
            session['admin_reset_otp_verified'] = True
            return redirect(url_for('admin_reset_password'))
        flash(result['message'], 'danger')
        return redirect(url_for('admin_forgot_verify'))
    return render_template('admin_forgot_verify.html', email=email)


@admin_app.route('/forgot-password/resend-otp')
def admin_forgot_resend_otp():
    from flask import jsonify as _jsonify
    email = session.get('admin_pending_reset_email')
    if not email:
        return _jsonify({'ok': False}), 400
    otp = _save_otp(email, purpose="reset")
    try:
        send_otp_email(email, otp, purpose="reset")
    except Exception:
        return _jsonify({'ok': False}), 500
    return _jsonify({'ok': True})


@admin_app.route('/forgot-password/reset', methods=['GET', 'POST'])
def admin_reset_password():
    email    = session.get('admin_pending_reset_email')
    verified = session.get('admin_reset_otp_verified')
    if not email or not verified:
        flash('Session expired.', 'danger')
        return redirect(url_for('admin_forgot_password'))
    if request.method == 'POST':
        pw, cpw = request.form.get('password', ''), request.form.get('confirm_password', '')
        if len(pw) < 8:
            flash('Password must be at least 8 characters.', 'danger')
            return redirect(url_for('admin_reset_password'))
        if pw != cpw:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('admin_reset_password'))
        user = User.query.filter_by(email=email).first()
        if user:
            user.set_password(pw)
            db.session.commit()
        session.pop('admin_pending_reset_email', None)
        session.pop('admin_reset_otp_verified', None)
        flash('Password reset successful! You can now log in.', 'success')
        return redirect(url_for('admin_login'))
    return render_template('admin_reset_password.html', email=email)


@admin_app.route('/logout')
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for('admin_login'))


# Admin routes

def _revenue_transactions(date_from=None, date_to=None):
    """Every money-moving event in one list: completed bookings, paid job
    orders, completed orders (online + in-store), and released refunds
    (negative amount, dated when the refund was actually released — not
    the original sale date, so a report already printed never changes).
    This is the single source of truth behind the Dashboard's revenue
    card, the Reports & Sales page, and Transaction History — they only
    ever disagree if one of them stops calling this."""
    def in_range(dt):
        if dt is None:
            return False
        d = dt.date() if hasattr(dt, 'date') else dt
        if date_from and d < date_from:
            return False
        if date_to and d > date_to:
            return False
        return True

    txns = []

    for b in Booking.query.filter(Booking.status == 'completed', Booking.total_amount > 0,
                                   Booking.is_archived == False).all():
        d = b.completed_at or b.created_at
        if not in_range(d):
            continue
        cust = b.contact_name if b.walkin_customer_id else (b.customer.fullname if b.customer else 'Unknown')
        txns.append({
            'ref': f'BKG-{b.id:03d}', 'type': 'booking', 'customer': cust or 'Unknown',
            'payment': (b.invoice_payment_method or b.payment_method or 'cash').upper(),
            'amount': b.total_amount, 'date': d, 'status': 'Completed', 'record_id': b.id,
        })

    for jo in JobOrder.query.join(Payment, Payment.job_order_id == JobOrder.id).all():
        d = jo.payment.paid_at
        if not in_range(d):
            continue
        txns.append({
            'ref': f'JO-{jo.id:03d}', 'type': 'job_order', 'customer': jo.customer_name or 'Unknown',
            'payment': (jo.payment.payment_method or 'cash').upper(),
            'amount': jo.total_amount, 'date': d, 'status': 'Paid', 'record_id': jo.id,
        })

    _walkin_cache = {}
    _ship_paid = (Order.delivery_method == 'ship') & Order.paid_at.isnot(None) & Order.status.in_(['done_packaging', 'shipping', 'completed'])
    for o in Order.query.filter(((Order.status == 'completed') & ((Order.delivery_method != 'ship') | Order.paid_at.isnot(None))) | _ship_paid).filter(Order.items.any()).all():
        d = (o.paid_at if (o.delivery_method == 'ship' and o.paid_at) else o.created_at)
        if not in_range(d):
            continue
        is_sale = bool(o.is_instore or o.walkin_customer_id)
        if o.is_instore:
            cust = o.contact_name or 'Walk-in'
        elif o.walkin_customer_id:
            if o.walkin_customer_id not in _walkin_cache:
                _walkin_cache[o.walkin_customer_id] = WalkInCustomer.query.get(o.walkin_customer_id)
            wc = _walkin_cache[o.walkin_customer_id]
            cust = wc.name if wc else 'Walk-in'
        else:
            cust = o.customer.fullname if o.customer else 'Unknown'
        txns.append({
            'ref': (f'SALE-{o.id:03d}' if is_sale else f'ORD-{o.id:03d}'),
            'type': ('sale' if is_sale else 'online'), 'customer': cust,
            'payment': (o.payment_method or 'cash').upper(),
            'amount': (o.total_amount or 0) + ((o.shipping_fee or 0) if o.delivery_method == 'ship' else 0),
            'date': d, 'status': 'Completed', 'record_id': o.id,
        })

    for rr in ReturnRequest.query.filter(ReturnRequest.resolution == 'refund', ReturnRequest.status == 'resolved').all():
        d = rr.resolved_at
        if not in_range(d):
            continue
        u = User.query.get(rr.user_id)
        orig_ref = f'ORD-{rr.order_id:03d}' if rr.kind == 'product' else f'BKG-{rr.booking_id:03d}'
        txns.append({
            'ref': f'RF-{rr.id:03d}', 'type': 'refund', 'customer': u.fullname if u else 'Unknown',
            'payment': '—', 'amount': -(rr.refund_amount or 0), 'date': d, 'status': 'Refunded',
            'record_id': rr.id, 'refund_for': orig_ref, 'return_ref': _return_ref(rr),
        })

    txns.sort(key=lambda t: t['date'], reverse=True)
    return txns


def _order_refund_totals():
    """{order_id: (total_refunded, is_full)} for every order with at least
    one released refund. A full refund (amount >= what was paid) means the
    order shouldn't count toward Orders Fulfilled; a partial one still
    counts, just billed for less."""
    totals = {}
    for rr in ReturnRequest.query.filter(ReturnRequest.resolution == 'refund', ReturnRequest.status == 'resolved',
                                          ReturnRequest.order_id.isnot(None)).all():
        totals[rr.order_id] = totals.get(rr.order_id, 0) + (rr.refund_amount or 0)
    if not totals:
        return {}
    orders_by_id = {o.id: o for o in Order.query.filter(Order.id.in_(totals.keys())).all()}
    return {oid: (amt, amt >= (orders_by_id[oid].total_amount if oid in orders_by_id else 0) - 0.01)
            for oid, amt in totals.items()}


def _reports_summary(date_from=None, date_to=None):
    """Everything the Dashboard's revenue card, the Reports & Sales page,
    and Transaction History all read from — one computation, so they can
    never disagree for the same date range."""
    def in_range(d):
        if date_from or date_to:
            dd = d.date() if hasattr(d, 'date') else d
            if date_from and dd < date_from:
                return False
            if date_to and dd > date_to:
                return False
        return True

    txns = _revenue_transactions(date_from, date_to)

    gross_by_type = {'booking': 0.0, 'job_order': 0.0, 'sale': 0.0, 'online': 0.0}
    for t in txns:
        if t['type'] in gross_by_type:
            gross_by_type[t['type']] += t['amount']
    gross_total = sum(gross_by_type.values())

    refund_txns = [t for t in txns if t['type'] == 'refund']
    refund_total = sum(-t['amount'] for t in refund_txns)
    net_revenue = gross_total - refund_total

    services_completed = sum(1 for t in txns if t['type'] in ('booking', 'job_order'))

    refund_totals = _order_refund_totals()
    fully_refunded_order_ids = {oid for oid, (amt, is_full) in refund_totals.items() if is_full}
    orders_fulfilled = sum(1 for t in txns if t['type'] in ('sale', 'online') and t['record_id'] not in fully_refunded_order_ids)
    refunded_orders_excluded = sum(1 for t in txns if t['type'] in ('sale', 'online') and t['record_id'] in fully_refunded_order_ids)

    order_status_counts = {}
    for o in Order.query.filter(Order.is_archived == False).filter(Order.items.any()).all():
        if not in_range(o.created_at):
            continue
        eff_status = 'refunded' if o.id in fully_refunded_order_ids else o.status
        order_status_counts[eff_status] = order_status_counts.get(eff_status, 0) + 1

    # A combo booking's .service is a comma-joined list ("Brake Cleaning, Change
    # Oil") — each service in it counts on its own. A back job's .service is
    # prefixed "Back job — <service>"; it counts under that same service (not
    # its own bucket) and also adds to service_backjob_tally for the "includes
    # X back job" note.
    service_tally = {}
    service_backjob_tally = {}
    for b in Booking.query.filter(Booking.status != 'cancelled', Booking.is_archived == False).all():
        if not in_range(b.completed_at or b.created_at):
            continue
        is_backjob = b.service.startswith('Back job — ')
        base = b.service[len('Back job — '):] if is_backjob else b.service
        for name in split_service_names(base):
            service_tally[name] = service_tally.get(name, 0) + 1
            if is_backjob:
                service_backjob_tally[name] = service_backjob_tally.get(name, 0) + 1
    for jo in JobOrder.query.filter(JobOrder.status != 'cancelled').all():
        if not in_range(jo.created_at):
            continue
        for item in jo.items:
            if item.item_type == 'service':
                service_tally[item.name] = service_tally.get(item.name, 0) + item.quantity
    top_services_list = [(name, count, service_backjob_tally.get(name, 0))
                          for name, count in sorted(service_tally.items(), key=lambda kv: kv[1], reverse=True)[:8]]

    # Mechanic Performance (completed bookings only) and the "parts added in
    # quotations" half of Top Products Sold share the same completed-bookings
    # + latest-quotation-per-booking lookup, so they're built together.
    completed_bookings = [b for b in Booking.query.filter(Booking.status == 'completed', Booking.is_archived == False).all()
                           if in_range(b.completed_at or b.created_at)]
    _mp_booking_ids = [b.id for b in completed_bookings]
    _mp_latest_quotations = {}
    if _mp_booking_ids:
        for q in Quotation.query.filter(Quotation.booking_id.in_(_mp_booking_ids)).order_by(Quotation.created_at.asc()).all():
            _mp_latest_quotations[q.booking_id] = q

    def _relevant_quotation_items(q):
        return q.items if q.status == 'approved' else [i for i in q.items if i.locked or i.item_type == 'product']

    def _labor_income(b, q):
        if not q:
            return b.total_amount or 0
        return sum(i.unit_price * i.quantity for i in _relevant_quotation_items(q) if i.item_type != 'product')

    mechanic_tally = {}   # name -> [completed, back_jobs, labor_income]
    product_sales = {}    # name -> [units, revenue]
    for b in completed_bookings:
        q = _mp_latest_quotations.get(b.id)
        if b.assigned_mechanic_name:
            row = mechanic_tally.setdefault(b.assigned_mechanic_name, [0, 0, 0.0])
            row[0] += 1
            if b.service.startswith('Back job — '):
                row[1] += 1
            row[2] += _labor_income(b, q)
        if q:
            for i in _relevant_quotation_items(q):
                if i.item_type == 'product':
                    prow = product_sales.setdefault(i.name, [0, 0.0])
                    prow[0] += i.quantity
                    prow[1] += i.quantity * i.unit_price

    # Completed job orders count toward their mechanic too — labor only, same
    # as bookings (parts lines are product sales, not the mechanic's income).
    for jo in JobOrder.query.filter(JobOrder.status == 'completed').all():
        if not jo.mechanic_name or not in_range(jo.completed_at or jo.created_at):
            continue
        row = mechanic_tally.setdefault(jo.mechanic_name, [0, 0, 0.0])
        row[0] += 1
        row[2] += sum(i.unit_price * i.quantity for i in jo.items if i.item_type != 'product')

    mechanic_performance = sorted(
        [{'name': name, 'completed': c, 'backjobs': bj, 'income': inc} for name, (c, bj, inc) in mechanic_tally.items()],
        key=lambda r: r['completed'], reverse=True)

    # Top Products Sold — online orders + in-store sales (both are Order rows),
    # plus parts billed through job orders (quotation parts already folded in
    # above). Fully-refunded orders are excluded entirely; for a partial refund
    # only the returned units (from the refund's own item lines) are taken off.
    refunded_qty = {}   # order_item_id -> units refunded
    for ri, rr in (db.session.query(ReturnRequestItem, ReturnRequest)
                   .join(ReturnRequest, ReturnRequest.id == ReturnRequestItem.return_request_id)
                   .filter(ReturnRequest.resolution == 'refund', ReturnRequest.status == 'resolved').all()):
        refunded_qty[ri.order_item_id] = refunded_qty.get(ri.order_item_id, 0) + ri.quantity
    for o in Order.query.filter(Order.status != 'cancelled', Order.is_archived == False).all():
        if not in_range(o.created_at) or o.id in fully_refunded_order_ids:
            continue
        for item in o.items:
            qty = item.quantity - refunded_qty.get(item.id, 0)
            if qty <= 0:
                continue
            pname = item.product.name if item.product else 'Unknown Product'
            prow = product_sales.setdefault(pname, [0, 0.0])
            prow[0] += qty
            prow[1] += qty * item.unit_price
    for jo in JobOrder.query.filter(JobOrder.status != 'cancelled').all():
        if not in_range(jo.created_at):
            continue
        for item in jo.items:
            if item.item_type == 'product':
                prow = product_sales.setdefault(item.name, [0, 0.0])
                prow[0] += item.quantity
                prow[1] += item.quantity * item.unit_price
    top_products_list = sorted(
        [{'name': name, 'units': u, 'revenue': rev} for name, (u, rev) in product_sales.items()],
        key=lambda r: r['units'], reverse=True)[:10]

    return {
        'transactions': txns,
        'gross_by_type': gross_by_type,
        'gross_total': gross_total,
        'refund_total': refund_total,
        'refund_count': len(refund_txns),
        'net_revenue': net_revenue,
        'services_completed': services_completed,
        'orders_fulfilled': orders_fulfilled,
        'refunded_orders_excluded': refunded_orders_excluded,
        'order_status_counts': order_status_counts,
        'top_services': top_services_list,
        'mechanic_performance': mechanic_performance,
        'top_products': top_products_list,
    }


def require_admin_or_staff(f):
    """Decorator: ensure only admin/staff can access a route."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ['admin', 'staff']:
            flash('Access denied.', 'danger')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


@admin_app.route('/dashboard')
@login_required
@require_admin_or_staff
def admin_dashboard():
    cleanup_abandoned_gcash_orders()
    # Net of refunds, all-time — the same _reports_summary() the Reports &
    # Sales page and Transaction History read from, so this card can never
    # drift from either of them.
    _rpt = _reports_summary()
    _total_rev = _rpt['net_revenue']
    all_orders      = Order.query.filter_by(is_archived=False).filter(Order.items.any()).order_by(Order.created_at.desc()).all()
    archived_orders = Order.query.filter_by(is_archived=True).filter(Order.items.any()).order_by(Order.created_at.desc()).all()

    # A refund claim against an order overrides what the order's own row
    # shows in Manage Orders — the customer doesn't care that fulfillment
    # says "Completed" once the money is on its way back.
    _refund_order_ids = [o.id for o in all_orders + archived_orders]
    _refund_claims = {}
    if _refund_order_ids:
        for rr in ReturnRequest.query.filter(
            ReturnRequest.order_id.in_(_refund_order_ids),
            ReturnRequest.resolution == 'refund',
        ).order_by(ReturnRequest.created_at.asc()).all():
            _refund_claims[rr.order_id] = rr  # last one wins if more than one over time
    for o in all_orders + archived_orders:
        rr = _refund_claims.get(o.id)
        o.refund_status_label = None
        if rr and rr.status == 'resolved':
            o.refund_status_label = 'Refunded'
        elif rr and rr.status == 'approved' and rr.item_returned:
            o.refund_status_label = 'Item Received'
    order_ship_json = json.dumps({
        str(o.id): {'delivery': str(o.delivery_method or 'pickup'),
                    'address': str(o.ship_address or ''),
                    'payment': str(o.payment_method or 'cash')}
        for o in all_orders + archived_orders
    })
    _booking_status_counts = dict(db.session.query(Booking.status, func.count(Booking.id)).filter(Booking.is_archived == False).group_by(Booking.status).all())
    # 'inprogress' (no underscore) is a legacy spelling some old rows may carry;
    # fold it into 'in_progress' so every view of this count reads one number.
    if 'inprogress' in _booking_status_counts:
        _booking_status_counts['in_progress'] = _booking_status_counts.get('in_progress', 0) + _booking_status_counts.pop('inprogress')

    # Same three "nothing collected yet" queries the Billing page itself uses
    # for its Pending tab — kept in lockstep so the sidebar count and the
    # page it links to never disagree.
    _paid_jo_ids = [r[0] for r in db.session.query(Payment.job_order_id).filter(Payment.job_order_id != None).all()]
    _pending_billing_count = (
        JobOrder.query.filter(JobOrder.status == 'in_progress',
                               ~JobOrder.id.in_(_paid_jo_ids) if _paid_jo_ids else True).count()
        + Booking.query.filter(Booking.status.in_(['in_progress', 'inprogress']),
                                Booking.payment_method.in_(['cash', None]),
                                Booking.walkin_customer_id == None,
                                Booking.is_archived == False).count()
        + Order.query.filter(Order.delivery_method == 'pickup', Order.status == 'shipped',
                              Order.walkin_customer_id == None, Order.is_archived == False)
                     .filter(Order.items.any()).count()
        # Invoices waiting at the counter — the customer told us they're
        # paying cash, so this is actual work for the cashier, same as the
        # queries above. Invoices still waiting on the customer to even pick
        # a method aren't counted here; there's nothing for the shop to do yet.
        + Booking.query.filter(Booking.status == 'completed', Booking.invoice_paid == False,
                                Booking.invoice_payment_method == 'cash',
                                Booking.is_archived == False).count()
        # In-store sales nobody's opened History to see yet.
        + Order.query.filter(Order.is_instore == True, Order.instore_seen == False).count()
    )

    _all_bookings_list = Booking.query.filter_by(is_archived=False).order_by(Booking.created_at.desc()).all()
    _q_booking_ids = [b.id for b in _all_bookings_list]
    _latest_quotations = {}
    if _q_booking_ids:
        # ordered oldest-first so the last write into the dict is the most recent quotation per booking
        for q in Quotation.query.filter(Quotation.booking_id.in_(_q_booking_ids)).order_by(Quotation.created_at.asc()).all():
            _latest_quotations[q.booking_id] = q
    for b in _all_bookings_list:
        b.quotation = _latest_quotations.get(b.id)

    # Bookings sitting in Inspection & Estimation with a quotation out for
    # the customer's answer — the one "nothing to do but wait" count that's
    # genuinely useful to call out separately on the dashboard.
    _awaiting_quotation_count = sum(
        1 for b in _all_bookings_list
        if b.status == 'inspection' and b.quotation and b.quotation.status == 'awaiting_customer'
    )

    return render_template('admin_dashboard.html',
        pending_billing_count=_pending_billing_count,
        billing_new_count=_billing_new_count(current_user),
        total_bookings=Booking.query.filter_by(is_archived=False).count(),
        total_orders=Order.query.filter_by(is_archived=False).filter(Order.items.any()).count(),
        total_users=User.query.count(),
        total_revenue=f'{_total_rev:,.2f}',
        booking_status_counts=_booking_status_counts,
        order_status_counts=_rpt['order_status_counts'],
        top_services=_rpt['top_services'],
        rpt=_rpt,
        new_users_today=User.query.filter(func.date(User.id) == date.today()).count(),
        recent_bookings=_all_bookings_list[:5],
        awaiting_quotation_count=_awaiting_quotation_count,
        all_bookings=_all_bookings_list,
        all_orders=all_orders,
        all_products=Product.query.all(),
        all_users=User.query.all(),
        all_services=Service.query.order_by(Service.name).all(),
        mechanic_specializations=MECHANIC_SPECIALIZATIONS,
        archived_orders=archived_orders,
        archived_bookings=Booking.query.filter_by(is_archived=True).order_by(Booking.created_at.desc()).all(),
        all_feedback=Feedback.query.order_by(Feedback.is_read.asc(), Feedback.created_at.desc()).all(),
        order_ship_json=order_ship_json,
        now=ph_now(),
        mech_status=mechanic_status_map(), mech_labels=MECH_LABELS,
        today=ph_now().date(),
        customer_origin=BASE_URL,
    )


@admin_app.route('/api/dashboard-stats')
@login_required
@require_admin_or_staff
def api_dashboard_stats():
    """Powers the Dashboard overview's 15-second auto-refresh — the same
    numbers admin_dashboard() computes for the initial page load, kept to
    just what the stat cards / chart / Pending Actions / Recent Bookings
    actually need, so polling this stays cheap."""
    _total_rev = _reports_summary()['net_revenue']

    _booking_status_counts = dict(db.session.query(Booking.status, func.count(Booking.id)).filter(Booking.is_archived == False).group_by(Booking.status).all())
    if 'inprogress' in _booking_status_counts:
        _booking_status_counts['in_progress'] = _booking_status_counts.get('in_progress', 0) + _booking_status_counts.pop('inprogress')

    _all_bookings_list = Booking.query.filter_by(is_archived=False).order_by(Booking.created_at.desc()).limit(50).all()
    _q_booking_ids = [b.id for b in _all_bookings_list]
    _latest_quotations = {}
    if _q_booking_ids:
        for q in Quotation.query.filter(Quotation.booking_id.in_(_q_booking_ids)).order_by(Quotation.created_at.asc()).all():
            _latest_quotations[q.booking_id] = q
    for b in _all_bookings_list:
        b.quotation = _latest_quotations.get(b.id)

    _awaiting_quotation_count = sum(
        1 for b in _all_bookings_list
        if b.status == 'inspection' and b.quotation and b.quotation.status == 'awaiting_customer'
    )

    _paid_jo_ids = [r[0] for r in db.session.query(Payment.job_order_id).filter(Payment.job_order_id != None).all()]
    _pending_billing_count = (
        JobOrder.query.filter(JobOrder.status == 'in_progress',
                               ~JobOrder.id.in_(_paid_jo_ids) if _paid_jo_ids else True).count()
        + Booking.query.filter(Booking.status.in_(['in_progress', 'inprogress']),
                                Booking.payment_method.in_(['cash', None]),
                                Booking.walkin_customer_id == None,
                                Booking.is_archived == False).count()
        + Order.query.filter(Order.delivery_method == 'pickup', Order.status == 'shipped',
                              Order.walkin_customer_id == None, Order.is_archived == False)
                     .filter(Order.items.any()).count()
        + Booking.query.filter(Booking.status == 'completed', Booking.invoice_paid == False,
                                Booking.invoice_payment_method == 'cash',
                                Booking.is_archived == False).count()
        + Order.query.filter(Order.is_instore == True, Order.instore_seen == False).count()
    )

    recent_bookings = []
    for b in _all_bookings_list[:5]:
        cust_name = b.contact_name if b.walkin_customer_id else (b.customer.fullname if b.customer else 'Unknown')
        recent_bookings.append({
            'id': b.id, 'ref': f'BKG-{b.id:03d}', 'customer': cust_name,
            'service': b.service[len('Back job — '):] if b.service.startswith('Back job — ') else b.service,
            'is_backjob': b.service.startswith('Back job — '),
            'date': b.date.strftime('%b %d, %Y'), 'time': b.time.strftime('%I:%M %p'),
            'status': b.status,
            'awaiting_quotation': bool(b.status == 'inspection' and b.quotation and b.quotation.status == 'awaiting_customer'),
        })

    return jsonify({
        'total_bookings': Booking.query.filter_by(is_archived=False).count(),
        'total_orders': Order.query.filter_by(is_archived=False).filter(Order.items.any()).count(),
        'total_users': User.query.count(),
        'total_revenue': f'{_total_rev:,.2f}',
        'booking_status_counts': _booking_status_counts,
        'order_status_counts': dict(db.session.query(Order.status, func.count(Order.id)).group_by(Order.status).all()),
        'new_users_today': User.query.filter(func.date(User.id) == date.today()).count(),
        'awaiting_quotation_count': _awaiting_quotation_count,
        'pending_billing_count': _pending_billing_count,
        'billing_new_count': _billing_new_count(current_user),
        'recent_bookings': recent_bookings,
    })


@admin_app.route('/feedback/<int:fid>/read', methods=['POST'])
@login_required
@require_admin_or_staff
def mark_feedback_read(fid):
    fb = Feedback.query.get_or_404(fid)
    fb.is_read = True
    db.session.commit()
    return jsonify({'success': True})


def _notify_customer(booking, title, body, priority=False, status=None, collapse_minutes=None, cta_label=None):
    """The one place a booking-change notification is produced. Writes the
    bell notification and — unless this is a walk-in with no account —
    emails the exact same title and body, so the two channels can never say
    something different. Every message here already carries the booking's
    reference and, for anything that changes an existing commitment, what
    stayed the same and the way out — built by the caller, not here, since
    the right words depend on what actually happened. Returns whether an
    email went out."""
    send_notification(booking.user_id, title, body, type='booking',
                       status=status or booking.status, booking_id=booking.id, priority=priority,
                       collapse_minutes=collapse_minutes)
    if booking.walkin_customer_id:
        return False
    customer = User.query.get(booking.user_id)
    if not (customer and customer.email):
        return False
    cta = (f'<p><a href="{BASE_URL}/customer/dashboard" style="display:inline-block;padding:10px 18px;background:#d00019;'
           f'color:#fff;border-radius:6px;text-decoration:none;font-weight:bold;">{cta_label}</a></p>') if cta_label else ''
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;">
      <p>Hi {customer.fullname},</p>
      <p>{body}</p>
      {cta}
      <p>— MotoTyres North Caloocan</p>
    </div>"""
    _send_gmail(customer.email, f'{title} — MotoTyres', html)
    return True


def _mechanic_swap_notification(booking, new_name):
    """The 'mechanic changed' notification — fires ONLY when a stated
    preference is broken; an empty preference means the shop can move the
    job freely, and there's nothing to tell the customer. Leads with naming
    who they asked for and that they're unavailable, then prominently states
    the time has NOT changed, who's handling it now, and how to hold out for
    the original mechanic instead. Always priority when it does fire — a
    mechanic swap is something the customer can push back on. Shared by a
    plain reassignment and the clash fix that hands the job to someone else,
    so the wording never drifts between the two paths."""
    preferred = booking.preferred_mechanic_name
    if not preferred:
        return False

    ref = f'BKG-{booking.id:03d}'
    date_str = booking.date.strftime('%b %d, %Y')
    time_str = booking.time.strftime('%I:%M %p')
    origin = mechanic_origin_note(new_name, preferred)

    if new_name:
        title = f'{preferred} is not available — same time, different mechanic'
        who_now = f'{new_name} is handling it instead ({origin}).'
    else:
        title = f'{preferred} is not available — same time, no mechanic yet'
        who_now = 'Nobody specific is assigned to it yet.'

    body = (f'You asked for {preferred}, but {preferred} is not available for your booking. '
            f'Your appointment time has NOT changed — {date_str} at {time_str} still stands. '
            f'{who_now} Services: {booking.service} ({ref}). '
            f"If you'd rather wait for {preferred}, reply and we'll find a day they're free.")
    # Sorting out the same booking twice in a few minutes reads as one
    # update, not one push per edit — collapse into the latest state.
    return _notify_customer(booking, title, body, priority=True, collapse_minutes=5)


def _mechanic_notice_email(customer_name, booking, mech_name, spec, kind):
    first = (customer_name or 'there').split(' ')[0]
    start_min = booking.time.hour * 60 + booking.time.minute
    end_min = real_end_minutes(start_min, booking.duration_minutes or DEFAULT_DURATION_MIN, booking.overrun_minutes or 0)
    when = f"{booking.date.strftime('%a, %b %d, %Y')}, {minutes_to_ampm(start_min)} – {minutes_to_ampm(end_min)}"
    rows = ''.join(
        f'<tr><td style="padding:9px 0;border-bottom:1px solid #eee;color:#555;font-size:14px;">{k}</td>'
        f'<td style="padding:9px 0;border-bottom:1px solid #eee;text-align:right;font-weight:bold;font-size:14px;">{v}</td></tr>'
        for k, v in [('Booking', f'BKG-{booking.id:03d}'), ('Service', booking.service), ('Date & time', when),
                     ('Mechanic', mech_name), ('Motorcycle', booking.motorcycle_model or '—')])
    headline = 'Your mechanic has been assigned' if kind == 'assigned' else 'Your mechanic has changed'
    intro = ('the shop assigned a mechanic to your booking:' if kind == 'assigned'
             else 'the shop changed the mechanic on your booking:')
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;background:#fff;border:1px solid #e5e5e5;border-radius:10px;overflow:hidden;">
      <div style="background:#111;padding:16px 22px;font-size:22px;font-weight:900;letter-spacing:1px;color:#fff;">MOTO <span style="color:#d00019;">TYRE</span></div>
      <div style="padding:22px;">
        <h2 style="margin:0 0 12px;font-size:20px;">{headline}</h2>
        <p style="margin:0 0 16px;font-size:14px;">Hi {first}, {intro}</p>
        <div style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:10px;padding:14px;margin-bottom:16px;">
          <table style="border-collapse:collapse;"><tr>
            <td style="width:44px;"><div style="width:42px;height:42px;border-radius:50%;background:#15803d;color:#fff;text-align:center;line-height:42px;font-weight:bold;">{_mechanic_initials(mech_name)}</div></td>
            <td style="padding-left:12px;"><div style="font-size:17px;font-weight:bold;">{mech_name}</div><div style="font-size:13px;color:#166534;">{spec or ''}</div></td>
          </tr></table>
        </div>
        <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">{rows}</table>
        <p style="font-size:14px;">Your appointment time stays the same. See you at the shop!</p>
        <p><a href="{BASE_URL}/customer/dashboard?open_booking={booking.id}" style="display:inline-block;padding:11px 20px;background:#d00019;color:#fff;border-radius:6px;text-decoration:none;font-weight:bold;">View My Booking</a></p>
      </div>
      <div style="background:#f6f6f6;padding:12px 22px;font-size:12px;color:#888;">MotoTyres North Caloocan · This is an automated email.</div>
    </div>"""


def _send_mechanic_notice(booking, mech_name, spec, kind):
    """Portal notification + email naming the mechanic, for the first
    assignment ('assigned') and any later swap ('changed'). Walk-ins have no
    account. Remembers what went out so the Day Schedule card can show it."""
    if booking.walkin_customer_id:
        return False
    ref = f'BKG-{booking.id:03d}'
    start_min = booking.time.hour * 60 + booking.time.minute
    end_min = real_end_minutes(start_min, booking.duration_minutes or DEFAULT_DURATION_MIN, booking.overrun_minutes or 0)
    when = f"{booking.date.strftime('%a, %b %d, %Y')}, {minutes_to_ampm(start_min)} – {minutes_to_ampm(end_min)}"
    spec_part = f' ({spec})' if spec else ''
    if kind == 'assigned':
        title = f'Your mechanic for {ref} is {mech_name}'
        body = f'{mech_name}{spec_part} will handle your {booking.service} on {when}. Your appointment time stays the same.'
        status, subject = 'mechanic_assigned', f'Your mechanic for {ref}: {mech_name}'
    else:
        title = f'Your mechanic for {ref} changed to {mech_name}'
        body = (f'The shop changed your mechanic — {mech_name}{spec_part} will now handle your {booking.service} on {when}. '
                f'Your appointment time stays the same.')
        status, subject = 'mechanic_changed', f'Your mechanic for {ref} changed: {mech_name}'
    send_notification(booking.user_id, title, body, type='booking', status=status, booking_id=booking.id, priority=(kind == 'changed'))
    customer = User.query.get(booking.user_id)
    emailed = False
    if customer and customer.email:
        _send_gmail(customer.email, subject, _mechanic_notice_email(customer.fullname, booking, mech_name, spec, kind))
        emailed = True
    booking.assign_notice_at = ph_now()
    booking.assign_notice_kind = kind
    booking.assign_notice_emailed = emailed
    db.session.commit()
    return emailed


def _mechanic_assigned_notification(booking, new_name):
    """A mechanic was just assigned to a booking that had no preference on
    file — ordinarily silent (see assign_booking_mechanic's docstring), but
    the Quotation page's 'Assign & Notify Customer' button opts into telling
    the customer anyway: they're about to have someone looking at their bike
    and should know who."""
    ref = f'BKG-{booking.id:03d}'
    spec = booking.assigned_mechanic_specialization or 'General Service'
    title = f'{new_name} has been assigned to your booking'
    body = (f'{new_name} ({spec}) has been assigned to your {booking.service} ({ref}) '
            f'and will begin the inspection shortly.')
    return _notify_customer(booking, title, body, priority=False)


@admin_app.route('/booking/<int:bid>/start-eligibility')
@login_required
@require_admin_or_staff
def booking_start_eligibility(bid):
    """Lets the batch details modal ask, per service, whether Start is open,
    early-startable, or blocked — same rules the status endpoint enforces."""
    booking = Booking.query.get_or_404(bid)
    now = ph_now()
    return jsonify({**start_eligibility(booking, now), 'now': now.strftime('%I:%M %p'),
                    'scheduled_time': booking.time.strftime('%I:%M %p')})


# ═══════════════ MECHANIC STATUS: Available / Reserved / Busy / Off Duty ═══════════════
STARTED_STATUSES = ('inspection', 'in_progress', 'inprogress')
STARTED_LABELS = {'inspection': 'Inspection & Estimation', 'in_progress': 'In Progress', 'inprogress': 'In Progress'}
MECH_LABELS = {'available': 'Available', 'reserved': 'Reserved', 'busy': 'Busy', 'off_duty': 'Off Duty'}
_NO_JOBS = {'started': [], 'reserved': []}


def _mechanic_jobs_map():
    """{mechanic name: {'started': [bookings], 'reserved': [bookings]}} for every live booking. A booking that was
    started (Inspection & Estimation / In Progress) makes the mechanic Busy; a Confirmed one that hasn't started makes
    them Reserved. Completed and cancelled bookings free them."""
    jobs = {}
    rows = Booking.query.filter(Booking.assigned_mechanic_name.isnot(None),
                                Booking.status.in_(('confirmed',) + STARTED_STATUSES)).order_by(Booking.date, Booking.time).all()
    for b in rows:
        d = jobs.setdefault(b.assigned_mechanic_name, {'started': [], 'reserved': []})
        (d['started'] if b.status in STARTED_STATUSES else d['reserved']).append(b)
    return jobs


def _mechanic_effective(m, jobs_map=None):
    """Which status shows, in this order: a started booking = Busy, Off Duty, a waiting Confirmed booking = Reserved,
    and with no booking at all, whatever the admin chose in Edit (Available or Busy). A real booking beats anything manual."""
    j = (jobs_map if jobs_map is not None else _mechanic_jobs_map()).get(m.name) or _NO_JOBS
    if j['started']:
        return 'busy'
    if m.status == 'off_duty':
        return 'off_duty'
    if j['reserved']:
        return 'reserved'
    return 'busy' if m.status == 'busy' else 'available'


def mechanic_status_map():
    jobs = _mechanic_jobs_map()
    return {m.name: _mechanic_effective(m, jobs) for m in Mechanic.query.all()}


def _mech_now_text(name):
    """'Kiko Egonia is now Busy' style status after an action (None when nobody is assigned)."""
    if not name:
        return None
    m = Mechanic.query.filter_by(name=name).first()
    return MECH_LABELS[_mechanic_effective(m)] if m else None


def _prepare_mechanic_for_start(booking, chosen_name):
    """Checks the mechanic who will do a job the admin is about to Start. Returns (mechanic, error)."""
    chosen = (chosen_name or '').strip()
    name = chosen or booking.assigned_mechanic_name
    m = Mechanic.query.filter_by(name=name).first() if name else None
    if not m:
        return None, 'Choose the mechanic who will do this job.'
    j = _mechanic_jobs_map().get(m.name) or _NO_JOBS
    other = [x for x in j['started'] if x.id != booking.id]
    if other:
        return None, (f"{m.name} is still working on booking #{other[0].id}. "
                      f"Choose another mechanic, or finish #{other[0].id} first.")
    if m.status == 'off_duty':
        return None, f'{m.name} is Off Duty. Choose another mechanic.'
    if chosen and chosen != booking.assigned_mechanic_name and _mechanic_effective(m) == 'busy':
        return None, f'{m.name} is Busy. Choose another mechanic.'
    return m, None


@admin_app.route('/api/booking/<int:bid>/start-info')
@login_required
@require_admin_or_staff
def api_booking_start_info(bid):
    """What the Start Service pop-up needs: the booking's mechanic and status, a note, and who can be chosen."""
    b = Booking.query.get_or_404(bid)
    jobs = _mechanic_jobs_map()
    mechs = Mechanic.query.order_by(Mechanic.name).all()
    status_of = {m.name: _mechanic_effective(m, jobs) for m in mechs}
    cur = b.assigned_mechanic_name
    note, need_pick = None, False
    if not cur:
        need_pick = True
    else:
        m = next((x for x in mechs if x.name == cur), None)
        j = jobs.get(cur) or _NO_JOBS
        other = [x for x in j['started'] if x.id != b.id]
        if other:
            need_pick = True
            note = f"{cur} is still working on booking #{other[0].id}. Choose another mechanic, or finish #{other[0].id} first."
        elif m and m.status == 'off_duty':
            need_pick = True
            note = f'{cur} is Off Duty. Choose another mechanic.'
        else:
            note = f'{cur} will change from {MECH_LABELS.get(status_of.get(cur), "Reserved")} to Busy until this job is completed or cancelled.'
    options = [{'name': m.name, 'specialization': m.specialization, 'status': status_of[m.name], 'label': MECH_LABELS[status_of[m.name]],
                'disabled': status_of[m.name] in ('busy', 'off_duty') and m.name != cur} for m in mechs]
    return jsonify({'mechanic': cur, 'status': status_of.get(cur), 'note': note, 'need_pick': need_pick, 'options': options})


@admin_app.route('/booking/<int:bid>/status', methods=['POST'])
@login_required
@require_admin_or_staff
def update_booking_status(bid):
    booking    = Booking.query.get_or_404(bid)
    new_status = validate_booking_status(clean_str(request.form.get('status', ''), max_len=20))
    if new_status == 'completed':
        flash('Bookings can only be completed via the Billing page.', 'danger')
        return redirect(url_for('admin_dashboard'))
    is_start = new_status in ('inspection', 'in_progress', 'inprogress')
    if is_start and booking.status == 'confirmed':
        elig = start_eligibility(booking, ph_now())
        msg = None
        if elig['state'] == 'too_early':
            msg = (f'This booking is scheduled for {booking.date.strftime("%b %d, %Y")} at {booking.time.strftime("%I:%M %p")} — '
                   f'it can start from {elig["opens_at"]}.')
        elif elig['state'] == 'early_busy':
            msg = elig['message']
        if msg:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': msg}), 400
            flash(msg, 'danger')
            return redirect(url_for('admin_dashboard'))
    if new_status in ('in_progress', 'inprogress'):
        waiting = Quotation.query.filter_by(booking_id=booking.id, status='awaiting_customer').first()
        if waiting:
            msg = "A quotation is waiting for the customer's answer — work can start once they reply."
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': msg}), 400
            flash(msg, 'danger')
            return redirect(url_for('admin_dashboard'))
    if is_start and booking.status == 'cancelled':
        # The customer cancelled while the shop's page still showed Start.
        msg = 'This booking was cancelled, so it can no longer be started. Refresh to see its latest status.'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': msg}), 409
        flash(msg, 'danger')
        return redirect(url_for('admin_dashboard'))
    start_mechanic = None
    if is_start and booking.status == 'confirmed':
        start_mechanic, mech_err = _prepare_mechanic_for_start(booking, request.form.get('mechanic_name'))
        if mech_err:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': mech_err, 'needs_mechanic': True}), 400
            flash(mech_err, 'danger')
            return redirect(url_for('admin_dashboard'))
    was_status = booking.status
    if is_start and booking.status == 'confirmed':
        from sqlalchemy import text as _sql_text
        claimed = db.session.execute(
            _sql_text("UPDATE booking SET status = :s WHERE id = :id AND status = 'confirmed'"),
            {'s': new_status, 'id': booking.id}).rowcount
        db.session.commit()
        db.session.refresh(booking)
        if not claimed:
            msg = f'This booking is now {booking.status.replace("_", " ")} — it can no longer be started. Refresh to see its latest status.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': msg}), 409
            flash(msg, 'danger')
            return redirect(url_for('admin_dashboard'))
    if start_mechanic is not None:
        if booking.assigned_mechanic_name != start_mechanic.name:
            booking.assigned_mechanic_name = start_mechanic.name
            booking.assigned_mechanic_specialization = start_mechanic.specialization
        # old walk-in notes must not come back once this job is done
        start_mechanic.manual_customer = None
        start_mechanic.manual_service = None
        start_mechanic.status = 'available'
    if is_start and not booking.actual_start_at:
        booking.actual_start_at = ph_now()
    if new_status == 'inspection' and not booking.inspection_at:
        booking.inspection_at = ph_now()
    if new_status in ('in_progress', 'inprogress'):
        booking.in_progress_at = ph_now()
        if not booking.inspection_at:
            booking.inspection_at = booking.in_progress_at   # skipped straight to work
    booking.status = new_status
    db.session.commit()

    ref = f'BKG-{booking.id:03d}'
    date_str = booking.date.strftime('%b %d, %Y')
    date_short = booking.date.strftime('%b %d')
    time_str = booking.time.strftime('%I:%M %p')
    messages = {
        'confirmed': (
            f'Confirmed — {date_short} at {time_str}',
            f'Your {booking.service} appointment ({ref}) on {date_str} at {time_str} is confirmed. Please arrive 15 minutes early.',
            False,
        ),
        'inspection': (
            f'{booking.service} — inspection & estimation',
            f'We started the inspection on your {booking.service} ({ref}). We will let you know if any extra work is needed.',
            False,
        ),
        'inprogress': (
            f'{booking.service} is now in progress',
            f'We started work on your {booking.service} ({ref}).',
            False,
        ),
        'in_progress': (
            f'{booking.service} is now in progress',
            f'We started work on your {booking.service} ({ref}).',
            False,
        ),
        'awaiting_payment': (
            f'{booking.service} is done — pay at the counter',
            f'Your {booking.service} ({ref}) is done. Please pay at the counter.',
            False,
        ),
        'completed': (
            f'{booking.service} complete — thanks for coming in',
            f'Your {booking.service} ({ref}) is complete. Thanks for coming in.',
            False,
        ),
        'cancelled': (
            f'Cancelled — {date_short} at {time_str}',
            f"Your {booking.service} appointment ({ref}) on {date_str} at {time_str} was cancelled by the shop. "
            f"If this was a mistake or you'd like to rebook, just get in touch.",
            True,
        ),
    }
    if new_status in messages:
        title, body, priority = messages[new_status]
        _notify_customer(booking, title, body, priority=priority, status=new_status.replace('_', ''))
    toast = f'Booking {booking.id} updated'
    _mname = booking.assigned_mechanic_name
    _mnow = _mech_now_text(_mname)
    if new_status == 'cancelled':
        toast = f'Booking {booking.id} cancelled' + (f' · {_mname} is now {_mnow}' if _mnow else '')
    elif new_status in ('in_progress', 'inprogress') and was_status in STARTED_STATUSES:
        toast = f'Booking {booking.id} is In Progress' + (f' · {_mname} stays Busy' if _mname else '')
    elif is_start:
        label = 'In Progress' if new_status in ('in_progress', 'inprogress') else 'started'
        toast = f'Booking {booking.id} ' + ('is ' if label == 'In Progress' else '') + label + (f' · {_mname} is now Busy' if _mname else '')
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'new_status': new_status, 'message': 'Booking status updated!', 'toast': toast,
                        'mechanic_statuses': mechanic_status_map()})
    flash('Booking status updated!', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/booking/<int:bid>/complete-invoice', methods=['POST'])
@login_required
@require_admin_or_staff
def complete_booking_invoice(bid):
    """The Outbox's 'Mark as Completed & Send Invoice' button — closes out a
    booking that went through Inspection & Estimation, billing exactly what
    the customer agreed to (the full quotation if they approved, just the
    booked service if they declined), and sends the invoice. Payment itself
    is still collected separately afterward, same as every other booking —
    this only marks the WORK done and tells the customer what they owe."""
    booking = Booking.query.get_or_404(bid)
    if booking.status == 'completed':
        return jsonify({'success': False, 'error': 'This booking is already completed.'}), 400

    q = Quotation.query.filter_by(booking_id=bid).order_by(Quotation.created_at.desc()).first()
    if q:
        breakdown = _quotation_breakdown(q.items)
        # A declined back-job quotation bills nothing but the (free or not-covered) redo line.
        total = (breakdown['approve_total'] if q.status == 'approved'
                 else (breakdown['booked'] if booking.service.startswith(BACKJOB_PREFIX) else breakdown['decline_total']))
    else:
        total = booking_service_price(booking.service)

    booking.status = 'completed'
    booking.completed_at = ph_now()
    booking.total_amount = total
    booking.invoice_paid = False
    booking.invoice_payment_method = None
    if not booking.in_progress_at:
        booking.in_progress_at = booking.completed_at

    # A back-job booking: the job itself is free. With nothing extra to pay
    # there's no invoice at all — the claim just closes — and its claim (if
    # still open) is closed here so the two never drift apart.
    linked_claim = (ReturnRequest.query.filter_by(redo_booking_id=booking.id).first()
                    if booking.service.startswith(BACKJOB_PREFIX) else None)
    if linked_claim and linked_claim.status == 'approved':
        linked_claim.status = 'resolved'
        linked_claim.resolved_at = ph_now()
        if total <= 0:
            booking.invoice_paid = True
            db.session.commit()
            done = _backjob_done_texts(linked_claim, 0.0)
            _notify_return_customer(linked_claim, done['title'], done['body'], priority=False,
                                    email_body=done['email_body'], email_subject=done['email_subject'], first_name_only=True)
            return jsonify({'success': True, 'ref': f'BKG-{booking.id:03d}', 'total': 0,
                            'customer_first_name': done['first_name']})
    elif total <= 0 and booking.service.startswith(BACKJOB_PREFIX):
        booking.invoice_paid = True
        db.session.commit()
        return jsonify({'success': True, 'ref': f'BKG-{booking.id:03d}', 'total': 0, 'customer_first_name': 'the customer'})
    # No quotation = the old on-the-spot path Billing doesn't invoice. Give it
    # a plain approved one (just the booked service) so the invoice flow, the
    # payment picker and Billing History all treat it like every other invoice.
    if not q and total > 0 and not booking.walkin_customer_id:
        customer_row = User.query.get(booking.user_id)
        q = Quotation(customer_name=(customer_row.fullname if customer_row else 'Customer'),
                      customer_phone=(customer_row.phone if customer_row and customer_row.phone else ''),
                      motorcycle_model=booking.motorcycle_model or '', total_amount=total, status='approved',
                      booking_id=booking.id, inspected_by=booking.assigned_mechanic_name, created_by=current_user.id,
                      decided_at=ph_now())
        db.session.add(q)
        db.session.flush()
        db.session.add(QuotationItem(quotation_id=q.id, item_type='service', name=booking.service, quantity=1,
                                      unit_price=total, locked=True))
    db.session.commit()

    ref = f'BKG-{booking.id:03d}'
    inv_ref = f'INV-{booking.id:03d}'
    title = f'Service Completed! 🎉 Invoice {inv_ref}'
    message = f'Your {booking.service} ({ref}) is done. Please choose Cash or GCash to pay ₱{total:,.2f}.'
    send_notification(booking.user_id, title, message, type='invoice', status='completed',
                       booking_id=booking.id, priority=True)

    customer = User.query.get(booking.user_id)
    customer_first_name = (booking.contact_name if booking.walkin_customer_id
                            else (customer.fullname if customer else 'the customer')).split(' ')[0]

    if not booking.walkin_customer_id and customer and customer.email:
        items_rows = ''
        if q and q.items:
            # On decline, the booked service and any materials already used
            # still bill — only the extra inspection findings drop off.
            relevant = (q.items if q.status == 'approved'
                        else [i for i in q.items if i.locked or (i.item_type == 'product' and not booking.service.startswith(BACKJOB_PREFIX))])
            items_rows = ''.join(
                f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;">{i.name}</td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;">₱{i.unit_price:,.2f}</td></tr>'
                for i in relevant
            )
        else:
            items_rows = (f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;">{booking.service}</td>'
                           f'<td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;">₱{total:,.2f}</td></tr>')
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;">
          <p>Hi {customer.fullname},</p>
          <p>Your {booking.service} ({ref}) is done. Here's your invoice {inv_ref}:</p>
          <table style="width:100%;border-collapse:collapse;margin:12px 0;">{items_rows}</table>
          <p style="font-size:1.1em;"><strong>Amount to pay: ₱{total:,.2f}</strong></p>
          <p><a href="{BASE_URL}/customer/dashboard?open_booking={booking.id}"
                style="display:inline-block;padding:10px 18px;background:#d00019;color:#fff;
                       border-radius:6px;text-decoration:none;font-weight:bold;">Pay Invoice</a></p>
          <p>You can pay by cash at the shop counter, or by GCash online.</p>
          <p>— MotoTyres North Caloocan</p>
        </div>"""
        _send_gmail(customer.email, f'{title} — MotoTyres', html)

    _mname = booking.assigned_mechanic_name
    _mnow = _mech_now_text(_mname)
    return jsonify({'success': True, 'ref': ref, 'total': total, 'customer_first_name': customer_first_name,
                    'toast': f'Booking {booking.id} completed' + (f' · {_mname} is now {_mnow}' if _mnow else ''),
                    'mechanic_statuses': mechanic_status_map()})


@admin_app.route('/booking/<int:bid>/invoice/record-cash', methods=['POST'])
@login_required
@require_admin_or_staff
def record_invoice_cash(bid):
    """The counter-side half of a cash invoice — the customer picked 'pay in
    cash' from their own notification (or is paying in person), but that's
    just a stated intent; the invoice only actually closes once the cashier
    has counted the money and records it here. Used by both the Billing
    Center's full Record Cash modal (counts change) and the Outbox's quick
    confirm (passes the exact total, no change)."""
    booking = Booking.query.get_or_404(bid)
    if booking.status != 'completed':
        return jsonify({'success': False, 'error': 'This booking has not been completed yet.'}), 400
    if booking.invoice_paid:
        return jsonify({'success': False, 'error': 'This invoice is already paid.'}), 400
    if booking.invoice_payment_method != 'cash':
        return jsonify({'success': False, 'error': 'The customer has not chosen cash for this invoice.'}), 400
    try:
        received = float(request.form.get('cash_received', booking.total_amount))
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid amount.'}), 400
    if received < booking.total_amount:
        return jsonify({'success': False, 'error': 'Cash received is less than the amount due.'}), 400

    booking.invoice_paid = True
    booking.invoice_paid_at = ph_now()
    booking.payment_method = 'cash'
    booking.invoice_cash_received = received
    booking.invoice_change = round(received - booking.total_amount, 2)
    booking.invoice_received_by = current_user.fullname
    booking.invoice_receipt_no = f'OR-{booking.id:06d}'
    # the invoice card stops being pinned to the top once it's paid
    Notification.query.filter_by(booking_id=booking.id, type='invoice', priority=True).update({'priority': False})
    db.session.commit()

    ref = f'BKG-{booking.id:03d}'
    send_notification(
        booking.user_id, 'Payment received ✅',
        f'You paid ₱{booking.total_amount:,.2f} by cash for {booking.service} ({ref}). Thank you for choosing MotoTyres!',
        type='invoice', status='paid', booking_id=booking.id,
    )
    customer = User.query.get(booking.user_id)
    customer_first_name = (booking.contact_name if booking.walkin_customer_id
                            else (customer.fullname if customer else 'the customer')).split(' ')[0]
    if not booking.walkin_customer_id and customer and customer.email:
        _q = Quotation.query.filter_by(booking_id=booking.id).order_by(Quotation.created_at.desc()).first()
        if _q:
            _rel = _q.items if _q.status == 'approved' else [i for i in _q.items if i.locked or (i.item_type == 'product' and not booking.service.startswith(BACKJOB_PREFIX))]
            _items = [(i.name, i.unit_price) for i in _rel]
        else:
            _items = [(booking.service, booking.total_amount)]
        _subj, _html = booking_receipt_email(customer.fullname, f'INV-{booking.id:03d}', ref, booking.service, _items,
                                             booking.total_amount, 'cash', booking.invoice_paid_at, booking.invoice_receipt_no)
        _send_gmail(customer.email, _subj, _html)

    return jsonify({'success': True, 'ref': ref, 'receipt_no': booking.invoice_receipt_no,
                     'customer_first_name': customer_first_name, 'change': booking.invoice_change})


@admin_app.route('/api/booking/<int:bid>/invoice-data')
@login_required
@require_admin_or_staff
def api_booking_invoice_data(bid):
    """Single source of truth for a booking's invoice/receipt — feeds the
    Billing Center's Record Cash modal (pre-payment) and Receipt modal
    (post-payment) alike, so they can never show different numbers."""
    booking = Booking.query.get_or_404(bid)
    q = Quotation.query.filter_by(booking_id=bid).order_by(Quotation.created_at.desc()).first()
    if q:
        # On decline, the booked service and any materials already used
        # still bill — only the extra inspection findings drop off.
        relevant = q.items if q.status == 'approved' else [i for i in q.items if i.locked or i.item_type == 'product']
        items = [{'name': i.name, 'price': i.unit_price} for i in relevant]
        # inspected_by is the mechanic as of when the quotation was sent;
        # fall back to the booking's current assignment for older quotations
        # that predate that field, or any other case it ended up blank —
        # the receipt should never show no mechanic when the booking has one.
        mechanic = q.inspected_by or booking.assigned_mechanic_name
    else:
        items = [{'name': booking.service, 'price': booking.total_amount}]
        mechanic = booking.assigned_mechanic_name
    customer = None if booking.walkin_customer_id else User.query.get(booking.user_id)
    customer_name = booking.contact_name if booking.walkin_customer_id else (customer.fullname if customer else 'Unknown customer')
    customer_phone = booking.contact_mobile if booking.walkin_customer_id else (customer.phone if customer else '')
    # Old-style, pre-invoice-flow completions were billed and paid in one
    # step on this same page — they have no invoice_payment_method on file,
    # but booking.payment_method still says how they actually paid.
    is_paid = booking.status == 'completed' and (booking.invoice_paid or not booking.invoice_payment_method)
    payment_method_used = booking.invoice_payment_method or (booking.payment_method if is_paid else None)
    paid_at = booking.invoice_paid_at or (booking.completed_at if is_paid else None)
    return jsonify({
        'booking_id': booking.id, 'ref': f'BKG-{booking.id:03d}', 'inv_ref': f'INV-{booking.id:03d}',
        'receipt_no': booking.invoice_receipt_no or (f'OR-{booking.id:06d}' if is_paid else None),
        'service': booking.service,
        'customer_name': customer_name, 'customer_phone': customer_phone or '',
        'motorcycle': booking.motorcycle_model or '', 'mechanic': mechanic or '',
        'items': items, 'total': booking.total_amount,
        'paid': is_paid, 'payment_method': payment_method_used,
        'cash_received': booking.invoice_cash_received, 'change': booking.invoice_change,
        'gcash_ref': booking.invoice_gcash_ref,
        'paid_at': paid_at.strftime('%b %d, %Y %I:%M %p') if paid_at else None,
        'completed_at': booking.completed_at.strftime('%b %d, %Y %I:%M %p') if booking.completed_at else None,
    })


@admin_app.route('/booking/<int:bid>/assign-mechanic', methods=['POST'])
@login_required
@require_admin_or_staff
def assign_booking_mechanic(bid):
    """Set or change who's actually doing a booking. Goes through the same
    shared routine everything else does — a mechanic can't be assigned here
    unless they're on today's on-duty roster and free for the whole window
    (turnover buffer included); an admin picking someone can't create the
    exact clash the customer-side flow would have refused. preferred_mechanic_*
    is never touched here — it's the customer's original ask, permanent.

    Whether the customer gets told is decided by the GAP between the two:
      - no preference on file            -> the shop moves the job freely, silently.
      - assigned matches the preference  -> the customer got what they asked
                                             for; nothing to say.
      - assigned differs from a stated
        preference                       -> a promise was broken; notify + email.
    That's evaluated fresh against the preference every time, not against
    whoever was assigned a moment ago — so it fires on EVERY change that
    leaves the booking not matching what was promised, not just the first."""
    booking = Booking.query.get_or_404(bid)
    mechanic_id = clean_str(request.form.get('mechanic_id', ''), max_len=10)

    def fail(msg):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': msg}), 400
        flash(msg, 'danger')
        return redirect(url_for('admin_dashboard'))

    old_name = booking.assigned_mechanic_name
    if mechanic_id:
        mechanic = Mechanic.query.get(int(mechanic_id)) if mechanic_id.isdigit() else None
        if not mechanic:
            return fail('Mechanic not found.')
        if mechanic.status == 'off_duty':
            return fail(f'{mechanic.name} is Off Duty and can\'t be assigned.')
        start_min = booking.time.hour * 60 + booking.time.minute
        duration  = booking.duration_minutes or DEFAULT_DURATION_MIN
        # The booking's own date/time isn't changing, so the shop-queue slot
        # and the fixed grid were already validated when it was made — only
        # the mechanic dimension is new information here.
        ok, error = _check_admin_booking_request(
            booking.date, start_min, duration, exclude_id=booking.id,
            mechanic_name=mechanic.name, require_slot_grid=False, check_shop_queue=False,
        )
        if not ok:
            return fail(error)
        new_name, new_spec = mechanic.name, mechanic.specialization
    else:
        new_name, new_spec = None, None

    booking.assigned_mechanic_name = new_name
    booking.assigned_mechanic_specialization = new_spec
    db.session.commit()

    # Opt-in only: a caller (the Quotation page's "Assign & Notify Customer"
    # button, specifically) can ask to be told even for the ordinarily-silent
    # no-preference case — everywhere else (e.g. a routine reassignment from
    # Manage Bookings) keeps the original quiet default.
    notify_first_assign = clean_str(request.form.get('notify_first_assign', ''), max_len=5) == '1'

    changed      = new_name != old_name
    had_promise  = bool(booking.preferred_mechanic_name)
    broke_promise = had_promise and new_name != booking.preferred_mechanic_name
    if changed and new_name and not old_name:
        _send_mechanic_notice(booking, new_name, new_spec, 'assigned')     # the shop's first pick
    elif changed and new_name and broke_promise:
        _mechanic_swap_notification(booking, new_name)                      # a stated preference was not honoured
        booking.assign_notice_at, booking.assign_notice_kind, booking.assign_notice_emailed = ph_now(), 'changed', True
        db.session.commit()
    elif changed and new_name:
        _send_mechanic_notice(booking, new_name, new_spec, 'changed')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'mechanic_name': new_name, 'mechanic_specialization': new_spec})
    flash('Mechanic updated.', 'success')
    return redirect(url_for('admin_dashboard'))


def get_capacity_row(query_date):
    return DailyCapacity.query.filter_by(date=query_date).first()


def get_on_duty_mechanics(query_date=None):
    """Who's actually working a given day: the first N by roster order (N
    from that date's capacity override, or the whole roster if none is set),
    further cut down to whoever isn't individually marked off duty in their
    own profile — that override always applies, however high N is set. This
    is THE roster every scheduling check in both apps reads."""
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


def _gather_intervals(query):
    """[(start_min, end_min)] for every Booking row a query returns."""
    result = []
    for b in query.all():
        b_start = b.time.hour * 60 + b.time.minute
        result.append((b_start, compute_finish_minutes(b_start, b.duration_minutes or DEFAULT_DURATION_MIN)))
    return result


def _walkin_windows(the_date, exclude_jo_id=None):
    """Walk-in jobs tying up a named mechanic on this date — still open
    (pending / in progress) and not marked done — as minute-of-day windows."""
    day_start = datetime.combine(the_date, datetime.min.time())
    q = JobOrder.query.filter(
        JobOrder.mechanic_name.isnot(None), JobOrder.start_at.isnot(None),
        JobOrder.start_at >= day_start, JobOrder.start_at < day_start + timedelta(days=1),
        JobOrder.mechanic_done_at.is_(None), JobOrder.status.in_(['pending', 'in_progress']))
    if exclude_jo_id:
        q = q.filter(JobOrder.id != exclude_jo_id)
    out = []
    for jo in q.all():
        start = jo.start_at.hour * 60 + jo.start_at.minute
        out.append({'mechanic': jo.mechanic_name, 'start': start, 'end': start + (jo.est_minutes or 30),
                    'ref': f'JO-{jo.id:03d}', 'jo_id': jo.id, 'customer': jo.customer_name})
    return out


def _gather_blocked_intervals(booking_date):
    """Admin-blocked start times for one day, as full-hour windows — folded
    into the shop's queue intervals so a blocked slot reads exactly like an
    already-booked one and an admin action can't be forced through it either.
    Walk-in jobs holding a mechanic count here too, so a slot's free-mechanic
    number and "Let the Shop Assign" both see them."""
    return [
        (t.hour * 60 + t.minute, t.hour * 60 + t.minute + SLOT_GRANULARITY_MIN)
        for t in (bs.time for bs in BlockedSlot.query.filter_by(date=booking_date).all())
    ] + [(w['start'], w['end']) for w in _walkin_windows(booking_date)]


def _check_admin_booking_request(booking_date, start_minutes, duration_minutes, exclude_id=None,
                                  mechanic_name=None, require_slot_grid=True, check_shop_queue=True):
    """THE call every admin-side action that touches a booking's schedule makes
    before committing — a reschedule (date/time change) and a mechanic
    reassignment (mechanic change) alike. This gathers what
    service_duration.validate_booking() needs and hands it the actual
    decision — the exact same routine the customer-side booking flow uses, so
    an admin action can never end up enforcing a looser rule than a customer
    would hit. The admin UI only pre-filters what it *offers*; this is what
    actually decides."""
    shop_intervals, daily_count = [], 0
    if check_shop_queue:
        q = Booking.query.filter(Booking.date == booking_date, Booking.status != 'cancelled')
        if exclude_id:
            q = q.filter(Booking.id != exclude_id)
        shop_intervals = _gather_intervals(q)
        daily_count = len(shop_intervals)
        shop_intervals += _gather_blocked_intervals(booking_date)

    # "On duty" is roster-count AND individually-available — not just the
    # profile toggle — so a mechanic dialed out by the capacity slider reads
    # exactly the same as one marked off duty on their own profile. Needed
    # for the shop-wide slot capacity below regardless of whether a specific
    # mechanic was named.
    _, _, on_duty = get_on_duty_mechanics(booking_date)

    mechanic_status = None
    mechanic_intervals = None
    if mechanic_name:
        mechanic_status = 'available' if any(m.name == mechanic_name for m in on_duty) else 'off duty'
        mq = Booking.query.filter(
            Booking.assigned_mechanic_name == mechanic_name,
            Booking.date == booking_date,
            Booking.status != 'cancelled',
        )
        if exclude_id:
            mq = mq.filter(Booking.id != exclude_id)
        mechanic_intervals = _gather_intervals(mq) + [
            (w['start'], w['end']) for w in _walkin_windows(booking_date) if w['mechanic'] == mechanic_name]

    return validate_booking(
        start_minutes, duration_minutes, shop_intervals, daily_count,
        require_slot_grid=require_slot_grid, daily_cap=get_daily_cap(booking_date), on_duty_count=len(on_duty),
        mechanic_name=mechanic_name, mechanic_status=mechanic_status, mechanic_intervals=mechanic_intervals,
    )


def _apply_reschedule(booking, new_date, new_time, notify=True):
    """Move a booking to a new date/time and, unless told not to, tell the
    customer. Shared by the direct Reschedule action and the clash-fix engine
    (fix #2, 'keep the mechanic, move the time') so both go through the exact
    same write and the exact same email — a fix is just a reschedule with a
    reason attached, not a separate code path."""
    duration  = booking.duration_minutes or DEFAULT_DURATION_MIN
    old_date_str = booking.date.strftime('%b %d, %Y')
    old_time_str = booking.time.strftime('%I:%M %p')

    booking.date      = new_date
    booking.time      = new_time
    booking.end_time  = compute_finish_time(new_time, duration)
    booking.overrun_minutes = 0
    booking.was_rescheduled = True
    db.session.commit()

    new_date_str = booking.date.strftime('%b %d, %Y')
    new_time_str = booking.time.strftime('%I:%M %p')
    emailed = False
    if notify:
        ref = f'BKG-{booking.id:03d}'
        title = f"Moved to {booking.date.strftime('%b %d')}, {new_time_str}"
        mechanic_line = booking.assigned_mechanic_name or 'shop decides'
        body = (f'Was: {old_date_str} at {old_time_str}. Now: {new_date_str} at {new_time_str}. '
                f'Mechanic: {mechanic_line} ({ref}). '
                f"If the new time does not work for you, reply to this email and we will find another.")
        emailed = _notify_customer(booking, title, body, priority=True)
    return {'date': new_date_str, 'time': new_time_str, 'emailed': emailed}


@admin_app.route('/booking/<int:bid>/reschedule-slots')
@login_required
@require_admin_or_staff
def booking_reschedule_slots(bid):
    """Only the start times this booking could really move to on a given day:
    open in the shop's queue (its own current slot doesn't count against it),
    not blocked, in the future, and — if it has a mechanic — free for them.
    Same rules the Reschedule button itself enforces when it's clicked."""
    booking = Booking.query.get_or_404(bid)
    try:
        the_date = datetime.strptime(request.args.get('date', '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400
    if the_date < ph_now().date():
        return jsonify({'slots': []})
    duration = MULTIDAY_INTAKE_MIN if booking.is_multiday else (booking.duration_minutes or DEFAULT_DURATION_MIN)
    q = Booking.query.filter(Booking.date == the_date, Booking.status != 'cancelled', Booking.id != booking.id)
    intervals = _gather_intervals(q) + _gather_blocked_intervals(the_date)
    now_minutes = None
    if the_date == ph_now().date():
        now = ph_now()
        now_minutes = now.hour * 60 + now.minute
    _, _, on_duty = get_on_duty_mechanics(the_date)
    out = []
    for sl in slot_statuses(duration, intervals, now_minutes, on_duty_count=len(on_duty)):
        if not sl['available']:
            continue
        if booking.assigned_mechanic_name:
            ok, _err = _check_admin_booking_request(the_date, sl['start'], duration, exclude_id=booking.id,
                                                    mechanic_name=booking.assigned_mechanic_name)
            if not ok:
                continue
        out.append({'time': minutes_to_hhmm(sl['start']), 'label': minutes_to_ampm(sl['start'])})
    return jsonify({'slots': out})


@admin_app.route('/booking/<int:bid>/reschedule', methods=['POST'])
@login_required
@require_admin_or_staff
def reschedule_booking(bid):
    """Move a booking to a new date/time. Goes through the exact same shared
    routine the customer-side booking flow uses — valid slot, fits before
    closing, no shop conflict, and if a mechanic is assigned, still free for
    the new window (turnover buffer included) — a reschedule can never quietly
    create a conflict that didn't exist before. The customer is told either
    way it can succeed: this route only ever produces a booking that's sound."""
    booking  = Booking.query.get_or_404(bid)
    if booking.status in ('inspection', 'in_progress', 'inprogress', 'completed'):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': "Reschedule isn't available once work has started."}), 400
        flash("Reschedule isn't available once work has started.", 'danger')
        return redirect(url_for('admin_dashboard'))
    date_str = clean_str(request.form.get('date', ''), max_len=10)
    time_str = clean_str(request.form.get('time', ''), max_len=5)

    def fail(msg):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': msg}), 400
        flash(msg, 'danger')
        return redirect(url_for('admin_dashboard'))

    try:
        new_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        new_time = datetime.strptime(time_str, '%H:%M').time()
    except ValueError:
        return fail('Invalid date or time.')

    if datetime.combine(new_date, new_time) <= ph_now():
        return fail('Cannot reschedule to a past or current time.')

    duration  = booking.duration_minutes or DEFAULT_DURATION_MIN
    start_min = new_time.hour * 60 + new_time.minute

    ok, error = _check_admin_booking_request(
        new_date, start_min, duration, exclude_id=booking.id,
        mechanic_name=booking.assigned_mechanic_name,
    )
    if not ok:
        return fail(error)

    result = _apply_reschedule(booking, new_date, new_time, notify=True)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'date': result['date'], 'time': result['time']})
    flash('Booking rescheduled.', 'success')
    return redirect(url_for('admin_dashboard'))


def _booking_customer_name(b):
    return b.contact_name if b.walkin_customer_id else (b.customer.fullname if b.customer else 'a customer')


# ── CLASHES ──────────────────────────────────────────────────────────────
# A clash is one mechanic double-booked, a booking with nobody assigned, or a
# booking sitting on someone no longer on duty. It is NOT the shop being over
# capacity — that's a capacity question (the Panel-3 bottleneck), never
# surfaced here.

def _mechanic_overlap_causes(bookings):
    """bookings: active Booking rows all assigned to the SAME mechanic. Returns
    {booking.id: cause dict} for every one that overlaps an earlier-starting
    booking on that mechanic — phrased around whichever earlier job is
    actually blocking it, 'running long' called out when the blocker itself
    currently carries a recorded overrun."""
    ordered = sorted(bookings, key=lambda b: (b.time, b.id))
    causes = {}
    for i in range(1, len(ordered)):
        cur = ordered[i]
        cur_start = cur.time.hour * 60 + cur.time.minute
        cur_end = real_end_minutes(cur_start, cur.duration_minutes or DEFAULT_DURATION_MIN, cur.overrun_minutes or 0)
        for j in range(i - 1, -1, -1):
            prev = ordered[j]
            prev_start = prev.time.hour * 60 + prev.time.minute
            prev_end = real_end_minutes(prev_start, prev.duration_minutes or DEFAULT_DURATION_MIN, prev.overrun_minutes or 0)
            if mechanic_overlaps(cur_start, cur_end, prev_start, prev_end):
                running_long = (prev.overrun_minutes or 0) > 0
                suffix = ' (running long)' if running_long else ''
                causes[cur.id] = {
                    'text': f"{cur.assigned_mechanic_name} is still on {_booking_customer_name(prev)}'s job until {minutes_to_ampm(prev_end)}{suffix}",
                    'type': 'running_long' if running_long else 'double_booked',
                    'blocker_id': prev.id,
                    'blocker_end': prev_end,
                }
                break
    return causes


def _mechanic_free_at(mechanic_name, booking_date, start_min, end_min, exclude_id):
    others = Booking.query.filter(
        Booking.assigned_mechanic_name == mechanic_name, Booking.date == booking_date,
        Booking.status != 'cancelled', Booking.id != exclude_id,
    ).all()
    for o in others:
        o_start = o.time.hour * 60 + o.time.minute
        o_end = real_end_minutes(o_start, o.duration_minutes or DEFAULT_DURATION_MIN, o.overrun_minutes or 0)
        if mechanic_overlaps(start_min, end_min, o_start, o_end):
            return False
    return True


def _fix1_preview(booking, on_duty_names):
    """Fix #1: give it to the mechanic the customer asked for, if they're free
    right now at this exact slot — same slot, nothing to tell the customer."""
    preferred = booking.preferred_mechanic_name
    if not preferred:
        return None
    if preferred not in on_duty_names:
        return {'available': False, 'mechanic': preferred, 'reason': f'{preferred} is not on duty.'}
    start_min = booking.time.hour * 60 + booking.time.minute
    duration = booking.duration_minutes or DEFAULT_DURATION_MIN
    end_min = real_end_minutes(start_min, duration, 0)
    if _mechanic_free_at(preferred, booking.date, start_min, end_min, booking.id):
        return {'available': True, 'mechanic': preferred}
    return {'available': False, 'mechanic': preferred, 'reason': f'{preferred} is busy then too.'}


def _fix2_slot_on_day(d, duration, mechanic, exclude_id, min_start=None):
    """Every slot on one day for one mechanic, checked against that day's
    shop/mechanic schedule fetched ONCE — not re-queried per slot. Calls the
    exact same validate_booking() every other booking path uses, just fed
    pre-gathered data instead of letting it (via _check_admin_booking_request)
    re-fetch that data on every single slot attempt. Returns the first free
    start-minute, or None."""
    shop_q = Booking.query.filter(Booking.date == d, Booking.status != 'cancelled')
    shop_q = shop_q.filter(Booking.id != exclude_id)
    shop_intervals = _gather_intervals(shop_q)
    daily_count = len(shop_intervals)
    shop_intervals = shop_intervals + _gather_blocked_intervals(d)

    _, _, on_duty = get_on_duty_mechanics(d)
    mechanic_status = 'available' if any(m.name == mechanic for m in on_duty) else 'off duty'

    mech_q = Booking.query.filter(
        Booking.assigned_mechanic_name == mechanic, Booking.date == d, Booking.status != 'cancelled',
    )
    mech_q = mech_q.filter(Booking.id != exclude_id)
    mechanic_intervals = _gather_intervals(mech_q)

    daily_cap = get_daily_cap(d)

    for slot in all_slot_starts():
        if min_start is not None and slot <= min_start:
            continue
        ok, _ = validate_booking(
            slot, duration, shop_intervals, daily_count,
            require_slot_grid=True, daily_cap=daily_cap, on_duty_count=len(on_duty),
            mechanic_name=mechanic, mechanic_status=mechanic_status, mechanic_intervals=mechanic_intervals,
        )
        if ok:
            return slot
    return None


def _fix2_preview(booking):
    """Fix #2: keep the assigned mechanic, move the time — a later slot the
    same day first, then the first workable date within 14 days."""
    mechanic = booking.assigned_mechanic_name
    if not mechanic:
        return None
    duration = booking.duration_minutes or DEFAULT_DURATION_MIN
    start_min = booking.time.hour * 60 + booking.time.minute

    slot = _fix2_slot_on_day(booking.date, duration, mechanic, booking.id, min_start=start_min)
    if slot is not None:
        return {'available': True, 'date': booking.date.isoformat(), 'time': minutes_to_hhmm(slot),
                'label': f"{booking.date.strftime('%b %d')} at {minutes_to_ampm(slot)}", 'same_day': True}

    d = booking.date
    for _ in range(14):
        d = d + timedelta(days=1)
        if not is_working_day(d):
            continue
        slot = _fix2_slot_on_day(d, duration, mechanic, booking.id)
        if slot is not None:
            return {'available': True, 'date': d.isoformat(), 'time': minutes_to_hhmm(slot),
                    'label': f"{d.strftime('%b %d')} at {minutes_to_ampm(slot)}", 'same_day': False}
    return {'available': False}


def _fix3_preview(booking, on_duty):
    """Fix #3: hand it to any other free mechanic at the same time — flagged
    as changing what the customer originally asked for."""
    start_min = booking.time.hour * 60 + booking.time.minute
    duration = booking.duration_minutes or DEFAULT_DURATION_MIN
    end_min = real_end_minutes(start_min, duration, 0)
    for m in on_duty:
        if m.name == booking.assigned_mechanic_name:
            continue
        if _mechanic_free_at(m.name, booking.date, start_min, end_min, booking.id):
            return {'available': True, 'mechanic': m.name, 'specialization': m.specialization}
    return {'available': False}


def _compute_day_clash_ids(the_date, bookings=None):
    """Lightweight companion to _compute_day_clashes — just the set of clashing
    booking IDs, skipping the per-clash fix previews (each of which can walk
    up to 14 days of slot checks). Callers that only need to know WHICH
    bookings are clashing — like the month calendar's badge dots — should use
    this instead of paying for fixes they'll never render. Same detection
    logic as _compute_day_clashes, kept in sync by hand since it's short."""
    if bookings is None:
        bookings = Booking.query.filter(Booking.date == the_date, Booking.status != 'cancelled').order_by(Booking.time).all()
    else:
        bookings = [b for b in bookings if b.status != 'cancelled']

    _, _, on_duty = get_on_duty_mechanics(the_date)
    on_duty_names = {m.name for m in on_duty}

    by_mechanic = {}
    for b in bookings:
        if b.assigned_mechanic_name:
            by_mechanic.setdefault(b.assigned_mechanic_name, []).append(b)
    overlap_causes = {}
    for blist in by_mechanic.values():
        overlap_causes.update(_mechanic_overlap_causes(blist))

    clash_ids = set()
    for b in bookings:
        if b.id in overlap_causes or not b.assigned_mechanic_name or b.assigned_mechanic_name not in on_duty_names:
            clash_ids.add(b.id)
    return clash_ids


def _compute_day_clashes(the_date, bookings=None):
    """Every clash open on one day, in booking order, each with its cause in
    plain words and a preview of what each numbered fix would actually do."""
    if bookings is None:
        bookings = Booking.query.filter(Booking.date == the_date, Booking.status != 'cancelled').order_by(Booking.time).all()
    else:
        bookings = [b for b in bookings if b.status != 'cancelled']

    _, _, on_duty = get_on_duty_mechanics(the_date)
    on_duty_names = {m.name for m in on_duty}

    by_mechanic = {}
    for b in bookings:
        if b.assigned_mechanic_name:
            by_mechanic.setdefault(b.assigned_mechanic_name, []).append(b)
    overlap_causes = {}
    for blist in by_mechanic.values():
        overlap_causes.update(_mechanic_overlap_causes(blist))

    clashes = []
    for b in sorted(bookings, key=lambda x: (x.time, x.id)):
        cause = None
        if b.id in overlap_causes:
            cause = overlap_causes[b.id]
        elif not b.assigned_mechanic_name:
            cause = {'text': 'No mechanic assigned', 'type': 'unassigned'}
        elif b.assigned_mechanic_name not in on_duty_names:
            cause = {'text': f'{b.assigned_mechanic_name} is not on duty', 'type': 'off_duty'}
        if not cause:
            continue

        start_min = b.time.hour * 60 + b.time.minute
        fixes = {
            'preferred':    _fix1_preview(b, on_duty_names),
            'move_time':    _fix2_preview(b),
            'any_free':     _fix3_preview(b, on_duty),
            'delay_notice': {'available': True} if cause['type'] == 'running_long' else None,
        }
        clashes.append({
            'id': b.id,
            'customer': _booking_customer_name(b),
            'service': b.service,
            'time_label': minutes_to_ampm(start_min),
            'cause': cause['text'],
            'cause_type': cause['type'],
            'assigned_mechanic': b.assigned_mechanic_name,
            'preferred_mechanic': b.preferred_mechanic_name,
            'fixes': fixes,
        })
    return clashes


@admin_app.route('/booking/<int:bid>/resolve-clash', methods=['POST'])
@login_required
@require_admin_or_staff
def resolve_clash(bid):
    """Apply one of the four clash fixes to a single booking. Every fix that
    actually changes the mechanic or the time re-validates through the same
    shared routine as any other schedule change — a fix can never trade one
    clash for a different one. Reports in plain words what happened and
    whether an email went out."""
    booking = Booking.query.get_or_404(bid)
    data = request.get_json() or {}
    fix = data.get('fix')
    notify = data.get('notify', True)

    def fail(msg):
        return jsonify({'success': False, 'error': msg}), 400

    if fix == 'preferred':
        _, _, on_duty = get_on_duty_mechanics(booking.date)
        preview = _fix1_preview(booking, {m.name for m in on_duty})
        if not preview or not preview.get('available'):
            return fail((preview or {}).get('reason') or 'No preferred mechanic on file for this booking.')
        mechanic = Mechanic.query.filter_by(name=preview['mechanic']).first()
        ok, error = _check_admin_booking_request(
            booking.date, booking.time.hour * 60 + booking.time.minute, booking.duration_minutes or DEFAULT_DURATION_MIN,
            exclude_id=booking.id, mechanic_name=mechanic.name, require_slot_grid=False, check_shop_queue=False,
        )
        if not ok:
            return fail(error)
        booking.assigned_mechanic_name = mechanic.name
        booking.assigned_mechanic_specialization = mechanic.specialization
        db.session.commit()
        return jsonify({'success': True, 'emailed': False,
                         'message': f'{mechanic.name} now has this job — same time slot, nothing sent to the customer.'})

    if fix == 'move_time':
        preview = _fix2_preview(booking)
        if not preview or not preview.get('available'):
            return fail('Nothing is free for this mechanic within the next two weeks. Please call the customer directly.')
        new_date = datetime.strptime(preview['date'], '%Y-%m-%d').date()
        new_time = datetime.strptime(preview['time'], '%H:%M').time()
        duration = booking.duration_minutes or DEFAULT_DURATION_MIN
        ok, error = _check_admin_booking_request(
            new_date, hhmm_to_minutes(preview['time']), duration, exclude_id=booking.id,
            mechanic_name=booking.assigned_mechanic_name,
        )
        if not ok:
            return fail(error)
        result = _apply_reschedule(booking, new_date, new_time, notify=notify)
        where = 'later today' if preview['same_day'] else f"on {new_date.strftime('%b %d')}"
        return jsonify({'success': True, 'emailed': result['emailed'],
                         'message': f"Kept {booking.assigned_mechanic_name}, moved the job to {result['time']} {where}."})

    if fix == 'any_free':
        _, _, on_duty = get_on_duty_mechanics(booking.date)
        preview = _fix3_preview(booking, on_duty)
        if not preview or not preview.get('available'):
            return fail('No one else on duty is free at this exact time. Please call the customer directly.')
        mechanic = Mechanic.query.filter_by(name=preview['mechanic']).first()
        old_name = booking.assigned_mechanic_name or 'No one'
        ok, error = _check_admin_booking_request(
            booking.date, booking.time.hour * 60 + booking.time.minute, booking.duration_minutes or DEFAULT_DURATION_MIN,
            exclude_id=booking.id, mechanic_name=mechanic.name, require_slot_grid=False, check_shop_queue=False,
        )
        if not ok:
            return fail(error)
        booking.assigned_mechanic_name = mechanic.name
        booking.assigned_mechanic_specialization = mechanic.specialization
        db.session.commit()

        emailed = _mechanic_swap_notification(booking, mechanic.name) if notify else False
        return jsonify({'success': True, 'emailed': emailed,
                         'message': f"{old_name} → {mechanic.name} at the same time — flagged as a change from what the customer asked for."})

    if fix == 'delay_notice':
        same_mechanic = Booking.query.filter(
            Booking.date == booking.date, Booking.status != 'cancelled',
            Booking.assigned_mechanic_name == booking.assigned_mechanic_name,
        ).all() if booking.assigned_mechanic_name else []
        cause = _mechanic_overlap_causes(same_mechanic).get(booking.id)
        if not cause or cause['type'] != 'running_long':
            return fail('This booking is not currently blocked by a job running long.')
        realistic_start = minutes_to_ampm(cause['blocker_end'])
        ref = f'BKG-{booking.id:03d}'
        time_str = booking.time.strftime('%I:%M %p')
        date_str = booking.date.strftime('%b %d, %Y')
        mechanic_line = booking.assigned_mechanic_name or 'shop decides'
        title = f'Your {time_str} slot is running behind, realistic start {realistic_start}'
        body = (f'The job ahead of yours is taking longer than expected, so we will not be able to start at '
                f'{time_str}. We now expect to start around {realistic_start} instead, on {date_str}. '
                f'Mechanic: {mechanic_line}. Services: {booking.service} ({ref}). '
                f'Your slot is still yours. If the new time does not work, reply to this email and we will '
                f'reschedule you at no charge.')
        emailed = _notify_customer(booking, title, body, priority=True)
        return jsonify({'success': True, 'emailed': emailed,
                         'message': f'Delay notice sent — told the customer to expect ~{realistic_start}.'})

    return fail('Unknown fix.')


@admin_app.route('/booking/<int:bid>/ready-for-pickup', methods=['POST'])
@login_required
@require_admin_or_staff
def booking_ready_for_pickup(bid):
    """Closes an open multi-day job: frees the mechanic's bay and tells the
    customer their bike is ready."""
    booking = Booking.query.get_or_404(bid)
    if not booking.is_multiday:
        return jsonify({'success': False, 'error': 'This is not a multi-day job.'}), 400
    if booking.status in ('cancelled', 'ready_for_pickup', 'completed'):
        return jsonify({'success': False, 'error': 'This job is already closed.'}), 400

    booking.status = 'ready_for_pickup'
    db.session.commit()

    ref = f'BKG-{booking.id:03d}'
    date_str = booking.date.strftime('%b %d, %Y')
    title = f'{booking.service} — ready for pickup'
    body = (f'Your motorcycle is ready for pickup. We carried out: {booking.service} ({ref}), dropped off '
            f'{date_str}. Collect anytime: Mon–Sun, 8:00 AM – 6:30 PM.')
    emailed = _notify_customer(booking, title, body, priority=False)
    return jsonify({'success': True, 'emailed': emailed, 'message': f'{booking.service} closed — bay freed.'})


def _fmt_minutes_long(m):
    if m % 60 == 0:
        h = m // 60
        return f'{h} hour' + ('' if h == 1 else 's')
    return f'{m} minutes'


def _running_late_email(customer_name, booking, headline, intro, label, big_time, sub):
    """The shop's standard status-update email: dark MOTO TYRE bar, one big
    number in a highlighted card, the booking facts, a button to the booking."""
    first = (customer_name or 'there').split(' ')[0]
    rows = ''.join(
        f'<tr><td style="padding:9px 0;border-bottom:1px solid #eee;color:#555;font-size:14px;">{k}</td>'
        f'<td style="padding:9px 0;border-bottom:1px solid #eee;text-align:right;font-weight:bold;font-size:14px;">{v}</td></tr>'
        for k, v in [('Booking', f'BKG-{booking.id:03d}'), ('Service', booking.service),
                     ('Date', booking.date.strftime('%a, %b %d, %Y')),
                     ('Mechanic', booking.assigned_mechanic_name or 'To be assigned'),
                     ('Motorcycle', booking.motorcycle_model or '—')])
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;background:#fff;border:1px solid #e5e5e5;border-radius:10px;overflow:hidden;">
      <div style="background:#111;padding:16px 22px;font-size:22px;font-weight:900;letter-spacing:1px;color:#fff;">MOTO <span style="color:#d00019;">TYRE</span></div>
      <div style="padding:22px;">
        <h2 style="margin:0 0 12px;font-size:20px;">{headline}</h2>
        <p style="margin:0 0 16px;font-size:14px;">Hi {first}, {intro}</p>
        <div style="background:#fff3e8;border:1px solid #fdba74;border-radius:10px;text-align:center;padding:16px;margin-bottom:16px;">
          <div style="font-size:11px;font-weight:bold;letter-spacing:1px;color:#c2410c;">{label}</div>
          <div style="font-size:34px;font-weight:900;margin:4px 0;">{big_time}</div>
          <div style="font-size:12px;color:#666;">{sub}</div>
        </div>
        <table style="width:100%;border-collapse:collapse;margin-bottom:16px;">{rows}</table>
        <p style="font-size:14px;">No need to do anything. We'll let you know as soon as it's done.</p>
        <p><a href="{BASE_URL}/customer/dashboard?open_booking={booking.id}" style="display:inline-block;padding:11px 20px;background:#d00019;color:#fff;border-radius:6px;text-decoration:none;font-weight:bold;">View My Booking</a></p>
        <p style="font-size:13px;color:#555;">Questions? Reply to this email or call the shop, 8:00 AM – 6:30 PM.</p>
      </div>
      <div style="background:#f6f6f6;padding:12px 22px;font-size:12px;color:#888;">MotoTyres North Caloocan · This is an automated email.</div>
    </div>"""


def _send_running_late_notice(booking, old_end_min, new_end_min, added_minutes):
    """Tells the customer — portal bell and email — that the job now runs
    longer (added_minutes > 0) or is back on schedule (added_minutes == 0).
    Remembers what was sent so the Day Schedule card can show it. Walk-ins
    have no account, so nothing is sent for them."""
    if booking.walkin_customer_id:
        return False
    ref = f'BKG-{booking.id:03d}'
    new_label, old_label = minutes_to_ampm(new_end_min), minutes_to_ampm(old_end_min)
    customer = User.query.get(booking.user_id)
    late = added_minutes > 0
    if late:
        longer = _fmt_minutes_long(added_minutes)
        title = f'{ref} is taking {longer} longer'
        body = (f'{booking.service} — the mechanic needs about {longer} more. '
                f'New estimated finish: {new_label} (was {old_label}).')
        status = 'running_late'
        subject = f'Update on {ref}: ready around {new_label}'
        email_html = _running_late_email(customer.fullname if customer else '', booking,
                                         'Your service is taking a little longer',
                                         f'the mechanic needs about {longer} more to finish your motorcycle.',
                                         'NEW ESTIMATED FINISH', new_label, f'was {old_label}')
        text = f'New finish {new_label}'
    else:
        title = f'{ref} is back on schedule'
        body = f'{booking.service} — good news, the mechanic is back on track. Estimated finish: {new_label}.'
        status = 'on_schedule'
        subject = f'Update on {ref}: back on schedule'
        email_html = _running_late_email(customer.fullname if customer else '', booking,
                                         'Your service is back on schedule',
                                         'the mechanic is back on track with your motorcycle.',
                                         'ESTIMATED FINISH', new_label, 'on schedule')
        email_html = email_html.replace('#fff3e8', '#ecfdf5').replace('#fdba74', '#a7f3d0').replace('#c2410c', '#15803d')
        text = f'Back on schedule · finish {new_label}'
    send_notification(booking.user_id, title, body, type='booking', status=status, booking_id=booking.id,
                      priority=False, collapse_minutes=None)
    emailed = False
    if customer and customer.email:
        _send_gmail(customer.email, subject, email_html)
        emailed = True
    booking.late_notice_at = ph_now()
    booking.late_notice_text = text
    booking.late_notice_emailed = emailed
    db.session.commit()
    return emailed


@admin_app.route('/booking/<int:bid>/running-long', methods=['POST'])
@login_required
@require_admin_or_staff
def booking_running_long(bid):
    """Counter staff records that a job is overrunning its scheduled duration
    (or clears that). The extra minutes push into the mechanic's real end
    time, which is what the clash detector and the delay notice both read."""
    booking = Booking.query.get_or_404(bid)
    data = request.get_json() or {}
    action = data.get('action')
    if booking.status not in ('inspection', 'in_progress', 'inprogress'):
        return jsonify({'success': False, 'error': 'Running-late controls unlock once Start is clicked in Bookings.'}), 400
    _start_min = booking.time.hour * 60 + booking.time.minute
    _dur = booking.duration_minutes or DEFAULT_DURATION_MIN
    old_overrun = booking.overrun_minutes or 0
    old_end = real_end_minutes(_start_min, _dur, old_overrun)
    if action == 'reset':
        booking.overrun_minutes = 0
    elif action == 'add30':
        booking.overrun_minutes = old_overrun + 30
    elif action == 'add60':
        booking.overrun_minutes = old_overrun + 60
    else:
        return jsonify({'success': False, 'error': 'Unknown action.'}), 400
    db.session.commit()

    # Save the new finish and tell the customer (bell + email). "Back on time"
    # only says anything if they'd actually been told it was running late.
    new_end = real_end_minutes(_start_min, _dur, booking.overrun_minutes or 0)
    notified = None
    if action in ('add30', 'add60'):
        notified = {'emailed': _send_running_late_notice(booking, old_end, new_end, 30 if action == 'add30' else 60)}
    elif old_overrun > 0:
        notified = {'emailed': _send_running_late_notice(booking, old_end, new_end, 0)}

    warning = None
    if booking.assigned_mechanic_name and (booking.overrun_minutes or 0) > 0:
        start_min = booking.time.hour * 60 + booking.time.minute
        end_min = real_end_minutes(start_min, booking.duration_minutes or DEFAULT_DURATION_MIN, booking.overrun_minutes or 0)
        nxt = (Booking.query.filter(Booking.assigned_mechanic_name == booking.assigned_mechanic_name,
                                    Booking.date == booking.date, Booking.id != booking.id,
                                    Booking.status.notin_(['cancelled', 'completed'])).order_by(Booking.time).all())
        for o in nxt:
            o_start = o.time.hour * 60 + o.time.minute
            if o_start > start_min and o_start < end_min:
                warning = (f'Now runs until {minutes_to_ampm(end_min)} — that overlaps {booking.assigned_mechanic_name}\'s next '
                           f'booking (BKG-{o.id:03d} at {minutes_to_ampm(o_start)}). Reassign or reschedule one of them.')
                break
    return jsonify({'success': True, 'overrun_minutes': booking.overrun_minutes, 'warning': warning,
                    'notified': bool(notified and not booking.walkin_customer_id), 'emailed': bool(notified and notified['emailed'])})


@admin_app.route('/booking/<int:bid>/cancel', methods=['POST'])
@login_required
@require_admin_or_staff
def cancel_booking_day_panel(bid):
    """Cancels a booking and frees the mechanic's time immediately (a
    cancelled booking is excluded from every conflict check). Sent only when
    the admin chooses to notify — but when it is sent, it's priority: a
    shop-initiated cancellation is the message people most want to avoid
    sending and the one where silence does the most damage. Stays visible —
    struck through — and never counts toward the daily cap again."""
    booking = Booking.query.get_or_404(bid)
    if booking.status in ('inspection', 'in_progress', 'inprogress', 'completed'):
        return jsonify({'success': False, 'error': "Cancel isn't available once work has started."}), 400
    data = request.get_json() or {}
    notify = data.get('notify', True)
    reason = clean_str(data.get('reason', ''), max_len=300)
    if notify and not reason:
        return jsonify({'success': False, 'error': 'Say why you are cancelling — the customer sees this reason.'}), 400
    booking.status = 'cancelled'
    db.session.commit()

    emailed = False
    if notify:
        ref = f'BKG-{booking.id:03d}'
        date_str = booking.date.strftime('%b %d, %Y')
        date_short = booking.date.strftime('%b %d')
        time_str = booking.time.strftime('%I:%M %p')
        title = f'Cancelled — {date_short} at {time_str}'
        reason_text = reason or 'the shop is unable to take this appointment'
        body = (f"Your {booking.service} appointment ({ref}) on {date_str} at {time_str} was cancelled. "
                f"Reason: {reason_text}. You can book again anytime, or reply with any questions.")
        emailed = _notify_customer(booking, title, body, priority=True, status='cancelled', cta_label='Book Again')
    _mname = booking.assigned_mechanic_name
    _mnow = _mech_now_text(_mname)
    return jsonify({'success': True, 'emailed': emailed,
                     'toast': f'Booking {booking.id} cancelled' + (f' · {_mname} is now {_mnow}' if _mnow else ''),
                     'mechanic_statuses': mechanic_status_map(),
                     'message': f'Cancelled{" — customer notified." if emailed else (" — customer not notified." if not notify else " — no email on file.")}'})


@admin_app.route('/booking/<int:bid>/undo-status', methods=['POST'])
@login_required
@require_admin_or_staff
def undo_booking_status(bid):
    """Moves a booking back exactly one step (In Progress → Inspection &
    Estimation → Confirmed) and clears the time that step was reached. A
    Completed booking can't be undone — its invoice is already out."""
    booking = Booking.query.get_or_404(bid)
    if booking.status == 'completed':
        return jsonify({'success': False, 'error': 'A completed booking can\'t be undone — the invoice has been sent.'}), 400
    if booking.status in ('in_progress', 'inprogress'):
        booking.status = 'inspection'
        booking.in_progress_at = None
    elif booking.status == 'inspection':
        q = Quotation.query.filter_by(booking_id=booking.id).first()
        if q:
            return jsonify({'success': False, 'error': 'A quotation was already sent for this booking — withdraw it before moving back to Confirmed.'}), 400
        booking.status = 'confirmed'
        booking.inspection_at = None
        booking.actual_start_at = None
    else:
        return jsonify({'success': False, 'error': 'There is no earlier step to go back to.'}), 400
    db.session.commit()
    return jsonify({'success': True, 'new_status': booking.status})


@admin_app.route('/booking/<int:bid>/restore', methods=['POST'])
@login_required
@require_admin_or_staff
def restore_booking_day_panel(bid):
    """Un-cancels a booking back to confirmed. Bypasses fresh-booking
    validation on purpose — restoring what already existed isn't creating a
    new commitment, and if it lands back on a clash, that's exactly what the
    Clashes-to-resolve panel exists to surface and fix."""
    booking = Booking.query.get_or_404(bid)
    if booking.status != 'cancelled':
        return jsonify({'success': False, 'error': 'This booking is not cancelled.'}), 400
    booking.status = 'confirmed'
    db.session.commit()
    return jsonify({'success': True, 'message': 'Booking restored.'})


@admin_app.route('/api/blocked-slots', methods=['GET', 'POST', 'DELETE'])
@login_required
@require_admin_or_staff
def api_blocked_slots():
    """A blocked slot is a manual hold on one start time — staff meeting,
    parts delivery — that removes it from the customer flow immediately
    without touching any real booking. GET lists a day's blocked times; POST
    blocks one; DELETE unblocks it."""
    if request.method == 'GET':
        date_str = request.args.get('date', '').strip()
        try:
            the_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'error': 'Invalid date'}), 400
        rows = BlockedSlot.query.filter_by(date=the_date).all()
        return jsonify({'blocked': [minutes_to_hhmm(r.time.hour * 60 + r.time.minute) for r in rows]})

    data = request.get_json() or {}
    date_str = clean_str(data.get('date', ''), max_len=10)
    time_str = clean_str(data.get('time', ''), max_len=5)
    try:
        the_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        the_time = datetime.strptime(time_str, '%H:%M').time()
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid date/time.'}), 400

    existing = BlockedSlot.query.filter_by(date=the_date, time=the_time).first()
    if request.method == 'DELETE':
        if existing:
            db.session.delete(existing)
            db.session.commit()
        return jsonify({'success': True, 'blocked': False})

    if existing:
        return jsonify({'success': True, 'blocked': True})

    start_min = the_time.hour * 60 + the_time.minute
    for b in Booking.query.filter(Booking.date == the_date, Booking.status != 'cancelled').all():
        b_start = b.time.hour * 60 + b.time.minute
        b_end = compute_finish_minutes(b_start, b.duration_minutes or DEFAULT_DURATION_MIN)
        if _overlaps(start_min, start_min + SLOT_GRANULARITY_MIN, b_start, b_end):
            return jsonify({'success': False, 'error': 'A booking already occupies this slot — cancel or move it first.'}), 400

    db.session.add(BlockedSlot(date=the_date, time=the_time, reason=clean_str(data.get('reason', ''), max_len=100)))
    db.session.commit()
    return jsonify({'success': True, 'blocked': True})


@admin_app.route('/api/dev/try-double-booking', methods=['POST'])
@login_required
@require_admin_or_staff
def dev_try_double_booking():
    """Proof, not a feature: calls the exact same server-side routine a real
    booking would, deliberately aimed at an existing booking's mechanic and
    exact slot (no exclude_id), and returns the rejection verbatim — showing
    the rule lives in _check_admin_booking_request / validate_booking, not in
    whatever the form happens to let you click."""
    data = request.get_json() or {}
    date_str = clean_str(data.get('date', ''), max_len=10)
    try:
        the_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400

    target = Booking.query.filter(
        Booking.date == the_date, Booking.status != 'cancelled',
        Booking.assigned_mechanic_name.isnot(None), Booking.assigned_mechanic_name != '',
    ).first()
    if not target:
        return jsonify({'ok': None, 'message': 'No assigned booking on this day to try to collide with.'})

    start_min = target.time.hour * 60 + target.time.minute
    duration = target.duration_minutes or DEFAULT_DURATION_MIN
    ok, error = _check_admin_booking_request(the_date, start_min, duration, mechanic_name=target.assigned_mechanic_name)
    return jsonify({
        'ok': ok,
        'message': error or 'Unexpectedly allowed — this should never happen.',
        'attempted': f'{target.assigned_mechanic_name} at {minutes_to_ampm(start_min)} on {the_date.strftime("%b %d, %Y")} (same slot as booking #{target.id})',
    })


@admin_app.route('/api/day-panel')
@login_required
@require_admin_or_staff
def api_day_panel():
    """Everything the Day panel renders, computed together in one pass since
    most of it shares the same day's bookings and on-duty roster: the header
    counts, the In-the-bay strip, every open clash with its fix previews, the
    per-mechanic timeline, and the time-slot blocks with their booking cards."""
    date_str = request.args.get('date', '').strip()
    try:
        the_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400

    bookings = Booking.query.filter(Booking.date == the_date).order_by(Booking.time).all()
    active = [b for b in bookings if b.status != 'cancelled']

    _, _, on_duty = get_on_duty_mechanics(the_date)
    daily_cap = get_daily_cap(the_date)
    clashes = _compute_day_clashes(the_date, bookings=bookings)
    clash_ids = {c['id'] for c in clashes}
    is_full = len(active) >= daily_cap

    # ── In the bay: every currently-open multi-day drop-off, regardless of
    # which day is being viewed — bay occupancy isn't a property of one date.
    bay_jobs = Booking.query.filter(
        Booking.is_multiday == True, Booking.status.notin_(['cancelled', 'ready_for_pickup']),
        Booking.date <= ph_now().date(),
    ).order_by(Booking.date).all()
    bay = []
    for b in bay_jobs:
        prog = multiday_progress(b.date, ph_now().date())
        bay.append({
            'id': b.id,
            'customer': _booking_customer_name(b),
            'service': b.service,
            'mechanic': b.assigned_mechanic_name,
            'day_label': prog['label'],
            'release_label': f"ready {prog['release_from'].strftime('%b %d')} to {prog['release_to'].strftime('%b %d')}",
            'dropoff_date': b.date.isoformat(),
        })

    walkin_w = _walkin_windows(the_date)

    # ── Mechanic timeline ──
    timeline = []
    free_all_day = []
    for m in on_duty:
        m_bookings = [b for b in active if b.assigned_mechanic_name == m.name]
        bars = []
        for w in walkin_w:
            if w['mechanic'] == m.name:
                bars.append({'id': -w['jo_id'], 'start': w['start'], 'end': w['end'], 'customer': f"Walk-in {w['ref']}",
                             'service': 'Walk-in job', 'is_multiday': False, 'clash': False, 'overrunning': False,
                             'status': 'walkin'})
        for b in m_bookings:
            start_min = b.time.hour * 60 + b.time.minute
            duration = MULTIDAY_INTAKE_MIN if b.is_multiday else (b.duration_minutes or DEFAULT_DURATION_MIN)
            end_min = real_end_minutes(start_min, duration, b.overrun_minutes or 0)
            bars.append({
                'id': b.id, 'start': start_min, 'end': end_min,
                'customer': _booking_customer_name(b), 'service': b.service,
                'is_multiday': bool(b.is_multiday),
                'clash': b.id in clash_ids,
                'overrunning': (b.overrun_minutes or 0) > 0,
                'status': b.status,
            })
        timeline.append({'mechanic_id': m.id, 'mechanic': m.name, 'specialization': m.specialization, 'bars': bars})
        if not bars:
            free_all_day.append(m.name)

    unassigned_bars = []
    for b in active:
        if not b.assigned_mechanic_name:
            start_min = b.time.hour * 60 + b.time.minute
            duration = MULTIDAY_INTAKE_MIN if b.is_multiday else (b.duration_minutes or DEFAULT_DURATION_MIN)
            end_min = real_end_minutes(start_min, duration, b.overrun_minutes or 0)
            unassigned_bars.append({
                'id': b.id, 'start': start_min, 'end': end_min,
                'customer': _booking_customer_name(b), 'service': b.service,
                'is_multiday': bool(b.is_multiday), 'clash': True,
                'overrunning': (b.overrun_minutes or 0) > 0, 'status': b.status,
            })

    # ── Per-booking workflow info for the cards ──
    _day_ids = [b.id for b in bookings]
    _day_quotes = {}
    if _day_ids:
        for q in Quotation.query.filter(Quotation.booking_id.in_(_day_ids)).order_by(Quotation.created_at.asc()).all():
            _day_quotes[q.booking_id] = q
    _day_claims = {}
    if _day_ids:
        for rr in ReturnRequest.query.filter(ReturnRequest.redo_booking_id.in_(_day_ids)).all():
            _day_claims[rr.redo_booking_id] = rr

    def _norm_status(st):
        return 'in_progress' if st == 'inprogress' else st

    def _t_label(dt):
        return dt.strftime('%I:%M %p').lstrip('0') if dt else None

    status_counts = {'confirmed': 0, 'inspection': 0, 'in_progress': 0, 'completed': 0}
    for b in active:
        st = _norm_status(b.status)
        if st in status_counts:
            status_counts[st] += 1

    # ── Time slots + booking cards ──
    blocked_times = {bs.time.strftime('%H:%M') for bs in BlockedSlot.query.filter_by(date=the_date).all()}
    slots = []
    for start in all_slot_starts():
        hhmm = minutes_to_hhmm(start)
        occupied_mechanics = set()
        for b in active:
            if not b.assigned_mechanic_name:
                continue
            b_start = b.time.hour * 60 + b.time.minute
            b_dur = MULTIDAY_INTAKE_MIN if b.is_multiday else (b.duration_minutes or DEFAULT_DURATION_MIN)
            b_end = real_end_minutes(b_start, b_dur, b.overrun_minutes or 0)
            if b_start <= start < b_end:
                occupied_mechanics.add(b.assigned_mechanic_name)
        for w in walkin_w:                       # a walk-in ties up its mechanic for the slot too
            if w['start'] <= start < w['end']:
                occupied_mechanics.add(w['mechanic'])

        slot_bookings = [b for b in bookings if (b.time.hour * 60 + b.time.minute) == start]
        cards = []
        for b in slot_bookings:
            start_min = b.time.hour * 60 + b.time.minute
            duration = MULTIDAY_INTAKE_MIN if b.is_multiday else (b.duration_minutes or DEFAULT_DURATION_MIN)
            end_min = real_end_minutes(start_min, duration, b.overrun_minutes or 0)
            tags = []
            if b.status == 'cancelled':
                tags.append('Cancelled')
            if (b.overrun_minutes or 0) > 0:
                tags.append('Running long')
            if b.id in clash_ids:
                tags.append('Clash')
            if b.was_rescheduled:
                tags.append('Rescheduled')
            if b.preferred_mechanic_name and b.assigned_mechanic_name and b.preferred_mechanic_name != b.assigned_mechanic_name:
                tags.append('Not their mechanic')
            moto = (b.motorcycle_model or '').strip()
            if b.motorcycle_plate:
                moto = f'{moto} ({b.motorcycle_plate})' if moto else b.motorcycle_plate
            cards.append({
                'id': b.id,
                'customer': _booking_customer_name(b),
                'start': start_min,
                'end': end_min,
                'time_label': minutes_to_ampm(start_min),
                'end_label': minutes_to_ampm(end_min),
                'duration_label': format_duration(duration),
                'ref': f'BKG-{b.id:03d}',
                'mechanic': b.assigned_mechanic_name,
                'specialization': b.assigned_mechanic_specialization,
                'preferred_mechanic': b.preferred_mechanic_name,
                'chosen_by': mechanic_origin_note(b.assigned_mechanic_name, b.preferred_mechanic_name),
                'motorcycle': moto,
                'services': b.service,
                'status': b.status,
                'tags': tags,
                'overrun_minutes': b.overrun_minutes or 0,
                'is_multiday': bool(b.is_multiday),
                'can_reschedule': b.status not in ('completed', 'cancelled'),
                'status_key': _norm_status(b.status),
                'steps': {
                    'confirmed': b.created_at.strftime('%b %d') if b.created_at else None,
                    'inspection': _t_label(b.inspection_at),
                    'in_progress': _t_label(b.in_progress_at),
                    'completed': _t_label(b.completed_at),
                },
                'quotation': ({'status': _day_quotes[b.id].status,
                               'extra': (lambda bd: bd['extra'] + bd['materials'])(_quotation_breakdown(_day_quotes[b.id].items))}
                              if b.id in _day_quotes else None),
                'is_backjob': b.service.startswith(BACKJOB_PREFIX),
                'rma_ref': (_return_ref(_day_claims[b.id]) if b.id in _day_claims else None),
                'customer_service': b.service[len(BACKJOB_PREFIX):] if b.service.startswith(BACKJOB_PREFIX) else b.service,
                'completed_label': _t_label(b.completed_at),
                'needs_mechanic': (not b.assigned_mechanic_name and b.status in ('confirmed', 'inspection', 'in_progress', 'inprogress')),
                'shop_to_assign': (not b.preferred_mechanic_name and not b.assigned_mechanic_name),
                'is_new': bool(b.created_at and (ph_now() - b.created_at) < timedelta(hours=24) and b.status == 'confirmed'),
                'assign_notice': ({'kind': b.assign_notice_kind, 'mechanic': b.assigned_mechanic_name,
                                   'sent_at': _t_label(b.assign_notice_at),
                                   'first_name': (_booking_customer_name(b) or '').split(' ')[0],
                                   'email': (b.customer.email if (b.customer and b.assign_notice_emailed) else None)}
                                  if b.assign_notice_at else None),
                'late_notice': ({'text': b.late_notice_text,
                                 'sent_at': _t_label(b.late_notice_at),
                                 'first_name': (_booking_customer_name(b) or '').split(' ')[0],
                                 'email': (b.customer.email if (b.customer and b.late_notice_emailed) else None)}
                                if b.late_notice_at else None),
            })

        active_here = [b for b in slot_bookings if b.status != 'cancelled']
        # One dot per slot: the booking still needing attention, else Completed.
        _open = [b for b in active_here if b.status != 'completed']
        _dot = _norm_status((_open[0] if _open else active_here[0]).status) if active_here else None
        slots.append({
            'dot_status': _dot,
            'needs_mechanic': sum(1 for b in active_here if not b.assigned_mechanic_name and b.status != 'completed'),
            'time': hhmm,
            'label': minutes_to_ampm(start),
            'booked_count': len(active_here),
            'mechanic_count': len(on_duty),
            'free_mechanics': max(len(on_duty) - len(occupied_mechanics), 0),
            'blocked': hhmm in blocked_times,
            'cards': cards,
        })

    return jsonify({
        'date': date_str,
        'date_label': the_date.strftime('%A, %B %d, %Y'),
        'booked_count': len(active),
        'daily_cap': daily_cap,
        'mechanic_count': len(on_duty),
        'is_full': is_full,
        'has_clash': bool(clashes),
        'bay': bay,
        'clashes': clashes,
        'timeline': timeline,
        'unassigned_bars': unassigned_bars,
        'free_all_day': free_all_day,
        'status_counts': status_counts,
        'needs_mechanic_count': sum(1 for b in active if not b.assigned_mechanic_name and b.status != 'completed'),
        'slots': slots,
    })


@admin_app.route('/api/day-schedule')
@login_required
@require_admin_or_staff
def api_day_schedule():
    """Every non-cancelled booking on one date, in order, with its computed
    time window — the 'what does this do to the day' canvas admin sees before
    and after any change (reschedule, reassignment, status update)."""
    date_str = request.args.get('date', '').strip()
    try:
        the_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400

    bookings = Booking.query.filter(
        Booking.date == the_date, Booking.status != 'cancelled'
    ).order_by(Booking.time).all()

    rows = []
    for b in bookings:
        start_min = b.time.hour * 60 + b.time.minute
        end_min   = compute_finish_minutes(start_min, b.duration_minutes or DEFAULT_DURATION_MIN)
        customer_name = b.contact_name if b.walkin_customer_id else (b.customer.fullname if b.customer else '—')
        rows.append({
            'id': b.id,
            'time': minutes_to_hhmm(start_min), 'time_label': minutes_to_ampm(start_min),
            'end_time': minutes_to_hhmm(end_min), 'end_label': minutes_to_ampm(end_min),
            'service': b.service,
            'customer': customer_name,
            'status': b.status,
            'assigned_mechanic': b.assigned_mechanic_name,
            'preferred_mechanic': b.preferred_mechanic_name,
            'is_multiday': bool(b.is_multiday),
            'is_walkin': bool(b.walkin_customer_id),
        })
    return jsonify({'date': date_str, 'bookings': rows})


@admin_app.route('/api/month-schedule')
@login_required
@require_admin_or_staff
def api_month_schedule():
    """One row per day in a month, sized for the calendar grid: how many
    bookings, whether the day is full against its cap, whether any two
    bookings on it actually clash, and up to three chips to preview."""
    try:
        year = int(request.args.get('year', ''))
        month = int(request.args.get('month', ''))
        if not (1 <= month <= 12):
            raise ValueError
    except ValueError:
        return jsonify({'error': 'Invalid year/month'}), 400

    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

    bookings = Booking.query.filter(
        Booking.date >= start, Booking.date < end
    ).order_by(Booking.date, Booking.time).all()

    by_day = {}
    for b in bookings:
        by_day.setdefault(b.date, []).append(b)

    days = {}
    for d, day_bookings in by_day.items():
        active = [b for b in day_bookings if b.status != 'cancelled']
        cap = get_daily_cap(d)
        is_full = len(active) >= cap

        # A clash is one mechanic double-booked, a booking with nobody
        # assigned, or a booking sitting on someone no longer on duty — the
        # exact same definition the Day panel's Clashes-to-resolve list uses,
        # computed once here and reused for the badge and every chip.
        clashing_ids = _compute_day_clash_ids(d, bookings=day_bookings)

        chips = []
        for b in day_bookings[:3]:
            start_min = b.time.hour * 60 + b.time.minute
            end_min = compute_finish_minutes(start_min, b.duration_minutes or DEFAULT_DURATION_MIN)
            first_name = (b.contact_name if b.walkin_customer_id else (b.customer.fullname if b.customer else '—')).split(' ')[0]
            chips.append({
                'id': b.id,
                'first_name': first_name,
                'time_label': minutes_to_ampm(start_min),
                'end_label': minutes_to_ampm(end_min),
                'status': b.status,
                'clash': b.id in clashing_ids,
            })

        days[d.isoformat()] = {
            'count': len(active),
            'total_count': len(day_bookings),
            'is_full': is_full,
            'has_clash': len(clashing_ids) > 0,
            'daily_cap': cap,
            'bookings': chips,
            'more': max(0, len(day_bookings) - 3),
        }

    return jsonify({'year': year, 'month': month, 'days': days})


@admin_app.route('/api/outbox')
@login_required
@require_admin_or_staff
def api_outbox():
    """Every message the shop has sent a customer, in one place — the exact
    same rows the customer's own bell reads (one record, two views), so this
    is literally what the shop can check when someone phones to say they
    were never told. Priority-and-unread rows sort to the top; everything
    else is newest first."""
    rows = Notification.query.order_by(Notification.created_at.desc()).limit(300).all()

    booking_ids = {n.booking_id for n in rows if n.booking_id}
    bookings = {b.id: b for b in Booking.query.filter(Booking.id.in_(booking_ids)).all()} if booking_ids else {}
    user_ids = {n.user_id for n in rows}
    users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}

    quotation_decision_booking_ids = {n.booking_id for n in rows if n.type == 'quotation_decision' and n.booking_id}
    quotations_by_booking = {}
    if quotation_decision_booking_ids:
        for q in Quotation.query.filter(Quotation.booking_id.in_(quotation_decision_booking_ids)).order_by(Quotation.created_at.desc()).all():
            quotations_by_booking.setdefault(q.booking_id, q)

    def sort_key(n):
        return (0 if (n.priority and not n.is_read) else 1, -n.created_at.timestamp())

    rows.sort(key=sort_key)

    def quotation_decision_payload(n, b):
        q = quotations_by_booking.get(n.booking_id)
        if not q or not b:
            return None
        breakdown = _quotation_breakdown(q.items)
        total = breakdown['approve_total'] if q.status == 'approved' else breakdown['decline_total']
        completed = b.status == 'completed'
        customer_name = b.contact_name if b.walkin_customer_id else (b.customer.fullname if b.customer else 'Unknown customer')
        return {
            'booking_id': b.id, 'ref': f'BKG-{b.id:03d}', 'service': b.service,
            'customer_name': customer_name, 'motorcycle': b.motorcycle_model or '',
            'decision': q.status, 'completed': completed,
            'total': total, 'original': breakdown['decline_total'],
            'booked': breakdown['booked'], 'materials': breakdown['materials'], 'extra': breakdown['extra'],
            'items': [{'name': i.name, 'price': i.unit_price, 'locked': i.locked, 'item_type': i.item_type} for i in q.items],
            'invoice_paid': bool(b.invoice_paid),
            'invoice_payment_method': b.invoice_payment_method,
        }

    items = []
    needs_action_count = 0
    for n in rows:
        b = bookings.get(n.booking_id) if n.booking_id else None
        u = users.get(n.user_id)
        qd = quotation_decision_payload(n, b) if n.type == 'quotation_decision' else None
        if qd and not qd['completed']:
            needs_action_count += 1
        items.append({
            'id': n.id,
            'customer': u.fullname if u else 'Unknown customer',
            'title': n.title,
            'message': n.message,
            'type': n.type,
            'status': n.status,
            'is_read': n.is_read,
            'priority': n.priority,
            'booking_id': n.booking_id,
            'booking_date': b.date.isoformat() if b else None,
            'quotation_decision': qd,
            'created_at': n.created_at.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
        })

    unread_count = Notification.query.filter_by(is_read=False).count()
    return jsonify({'items': items, 'unread_count': unread_count, 'needs_action_count': needs_action_count})


@admin_app.route('/api/outbox/<int:nid>/read', methods=['POST'])
@login_required
@require_admin_or_staff
def api_outbox_read(nid):
    n = Notification.query.get_or_404(nid)
    n.is_read = True
    db.session.commit()
    return jsonify({'success': True})


@admin_app.route('/api/outbox/mark-all-read', methods=['POST'])
@login_required
@require_admin_or_staff
def api_outbox_mark_all_read():
    Notification.query.filter_by(is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})


# ── RETURNS / WARRANTY ─────────────────────────────────────────────────────
# A customer reporting a spare part that arrived wrong, or a service that
# didn't hold, and what they want done about it. The shop looks at the
# evidence, decides (approve with a remedy, or deny — both with a reason),
# and separately marks the remedy actually carried out.
#
# The rule the whole feature rests on: every claim forces ONE mutually
# exclusive choice — put it right, or give the money back, never both.
RETURN_OUTCOME_LABELS = {
    'product': {'replacement': 'Return and replacement', 'refund': 'Return and refund'},
    'service': {'redo_service': 'Back job', 'refund': 'Refund'},
}


RETURN_REASON_LABELS = {kind: {code: label for code, label, _ in opts} for kind, opts in RETURN_REASONS.items()}


def _return_ref(rr):
    return f'RMA-{rr.id:03d}'


def _return_line_items(rr):
    """[{name, quantity, unit_price, subtotal}] for a product claim's picked
    lines — empty for a service claim."""
    if rr.kind != 'product':
        return []
    rows = ReturnRequestItem.query.filter_by(return_request_id=rr.id).all()
    if not rows:
        return []
    items_by_id = {i.id: i for i in OrderItem.query.filter(
        OrderItem.id.in_([row.order_item_id for row in rows])
    ).all()}
    out = []
    for row in rows:
        item = items_by_id.get(row.order_item_id)
        if not item:
            continue
        out.append({
            'name': item.product.name if item.product else 'Item',
            'quantity': row.quantity,
            'unit_price': item.unit_price,
            'subtotal': item.unit_price * row.quantity,
        })
    return out


def _return_subject_label(rr):
    """Human-readable 'what' the claim is about: the picked line items, or
    the service — whichever of order/booking this claim is against."""
    if rr.kind == 'product':
        names = [f"{i['name']} ×{i['quantity']}" for i in _return_line_items(rr)]
        if names:
            return ', '.join(names)
        return f'ORD-{rr.order_id:03d}' if rr.order_id else 'an order'
    booking = Booking.query.get(rr.booking_id) if rr.booking_id else None
    return booking.service if booking else 'a service'


def _notify_return_customer(rr, title, body, priority=True, email_body=None, status=None,
                            email_subject=None, first_name_only=False):
    """Same shape as _notify_customer for bookings — bell + email, identical
    text, so a return decision never reads differently between the two.
    Opening it lands on this specific claim (Screen 3), not the list."""
    send_notification(rr.user_id, title, body, type='booking' if rr.kind == 'service' else 'order',
                       status=status or rr.status, priority=priority, return_request_id=rr.id)
    customer = User.query.get(rr.user_id)
    if not (customer and customer.email):
        return False
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;">
      <p>Hi {(customer.fullname or '').split(' ')[0] if first_name_only else customer.fullname},</p>
      <p>{email_body or body}</p>
      <p>— MotoTyres North Caloocan</p>
    </div>"""
    _send_gmail(customer.email, email_subject or f'{title} — MotoTyres', html)
    return True


@admin_app.route('/api/returns')
@login_required
@require_admin_or_staff
def api_returns():
    """Every return/warranty claim, plus the four work-counters and the
    filter-tab counts the queue page is built around. Unreviewed claims sort
    first; anything unreviewed past one working day is flagged overdue — a
    returns queue fails by going quiet, not by being wrong."""
    rows = ReturnRequest.query.order_by(ReturnRequest.created_at.desc()).all()
    user_ids = {r.user_id for r in rows}
    users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    order_ids = {r.order_id for r in rows if r.order_id}
    orders = {o.id: o for o in Order.query.filter(Order.id.in_(order_ids)).all()} if order_ids else {}
    booking_ids = {r.booking_id for r in rows if r.booking_id}
    bookings = {b.id: b for b in Booking.query.filter(Booking.id.in_(booking_ids)).all()} if booking_ids else {}
    today = ph_now().date()

    def sort_key(r):
        # Waiting on the customer isn't waiting on the shop — those don't
        # jump the queue the way a genuinely unreviewed claim does.
        unreviewed = r.status in ('submitted', 'under_review') and not r.awaiting_customer_info
        return (0 if unreviewed else 1, -r.created_at.timestamp())
    rows.sort(key=sort_key)

    needs_review = waiting_for_part = back_jobs_not_booked = 0
    pesos_approved_not_released = 0.0
    filter_counts = {'open': 0, 'needs_review': 0, 'parts': 0, 'services': 0, 'refund_asked': 0, 'closed': 0, 'all': len(rows)}

    items = []
    for r in rows:
        u = users.get(r.user_id)
        reason_codes = [c for c in (r.reasons or '').split(',') if c]
        reason_labels = [RETURN_REASON_LABELS.get(r.kind, {}).get(c, c) for c in reason_codes if c != 'other']

        origin, motorcycle, origin_date, claim_value, original_mechanic = None, None, None, None, None
        line_items = _return_line_items(r)
        # Built from the same batch-fetched orders/bookings/line_items already
        # in scope here — matches _return_subject_label()'s logic exactly,
        # without paying for its own fresh queries on every single row.
        if r.kind == 'product':
            names = [f"{li['name']} ×{li['quantity']}" for li in line_items]
            subject = ', '.join(names) if names else (f'ORD-{r.order_id:03d}' if r.order_id else 'an order')
        else:
            subject_booking = bookings.get(r.booking_id) if r.booking_id else None
            subject = subject_booking.service if subject_booking else 'a service'
        if r.kind == 'product' and r.order_id in orders:
            o = orders[r.order_id]
            origin = f'ORD-{r.order_id:03d}'
            motorcycle = u.motorcycle_model if u else None
            origin_date = o.created_at.strftime('%b %d, %Y')
            claim_value = sum(li['subtotal'] for li in line_items) or None
        elif r.kind == 'service' and r.booking_id in bookings:
            b = bookings[r.booking_id]
            origin = f"{b.service} — {b.date.strftime('%b %d, %Y')}"
            motorcycle = b.motorcycle_model
            origin_date = b.date.strftime('%b %d, %Y')
            claim_value = b.total_amount
            original_mechanic = b.assigned_mechanic_name

        # OPEN_RETURN_STATUSES (security.py) is narrower on purpose — it only
        # governs whether a second claim can be filed. A replacement sitting
        # at 'arrived' still needs the shop to hand it over, so it belongs in
        # this queue's own "Open" tab even though a new claim isn't blocked.
        is_open = r.status in OPEN_RETURN_STATUSES or r.status in ('replacement_shipped', 'replacement_arrived')
        is_unreviewed = r.status in ('submitted', 'under_review') and not r.awaiting_customer_info
        overdue = is_unreviewed and working_days_between(r.created_at.date(), today) >= 1

        if is_unreviewed:
            needs_review += 1
        if r.kind == 'product' and r.status == 'approved' and not r.item_returned:
            waiting_for_part += 1
        if r.status == 'approved' and r.resolution == 'refund' and r.refund_amount:
            pesos_approved_not_released += r.refund_amount
        if r.status == 'approved' and r.resolution == 'redo_service' and not r.redo_date:
            back_jobs_not_booked += 1

        if is_open: filter_counts['open'] += 1
        if is_unreviewed: filter_counts['needs_review'] += 1
        if r.kind == 'product': filter_counts['parts'] += 1
        if r.kind == 'service': filter_counts['services'] += 1
        if r.desired_outcome == 'refund': filter_counts['refund_asked'] += 1
        if r.status in ('resolved', 'denied', 'cancelled', 'completed'): filter_counts['closed'] += 1

        items.append({
            'id': r.id, 'ref': _return_ref(r),
            'customer': u.fullname if u else 'Unknown customer',
            'customer_email': u.email if u else None,
            'customer_phone': u.phone if u else None,
            'motorcycle': motorcycle,
            'kind': r.kind,
            'origin': origin,
            'origin_date': origin_date,
            'claim_value': claim_value,
            'original_mechanic': original_mechanic,
            'subject': subject,
            'line_items': line_items,
            'reasons': reason_labels,
            'other_reason_text': r.other_reason_text,
            'desired_outcome': r.desired_outcome,
            'requested_mechanic_name': r.requested_mechanic_name,
            'requested_refund_amount': r.requested_refund_amount,
            'refund_method': r.refund_method,
            'refund_receipt': r.refund_receipt,
            'cash_ready_at': r.cash_ready_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.cash_ready_at else None,
            'redo_when': _fmt_when(_redo_start(r)) if _redo_start(r) else None,
            'redo_start_iso': _redo_start(r).strftime('%Y-%m-%dT%H:%M:%S+08:00') if _redo_start(r) else None,
            'redo_quotation_status': (_redo_quotation(r).status if _redo_quotation(r) else None) if r.redo_booking_id else None,
            'redo_not_covered': ({'reason': _redo_quotation(r).not_covered_reason,
                                   'photos': [x for x in (_redo_quotation(r).not_covered_photos or '').split(',') if x]}
                                  if (r.redo_booking_id and _redo_quotation(r) and _redo_quotation(r).not_covered) else None),
            'redo_extra_total': _redo_extra_total(r) if (r.redo_booking_id and r.resolution == 'redo_service') else 0,
            'redo_done_texts': (_backjob_done_texts(r, _redo_extra_total(r)) if (r.status == 'resolved' and r.resolution == 'redo_service') else None),
            'info_requested_at': r.info_requested_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.info_requested_at else None,
            'gcash_number': r.gcash_number,
            'gcash_account_name': r.gcash_account_name,
            'photos': [p for p in (r.photos or '').split(',') if p],
            'status': r.status,
            'decision_reason': r.decision_reason,
            'resolution': r.resolution,
            'refund_amount': r.refund_amount,
            'order_id': r.order_id,
            'booking_id': r.booking_id,
            'awaiting_customer_info': bool(r.awaiting_customer_info),
            'info_request_note': r.info_request_note,
            'info_provided_at': r.info_provided_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.info_provided_at else None,
            'info_provided_text': r.info_provided_text,
            'item_returned': bool(r.item_returned),
            'item_returned_at': r.item_returned_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.item_returned_at else None,
            'redo_date': r.redo_date.isoformat() if r.redo_date else None,
            'redo_time': r.redo_time.strftime('%H:%M') if r.redo_time else None,
            'redo_mechanic_name': r.redo_mechanic_name,
            'redo_booking_id': r.redo_booking_id,
            'internal_notes': r.internal_notes,
            'overdue': overdue,
            'created_at': r.created_at.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
            'decided_at': r.decided_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.decided_at else None,
            'resolved_at': r.resolved_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.resolved_at else None,
            'replacement_shipped_at': r.replacement_shipped_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.replacement_shipped_at else None,
            'replacement_arrived_at': r.replacement_arrived_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.replacement_arrived_at else None,
            'completed_at': r.completed_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.completed_at else None,
            'delivery_address': format_delivery_address(r.delivery_name, r.delivery_mobile, r.delivery_street,
                                                          r.delivery_barangay, r.delivery_city, r.delivery_zip) if r.delivery_street else None,
            'is_pickup': bool(r.is_pickup),
            'out_of_stock': (not _replacement_stock(r)[0]) if (r.kind == 'product' and r.resolution == 'replacement' and r.status in ('approved',) and not r.item_returned) else bool(r.replacement_out_of_stock),
            'stock_left': _replacement_stock(r)[1] if r.kind == 'product' else None,
            'customer_at_shop': bool(r.customer_at_shop),
            'customer_arrived_at': r.customer_arrived_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.customer_arrived_at else None,
            'customer_back_at': r.customer_back_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.customer_back_at else None,
            'completed_by_shop': bool(r.completed_by_shop),
            'reserved': bool(r.replacement_reserved),
            'cancelled_at': r.cancelled_at.strftime('%Y-%m-%dT%H:%M:%S+08:00') if r.cancelled_at else None,
        })

    counters = {
        'needs_review': needs_review,
        'waiting_for_part': waiting_for_part,
        'pesos_approved_not_released': pesos_approved_not_released,
        'back_jobs_not_booked': back_jobs_not_booked,
    }
    return jsonify({'items': items, 'open_count': filter_counts['open'], 'counters': counters, 'filter_counts': filter_counts})


@admin_app.route('/returns/<int:rid>/decide', methods=['POST'])
@login_required
@require_admin_or_staff
def decide_return_request(rid):
    """Three ways a review ends: approve exactly what they asked for, approve
    but swap in a different remedy (claim is fair, remedy isn't), or decline.
    Actually carrying an approved remedy out is a separate step — Mark
    Resolved — since approving is a decision and fulfilling it is a
    real-world action that may take time.

    A claim with photos can't be approved until the reviewer has actually
    looked at them — enforced here too, not just as a UI courtesy, the same
    way the eligibility windows are."""
    rr = ReturnRequest.query.get_or_404(rid)
    if rr.status not in ('submitted', 'under_review'):
        return jsonify({'success': False, 'error': 'This claim has already been decided.'}), 400

    data = request.get_json() or {}
    decision = data.get('decision')
    if decision not in ('approve_as_asked', 'approve_alternate', 'decline'):
        return jsonify({'success': False, 'error': 'Unknown decision.'}), 400

    if decision in ('approve_as_asked', 'approve_alternate') and rr.photos and not data.get('photo_checked'):
        return jsonify({'success': False, 'error': 'Tick the photo check before approving — the photos need an actual look, not a rubber stamp.'}), 400

    reason = clean_str(data.get('reason', ''), max_len=500)
    if decision in ('approve_alternate', 'decline') and not reason:
        label = 'Say what changed and why — the customer sees this word for word.' if decision == 'approve_alternate' \
            else 'A reason is required — the customer sees this word for word.'
        return jsonify({'success': False, 'error': label}), 400

    ref = _return_ref(rr)
    subject = _return_subject_label(rr)

    if decision in ('approve_as_asked', 'approve_alternate'):
        if decision == 'approve_as_asked':
            resolution = rr.desired_outcome
        else:
            resolution = data.get('resolution', '')
            if resolution not in RETURN_OUTCOMES_BY_KIND[rr.kind]:
                return jsonify({'success': False, 'error': 'Pick the remedy you are offering instead — put it right, or give the money back, never both.'}), 400
            if resolution == rr.desired_outcome:
                return jsonify({'success': False, 'error': "That's the same remedy they asked for — use Approve As Asked instead."}), 400

        rr.status = 'approved'
        rr.resolution = resolution
        rr.awaiting_customer_info = False
        if resolution == 'refund':
            suggested = rr.requested_refund_amount or 0.0
            rr.refund_amount = clean_float(data.get('refund_amount', ''), default=suggested, min_val=0)
        rr.decision_reason = reason or ('Approved as requested.' if decision == 'approve_as_asked' else '')
        rr.decided_at = ph_now()

        alternate = decision == 'approve_alternate'
        reason_line = f' {reason}' if reason else ''

        # Every product remedy needs the part back first — replacement or
        # refund alike — so approving one always opens that sub-state.
        if rr.kind == 'product':
            rr.item_returned = False
            if resolution == 'refund':
                title = (f'Approved — ₱{rr.refund_amount:,.2f} refund instead of a replacement' if alternate
                         else f'Approved — return the part to get ₱{rr.refund_amount:,.2f} back')
                body = ((f'Your {subject} claim ({ref}) was approved, but instead of a replacement we\'re giving a '
                         f'refund:{reason_line} Return the item to the shop so we can release the refund.') if alternate else
                        (f'Your {subject} claim ({ref}) was approved: refund. Return the item to the shop so we '
                         f'can release the refund. Reason: {reason}.'))
            else:
                title = 'Approved — bring your item to the shop'
                _shop_line = ('Bring the item to MotoTyres North Caloocan, Saranay Rd, Barangay 171 Bagumbong, Caloocan City '
                              '(Monday – Sunday · 7:00 AM – 6:30 PM). ')
                _how = ("We'll check it in front of you and replace it right away." if _replacement_stock(rr)[0] else
                        "We'll check it in front of you. The replacement is out of stock right now, so we'll notify you when it's ready to pick up.")
                body = ((f'Your return for {subject} ({ref}) is approved — we\'re giving a replacement instead of a refund.{reason_line} ') if alternate
                        else f'Your return for {subject} ({ref}) is approved. ') + _shop_line + _how
        else:
            if resolution == 'refund':
                title = f'Approved — ₱{rr.refund_amount:,.2f} refund instead of a back job' if alternate else f'Approved — ₱{rr.refund_amount:,.2f} refund'
                body = ((f'Your {subject} claim ({ref}) was approved, but instead of redoing the work we\'re '
                         f"refunding:{reason_line} We'll release it shortly.") if alternate else
                        (f"Your {subject} claim ({ref}) was approved: refund. Reason: {reason}. We'll release it "
                         f'shortly.'))
            else:
                title = 'Approved — back job instead of a refund' if alternate else f'Approved — back job for {subject}'
                body = ((f'Your {subject} claim ({ref}) was approved, but instead of a refund we will redo the '
                         f"work at no charge:{reason_line} We'll be in touch to schedule it.") if alternate else
                        (f'Your {subject} claim ({ref}) was approved: we will redo the work at no charge. '
                         f"Reason: {reason}. We'll be in touch to schedule it."))
        db.session.commit()
    else:  # decline
        rr.status = 'denied'
        rr.decision_reason = reason
        rr.decided_at = ph_now()
        rr.awaiting_customer_info = False
        db.session.commit()

        title = f'Not approved — {subject}'
        request_noun = 'return request' if rr.kind == 'product' else ('back job request' if rr.desired_outcome == 'redo_service' else 'refund request')
        body = (f'Your {request_noun} ({ref}) about {subject} was not approved. Reason: {reason}. '
                f'Reply to this if you have more information or evidence to add.')

    emailed = _notify_return_customer(rr, title, body, priority=True)
    return jsonify({'success': True, 'emailed': emailed, 'message': f'{rr.status.title()} — customer notified.'})


@admin_app.route('/returns/<int:rid>/request-info', methods=['POST'])
@login_required
@require_admin_or_staff
def request_return_info(rid):
    """Moves a claim into 'needs the customer's help before we can decide' —
    the note becomes the notification title verbatim, since it's the one
    thing the customer needs to see from a lock screen. Stays pinned in
    their bell until they actually reply, not merely opened."""
    rr = ReturnRequest.query.get_or_404(rid)
    if rr.status not in ('submitted', 'under_review'):
        return jsonify({'success': False, 'error': 'This claim is no longer open for review.'}), 400
    note = clean_str((request.get_json() or {}).get('note', ''), max_len=500)
    if not note:
        return jsonify({'success': False, 'error': 'Say what you need from the customer.'}), 400

    rr.status = 'under_review'
    rr.awaiting_customer_info = True
    rr.info_request_note = note
    rr.info_requested_at = ph_now()
    db.session.commit()

    ref = _return_ref(rr)
    body = f'About your {_return_subject_label(rr)} claim ({ref}): {note}'
    # The title is the lock-screen line (the bell's title column holds 120
    # characters); the full message, up to 500, is always in the body.
    title = note if len(note) <= 120 else note[:117].rstrip() + '…'
    emailed = _notify_return_customer(rr, title, body, priority=True)
    return jsonify({'success': True, 'emailed': emailed, 'message': 'Sent — customer notified.'})


@admin_app.route('/returns/<int:rid>/notes', methods=['POST'])
@login_required
@require_admin_or_staff
def save_return_notes(rid):
    """The shop's own working notes on a claim — never surfaced to the
    customer, never part of a notification. Separate from decision_reason,
    which the customer does see."""
    rr = ReturnRequest.query.get_or_404(rid)
    notes = clean_str((request.get_json() or {}).get('notes', ''), max_len=2000)
    rr.internal_notes = notes or None
    db.session.commit()
    return jsonify({'success': True})


def _replacement_items(rr):
    """[(Product, quantity)] for the lines of a part claim."""
    out = []
    for row in ReturnRequestItem.query.filter_by(return_request_id=rr.id).all():
        oi = OrderItem.query.get(row.order_item_id)
        prod = Product.query.get(oi.product_id) if oi else None
        if prod:
            out.append((prod, row.quantity))
    return out


def _replacement_stock(rr):
    """(in_stock, left): whether every item has enough on the shelf for the quantity being replaced,
    and the smallest number left among them."""
    items = _replacement_items(rr)
    if not items:
        return True, 0
    return all((p.stock or 0) >= q for p, q in items), min((p.stock or 0) for p, q in items)


def _set_aside_replacement(rr):
    """Takes the replacement out of stock for this claim."""
    for p, q in _replacement_items(rr):
        p.stock = max(0, (p.stock or 0) - q)
    rr.replacement_reserved = True


def _release_replacement(rr):
    for p, q in _replacement_items(rr):
        p.stock = (p.stock or 0) + q
    rr.replacement_reserved = False


@admin_app.route('/returns/<int:rid>/customer-arrived', methods=['POST'])
@login_required
@require_admin_or_staff
def mark_customer_arrived(rid):
    """The customer (or the person they sent) is at the counter with the request number. Used twice for a
    replacement: the first visit to hand in the item, and again to come back for a replacement that was set aside."""
    rr = ReturnRequest.query.get_or_404(rid)
    if rr.kind != 'product' or rr.resolution != 'replacement':
        return jsonify({'success': False, 'error': 'Only an in-store replacement uses this step.'}), 400
    if rr.status not in ('approved', 'replacement_shipped') or rr.customer_at_shop:
        return jsonify({'success': False, 'error': 'This claim is not waiting for the customer to arrive.'}), 400
    if rr.status == 'approved' and rr.item_returned:
        return jsonify({'success': False, 'error': 'The shop already has the item — mark the replacement ready for pickup once the stock is in.'}), 400
    back_for_pickup = rr.status == 'replacement_shipped'
    rr.customer_at_shop = True
    if back_for_pickup:
        rr.customer_back_at = ph_now()
    else:
        rr.customer_arrived_at = ph_now()
    # Decided here from the live shelf, so a part that was restocked since the request was filed counts as in stock.
    if not back_for_pickup:
        rr.replacement_out_of_stock = not _replacement_stock(rr)[0]
    db.session.commit()
    ref = _return_ref(rr)
    subject = _return_subject_label(rr)
    if back_for_pickup:
        title, body = "You've arrived for your replacement", f'We see you at MotoTyres North Caloocan for {ref}. We\'re getting your replacement ready now.'
    else:
        title, body = "You've arrived at the shop", f'We see you at MotoTyres North Caloocan for {ref}. Our staff is checking the {subject} with you now.'
    emailed = _notify_return_customer(rr, title, body, priority=False, status='at_shop')
    return jsonify({'success': True, 'emailed': emailed, 'message': 'Customer marked as arrived — notified.'})


@admin_app.route('/returns/<int:rid>/replacement-given', methods=['POST'])
@login_required
@require_admin_or_staff
def mark_replacement_given(rid):
    """The replacement was handed over at the counter. The customer is then asked to confirm they got it
    (or the shop completes it for them)."""
    rr = ReturnRequest.query.get_or_404(rid)
    if rr.kind != 'product' or rr.resolution != 'replacement' or not rr.customer_at_shop:
        return jsonify({'success': False, 'error': 'Press Customer Arrived first.'}), 400
    if rr.status == 'approved':                       # first visit, replacement on the shelf
        if not _replacement_stock(rr)[0]:
            return jsonify({'success': False, 'error': 'This item is out of stock — press Mark Part Received and keep the item instead.'}), 400
        _set_aside_replacement(rr)
        rr.item_returned = True
        rr.item_returned_at = ph_now()
    elif rr.status != 'replacement_shipped':          # back for pickup of the one set aside
        return jsonify({'success': False, 'error': 'This claim is not at the counter for a replacement.'}), 400
    rr.status = 'replacement_arrived'
    rr.replacement_arrived_at = ph_now()
    rr.customer_at_shop = False
    db.session.commit()
    ref = _return_ref(rr)
    subject = _return_subject_label(rr)
    emailed = _notify_return_customer(
        rr, "Here's your replacement",
        f'We gave you a new {subject} at the shop ({ref}). Please confirm in My Requests that you received it.',
        priority=True, status='replacement_given')
    return jsonify({'success': True, 'emailed': emailed, 'message': 'Replacement given — customer asked to confirm.'})


@admin_app.route('/returns/<int:rid>/failed-check', methods=['POST'])
@login_required
@require_admin_or_staff
def mark_item_failed_check(rid):
    """The item didn't pass the check at the counter — the claim is closed, with a reason the customer is shown."""
    rr = ReturnRequest.query.get_or_404(rid)
    reason = clean_str((request.get_json(silent=True) or {}).get('reason', ''), max_len=500)
    if rr.kind != 'product' or rr.resolution != 'replacement' or not rr.customer_at_shop:
        return jsonify({'success': False, 'error': 'Press Customer Arrived first.'}), 400
    if not reason:
        return jsonify({'success': False, 'error': 'Type the reason first.'}), 400
    if rr.replacement_reserved:
        _release_replacement(rr)
    rr.status = 'resolved'
    rr.resolved_at = ph_now()
    rr.decision_reason = reason
    rr.customer_at_shop = False
    db.session.commit()
    ref = _return_ref(rr)
    subject = _return_subject_label(rr)
    emailed = _notify_return_customer(
        rr, "We can't replace this item",
        f'We checked the {subject} with you at the shop ({ref}) and it didn\'t pass the check, so we can\'t replace it. {reason} The item stays with you.',
        priority=False, status='closed')
    return jsonify({'success': True, 'emailed': emailed, 'message': 'Claim closed — customer told why.'})


@admin_app.route('/returns/<int:rid>/item-received', methods=['POST'])
@login_required
@require_admin_or_staff
def mark_return_item_received(rid):
    """The physical part is back at the shop — clears the way for the release
    action. This is its own state change (Waiting for your item -> Approved),
    so the customer hears about it — nothing moves silently."""
    rr = ReturnRequest.query.get_or_404(rid)
    if rr.kind != 'product' or rr.status != 'approved':
        return jsonify({'success': False, 'error': 'Only an approved product claim can have its item marked received.'}), 400
    if rr.resolution == 'replacement':
        if not rr.customer_at_shop:
            return jsonify({'success': False, 'error': 'Press Customer Arrived first.'}), 400
        if _replacement_stock(rr)[0]:
            return jsonify({'success': False, 'error': 'It is in stock — press Replacement Given and hand it over now.'}), 400
        rr.replacement_out_of_stock = True
        rr.customer_at_shop = False
    rr.item_returned = True
    rr.item_returned_at = ph_now()
    db.session.commit()

    ref = _return_ref(rr)
    subject = _return_subject_label(rr)
    if rr.resolution == 'refund':
        title = 'We received your item — refund next'
        body = f'We received the {subject} you sent back ({ref}). Your ₱{rr.refund_amount:,.2f} refund is next.'
    else:
        title = 'We have your item — waiting for stock'
        body = f'We checked and kept the {subject} you brought in ({ref}). The replacement is out of stock, so we\'ll notify you here and by email when it\'s ready to pick up at the shop.'
    emailed = _notify_return_customer(rr, title, body, priority=False, status='waiting_for_stock' if rr.resolution == 'replacement' else None)
    return jsonify({'success': True, 'emailed': emailed, 'message': 'Item marked received — customer notified.'})


def _return_original_service(rr):
    """The original booking's duration and service name — a back job runs as
    long as the job it's redoing, not a generic default, and the calendar
    entry should read like the job it actually is."""
    b = Booking.query.get(rr.booking_id) if rr.booking_id else None
    if b:
        return b.duration_minutes or DEFAULT_DURATION_MIN, b.service
    return DEFAULT_DURATION_MIN, _return_subject_label(rr)


BACKJOB_PREFIX = 'Back job — '


def _fmt_when(dt):
    """'Tue, Oct 13, 2026, 10:30 AM' — the one format back-job dates use everywhere."""
    return dt.strftime('%a, %b %d, %Y, ') + dt.strftime('%I:%M %p').lstrip('0')


def _redo_start(rr):
    return datetime.combine(rr.redo_date, rr.redo_time) if (rr.redo_date and rr.redo_time) else None


def _redo_quotation(rr):
    """Latest Inspection & Estimation quotation on the back job's own booking, if any."""
    if not rr.redo_booking_id:
        return None
    return Quotation.query.filter_by(booking_id=rr.redo_booking_id).order_by(Quotation.created_at.desc()).first()


def _redo_extra_total(rr):
    """What the customer owes for the back job's booking: the back job itself
    is a free line, so on approval it's only the extra work, on decline only
    anything already billable (materials). A not-covered finding charges the
    redo line at its normal price instead, which is already on the quotation."""
    q = _redo_quotation(rr)
    if not q:
        return 0.0
    breakdown = _quotation_breakdown(q.items)
    # Declined: only the redo line itself (₱0 when covered) — materials went with the extra work.
    return float(breakdown['approve_total'] if q.status == 'approved' else breakdown['booked'])


def _backjob_done_texts(rr, extra_total=0.0):
    ref = _return_ref(rr)
    subject = _return_subject_label(rr)
    mechanic = rr.redo_mechanic_name or 'The mechanic'
    customer = User.query.get(rr.user_id)
    first = ((customer.fullname if customer else '') or 'there').split(' ')[0]
    title = 'Your back job is done'
    body = f'Your back job for {subject} ({ref}) is done. Your claim is now closed.'
    if extra_total > 0:
        body += f' The extra work you approved (₱{extra_total:,.2f}) is on your invoice.'
        done = (f'{mechanic} has finished redoing your {subject}. The back job itself is free; the extra work you approved '
                f'(₱{extra_total:,.2f}) is on your invoice. Your claim {ref} is now closed. If the problem comes back, '
                f'you can file a new claim from Returns & Warranty.')
    else:
        done = (f'{mechanic} has finished redoing your {subject} at no charge. Your claim {ref} is now closed. '
                f'If the problem comes back, you can file a new claim from Returns & Warranty.')
    return {'title': title, 'body': body, 'email_subject': f'Your back job for {ref} is done',
            'email_body': done, 'first_name': first}


def _redo_gate(rr):
    if rr.kind != 'service' or rr.resolution != 'redo_service' or rr.status != 'approved':
        return 'Only an approved back-job claim can be scheduled.'
    return None


@admin_app.route('/returns/<int:rid>/redo-availability')
@login_required
@require_admin_or_staff
def return_redo_availability(rid):
    """The back-job scheduler's slot grid — same shop-queue rules as every
    other booking path (open 8:00 AM, 6:30 PM finish cutoff, the lunch pause
    baked into every finish time, no collision with the existing queue),
    sized to the ORIGINAL service's duration, not a guess."""
    rr = ReturnRequest.query.get_or_404(rid)
    err = _redo_gate(rr)
    if err:
        return jsonify({'error': err}), 400
    date_str = request.args.get('date', '').strip()
    try:
        the_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400

    duration, service_name = _return_original_service(rr)
    q = Booking.query.filter(Booking.date == the_date, Booking.status != 'cancelled')
    if rr.redo_booking_id:
        q = q.filter(Booking.id != rr.redo_booking_id)   # rescheduling: its own current slot is free to reuse
    intervals = _gather_intervals(q) + _gather_blocked_intervals(the_date)
    now_minutes = None
    if the_date == ph_now().date():
        now = ph_now()
        now_minutes = now.hour * 60 + now.minute

    _, _, on_duty = get_on_duty_mechanics(the_date)
    slots = slot_statuses(duration, intervals, now_minutes, on_duty_count=len(on_duty))
    return jsonify({
        'duration_minutes': duration,
        'service_name': service_name,
        'preferred_mechanic_name': rr.requested_mechanic_name,
        'slots': [{
            'time': minutes_to_hhmm(s['start']), 'end_time': minutes_to_hhmm(s['end']),
            'label': minutes_to_ampm(s['start']), 'end_label': minutes_to_ampm(s['end']),
            'available': s['available'], 'reason': s['reason'],
        } for s in slots],
    })


@admin_app.route('/returns/<int:rid>/redo-mechanics')
@login_required
@require_admin_or_staff
def return_redo_mechanics(rid):
    """On-duty mechanics for the picked slot, busy ones flagged rather than
    hidden — same convention as the customer-side picker."""
    rr = ReturnRequest.query.get_or_404(rid)
    err = _redo_gate(rr)
    if err:
        return jsonify({'error': err}), 400
    date_str = request.args.get('date', '').strip()
    time_str = request.args.get('time', '').strip()
    try:
        the_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        start_min = hhmm_to_minutes(time_str)
    except (ValueError, IndexError):
        return jsonify({'error': 'Invalid date or time'}), 400

    duration, _ = _return_original_service(rr)
    end_min = compute_finish_minutes(start_min, duration)
    _, _, on_duty = get_on_duty_mechanics(the_date)

    busy_names = set()
    for b in Booking.query.filter(Booking.assigned_mechanic_name.isnot(None), Booking.date == the_date,
                                   Booking.status != 'cancelled').all():
        if rr.redo_booking_id and b.id == rr.redo_booking_id:
            continue
        b_start = b.time.hour * 60 + b.time.minute
        b_end = compute_finish_minutes(b_start, b.duration_minutes or DEFAULT_DURATION_MIN)
        if mechanic_overlaps(start_min, end_min, b_start, b_end):
            busy_names.add(b.assigned_mechanic_name)

    return jsonify({
        'preferred_mechanic_name': rr.requested_mechanic_name,
        'mechanics': [{'id': m.id, 'name': m.name, 'specialization': m.specialization, 'busy': m.name in busy_names}
                      for m in on_duty],
    })


@admin_app.route('/returns/<int:rid>/schedule-redo', methods=['POST'])
@login_required
@require_admin_or_staff
def schedule_return_redo(rid):
    """Books the back job as a real, zero-charge appointment on the shop's
    own calendar — the same validated write path a reschedule uses, checked
    again right here (the slot grid the admin saw may be stale by the time
    they click Book, e.g. another admin took the mechanic first) rather than
    trusting what was true when the grid was drawn. Approving the remedy and
    scheduling it are two separate state changes, each raising exactly one
    notification, not one bundled into three."""
    rr = ReturnRequest.query.get_or_404(rid)
    err = _redo_gate(rr)
    if err:
        return jsonify({'success': False, 'error': err}), 400
    data = request.get_json() or {}
    try:
        redo_date = datetime.strptime(clean_str(data.get('date', ''), max_len=10), '%Y-%m-%d').date()
        redo_time = datetime.strptime(clean_str(data.get('time', ''), max_len=5), '%H:%M').time()
    except ValueError:
        return jsonify({'success': False, 'error': 'Pick a valid date and time.'}), 400
    mechanic_name = clean_str(data.get('mechanic_name', ''), max_len=100)
    if not mechanic_name:
        return jsonify({'success': False, 'error': 'Name who is doing the redo.'}), 400
    if datetime.combine(redo_date, redo_time) <= ph_now():
        return jsonify({'success': False, 'error': 'Cannot book a past or current time.'}), 400

    duration, service_name = _return_original_service(rr)
    start_min = redo_time.hour * 60 + redo_time.minute

    # The moment-of-booking check — not just what the grid showed when it was
    # drawn. If the mechanic (or the slot) was taken while the admin was
    # deciding, this is exactly where that shows up.
    ok, error = _check_admin_booking_request(redo_date, start_min, duration, mechanic_name=mechanic_name,
                                              exclude_id=rr.redo_booking_id)
    if not ok:
        return jsonify({'success': False, 'error': f'{error} Pick again.'}), 409

    mechanic = Mechanic.query.filter_by(name=mechanic_name).first()
    customer = User.query.get(rr.user_id)
    existing_booking = Booking.query.get(rr.redo_booking_id) if rr.redo_booking_id else None
    if existing_booking and existing_booking.status in ('confirmed', 'inspection', 'in_progress', 'inprogress'):
        # Reschedule: move the same calendar entry rather than booking a second one.
        existing_booking.date, existing_booking.time = redo_date, redo_time
        existing_booking.end_time = compute_finish_time(redo_time, duration)
        existing_booking.assigned_mechanic_name = mechanic_name
        existing_booking.assigned_mechanic_specialization = mechanic.specialization if mechanic else None
        existing_booking.was_rescheduled = True
        rr.redo_date, rr.redo_time, rr.redo_mechanic_name = redo_date, redo_time, mechanic_name
        db.session.commit()
        when = _fmt_when(datetime.combine(redo_date, redo_time))
        ref = _return_ref(rr)
        title = f'Your back job was rescheduled to {when}'
        body = (f'Your {_return_subject_label(rr)} back job ({ref}) now takes place on {when} with {mechanic_name} — '
                f'no charge for the back job itself.')
        emailed = _notify_return_customer(rr, title, body, priority=True)
        return jsonify({'success': True, 'emailed': emailed, 'message': 'Rescheduled — customer notified.'})

    booking = Booking(
        user_id=rr.user_id,
        service=f'Back job — {service_name}',
        date=redo_date, time=redo_time,
        end_time=compute_finish_time(redo_time, duration),
        duration_minutes=duration,
        status='confirmed',
        total_amount=0,
        motorcycle_model=customer.motorcycle_model if customer else None,
        assigned_mechanic_name=mechanic_name,
        assigned_mechanic_specialization=mechanic.specialization if mechanic else None,
        preferred_mechanic_name=rr.requested_mechanic_name,
    )
    db.session.add(booking)
    db.session.flush()

    rr.redo_date = redo_date
    rr.redo_time = redo_time
    rr.redo_mechanic_name = mechanic_name
    rr.redo_booking_id = booking.id
    db.session.commit()

    ref = _return_ref(rr)
    date_label = redo_date.strftime('%A, %b %d')
    time_label = redo_time.strftime('%I:%M %p').lstrip('0')
    title = f'Back job booked for {date_label}, {time_label} with {mechanic_name}'
    body = (f'Your {_return_subject_label(rr)} back job ({ref}) is booked for {date_label} at {time_label} with '
            f'{mechanic_name} — no charge, there is nothing to pay.')
    emailed = _notify_return_customer(rr, title, body, priority=True)
    return jsonify({'success': True, 'emailed': emailed, 'message': 'Scheduled — customer notified.'})


RETURN_RECEIPT_MAX_BYTES = 5 * 1024 * 1024


def _save_refund_receipt(file, rid):
    """Cloudinary when configured, else static/return_evidence/ (the same shared
    folder the customer site already serves return photos from). Returns the
    value to store on ReturnRequest.refund_receipt."""
    cloud_url = upload_image(file, 'return_evidence')
    if cloud_url:
        return cloud_url
    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = f'refund_{rid}_{uuid.uuid4().hex}.{ext}'
    folder = os.path.join(admin_app.root_path, 'static', 'return_evidence')
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, filename))
    return filename


def _fmt_gcash(number):
    n = ''.join(ch for ch in (number or '') if ch.isdigit())
    return f'{n[:4]} {n[4:7]} {n[7:]}' if len(n) == 11 else (number or '')


@admin_app.route('/returns/<int:rid>/release-gcash', methods=['POST'])
@login_required
@require_admin_or_staff
def release_gcash_refund(rid):
    """The shop has sent the money from its own GCash and uploads a screenshot
    of that receipt as proof. This is the only way a GCash refund closes: the
    claim becomes Refunded (which also puts it in Transaction History as a
    refund), and the customer gets a bell notification that opens their claim
    plus an email that only says it was sent and to view the receipt."""
    rr = ReturnRequest.query.get_or_404(rid)
    if rr.status != 'approved' or rr.resolution != 'refund':
        return jsonify({'success': False, 'error': 'Only an approved refund can be released.'}), 400
    if rr.refund_method != 'gcash':
        return jsonify({'success': False, 'error': 'This claim is not a GCash refund.'}), 400
    if rr.kind == 'product' and not rr.item_returned:
        return jsonify({'success': False, 'error': "Mark the item received first — the part hasn't come back yet."}), 400
    if not (rr.refund_amount and rr.refund_amount > 0):
        return jsonify({'success': False, 'error': 'This claim has no approved refund amount.'}), 400

    f = request.files.get('receipt')
    if not f or not f.filename or not allowed_file(f.filename) or not (f.mimetype or '').startswith('image/'):
        return jsonify({'success': False, 'error': 'Upload a screenshot of the GCash receipt (an image file).'}), 400
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    if size > RETURN_RECEIPT_MAX_BYTES:
        return jsonify({'success': False, 'error': 'That image is over 5MB — upload a smaller screenshot.'}), 400

    rr.refund_receipt = _save_refund_receipt(f, rr.id)
    rr.status = 'resolved'
    rr.resolved_at = ph_now()
    db.session.commit()

    ref = _return_ref(rr)
    subject = _return_subject_label(rr)
    amount = f'₱{rr.refund_amount:,.2f}'
    noun = 'return' if rr.kind == 'product' else 'claim'
    title = f'Your {amount} refund was sent'
    body = f'We sent {amount} to your GCash ({_fmt_gcash(rr.gcash_number)}) for your {subject} {noun} ({ref}).'
    email_body = (f'Your refund of {amount} for {subject} ({ref}) was sent to your GCash. '
                  f'Please view the receipt in your MotoTyres portal, under Returns &amp; Warranty.')
    emailed = _notify_return_customer(rr, title, body, priority=False, email_body=email_body, status='refunded')
    return jsonify({'success': True, 'emailed': emailed, 'message': 'Refund marked sent — customer notified.'})


# ── Cash refunds: two steps at the shop counter ──
def _cash_refund_guard(rr):
    """(error response | None) shared by both cash steps."""
    if rr.status != 'approved' or rr.resolution != 'refund':
        return jsonify({'success': False, 'error': 'Only an approved refund can be handled here.'}), 400
    if rr.refund_method == 'gcash':
        return jsonify({'success': False, 'error': 'This is a GCash refund — release it with the receipt screenshot.'}), 400
    if rr.kind == 'product' and not rr.item_returned:
        return jsonify({'success': False, 'error': "Mark the item received first — the part hasn't come back yet."}), 400
    if not (rr.refund_amount and rr.refund_amount > 0):
        return jsonify({'success': False, 'error': 'This claim has no approved refund amount.'}), 400
    return None


@admin_app.route('/returns/<int:rid>/cash-ready', methods=['POST'])
@login_required
@require_admin_or_staff
def cash_refund_ready(rid):
    """Step 1 — tell the customer their cash refund is waiting at the counter.
    The claim stays approved (badge: Ready to Claim) until the cash is handed over."""
    rr = ReturnRequest.query.get_or_404(rid)
    err = _cash_refund_guard(rr)
    if err:
        return err
    if rr.cash_ready_at:
        return jsonify({'success': False, 'error': 'The customer was already told this is ready to claim.'}), 400
    rr.cash_ready_at = ph_now()
    db.session.commit()

    ref = _return_ref(rr)
    subject = _return_subject_label(rr)
    amount = f'₱{rr.refund_amount:,.2f}'
    noun = 'return' if rr.kind == 'product' else 'claim'
    title = f'Your {amount} refund is ready to claim'
    body = (f'Your {amount} cash refund for your {subject} {noun} ({ref}) is ready. Claim it at the MotoTyres North '
            f'Caloocan shop counter, 8:00 AM – 6:30 PM, and mention claim number {ref}.')
    email_body = (f'your {amount} refund for {subject} is ready. You can claim it in cash at the MotoTyres North '
                  f'Caloocan shop counter, 8:00 AM – 6:30 PM. Just mention your claim number {ref} at the counter.')
    customer = User.query.get(rr.user_id)
    first = ((customer.fullname if customer else '') or 'there').split(' ')[0]
    emailed = _notify_return_customer(rr, title, body, priority=True, status='ready_to_claim',
                                      email_body=email_body[0].upper() + email_body[1:],
                                      email_subject=f'Your refund for {ref} is ready to claim', first_name_only=True)
    return jsonify({'success': True, 'emailed': emailed, 'message': f'{first} has been told their refund is ready to claim.'})


@admin_app.route('/returns/<int:rid>/cash-given', methods=['POST'])
@login_required
@require_admin_or_staff
def cash_refund_given(rid):
    """Step 2 — the cash was counted and handed over. Closes the claim as
    Refunded (which also records it as a refund in Transaction History)."""
    rr = ReturnRequest.query.get_or_404(rid)
    err = _cash_refund_guard(rr)
    if err:
        return err
    if not rr.cash_ready_at:
        return jsonify({'success': False, 'error': 'Tell the customer it is ready to claim first.'}), 400
    if not (request.get_json(silent=True) or {}).get('counted'):
        return jsonify({'success': False, 'error': 'Confirm that you counted the amount and handed it to the customer.'}), 400

    rr.status = 'resolved'
    rr.resolved_at = ph_now()
    rr.refund_given_by = current_user.fullname
    db.session.commit()

    ref = _return_ref(rr)
    amount = f'₱{rr.refund_amount:,.2f}'
    when = rr.resolved_at.strftime('%b %d, %Y, %I:%M %p')
    title = f'Your {amount} refund is complete'
    body = f'You received your {amount} refund in cash at the shop counter on {when}. Your claim {ref} is now closed.'
    email_body = (f'You received your {amount} refund in cash at the shop counter on {when}. Your return claim {ref} '
                  f'is now closed. Thank you for choosing MotoTyres North Caloocan.')
    emailed = _notify_return_customer(rr, title, body, priority=False, status='refunded', email_body=email_body,
                                      email_subject=f'Your refund for {ref} is complete', first_name_only=True)
    return jsonify({'success': True, 'emailed': emailed, 'message': 'Cash refund recorded — claim closed.'})


@admin_app.route('/returns/<int:rid>/resolve', methods=['POST'])
@login_required
@require_admin_or_staff
def resolve_return_request(rid):
    """Marks an approved claim's remedy as actually carried out — the refund
    was issued, the back job redone, or (for a replacement) the part shipped
    out. A product claim can't resolve until its item is marked received; a
    back job can't resolve until it's been scheduled — carrying out a remedy
    that was never actually delivered isn't something this button can paper
    over. A replacement only reaches "on the way" here — see mark_return_arrived
    for the delivery step, and complete_return_request for the finish line."""
    rr = ReturnRequest.query.get_or_404(rid)
    if rr.status != 'approved':
        return jsonify({'success': False, 'error': 'Only an approved claim can be marked resolved.'}), 400
    if rr.kind == 'product' and not rr.item_returned:
        return jsonify({'success': False, 'error': "Mark the item received first — the part hasn't come back yet."}), 400
    if rr.resolution == 'replacement' and rr.kind == 'product':
        # Mark Ready for Pickup — the part was out of stock and is now in: set one aside for this claim.
        if not rr.item_returned or rr.replacement_reserved:
            return jsonify({'success': False, 'error': 'This replacement is not waiting for stock.'}), 400
        if not _replacement_stock(rr)[0]:
            return jsonify({'success': False, 'error': 'Not enough stock yet — set one aside once it arrives.'}), 400
        _set_aside_replacement(rr)
        rr.status = 'replacement_shipped'
        rr.replacement_shipped_at = ph_now()
        db.session.commit()
        ref = _return_ref(rr)
        subject = _return_subject_label(rr)
        emailed = _notify_return_customer(
            rr, 'Your replacement is ready for pickup',
            f'The replacement for {subject} ({ref}) is ready to pick up at the shop (Monday – Sunday, 7:00 AM – 6:30 PM). Just mention your request number {ref}.',
            priority=False, status='ready_for_pickup')
        return jsonify({'success': True, 'emailed': emailed, 'message': 'Marked ready for pickup — customer notified.'})
    if rr.resolution == 'refund':
        return jsonify({'success': False, 'error': 'Refunds are released from their own buttons — Release (GCash) or Ready to Claim / Mark Cash Given (cash).'}), 400
    if rr.kind == 'service' and rr.resolution == 'redo_service' and not rr.redo_date:
        return jsonify({'success': False, 'error': 'Schedule the back job first.'}), 400
    if rr.kind == 'service' and rr.resolution == 'redo_service':
        # Checked here, not just on the button: the clock the button used can be wrong or stale.
        start = _redo_start(rr)
        if start and ph_now() < start:
            return jsonify({'success': False, 'error': f'The back job starts {_fmt_when(start)} — it can only be marked done once it starts.'}), 400
        quote = _redo_quotation(rr)
        if quote and quote.status == 'awaiting_customer':
            return jsonify({'success': False, 'error': "A quotation is still waiting for the customer's answer — wait for their reply first."}), 400
    if rr.resolution == 'replacement' and not rr.is_pickup and not rr.delivery_street:
        return jsonify({'success': False, 'error': 'This claim has no delivery address on file — contact the customer directly.'}), 400

    # A replacement isn't done once it's sent — it isn't real until the
    # customer actually has it in hand, so this step only gets it as far as
    # "on the way." Refund and redo-service have no such in-between:
    # releasing the money or redoing the job IS the whole remedy.
    if rr.resolution == 'replacement':
        rr.status = 'replacement_shipped'
        rr.replacement_shipped_at = ph_now()
    else:
        rr.status = 'resolved'
        rr.resolved_at = ph_now()

    # The back job is a real appointment on the shop's calendar — closing the
    # claim closes that booking too, so the mechanic's day reflects it.
    redo_extra = 0.0
    if rr.kind == 'service' and rr.resolution == 'redo_service' and rr.redo_booking_id:
        redo_extra = _redo_extra_total(rr)
        b = Booking.query.get(rr.redo_booking_id)
        if b and redo_extra <= 0:
            # Nothing to pay for the free back job → close it with no invoice.
            # (With approved extra work the invoice flow below completes it instead.)
            b.status = 'completed'
            b.completed_at = ph_now()
            b.total_amount = 0
            b.invoice_paid = True

    db.session.commit()

    if redo_extra > 0 and rr.redo_booking_id:
        complete_booking_invoice(rr.redo_booking_id)   # sends the invoice for just the approved extra

    ref = _return_ref(rr)
    subject = _return_subject_label(rr)
    if rr.resolution == 'refund':
        title = f'Your refund of ₱{rr.refund_amount:,.2f} has been released'
        body = f'Your refund for {subject} ({ref}) — ₱{rr.refund_amount:,.2f} — has been released to your original payment method.'
    elif rr.resolution == 'replacement' and rr.is_pickup:
        title = 'Your replacement is ready for pickup'
        body = f'The replacement for {subject} ({ref}) is ready to pick up at the shop (Monday – Sunday, 7:00 AM – 6:30 PM). Just mention your request number {ref}.'
    elif rr.resolution == 'replacement':
        addr = format_delivery_address(rr.delivery_name, rr.delivery_mobile, rr.delivery_street,
                                        rr.delivery_barangay, rr.delivery_city, rr.delivery_zip)
        title = 'Your replacement is on the way'
        body = f'The replacement for {subject} ({ref}) is on its way. It will arrive at {addr}.'
    else:
        done = _backjob_done_texts(rr, redo_extra)
        title, body = done['title'], done['body']
    if rr.resolution == 'redo_service':
        emailed = _notify_return_customer(rr, title, body, priority=False, email_body=done['email_body'],
                                          email_subject=done['email_subject'], first_name_only=True)
    else:
        emailed = _notify_return_customer(rr, title, body, priority=False)
    if rr.resolution == 'replacement':
        msg = 'Marked ready for pickup — customer notified.' if rr.is_pickup else 'Marked on the way — customer notified.'
    else:
        msg = 'Marked resolved — customer notified.'
    return jsonify({'success': True, 'emailed': emailed, 'message': msg})


@admin_app.route('/returns/<int:rid>/complete', methods=['POST'])
@login_required
@require_admin_or_staff
def complete_return_request(rid):
    """Staff-side fallback for the same finish line the customer's own
    'I received it' button reaches — a walk-in customer picking the part up
    in person may never touch the app, so the shop needs its own way to
    close this out instead of leaving it stuck at 'arrived' forever."""
    rr = ReturnRequest.query.get_or_404(rid)
    if rr.resolution != 'replacement' or rr.status != 'replacement_arrived':
        return jsonify({'success': False, 'error': 'This claim is not waiting on a pickup.'}), 400
    rr.status = 'completed'
    rr.completed_at = ph_now()
    rr.completed_by_shop = True
    db.session.commit()
    ref = _return_ref(rr)
    subject = _return_subject_label(rr)
    emailed = _notify_return_customer(
        rr, 'Replacement handed over',
        f'The replacement for {subject} ({ref}) has been handed over. Thanks for choosing MotoTyres North Caloocan!',
        priority=False,
    )
    return jsonify({'success': True, 'emailed': emailed, 'message': 'Marked completed — customer notified.'})


@admin_app.route('/api/all-bookings')
@login_required
@require_admin_or_staff
def api_all_bookings():
    """The flat list view: every booking, one row each, for the sortable /
    filterable table beside the calendar."""
    bookings = Booking.query.order_by(Booking.date.desc(), Booking.time.desc()).all()

    rows = []
    for b in bookings:
        start_min = b.time.hour * 60 + b.time.minute
        duration = b.duration_minutes or DEFAULT_DURATION_MIN
        end_min = compute_finish_minutes(start_min, duration)
        customer_name = b.contact_name if b.walkin_customer_id else (b.customer.fullname if b.customer else '—')
        preferred = (b.preferred_mechanic_name or '').strip()
        assigned = (b.assigned_mechanic_name or '').strip()
        rows.append({
            'id': b.id,
            'date': b.date.isoformat(),
            'date_label': b.date.strftime('%b %d, %Y'),
            'time': minutes_to_hhmm(start_min), 'time_label': minutes_to_ampm(start_min),
            'end_label': minutes_to_ampm(end_min),
            'duration_minutes': duration,
            'is_multiday': bool(b.is_multiday),
            'customer': customer_name,
            'assigned_mechanic': assigned or None,
            'preferred_mechanic': preferred or None,
            'shows_preference': bool(preferred) and preferred != assigned,
            'service': b.service,
            'status': b.status,
        })
    return jsonify({'bookings': rows})


@admin_app.route('/api/capacity')
@login_required
@require_admin_or_staff
def api_capacity():
    """Everything the capacity strip shows for one day: the roster panel, the
    cap panel, and the bottleneck panel that's the actual point of the
    dashboard — which lever, staff or policy, is really limiting the day."""
    date_str = request.args.get('date', '').strip()
    try:
        the_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Invalid date'}), 400

    roster, rostered_today, on_duty = get_on_duty_mechanics(the_date)
    daily_cap = get_daily_cap(the_date)

    off_duty_within_roster = [m.name for m in rostered_today if m.status != 'available']
    staff_note = None
    if off_duty_within_roster:
        who = ' and '.join(off_duty_within_roster) if len(off_duty_within_roster) <= 2 else \
              f"{', '.join(off_duty_within_roster[:-1])}, and {off_duty_within_roster[-1]}"
        verb = 'is' if len(off_duty_within_roster) == 1 else 'are'
        staff_note = f'{who} {verb} marked off duty, so {len(on_duty)} actually working'

    shortest = db.session.query(func.min(Service.duration_minutes)).filter(
        Service.is_active == True, Service.is_multiday == False
    ).scalar() or DEFAULT_DURATION_MIN

    bottleneck = capacity_bottleneck(shortest, len(on_duty), daily_cap)

    return jsonify({
        'date': date_str,
        'roster_size': len(roster),
        'mechanic_count': len(rostered_today),
        'working_today': len(on_duty),
        'on_duty_names': [m.name for m in on_duty],
        'staff_note': staff_note,
        'daily_cap': daily_cap,
        'shortest_service_minutes': shortest,
        'bottleneck': bottleneck,
    })


@admin_app.route('/api/capacity', methods=['POST'])
@login_required
@require_admin_or_staff
def api_set_capacity():
    """Admin moves the roster slider or the cap stepper for one day — upserts
    the override row; either field can be sent alone."""
    data = request.get_json() or {}
    date_str = clean_str(data.get('date', ''), max_len=10)
    try:
        the_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid date'}), 400

    row = get_capacity_row(the_date)
    if not row:
        row = DailyCapacity(date=the_date)
        db.session.add(row)

    if 'mechanic_count' in data:
        mc = data.get('mechanic_count')
        roster_size = Mechanic.query.count()
        row.mechanic_count = None if mc is None else max(0, min(int(mc), roster_size))
    if 'daily_cap' in data:
        dc = data.get('daily_cap')
        row.daily_cap = None if dc is None else max(0, int(dc))

    db.session.commit()
    return jsonify({'success': True})



# ═══════════════ SHIP TO ADDRESS (Lalamove) — the shop's side of the flow ═══════════════
SHIP_OPTION_LABELS = {'priority': 'Priority', 'regular': 'Regular', 'pooling': 'Pooling'}
SHIP_ACTIVE = ('confirmed', 'in_progress', 'done_packaging', 'shipping')
SHIP_VEHICLES = ('Motorcycle', 'Sedan', 'Subcompact SUV')


def _ship_prices(o):
    try:
        return json.loads(o.shipping_prices or '{}') or {}
    except ValueError:
        return {}


def _ship_grand_total(o):
    return round((o.total_amount or 0) + (o.shipping_fee or 0), 2)


def _ship_when(dt):
    """'Sat, Oct 17, 6:18 PM' - how Ship to Address dates read everywhere."""
    if not dt:
        return ''
    return dt.strftime('%a, %b ') + str(dt.day) + ', ' + dt.strftime('%I:%M %p').lstrip('0')


@admin_app.template_filter('money')
def _money_filter(n):
    """Money everywhere reads like 1,500.00 (the template adds the peso sign)."""
    return f'{float(n or 0):,.2f}'


@admin_app.template_filter('when')
def _when_filter(dt):
    return _ship_when(dt)


@admin_app.template_filter('time_left')
def _time_left_filter(dt):
    """'5h 20m' / '45m' until a deadline (never negative)."""
    if not dt:
        return ''
    secs = max(0, int((dt - ph_now()).total_seconds()))
    h, m = secs // 3600, (secs % 3600) // 60
    if h:
        return f'{h}h {m}m'
    return f'{m}m' if m else 'less than a minute'


@admin_app.template_filter('hours_left')
def _hours_left_filter(dt):
    return (dt - ph_now()).total_seconds() / 3600 if dt else 99


@admin_app.template_filter('ship_prices')
def _ship_prices_filter(o):
    return _ship_prices(o)


def _ship_order_or_error(oid, allowed_statuses=None):
    order = Order.query.get_or_404(oid)
    if order.delivery_method != 'ship':
        return None, (jsonify({'success': False, 'error': 'This step is only for Ship to Address orders.'}), 400)
    if allowed_statuses and order.status not in allowed_statuses:
        return None, (jsonify({'success': False, 'error': 'This order is not at that step.'}), 400)
    return order, None


def _restock_order(order):
    for it in OrderItem.query.filter_by(order_id=order.id).all():
        prod = Product.query.get(it.product_id)
        if prod:
            prod.stock = (prod.stock or 0) + it.quantity
    order.stock_restocked = True


def paymongo_refund(order, reason):
    """Refunds the full amount paid (items + shipping) to the customer's GCash through PayMongo. Returns (ok, error)."""
    headers = {
        "Authorization": f"Basic {base64.b64encode(f'{PAYMONGO_SECRET_KEY}:'.encode()).decode()}",
        "Content-Type": "application/json",
    }
    payment_id = order.paymongo_payment_id
    if not payment_id and order.payment_ref:           # older record: look the payment up from its checkout session
        try:
            res = requests.get(f"{PAYMONGO_API_URL}/checkout_sessions/{order.payment_ref}", headers=headers, timeout=15)
            if res.status_code == 200:
                pays = res.json()['data']['attributes'].get('payments') or []
                payment_id = pays[0]['id'] if pays else None
        except Exception as e:
            print('[paymongo] lookup failed:', e)
    if not payment_id:
        return False, 'We could not find this payment in PayMongo, so it could not be refunded automatically.'
    payload = {"data": {"attributes": {"amount": int(round(_ship_grand_total(order) * 100)), "payment_id": payment_id,
                                       "reason": "requested_by_customer", "notes": (reason or 'Order cancelled by the shop')[:200]}}}
    try:
        res = requests.post(f"{PAYMONGO_API_URL}/refunds", json=payload, headers=headers, timeout=20)
    except Exception as e:
        return False, f'Could not reach PayMongo: {e}'
    if res.status_code in (200, 201):
        return True, None
    try:
        detail = res.json()['errors'][0]['detail']
    except Exception:
        detail = f'PayMongo answered {res.status_code}.'
    return False, f'PayMongo could not refund this payment: {detail}'


@admin_app.route('/order/<int:oid>/ship/status', methods=['POST'])
@login_required
@require_admin_or_staff
def ship_set_status(oid):
    """Confirmed -> In Progress -> Done Packaging (forward only)."""
    order, err = _ship_order_or_error(oid)
    if err: return err
    new = clean_str((request.get_json(silent=True) or request.form).get('status', ''), max_len=20)
    nxt = {'confirmed': 'in_progress', 'in_progress': 'done_packaging'}
    if nxt.get(order.status) != new:
        return jsonify({'success': False, 'error': 'That is not the next step for this order.'}), 400
    order.status = new
    db.session.commit()
    notice = order_status_message(new, order.id, delivery_method='ship', ship_address=order.ship_address, when=ph_now())
    if notice:
        _notify_order_customer(order, notice[0], notice[1], status=new)
    return jsonify({'success': True, 'status': new})


@admin_app.route('/order/<int:oid>/ship/options', methods=['POST'])
@login_required
@require_admin_or_staff
def ship_send_options(oid):
    """Send (or edit) the shipping options once the order is packed. The customer then has 24 hours to choose and pay.
    Sending or editing both start a fresh 24 hours and a fresh reminder."""
    order, err = _ship_order_or_error(oid, ('done_packaging',))
    if err: return err
    if order.paid_at:
        return jsonify({'success': False, 'error': 'This order is already paid.'}), 400
    d = request.get_json(silent=True) or {}
    vehicle = clean_str(d.get('vehicle', ''), max_len=40)
    reason  = clean_str(d.get('reason', ''), max_len=120)
    errors, fields = [], []
    if vehicle not in SHIP_VEHICLES:
        errors.append('Choose the vehicle for this order.'); fields.append('vehicle')
        vehicle = ''
    if vehicle == 'Motorcycle':
        reason = ''
    elif vehicle:
        if not reason:
            errors.append('Type the reason. The customer will see it.' if d.get('reason_custom')
                          else 'Choose a reason or type your own. The customer will see it.')
            fields.append('reason')

    def peso(key):
        raw = str(d.get(key) if d.get(key) is not None else '').replace(',', '').replace('\u20b1', '').strip()
        if not raw:
            return None, False
        if not raw.isdigit() or not (1 <= int(raw) <= 2000):
            return None, True
        return int(raw), False
    prices, price_bad = {}, False
    for key in ('priority', 'regular'):
        v, bad = peso(key)
        prices[key] = v
        if v is None:
            price_bad = True; fields.append(key)
    if price_bad:
        errors.append('Type the Lalamove price for Priority and Regular, from \u20b11 to \u20b12,000. Pooling can be left blank.')
    v, bad = peso('pooling')
    prices['pooling'] = v
    if bad:
        errors.append('Pooling must be from \u20b11 to \u20b12,000, or leave it blank.'); fields.append('pooling')
    if errors:
        return jsonify({'success': False, 'error': errors[0], 'errors': errors, 'fields': fields}), 400
    first = not order.shipping_options_sent_at
    order.shipping_vehicle = vehicle
    order.shipping_reason = reason or None
    order.shipping_prices = json.dumps(prices)
    order.shipping_options_sent_at = ph_now()
    order.shipping_deadline_at = ph_now() + timedelta(hours=24)
    order.shipping_reminder_at = None
    db.session.commit()
    ref = f'ORD-{order.id:03d}'
    _t, _m, _s = ship_notice('options' if first else 'options_updated', ref, vehicle=vehicle, prices=prices,
                             deadline=_ship_when(order.shipping_deadline_at))
    extra = ship_email_extra('options', vehicle=vehicle, reason=reason, prices=prices,
                             items_total=order.total_amount, deadline=_ship_when(order.shipping_deadline_at))
    _notify_order_customer(order, _t, _m, status=_s, extra_html=extra)
    return jsonify({'success': True, 'first': first})


@admin_app.route('/order/<int:oid>/ship/start', methods=['POST'])
@login_required
@require_admin_or_staff
def ship_start_shipping(oid):
    """After payment: the vehicle is booked in Lalamove, the order goes to Shipping, and the rider/driver details are saved."""
    order, err = _ship_order_or_error(oid, ('done_packaging',))
    if err: return err
    if not order.paid_at:
        return jsonify({'success': False, 'error': 'Shipping unlocks once the customer pays.'}), 400
    d = request.get_json(silent=True) or {}
    rtype = 'rider' if (order.shipping_vehicle or '') == 'Motorcycle' else 'driver'
    who = rtype
    name  = clean_str(d.get('name', ''), max_len=100)
    phone = re.sub(r'[\s-]', '', clean_str(d.get('phone', ''), max_len=20))
    model = clean_str(d.get('model', ''), max_len=60)
    plate = clean_str(d.get('plate', ''), max_len=30).upper()
    link  = clean_str(d.get('link', ''), max_len=500)
    missing, bad, fields = [], [], []
    for key, label, val in (('name', f'{who} name', name), ('phone', f'{who} mobile', phone),
                            ('model', 'motorcycle model' if rtype == 'rider' else 'car model', model),
                            ('plate', 'plate number', plate), ('link', 'Lalamove tracking link', link)):
        if not val:
            missing.append(label); fields.append(key)
    if phone and not re.fullmatch(r'09\d{9}', phone):
        bad.append('The mobile number must be 09 followed by 9 digits.'); fields.append('phone')
    if link and not link.lower().startswith('https://'):
        bad.append('The tracking link must start with https://'); fields.append('link')
    fare = None
    raw_fare = str(d.get('fare', '') if d.get('fare') is not None else '').replace(',', '').strip()
    if raw_fare:
        try:
            fare = round(float(raw_fare), 2)
            if fare <= 0: raise ValueError
        except ValueError:
            bad.append('The Lalamove fare is not a valid amount.'); fields.append('fare')
    errors = []
    if missing:
        errors.append('Please add the ' + (', '.join(missing[:-1]) + ' and ' + missing[-1] if len(missing) > 1 else missing[0]) + '.')
    errors += bad
    if errors:
        return jsonify({'success': False, 'error': errors[0], 'errors': errors, 'fields': fields}), 400
    order.rider_type, order.rider_name, order.rider_phone, order.rider_plate, order.lalamove_link = rtype, name, phone, plate, link
    order.rider_model = model
    order.lalamove_fare = fare
    order.status = 'shipping'
    order.shipping_started_at = ph_now()
    db.session.commit()
    ref = f'ORD-{order.id:03d}'
    _t, _m, _s = ship_notice('on_the_way', ref, rider_type=rtype, name=name, model=model, plate=plate, vehicle=order.shipping_vehicle or '')
    _cust = User.query.get(order.user_id)
    mail = ship_on_the_way_email(_cust.fullname if _cust else '', ref, dict(
        rider_type=rtype, name=name, phone=phone, model=model, plate=plate, vehicle=order.shipping_vehicle or '',
        option=SHIP_OPTION_LABELS.get(order.shipping_option, ''), link=link, base_url=os.getenv('BASE_URL', '')))
    _notify_order_customer(order, _t, _m, status=_s, email=mail)
    return jsonify({'success': True})


@admin_app.route('/order/<int:oid>/ship/complete', methods=['POST'])
@login_required
@require_admin_or_staff
def ship_complete(oid):
    """Completed once the customer confirmed it arrived, or by the shop after checking Lalamove (with the proof saved)."""
    order, err = _ship_order_or_error(oid, ('shipping',))
    if err: return err
    d = request.get_json(silent=True) or {}
    proof = clean_str(d.get('proof', ''), max_len=500)
    if not order.received_at:
        errors, fields = [], []
        if not d.get('delivered'):
            errors.append('Tick the box once Lalamove shows the order as Delivered.'); fields.append('delivered')
        if not proof:
            errors.append('Write what Lalamove shows, like the time it was delivered.'); fields.append('proof')
        if errors:
            return jsonify({'success': False, 'error': errors[0], 'errors': errors, 'fields': fields}), 400
    order.status = 'completed'
    order.delivered_at = ph_now()
    order.completed_by_shop = not bool(order.received_at)
    if proof and order.completed_by_shop:
        order.completion_proof = proof
    db.session.commit()
    _t, _m, _s = ship_notice('completed', f'ORD-{order.id:03d}', by_shop=bool(order.completed_by_shop), proof=order.completion_proof or '')
    _notify_order_customer(order, _t, _m, status=_s)
    return jsonify({'success': True})


@admin_app.route('/order/<int:oid>/ship/remind', methods=['POST'])
@login_required
@require_admin_or_staff
def ship_remind_received(oid):
    order, err = _ship_order_or_error(oid, ('shipping',))
    if err: return err
    if order.received_at:
        return jsonify({'success': False, 'error': 'The customer already confirmed.'}), 400
    order.received_reminder_at = ph_now()
    db.session.commit()
    _t, _m, _s = ship_notice('received_check', f'ORD-{order.id:03d}')
    _notify_order_customer(order, _t, _m, status=_s)
    return jsonify({'success': True, 'time': _ship_when(order.received_reminder_at)})


@admin_app.route('/order/<int:oid>/ship/cancel', methods=['POST'])
@login_required
@require_admin_or_staff
def ship_cancel(oid):
    """Cancel before it ships. Unpaid: simply cancelled. Paid: the full amount goes back to the customer's GCash through PayMongo."""
    order, err = _ship_order_or_error(oid, ('confirmed', 'in_progress', 'done_packaging'))
    if err: return err
    d = request.get_json(silent=True) or {}
    reason = clean_str(d.get('reason', ''), max_len=300)
    restock = bool(d.get('restock', True))
    ref = f'ORD-{order.id:03d}'
    if order.paid_at:
        if not reason:
            return jsonify({'success': False, 'error': 'Type the reason for the refund.'}), 400
        ok, why = paymongo_refund(order, reason)
        if not ok:
            return jsonify({'success': False, 'error': why}), 502
        order.status = 'refunded'
        order.refunded_at = ph_now()
        order.refund_amount = _ship_grand_total(order)
        order.refund_reason = reason
        title, body, status = ship_notice('refunded', ref, reason=reason, amount=order.refund_amount)
        extra = ship_email_extra('refunded', items_total=order.total_amount, shipping_fee=order.shipping_fee or 0, amount=order.refund_amount)
    else:
        order.status = 'cancelled'
        title, body, status = ship_notice('cancelled', ref, by='shop', reason=reason)
        extra = ''
    order.cancelled_by = 'shop'
    order.cancel_reason = reason or None
    if restock:
        _restock_order(order)
    else:
        order.stock_restocked = False
    db.session.commit()
    _notify_order_customer(order, title, body, status=status, extra_html=extra)
    stock_note = 'Items put back in stock.' if restock else 'Items NOT put back in stock.'
    for admin in User.query.filter_by(role='admin').all():
        send_notification(admin.id, f'{ref} ' + ('refunded' if order.status == 'refunded' else 'cancelled'),
            (f'{ref} refunded \u00b7 \u20b1{order.refund_amount:,.2f} back to GCash via PayMongo. Reason: {reason}. ' if order.status == 'refunded'
             else f'{ref} cancelled by the shop \u00b7 not charged. ') + stock_note, type='order', status=order.status, order_id=order.id)
    return jsonify({'success': True, 'status': order.status})


@admin_app.route('/order/<int:oid>/status', methods=['POST'])
@login_required
@require_admin_or_staff
def update_order_status(oid):
    order = Order.query.get_or_404(oid)
    new_status = validate_order_status(clean_str(request.form.get('status', ''), max_len=20))
    _pay = (order.payment_method or '').lower()
    _is_pickup = order.delivery_method != 'ship'

    if new_status == 'completed' and _is_pickup:
        msg = 'Pick-up orders are completed on the Billing page when the customer picks it up.'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': msg}), 400
        flash(msg, 'danger')
        return redirect(url_for('admin_dashboard'))

    if not _is_pickup:
        msg = 'Ship to Address orders move through the Lalamove steps: use In Progress, Done Packaging, Send shipping options, Shipping and Completed.'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': msg}), 400
        flash(msg, 'danger')
        return redirect(url_for('admin_dashboard'))

    if new_status == 'completed' and not _is_pickup:
        msg = 'Ship-to-address orders are completed when the customer confirms receipt on their dashboard — only they know it actually arrived.'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': msg}), 400
        flash(msg, 'danger')
        return redirect(url_for('admin_dashboard'))

    # Ship-to-address orders: staff can move it up to "shipped" (on the way); only the
    # customer's own receipt confirmation can complete it from there — see the check above.
    if not _is_pickup:
        ship_allowed_next = {
            'pending':          {'confirmed', 'cancelled'},
            'awaiting_payment': {'confirmed', 'cancelled'},
            'confirmed':        {'processing', 'shipped', 'cancelled'},
            'processing':       {'shipped', 'cancelled'},
            'shipped':          {'cancelled'},
        }
        if order.status in ship_allowed_next and new_status != order.status and new_status not in ship_allowed_next[order.status]:
            msg = 'Invalid status change for this order.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': msg}), 400
            flash(msg, 'danger')
            return redirect(url_for('admin_dashboard'))

    # Pick-up orders follow a fixed sequence — confirmed -> processing -> shipped
    # (ready for pickup) — for both payment methods; "completed" only ever
    # happens on the Billing page, GCash included, since that's the one place
    # that marks an order actually handed over.
    if _is_pickup:
        if _pay == 'gcash':
            allowed_next = {
                'confirmed':  {'processing', 'shipped', 'cancelled'},
                'processing': {'shipped', 'cancelled'},
                'shipped':    {'cancelled'},
            }
        else:
            allowed_next = {
                'pending':          {'confirmed', 'cancelled'},
                'awaiting_payment': {'confirmed', 'cancelled'},
                'confirmed':        {'processing', 'shipped', 'cancelled'},
                'processing':       {'shipped', 'cancelled'},
                'shipped':          {'cancelled'},
            }
        if order.status in allowed_next and new_status != order.status and new_status not in allowed_next[order.status]:
            msg = 'Invalid status change for this order.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'success': False, 'error': msg}), 400
            flash(msg, 'danger')
            return redirect(url_for('admin_dashboard'))

    if new_status == 'cancelled' and order.status != 'cancelled':
        for item in OrderItem.query.filter_by(order_id=order.id).all():
            product = Product.query.get(item.product_id)
            if product:
                product.stock += item.quantity
    prev_status  = order.status
    order.status = new_status
    if new_status == 'completed' and not order.delivered_at:
        order.delivered_at = ph_now()
    db.session.commit()
    # Every tracking step gets a customer notification stamped with the exact time
    # of the update — ship-to-address orders get delivery-specific wording. Re-saving
    # the same status is a no-op so the customer isn't notified twice for one step.
    notice = order_status_message(new_status, order.id,
                                  delivery_method=order.delivery_method,
                                  ship_address=order.ship_address,
                                  when=ph_now()) if new_status != prev_status else None
    if notice:
        title, msg = notice
        _notify_order_customer(order, title, msg, status=new_status)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'new_status': new_status, 'message': 'Order status updated!'})
    flash('Order status updated!', 'success')
    return redirect(url_for('admin_dashboard'))


# Service routes

def _parse_duration_fields(form):
    """(duration_minutes, is_multiday, duration_label) from the Add/Edit Service form."""
    is_multiday = form.get('is_multiday') == 'on'
    if is_multiday:
        label = clean_str(form.get('duration_label', ''), max_len=30) or '3–5 days'
        return MULTIDAY_INTAKE_MIN, True, label
    minutes = clean_int(form.get('duration_minutes', DEFAULT_DURATION_MIN),
                        default=DEFAULT_DURATION_MIN, min_val=1, max_val=1440)
    return minutes, False, None


@admin_app.route('/service/add', methods=['POST'])
@login_required
@require_admin_or_staff
def add_service():
    name  = clean_str(request.form.get('name', ''), max_len=100)
    desc  = clean_str(request.form.get('description', ''), max_len=500)
    price = clean_float(request.form.get('price', 0), default=0.0, min_val=0.0)
    duration_minutes, is_multiday, duration_label = _parse_duration_fields(request.form)
    if not name:
        flash('Service name is required.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if Service.query.filter_by(name=name).first():
        flash(f'Service "{name}" already exists.', 'warning')
        return redirect(url_for('admin_dashboard'))
    db.session.add(Service(name=name, description=desc, price=price,
                           duration_minutes=duration_minutes, is_multiday=is_multiday,
                           duration_label=duration_label))
    db.session.commit()
    flash(f'Service "{name}" added.', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/service/<int:sid>/toggle', methods=['POST'])
@login_required
@require_admin_or_staff
def toggle_service(sid):
    svc = Service.query.get_or_404(sid)
    svc.is_active = not svc.is_active
    db.session.commit()
    status = 'activated' if svc.is_active else 'deactivated'
    flash(f'Service "{svc.name}" {status}.', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/service/<int:sid>/edit', methods=['POST'])
@login_required
@require_admin_or_staff
def edit_service(sid):
    svc   = Service.query.get_or_404(sid)
    name  = clean_str(request.form.get('name', ''), max_len=100)
    desc  = clean_str(request.form.get('description', ''), max_len=500)
    price = clean_float(request.form.get('price', 0), default=0.0, min_val=0.0)
    duration_minutes, is_multiday, duration_label = _parse_duration_fields(request.form)
    if not name:
        flash('Service name is required.', 'danger')
        return redirect(url_for('admin_dashboard'))
    existing = Service.query.filter_by(name=name).first()
    if existing and existing.id != sid:
        flash(f'Service "{name}" already exists.', 'warning')
        return redirect(url_for('admin_dashboard'))
    svc.name = name
    svc.description = desc
    svc.price = price
    svc.duration_minutes = duration_minutes
    svc.is_multiday = is_multiday
    svc.duration_label = duration_label
    db.session.commit()
    flash(f'Service updated to "{name}".', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/service/<int:sid>/delete', methods=['POST'])
@login_required
@require_admin_or_staff
def delete_service(sid):
    svc = Service.query.get_or_404(sid)
    db.session.delete(svc)
    db.session.commit()
    flash(f'Service "{svc.name}" deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/api/services')
@login_required
def admin_api_services():
    try:
        svcs = Service.query.filter_by(is_active=True).order_by(Service.name).all()
        return jsonify([{'id': s.id, 'name': s.name, 'price': s.price, 'duration_minutes': s.duration_minutes or 30} for s in svcs])
    except Exception:
        return jsonify([])



def _save_product_image(file):
    """Uploads a product photo to Cloudinary (falls back to local disk only
    when Cloudinary isn't configured, e.g. local dev) and returns the URL/path
    to store on Product.image_url, or None if no usable file was given."""
    if not file or file.filename == '' or not allowed_file(file.filename):
        return None
    cloud_url = upload_image(file, 'product_photos')
    if cloud_url:
        return cloud_url
    ext      = file.filename.rsplit('.', 1)[1].lower()
    filename = f"product_{uuid.uuid4().hex}.{ext}"
    folder   = os.path.join(admin_app.root_path, 'static', 'product_photos')
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, filename))
    return filename  # bare filename — resolved against static/product_photos at render time, same convention as User.profile_pic


LOW_STOCK_BELOW = 5   # same cut-off the Manage Products table uses for its amber "low" count


def _product_change_response(product, saved_msg):
    """Product add/edit/delete never use flash(): a flash sits in the session
    until ANY page renders it, so it used to pop up on Quotations or Job
    Orders. Instead the Manage Products page shows its own pop-up from this
    JSON — green when saved, orange when the product is now low or out."""
    level, msg = 'success', saved_msg
    if product is not None:
        if product.stock <= 0:
            level, msg = 'warning', f'{saved_msg} But it is now out of stock.'
        elif product.stock < LOW_STOCK_BELOW:
            level, msg = 'warning', f'{saved_msg} But only {product.stock} left — low on stock.'
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'level': level, 'message': msg})
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/product/add', methods=['POST'])
@login_required
@require_admin_or_staff
def add_product():
    image_url = _save_product_image(request.files.get('image'))
    new_p = Product(
        name=request.form['name'], category=request.form['category'],
        description=request.form.get('description', ''),
        price=float(request.form['price']), stock=int(request.form['stock']),
        image_url=image_url
    )
    db.session.add(new_p)
    db.session.commit()
    return _product_change_response(new_p, f'Product "{new_p.name}" added!')


@admin_app.route('/product/<int:pid>/edit', methods=['POST'])
@login_required
@require_admin_or_staff
def edit_product(pid):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    p             = Product.query.get_or_404(pid)
    p.barcode     = request.form.get('barcode', '').strip() or None
    p.name        = request.form['name']
    p.category    = request.form['category']
    p.price       = float(request.form['price'])
    p.stock       = int(request.form['stock'])
    p.description = request.form.get('description', '')
    new_image = _save_product_image(request.files.get('image'))
    if new_image:
        p.image_url = new_image
    elif request.form.get('remove_image') == '1':
        p.image_url = None
    db.session.commit()
    return _product_change_response(p, f'Product "{p.name}" updated!')


@admin_app.route('/product/<int:pid>/delete', methods=['POST'])
@login_required
@require_admin_or_staff
def delete_product(pid):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    p = Product.query.get_or_404(pid)
    name = p.name
    db.session.delete(p)
    db.session.commit()
    return _product_change_response(None, f'Product "{name}" deleted!')


# ── Mechanic attendance ──────────────────────────────────────────────────────
# Shift 8:00 AM–6:30 PM, lunch 12:00–12:30 PM (unpaid: never counted as worked
# time). Timing in after 8:15 AM is "late", measured from the 8:00 AM start.
SHIFT_START_MIN = 8 * 60
SHIFT_END_MIN   = 18 * 60 + 30
LUNCH_START_MIN = 12 * 60
LUNCH_END_MIN   = 12 * 60 + 30
LATE_AFTER_MIN  = 8 * 60 + 15


def _t_min(t):
    return t.hour * 60 + t.minute


def _hm_label(minutes):
    h, m = divmod(int(minutes), 60)
    return f'{h}h {m:02d}m' if h else f'{m} min'


def _worked_minutes(t_in, t_out):
    start, end = _t_min(t_in), _t_min(t_out)
    lunch = max(0, min(end, LUNCH_END_MIN) - max(start, LUNCH_START_MIN))
    return max(0, end - start - lunch)


def _attendance_info(rec):
    """Everything one attendance row displays, derived from the stored times."""
    absent = bool(rec and rec.is_absent)
    t_in = None if absent else (rec.time_in if rec else None)
    t_out = None if absent else (rec.time_out if rec else None)
    late_min = max(0, _t_min(t_in) - SHIFT_START_MIN) if (t_in and _t_min(t_in) > LATE_AFTER_MIN) else 0
    if absent:
        status, label = 'absent', 'Absent'
    elif not t_in:
        status, label = 'not_in', 'Not timed in yet'
    elif late_min:
        status, label = 'late', f'Late {_hm_label(late_min)}'
    else:
        status, label = 'present', 'Present'
    worked = _worked_minutes(t_in, t_out) if (t_in and t_out) else None
    return {
        'status': status, 'status_label': label, 'late_minutes': late_min,
        'time_in': t_in.strftime('%I:%M %p') if t_in else None,
        'time_out': t_out.strftime('%I:%M %p') if t_out else None,
        'time_in_value': t_in.strftime('%H:%M') if t_in else '',
        'time_out_value': t_out.strftime('%H:%M') if t_out else '',
        'worked_minutes': worked, 'worked_label': _hm_label(worked) if worked is not None else None,
        'on_duty': bool(t_in and not t_out),
    }


def _attendance_for_date(d):
    recs = {r.mechanic_id: r for r in MechanicAttendance.query.filter_by(date=d).all()}
    return {m.id: _attendance_info(recs.get(m.id)) for m in Mechanic.query.all()}


def _parse_att_date(raw):
    """(date, error response) — blank means today; future days are refused."""
    today = ph_now().date()
    try:
        d = date.fromisoformat(raw) if raw else today
    except ValueError:
        return None, (jsonify({'success': False, 'error': 'Invalid date.'}), 400)
    if d > today:
        return None, (jsonify({'success': False, 'error': 'Attendance can\'t be recorded for a future date.'}), 400)
    return d, None


def _attendance_rec(mid, d):
    rec = MechanicAttendance.query.filter_by(mechanic_id=mid, date=d).first()
    if not rec:
        rec = MechanicAttendance(mechanic_id=mid, date=d)
        db.session.add(rec)
    return rec


@admin_app.route('/api/attendance')
@login_required
@require_admin_or_staff
def api_attendance():
    d, err = _parse_att_date(request.args.get('date', ''))
    if err:
        return err
    info = _attendance_for_date(d)
    rows = []
    for m in Mechanic.query.order_by(Mechanic.name).all():
        rows.append({'mechanic_id': m.id, 'name': m.name, 'specialization': m.specialization, **info[m.id]})
    counts = {k: sum(1 for r in rows if r['status'] == k) for k in ('present', 'late', 'absent', 'not_in')}
    return jsonify({'date': d.isoformat(), 'is_today': d == ph_now().date(), 'rows': rows, 'counts': counts,
                    'today': ph_now().date().isoformat()})


@admin_app.route('/api/attendance/<int:mid>/<action>', methods=['POST'])
@login_required
@require_admin_or_staff
def api_attendance_action(mid, action):
    """time-in / time-out stamp the current time (today only); absent marks the
    day absent; edit takes explicit times for any past or present day."""
    Mechanic.query.get_or_404(mid)
    data = request.get_json(silent=True) or {}
    d, err = _parse_att_date(data.get('date', ''))
    if err:
        return err
    now = ph_now()
    if action in ('time-in', 'time-out') and d != now.date():
        return jsonify({'success': False, 'error': 'Time in / Time out record the current time, so they only work for today. Use Edit for earlier days.'}), 400

    rec = _attendance_rec(mid, d)
    if action == 'time-in':
        if rec.time_in and not rec.is_absent:
            return jsonify({'success': False, 'error': 'Already timed in.'}), 400
        rec.is_absent, rec.time_in, rec.time_out = False, now.time().replace(second=0, microsecond=0), None
    elif action == 'time-out':
        if rec.is_absent or not rec.time_in:
            return jsonify({'success': False, 'error': 'Time in first.'}), 400
        if rec.time_out:
            return jsonify({'success': False, 'error': 'Already timed out.'}), 400
        t_out = now.time().replace(second=0, microsecond=0)
        if t_out < rec.time_in:
            return jsonify({'success': False, 'error': 'Time out can\'t be earlier than time in.'}), 400
        rec.time_out = t_out
    elif action == 'absent':
        rec.is_absent, rec.time_in, rec.time_out = True, None, None
    elif action == 'edit':
        if data.get('absent'):
            rec.is_absent, rec.time_in, rec.time_out = True, None, None
        else:
            try:
                t_in = datetime.strptime(data['time_in'], '%H:%M').time() if data.get('time_in') else None
                t_out = datetime.strptime(data['time_out'], '%H:%M').time() if data.get('time_out') else None
            except ValueError:
                return jsonify({'success': False, 'error': 'Invalid time.'}), 400
            if t_out and not t_in:
                return jsonify({'success': False, 'error': 'Add a time in before a time out.'}), 400
            if t_in and t_out and t_out < t_in:
                return jsonify({'success': False, 'error': 'Time out can\'t be earlier than time in.'}), 400
            if d == now.date() and any(t and t > now.time() for t in (t_in, t_out)):
                return jsonify({'success': False, 'error': 'A time can\'t be later than right now.'}), 400
            rec.is_absent, rec.time_in, rec.time_out = False, t_in, t_out
    else:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'Unknown action.'}), 404
    db.session.commit()
    return jsonify({'success': True})


@admin_app.route('/api/mechanics-roster')
@login_required
@require_admin_or_staff
def api_mechanics_roster():
    """The roster behind the bookings calendar, not a separate address book —
    reads the exact same Mechanic and Booking rows the calendar does, so a
    status change made here, or an assignment made from the calendar, shows
    up in both immediately. Also answers the actual point of this page: not
    just who exists, but what each one is doing right now.

    'Effective status' is derived, never stored, resolved in order: marked
    off duty in the profile always wins (nothing overrides it — they're not
    in the shop); otherwise a live or upcoming job today makes them busy no
    matter what the profile says; otherwise the profile value stands
    (available, or busy for a manually-set, non-booking reason). 'Not
    rostered today' is a wholly separate, purely positional fact — whoever
    sits past the on-duty slider's cutoff keeps their real status and just
    gets that note added beneath it, because the slider is today's staffing
    decision and the profile is a fact about the person."""
    today = ph_now().date()
    now = ph_now()
    now_min = now.hour * 60 + now.minute

    mechanics = Mechanic.query.order_by(Mechanic.name).all()
    _, rostered_today_list, _ = get_on_duty_mechanics(today)
    rostered_names = {m.name for m in rostered_today_list}

    jobs_map = _mechanic_jobs_map()
    attendance_today = _attendance_for_date(today)
    result = []

    def job_dict(b):
        b_start = b.time.hour * 60 + b.time.minute
        b_end = real_end_minutes(b_start, b.duration_minutes or DEFAULT_DURATION_MIN, b.overrun_minutes or 0)
        started = b.status in STARTED_STATUSES
        return {
            'booking_id': b.id, 'ref': f'BKG-{b.id:03d}', 'customer': _booking_customer_name(b), 'service': b.service,
            'date_label': b.date.strftime('%b %d'), 'start_label': minutes_to_ampm(b_start), 'end_label': minutes_to_ampm(b_end),
            'is_today': b.date == today, 'state': 'started' if started else 'confirmed',
            'status_label': STARTED_LABELS.get(b.status, ''), 'when_full': b.date.strftime('%b %d, %Y ') + b.time.strftime('%I:%M %p'),
        }

    for m in mechanics:
        j = jobs_map.get(m.name) or _NO_JOBS
        all_jobs = j['started'] + j['reserved']
        assigned = job_dict(all_jobs[0]) if all_jobs else None
        more_count = max(0, len(all_jobs) - 1)

        # What was typed in Edit only shows when there is no booking at all, and is always labelled.
        assigned_is_manual = False
        if not all_jobs and (m.manual_customer or m.manual_service):
            assigned = {'customer': m.manual_customer or '', 'service': m.manual_service or ''}
            assigned_is_manual = True

        lock = None
        if j['started']:
            sb = j['started'][0]
            lock = {'kind': 'started', 'ref': f'#{sb.id}', 'status_label': STARTED_LABELS.get(sb.status, '')}
        elif j['reserved']:
            lock = {'kind': 'reserved', 'refs': [f'#{x.id}' for x in j['reserved']],
                    'waiting': [{'ref': f'#{x.id}', 'service': x.service, 'when': x.date.strftime('%b %d, %Y ') + x.time.strftime('%I:%M %p')}
                                for x in j['reserved']]}

        result.append({
            'id': m.id, 'name': m.name, 'specialization': m.specialization, 'status': m.status,
            'phone': m.phone, 'note': m.note,
            'attendance': attendance_today.get(m.id),
            'not_rostered_today': m.name not in rostered_names,
            'effective_status': _mechanic_effective(m, jobs_map),
            'active_jobs_count': len(all_jobs), 'lock': lock,
            'assigned': assigned, 'more_count': more_count, 'assigned_is_manual': assigned_is_manual,
            'manual_customer': m.manual_customer, 'manual_service': m.manual_service,
        })
    return jsonify({'mechanics': result, 'today': today.isoformat()})


@admin_app.route('/api/mechanics/<int:mid>/assignable-bookings')
@login_required
@require_admin_or_staff
def api_mechanic_assignable_bookings(mid):
    """Today's unassigned bookings, from one mechanic's point of view — 'what
    can X actually pick up' rather than 'who fits this slot'. Every
    unassigned booking today is listed, never dropped from the list; each is
    fit-checked through the exact same routine actually assigning it would
    use, so a greyed-out reason here can never disagree with the refusal
    you'd get by trying it anyway. This is the secondary way in — the
    primary one is the assign control on the booking's own card."""
    mechanic = Mechanic.query.get_or_404(mid)
    today = ph_now().date()
    bookings = Booking.query.filter(
        Booking.date == today, Booking.assigned_mechanic_name.is_(None),
        Booking.status.notin_(['cancelled', 'completed']),
    ).order_by(Booking.time).all()

    mine = _mechanic_jobs_map().get(mechanic.name) or _NO_JOBS
    taken = {(x.date, x.time) for x in mine['started'] + mine['reserved']}
    items = []
    for b in bookings:
        start_min = b.time.hour * 60 + b.time.minute
        duration = b.duration_minutes or DEFAULT_DURATION_MIN
        if mechanic.status == 'off_duty':
            ok, reason = False, 'Off Duty'
        elif (b.date, b.time) in taken:
            ok, reason = False, 'already booked then'
        else:
            ok, reason = _check_admin_booking_request(
                b.date, start_min, duration, exclude_id=b.id,
                mechanic_name=mechanic.name, require_slot_grid=False, check_shop_queue=False,
            )
        items.append({
            'booking_id': b.id, 'ref': f'BKG-{b.id:03d}',
            'customer': _booking_customer_name(b), 'service': b.service,
            'start_label': minutes_to_ampm(start_min),
            'end_label': minutes_to_ampm(real_end_minutes(start_min, duration, b.overrun_minutes or 0)),
            'fits': ok, 'reason': None if ok else reason,
        })
    return jsonify({'mechanic_id': mechanic.id, 'mechanic_name': mechanic.name, 'items': items})


@admin_app.route('/mechanic/add', methods=['POST'])
@login_required
@require_admin_or_staff
def add_mechanic():
    if current_user.role != 'admin':
        return jsonify({'success': False, 'error': 'Access denied.'}), 403
    data = request.get_json() or {}
    name = clean_str(data.get('name', ''), max_len=100)
    specialization = clean_str(data.get('specialization', ''), max_len=100)
    status = data.get('status') if data.get('status') in ('available', 'busy', 'off_duty') else 'available'
    phone = clean_str(data.get('phone', ''), max_len=20)
    note = clean_str(data.get('note', ''), max_len=500)
    manual_customer = clean_str(data.get('manual_customer', ''), max_len=200)
    manual_service = clean_str(data.get('manual_service', ''), max_len=300)
    if not name or specialization not in MECHANIC_SPECIALIZATIONS:
        return jsonify({'success': False, 'error': 'Name is required and specialization must be one of the six.'}), 400
    if phone and not is_valid_phone(phone):
        return jsonify({'success': False, 'error': 'That phone number does not look right — use a Philippine mobile number.'}), 400
    m = Mechanic(name=name, specialization=specialization, status=status, phone=phone or None, note=note or None,
                 manual_customer=manual_customer or None, manual_service=manual_service or None)
    db.session.add(m)
    db.session.commit()
    return jsonify({'success': True, 'id': m.id})


@admin_app.route('/mechanic/<int:mid>/edit', methods=['POST'])
@login_required
@require_admin_or_staff
def edit_mechanic(mid):
    if current_user.role != 'admin':
        return jsonify({'success': False, 'error': 'Access denied.'}), 403
    m = Mechanic.query.get_or_404(mid)
    data = request.get_json() or {}
    name = clean_str(data.get('name', ''), max_len=100)
    specialization = clean_str(data.get('specialization', ''), max_len=100)
    phone = clean_str(data.get('phone', ''), max_len=20)
    note = clean_str(data.get('note', ''), max_len=500)
    manual_customer = clean_str(data.get('manual_customer', ''), max_len=200)
    manual_service = clean_str(data.get('manual_service', ''), max_len=300)
    status = data.get('status') if data.get('status') in ('available', 'busy', 'off_duty') else m.status
    if not name or specialization not in MECHANIC_SPECIALIZATIONS:
        return jsonify({'success': False, 'error': 'Name is required and specialization must be one of the six.'}), 400
    if phone and not is_valid_phone(phone):
        return jsonify({'success': False, 'error': 'That phone number does not look right — use a Philippine mobile number.'}), 400
    # Bookings store the mechanic's name as plain text (assigned/preferred
    # mechanic fields), not a foreign key — a rename here does not rewrite
    # today's or past bookings, so a profile rename mid-shift can briefly
    # detach a mechanic from jobs already on the board under their old name.
    old_name = m.name
    jobs = _mechanic_jobs_map().get(old_name) or _NO_JOBS
    removed = 0
    if jobs['started']:
        status = m.status                    # locked: it changes on its own when the booking is completed or cancelled
    elif jobs['reserved']:
        if status == 'off_duty':
            for bk in jobs['reserved']:      # their waiting bookings go back to "not assigned yet"
                bk.assigned_mechanic_name = None
                bk.assigned_mechanic_specialization = None
                removed += 1
        else:
            status = 'available'             # Reserved is automatic; Available / Busy can't be picked while booked
    m.name = name
    m.specialization = specialization
    m.phone = phone or None
    m.note = note or None
    m.manual_customer = manual_customer or None
    m.manual_service = manual_service or None
    m.status = status
    db.session.commit()
    msg = 'Mechanic updated.'
    if status == 'off_duty' and old_name:
        msg = f'{name} is now Off Duty' + (f' · {removed} booking{"s" if removed != 1 else ""} need{"s" if removed == 1 else ""} another mechanic' if removed else '')
    return jsonify({'success': True, 'message': msg, 'toast': msg})


@admin_app.route('/mechanic/<int:mid>/delete', methods=['POST'])
@login_required
@require_admin_or_staff
def delete_mechanic(mid):
    """Deleting the profile never deletes the work. Every active booking
    still pointing at this mechanic gets cleared, never removed — it drops
    to 'No mechanic assigned' and surfaces in the day panel's clash list,
    where the existing fixes re-home it. Any customer preference naming
    this mechanic is cleared too, on any booking, any status — nobody is
    left pointing at someone who's gone."""
    if current_user.role != 'admin':
        return jsonify({'success': False, 'error': 'Access denied.'}), 403
    m = Mechanic.query.get_or_404(mid)
    today = ph_now().date()

    active = Booking.query.filter(
        Booking.assigned_mechanic_name == m.name, Booking.date >= today,
        Booking.status.notin_(['cancelled', 'completed']),
    ).all()
    for b in active:
        b.assigned_mechanic_name = None
        b.assigned_mechanic_specialization = None

    for b in Booking.query.filter(Booking.preferred_mechanic_name == m.name).all():
        b.preferred_mechanic_name = None
        b.preferred_mechanic_specialization = None

    unassigned_count = len(active)
    db.session.delete(m)
    db.session.commit()
    return jsonify({'success': True, 'unassigned_count': unassigned_count})



@admin_app.route('/user/<int:uid>/delete', methods=['POST'])
@login_required
def delete_user(uid):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    user = User.query.get_or_404(uid)
    if user.id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if user.role == 'admin':
        flash('Admin accounts cannot be deleted.', 'danger')
        return redirect(url_for('admin_dashboard'))
    OTPRecord.query.filter_by(email=user.email).delete()
    for order in user.orders:
        OrderItem.query.filter_by(order_id=order.id).delete()
    Order.query.filter_by(user_id=user.id).delete()
    Booking.query.filter_by(user_id=user.id).delete()
    Notification.query.filter_by(user_id=user.id).delete()
    name = user.fullname
    db.session.delete(user)
    db.session.commit()
    flash(f'User "{name}" deleted successfully.', 'success')
    return redirect(url_for('admin_dashboard'))


ALLOWED_USER_ACTIONS = {'deactivate', 'reactivate', 'ban', 'flag'}


@admin_app.route('/user/<int:uid>/manage', methods=['POST'])
@login_required
def manage_user(uid):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    user = User.query.get_or_404(uid)
    if user.id == current_user.id:
        flash('You cannot manage your own account.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if user.role == 'admin':
        flash('Admin accounts cannot be managed here.', 'danger')
        return redirect(url_for('admin_dashboard'))

    action = clean_str(request.form.get('action', ''), max_len=20)
    if action not in ALLOWED_USER_ACTIONS:
        flash('Invalid action.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if action == 'deactivate':
        user.account_status = 'deactivated'
        flash(f'{user.fullname} has been deactivated and can no longer log in.', 'success')

    elif action == 'reactivate':
        user.account_status = 'active'
        db.session.commit()
        send_notification(
            user.id, 'Account Restored ✅',
            'Your account access has been restored. You can log in, book appointments, '
            'and place orders again.',
            type='account', status='active')
        flash(f"{user.fullname}'s account has been reactivated.", 'success')
        return redirect(url_for('admin_dashboard'))

    elif action == 'ban':
        user.account_status = 'banned'
        db.session.commit()
        send_notification(
            user.id, '🚫 Account Banned',
            'Your account has been banned due to a violation of MotoTyres\' policies. '
            'You are no longer allowed to book appointments or place orders. '
            'If you believe this is a mistake, please contact our support team.',
            type='account', status='banned')
        flash(f'{user.fullname} has been banned.', 'success')
        return redirect(url_for('admin_dashboard'))

    elif action == 'flag':
        user.is_flagged = True
        db.session.commit()
        send_notification(
            user.id, '⚠️ Account Warning',
            'Your account has been flagged for performing actions that violate MotoTyres\' '
            'terms of service. Please review our policies — repeated violations may lead to '
            'your account being suspended or banned.',
            type='account', status='flagged')
        flash(f'{user.fullname} has been red-flagged.', 'success')
        return redirect(url_for('admin_dashboard'))

    db.session.commit()
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/update-profile', methods=['POST'])
@login_required
@require_admin_or_staff
def update_admin_profile():
    current_user.fullname = request.form['fullname']
    current_user.phone    = request.form['phone']
    db.session.commit()
    flash('Profile updated!', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/account/change-password', methods=['POST'])
@login_required
@require_admin_or_staff
def change_admin_password():
    current_pw = request.form.get('current_password', '')
    new_pw = request.form.get('new_password', '')
    if not current_user.check_password(current_pw):
        return jsonify({'success': False, 'error': 'Current password is incorrect.'}), 400
    if len(new_pw) < 6:
        return jsonify({'success': False, 'error': 'New password must be at least 6 characters.'}), 400
    current_user.set_password(new_pw)
    db.session.commit()
    return jsonify({'success': True})


@admin_app.route('/profile/upload-pic', methods=['POST'])
@login_required
@require_admin_or_staff
def upload_profile_pic():
    """Saves the cropped photo the Account Settings cropper sends. Cloud storage
    when it's configured; otherwise the image itself goes in the database, so a
    redeploy (which wipes files on disk) can never lose it. Always answers JSON
    with a plain-language error the page shows as-is."""
    file = request.files.get('profile_pic')
    if not file or file.filename == '':
        return jsonify({'success': False, 'error': 'Choose a photo first.'}), 400
    if not (file.mimetype or '').startswith('image/'):
        return jsonify({'success': False, 'error': 'That file is not an image. Use a JPG, PNG or GIF.'}), 400
    data = file.read()
    if len(data) > 5 * 1024 * 1024:
        return jsonify({'success': False, 'error': 'That photo is over 5MB. Choose a smaller one.'}), 400
    if not data:
        return jsonify({'success': False, 'error': 'The photo was empty. Try choosing it again.'}), 400
    try:
        file.stream.seek(0)
        cloud_url = upload_image(file, 'profile_pics')
        if cloud_url:
            current_user.profile_pic = cloud_url
            current_user.profile_pic_blob = None
            current_user.profile_pic_mime = None
        else:
            current_user.profile_pic_blob = data
            current_user.profile_pic_mime = (file.mimetype or 'image/jpeg')[:40]
            current_user.profile_pic = url_for('profile_pic_blob', uid=current_user.id,
                                               v=int(datetime.utcnow().timestamp()), _external=True)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print('[profile pic] save failed:', e)
        return jsonify({'success': False, 'error': 'We could not save your picture. Please try again in a moment.'}), 500
    return jsonify({'success': True, 'url': current_user.profile_pic})


@admin_app.route('/profile-pic/<int:uid>')
@login_required
def profile_pic_blob(uid):
    """Serves a picture stored in the database (see upload_profile_pic)."""
    user = User.query.get_or_404(uid)
    if not user.profile_pic_blob:
        abort(404)
    resp = make_response(user.profile_pic_blob)
    resp.headers['Content-Type'] = user.profile_pic_mime or 'image/jpeg'
    resp.headers['Cache-Control'] = 'private, max-age=86400'   # the URL carries a version, so a new photo is a new URL
    return resp


@admin_app.route('/import-products', methods=['POST'])
@login_required
def import_products():
    if current_user.role != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    data = request.get_json()
    if not data or not data.get('products'):
        return jsonify({'success': False, 'error': 'No data received'}), 400
    try:
        OrderItem.query.delete()
        Order.query.delete()
        Product.query.delete()
        db.session.flush()
        imported = 0
        for row in data['products'][:500]:
            try:
                name = str(row.get('name', '')).strip()
                category = str(row.get('category', '')).strip()
                if not name or not category:
                    continue
                price = float(str(row.get('price', '0')).replace(',', '').strip() or 0)
                stock = int(float(str(row.get('stock', '0')).replace(',', '').strip() or 0))
                db.session.add(Product(
                    barcode=str(row.get('barcode', '')).strip() or None,
                    name=name, category=category, price=price, stock=stock,
                    description=str(row.get('description', '')).strip() or None
                ))
                imported += 1
            except:
                continue
        db.session.commit()
        return jsonify({'success': True, 'count': imported})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# Notification routes

@admin_app.route('/api/notifications')
@login_required
@require_admin_or_staff
def get_notifications():
    notifs = Notification.query.filter_by(user_id=current_user.id)\
                               .order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify([{
        'id': n.id, 'title': n.title, 'message': n.message,
        'type': n.type, 'status': n.status, 'is_read': n.is_read,
        'created_at': n.created_at.strftime('%Y-%m-%dT%H:%M:%S+08:00')
    } for n in notifs])


@admin_app.route('/api/notifications/<int:nid>/read', methods=['POST'])
@login_required
@require_admin_or_staff
def read_notification(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    n.is_read = True
    db.session.commit()
    return jsonify({'success': True})


@admin_app.route('/api/notifications/read-all', methods=['POST'])
@login_required
@require_admin_or_staff
def read_all_notifications():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})


@admin_app.route('/api/notifications/<int:nid>/delete', methods=['POST'])
@login_required
@require_admin_or_staff
def delete_notification(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    db.session.delete(n)
    db.session.commit()
    return jsonify({'success': True})


# Quotation routes

@admin_app.route('/pos')
@login_required
def pos():
    return redirect(url_for('quotation_new'))


@admin_app.route('/quotation/new')
@login_required
@require_admin_or_staff
def quotation_new():
    products = Product.query.filter(Product.stock > 0).order_by(Product.category, Product.name).all()
    services = Service.query.filter_by(is_active=True).order_by(Service.name).all()
    mechanics = Mechanic.query.order_by(Mechanic.name).all()
    booking = None
    booking_id = request.args.get('booking_id', type=int)
    if booking_id:
        b = Booking.query.get(booking_id)
        if b:
            customer_name = b.contact_name if b.walkin_customer_id else (b.customer.fullname if b.customer else '')
            customer_phone = b.contact_mobile if b.walkin_customer_id else (b.customer.phone if b.customer else '')
            # Inspected By is read from the booking, never typed — whoever's
            # actually assigned to the job is who inspects it. The "customer
            # chose this person" tag is only true when the assignment still
            # matches what they originally asked for; any other case (shop
            # picked, or picked someone else because the request wasn't free)
            # reads as "assigned by the shop".
            booking = {
                'id': b.id, 'ref': f'BKG-{b.id:03d}',
                'customer_name': customer_name or '',
                'customer_phone': customer_phone or '',
                'motorcycle_model': b.motorcycle_model or '',
                'motorcycle_plate': b.motorcycle_plate or '',
                'service_name': b.service,
                'service_price': booking_service_price(b.service),
                'is_backjob': b.service.startswith(BACKJOB_PREFIX),
                'regular_price': booking_service_price(b.service[len(BACKJOB_PREFIX):]) if b.service.startswith(BACKJOB_PREFIX) else 0,
                'assigned_mechanic_name': b.assigned_mechanic_name,
                'assigned_mechanic_specialization': b.assigned_mechanic_specialization,
                'assigned_mechanic_initials': _mechanic_initials(b.assigned_mechanic_name),
                'mechanic_is_customer_choice': bool(b.preferred_mechanic_name) and b.preferred_mechanic_name == b.assigned_mechanic_name,
            }
    return render_template('quotation.html', products=products, services=services,
                            mechanics=mechanics, booking=booking)


@admin_app.route('/api/quotation/evidence', methods=['POST'])
@login_required
@require_admin_or_staff
def quotation_evidence_upload():
    """One photo for a back job's 'not covered' finding — images only, up to 5MB."""
    f = request.files.get('photo')
    if not f or not f.filename or not allowed_file(f.filename) or not (f.mimetype or '').startswith('image/'):
        return jsonify({'success': False, 'error': 'Upload an image file.'}), 400
    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    if size > 5 * 1024 * 1024:
        return jsonify({'success': False, 'error': 'That image is over 5MB.'}), 400
    cloud_url = upload_image(f, 'return_evidence')
    if cloud_url:
        return jsonify({'success': True, 'photo': cloud_url})
    ext = f.filename.rsplit('.', 1)[1].lower()
    name = f'notcovered_{uuid.uuid4().hex}.{ext}'
    folder = os.path.join(admin_app.root_path, 'static', 'return_evidence')
    os.makedirs(folder, exist_ok=True)
    f.save(os.path.join(folder, name))
    return jsonify({'success': True, 'photo': name})


@admin_app.route('/api/quotation/save', methods=['POST'])
@login_required
@require_admin_or_staff
def save_quotation():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data'}), 400
    items = data.get('items', [])
    if not items:
        return jsonify({'success': False, 'error': 'No items in quotation'}), 400
    total = sum(float(i['unit_price']) * int(i['quantity']) for i in items)
    booking_id = data.get('booking_id')
    booking = Booking.query.get(booking_id) if booking_id else None

    # Who inspected it is read from the booking's own assigned mechanic, not
    # whatever the client sent — the admin never types this, so there's
    # nothing to trust (or distrust) in the payload for it. A booking-linked
    # quotation can't be sent at all until someone's assigned.
    if booking:
        if not booking.assigned_mechanic_name:
            return jsonify({'success': False, 'error': 'Assign a mechanic before sending the quotation.'}), 400
        inspected_by = booking.assigned_mechanic_name
    else:
        inspected_by = (data.get('inspected_by') or '').strip() or None

    # Back job found NOT to be the shop's fault: the mechanic must say why and
    # attach photos — no reason or no photo, no quotation.
    nc = data.get('not_covered') if (booking and booking.service.startswith(BACKJOB_PREFIX)) else None
    nc_reason, nc_photos = None, []
    if nc:
        nc_reason = clean_str(nc.get('reason', ''), max_len=500)
        nc_photos = [clean_str(x, max_len=300) for x in (nc.get('photos') or []) if x][:5]
        if not nc_reason or not nc_photos:
            return jsonify({'success': False, 'error': "A problem that isn't covered needs a reason and at least one photo from the Inspection tab."}), 400

    q = Quotation(
        not_covered      = bool(nc),
        not_covered_reason = nc_reason,
        not_covered_photos = ','.join(nc_photos) or None,
        customer_name    = data.get('customer_name', '').strip(),
        customer_phone   = data.get('customer_phone', '').strip(),
        motorcycle_model = data.get('motorcycle_model', '').strip(),
        motorcycle_plate = data.get('motorcycle_plate', '').strip(),
        notes            = data.get('notes', '').strip(),
        total_amount     = total,
        status           = 'awaiting_customer' if booking else 'pending',
        booking_id       = booking.id if booking else None,
        inspected_by     = inspected_by,
        created_by       = current_user.id,
    )
    db.session.add(q)
    db.session.flush()
    for i in items:
        db.session.add(QuotationItem(
            quotation_id = q.id,
            item_type    = i.get('item_type', 'service'),
            name         = i['name'],
            quantity     = int(i['quantity']),
            unit_price   = float(i['unit_price']),
            locked       = bool(i.get('locked')),
        ))
    db.session.commit()

    if booking:
        ref = f'BKG-{booking.id:03d}'
        breakdown = _quotation_breakdown(items)
        decline_note = ' (service + materials)' if breakdown['materials'] else ' (booked service only)'
        title = 'Quotation ready: please respond'
        message = f'During inspection of your {booking.service} ({ref}), the mechanic found additional work.'
        if nc_reason:
            message = (f'During inspection of your {booking.service} ({ref}), the mechanic found the problem is not covered '
                       f'by the back job: {nc_reason}')
        send_notification(booking.user_id, title, message, type='quotation', status='awaiting_customer',
                           booking_id=booking.id, priority=True)

        customer = User.query.get(booking.user_id)
        if customer and customer.email:
            rows = ''.join(
                f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;">{i["name"]}</td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;white-space:nowrap;">'
                f'₱{float(i["unit_price"]):,.2f}</td></tr>'
                for i in items
            )
            html = f"""
            <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;">
              <p>Hi {customer.fullname},</p>
              <p>{message} Please review and approve or decline:</p>
              <table style="width:100%;border-collapse:collapse;margin:12px 0;">{rows}</table>
              <p>If you approve: <strong>₱{total:,.2f}</strong><br>
                 If you decline: <strong>₱{breakdown['decline_total']:,.2f}</strong>{decline_note}</p>
              <p><a href="{BASE_URL}/customer/dashboard?open_booking={booking.id}"
                    style="display:inline-block;padding:10px 18px;background:#d00019;color:#fff;
                           border-radius:6px;text-decoration:none;font-weight:bold;">Review Quotation</a></p>
              <p>— MotoTyres North Caloocan</p>
            </div>"""
            _send_gmail(customer.email, f'{title} — MotoTyres', html)

    customer_first_name = (q.customer_name or '').split(' ')[0] or None
    return jsonify({'success': True, 'quotation_id': q.id, 'ref': f'QUO-{q.id:03d}',
                     'inspected_by': inspected_by, 'customer_first_name': customer_first_name})


@admin_app.route('/quotations')
@login_required
@require_admin_or_staff
def quotations_list():
    """Two separate worlds under one page: booking-linked quotations from
    Inspection & Estimation (the customer answers remotely, in their own
    notifications) and walk-in quotations (the customer answers at the
    counter, so the admin records it here). They use different status
    vocabularies and never cross over — a booking quotation never becomes a
    Job Order, a walk-in one never touches a booking."""
    section = request.args.get('section', 'online')
    if section not in ('online', 'walkin'):
        section = 'online'
    status_filter = request.args.get('status', 'all')

    online_base = Quotation.query.filter(Quotation.booking_id.isnot(None))
    walkin_base = Quotation.query.filter(Quotation.booking_id.is_(None))

    def count(base, values):
        return base.filter(Quotation.status.in_(values)).count()

    online_counts = {
        'awaiting_customer': count(online_base, ['awaiting_customer']),
        'approved':          count(online_base, ['approved']),
        'declined':          count(online_base, ['declined']),
    }
    walkin_counts = {
        'pending':  count(walkin_base, ['pending']),
        'approved': count(walkin_base, ['approved', 'accepted']),
        'declined': count(walkin_base, ['declined', 'rejected']),
    }
    online_total = online_base.count()
    walkin_total = walkin_base.count()

    base = online_base if section == 'online' else walkin_base
    status_values = {
        'online': {'awaiting_customer': ['awaiting_customer'], 'approved': ['approved'], 'declined': ['declined']},
        'walkin': {'pending': ['pending'], 'approved': ['approved', 'accepted'], 'declined': ['declined', 'rejected']},
    }[section]
    if status_filter != 'all' and status_filter in status_values:
        base = base.filter(Quotation.status.in_(status_values[status_filter]))
    quotations = base.order_by(Quotation.created_at.desc()).all()

    booking_ids = {q.booking_id for q in quotations if q.booking_id}
    bookings_by_id = {b.id: b for b in Booking.query.filter(Booking.id.in_(booking_ids)).all()} if booking_ids else {}
    quot_ids = [q.id for q in quotations]
    jo_by_quot = {}
    if quot_ids:
        for jo in JobOrder.query.filter(JobOrder.quotation_id.in_(quot_ids)).all():
            jo_by_quot[jo.quotation_id] = jo
    for q in quotations:
        q.linked_booking = bookings_by_id.get(q.booking_id) if q.booking_id else None
        q.linked_jo = jo_by_quot.get(q.id)

    return render_template('quotations_list.html', quotations=quotations, section=section,
                           status_filter=status_filter, online_total=online_total, walkin_total=walkin_total,
                           online_counts=online_counts, walkin_counts=walkin_counts)


@admin_app.route('/quotation/<int:qid>/withdraw', methods=['POST'])
@login_required
@require_admin_or_staff
def withdraw_quotation(qid):
    """Pulls back a booking quotation before the customer has answered —
    the booking returns to Inspection & Estimation as if it was never sent,
    so the shop can redo it (wrong price, missing item, etc.)."""
    quot = Quotation.query.get_or_404(qid)
    if not quot.booking_id:
        return jsonify({'success': False, 'error': 'Only booking quotations can be withdrawn.'}), 400
    if quot.status != 'awaiting_customer':
        return jsonify({'success': False, 'error': 'This quotation can no longer be withdrawn.'}), 400
    booking_id = quot.booking_id
    QuotationItem.query.filter_by(quotation_id=quot.id).delete()
    db.session.delete(quot)
    Notification.query.filter_by(booking_id=booking_id, type='quotation').delete()
    db.session.commit()
    return jsonify({'success': True})


@admin_app.route('/quotation/<int:qid>/approve', methods=['POST'])
@login_required
@require_admin_or_staff
def approve_walkin_quotation(qid):
    """The counter-side equivalent of the customer's own approve/decline —
    a walk-in quotation has no app for the customer to answer in, so the
    admin records what they said in person. Approving doesn't create the Job
    Order by itself; that's a separate, deliberate 'Convert to JO' step."""
    quot = Quotation.query.get_or_404(qid)
    if quot.booking_id:
        return jsonify({'success': False, 'error': 'Booking-linked quotations are answered by the customer.'}), 400
    if quot.status != 'pending':
        return jsonify({'success': False, 'error': 'This quotation has already been decided.'}), 400
    quot.status = 'approved'
    quot.decided_at = ph_now()
    db.session.commit()
    return jsonify({'success': True})


@admin_app.route('/quotation/<int:qid>/convert', methods=['POST'])
@login_required
@require_admin_or_staff
def convert_quotation(qid):
    quot = Quotation.query.get_or_404(qid)
    if quot.status not in ('approved', 'accepted'):
        flash('Only approved quotations can be converted.', 'danger')
        return redirect(url_for('quotations_list'))
    if JobOrder.query.filter_by(quotation_id=quot.id).first():
        flash('This quotation already has a Job Order.', 'danger')
        return redirect(url_for('quotations_list'))
    jo = JobOrder(
        quotation_id     = quot.id,
        customer_name    = quot.customer_name,
        customer_phone   = quot.customer_phone,
        motorcycle_model = quot.motorcycle_model,
        motorcycle_plate = quot.motorcycle_plate,
        notes            = quot.notes,
        total_amount     = quot.total_amount,
        status           = 'pending',
        created_by       = current_user.id,
    )
    db.session.add(jo)
    db.session.flush()
    for qi in quot.items:
        db.session.add(JobOrderItem(
            job_order_id = jo.id,
            item_type    = qi.item_type,
            name         = qi.name,
            quantity     = qi.quantity,
            unit_price   = qi.unit_price,
        ))
    # Status stays 'approved' — whether it already has a Job Order is read
    # fresh from JobOrder.quotation_id, not tracked as a separate status.
    db.session.commit()
    flash(f'Quotation QUO-{quot.id:03d} converted to Job Order JO-{jo.id:03d}.', 'success')
    return redirect(url_for('job_orders'))


@admin_app.route('/quotation/<int:qid>/reject', methods=['POST'])
@login_required
@require_admin_or_staff
def reject_quotation(qid):
    quot = Quotation.query.get_or_404(qid)
    if quot.booking_id:
        return jsonify({'success': False, 'error': 'Booking-linked quotations are answered by the customer.'}), 400
    if quot.status != 'pending':
        return jsonify({'success': False, 'error': 'This quotation has already been decided.'}), 400
    quot.status = 'declined'
    quot.decided_at = ph_now()
    db.session.commit()
    return jsonify({'success': True})


# Job Order routes

@admin_app.route('/job-orders')
@login_required
@require_admin_or_staff
def job_orders():
    new_id = request.args.get('new', type=int)
    status_filter = 'all' if new_id else request.args.get('status', 'all')
    q = JobOrder.query
    if status_filter != 'all':
        q = q.filter_by(status=status_filter)
    orders = q.order_by(JobOrder.created_at.desc()).all()
    mechanics = Mechanic.query.order_by(Mechanic.name).all()
    new_jo = JobOrder.query.get(new_id) if new_id else None

    now = ph_now()
    # Per walk-in job: who it ties up, until when, and whether they're still held.
    jo_busy = {}
    for jo in orders:
        if not (jo.mechanic_name and jo.start_at):
            continue
        start = jo.start_at.hour * 60 + jo.start_at.minute
        est = jo.est_minutes or 30
        jo_busy[jo.id] = {
            'active': bool(jo.status in ('pending', 'in_progress') and not jo.mechanic_done_at and jo.start_at.date() == now.date()),
            'started': minutes_to_ampm(start), 'until': minutes_to_ampm(start + est), 'est': format_duration(est),
            'first': jo.mechanic_name.split(' ')[0],
            'freed_at': jo.mechanic_done_at.strftime('%I:%M %p').lstrip('0') if jo.mechanic_done_at else None,
        }

    # "Right now" slot summary: the next few 30-minute slots that have a walk-in in them.
    _, _, on_duty = get_on_duty_mechanics(now.date())
    windows = _walkin_windows(now.date())
    today_bookings = Booking.query.filter(Booking.date == now.date(), Booking.assigned_mechanic_name.isnot(None),
                                          Booking.status.notin_(['cancelled', 'completed'])).all()
    today_slots = []
    slot_start = (now.hour * 60 + now.minute) // 30 * 30
    for s0 in range(slot_start, slot_start + 6 * 30, 30):
        s1 = s0 + 30
        rows, busy_names = [], set()
        for w in windows:
            if mechanic_overlaps(s0, s1, w['start'], w['end']):
                rows.append({'name': w['mechanic'], 'range': f"{minutes_to_ampm(w['start'])} – {minutes_to_ampm(w['end'])}",
                             'kind': 'Walk-in', 'ref': w['ref']})
                busy_names.add(w['mechanic'])
        if not rows:
            continue
        for b in today_bookings:
            b_start = b.time.hour * 60 + b.time.minute
            b_end = real_end_minutes(b_start, b.duration_minutes or DEFAULT_DURATION_MIN, b.overrun_minutes or 0)
            if mechanic_overlaps(s0, s1, b_start, b_end):
                rows.append({'name': b.assigned_mechanic_name, 'range': f'{minutes_to_ampm(b_start)} – {minutes_to_ampm(b_end)}',
                             'kind': 'Online', 'ref': f'BKG-{b.id:03d}'})
                busy_names.add(b.assigned_mechanic_name)
        today_slots.append({'label': minutes_to_ampm(s0), 'free': max(len(on_duty) - len(busy_names), 0),
                            'total': len(on_duty), 'rows': rows})
    return render_template('job_orders.html', job_orders=orders, mechanics=mechanics,
                           status_filter=status_filter, new_jo=new_jo, jo_busy=jo_busy,
                           today_slots=today_slots, today_label=now.strftime('%A, %B %d, %Y'))


@admin_app.route('/job-order/<int:jid>/status', methods=['POST'])
@login_required
@require_admin_or_staff
def update_job_order_status(jid):
    jo = JobOrder.query.get_or_404(jid)
    new_status = request.form.get('status', '')
    transitions = {
        'pending':     ['in_progress', 'cancelled'],
        'in_progress': ['cancelled'],
    }
    if jo.status in ['completed', 'cancelled']:
        return jsonify({'success': False, 'error': 'Status is final and cannot be changed'}), 400
    if new_status not in transitions.get(jo.status, []):
        return jsonify({'success': False, 'error': 'Invalid status transition'}), 400
    jo.status = new_status
    if new_status == 'completed':
        jo.completed_at = datetime.utcnow() + timedelta(hours=8)
    mechanic_name = request.form.get('mechanic_name')
    if mechanic_name is not None:
        jo.mechanic_name = mechanic_name.strip() or None
    db.session.commit()
    return jsonify({'success': True, 'status': jo.status})


def _billing_new_count(user):
    """Payments completed since this admin last opened Billing History, across
    bookings, online orders, in-store sales and job orders — the same rules
    History itself uses, so the badge and the list never disagree. The first
    time an admin is seen there's nothing to compare against, so it starts at 0."""
    seen = getattr(user, 'billing_seen_at', None)
    if seen is None:
        user.billing_seen_at = ph_now()
        db.session.commit()
        return 0
    quoted = {r[0] for r in db.session.query(Quotation.booking_id).filter(Quotation.booking_id.isnot(None)).all()}
    bq = Booking.query.filter(
        Booking.status == 'completed',
        db.or_(Booking.total_amount > 0, Booking.service.like(BACKJOB_PREFIX + '%')),
        Booking.is_archived == False, Booking.walkin_customer_id == None,
        func.coalesce(Booking.invoice_paid_at, Booking.completed_at, Booking.created_at) > seen)
    if quoted:
        bq = bq.filter(db.or_(~Booking.id.in_(quoted), Booking.invoice_paid == True))
    oq = (Order.query.filter(Order.payment_method.in_(['cash', 'gcash']), Order.status == 'completed')
          .filter(Order.items.any())
          .filter(func.coalesce(Order.delivered_at, Order.created_at) > seen))
    jq = JobOrder.query.join(Payment, Payment.job_order_id == JobOrder.id).filter(Payment.paid_at > seen)
    return bq.count() + oq.count() + jq.count()


@admin_app.route('/payments')
@login_required
@require_admin_or_staff
def payments():
    tab = request.args.get('tab', 'pending')

    # Bookings that went through Inspection & Estimation have a Quotation row
    # — that's what separates "new" invoice-flow completions (billed via the
    # Outbox + this page's Record Cash) from "old" on-the-spot completions
    # (billed entirely here in one step, no separate invoice ever sent).
    _quoted_booking_ids = {r[0] for r in db.session.query(Quotation.booking_id)
                           .filter(Quotation.booking_id.isnot(None)).all()}
    _new_instore_ids = []

    if tab == 'pending':
        paid_jo_ids = [r[0] for r in db.session.query(Payment.job_order_id).filter(Payment.job_order_id != None).all()]
        pending_jos = JobOrder.query.filter(
            JobOrder.status == 'in_progress',
            ~JobOrder.id.in_(paid_jo_ids) if paid_jo_ids else True
        ).order_by(JobOrder.created_at.desc()).all()

        # Only bookings the mechanic has actually started — a merely-confirmed
        # booking has no work done yet, so there's nothing to bill for it.
        pending_bookings = Booking.query.filter(
            Booking.status.in_(['in_progress', 'inprogress']),
            Booking.payment_method.in_(['cash', None]),
            Booking.walkin_customer_id == None,
            Booking.is_archived == False,
            ~Booking.service.like(BACKJOB_PREFIX + '%'),   # free back jobs close from Claim Review
        ).all()
        for b in pending_bookings:
            b.is_invoice_flow = False

        # Completed-but-unpaid invoices from the Inspection & Estimation flow
        # — the customer may or may not have picked a method yet.
        pending_invoices = Booking.query.filter(
            Booking.status == 'completed',
            Booking.invoice_paid == False,
            Booking.walkin_customer_id == None,
            Booking.is_archived == False,
            Booking.id.in_(_quoted_booking_ids) if _quoted_booking_ids else False,
        ).all()
        _inv_quotations = {}
        if pending_invoices:
            for q in Quotation.query.filter(Quotation.booking_id.in_([b.id for b in pending_invoices])) \
                                     .order_by(Quotation.created_at.asc()).all():
                _inv_quotations[q.booking_id] = q
        for b in pending_invoices:
            b.is_invoice_flow = True
            q = _inv_quotations.get(b.id)
            b.extra_item_count = len([i for i in q.items if not i.locked]) if (q and q.status == 'approved') else 0

        pending_bookings = sorted(
            pending_bookings + pending_invoices,
            key=lambda b: b.completed_at or b.created_at, reverse=True,
        )

        # The item has to actually be ready before there's anything to do here —
        # cash still needs the payment collected, GCash (prepaid) just needs to
        # be handed over, but a merely-confirmed order isn't prepared yet and
        # has nothing to act on.
        pending_orders = Order.query.filter(
            Order.delivery_method == 'pickup',
            Order.status == 'shipped',
            Order.walkin_customer_id == None,
            Order.is_archived == False,
        ).filter(Order.items.any()).order_by(Order.created_at.desc()).all()

        history_jos      = []
        history_bookings = []
        history_orders   = []
        new_bkg_ids, new_ord_ids, new_jo_ids = set(), set(), set()
    else:
        pending_jos      = []
        pending_bookings = []
        pending_orders   = []

        history_jos = (JobOrder.query
                       .join(Payment, Payment.job_order_id == JobOrder.id)
                       .order_by(Payment.paid_at.desc()).limit(50).all())

        _booking_history_q = Booking.query.filter(
            Booking.status == 'completed',
            db.or_(Booking.total_amount > 0, Booking.service.like(BACKJOB_PREFIX + '%')),
            Booking.is_archived == False, Booking.walkin_customer_id == None)
        if _quoted_booking_ids:
            _booking_history_q = _booking_history_q.filter(
                db.or_(~Booking.id.in_(_quoted_booking_ids), Booking.invoice_paid == True))
        history_bookings = _booking_history_q.order_by(
            func.coalesce(Booking.invoice_paid_at, Booking.completed_at, Booking.created_at).desc()
        ).limit(50).all()
        _hist_booking_ids = [b.id for b in history_bookings]
        _hist_quotations = {}
        if _hist_booking_ids:
            for q in Quotation.query.filter(Quotation.booking_id.in_(_hist_booking_ids)) \
                                     .order_by(Quotation.created_at.asc()).all():
                _hist_quotations[q.booking_id] = q
        for b in history_bookings:
            q = _hist_quotations.get(b.id)
            b.extra_item_count = len([i for i in q.items if not i.locked]) if (q and q.status == 'approved') else 0
            b.paid_at_display = b.invoice_paid_at or b.completed_at or b.created_at
            # Back jobs: free unless the customer approved extra work — then show the split.
            b.is_backjob = b.service.startswith(BACKJOB_PREFIX)
            b.bj_state, b.bj_free, b.bj_work, b.bj_mat = None, 0, 0, 0
            if b.is_backjob:
                if q and q.status == 'approved':
                    _bd = _quotation_breakdown(q.items)
                    b.bj_state, b.bj_free, b.bj_work, b.bj_mat = 'approved', _bd['booked'], _bd['extra'], _bd['materials']
                elif q and q.status == 'declined':
                    b.bj_state = 'declined'
                else:
                    b.bj_state = 'none'

        history_orders = (Order.query
                          .filter(Order.payment_method.in_(['cash', 'gcash']), Order.status == 'completed')
                          .filter(Order.items.any())
                          .order_by(func.coalesce(Order.delivered_at, Order.created_at).desc()).limit(50).all())

        # NEW = completed since this admin last opened History. The tags stay for
        # this visit, then clear: opening History stamps "now" as the new baseline.
        seen_before = current_user.billing_seen_at or ph_now()
        new_bkg_ids = {b.id for b in history_bookings if b.paid_at_display > seen_before}
        new_ord_ids = {o.id for o in history_orders if (o.delivered_at or o.created_at) > seen_before}
        new_jo_ids = {jo.id for jo in history_jos if jo.payment and jo.payment.paid_at > seen_before}
        current_user.billing_seen_at = ph_now()
        db.session.commit()

        # Rows the admin hasn't opened History since — highlighted once, then
        # marked seen so the badge and highlight both clear together.
        _new_instore_ids = [o.id for o in history_orders if o.is_instore and not o.instore_seen]
        if _new_instore_ids:
            Order.query.filter(Order.id.in_(_new_instore_ids)).update(
                {Order.instore_seen: True}, synchronize_session=False)
            db.session.commit()

    _counter_cash_count = Booking.query.filter(
        Booking.status == 'completed', Booking.invoice_paid == False,
        Booking.invoice_payment_method == 'cash', Booking.is_archived == False,
    ).count()

    _booking_history_count_q = Booking.query.filter(
        Booking.status == 'completed',
        db.or_(Booking.total_amount > 0, Booking.service.like(BACKJOB_PREFIX + '%')),
        Booking.is_archived == False, Booking.walkin_customer_id == None)
    if _quoted_booking_ids:
        _booking_history_count_q = _booking_history_count_q.filter(
            db.or_(~Booking.id.in_(_quoted_booking_ids), Booking.invoice_paid == True))
    _history_count = (
        JobOrder.query.join(Payment, Payment.job_order_id == JobOrder.id).count()
        + _booking_history_count_q.count()
        + Order.query.filter(Order.payment_method.in_(['cash', 'gcash']), Order.status == 'completed')
                     .filter(Order.items.any()).count()
    )
    # Recomputed after the baseline moves, so opening History reads 0.
    _instore_unseen_count = _billing_new_count(current_user)

    # ── Recent Transactions panel: the latest payments across every type, on
    # both tabs. Same inclusion rules as History (so the two never disagree);
    # orders carry their receipt data so a click can open it without a fetch.
    _recent = []
    for pay in Payment.query.order_by(Payment.paid_at.desc()).limit(100).all():
        jo = pay.job_order
        if not jo:
            continue
        _recent.append({'tag': 'JO', 'kind': 'jo', 'id': pay.id, 'ref': f'JO-{jo.id:03d}', 'customer': jo.customer_name,
                        'amount': pay.amount, 'method': (pay.payment_method or 'cash').lower(), 'paid_at': pay.paid_at,
                        'receipt_url': url_for('payment_receipt', pid=pay.id)})
    _rq = Booking.query.filter(Booking.status == 'completed',
                               db.or_(Booking.total_amount > 0, Booking.service.like(BACKJOB_PREFIX + '%')),
                               Booking.is_archived == False, Booking.walkin_customer_id == None)
    if _quoted_booking_ids:
        _rq = _rq.filter(db.or_(~Booking.id.in_(_quoted_booking_ids), Booking.invoice_paid == True))
    for b in _rq.order_by(func.coalesce(Booking.invoice_paid_at, Booking.completed_at, Booking.created_at).desc()).limit(100).all():
        _recent.append({'tag': 'BKG', 'kind': 'bkg', 'id': b.id, 'ref': f'BKG-{b.id:03d}',
                        'customer': b.contact_name or (b.customer.fullname if b.customer else 'Customer'),
                        'amount': b.total_amount, 'method': (b.invoice_payment_method or b.payment_method or 'cash').lower(),
                        'paid_at': b.invoice_paid_at or b.completed_at or b.created_at})
    for o in (Order.query.filter(Order.payment_method.in_(['cash', 'gcash']), Order.status == 'completed')
              .filter(Order.items.any()).order_by(Order.created_at.desc()).limit(100).all()):
        _name = o.contact_name if o.is_instore else o.customer.fullname
        _ref = f'SALE-{o.id:03d}' if o.is_instore else f'ORD-{o.id:03d}'
        _ship = (o.delivery_method == 'ship' and not o.is_instore)
        _amt = (o.total_amount or 0) + ((o.shipping_fee or 0) if _ship else 0)       # what was really paid
        _when = (o.paid_at if _ship and o.paid_at else None) or o.delivered_at or o.created_at
        _items = [{'name': (i.product.name if i.product else 'Unknown'), 'qty': i.quantity, 'price': i.unit_price} for i in o.items]
        if _ship and o.shipping_fee:
            _items.append({'name': f"Shipping fee ({(o.shipping_option or '').title()} \u00b7 Lalamove {o.shipping_vehicle or ''})", 'qty': 1, 'price': o.shipping_fee})
        _recent.append({'tag': 'SALE' if o.is_instore else 'ORD', 'kind': 'ord', 'id': o.id, 'ref': _ref, 'customer': _name,
                        'amount': _amt, 'method': (o.payment_method or 'cash').lower(), 'paid_at': _when,
                        'ord': {'ref': _ref, 'customer': _name, 'phone': '' if o.is_instore else (o.customer.phone or ''),
                                'amount': _amt, 'method': o.payment_method,
                                'date': _when.strftime('%b %d, %Y %I:%M %p'),
                                'items': _items,
                                'delivery': o.delivery_method, 'is_instore': bool(o.is_instore)}})
    _recent.sort(key=lambda r: r['paid_at'], reverse=True)
    _today_d = ph_now().date()
    _today_rows = [r for r in _recent if r['paid_at'].date() == _today_d]
    recent_today_total = sum(r['amount'] or 0 for r in _today_rows)
    recent_today_count = len(_today_rows)
    recent_groups = []
    for r in _recent[:6]:
        dd = r['paid_at'].date()
        label = 'Today' if dd == _today_d else ('Yesterday' if dd == _today_d - timedelta(days=1) else dd.strftime('%b %d, %Y'))
        r['time'] = r['paid_at'].strftime('%I:%M %p')
        if not recent_groups or recent_groups[-1]['label'] != label:
            recent_groups.append({'label': label, 'items': []})
        recent_groups[-1]['items'].append(r)

    return render_template('payments.html',
        tab=tab,
        pending_jos=pending_jos,
        pending_bookings=pending_bookings,
        pending_orders=pending_orders,
        history_jos=history_jos,
        history_bookings=history_bookings,
        history_orders=history_orders,
        counter_cash_count=_counter_cash_count,
        recent_groups=recent_groups,
        recent_today_total=recent_today_total,
        recent_today_count=recent_today_count,
        history_count=_history_count,
        instore_unseen_count=_instore_unseen_count,
        new_instore_ids=_new_instore_ids,
        new_bkg_ids=new_bkg_ids, new_ord_ids=new_ord_ids, new_jo_ids=new_jo_ids,
        all_services=Service.query.filter_by(is_active=True).all())


@admin_app.route('/payments/process/<int:joid>', methods=['GET'])
@login_required
@require_admin_or_staff
def payment_process(joid):
    jo = JobOrder.query.get_or_404(joid)
    if jo.payment:
        flash('This job order has already been paid.', 'warning')
        return redirect(url_for('payments'))
    if jo.status not in ('pending', 'in_progress'):
        flash('Only pending or in-progress job orders can be paid.', 'danger')
        return redirect(url_for('payments'))
    return render_template('payment_process.html', jo=jo)


@admin_app.route('/payments/process/<int:joid>', methods=['POST'])
@login_required
@require_admin_or_staff
def payment_submit(joid):
    jo = JobOrder.query.get_or_404(joid)
    if jo.payment:
        return jsonify({'success': False, 'error': 'Already paid'}), 400
    method    = request.form.get('payment_method', 'cash')
    ref_no    = request.form.get('reference_no', '').strip() or None
    if method not in ('cash', 'gcash'):
        return jsonify({'success': False, 'error': 'Invalid payment method'}), 400
    if method == 'gcash' and not ref_no:
        return jsonify({'success': False, 'error': 'GCash reference number is required'}), 400
    try:
        pmt = Payment(
            job_order_id   = jo.id,
            amount         = jo.total_amount,
            payment_method = method,
            reference_no   = ref_no,
            created_by     = current_user.id,
        )
        db.session.add(pmt)
        jo.status       = 'completed'
        jo.completed_at = datetime.utcnow() + timedelta(hours=8)
        db.session.commit()
        return jsonify({'success': True, 'ref': f'JO-{jo.id:03d}', 'amount': jo.total_amount,
                        'method': method, 'payment_id': pmt.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    
@admin_app.route('/payments/booking/<int:bid>', methods=['GET'])
@login_required
@require_admin_or_staff
def booking_payment_process(bid):
    booking = Booking.query.get_or_404(bid)
    # Sums every service in a combo booking — a plain name match would miss those.
    service_price = booking_service_price(booking.service)
    return render_template('booking_payment_process.html', booking=booking, service_price=service_price)


@admin_app.route('/payments/booking/<int:bid>', methods=['POST'])
@login_required
@require_admin_or_staff
def booking_payment_submit(bid):
    booking = Booking.query.get_or_404(bid)
    method  = request.form.get('payment_method', 'cash')
    ref_no  = request.form.get('reference_no', '').strip() or None
    amount  = request.form.get('amount', '0')

    if method not in ('cash', 'gcash'):
        return jsonify({'success': False, 'error': 'Invalid payment method'}), 400
    if method == 'gcash' and not ref_no:
        return jsonify({'success': False, 'error': 'GCash reference number is required'}), 400

    try:
        amount_f = float(amount)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid amount'}), 400

    try:
        booking.status         = 'completed'
        if not booking.completed_at:
            booking.completed_at = ph_now()
        booking.payment_method = method
        booking.total_amount   = amount_f
        db.session.commit()

        # Notify customer
        send_notification(
            booking.user_id,
            'Service Completed! 🎉',
            f'Your {booking.service} on {booking.date.strftime("%b %d, %Y")} has been completed and paid. Thank you!',
            type='booking', status='completed'
        )

        return jsonify({
            'success':   True,
            'ref':       f'BKG-{booking.id:03d}',
            'amount':    amount_f,
            'method':    method,
            'booking_id': booking.id
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_app.route('/payments/receipt/<int:pid>')
@login_required
@require_admin_or_staff
def payment_receipt(pid):
    pmt = Payment.query.get_or_404(pid)
    return render_template('payment_receipt.html', pmt=pmt, jo=pmt.job_order)


@admin_app.route('/billing/order/<int:oid>/complete', methods=['POST'])
@login_required
@require_admin_or_staff
def billing_order_complete(oid):
    order = Order.query.get_or_404(oid)
    if order.status == 'completed':
        return jsonify({'success': False, 'error': 'Order already completed'}), 400
    if order.delivery_method != 'pickup':
        return jsonify({'success': False, 'error': 'Only pick-up orders can be billed here'}), 400
    is_cash = (order.payment_method or 'cash') == 'cash'
    order.status = 'completed'
    if not order.delivered_at:
        order.delivered_at = ph_now()
    db.session.commit()
    body = (f'Your order ORD-{order.id:03d} has been picked up and payment collected. Thank you!' if is_cash
            else f'Your order ORD-{order.id:03d} has been picked up. Thank you!')
    _notify_order_customer(order, 'Order Completed!', body, status='completed')
    return jsonify({'success': True, 'ref': f'ORD-{order.id:03d}', 'amount': order.total_amount})


def _get_app_state(key, default=None):
    row = AppState.query.get(key)
    return row.value if row else default


def _set_app_state(key, value):
    row = AppState.query.get(key)
    if row:
        row.value = value
    else:
        db.session.add(AppState(key=key, value=value))


@admin_app.route('/api/transactions')
@login_required
@require_admin_or_staff
def api_transactions():
    txns = []
    _last_viewed_raw = _get_app_state('last_viewed_transactions_at')
    try:
        _last_viewed = datetime.fromisoformat(_last_viewed_raw) if _last_viewed_raw else None
    except ValueError:
        _last_viewed = None

    paid_jos = JobOrder.query.join(Payment, Payment.job_order_id == JobOrder.id).all()
    for jo in paid_jos:
        txns.append({
            '_dt': jo.payment.paid_at,
            'ref': f'JO-{jo.id:03d}',
            'type': 'Job Order',
            'customer': jo.customer_name or 'Unknown',
            'payment': (jo.payment.payment_method or 'cash').upper(),
            'status': 'Paid',
            'total': jo.total_amount,
        })

    sale_orders = Order.query.filter(
        Order.walkin_customer_id != None, Order.status == 'completed'
    ).filter(Order.items.any()).all()
    for o in sale_orders:
        wc = WalkInCustomer.query.get(o.walkin_customer_id) if o.walkin_customer_id else None
        txns.append({
            '_dt': o.created_at,
            'ref': f'ORD-{o.id:03d}',
            'type': 'Sale',
            'customer': wc.name if wc else 'Walk-in',
            'payment': (o.payment_method or 'cash').upper(),
            'status': 'Completed',
            'total': o.total_amount,
        })

    # In-Store Parts Purchase sales — a typed name, not a WalkInCustomer
    # lookup, so they're tracked separately via is_instore rather than
    # walkin_customer_id.
    instore_orders = Order.query.filter(
        Order.is_instore == True, Order.walkin_customer_id == None, Order.status == 'completed'
    ).filter(Order.items.any()).all()
    for o in instore_orders:
        txns.append({
            '_dt': o.created_at,
            'ref': f'SALE-{o.id:03d}',
            'type': 'Sale',
            'customer': o.contact_name or 'Walk-in',
            'payment': (o.payment_method or 'cash').upper(),
            'status': 'Completed',
            'total': o.total_amount,
        })

    online_orders = Order.query.filter(
        Order.walkin_customer_id == None, Order.is_instore == False, Order.status == 'completed'
    ).filter(Order.items.any()).all()
    for o in online_orders:
        txns.append({
            '_dt': o.created_at,
            'ref': f'ORD-{o.id:03d}',
            'type': 'Online',
            'customer': o.customer.fullname if o.customer else 'Unknown',
            'payment': (o.payment_method or 'cash').upper(),
            'status': 'Completed',
            'total': o.total_amount,
        })

    txns.sort(key=lambda x: x['_dt'], reverse=True)
    result = []
    for i, t in enumerate(txns):
        result.append({
            'trx_id': f'TRX{i+1:03d}',
            'ref': t['ref'],
            'type': t['type'],
            'customer': t['customer'],
            'payment': t['payment'],
            'status': t['status'],
            'total': t['total'],
            'date': t['_dt'].strftime('%b %d, %Y'),
            'is_new': bool(_last_viewed and t['_dt'] > _last_viewed),
        })
    _set_app_state('last_viewed_transactions_at', ph_now().isoformat())
    db.session.commit()
    return jsonify(result)


@admin_app.route('/api/products')
@login_required
@require_admin_or_staff
def api_products():
    products = Product.query.order_by(Product.category, Product.name).all()
    return jsonify([{'id': p.id, 'name': p.name, 'price': p.price, 'stock': p.stock, 'category': p.category} for p in products])


@admin_app.route('/pos/products')
@login_required
@require_admin_or_staff
def pos_products():
    products = Product.query.filter(Product.stock > 0).order_by(Product.category, Product.name).all()
    return jsonify([{'id': p.id, 'name': p.name, 'price': p.price, 'stock': p.stock, 'category': p.category} for p in products])


@admin_app.route('/pos/checkout', methods=['POST'])
@login_required
@require_admin_or_staff
def pos_checkout():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data received'}), 400
    cart        = data.get('cart', [])
    services    = data.get('services', [])
    customer_id = data.get('customer_id')
    total       = float(data.get('total', 0))
    payment_method = data.get('payment_method', 'cash')
    if not cart and not services:
        return jsonify({'success': False, 'error': 'Cart is empty'}), 400
    for item in cart:
        if not item.get('source_order_id'):
            product = Product.query.get(item['product_id'])
            if not product:
                return jsonify({'success': False, 'error': f'Product not found'}), 400
            if product.stock < item['quantity']:
                return jsonify({'success': False, 'error': f'Insufficient stock for {product.name}'}), 400

    order_id = None
    completed_order_ids = set()
    completed_booking_ids = set()

    new_cart_items     = [item for item in cart if not item.get('source_order_id')]
    pending_cart_items = [item for item in cart if item.get('source_order_id')]

    for item in pending_cart_items:
        source_order_id = item.get('source_order_id')
        if source_order_id and source_order_id not in completed_order_ids:
            source_order = Order.query.get(source_order_id)
            if source_order:
                source_order.status = 'completed'
                if not source_order.delivered_at:
                    source_order.delivered_at = ph_now()
                source_order.payment_method = payment_method
                completed_order_ids.add(source_order_id)
                if source_order.user_id:
                    send_notification(source_order.user_id, 'Order Completed!',
                        f'Your order ORD-{source_order.id:03d} has been completed and paid.',
                        type='order', status='completed')

    if new_cart_items:
        customer_name = clean_str(data.get('customer_name', ''), max_len=100)
        if not customer_id and not customer_name:
            return jsonify({'success': False, 'error': 'Customer name is required.'}), 400
        uid = int(customer_id) if customer_id else current_user.id
        new_total = sum(item['quantity'] * float(item['unit_price']) for item in new_cart_items)
        order = Order(user_id=uid, total_amount=new_total, status='completed', payment_method=payment_method,
                      delivered_at=ph_now(), is_instore=True, instore_seen=False,
                      contact_name=None if customer_id else customer_name)
        db.session.add(order)
        db.session.flush()
        order_id = order.id
        for item in new_cart_items:
            product = Product.query.get(item['product_id'])
            db.session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=item['quantity'], unit_price=float(item['unit_price'])))
            product.stock -= item['quantity']
        if customer_id:
            send_notification(int(customer_id), 'Order Completed (In-Store)',
                f'Your in-store order ORD-{order.id:03d} worth ₱{new_total:,.2f} has been completed.',
                type='order', status='completed')

    booking_ids = []
    service_revenue = 0
    for svc in services:
        source_booking_id = svc.get('source_booking_id')
        svc_price = float(svc.get('price', 0))
        svc_qty   = int(svc.get('qty', 1))
        if source_booking_id:
            source_booking = Booking.query.get(source_booking_id)
            if source_booking:
                source_booking.status = 'completed'
                if not source_booking.completed_at:
                    source_booking.completed_at = ph_now()
                source_booking.payment_method = payment_method
                source_booking.total_amount = svc_price * svc_qty
                completed_booking_ids.add(source_booking_id)
                booking_ids.append(source_booking_id)
                service_revenue += svc_price * svc_qty
                if source_booking.user_id:
                    send_notification(source_booking.user_id, 'Service Completed!',
                        f'Your {source_booking.service} service has been completed. Thank you!',
                        type='booking', status='completed')
        else:
            booking = Booking(
                user_id=int(customer_id) if customer_id else current_user.id,
                service=svc['name'], date=date.today(), time=datetime.now().time(),
                motorcycle_model=svc.get('motorcycle_model', ''),
                motorcycle_plate=svc.get('motorcycle_plate', ''),
                notes='Walk-in POS service.', status='completed', payment_method=payment_method,
                total_amount=svc_price * svc_qty, completed_at=ph_now()
            )
            db.session.add(booking)
            db.session.flush()
            booking_ids.append(booking.id)
            service_revenue += svc_price * svc_qty
            if customer_id:
                send_notification(int(customer_id), 'Service Completed (In-Store)',
                    f'Your {svc["name"]} walk-in service has been recorded.',
                    type='booking', status='completed')

    db.session.commit()
    return jsonify({
        'success': True, 'order_id': order_id, 'booking_ids': booking_ids,
        'completed_orders': list(completed_order_ids),
        'completed_bookings': list(completed_booking_ids),
        'message': 'Transaction completed successfully.'
    })


@admin_app.route('/pos/transactions')
@login_required
@require_admin_or_staff
def pos_transactions():
    today  = date.today()
    orders = (Order.query.filter(Order.status == 'completed', func.date(Order.created_at) == today)
              .order_by(Order.created_at.desc()).limit(20).all())
    result = []
    for o in orders:
        customer = db.session.get(User, o.user_id)
        result.append({
            'type': 'order', 'id': f'ORD-{o.id:03d}',
            'customer': customer.fullname if customer else 'Walk-in',
            'total': o.total_amount, 'time': o.created_at.strftime('%I:%M %p'),
            'items': [f'{i.product.name} x{i.quantity}' for i in o.items]
        })
    return jsonify(result)


@admin_app.route('/walk-in')
@login_required
@require_admin_or_staff
def walk_in():
    products  = Product.query.filter(Product.stock > 0).order_by(Product.category, Product.name).all()
    mechanics = Mechanic.query.order_by(Mechanic.name).all()
    services  = Service.query.filter_by(is_active=True).order_by(Service.name).all()

    view_quotation = None
    qid = request.args.get('view_quotation', type=int)
    if qid:
        q = Quotation.query.get(qid)
        if q:
            view_quotation = {
                'ref': f'QUO-{q.id:03d}', 'inspected_by': q.inspected_by,
                'items': [{'name': i.name, 'item_type': i.item_type, 'quantity': i.quantity, 'unit_price': i.unit_price}
                          for i in q.items],
                'total': q.total_amount,
            }
    return render_template('walkin.html', products=products, mechanics=mechanics, services=services,
                           view_quotation=view_quotation)


@admin_app.route('/walk-in/search-customer')
@login_required
@require_admin_or_staff
def walkin_search_customer():
    phone = request.args.get('phone', '').strip()
    if len(phone) < 4:
        return jsonify([])
    customers = WalkInCustomer.query.filter(
        WalkInCustomer.phone.like(f'%{phone}%')
    ).order_by(WalkInCustomer.created_at.desc()).limit(5).all()
    return jsonify([{
        'id': c.id, 'name': c.name, 'phone': c.phone,
        'motorcycle_model': c.motorcycle_model or '',
        'motorcycle_plate': c.motorcycle_plate or ''
    } for c in customers])


@admin_app.route('/walk-in/customer/<int:cid>/history')
@login_required
@require_admin_or_staff
def walkin_customer_history(cid):
    customer = WalkInCustomer.query.get_or_404(cid)
    bookings = Booking.query.filter_by(walkin_customer_id=cid).order_by(Booking.created_at.desc()).limit(15).all()
    orders   = Order.query.filter_by(walkin_customer_id=cid).order_by(Order.created_at.desc()).limit(15).all()
    history  = []
    for b in bookings:
        history.append({
            'type': 'service',
            'date': b.created_at.strftime('%b %d, %Y'),
            'description': b.service,
            'amount': b.total_amount or 0,
            'status': b.status,
            'mechanic': b.assigned_mechanic_name or '—'
        })
    for o in orders:
        items_desc = ', '.join(f"{oi.product.name} x{oi.quantity}" for oi in o.items if oi.product)
        history.append({
            'type': 'product',
            'date': o.created_at.strftime('%b %d, %Y'),
            'description': items_desc or 'Products',
            'amount': o.total_amount,
            'status': o.status,
            'mechanic': '—'
        })
    history.sort(key=lambda x: x['date'], reverse=True)
    return jsonify({
        'customer': {
            'id': customer.id, 'name': customer.name, 'phone': customer.phone,
            'motorcycle_model': customer.motorcycle_model or '',
            'motorcycle_plate': customer.motorcycle_plate or '',
            'created_at': customer.created_at.strftime('%b %d, %Y')
        },
        'history': history[:20]
    })


def _walkin_estimate(services, findings):
    """Minutes a walk-in job will take: each service's catalog time × quantity
    (30 for a custom one), plus the time the mechanic gave each inspection finding."""
    names = [s.get('name') for s in services if s.get('name')]
    by_name = {s.name: s for s in Service.query.filter(Service.name.in_(names)).all()} if names else {}
    total = 0
    for s in services:
        svc = by_name.get(s.get('name'))
        total += ((svc.duration_minutes if (svc and svc.duration_minutes) else 30) * max(1, int(s.get('qty', 1) or 1)))
    for f in findings:
        try:
            total += max(0, int(f.get('minutes') or 0))
        except (TypeError, ValueError):
            pass
    return total


@admin_app.route('/walk-in/mechanic-status')
@login_required
@require_admin_or_staff
def walkin_mechanic_status():
    """For the job the admin is building: which mechanic is already on another
    walk-in during [now, now + minutes] (greyed out), and which have an online
    booking in that window (a warning, but allowed)."""
    minutes = max(1, request.args.get('minutes', type=int) or 30)
    now = ph_now()
    start_min = now.hour * 60 + now.minute
    end_min = start_min + minutes
    busy = {}
    for w in _walkin_windows(now.date()):
        if mechanic_overlaps(start_min, end_min, w['start'], w['end']):
            busy[w['mechanic']] = {'ref': w['ref'], 'until': minutes_to_ampm(w['end'])}
    online = {}
    for b in Booking.query.filter(Booking.date == now.date(), Booking.assigned_mechanic_name.isnot(None),
                                  Booking.status.notin_(['cancelled', 'completed'])).all():
        b_start = b.time.hour * 60 + b.time.minute
        b_end = real_end_minutes(b_start, b.duration_minutes or DEFAULT_DURATION_MIN, b.overrun_minutes or 0)
        if mechanic_overlaps(start_min, end_min, b_start, b_end):
            online.setdefault(b.assigned_mechanic_name, []).append({
                'ref': f'BKG-{b.id:03d}', 'customer': _booking_customer_name(b),
                'time': f'{minutes_to_ampm(b_start)} – {minutes_to_ampm(b_end)}'})
    return jsonify({'from': minutes_to_ampm(start_min), 'to': minutes_to_ampm(end_min),
                    'mechanics': {m.name: {'id': m.id, 'walkin': busy.get(m.name), 'online': online.get(m.name, [])}
                                  for m in Mechanic.query.all()}})


@admin_app.route('/job-order/<int:jid>/extend', methods=['POST'])
@login_required
@require_admin_or_staff
def extend_job_order_time(jid):
    """+30m / +1h: the mechanic is tied up longer, so online booking sees them busy longer."""
    jo = JobOrder.query.get_or_404(jid)
    minutes = (request.get_json(silent=True) or {}).get('minutes')
    if minutes not in (30, 60):
        return jsonify({'success': False, 'error': 'Unknown extension.'}), 400
    if not jo.mechanic_name or jo.mechanic_done_at or jo.status not in ('pending', 'in_progress'):
        return jsonify({'success': False, 'error': 'This job is not holding a mechanic.'}), 400
    jo.est_minutes = (jo.est_minutes or 30) + minutes
    db.session.commit()
    until = (jo.start_at.hour * 60 + jo.start_at.minute + jo.est_minutes) if jo.start_at else None
    return jsonify({'success': True, 'until': minutes_to_ampm(until) if until is not None else None})


@admin_app.route('/job-order/<int:jid>/mechanic-done', methods=['POST'])
@login_required
@require_admin_or_staff
def job_order_mechanic_done(jid):
    """Mark Done: the mechanic is free for online booking right now."""
    jo = JobOrder.query.get_or_404(jid)
    if not jo.mechanic_name:
        return jsonify({'success': False, 'error': 'No mechanic is assigned to this job.'}), 400
    jo.mechanic_done_at = ph_now()
    db.session.commit()
    return jsonify({'success': True})


@admin_app.route('/walk-in/create-job-order', methods=['POST'])
@login_required
@require_admin_or_staff
def walkin_create_job_order():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data received'}), 400

    customer_name  = clean_str(data.get('customer_name', ''), max_len=100)
    customer_phone = clean_str(data.get('customer_phone', ''), max_len=20)
    moto_model     = clean_str(data.get('motorcycle_model', ''), max_len=100)
    moto_plate     = clean_str(data.get('motorcycle_plate', ''), max_len=20)
    notes          = clean_str(data.get('notes', ''), max_len=500)
    mechanic_id    = data.get('mechanic_id')
    services       = data.get('services', [])
    cart           = data.get('cart', [])
    findings       = data.get('findings', [])

    if not customer_name:
        return jsonify({'success': False, 'error': 'Customer name is required'}), 400
    if not services and not cart and not findings:
        return jsonify({'success': False, 'error': 'No services, products, or findings added'}), 400

    mechanic_name = None
    if mechanic_id:
        mechanic = Mechanic.query.get(int(mechanic_id))
        if mechanic:
            mechanic_name = mechanic.name

    items = []
    for svc in services:
        items.append({'item_type': 'service', 'name': svc['name'],
                      'quantity': int(svc.get('qty', 1)), 'unit_price': float(svc.get('price', 0))})
    for p in cart:
        product = Product.query.get(p.get('product_id')) if p.get('product_id') else None
        items.append({'item_type': 'product', 'name': product.name if product else p.get('name', ''),
                      'quantity': int(p.get('quantity', 1)), 'unit_price': float(p.get('unit_price', 0))})
    for f in findings:
        name = clean_str(f.get('name', ''), max_len=150)
        if not name:
            continue
        items.append({'item_type': 'finding', 'name': name,
                      'quantity': 1, 'unit_price': float(f.get('price', 0))})

    total = sum(i['unit_price'] * i['quantity'] for i in items)

    # How long the mechanic is tied up: service times + inspection times, starting
    # now. A job with only products and a mechanic still holds them for 30 minutes.
    now = ph_now()
    est_minutes = _walkin_estimate(services, findings)
    if not est_minutes and (cart or items):
        est_minutes = 30
    if mechanic_name:
        start_min = now.hour * 60 + now.minute
        for w in _walkin_windows(now.date()):
            if w['mechanic'] == mechanic_name and mechanic_overlaps(start_min, start_min + est_minutes, w['start'], w['end']):
                return jsonify({'success': False, 'error': f"{mechanic_name} is on {w['ref']} until {minutes_to_ampm(w['end'])}. Pick someone else."}), 409

    try:
        jo = JobOrder(
            start_at         = now,
            est_minutes      = est_minutes or None,
            customer_name    = customer_name,
            customer_phone   = customer_phone,
            motorcycle_model = moto_model,
            motorcycle_plate = moto_plate,
            notes            = notes,
            total_amount     = total,
            status           = 'pending',
            mechanic_name    = mechanic_name,
            created_by       = current_user.id,
        )
        db.session.add(jo)
        db.session.flush()
        for i in items:
            db.session.add(JobOrderItem(
                job_order_id = jo.id,
                item_type    = i['item_type'],
                name         = i['name'],
                quantity     = i['quantity'],
                unit_price   = i['unit_price'],
            ))
        db.session.commit()
        return jsonify({'success': True, 'job_order_id': jo.id, 'ref': f'JO-{jo.id:03d}'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@admin_app.route('/walk-in/checkout', methods=['POST'])
@login_required
@require_admin_or_staff
def walkin_checkout():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data received'}), 400

    customer_name  = clean_str(data.get('customer_name', ''), max_len=100)
    customer_phone = clean_str(data.get('customer_phone', ''), max_len=20)
    moto_model     = clean_str(data.get('motorcycle_model', ''), max_len=100)
    moto_plate     = clean_str(data.get('motorcycle_plate', ''), max_len=20)
    walkin_id      = data.get('walkin_customer_id')
    mechanic_id    = data.get('mechanic_id')
    cart           = data.get('cart', [])
    services       = data.get('services', [])
    payment_method = data.get('payment_method', 'cash')
    action         = data.get('action', 'complete')  # 'complete' | 'save'
    work_status    = data.get('work_status', 'pending')

    if not customer_name:
        return jsonify({'success': False, 'error': 'Customer name is required'}), 400
    if not cart and not services:
        return jsonify({'success': False, 'error': 'Cart is empty'}), 400

    # Get or create walk-in customer record
    if walkin_id:
        walkin_customer = WalkInCustomer.query.get(int(walkin_id))
        if walkin_customer:
            walkin_customer.name  = customer_name
            walkin_customer.phone = customer_phone
            if moto_model: walkin_customer.motorcycle_model = moto_model
            if moto_plate: walkin_customer.motorcycle_plate = moto_plate
    else:
        walkin_customer = None

    if not walkin_customer:
        walkin_customer = WalkInCustomer(
            name=customer_name, phone=customer_phone,
            motorcycle_model=moto_model, motorcycle_plate=moto_plate
        )
        db.session.add(walkin_customer)
        db.session.flush()

    # Mechanic info — a walk-in is immediate, not a future slot reservation, so
    # the fixed hourly grid and the shop-queue check don't apply, but the
    # mechanic still has to actually be on today's on-duty roster, and not
    # already mid-job on a scheduled appointment that overlaps right now —
    # same shared routine, narrowed to what a walk-in can actually be checked
    # against. Duration is looked up from the service catalog when it matches
    # a known service name; unmatched or custom line items fall back to a
    # conservative default rather than skipping the check outright.
    mechanic_name = None
    mechanic_spec = None
    if mechanic_id:
        mechanic = Mechanic.query.get(int(mechanic_id))
        if not mechanic:
            return jsonify({'success': False, 'error': 'Mechanic not found.'}), 400
        svc_names = [s.get('name', '') for s in services if s.get('name')]
        catalog = {s.name: s.duration_minutes for s in Service.query.filter(Service.name.in_(svc_names)).all()}
        walkin_duration = sum(catalog.get(n, DEFAULT_DURATION_MIN) for n in svc_names) or DEFAULT_DURATION_MIN
        now_min = ph_now().hour * 60 + ph_now().minute
        ok, error = _check_admin_booking_request(
            date.today(), now_min, walkin_duration, mechanic_name=mechanic.name,
            require_slot_grid=False, check_shop_queue=False,
        )
        if not ok:
            return jsonify({'success': False, 'error': error}), 400
        mechanic_name = mechanic.name
        mechanic_spec = mechanic.specialization

    order_id    = None
    booking_ids = []
    booking_status = 'completed' if action == 'complete' else work_status

    # Products — only deduct stock on complete
    if cart and action == 'complete':
        for item in cart:
            product = Product.query.get(item['product_id'])
            if not product:
                db.session.rollback()
                return jsonify({'success': False, 'error': 'Product not found'}), 400
            if product.stock < item['quantity']:
                db.session.rollback()
                return jsonify({'success': False, 'error': f'Insufficient stock for {product.name}'}), 400

        prod_total = sum(item['quantity'] * float(item['unit_price']) for item in cart)
        order = Order(
            user_id=current_user.id,
            total_amount=prod_total,
            status='completed',
            payment_method=payment_method,
            walkin_customer_id=walkin_customer.id,
            delivered_at=ph_now()
        )
        db.session.add(order)
        db.session.flush()
        order_id = order.id
        for item in cart:
            product = Product.query.get(item['product_id'])
            db.session.add(OrderItem(
                order_id=order.id, product_id=product.id,
                quantity=item['quantity'], unit_price=float(item['unit_price'])
            ))
            product.stock -= item['quantity']

    # Services — multiple services in one walk-in transaction share a batch id so
    # Manage Booking compresses them into a single row.
    batch_id = str(uuid.uuid4()) if len(services) > 1 else None
    for svc in services:
        svc_price = float(svc.get('price', 0))
        svc_qty   = int(svc.get('qty', 1))
        booking = Booking(
            user_id=current_user.id,
            service=svc['name'],
            date=date.today(),
            time=datetime.now().time(),
            motorcycle_model=moto_model,
            motorcycle_plate=moto_plate,
            contact_name=customer_name,
            contact_mobile=customer_phone,
            notes=svc.get('notes', '') or 'Walk-in transaction.',
            status=booking_status,
            payment_method=payment_method if action == 'complete' else 'cash',
            total_amount=svc_price * svc_qty,
            assigned_mechanic_name=mechanic_name,
            assigned_mechanic_specialization=mechanic_spec,
            preferred_mechanic_name=mechanic_name,
            preferred_mechanic_specialization=mechanic_spec,
            walkin_customer_id=walkin_customer.id,
            booking_batch=batch_id,
            completed_at=(ph_now() if booking_status == 'completed' else None)
        )
        db.session.add(booking)
        db.session.flush()
        booking_ids.append(booking.id)

    db.session.commit()
    return jsonify({
        'success': True,
        'action': action,
        'order_id': order_id,
        'booking_ids': booking_ids,
        'walkin_customer_id': walkin_customer.id,
        'customer_name': walkin_customer.name,
        'message': 'Transaction completed successfully.' if action == 'complete' else 'Walk-in saved successfully.'
    })


@admin_app.route('/pos/customer/<int:cid>/items')
@login_required
@require_admin_or_staff
def pos_customer_items(cid: int):
    customer = User.query.get_or_404(cid)
    pending_orders = Order.query.filter(
        Order.user_id == cid,
        Order.status == 'processing',
        Order.payment_method == 'cash',
        Order.delivery_method == 'pickup'
    ).order_by(Order.created_at.desc()).all()
    orders_data = []
    for order in pending_orders:
        items = []
        for oi in order.items:
            product = db.session.get(Product, oi.product_id)
            items.append({'product_id': oi.product_id, 'product_name': product.name if product else 'Unknown',
                          'quantity': oi.quantity, 'unit_price': oi.unit_price})
        orders_data.append({'order_id': order.id, 'status': order.status, 'total': order.total_amount,
                            'created_at': order.created_at.isoformat(), 'items': items})
    pending_bookings = Booking.query.filter(
        Booking.user_id == cid, Booking.status.in_(['in_progress', 'inprogress'])
    ).order_by(Booking.created_at.desc()).all()
    bookings_data = []
    for b in pending_bookings:
        bookings_data.append({
            'booking_id': b.id, 'service': b.service,
            'price': booking_service_price(b.service),
            'date': b.date.strftime('%Y-%m-%d'), 'time': b.time.strftime('%H:%M'),
            'status': b.status, 'created_at': b.created_at.isoformat()
        })
    return jsonify({'customer_id': cid, 'customer_name': customer.fullname,
                    'orders': orders_data, 'bookings': bookings_data})


# Walk-In E-Receipt PDF

@admin_app.route('/walk-in/receipt')
@login_required
@require_admin_or_staff
def walkin_receipt():
    walkin_id       = request.args.get('walkin_id', type=int)
    order_id        = request.args.get('order_id', type=int)
    booking_ids_str = request.args.get('booking_ids', '')
    payment         = request.args.get('payment', 'cash')

    walkin   = WalkInCustomer.query.get(walkin_id) if walkin_id else None
    order    = Order.query.get(order_id) if order_id else None
    bookings = []
    if booking_ids_str:
        for bid in booking_ids_str.split(','):
            bid = bid.strip()
            if bid.isdigit():
                b = Booking.query.get(int(bid))
                if b:
                    bookings.append(b)

    ref_id  = bookings[0].id if bookings else (order.id if order else 0)
    txn_ref = f'WLK-{ref_id:05d}'
    now     = ph_now()

    svc_total   = sum((b.total_amount or 0) for b in bookings)
    parts_total = (order.total_amount or 0) if order else 0
    grand_total = svc_total + parts_total

    mechanic_name = ''
    if bookings and bookings[0].assigned_mechanic_name:
        mechanic_name = bookings[0].assigned_mechanic_name
        if bookings[0].assigned_mechanic_specialization:
            mechanic_name += f' · {bookings[0].assigned_mechanic_specialization}'

    # --- Build PDF ---
    W = 3.5 * inch
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=(W, 11 * inch),
        rightMargin=0.2 * inch, leftMargin=0.2 * inch,
        topMargin=0.3 * inch, bottomMargin=0.3 * inch
    )

    RED  = colors.HexColor('#e8401c')
    DARK = colors.HexColor('#111827')
    GREY = colors.HexColor('#6b7280')
    LGREY= colors.HexColor('#d1d5db')

    def s(name, **kw):
        return ParagraphStyle(name, **kw)

    ctr      = s('c',  alignment=1, fontName='Helvetica',      fontSize=8,  textColor=DARK, spaceAfter=2)
    ctr_bold = s('cb', alignment=1, fontName='Helvetica-Bold', fontSize=8,  textColor=DARK, spaceAfter=2)
    lft      = s('l',  alignment=0, fontName='Helvetica',      fontSize=8,  textColor=DARK, spaceAfter=2)
    lft_bold = s('lb', alignment=0, fontName='Helvetica-Bold', fontSize=8,  textColor=DARK, spaceAfter=2)
    sm       = s('sm', alignment=1, fontName='Helvetica',      fontSize=7,  textColor=GREY, spaceAfter=2)
    label    = s('lbl',alignment=0, fontName='Helvetica-Bold', fontSize=6,  textColor=GREY, spaceAfter=3, leading=8)

    story = []

    # Header
    story.append(Paragraph(f'<font color="#e8401c"><b>MOTO</b></font><b>TYRE</b>',
        s('brand', alignment=1, fontName='Helvetica-Bold', fontSize=20, textColor=DARK, spaceAfter=1)))
    story.append(Paragraph('MOTO SHOP', s('sub', alignment=1, fontName='Helvetica', fontSize=7, textColor=GREY, spaceAfter=2)))
    story.append(Paragraph('Saranay Rd, Brgy. 171 Bagumbong, Caloocan City', sm))
    story.append(Paragraph('0915 269 8366  ·  Mon–Sun 8AM–6:30PM', sm))
    story.append(HRFlowable(width='100%', thickness=2, color=RED, spaceBefore=5, spaceAfter=6))

    # Receipt ID
    story.append(Paragraph('WALK-IN E-RECEIPT',
        s('rt', alignment=1, fontName='Helvetica-Bold', fontSize=9, textColor=GREY, spaceAfter=3, letterSpacing=1.5)))
    story.append(Paragraph(f'<b><font color="#e8401c">{txn_ref}</font></b>',
        s('rn', alignment=1, fontName='Helvetica-Bold', fontSize=14, textColor=DARK, spaceAfter=2)))
    story.append(Paragraph(now.strftime('%B %d, %Y  ·  %I:%M %p'), sm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=LGREY, spaceBefore=5, spaceAfter=5))

    # Customer
    if walkin:
        story.append(Paragraph('CUSTOMER', label))
        story.append(Paragraph(f'<b>{walkin.name}</b>', lft_bold))
        if walkin.phone:
            story.append(Paragraph(walkin.phone, lft))
        moto_parts = [walkin.motorcycle_model, walkin.motorcycle_plate]
        moto = '  ·  '.join(x for x in moto_parts if x)
        if moto:
            story.append(Paragraph(moto, lft))

    # Mechanic
    if mechanic_name:
        story.append(Spacer(1, 4))
        story.append(Paragraph('MECHANIC', label))
        story.append(Paragraph(mechanic_name, lft))

    story.append(HRFlowable(width='100%', thickness=0.5, color=LGREY, spaceBefore=5, spaceAfter=5))

    # Services
    if bookings:
        story.append(Paragraph('SERVICES', label))
        for b in bookings:
            amt = b.total_amount or 0
            t = Table([[b.service, f'₱{amt:,.2f}']], colWidths=[2.4*inch, 0.9*inch])
            t.setStyle(TableStyle([
                ('FONTNAME',      (0,0), (-1,-1), 'Helvetica'),
                ('FONTSIZE',      (0,0), (-1,-1), 8),
                ('TEXTCOLOR',     (0,0), (-1,-1), DARK),
                ('ALIGN',         (1,0), (1,-1),  'RIGHT'),
                ('TOPPADDING',    (0,0), (-1,-1), 1),
                ('BOTTOMPADDING', (0,0), (-1,-1), 1),
            ]))
            story.append(t)

    # Products
    if order and order.items:
        story.append(Spacer(1, 4))
        story.append(Paragraph('PARTS & PRODUCTS', label))
        for oi in order.items:
            name_str = oi.product.name if oi.product else 'Product'
            sub      = oi.unit_price * oi.quantity
            t = Table([[f'{name_str}  x{oi.quantity}', f'₱{sub:,.2f}']], colWidths=[2.4*inch, 0.9*inch])
            t.setStyle(TableStyle([
                ('FONTNAME',      (0,0), (-1,-1), 'Helvetica'),
                ('FONTSIZE',      (0,0), (-1,-1), 8),
                ('TEXTCOLOR',     (0,0), (-1,-1), DARK),
                ('ALIGN',         (1,0), (1,-1),  'RIGHT'),
                ('TOPPADDING',    (0,0), (-1,-1), 1),
                ('BOTTOMPADDING', (0,0), (-1,-1), 1),
            ]))
            story.append(t)

    story.append(HRFlowable(width='100%', thickness=0.5, color=LGREY, spaceBefore=5, spaceAfter=4))

    # Totals
    rows = []
    if bookings and order:
        rows.append([Paragraph('Services', lft), Paragraph(f'₱{svc_total:,.2f}',   s('tr', alignment=2, fontName='Helvetica',      fontSize=8, textColor=DARK))])
        rows.append([Paragraph('Parts',    lft), Paragraph(f'₱{parts_total:,.2f}', s('tr2',alignment=2, fontName='Helvetica',      fontSize=8, textColor=DARK))])
    rows.append([
        Paragraph('<b>TOTAL</b>', s('tl', alignment=0, fontName='Helvetica-Bold', fontSize=10, textColor=DARK)),
        Paragraph(f'<b><font color="#e8401c">₱{grand_total:,.2f}</font></b>',
                  s('tv', alignment=2, fontName='Helvetica-Bold', fontSize=10, textColor=RED))
    ])
    rows.append([
        Paragraph('Payment', lft),
        Paragraph('GCash' if payment == 'gcash' else 'Cash',
                  s('pm', alignment=2, fontName='Helvetica', fontSize=8, textColor=GREY))
    ])

    totals_tbl = Table(rows, colWidths=[2.0*inch, 1.3*inch])
    totals_tbl.setStyle(TableStyle([
        ('TOPPADDING',    (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('LINEABOVE',     (0,-2), (-1,-2), 0.75, DARK),
    ]))
    story.append(totals_tbl)

    story.append(HRFlowable(width='100%', thickness=2, color=RED, spaceBefore=7, spaceAfter=6))
    story.append(Paragraph('Thank you for choosing MotoTyres!', sm))
    story.append(Paragraph(f'Transaction: {txn_ref}', s('tf', alignment=1, fontName='Helvetica', fontSize=7, textColor=LGREY)))

    doc.build(story)
    buffer.seek(0)
    resp = make_response(buffer.read())
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = f'inline; filename=receipt_{txn_ref}.pdf'
    return resp


# Report routes

@admin_app.route('/api/reports-data')
@login_required
@require_admin_or_staff
def api_reports_data():
    """Powers the Reports & Sales page's live date-range filtering — the
    From/To fields re-fetch this instead of reloading the page, so the
    cards, charts, breakdown, and log all update from one consistent call."""
    date_from_s = request.args.get('date_from', '').strip()
    date_to_s = request.args.get('date_to', '').strip()
    date_from = date_to = None
    try:
        if date_from_s:
            date_from = datetime.strptime(date_from_s, '%Y-%m-%d').date()
        if date_to_s:
            date_to = datetime.strptime(date_to_s, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid date.'}), 400
    if date_from and date_to and date_from > date_to:
        return jsonify({'success': False, 'error': '"From" must be on or before "To".'}), 400

    rpt = _reports_summary(date_from, date_to)
    total_customers = User.query.filter_by(role='customer').count()

    txns_json = [{
        'ref': t['ref'], 'type': t['type'], 'customer': t['customer'], 'payment': t['payment'],
        'amount': t['amount'], 'date': t['date'].strftime('%b %d, %Y %I:%M %p'),
        'status': t['status'], 'refund_for': t.get('refund_for'), 'return_ref': t.get('return_ref'),
    } for t in rpt['transactions'][:100]]

    return jsonify({
        'success': True,
        'gross_total': rpt['gross_total'],
        'gross_by_type': rpt['gross_by_type'],
        'refund_total': rpt['refund_total'],
        'refund_count': rpt['refund_count'],
        'net_revenue': rpt['net_revenue'],
        'services_completed': rpt['services_completed'],
        'orders_fulfilled': rpt['orders_fulfilled'],
        'refunded_orders_excluded': rpt['refunded_orders_excluded'],
        'total_customers': total_customers,
        'order_status_counts': rpt['order_status_counts'],
        'top_services': rpt['top_services'],
        'mechanic_performance': rpt['mechanic_performance'],
        'top_products': rpt['top_products'],
        'transactions': txns_json,
        'transaction_count': len(rpt['transactions']),
    })


@admin_app.route('/report/generate', methods=['POST'])
@login_required
@require_admin_or_staff
def generate_report():
    date_from = request.form.get('date_from')
    date_to   = request.form.get('date_to')
    try:
        d_from = datetime.strptime(date_from, '%Y-%m-%d').date()
        d_to   = datetime.strptime(date_to,   '%Y-%m-%d').date()
    except:
        flash('Invalid date range.', 'danger')
        return redirect(url_for('admin_dashboard'))

    from collections import defaultdict

    orders = Order.query.filter(
        func.date(Order.created_at) >= d_from, func.date(Order.created_at) <= d_to
    ).order_by(Order.created_at.desc()).all()
    bookings = Booking.query.filter(
        Booking.date >= d_from, Booking.date <= d_to
    ).order_by(Booking.date.desc()).all()

    # Mechanic Performance and Top Products Sold are the same computation the
    # live Reports & Sales page uses, so the PDF never disagrees with it.
    _rpt_detail = _reports_summary(d_from, d_to)
    mechanic_performance = _rpt_detail['mechanic_performance']
    top_products_sold    = _rpt_detail['top_products']

    def order_counts_as_revenue(o):
        if o.delivery_method == 'ship':      # Lalamove orders count once paid, never on hold, cancelled or refunded
            return bool(o.paid_at) and o.status in ('done_packaging', 'shipping', 'completed')
        return (o.status not in ('cancelled', 'awaiting_payment')
                and not (o.payment_method == 'cash' and o.delivery_method == 'pickup' and o.status != 'completed'))

    # Total Revenue mirrors the dashboard's definition (orders + completed bookings +
    # job-order payments) — previously this only summed orders, so it never matched
    # the "Total Revenue" figure shown on the Reports & Sales page above it.
    order_revenue = sum((o.total_amount or 0) + ((o.shipping_fee or 0) if o.delivery_method == 'ship' else 0)
                        for o in orders if order_counts_as_revenue(o))
    booking_revenue = sum(b.total_amount or 0 for b in bookings if b.status == 'completed')
    job_order_revenue = db.session.query(func.sum(Payment.amount)).filter(
        func.date(Payment.paid_at) >= d_from, func.date(Payment.paid_at) <= d_to
    ).scalar() or 0

    gross_revenue = order_revenue + booking_revenue + job_order_revenue

    # Refunds released within this period — subtracted here, on the date
    # they were actually released, not the original sale's date, so a
    # report already printed never changes after the fact.
    _refunds_in_period = ReturnRequest.query.filter(
        ReturnRequest.resolution == 'refund', ReturnRequest.status == 'resolved',
        func.date(ReturnRequest.resolved_at) >= d_from, func.date(ReturnRequest.resolved_at) <= d_to,
    ).all()
    refund_total = sum(rr.refund_amount or 0 for rr in _refunds_in_period)
    refund_count = len(_refunds_in_period)
    refund_rows = [(_return_ref(rr),
                     f'ORD-{rr.order_id:03d}' if rr.kind == 'product' else f'BKG-{rr.booking_id:03d}',
                     rr.refund_amount or 0, rr.resolved_at) for rr in _refunds_in_period]

    total_revenue      = gross_revenue - refund_total
    total_orders       = len(orders)
    total_bookings     = len(bookings)
    completed_orders   = sum(1 for o in orders if o.status == 'completed')
    completed_bookings = sum(1 for b in bookings if b.status == 'completed')
    cancelled_orders   = sum(1 for o in orders if o.status == 'cancelled')
    cancelled_bookings = sum(1 for b in bookings if b.status == 'cancelled')

    # Orders / bookings broken down by status, each with the revenue actually
    # attributable to that status (so admin can see e.g. how much is still sitting
    # in "shipped" vs already banked as "completed").
    order_status_rows = defaultdict(lambda: [0, 0.0])
    for o in orders:
        order_status_rows[o.status][0] += 1
        if order_counts_as_revenue(o):
            order_status_rows[o.status][1] += o.total_amount

    booking_status_rows = defaultdict(int)
    for b in bookings:
        booking_status_rows[b.status] += 1

    # Top services booked — only non-cancelled bookings; revenue only from completed
    # ones. A combo booking's .service is comma-joined, so each service in it counts
    # on its own; a back job's .service is prefixed "Back job — <service>" and counts
    # under that same service rather than its own bucket.
    service_bookings = defaultdict(lambda: [0, 0.0])
    for b in bookings:
        if b.status == 'cancelled':
            continue
        is_backjob = b.service.startswith('Back job — ')
        base = b.service[len('Back job — '):] if is_backjob else b.service
        names = split_service_names(base)
        for name in names:
            service_bookings[name][0] += 1
            if b.status == 'completed':
                service_bookings[name][1] += (b.total_amount or 0) / len(names)
    top_services = sorted(service_bookings.items(), key=lambda kv: kv[1][0], reverse=True)[:8]

    # Payment method split, orders only (cash vs GCash) — non-cancelled orders.
    payment_split = defaultdict(lambda: [0, 0.0])
    for o in orders:
        if o.status == 'cancelled':
            continue
        method = (o.payment_method or 'cash').upper()
        payment_split[method][0] += 1
        if order_counts_as_revenue(o):
            payment_split[method][1] += o.total_amount

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    story = []
    W = A4[0] - 80
    RED   = colors.HexColor('#e8401c')
    DARK  = colors.HexColor('#1a1d23')
    LIGHT = colors.HexColor('#f3f4f6')
    MUTED = colors.HexColor('#6b7280')
    styles = getSampleStyleSheet()

    def style(name, **kwargs):
        # ParagraphStyle defaults leading to 12pt regardless of fontSize, so a large
        # fontSize (e.g. the 24pt title) with no leading override gets a line height
        # far shorter than the text itself, and the next paragraph renders on top of it.
        kwargs.setdefault('leading', kwargs.get('fontSize', 10) * 1.25)
        return ParagraphStyle(name, **kwargs)
    title_style   = style('T', fontSize=24, fontName='Helvetica-Bold', textColor=RED, spaceAfter=2)
    sub_style     = style('S', fontSize=10, fontName='Helvetica', textColor=MUTED, spaceAfter=4)
    heading_style = style('H', fontSize=12, fontName='Helvetica-Bold', textColor=DARK, spaceBefore=14, spaceAfter=6)
    small_style   = style('SM', fontSize=8, fontName='Helvetica', textColor=MUTED)

    def section_table(headers, rows, col_widths, align_right_cols=(), bold_last_row=False):
        t = Table([headers] + rows, colWidths=col_widths, repeatRows=1)
        cmds = [
            ('BACKGROUND', (0,0), (-1,0), DARK), ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,0), 8.5),
            ('FONTNAME', (0,1), (-1,-1), 'Helvetica'), ('FONTSIZE', (0,1), (-1,-1), 8.5),
            ('TEXTCOLOR', (0,1), (-1,-1), DARK),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT]),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#e5e7eb')),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('TOPPADDING', (0,0), (-1,-1), 5), ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('LEFTPADDING', (0,0), (-1,-1), 8), ('RIGHTPADDING', (0,0), (-1,-1), 8),
        ]
        for c in align_right_cols:
            cmds.append(('ALIGN', (c,0), (c,-1), 'RIGHT'))
        if bold_last_row and rows:
            cmds += [
                ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
                ('BACKGROUND', (0,-1), (-1,-1), LIGHT),
                ('TEXTCOLOR', (0,-1), (-1,-1), RED),
            ]
        t.setStyle(TableStyle(cmds))
        return t

    story.append(Paragraph('MOTOTYRES MOTO SHOP', title_style))
    story.append(Paragraph('Sales & Operations Report', style('ST', fontSize=13, fontName='Helvetica-Bold', textColor=DARK, spaceAfter=2)))
    story.append(Paragraph(f'Period: {d_from.strftime("%b %d, %Y")} — {d_to.strftime("%b %d, %Y")}', sub_style))
    story.append(Paragraph(f'Generated: {ph_now().strftime("%b %d, %Y at %I:%M %p")}', sub_style))
    story.append(HRFlowable(width=W, thickness=2, color=RED, spaceAfter=12))

    story.append(Paragraph('SUMMARY', heading_style))
    summary_data = [
        ['Net Revenue', 'Total Orders', 'Total Bookings', 'Completed Orders'],
        [f'P{total_revenue:,.2f}', str(total_orders), str(total_bookings), str(completed_orders)],
    ]
    st = Table(summary_data, colWidths=[W/4]*4)
    st.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), DARK), ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,0), 9),
        ('BACKGROUND', (0,1), (-1,1), LIGHT), ('FONTNAME', (0,1), (-1,1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,1), (-1,1), 14), ('TEXTCOLOR', (0,1), (0,1), RED),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'), ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 8), ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('GRID', (0,0), (-1,-1), 0.5, colors.white),
    ]))
    story.append(st)
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f'Completed Bookings: <b>{completed_bookings}</b> &nbsp;&nbsp;|&nbsp;&nbsp; '
        f'Cancelled Orders: <b>{cancelled_orders}</b> &nbsp;&nbsp;|&nbsp;&nbsp; '
        f'Cancelled Bookings: <b>{cancelled_bookings}</b>',
        sub_style
    ))
    story.append(Spacer(1, 6))

    story.append(Paragraph('REVENUE BREAKDOWN', heading_style))
    story.append(section_table(
        ['Source', 'Amount'],
        [
            ['Product Sales (Orders)', f'P{order_revenue:,.2f}'],
            ['Service Bookings (Completed)', f'P{booking_revenue:,.2f}'],
            ['Job Orders / Walk-ins', f'P{job_order_revenue:,.2f}'],
            ['GROSS REVENUE', f'P{gross_revenue:,.2f}'],
            [f'Refunds ({refund_count})', f'-P{refund_total:,.2f}'],
            ['NET REVENUE', f'P{total_revenue:,.2f}'],
        ],
        [W*0.65, W*0.35], align_right_cols=(1,), bold_last_row=True
    ))
    if refund_rows:
        story.append(Spacer(1, 4))
        story.append(section_table(
            ['Refund Ref', 'For', 'Amount', 'Released'],
            [[ref, orig, f'-P{amt:,.2f}', dt.strftime('%b %d, %Y')] for ref, orig, amt, dt in refund_rows],
            [W*0.2, W*0.3, W*0.25, W*0.25], align_right_cols=(2,)
        ))

    story.append(Paragraph('ORDERS BY STATUS', heading_style))
    order_rows_sorted = sorted(order_status_rows.items())
    if order_rows_sorted:
        story.append(section_table(
            ['Status', 'Count', 'Revenue'],
            [[s.replace('_', ' ').title(), str(c), f'P{r:,.2f}'] for s, (c, r) in order_rows_sorted],
            [W*0.4, W*0.25, W*0.35], align_right_cols=(1, 2)
        ))
    else:
        story.append(Paragraph('No orders in this period.', sub_style))

    story.append(Paragraph('BOOKINGS BY STATUS', heading_style))
    booking_rows_sorted = sorted(booking_status_rows.items())
    if booking_rows_sorted:
        story.append(section_table(
            ['Status', 'Count'],
            [[s.replace('_', ' ').title(), str(c)] for s, c in booking_rows_sorted],
            [W*0.7, W*0.3], align_right_cols=(1,)
        ))
    else:
        story.append(Paragraph('No bookings in this period.', sub_style))

    story.append(Paragraph('TOP PRODUCTS SOLD', heading_style))
    if top_products_sold:
        story.append(section_table(
            ['Product', 'Units Sold', 'Revenue'],
            [[r['name'], str(int(r['units'])), f'P{r["revenue"]:,.2f}'] for r in top_products_sold],
            [W*0.5, W*0.2, W*0.3], align_right_cols=(1, 2)
        ))
    else:
        story.append(Paragraph('No product sales in this period.', sub_style))

    story.append(Paragraph('TOP SERVICES BOOKED', heading_style))
    if top_services:
        story.append(section_table(
            ['Service', 'Bookings', 'Revenue (Completed)'],
            [[name, str(cnt), f'P{rev:,.2f}'] for name, (cnt, rev) in top_services],
            [W*0.45, W*0.2, W*0.35], align_right_cols=(1, 2)
        ))
    else:
        story.append(Paragraph('No bookings in this period.', sub_style))

    story.append(Paragraph('MECHANIC PERFORMANCE', heading_style))
    if mechanic_performance:
        story.append(section_table(
            ['Mechanic', 'Services Completed', 'Back Jobs', 'Service Income (Labor Only)'],
            [[r['name'], str(r['completed']), str(r['backjobs']), f'P{r["income"]:,.2f}'] for r in mechanic_performance] +
            [['TOTAL', str(sum(r['completed'] for r in mechanic_performance)),
              str(sum(r['backjobs'] for r in mechanic_performance)),
              f'P{sum(r["income"] for r in mechanic_performance):,.2f}']],
            [W*0.3, W*0.25, W*0.2, W*0.25], align_right_cols=(1, 2, 3), bold_last_row=True
        ))
        story.append(Paragraph('Service income reflects labor only — parts and materials billed on a quotation are excluded.', small_style))
    else:
        story.append(Paragraph('No completed bookings in this period.', sub_style))

    story.append(Paragraph('PAYMENT METHOD BREAKDOWN (ORDERS)', heading_style))
    if payment_split:
        story.append(section_table(
            ['Method', 'Orders', 'Revenue'],
            [[method, str(c), f'P{r:,.2f}'] for method, (c, r) in sorted(payment_split.items())],
            [W*0.4, W*0.25, W*0.35], align_right_cols=(1, 2)
        ))
    else:
        story.append(Paragraph('No orders in this period.', sub_style))

    story.append(Spacer(1, 10))
    story.append(HRFlowable(width=W, thickness=1, color=MUTED, spaceAfter=6))
    story.append(Paragraph('This report was automatically generated by MotoTyres Admin Dashboard.', small_style))

    doc.build(story)
    buffer.seek(0)
    filename = f'mototyre_report_{d_from}_{d_to}.pdf'
    response = make_response(buffer.read())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response


# Archiving routes

@admin_app.route('/order/<int:oid>/archive', methods=['POST'])
@login_required
@require_admin_or_staff
def archive_order(oid):
    order = Order.query.get_or_404(oid)
    min_days = 30 if order.status == 'completed' else 7
    age = (ph_now() - order.created_at).days
    if age < min_days:
        flash(f'Order ORD-{order.id:03d} must be at least {min_days} days old to archive.', 'warning')
        return redirect(url_for('admin_dashboard'))
    order.is_archived = True
    db.session.commit()
    flash(f'Order ORD-{order.id:03d} archived.', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/booking/<int:bid>/archive', methods=['POST'])
@login_required
@require_admin_or_staff
def archive_booking(bid):
    booking = Booking.query.get_or_404(bid)
    min_days = 30 if booking.status == 'completed' else 7
    age = (ph_now().date() - booking.date).days
    if age < min_days:
        flash(f'Booking #{booking.id} must be at least {min_days} days old to archive.', 'warning')
        return redirect(url_for('admin_dashboard'))
    booking.is_archived = True
    db.session.commit()
    flash(f'Booking #{booking.id} archived.', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/archive-all', methods=['POST'])
@login_required
def archive_all_old():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    completed_cutoff = ph_now() - timedelta(days=30)
    cancelled_cutoff = ph_now() - timedelta(days=7)
    old_orders = Order.query.filter(
        db.or_(
            db.and_(Order.status == 'completed', Order.created_at < completed_cutoff),
            db.and_(Order.status == 'cancelled', Order.created_at < cancelled_cutoff)
        ), Order.is_archived == False
    ).all()
    old_bookings = Booking.query.filter(
        db.or_(
            db.and_(Booking.status == 'completed', Booking.date < completed_cutoff.date()),
            db.and_(Booking.status == 'cancelled', Booking.date < cancelled_cutoff.date())
        ), Booking.is_archived == False
    ).all()
    for o in old_orders: o.is_archived = True
    for b in old_bookings: b.is_archived = True
    db.session.commit()
    flash(f'Archived {len(old_orders)} orders and {len(old_bookings)} bookings.', 'success')
    return redirect(url_for('admin_dashboard'))

@admin_app.route('/order/<int:oid>/unarchive', methods=['POST'])
@login_required
@require_admin_or_staff
def unarchive_order(oid):
    order = Order.query.get_or_404(oid)
    order.is_archived = False
    db.session.commit()
    flash(f'Order ORD-{order.id:03d} restored.', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/booking/<int:bid>/unarchive', methods=['POST'])
@login_required
@require_admin_or_staff
def unarchive_booking(bid):
    booking = Booking.query.get_or_404(bid)
    booking.is_archived = False
    db.session.commit()
    flash(f'Booking #{booking.id} restored.', 'success')
    return redirect(url_for('admin_dashboard'))


with admin_app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print('[MIGRATION] create_all error:', e)
    for _stmt in [
        "ALTER TABLE booking ADD COLUMN walkin_customer_id INT DEFAULT NULL",
        "ALTER TABLE `order` ADD COLUMN walkin_customer_id INT DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN booking_batch VARCHAR(36) DEFAULT NULL",
    ]:
        try:
            from sqlalchemy import text as _tmig
            db.session.execute(_tmig(_stmt))
            db.session.commit()
        except Exception:
            db.session.rollback()

with admin_app.app_context():
    try:
        from sqlalchemy import text as _text2
        db.session.execute(_text2("ALTER TABLE service ADD COLUMN description VARCHAR(200) NOT NULL DEFAULT ''"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        from sqlalchemy import text as _text2
        db.session.execute(_text2("ALTER TABLE service ADD COLUMN price FLOAT NOT NULL DEFAULT 0"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        from sqlalchemy import text as _text3
        db.session.execute(_text3("UPDATE service SET is_active=1 WHERE is_active IS NULL"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        from sqlalchemy import text as _text4
        db.session.execute(_text4("ALTER TABLE booking ADD COLUMN odometer INT DEFAULT NULL"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        from sqlalchemy import text as _text5
        db.session.execute(_text5("ALTER TABLE booking ADD COLUMN total_amount FLOAT DEFAULT 0"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        from sqlalchemy import text as _text6
        db.session.execute(_text6("""
            DELETE FROM "order" WHERE id NOT IN (
                SELECT DISTINCT order_id FROM order_item
            )
        """))
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        from sqlalchemy import text as _text7
        db.session.execute(_text7("ALTER TABLE return_request ADD COLUMN replacement_arrived_at DATETIME DEFAULT NULL"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        from sqlalchemy import text as _text8
        db.session.execute(_text8("ALTER TABLE return_request ADD COLUMN completed_at DATETIME DEFAULT NULL"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    for _col, _ddl in [
        ('replacement_shipped_at', 'DATETIME DEFAULT NULL'),
        ('delivery_name', 'VARCHAR(100) DEFAULT NULL'),
        ('delivery_mobile', 'VARCHAR(20) DEFAULT NULL'),
        ('delivery_city', 'VARCHAR(50) DEFAULT NULL'),
        ('delivery_barangay', 'VARCHAR(50) DEFAULT NULL'),
        ('delivery_street', 'VARCHAR(255) DEFAULT NULL'),
        ('delivery_zip', 'VARCHAR(10) DEFAULT NULL'),
    ]:
        try:
            from sqlalchemy import text as _text9
            db.session.execute(_text9(f"ALTER TABLE return_request ADD COLUMN {_col} {_ddl}"))
            db.session.commit()
        except Exception:
            db.session.rollback()
    for _col, _ddl in [
        ('default_delivery_name', 'VARCHAR(100) DEFAULT NULL'),
        ('default_delivery_mobile', 'VARCHAR(20) DEFAULT NULL'),
        ('default_delivery_city', 'VARCHAR(50) DEFAULT NULL'),
        ('default_delivery_barangay', 'VARCHAR(50) DEFAULT NULL'),
        ('default_delivery_street', 'VARCHAR(255) DEFAULT NULL'),
        ('default_delivery_zip', 'VARCHAR(10) DEFAULT NULL'),
    ]:
        try:
            from sqlalchemy import text as _text10
            db.session.execute(_text10(f"ALTER TABLE user ADD COLUMN {_col} {_ddl}"))
            db.session.commit()
        except Exception:
            db.session.rollback()
    for _stmt in [
        "ALTER TABLE quotation ADD COLUMN booking_id INT DEFAULT NULL",
        "ALTER TABLE quotation ADD COLUMN inspected_by VARCHAR(100) DEFAULT NULL",
        "ALTER TABLE quotation_item ADD COLUMN locked TINYINT(1) DEFAULT 0",
        "ALTER TABLE booking ADD COLUMN invoice_payment_method VARCHAR(10) DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN invoice_paid TINYINT(1) DEFAULT 0",
        "ALTER TABLE booking ADD COLUMN invoice_paid_at DATETIME DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN invoice_cash_received FLOAT DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN invoice_change FLOAT DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN invoice_receipt_no VARCHAR(20) DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN invoice_gcash_ref VARCHAR(50) DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN actual_start_at DATETIME DEFAULT NULL",
        "ALTER TABLE job_order ADD COLUMN start_at DATETIME DEFAULT NULL",
        "ALTER TABLE job_order ADD COLUMN est_minutes INT DEFAULT NULL",
        "ALTER TABLE job_order ADD COLUMN mechanic_done_at DATETIME DEFAULT NULL",
        "ALTER TABLE `user` ADD COLUMN profile_pic_blob MEDIUMBLOB DEFAULT NULL",
        "ALTER TABLE `user` ADD COLUMN profile_pic_mime VARCHAR(40) DEFAULT NULL",
        "ALTER TABLE `user` ADD COLUMN billing_seen_at DATETIME DEFAULT NULL",
        "ALTER TABLE `order` ADD COLUMN payment_ref VARCHAR(80) DEFAULT NULL",
        "ALTER TABLE `order` ADD COLUMN paid_at DATETIME",
        "ALTER TABLE `order` ADD COLUMN shipping_vehicle VARCHAR(40)",
        "ALTER TABLE `order` ADD COLUMN shipping_reason VARCHAR(200)",
        "ALTER TABLE `order` ADD COLUMN shipping_prices TEXT",
        "ALTER TABLE `order` ADD COLUMN shipping_options_sent_at DATETIME",
        "ALTER TABLE `order` ADD COLUMN shipping_deadline_at DATETIME",
        "ALTER TABLE `order` ADD COLUMN shipping_reminder_at DATETIME",
        "ALTER TABLE `order` ADD COLUMN shipping_option VARCHAR(20)",
        "ALTER TABLE `order` ADD COLUMN shipping_fee DOUBLE",
        "ALTER TABLE `order` ADD COLUMN lalamove_fare DOUBLE",
        "ALTER TABLE `order` ADD COLUMN paymongo_payment_id VARCHAR(80)",
        "ALTER TABLE `order` ADD COLUMN rider_type VARCHAR(12)",
        "ALTER TABLE `order` ADD COLUMN rider_name VARCHAR(100)",
        "ALTER TABLE `order` ADD COLUMN rider_phone VARCHAR(20)",
        "ALTER TABLE `order` ADD COLUMN rider_plate VARCHAR(30)",
        "ALTER TABLE `order` ADD COLUMN rider_model VARCHAR(60)",
        "ALTER TABLE `order` ADD COLUMN pay_started_at DATETIME",
        "ALTER TABLE notification ADD COLUMN order_id INT DEFAULT NULL",
        "ALTER TABLE `order` ADD COLUMN lalamove_link VARCHAR(500)",
        "ALTER TABLE `order` ADD COLUMN shipping_started_at DATETIME",
        "ALTER TABLE `order` ADD COLUMN received_at DATETIME",
        "ALTER TABLE `order` ADD COLUMN received_reminder_at DATETIME",
        "ALTER TABLE `order` ADD COLUMN completed_by_shop TINYINT(1) DEFAULT 0",
        "ALTER TABLE `order` ADD COLUMN completion_proof VARCHAR(500)",
        "ALTER TABLE `order` ADD COLUMN cancelled_by VARCHAR(12)",
        "ALTER TABLE `order` ADD COLUMN cancel_reason VARCHAR(300)",
        "ALTER TABLE `order` ADD COLUMN refunded_at DATETIME",
        "ALTER TABLE `order` ADD COLUMN refund_amount DOUBLE",
        "ALTER TABLE `order` ADD COLUMN refund_reason VARCHAR(300)",
        "ALTER TABLE `order` ADD COLUMN stock_restocked TINYINT(1) DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN inspection_at DATETIME DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN in_progress_at DATETIME DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN late_notice_at DATETIME DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN late_notice_text VARCHAR(120) DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN late_notice_emailed TINYINT(1) DEFAULT 0",
        "ALTER TABLE booking ADD COLUMN assign_notice_at DATETIME DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN assign_notice_kind VARCHAR(10) DEFAULT NULL",
        "ALTER TABLE booking ADD COLUMN assign_notice_emailed TINYINT(1) DEFAULT 0",
        "ALTER TABLE return_request ADD COLUMN refund_method VARCHAR(10) DEFAULT NULL",
        "ALTER TABLE return_request ADD COLUMN gcash_number VARCHAR(20) DEFAULT NULL",
        "ALTER TABLE return_request ADD COLUMN gcash_account_name VARCHAR(100) DEFAULT NULL",
        "ALTER TABLE return_request ADD COLUMN refund_receipt VARCHAR(500) DEFAULT NULL",
        "ALTER TABLE return_request ADD COLUMN cash_ready_at DATETIME DEFAULT NULL",
        "ALTER TABLE return_request ADD COLUMN info_requested_at DATETIME DEFAULT NULL",
        "ALTER TABLE return_request ADD COLUMN refund_given_by VARCHAR(100) DEFAULT NULL",
        "ALTER TABLE return_request ADD COLUMN replacement_out_of_stock TINYINT(1) DEFAULT 0",
        "ALTER TABLE return_request ADD COLUMN customer_at_shop TINYINT(1) DEFAULT 0",
        "ALTER TABLE return_request ADD COLUMN replacement_reserved TINYINT(1) DEFAULT 0",
        "ALTER TABLE return_request ADD COLUMN customer_arrived_at DATETIME DEFAULT NULL",
        "ALTER TABLE return_request ADD COLUMN customer_back_at DATETIME DEFAULT NULL",
        "ALTER TABLE return_request ADD COLUMN completed_by_shop TINYINT(1) DEFAULT 0",
        "ALTER TABLE booking ADD COLUMN invoice_received_by VARCHAR(100) DEFAULT NULL",
        "ALTER TABLE quotation ADD COLUMN decided_at DATETIME DEFAULT NULL",
        "ALTER TABLE quotation ADD COLUMN not_covered TINYINT(1) DEFAULT 0",
        "ALTER TABLE quotation ADD COLUMN not_covered_reason TEXT DEFAULT NULL",
        "ALTER TABLE quotation ADD COLUMN not_covered_photos VARCHAR(500) DEFAULT NULL",
        "ALTER TABLE `order` ADD COLUMN contact_name VARCHAR(100) DEFAULT NULL",
        "ALTER TABLE `order` ADD COLUMN is_instore TINYINT(1) DEFAULT 0",
        "ALTER TABLE `order` ADD COLUMN instore_seen TINYINT(1) DEFAULT 1",
        "ALTER TABLE return_request ADD COLUMN is_pickup TINYINT(1) DEFAULT 0",
        "ALTER TABLE product ADD COLUMN image_url VARCHAR(500) DEFAULT NULL",
    ]:
        try:
            from sqlalchemy import text as _tmig2
            db.session.execute(_tmig2(_stmt))
            db.session.commit()
        except Exception:
            db.session.rollback()

with admin_app.app_context():
    try:
        db.create_all()
        _default_services = [
            ('Ball Race Installation',      'Steering ball race replacement',         350),
            ('Brake Cleaning',              'Brake system cleaning',                  200),
            ('Change Brake Pad',            'Brake pad replacement',                  300),
            ('Change Oil',                  'Engine oil replacement',                 350),
            ('CVT Cleaning',               'CVT belt & pulley cleaning',             500),
            ('CVT Upgrade',                 'CVT performance upgrade',                700),
            ('Diagnostic (API Tech / MST)', 'Electronic diagnostic scan',             300),
            ('FI Cleaning',                 'Fuel injection system cleaning',         600),
            ('Full Maintenance Package',    'Complete maintenance service',          1200),
            ('General Rewiring',            'Full electrical rewiring',               500),
            ('Horn Installation',           'Horn install & wiring',                  150),
            ('Overhaul',                    'Full engine overhaul',                  2500),
            ('Remap',                       'ECU remapping & tuning',                1500),
            ('Rubber Link Stopper',         'Rubber link stopper replacement',        100),
            ('Suspension Tuning',           'Front & rear suspension setup',          400),
            ('Throttle Body Cleaning',      'Clean throttle body assembly',           400),
            ('Top Overhaul',               'Top-end engine rebuild',                 1500),
            ('Tune-Up',                     'Spark plug, filters & adjustment',       800),
        ]
        for name, desc, price in _default_services:
            svc = Service.query.filter_by(name=name).first()
            if not svc:
                db.session.add(Service(name=name, description=desc, price=price))
            else:
                if not svc.description:
                    svc.description = desc
                if not svc.price:
                    svc.price = price
        db.session.commit()
        print('[MIGRATION] Service table ready')
    except Exception as e:
        db.session.rollback()
        print('[MIGRATION] Service table error:', e)

if __name__ == '__main__':
    # ADMIN PORTAL — runs on port 5001
    admin_app.run(debug=True, port=5001, use_reloader=False)