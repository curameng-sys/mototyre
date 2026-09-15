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
from security import clean_str, clean_int, clean_float, is_valid_email, is_valid_phone, validate_otp_purpose
from order_notifications import order_status_message, with_stamp, shipping_destination
from service_duration import (
    DEFAULT_DURATION_MIN, combine_services, combined_service_name, split_service_names,
    format_duration, minutes_to_ampm, minutes_to_hhmm, hhmm_to_minutes, slot_statuses,
    all_slot_starts, SHOP_CLOSE_MIN, compute_finish_minutes, mechanic_overlaps,
    mechanic_origin_note, validate_booking,
)
from gmail_helper import send_gmail_html as _send_gmail, send_otp_email
import os, uuid, random, string, base64, requests
from urllib.parse import quote
import pymysql
import threading

def _ensure_database():
    conn = pymysql.connect(host='localhost', port=3306, user='root', password='')
    try:
        conn.cursor().execute("CREATE DATABASE IF NOT EXISTS mototyre CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        conn.commit()
    finally:
        conn.close()

_ensure_database()

app = Flask(__name__)
app.config.update(
    SECRET_KEY='mototyre-fixed-secret-key-xK9mP2qL7rZ3wN8vB4',
    SESSION_COOKIE_NAME='mototyre_customer_session',
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_DOMAIN=None,
    REMEMBER_COOKIE_SAMESITE='Lax',
    REMEMBER_COOKIE_SECURE=False,
    SQLALCHEMY_DATABASE_URI="mysql+pymysql://root:@localhost:3306/mototyre",
    SQLALCHEMY_ENGINE_OPTIONS={},
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

# Hosts the customer is allowed to be redirected back to after payment.
ALLOWED_RETURN_ORIGINS = [
    "http://127.0.0.1:5000",
    "http://localhost:5000",
    "https://h4fjzg66-5000.jpe1.devtunnels.ms",
]

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


def send_booking_confirmation_email(booking):
    """States the mechanic and where they came from, using the same phrasing
    the admin-side reassignment email uses (mechanic_origin_note), so a
    customer never sees two different explanations for the same fact."""
    try:
        user = User.query.get(booking.user_id)
        if not user or not user.email:
            return

        date_str = booking.date.strftime("%B %d, %Y")
        time_str = booking.time.strftime("%I:%M %p")
        if booking.is_multiday:
            when_line = f"{time_str} drop-off on {date_str}"
        elif booking.end_time:
            when_line = f"{time_str} – {booking.end_time.strftime('%I:%M %p')} on {date_str}"
        else:
            when_line = f"{time_str} on {date_str}"

        origin = mechanic_origin_note(booking.assigned_mechanic_name, booking.preferred_mechanic_name)
        if booking.assigned_mechanic_name:
            mechanic_row = f"""
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">Mechanic</td>
                <td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">
                  {booking.assigned_mechanic_name} <span style="color:#9ca3af;">({origin})</span>
                </td>
              </tr>"""
        else:
            mechanic_row = """
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">Mechanic</td>
                <td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">We'll assign one closer to your appointment</td>
              </tr>"""

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
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">Order</td>
                <td style="padding:6px 0;color:#111827;font-size:13px;font-weight:700;text-align:right;">ORD-{booking.id:03d}</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">Service</td>
                <td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">{booking.service}</td>
              </tr>
              <tr>
                <td style="padding:6px 0;color:#6b7280;font-size:13px;">When</td>
                <td style="padding:6px 0;color:#111827;font-size:13px;text-align:right;">{when_line}</td>
              </tr>{mechanic_row}
            </table>
          </div>
          <div style="background:#f9fafb;border-top:1px solid #f3f4f6;padding:20px 32px;text-align:center;">
            <div style="color:#6b7280;font-size:12px;line-height:1.7;">
              <strong style="color:#111827;">MotoTyre North Caloocan</strong><br>
              Saranay Rd, Brgy. 171 Bagumbong, Caloocan City<br>
              &#128222; 0915 269 8366 &nbsp;|&nbsp; Mon–Sat 8:00 AM – 7:00 PM
            </div>
          </div>
        </div>"""

        _send_gmail(user.email, f"Booking Confirmed — ORD-{booking.id:03d}", html)
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
    assigned_mechanic_name           = db.Column(db.String(100))  # who is actually doing the work — shop changes this freely
    assigned_mechanic_specialization = db.Column(db.String(100))
    preferred_mechanic_name           = db.Column(db.String(100))  # who the customer asked for, or None — never overwritten after booking creation
    preferred_mechanic_specialization = db.Column(db.String(100))
    contact_name   = db.Column(db.String(100))
    contact_mobile = db.Column(db.String(20))
    odometer       = db.Column(db.Integer)
    is_archived    = db.Column(db.Boolean, default=False)
    booking_batch  = db.Column(db.String(36), nullable=True)
    duration_minutes = db.Column(db.Integer, default=60)  # total estimated job length
    end_time         = db.Column(db.Time, nullable=True)  # computed: time + duration
    is_multiday      = db.Column(db.Boolean, default=False)


class Mechanic(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(100), nullable=False)
    specialization = db.Column(db.String(100), nullable=False)
    status         = db.Column(db.String(20), default='available')
    created_at     = db.Column(db.DateTime, default=ph_now)


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


class Notification(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title      = db.Column(db.String(120), nullable=False)
    message    = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(30), default='update')
    status     = db.Column(db.String(30))
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=ph_now)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def send_notification(user_id, title, message, type='update', status=None):
    db.session.add(Notification(user_id=user_id, title=title, message=message, type=type, status=status))
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
    return render_template('landing.html')

@app.route('/products')
def products():
    return render_template('Products.html')


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
    return render_template('customer_dashboard.html', bookings=bookings, orders=orders,
                           products=products, services=services)


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

    mechanic = None
    mechanic_status = None
    mechanic_intervals = None
    if mechanic_id:
        mechanic = Mechanic.query.get(int(mechanic_id)) if str(mechanic_id).isdigit() else None
        if not mechanic:
            return None, 'That mechanic could not be found. Please choose someone else.'
        mechanic_status = mechanic.status
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

    done_text = (f" Estimated completion: {combo['duration_label']}." if combo['is_multiday']
                 else f" Done by {minutes_to_ampm(end_min)}.")
    send_notification(
        current_user.id, 'Booking Confirmed! ✅',
        f'Your {service} appointment on {booking.date.strftime("%b %d, %Y")} at {booking.time.strftime("%I:%M %p")} is confirmed.{done_text} Please arrive 15 minutes early.',
        type='booking', status='confirmed'
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
    booking_summaries = []
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
        created_bookings.append(booking)
        done_text = (f"est. {combo['duration_label']}" if combo['is_multiday']
                     else f"done by {minutes_to_ampm(end_min)}")
        booking_summaries.append({
            'service': service,
            'date':    booking.date.strftime('%b %d, %Y'),
            'time':    booking.time.strftime('%I:%M %p'),
            'done':    done_text,
        })

    if created:
        db.session.commit()
        for booking in created_bookings:
            send_booking_confirmation_email(booking)

        # One combined notification instead of one per booking, so a multi-service
        # queue doesn't stack several near-identical notifications for the customer.
        if len(booking_summaries) == 1:
            s = booking_summaries[0]
            send_notification(
                current_user.id, 'Booking Confirmed! ✅',
                f"Your {s['service']} appointment on {s['date']} at {s['time']} is confirmed — "
                f"{s['done']}. Please arrive 15 minutes early.",
                type='booking', status='confirmed'
            )
        else:
            lines = '\n'.join(f"• {s['service']} — {s['date']} at {s['time']} ({s['done']})" for s in booking_summaries)
            send_notification(
                current_user.id, f'{len(booking_summaries)} Bookings Confirmed! ✅',
                f"Your appointments are confirmed:\n{lines}\nPlease arrive 15 minutes early for each.",
                type='booking', status='confirmed'
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
    # Ordered by id (hire/creation order) — a stable stand-in for a rotation queue,
    # used both as the picker's list order and to pick who's "next in line" for
    # shop-assigned bookings (the first one on this list who isn't busy then).
    mechanics = Mechanic.query.filter_by(status='available').order_by(Mechanic.id).all()

    # A mechanic is busy only if their existing job's time window overlaps the
    # slot being picked — not for every booking they've ever had. Busy mechanics
    # are still returned (marked busy) rather than dropped, so the customer sees
    # the whole crew and why someone isn't available right now.
    busy_names = set()
    date_str = request.args.get('date', '').strip()
    time_str = request.args.get('time', '').strip()
    duration = request.args.get('duration', type=int) or DEFAULT_DURATION_MIN
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
    return jsonify([{
        'id': n.id, 'title': n.title, 'message': n.message,
        'type': n.type, 'status': n.status, 'is_read': n.is_read,
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
scheduler.start()
print('[SCHEDULER] Booking reminder service started — checking every 1 minute')
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