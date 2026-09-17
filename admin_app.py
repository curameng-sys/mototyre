from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, redirect, url_for, flash, request, session, jsonify, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, date
from sqlalchemy import func, and_, not_
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
from order_notifications import order_status_message, with_stamp, shipping_destination
from service_duration import (split_service_names, DEFAULT_DURATION_MIN, MULTIDAY_INTAKE_MIN,
    compute_finish_time, mechanic_origin_note, compute_finish_minutes, all_slot_starts,
    SHOP_CLOSE_MIN, mechanic_overlaps, minutes_to_ampm, hhmm_to_minutes, minutes_to_hhmm,
    validate_booking, MAX_BOOKINGS_PER_DAY, max_jobs_one_mechanic, capacity_bottleneck,
    SLOT_GRANULARITY_MIN, SHOP_OPEN_MIN, real_end_minutes, multiday_progress, format_duration,
    MULTIDAY_MIN_DAYS, MULTIDAY_MAX_DAYS, is_working_day, _overlaps, working_days_between,
    slot_statuses, MECHANIC_SPECIALIZATIONS)
import json
import os, uuid, random, string, base64, requests
import threading

# App setup

# Same DATABASE_URL env var and same default as app.py — one database, read
# identically by both apps, never two separately-maintained connection strings.
DATABASE_URL = os.getenv('DATABASE_URL', 'mysql+pymysql://root:@localhost:3306/mototyre')

admin_app = Flask(__name__, template_folder='templates', static_folder='static')
admin_app.config.update(
    SECRET_KEY=os.getenv('ADMIN_SECRET_KEY', 'mototyre-admin-secret-key-aP5nQ9vX2kR8mT6yW1'),
    SESSION_COOKIE_NAME='mototyre_admin_session',
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', 'false').lower() == 'true',
    SQLALCHEMY_DATABASE_URI=DATABASE_URL,
    SQLALCHEMY_ENGINE_OPTIONS={},
    SQLALCHEMY_TRACK_MODIFICATIONS=False
)

db = SQLAlchemy(admin_app)
login_manager = LoginManager(admin_app)
login_manager.login_view = 'admin_login'

# Gmail config

GMAIL_SCOPES     = ["https://www.googleapis.com/auth/gmail.send"]
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_CREDS_FILE = "credentials.json"
GMAIL_SENDER     = os.getenv("GMAIL_SENDER", "mototyre0505@gmail.com")
OTP_EXPIRY_MINS  = 2

# PayMongo config

PAYMONGO_SECRET_KEY = os.getenv("PAYMONGO_SECRET_KEY", "sk_test_qzA2hw8wmbB6AR46TSWYjKPV")
PAYMONGO_API_URL = "https://api.paymongo.com/v1"
BASE_URL = os.getenv("BASE_URL", "https://h4fjzg66-5000.jpe1.devtunnels.ms")

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
        except Exception as e:
            print(f"[GMAIL ADMIN] Send failed: {e}")
    threading.Thread(target=_do_send, daemon=True).start()

def send_otp_email(email, otp, purpose="login"):
    configs = {
        "login": ("Your MotoTyre Admin login code", "Admin Login Verification", "complete your admin login"),
        "reset": ("Your MotoTyre Admin password reset code", "Admin Password Reset", "reset your admin password"),
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
    created_at  = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    order_items = db.relationship('OrderItem', backref='product', lazy=True)


class Order(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total_amount    = db.Column(db.Float, nullable=False)
    status          = db.Column(db.String(20), default='pending')
    payment_method  = db.Column(db.String(20), default='cash')
    delivery_method = db.Column(db.String(20), default='pickup')
    ship_address    = db.Column(db.Text)
    created_at      = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    items           = db.relationship('OrderItem', backref='order', lazy=True)
    is_archived     = db.Column(db.Boolean, default=False)
    walkin_customer_id = db.Column(db.Integer, nullable=True)
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
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


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
    status           = db.Column(db.String(20), default='pending')  # pending, accepted, rejected
    created_by       = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at       = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    items            = db.relationship('QuotationItem', backref='quotation', lazy=True, cascade='all, delete-orphan')


class QuotationItem(db.Model):
    __tablename__ = 'quotation_item'
    id           = db.Column(db.Integer, primary_key=True)
    quotation_id = db.Column(db.Integer, db.ForeignKey('quotation.id'), nullable=False)
    item_type    = db.Column(db.String(10), default='service')  # 'service' or 'product'
    name         = db.Column(db.String(150), nullable=False)
    quantity     = db.Column(db.Integer, default=1)
    unit_price   = db.Column(db.Float, nullable=False)


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
                       return_request_id=None, collapse_minutes=None):
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
                                 return_request_id=return_request_id))
    db.session.commit()


def booking_service_price(service_str):
    """Sum the catalog price for every service in a booking's (possibly combined,
    comma-separated) service string — a plain name lookup misses combo bookings."""
    names = split_service_names(service_str)
    if not names:
        return 0
    found = {s.name: s.price for s in Service.query.filter(Service.name.in_(names), Service.is_active == True).all()}
    return sum(found.get(n, 0) for n in names)


def booking_finish_time(b):
    """A booking's finish time, break-aware. Prefers the value stored at creation
    time; falls back to computing it via the SAME shared helper the customer-side
    booking flow uses, for legacy bookings made before duration tracking existed —
    so the admin view and the customer view can never disagree."""
    if b.end_time:
        return b.end_time
    return compute_finish_time(b.time, b.duration_minutes or DEFAULT_DURATION_MIN)


admin_app.jinja_env.globals['booking_finish_time'] = booking_finish_time


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
    # Cash pick-up orders are only paid at the counter (Billing page), so they count as
    # revenue only once completed — same treatment as awaiting_payment.
    _order_rev   = db.session.query(func.sum(Order.total_amount)).filter(
        Order.status.notin_(["cancelled", "awaiting_payment"]),
        not_(and_(Order.payment_method == "cash", Order.delivery_method == "pickup", Order.status != "completed")),
    ).scalar() or 0
    _booking_rev = db.session.query(func.sum(Booking.total_amount)).filter(Booking.status == "completed").scalar() or 0
    _jo_rev      = db.session.query(func.sum(Payment.amount)).scalar() or 0
    _total_rev   = _order_rev + _booking_rev + _jo_rev
    all_orders      = Order.query.filter_by(is_archived=False).filter(Order.items.any()).order_by(Order.created_at.desc()).all()
    archived_orders = Order.query.filter_by(is_archived=True).filter(Order.items.any()).order_by(Order.created_at.desc()).all()
    order_ship_json = json.dumps({
        str(o.id): {'delivery': str(o.delivery_method or 'pickup'),
                    'address': str(o.ship_address or ''),
                    'payment': str(o.payment_method or 'cash')}
        for o in all_orders + archived_orders
    })
    return render_template('admin_dashboard.html',
        total_bookings=Booking.query.count(),
        total_orders=Order.query.count(),
        total_users=User.query.count(),
        total_revenue=f'{_total_rev:,.2f}',
        booking_status_counts=dict(db.session.query(Booking.status, func.count(Booking.id)).filter(Booking.is_archived == False).group_by(Booking.status).all()),
        order_status_counts=dict(db.session.query(Order.status, func.count(Order.id)).group_by(Order.status).all()),
        top_services=db.session.query(Booking.service, func.count(Booking.id).label('count')).group_by(Booking.service).order_by(func.count(Booking.id).desc()).limit(5).all(),
        new_users_today=User.query.filter(func.date(User.id) == date.today()).count(),
        recent_bookings=Booking.query.filter_by(is_archived=False).order_by(Booking.created_at.desc()).limit(5).all(),
        all_bookings=Booking.query.filter_by(is_archived=False).order_by(Booking.created_at.desc()).all(),
        all_orders=all_orders,
        all_products=Product.query.all(),
        all_users=User.query.all(),
        all_services=Service.query.order_by(Service.name).all(),
        mechanic_specializations=MECHANIC_SPECIALIZATIONS,
        archived_orders=archived_orders,
        archived_bookings=Booking.query.filter_by(is_archived=True).order_by(Booking.created_at.desc()).all(),
        order_ship_json=order_ship_json,
        now=ph_now(),
        today=ph_now().date(),
    )


def _notify_customer(booking, title, body, priority=False, status=None, collapse_minutes=None):
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
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;">
      <p>Hi {customer.fullname},</p>
      <p>{body}</p>
      <p>— MotoTyre North Caloocan</p>
    </div>"""
    _send_gmail(customer.email, f'{title} — MotoTyre', html)
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


@admin_app.route('/booking/<int:bid>/status', methods=['POST'])
@login_required
@require_admin_or_staff
def update_booking_status(bid):
    booking    = Booking.query.get_or_404(bid)
    new_status = validate_booking_status(clean_str(request.form.get('status', ''), max_len=20))
    if new_status == 'completed':
        flash('Bookings can only be completed via the Billing page.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if new_status in ('in_progress', 'inprogress') and datetime.combine(booking.date, booking.time) > ph_now():
        msg = f'This booking is scheduled for {booking.date.strftime("%b %d, %Y")} at {booking.time.strftime("%I:%M %p")} — it cannot be started before then.'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'error': msg}), 400
        flash(msg, 'danger')
        return redirect(url_for('admin_dashboard'))
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
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'new_status': new_status, 'message': 'Booking status updated!'})
    flash('Booking status updated!', 'success')
    return redirect(url_for('admin_dashboard'))


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

    changed      = new_name != old_name
    had_promise  = bool(booking.preferred_mechanic_name)
    broke_promise = had_promise and new_name != booking.preferred_mechanic_name
    if changed and broke_promise:
        _mechanic_swap_notification(booking, new_name)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'mechanic_name': new_name})
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


def _gather_blocked_intervals(booking_date):
    """Admin-blocked start times for one day, as full-hour windows — folded
    into the shop's queue intervals so a blocked slot reads exactly like an
    already-booked one and an admin action can't be forced through it either."""
    return [
        (t.hour * 60 + t.minute, t.hour * 60 + t.minute + SLOT_GRANULARITY_MIN)
        for t in (bs.time for bs in BlockedSlot.query.filter_by(date=booking_date).all())
    ]


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

    mechanic_status = None
    mechanic_intervals = None
    if mechanic_name:
        # "On duty" is roster-count AND individually-available — not just the
        # profile toggle — so a mechanic dialed out by the capacity slider
        # reads exactly the same as one marked off duty on their own profile.
        _, _, on_duty = get_on_duty_mechanics(booking_date)
        mechanic_status = 'available' if any(m.name == mechanic_name for m in on_duty) else 'off duty'
        mq = Booking.query.filter(
            Booking.assigned_mechanic_name == mechanic_name,
            Booking.date == booking_date,
            Booking.status != 'cancelled',
        )
        if exclude_id:
            mq = mq.filter(Booking.id != exclude_id)
        mechanic_intervals = _gather_intervals(mq)

    return validate_booking(
        start_minutes, duration_minutes, shop_intervals, daily_count,
        require_slot_grid=require_slot_grid, daily_cap=get_daily_cap(booking_date),
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


def _fix2_preview(booking):
    """Fix #2: keep the assigned mechanic, move the time — a later slot the
    same day first, then the first workable date within 14 days."""
    mechanic = booking.assigned_mechanic_name
    if not mechanic:
        return None
    duration = booking.duration_minutes or DEFAULT_DURATION_MIN
    start_min = booking.time.hour * 60 + booking.time.minute

    for slot in all_slot_starts():
        if slot <= start_min:
            continue
        ok, _ = _check_admin_booking_request(booking.date, slot, duration, exclude_id=booking.id, mechanic_name=mechanic)
        if ok:
            return {'available': True, 'date': booking.date.isoformat(), 'time': minutes_to_hhmm(slot),
                    'label': f"{booking.date.strftime('%b %d')} at {minutes_to_ampm(slot)}", 'same_day': True}

    d = booking.date
    for _ in range(14):
        d = d + timedelta(days=1)
        if not is_working_day(d):
            continue
        for slot in all_slot_starts():
            ok, _ = _check_admin_booking_request(d, slot, duration, exclude_id=booking.id, mechanic_name=mechanic)
            if ok:
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
            f'{date_str}. Collect anytime: Mon–Sat, 8:00 AM – 6:30 PM.')
    emailed = _notify_customer(booking, title, body, priority=False)
    return jsonify({'success': True, 'emailed': emailed, 'message': f'{booking.service} closed — bay freed.'})


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
    if action == 'reset':
        booking.overrun_minutes = 0
    elif action == 'add30':
        booking.overrun_minutes = (booking.overrun_minutes or 0) + 30
    elif action == 'add60':
        booking.overrun_minutes = (booking.overrun_minutes or 0) + 60
    else:
        return jsonify({'success': False, 'error': 'Unknown action.'}), 400
    db.session.commit()
    return jsonify({'success': True, 'overrun_minutes': booking.overrun_minutes})


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
    data = request.get_json() or {}
    notify = data.get('notify', True)
    reason = clean_str(data.get('reason', ''), max_len=200)
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
        body = (f"Your {booking.service} appointment ({ref}) on {date_str} at {time_str} was cancelled — "
                f"{reason_text}. Rebook anytime, or reply to this email with questions.")
        emailed = _notify_customer(booking, title, body, priority=True)
    return jsonify({'success': True, 'emailed': emailed,
                     'message': f'Cancelled{" — customer notified." if emailed else (" — customer not notified." if not notify else " — no email on file.")}'})


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

    # ── Mechanic timeline ──
    timeline = []
    free_all_day = []
    for m in on_duty:
        m_bookings = [b for b in active if b.assigned_mechanic_name == m.name]
        bars = []
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
            })

        active_here = [b for b in slot_bookings if b.status != 'cancelled']
        slots.append({
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
        clashing_ids = {c['id'] for c in _compute_day_clashes(d, bookings=day_bookings)}

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

    def sort_key(n):
        return (0 if (n.priority and not n.is_read) else 1, -n.created_at.timestamp())

    rows.sort(key=sort_key)

    items = []
    for n in rows:
        b = bookings.get(n.booking_id) if n.booking_id else None
        u = users.get(n.user_id)
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
            'created_at': n.created_at.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
        })

    unread_count = Notification.query.filter_by(is_read=False).count()
    return jsonify({'items': items, 'unread_count': unread_count})


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
    out = []
    for row in rows:
        item = OrderItem.query.get(row.order_item_id)
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


def _notify_return_customer(rr, title, body, priority=True):
    """Same shape as _notify_customer for bookings — bell + email, identical
    text, so a return decision never reads differently between the two.
    Opening it lands on this specific claim (Screen 3), not the list."""
    send_notification(rr.user_id, title, body, type='booking' if rr.kind == 'service' else 'order',
                       status=rr.status, priority=priority, return_request_id=rr.id)
    customer = User.query.get(rr.user_id)
    if not (customer and customer.email):
        return False
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;">
      <p>Hi {customer.fullname},</p>
      <p>{body}</p>
      <p>— MotoTyre North Caloocan</p>
    </div>"""
    _send_gmail(customer.email, f'{title} — MotoTyre', html)
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

        is_open = r.status in OPEN_RETURN_STATUSES
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
        if r.status in ('resolved', 'denied', 'cancelled'): filter_counts['closed'] += 1

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
            'subject': _return_subject_label(r),
            'line_items': line_items,
            'reasons': reason_labels,
            'other_reason_text': r.other_reason_text,
            'desired_outcome': r.desired_outcome,
            'requested_mechanic_name': r.requested_mechanic_name,
            'requested_refund_amount': r.requested_refund_amount,
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
                title = 'Approved — replacement instead of a refund' if alternate else 'Approved — return the part for a replacement'
                body = ((f'Your {subject} claim ({ref}) was approved, but instead of a refund we\'re sending a '
                         f'replacement:{reason_line} Return the item to the shop so we can send the replacement.') if alternate else
                        (f'Your {subject} claim ({ref}) was approved: replacement. Return the item to the shop '
                         f'so we can send the replacement. Reason: {reason}.'))
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
        body = (f'Your return request ({ref}) about {subject} was not approved. Reason: {reason}. '
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
    note = clean_str((request.get_json() or {}).get('note', ''), max_len=200)
    if not note:
        return jsonify({'success': False, 'error': 'Say what you need from the customer.'}), 400

    rr.status = 'under_review'
    rr.awaiting_customer_info = True
    rr.info_request_note = note
    db.session.commit()

    ref = _return_ref(rr)
    body = f'About your {_return_subject_label(rr)} claim ({ref}): {note}'
    emailed = _notify_return_customer(rr, note, body, priority=True)
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
    rr.item_returned = True
    rr.item_returned_at = ph_now()
    db.session.commit()

    ref = _return_ref(rr)
    subject = _return_subject_label(rr)
    if rr.resolution == 'refund':
        title = 'We received your item — refund next'
        body = f'We received the {subject} you sent back ({ref}). Your ₱{rr.refund_amount:,.2f} refund is next.'
    else:
        title = 'We received your item — replacement next'
        body = f'We received the {subject} you sent back ({ref}). Your replacement ships next, checked before it leaves.'
    emailed = _notify_return_customer(rr, title, body, priority=False)
    return jsonify({'success': True, 'emailed': emailed, 'message': 'Item marked received — customer notified.'})


def _return_original_service(rr):
    """The original booking's duration and service name — a back job runs as
    long as the job it's redoing, not a generic default, and the calendar
    entry should read like the job it actually is."""
    b = Booking.query.get(rr.booking_id) if rr.booking_id else None
    if b:
        return b.duration_minutes or DEFAULT_DURATION_MIN, b.service
    return DEFAULT_DURATION_MIN, _return_subject_label(rr)


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
    intervals = _gather_intervals(q) + _gather_blocked_intervals(the_date)
    now_minutes = None
    if the_date == ph_now().date():
        now = ph_now()
        now_minutes = now.hour * 60 + now.minute

    slots = slot_statuses(duration, intervals, now_minutes)
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
    ok, error = _check_admin_booking_request(redo_date, start_min, duration, mechanic_name=mechanic_name)
    if not ok:
        return jsonify({'success': False, 'error': f'{error} Pick again.'}), 409

    mechanic = Mechanic.query.filter_by(name=mechanic_name).first()
    customer = User.query.get(rr.user_id)
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


@admin_app.route('/returns/<int:rid>/resolve', methods=['POST'])
@login_required
@require_admin_or_staff
def resolve_return_request(rid):
    """Marks an approved claim's remedy as actually carried out — the refund
    was issued, the replacement sent, the back job redone. A product claim
    can't resolve until its item is marked received; a back job can't
    resolve until it's been scheduled — carrying out a remedy that was never
    actually delivered isn't something this button can paper over."""
    rr = ReturnRequest.query.get_or_404(rid)
    if rr.status != 'approved':
        return jsonify({'success': False, 'error': 'Only an approved claim can be marked resolved.'}), 400
    if rr.kind == 'product' and not rr.item_returned:
        return jsonify({'success': False, 'error': "Mark the item received first — the part hasn't come back yet."}), 400
    if rr.kind == 'service' and rr.resolution == 'redo_service' and not rr.redo_date:
        return jsonify({'success': False, 'error': 'Schedule the back job first.'}), 400

    rr.status = 'resolved'
    rr.resolved_at = ph_now()

    # The back job is a real appointment on the shop's calendar — closing the
    # claim closes that booking too, so the mechanic's day reflects it.
    if rr.kind == 'service' and rr.resolution == 'redo_service' and rr.redo_booking_id:
        b = Booking.query.get(rr.redo_booking_id)
        if b:
            b.status = 'completed'
            b.completed_at = ph_now()

    db.session.commit()

    ref = _return_ref(rr)
    subject = _return_subject_label(rr)
    if rr.resolution == 'refund':
        title = f'Your refund of ₱{rr.refund_amount:,.2f} has been released'
        body = f'Your refund for {subject} ({ref}) — ₱{rr.refund_amount:,.2f} — has been released to your original payment method.'
    elif rr.resolution == 'replacement':
        title = 'Your replacement has shipped'
        body = f'The replacement for {subject} ({ref}) is on its way — checked before it left.'
    else:
        title = 'Your back job is complete'
        body = f'The redo for {subject} ({ref}) is done.'
    emailed = _notify_return_customer(rr, title, body, priority=False)
    return jsonify({'success': True, 'emailed': emailed, 'message': 'Marked resolved — customer notified.'})


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


@admin_app.route('/order/<int:oid>/status', methods=['POST'])
@login_required
@require_admin_or_staff
def update_order_status(oid):
    order = Order.query.get_or_404(oid)
    new_status = validate_order_status(clean_str(request.form.get('status', ''), max_len=20))
    _pay = (order.payment_method or '').lower()
    _is_pickup = order.delivery_method != 'ship'

    if new_status == 'completed' and _pay == 'cash' and _is_pickup:
        msg = 'Cash pick-up orders are completed on the Billing page when the customer pays at the counter.'
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

    # Pick-up orders follow a fixed sequence.
    #   GCash (prepaid):  confirmed -> shipped (ready for pickup) -> completed
    #   Cash  (pay at counter): pending/awaiting_payment -> confirmed -> shipped (ready for pickup)
    #                           (completed happens on the Billing page)
    if _is_pickup:
        if _pay == 'gcash':
            allowed_next = {
                'confirmed': {'shipped', 'cancelled'},
                'shipped':   {'completed', 'cancelled'},
            }
        else:
            allowed_next = {
                'pending':          {'confirmed', 'cancelled'},
                'awaiting_payment': {'confirmed', 'cancelled'},
                'confirmed':        {'shipped', 'cancelled'},
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
        send_notification(order.user_id, title, msg, type='order', status=new_status)
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
        return jsonify([{'id': s.id, 'name': s.name, 'price': s.price} for s in svcs])
    except Exception:
        return jsonify([])



@admin_app.route('/product/add', methods=['POST'])
@login_required
@require_admin_or_staff
def add_product():
    db.session.add(Product(
        name=request.form['name'], category=request.form['category'],
        description=request.form.get('description', ''),
        price=float(request.form['price']), stock=int(request.form['stock'])
    ))
    db.session.commit()
    flash('Product added successfully!', 'success')
    return redirect(url_for('admin_dashboard'))


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
    db.session.commit()
    flash(f'Product "{p.name}" updated!', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/product/<int:pid>/delete', methods=['POST'])
@login_required
@require_admin_or_staff
def delete_product(pid):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    p = Product.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    flash(f'Product "{p.name}" deleted!', 'success')
    return redirect(url_for('admin_dashboard'))


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

    upcoming_bookings = Booking.query.filter(
        Booking.date >= today, Booking.status.notin_(['cancelled', 'completed']),
    ).order_by(Booking.date, Booking.time).all()
    by_mechanic = {}
    for b in upcoming_bookings:
        if b.assigned_mechanic_name:
            by_mechanic.setdefault(b.assigned_mechanic_name, []).append(b)

    result = []
    for m in mechanics:
        jobs = by_mechanic.get(m.name, [])
        current, nxt, has_job_today = None, None, False
        for b in jobs:
            b_start = b.time.hour * 60 + b.time.minute
            b_end = real_end_minutes(b_start, b.duration_minutes or DEFAULT_DURATION_MIN, b.overrun_minutes or 0)
            is_today = b.date == today
            if is_today:
                has_job_today = True
            job = {
                'booking_id': b.id, 'ref': f'BKG-{b.id:03d}',
                'customer': _booking_customer_name(b), 'service': b.service,
                'date_label': b.date.strftime('%b %d'), 'start_label': minutes_to_ampm(b_start),
                'end_label': minutes_to_ampm(b_end), 'is_today': is_today,
            }
            if is_today and b_start <= now_min < b_end and current is None:
                current = job
            if ((not is_today) or (is_today and b_start > now_min)) and nxt is None:
                nxt = job
            if current and nxt:
                break

        assigned = current or nxt or (jobs and {
            'booking_id': jobs[0].id, 'ref': f'BKG-{jobs[0].id:03d}',
            'customer': _booking_customer_name(jobs[0]), 'service': jobs[0].service,
            'date_label': jobs[0].date.strftime('%b %d'),
            'start_label': minutes_to_ampm(jobs[0].time.hour * 60 + jobs[0].time.minute),
            'end_label': minutes_to_ampm(real_end_minutes(jobs[0].time.hour * 60 + jobs[0].time.minute,
                                                            jobs[0].duration_minutes or DEFAULT_DURATION_MIN, jobs[0].overrun_minutes or 0)),
            'is_today': jobs[0].date == today,
        }) or None
        more_count = max(0, len(jobs) - 1) if assigned else 0

        # The manual fields are a fallback, never an override: a real booking
        # always wins, and what's typed here only surfaces when there isn't
        # one — always labelled so it's never mistaken for schedule data.
        assigned_is_manual = False
        if not assigned and (m.manual_customer or m.manual_service):
            assigned = {'customer': m.manual_customer or '', 'service': m.manual_service or ''}
            assigned_is_manual = True

        if m.status == 'off_duty':
            effective_status = 'off_duty'
        elif has_job_today:
            effective_status = 'busy'
        else:
            effective_status = 'busy' if m.status == 'busy' else 'available'

        result.append({
            'id': m.id, 'name': m.name, 'specialization': m.specialization, 'status': m.status,
            'phone': m.phone, 'note': m.note,
            'not_rostered_today': m.name not in rostered_names,
            'effective_status': effective_status,
            'active_jobs_count': len(jobs),
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

    items = []
    for b in bookings:
        start_min = b.time.hour * 60 + b.time.minute
        duration = b.duration_minutes or DEFAULT_DURATION_MIN
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
    m.name = name
    m.specialization = specialization
    m.phone = phone or None
    m.note = note or None
    m.manual_customer = manual_customer or None
    m.manual_service = manual_service or None
    m.status = status
    db.session.commit()
    return jsonify({'success': True})


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


@admin_app.route('/add-staff', methods=['POST'])
@login_required
def add_staff():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    email = request.form['email']
    if User.query.filter_by(email=email).first():
        flash('Email already exists.', 'danger')
        return redirect(url_for('admin_dashboard'))
    staff = User(fullname=request.form['fullname'], email=email,
                 phone=request.form['phone'], role='staff', email_verified=True)
    staff.set_password(request.form['password'])
    db.session.add(staff)
    db.session.commit()
    flash(f'Staff account for {staff.fullname} created!', 'success')
    return redirect(url_for('admin_dashboard'))


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
            'Your account has been banned due to a violation of MotoTyre\'s policies. '
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
            'Your account has been flagged for performing actions that violate MotoTyre\'s '
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


@admin_app.route('/profile/upload-pic', methods=['POST'])
@login_required
@require_admin_or_staff
def upload_profile_pic():
    file = request.files.get('profile_pic')
    if not file or file.filename == '':
        flash('No file selected.', 'danger')
    elif allowed_file(file.filename):
        ext      = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{current_user.id}_{uuid.uuid4().hex}.{ext}"
        folder   = os.path.join(admin_app.root_path, 'static', 'profile_pics')
        os.makedirs(folder, exist_ok=True)
        file.save(os.path.join(folder, filename))
        current_user.profile_pic = filename
        db.session.commit()
        flash('Profile picture updated!', 'success')
    else:
        flash('Invalid file type.', 'danger')
    return redirect(url_for('admin_dashboard'))


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
    return render_template('quotation.html', products=products, services=services, mechanics=mechanics)


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
    q = Quotation(
        customer_name    = data.get('customer_name', '').strip(),
        customer_phone   = data.get('customer_phone', '').strip(),
        motorcycle_model = data.get('motorcycle_model', '').strip(),
        motorcycle_plate = data.get('motorcycle_plate', '').strip(),
        notes            = data.get('notes', '').strip(),
        total_amount     = total,
        status           = 'pending',
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
        ))
    db.session.commit()
    return jsonify({'success': True, 'quotation_id': q.id, 'ref': f'QUO-{q.id:03d}'})


@admin_app.route('/quotations')
@login_required
@require_admin_or_staff
def quotations_list():
    status_filter = request.args.get('status', 'all')
    q = Quotation.query
    if status_filter != 'all':
        q = q.filter_by(status=status_filter)
    quotations = q.order_by(Quotation.created_at.desc()).all()
    return render_template('quotations_list.html', quotations=quotations, status_filter=status_filter)


@admin_app.route('/quotation/<int:qid>/convert', methods=['POST'])
@login_required
@require_admin_or_staff
def convert_quotation(qid):
    quot = Quotation.query.get_or_404(qid)
    if quot.status != 'pending':
        flash('Only pending quotations can be converted.', 'danger')
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
    quot.status = 'accepted'
    db.session.commit()
    flash(f'Quotation QUO-{quot.id:03d} converted to Job Order JO-{jo.id:03d}.', 'success')
    return redirect(url_for('job_orders'))


@admin_app.route('/quotation/<int:qid>/reject', methods=['POST'])
@login_required
@require_admin_or_staff
def reject_quotation(qid):
    quot = Quotation.query.get_or_404(qid)
    quot.status = 'rejected'
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
    return render_template('job_orders.html', job_orders=orders, mechanics=mechanics,
                           status_filter=status_filter, new_jo=new_jo)


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


@admin_app.route('/payments')
@login_required
@require_admin_or_staff
def payments():
    tab = request.args.get('tab', 'pending')

    if tab == 'pending':
        paid_jo_ids = [r[0] for r in db.session.query(Payment.job_order_id).filter(Payment.job_order_id != None).all()]
        pending_jos = JobOrder.query.filter(
            JobOrder.status == 'in_progress',
            ~JobOrder.id.in_(paid_jo_ids) if paid_jo_ids else True
        ).order_by(JobOrder.created_at.desc()).all()

        pending_bookings = Booking.query.filter(
            Booking.status.in_(['confirmed', 'in_progress', 'inprogress']),
            Booking.payment_method.in_(['cash', None]),
            Booking.walkin_customer_id == None,
            Booking.is_archived == False
        ).order_by(Booking.created_at.desc()).all()

        pending_orders = Order.query.filter(
            Order.payment_method == 'cash',
            Order.delivery_method == 'pickup',
            Order.status.in_(['pending', 'awaiting_payment', 'confirmed', 'shipped']),
            Order.walkin_customer_id == None,
            Order.is_archived == False
        ).filter(Order.items.any()).order_by(Order.created_at.desc()).all()

        history_jos      = []
        history_bookings = []
        history_orders   = []
    else:
        pending_jos      = []
        pending_bookings = []
        pending_orders   = []

        history_jos = (JobOrder.query
                       .join(Payment, Payment.job_order_id == JobOrder.id)
                       .order_by(Payment.paid_at.desc()).limit(50).all())

        history_bookings = (Booking.query
                            .filter(Booking.status == 'completed', Booking.total_amount > 0,
                                    Booking.is_archived == False, Booking.walkin_customer_id == None)
                            .order_by(Booking.created_at.desc()).limit(50).all())

        history_orders = (Order.query
                          .filter(Order.payment_method == 'cash', Order.delivery_method == 'pickup',
                                  Order.status == 'completed')
                          .filter(Order.items.any())
                          .order_by(Order.created_at.desc()).limit(50).all())

    return render_template('payments.html',
        tab=tab,
        pending_jos=pending_jos,
        pending_bookings=pending_bookings,
        pending_orders=pending_orders,
        history_jos=history_jos,
        history_bookings=history_bookings,
        history_orders=history_orders,
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
    if order.payment_method != 'cash' or order.delivery_method != 'pickup':
        return jsonify({'success': False, 'error': 'Only cash pick-up orders can be billed here'}), 400
    order.status = 'completed'
    if not order.delivered_at:
        order.delivered_at = ph_now()
    db.session.commit()
    send_notification(order.user_id, 'Order Completed!',
        f'Your order ORD-{order.id:03d} has been picked up and payment collected. Thank you!',
        type='order', status='completed')
    return jsonify({'success': True, 'ref': f'ORD-{order.id:03d}', 'amount': order.total_amount})


@admin_app.route('/api/transactions')
@login_required
@require_admin_or_staff
def api_transactions():
    txns = []

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

    online_orders = Order.query.filter(
        Order.walkin_customer_id == None, Order.status == 'completed'
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
        })
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
        uid = int(customer_id) if customer_id else current_user.id
        new_total = sum(item['quantity'] * float(item['unit_price']) for item in new_cart_items)
        order = Order(user_id=uid, total_amount=new_total, status='completed', payment_method=payment_method,
                      delivered_at=ph_now())
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
    return render_template('walkin.html', products=products, mechanics=mechanics, services=services)


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

    if not customer_name:
        return jsonify({'success': False, 'error': 'Customer name is required'}), 400
    if not services and not cart:
        return jsonify({'success': False, 'error': 'No services or products added'}), 400

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

    total = sum(i['unit_price'] * i['quantity'] for i in items)

    try:
        jo = JobOrder(
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
    story.append(Paragraph('0915 269 8366  ·  Mon–Sat 8AM–7PM', sm))
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
    story.append(Paragraph('Thank you for choosing MotoTyre!', sm))
    story.append(Paragraph(f'Transaction: {txn_ref}', s('tf', alignment=1, fontName='Helvetica', fontSize=7, textColor=LGREY)))

    doc.build(story)
    buffer.seek(0)
    resp = make_response(buffer.read())
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = f'inline; filename=receipt_{txn_ref}.pdf'
    return resp


# Report routes

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

    def order_counts_as_revenue(o):
        return (o.status not in ('cancelled', 'awaiting_payment')
                and not (o.payment_method == 'cash' and o.delivery_method == 'pickup' and o.status != 'completed'))

    # Total Revenue mirrors the dashboard's definition (orders + completed bookings +
    # job-order payments) — previously this only summed orders, so it never matched
    # the "Total Revenue" figure shown on the Reports & Sales page above it.
    order_revenue = sum(o.total_amount for o in orders if order_counts_as_revenue(o))
    booking_revenue = sum(b.total_amount or 0 for b in bookings if b.status == 'completed')
    job_order_revenue = db.session.query(func.sum(Payment.amount)).filter(
        func.date(Payment.paid_at) >= d_from, func.date(Payment.paid_at) <= d_to
    ).scalar() or 0

    total_revenue      = order_revenue + booking_revenue + job_order_revenue
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

    # Top-selling products — only from orders that weren't cancelled, since a
    # cancelled order was never actually sold.
    product_sales = defaultdict(lambda: [0, 0.0])
    for o in orders:
        if o.status == 'cancelled':
            continue
        for item in o.items:
            pname = item.product.name if item.product else 'Unknown Product'
            product_sales[pname][0] += item.quantity
            product_sales[pname][1] += item.quantity * item.unit_price
    top_products = sorted(product_sales.items(), key=lambda kv: kv[1][1], reverse=True)[:8]

    # Top services booked — only non-cancelled bookings; revenue only from completed ones.
    service_bookings = defaultdict(lambda: [0, 0.0])
    for b in bookings:
        if b.status == 'cancelled':
            continue
        service_bookings[b.service][0] += 1
        if b.status == 'completed':
            service_bookings[b.service][1] += (b.total_amount or 0)
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

    story.append(Paragraph('MOTOTYRE MOTO SHOP', title_style))
    story.append(Paragraph('Sales & Operations Report', style('ST', fontSize=13, fontName='Helvetica-Bold', textColor=DARK, spaceAfter=2)))
    story.append(Paragraph(f'Period: {d_from.strftime("%b %d, %Y")} — {d_to.strftime("%b %d, %Y")}', sub_style))
    story.append(Paragraph(f'Generated: {ph_now().strftime("%b %d, %Y at %I:%M %p")}', sub_style))
    story.append(HRFlowable(width=W, thickness=2, color=RED, spaceAfter=12))

    story.append(Paragraph('SUMMARY', heading_style))
    summary_data = [
        ['Total Revenue', 'Total Orders', 'Total Bookings', 'Completed Orders'],
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
            ['TOTAL REVENUE', f'P{total_revenue:,.2f}'],
        ],
        [W*0.65, W*0.35], align_right_cols=(1,), bold_last_row=True
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

    story.append(Paragraph('TOP-SELLING PRODUCTS', heading_style))
    if top_products:
        story.append(section_table(
            ['Product', 'Qty Sold', 'Revenue'],
            [[name, str(int(q)), f'P{rev:,.2f}'] for name, (q, rev) in top_products],
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
    story.append(Paragraph('This report was automatically generated by MotoTyre Admin Dashboard.', small_style))

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


@admin_app.route('/staff/dashboard')
@login_required
def staff_dashboard():
    if current_user.role != 'staff':
        return redirect(url_for('admin_dashboard'))
    return render_template('staff_dashboard.html',
        all_bookings=Booking.query.order_by(Booking.created_at.desc()).all(),
        all_orders=Order.query.order_by(Order.created_at.desc()).all(),
        all_customers=User.query.filter_by(role='customer').all(),
        all_products=Product.query.all(),
        recent_bookings=Booking.query.order_by(Booking.created_at.desc()).limit(5).all(),
        today=date.today(),
        booking_status_counts=dict(db.session.query(Booking.status, func.count(Booking.id)).group_by(Booking.status).all()),
        order_status_counts=dict(db.session.query(Order.status, func.count(Order.id)).group_by(Order.status).all())
    )


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