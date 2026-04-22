from flask import Flask, render_template, redirect, url_for, flash, request, session, jsonify, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta, date
from sqlalchemy import func
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
from security import clean_str, clean_float, is_valid_email, validate_booking_status, validate_order_status
import json
import os, uuid, random, string, base64, requests

# App setup

admin_app = Flask(__name__, template_folder='templates', static_folder='static')
admin_app.config.update(
    SECRET_KEY='mototyre-admin-secret-key-aP5nQ9vX2kR8mT6yW1',
    SESSION_COOKIE_NAME='mototyre_admin_session',
    SQLALCHEMY_DATABASE_URI=(
        "mysql+pymysql://avnadmin:AVNS_1RNbCP5RORVJ7VaVhRD@"
        "mysql-1d9dceb5-mackycastanales05-1369.f.aivencloud.com:12422/defaultdb"
        "?ssl_ca=ca.pem"
    ),
    SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"ssl_ca": "CA.pem"}},
    SQLALCHEMY_TRACK_MODIFICATIONS=False
)

db = SQLAlchemy(admin_app)
login_manager = LoginManager(admin_app)
login_manager.login_view = 'admin_login'

# Gmail config

GMAIL_SCOPES     = ["https://www.googleapis.com/auth/gmail.send"]
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_CREDS_FILE = "credentials.json"
GMAIL_SENDER     = os.getenv("GMAIL_SENDER", "mackycastanales05@gmail.com")
OTP_EXPIRY_MINS  = 2

# PayMongo config

PAYMONGO_SECRET_KEY = os.getenv("PAYMONGO_SECRET_KEY", "sk_test_qzA2hw8wmbB6AR46TSWYjKPV")
PAYMONGO_API_URL = "https://api.paymongo.com/v1"
BASE_URL = "https://ninja-portion-recycler.ngrok-free.dev"

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

def _get_gmail_service():
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
    return build("gmail", "v1", credentials=creds)

def _send_gmail(to, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["to"], msg["from"], msg["subject"] = to, GMAIL_SENDER, subject
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    _get_gmail_service().users().messages().send(userId="me", body={"raw": raw}).execute()

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
    motorcycle_plate = db.Column(db.String(20))
    motorcycle_model = db.Column(db.String(100))
    profile_pic      = db.Column(db.String(255))
    email_verified   = db.Column(db.Boolean, default=False)
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
    service          = db.Column(db.String(100), nullable=False)
    date             = db.Column(db.Date, nullable=False)
    time             = db.Column(db.Time, nullable=False)
    motorcycle_model = db.Column(db.String(100))
    motorcycle_plate = db.Column(db.String(20))
    notes            = db.Column(db.Text)
    status           = db.Column(db.String(20), default='pending')
    payment_method   = db.Column(db.String(20), default='cash')
    created_at       = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    reminder_sent    = db.Column(db.Boolean, default=False)
    mechanic_name           = db.Column(db.String(100))
    mechanic_specialization = db.Column(db.String(100))
    contact_name    = db.Column(db.String(100))
    contact_mobile  = db.Column(db.String(20))
    odometer        = db.Column(db.Integer)
    is_archived     = db.Column(db.Boolean, default=False)
    total_amount    = db.Column(db.Float, default=0)


class Mechanic(db.Model):
    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(100), nullable=False)
    specialization = db.Column(db.String(100), nullable=False)
    status         = db.Column(db.String(20), default='available')
    created_at     = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))


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
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


class Notification(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title      = db.Column(db.String(120), nullable=False)
    message    = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(30), default='update')
    status     = db.Column(db.String(30))
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# Helpers

def ph_now():
    return datetime.utcnow() + timedelta(hours=8)

def send_notification(user_id, title, message, type='update', status=None):
    db.session.add(Notification(user_id=user_id, title=title, message=message, type=type, status=status))
    db.session.commit()

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
            # Send OTP for 2FA
            otp = _save_otp(email, purpose="login")
            try:
                send_otp_email(email, otp, purpose="login")
            except Exception as e:
                flash(f'Could not send OTP: {e}', 'danger')
                return redirect(url_for('admin_login'))
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
    all_orders      = Order.query.filter_by(is_archived=False).filter(Order.items.any()).order_by(Order.created_at.desc()).all()
    archived_orders = Order.query.filter_by(is_archived=True).filter(Order.items.any()).order_by(Order.created_at.desc()).all()
    order_ship_json = json.dumps({
        str(o.id): {'delivery': str(o.delivery_method or 'pickup'), 'address': str(o.ship_address or '')}
        for o in all_orders + archived_orders
    })
    return render_template('admin_dashboard.html',
        total_bookings=Booking.query.count(),
        total_orders=Order.query.count(),
        total_users=User.query.count(),
        total_revenue=f'{(db.session.query(func.sum(Order.total_amount)).filter(Order.status.notin_(["cancelled", "awaiting_payment"])).scalar() or 0) + (db.session.query(func.sum(Booking.total_amount)).filter(Booking.status == "completed").scalar() or 0):,.2f}',
        booking_status_counts=dict(db.session.query(Booking.status, func.count(Booking.id)).group_by(Booking.status).all()),
        order_status_counts=dict(db.session.query(Order.status, func.count(Order.id)).group_by(Order.status).all()),
        top_services=db.session.query(Booking.service, func.count(Booking.id).label('count')).group_by(Booking.service).order_by(func.count(Booking.id).desc()).limit(5).all(),
        new_users_today=User.query.filter(func.date(User.id) == date.today()).count(),
        recent_bookings=Booking.query.filter_by(is_archived=False).order_by(Booking.created_at.desc()).limit(5).all(),
        all_bookings=Booking.query.filter_by(is_archived=False).order_by(Booking.created_at.desc()).all(),
        all_orders=all_orders,
        all_products=Product.query.all(),
        all_users=User.query.all(),
        all_mechanics=Mechanic.query.order_by(Mechanic.name).all(),
        all_services=Service.query.order_by(Service.name).all(),
        archived_orders=archived_orders,
        archived_bookings=Booking.query.filter_by(is_archived=True).order_by(Booking.created_at.desc()).all(),
        order_ship_json=order_ship_json,
        now=ph_now(),
        today=ph_now().date(),
    )


@admin_app.route('/booking/<int:bid>/status', methods=['POST'])
@login_required
@require_admin_or_staff
def update_booking_status(bid):
    booking    = Booking.query.get_or_404(bid)
    new_status = validate_booking_status(clean_str(request.form.get('status', ''), max_len=20))
    booking.status = new_status
    db.session.commit()
    messages = {
        'confirmed':   ('Booking Confirmed! ✅', f'Your {booking.service} on {booking.date.strftime("%b %d, %Y")} at {booking.time.strftime("%I:%M %p")} has been confirmed. Please arrive 15 minutes before your scheduled time.'),
        'inprogress':  ('Service In Progress 🔧', f'Your {booking.service} is now in progress.'),
        'in_progress': ('Service In Progress 🔧', f'Your {booking.service} is now in progress.'),
        'completed':   ('Service Completed! 🎉',  f'Your {booking.service} has been completed. Thank you!'),
        'cancelled':   ('Booking Cancelled ❌',   f'Your {booking.service} on {booking.date.strftime("%b %d, %Y")} was cancelled.'),
    }
    if new_status in messages:
        title, msg = messages[new_status]
        send_notification(booking.user_id, title, msg, type='booking', status=new_status.replace('_', ''))
    flash('Booking status updated!', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/order/<int:oid>/status', methods=['POST'])
@login_required
@require_admin_or_staff
def update_order_status(oid):
    order = Order.query.get_or_404(oid)
    new_status = validate_order_status(clean_str(request.form.get('status', ''), max_len=20))
    if new_status == 'completed' and order.payment_method == 'cash' and order.delivery_method == 'pickup':
        flash('Cash pick-up orders can only be completed via the Quotation page.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if new_status == 'cancelled' and order.status != 'cancelled':
        for item in OrderItem.query.filter_by(order_id=order.id).all():
            product = Product.query.get(item.product_id)
            if product:
                product.stock += item.quantity
    order.status = new_status
    db.session.commit()
    messages = {
        'confirmed':  ('Order Confirmed!',        f'Your order ORD-{order.id:03d} is confirmed and being prepared.'),
        'processing': ('Order Processing',        f'Your order ORD-{order.id:03d} is being processed.'),
        'shipped':    ('Order Out for Delivery!', f'Your order ORD-{order.id:03d} is on its way!'),
        'delivered':  ('Order Delivered!',        f'Your order ORD-{order.id:03d} has been delivered.'),
        'completed':  ('Order Completed!',        f'Your order ORD-{order.id:03d} has been completed. Thank you!'),
        'cancelled':  ('Order Cancelled',         f'Your order ORD-{order.id:03d} has been cancelled.'),
    }
    if new_status in messages:
        title, msg = messages[new_status]
        send_notification(order.user_id, title, msg, type='order', status=new_status)
    flash('Order status updated!', 'success')
    return redirect(url_for('admin_dashboard'))


# Service routes

@admin_app.route('/service/add', methods=['POST'])
@login_required
@require_admin_or_staff
def add_service():
    name  = clean_str(request.form.get('name', ''), max_len=100)
    desc  = clean_str(request.form.get('description', ''), max_len=500)
    price = clean_float(request.form.get('price', 0), default=0.0, min_val=0.0)
    if not name:
        flash('Service name is required.', 'danger')
        return redirect(url_for('admin_dashboard'))
    if Service.query.filter_by(name=name).first():
        flash(f'Service "{name}" already exists.', 'warning')
        return redirect(url_for('admin_dashboard'))
    db.session.add(Service(name=name, description=desc, price=price))
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


@admin_app.route('/mechanic/add', methods=['POST'])
@login_required
@require_admin_or_staff
def add_mechanic():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    db.session.add(Mechanic(
        name=request.form['name'],
        specialization=request.form['specialization'],
        status=request.form.get('status', 'available')
    ))
    db.session.commit()
    flash('Mechanic added!', 'success')
    return redirect(url_for('admin_dashboard'))


@admin_app.route('/mechanic/<int:mid>/delete', methods=['POST'])
@login_required
@require_admin_or_staff
def delete_mechanic(mid):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    m = Mechanic.query.get_or_404(mid)
    db.session.delete(m)
    db.session.commit()
    flash(f'Mechanic "{m.name}" deleted!', 'success')
    return redirect(url_for('admin_dashboard'))


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


# POS routes

@admin_app.route('/pos')
@login_required
@require_admin_or_staff
def pos():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    products  = Product.query.filter(Product.stock > 0).order_by(Product.category, Product.name).all()
    customers = User.query.filter_by(role='customer').order_by(User.fullname).all()
    services  = Service.query.filter_by(is_active=True).order_by(Service.name).all()
    return render_template('pos.html', products=products, customers=customers, services=services)


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
                source_order.payment_method = payment_method
                completed_order_ids.add(source_order_id)
                if source_order.user_id:
                    send_notification(source_order.user_id, 'Order Completed!',
                        f'Your order ORD-{source_order.id:03d} has been completed and paid.',
                        type='order', status='completed')

    if new_cart_items:
        uid = int(customer_id) if customer_id else current_user.id
        new_total = sum(item['quantity'] * float(item['unit_price']) for item in new_cart_items)
        order = Order(user_id=uid, total_amount=new_total, status='completed', payment_method=payment_method)
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
                total_amount=svc_price * svc_qty
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
        svc = Service.query.filter_by(name=b.service, is_active=True).first()
        bookings_data.append({
            'booking_id': b.id, 'service': b.service,
            'price': svc.price if svc else 0,
            'date': b.date.strftime('%Y-%m-%d'), 'time': b.time.strftime('%H:%M'),
            'status': b.status, 'created_at': b.created_at.isoformat()
        })
    return jsonify({'customer_id': cid, 'customer_name': customer.fullname,
                    'orders': orders_data, 'bookings': bookings_data})


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

    orders = Order.query.filter(
        func.date(Order.created_at) >= d_from, func.date(Order.created_at) <= d_to
    ).order_by(Order.created_at.desc()).all()
    bookings = Booking.query.filter(
        Booking.date >= d_from, Booking.date <= d_to
    ).order_by(Booking.date.desc()).all()

    total_revenue      = sum(o.total_amount for o in orders if o.status not in ['cancelled', 'awaiting_payment'])
    total_orders       = len(orders)
    total_bookings     = len(bookings)
    completed_orders   = sum(1 for o in orders if o.status == 'completed')

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    story = []
    W = A4[0] - 80
    RED   = colors.HexColor('#e8401c')
    DARK  = colors.HexColor('#1a1d23')
    LIGHT = colors.HexColor('#f3f4f6')
    MUTED = colors.HexColor('#6b7280')
    styles = getSampleStyleSheet()

    def style(name, **kwargs): return ParagraphStyle(name, **kwargs)
    title_style   = style('T', fontSize=24, fontName='Helvetica-Bold', textColor=RED, spaceAfter=2)
    sub_style     = style('S', fontSize=10, fontName='Helvetica', textColor=MUTED, spaceAfter=4)
    heading_style = style('H', fontSize=12, fontName='Helvetica-Bold', textColor=DARK, spaceBefore=14, spaceAfter=6)
    small_style   = style('SM', fontSize=8, fontName='Helvetica', textColor=MUTED)

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
        ('ROWHEIGHT', (0,0), (-1,-1), 28), ('GRID', (0,0), (-1,-1), 0.5, colors.white),
    ]))
    story.append(st)
    story.append(Spacer(1, 16))
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