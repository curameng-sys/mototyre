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
from security import clean_str, clean_int, clean_float, is_valid_email, is_valid_phone, validate_otp_purpose
import os, uuid, random, string, base64, requests

app = Flask(__name__)
app.config.update(
    SECRET_KEY='mototyre-fixed-secret-key-xK9mP2qL7rZ3wN8vB4',
    SESSION_COOKIE_NAME='mototyre_customer_session',
    SQLALCHEMY_DATABASE_URI=(
        "mysql+pymysql://avnadmin:AVNS_1RNbCP5RORVJ7VaVhRD@"
        "mysql-1d9dceb5-mackycastanales05-1369.f.aivencloud.com:12422/defaultdb"
        "?ssl_ca=ca.pem"
    ),
    SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"ssl_ca": "CA.pem"}},
    SQLALCHEMY_TRACK_MODIFICATIONS=False
)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

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
                "line_items": [
                    {
                        "name": description,
                        "quantity": 1,
                        "amount": int(amount * 100),
                        "currency": "PHP"
                    }
                ],
                "payment_method_types": ["gcash"],
                "success_url": f"{BASE_URL}/payment/success?order_id={order_id or ''}&booking_id={booking_id or ''}",
                "cancel_url": f"{BASE_URL}/payment/failed?order_id={order_id or ''}&booking_id={booking_id or ''}"
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


def send_otp_email(email, otp, purpose="verify"):
    configs = {
        "verify": ("Verify your MotoTyre email",        "Email Verification", "verify your email address"),
        "reset":  ("Your MotoTyre password reset code", "Password Reset",     "reset your password"),
        "login":  ("Your MotoTyre login code",          "Login Verification", "complete your login"),
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


def send_order_receipt_email(order):
    try:
        if order.receipt_sent:
            return  # already sent, skip
        user = User.query.get(order.user_id)
        if not user or not user.email:
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
          <!-- Header -->
          <div style="background:#0c0d0f;padding:28px 32px;text-align:center;">
            <div style="font-family:Georgia,serif;font-size:26px;font-weight:900;letter-spacing:4px;color:#ffffff;">MOTO<span style="color:#ff0f0f;">TYRE</span></div>
            <div style="color:#6b7280;font-size:12px;letter-spacing:2px;text-transform:uppercase;margin-top:4px;">Order Receipt</div>
          </div>

          <!-- Success banner -->
          <div style="background:#f0fdf4;border-bottom:1px solid #bbf7d0;padding:16px 32px;display:flex;align-items:center;gap:12px;">
            <div style="width:36px;height:36px;background:#16a34a;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
              <span style="color:#fff;font-size:18px;line-height:1;">&#10003;</span>
            </div>
            <div>
              <div style="font-weight:700;color:#15803d;font-size:15px;">Payment Confirmed!</div>
              <div style="color:#4b5563;font-size:13px;margin-top:2px;">Your GCash payment was received successfully.</div>
            </div>
          </div>

          <!-- Order info -->
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

          <!-- Items table -->
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

          <!-- Total -->
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

          <!-- Footer -->
          <div style="background:#f9fafb;border-top:1px solid #f3f4f6;padding:20px 32px;text-align:center;">
            <div style="color:#6b7280;font-size:12px;line-height:1.7;">
              <strong style="color:#111827;">MotoTyre North Caloocan</strong><br>
              Saranay Rd, Brgy. 171 Bagumbong, Caloocan City<br>
              &#128222; 0915 269 8366 &nbsp;|&nbsp; Mon–Sat 8:00 AM – 7:00 PM
            </div>
            <div style="margin-top:12px;color:#9ca3af;font-size:11px;">Thank you for choosing MotoTyre! &#127947;</div>
          </div>
        </div>"""

        _send_gmail(user.email, f"Your MotoTyre Receipt — {order_num}", html)
        order.receipt_sent = True
        db.session.commit()
        print(f"[receipt email] sent to {user.email} for {order_num}")
    except Exception as e:
        print(f"[receipt email] failed for order {order.id}: {e}")
        db.session.rollback()


# Helpers

def ph_now():
    return datetime.utcnow() + timedelta(hours=8)


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
    created_at = db.Column(db.DateTime, default=ph_now)


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
    created_at = db.Column(db.DateTime, default=ph_now)
    reminder_sent    = db.Column(db.Boolean, default=False)
    mechanic_name           = db.Column(db.String(100))
    mechanic_specialization = db.Column(db.String(100))
    contact_name  = db.Column(db.String(100))
    contact_mobile = db.Column(db.String(20))
    odometer = db.Column(db.Integer)
    is_archived = db.Column(db.Boolean, default=False)

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
    created_at = db.Column(db.DateTime, default=ph_now)
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
    """Cancel GCash orders that haven't been paid within 30 minutes."""
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

@app.route('/')
def home():
    return render_template('landing.html')

@app.route('/products')
def products():
    return render_template('Products.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        # Admins/staff should not be using this portal — redirect them out
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

        # Block admin/staff from logging in through the user portal
        if user and user.role in ['admin', 'staff']:
            flash('Admin and staff accounts must use the Admin Portal.', 'danger')
            return redirect(url_for('login'))

        if user and user.check_password(password):
            if not user.email_verified:
                flash('Please verify your email before logging in.', 'warning')
                return redirect(url_for('login'))
            otp = _save_otp(email, purpose="login")
            try:
                send_otp_email(email, otp, purpose="login")
            except Exception as e:
                flash(f'Could not send OTP: {e}', 'danger')
                return redirect(url_for('login'))
            session['pending_login_email'] = email
            return redirect(url_for('verify_login_otp'))

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
    redirect_map = {'login': 'verify_login_otp', 'reset': 'forgot_password_verify', 'verify': 'verify_email_otp'}
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
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
            return redirect(url_for('register'))
        if not is_valid_phone(phone):
            flash('Invalid phone number.', 'danger')
            return redirect(url_for('register'))

        firstname = clean_str(request.form.get('firstname', ''), max_len=50)
        lastname  = clean_str(request.form.get('lastname', ''), max_len=50)
        suffix    = clean_str(request.form.get('suffix', ''), max_len=10)
        fullname  = f"{firstname} {lastname}" + (f" {suffix}" if suffix else "")

        user = User(
            fullname=fullname, email=email, phone=phone,
            motorcycle_plate=request.form.get('plate'),
            motorcycle_model=request.form.get('model'),
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
        flash('Account created! Check your email for a verification code.', 'success')
        return redirect(url_for('verify_email_otp'))

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


# Forgot password routes

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


# Customer routes

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

    bookings   = Booking.query.filter_by(user_id=current_user.id).order_by(Booking.created_at.desc()).all()
    orders     = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    products   = Product.query.filter(Product.stock > 0).all()
    services   = Service.query.filter_by(is_active=True).order_by(Service.name).all()
    return render_template('customer_dashboard.html', bookings=bookings, orders=orders,
                           products=products, services=services)


@app.route('/customer/book', methods=['POST'])
@login_required
def book_service():
    service = clean_str(request.form.get('service', ''), max_len=100)
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

    odo_raw = clean_str(request.form.get('odometer', ''), max_len=10)
    booking = Booking(
    user_id=current_user.id,
    service=service,
    date=booking_date,
    time=booking_time,
    motorcycle_model=clean_str(request.form.get('motorcycle_model', current_user.motorcycle_model or ''), max_len=100),
    motorcycle_plate=clean_str(request.form.get('motorcycle_plate', current_user.motorcycle_plate or ''), max_len=20),
    notes=clean_str(request.form.get('notes', ''), max_len=500),
    contact_name=clean_str(request.form.get('contact_name', ''), max_len=100),
    contact_mobile=clean_str(request.form.get('contact_mobile', ''), max_len=13),
    odometer=int(odo_raw) if odo_raw.isdigit() else None,
    )
    mechanic_id = request.form.get('mechanic_id', '')
    if mechanic_id:
        mechanic = Mechanic.query.get(int(mechanic_id))
        if mechanic:
            booking.mechanic_name = mechanic.name
            booking.mechanic_specialization = mechanic.specialization
    db.session.add(booking)
    db.session.commit()
    
    send_notification(
        current_user.id, 'Booking Received!',
        f'Your {service} appointment on {booking.date.strftime("%b %d, %Y")} at {booking.time.strftime("%I:%M %p")} is pending confirmation.',
        type='booking', status='pending'
    )
    
    admins = User.query.filter_by(role='admin').all()
    for admin in admins:
        send_notification(
            admin.id,
            'New Booking Received',
            f'{current_user.fullname} booked {service} on {booking.date.strftime("%b %d, %Y")} at {booking.time.strftime("%I:%M %p")}.',
            type='booking', status='pending'
        )
    
    flash('Appointment booked successfully!', 'success')
    return redirect(url_for('customer_dashboard'))


@app.route('/customer/cart/checkout', methods=['POST'])
@login_required
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
            return jsonify({'success': False, 'error': f'Product not found'}), 400
        qty = int(item['quantity'])
        if product.stock < qty:
            return jsonify({'success': False, 'error': f'Not enough stock for {product.name}'}), 400
        resolved.append((product, qty))

    total = sum(p.price * q for p, q in resolved)
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
        desc = f"MotoTyre Order #{order.id:03d}: " + ', '.join(f"{p.name} x{q}" for p, q in resolved)
        result = create_gcash_payment(amount=total, description=desc[:100], order_id=order.id)
        if result['success']:
            return jsonify({'success': True, 'gcash': True, 'checkout_url': result['checkout_url']})
        return jsonify({'success': False, 'error': 'Could not create GCash payment'}), 500

    items_desc = ', '.join(f"{p.name} x{q}" for p, q in resolved)
    send_notification(current_user.id, 'Order Placed!',
        f'Your order for {items_desc[:80]} worth ₱{total:.2f} is now pending.',
        type='order', status='pending')
    for admin in User.query.filter_by(role='admin').all():
        send_notification(admin.id, 'New Order Received',
            f'{current_user.fullname} placed an order for ₱{total:.2f}.', type='order', status='pending')

    return jsonify({'success': True, 'gcash': False})


@app.route('/customer/order', methods=['POST'])
@login_required
def place_order():
    product  = Product.query.get_or_404(clean_int(request.form.get('product_id', 0), default=0))
    quantity = clean_int(request.form.get('quantity', 1), default=1, min_val=1, max_val=9999)
    if product.stock < quantity:
        flash('Not enough stock available.', 'danger')
        return redirect(url_for('customer_dashboard'))

    total = product.price * quantity
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
    elif delivery_method == 'pickup':
        order_status = 'awaiting_payment'
    else:
        order_status = 'pending'
    
    order = Order(
        user_id=current_user.id,
        total_amount=total,
        payment_method=payment_method,
        delivery_method=delivery_method,
        ship_address=ship_address,
        status=order_status
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=quantity, unit_price=product.price))
    product.stock -= quantity
    db.session.commit()
    
    if payment_method.lower() != 'gcash':
        delivery_text = "for pickup" if delivery_method == 'pickup' else "for delivery"
        send_notification(
            current_user.id, 'Order Placed!',
            f'Your order for {product.name} (x{quantity}) worth ₱{total:.2f} is now pending {delivery_text}.',
            type='order', status='pending'
        )
    
    if payment_method.lower() == 'gcash':
        description = f"MotoTyre Order #{order.id:03d}: {product.name} x{quantity}"
        result = create_gcash_payment(
            amount=total,
            description=description,
            order_id=order.id
        )
        
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
        current_user.motorcycle_model = clean_str(request.form.get('motorcycle_model', ''), max_len=100)
        current_user.motorcycle_plate = clean_str(request.form.get('motorcycle_plate', ''), max_len=20)
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

@app.route('/api/mechanics')
@login_required
def get_mechanics():
    mechanics = Mechanic.query.filter_by(status='available').order_by(Mechanic.name).all()
    return jsonify([{
        'id': m.id,
        'name': m.name,
        'specialization': m.specialization
    } for m in mechanics])


# Notification routes

@app.route('/api/notifications')
@login_required
def get_notifications():
    notifs = Notification.query.filter_by(user_id=current_user.id)\
                               .order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify([{
        'id': n.id, 'title': n.title, 'message': n.message,
        'type': n.type, 'status': n.status,
        'is_read': n.is_read,
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


# Order confirm received

@app.route('/customer/order/<int:oid>/confirm-received', methods=['POST'])
@login_required
def confirm_order_received(oid):
    order = Order.query.get_or_404(oid)
    if order.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Unauthorized.'}), 403
    if order.status != 'shipped':
        return jsonify({'success': False, 'message': 'Order is not in shipped status.'}), 400
    new_status = 'completed'
    order.status = new_status
    db.session.commit()
    send_notification(
        current_user.id, 'Order Received!',
        f'You confirmed receipt of your order ORD-{order.id:03d}. Thank you!',
        type='order', status=new_status
    )
    for admin in User.query.filter_by(role='admin').all():
        send_notification(
            admin.id, 'Order Received ✅',
            f'{current_user.fullname} confirmed receipt of ORD-{order.id:03d}.',
            type='order', status=new_status
        )
    return jsonify({'success': True, 'new_status': new_status})


# Payment routes

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
    items_desc = ", ".join([f"{item.product.name} x{item.quantity}" for item in order.items])
    description = f"MotoTyre Order #{order.id:03d}: {items_desc[:100]}"
    result = create_gcash_payment(amount=order.total_amount, description=description, order_id=order.id)
    if result["success"]:
        order.payment_method = 'gcash'
        db.session.commit()
        return redirect(result["checkout_url"])
    else:
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
    result = create_gcash_payment(amount=amount, description=description, booking_id=booking.id)
    if result["success"]:
        booking.payment_method = 'gcash'
        db.session.commit()
        return redirect(result["checkout_url"])
    else:
        flash('Could not create payment. Please try again.', 'danger')
        return redirect(url_for('customer_dashboard'))


@app.route('/payment/success')
def payment_success():
    order_id = request.args.get('order_id')
    booking_id = request.args.get('booking_id')
    if order_id:
        try:
            order = Order.query.get(int(order_id))
            if order and order.status == 'awaiting_payment':
                order.status = 'confirmed'
                db.session.commit()
                items_desc = ", ".join([f"{item.product.name} x{item.quantity}" for item in order.items])
                send_notification(
                    order.user_id, 'Order Confirmed! ✅',
                    f'Your payment for Order ORD-{order.id:03d} ({items_desc}) worth ₱{order.total_amount:.2f} has been confirmed.',
                    type='order', status='confirmed'
                )
            # send receipt regardless — receipt_sent flag prevents duplicates
            if order and order.status == 'confirmed':
                send_order_receipt_email(order)
        except Exception as e:
            print(f"[payment/success] order error: {e}")
    if booking_id:
        try:
            booking = Booking.query.get(int(booking_id))
            if booking and booking.status == 'pending':
                booking.status = 'confirmed'
                db.session.commit()
        except:
            pass
    return redirect('http://127.0.0.1:5000/customer/dashboard')


@app.route('/payment/failed')
def payment_failed():
    order_id = request.args.get('order_id')
    booking_id = request.args.get('booking_id')
    if order_id:
        try:
            order = Order.query.get(int(order_id))
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
    return redirect('http://127.0.0.1:5000/customer/dashboard')


@app.route('/webhook/paymongo', methods=['POST'])
def paymongo_webhook():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data'}), 400
    event_type = data.get('data', {}).get('attributes', {}).get('type')
    resource = data.get('data', {}).get('attributes', {}).get('data', {})
    if event_type == 'link.payment.paid':
        metadata = resource.get('attributes', {}).get('metadata', {})
        order_id = metadata.get('order_id')
        if order_id:
            order = Order.query.get(int(order_id))
            if order and order.status in ['pending', 'awaiting_payment']:
                order.status = 'confirmed'
                order.payment_method = 'gcash'
                db.session.commit()
                send_notification(order.user_id, 'Payment Received!',
                    f'Your payment for Order #{order.id:03d} has been confirmed.', type='order', status='confirmed')
                send_order_receipt_email(order)
        booking_id = metadata.get('booking_id')
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


@app.route('/api/services')
def api_services():
    try:
        svcs = Service.query.filter_by(is_active=True).order_by(Service.name).all()
        return jsonify([{'name': s.name, 'price': s.price} for s in svcs])
    except Exception as e:
        return jsonify([])


from apscheduler.schedulers.background import BackgroundScheduler
import atexit

scheduler = BackgroundScheduler(timezone='Asia/Manila')
scheduler.add_job(
    func=check_upcoming_bookings,
    trigger='interval',
    minutes=1,
    id='booking_reminder_job',
    replace_existing=True
)
scheduler.start()
print('[SCHEDULER] Booking reminder service started — checking every 1 minute')
atexit.register(lambda: scheduler.shutdown())

with app.app_context():
    try:
        from sqlalchemy import text as _text
        db.session.execute(_text("ALTER TABLE `order` ADD COLUMN receipt_sent TINYINT(1) NOT NULL DEFAULT 0"))
        db.session.commit()
        print('[MIGRATION] Added receipt_sent column to order table')
    except Exception:
        db.session.rollback()

with app.app_context():
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

with app.app_context():
    try:
        db.create_all()
        _default_services = [
            ('Ball Race Installation',      'Steering ball race replacement',         350),
            ('Brake Cleaning',              'Brake system cleaning',                  200),
            ('Change Brake Pad',            'Brake pad replacement',                  300),
            ('Change Oil',                  'Engine oil replacement',                 350),
            ('CVT Cleaning',                'CVT belt & pulley cleaning',             500),
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
    # USER PORTAL — runs on port 5000
    app.run(debug=True, port=5000, use_reloader=False)