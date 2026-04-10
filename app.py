from flask import Flask, render_template, redirect, url_for, flash, request, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from flask import jsonify
from sqlalchemy import func
from datetime import date as date_today
from flask import jsonify, request
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import uuid
import random
import string
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Gmail API
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

app = Flask(__name__)
app.config['SECRET_KEY'] = 'mototyre-fixed-secret-key-xK9mP2qL7rZ3wN8vB4'
app.config['SQLALCHEMY_DATABASE_URI'] = (
    "mysql+pymysql://avnadmin:AVNS_1RNbCP5RORVJ7VaVhRD@"
    "mysql-1d9dceb5-mackycastanales05-1369.f.aivencloud.com:12422/defaultdb"
    "?ssl_ca=ca.pem"
)
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "connect_args": {
        "ssl_ca": "CA.pem"
    }
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


# ─────────────────────────────────────────
# GMAIL OTP CONFIG
# ─────────────────────────────────────────

GMAIL_SCOPES     = ["https://www.googleapis.com/auth/gmail.send"]
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_CREDS_FILE = "credentials.json"
GMAIL_SENDER     = os.getenv("GMAIL_SENDER", "mackycastanales05@gmail.com")
OTP_EXPIRY_MINS  = 10


def _get_gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _send_gmail(to: str, subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["to"]      = to
    msg["from"]    = GMAIL_SENDER
    msg["subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = _get_gmail_service()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_otp_email(email: str, otp: str, purpose: str = "verify"):
    if purpose == "verify":
        subject = "Verify your MotoTyre email"
        heading = "Email Verification"
        subtext = "Use the code below to verify your email address."
    elif purpose == "reset":
        subject = "Your MotoTyre password reset code"
        heading = "Password Reset"
        subtext = "Use the code below to reset your password."
    else:
        subject = "Your MotoTyre login code"
        heading = "Login Verification"
        subtext = "Use the code below to complete your login."

    html = """
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:32px;
                border:1px solid #e5e7eb;border-radius:8px;">
      <h2 style="color:#111827;margin-bottom:8px;">{heading}</h2>
      <p style="color:#6b7280;">{subtext} It expires in {OTP_EXPIRY_MINS} minutes.</p>
      <div style="font-size:36px;font-weight:bold;letter-spacing:12px;color:#111827;
                  background:#f3f4f6;padding:20px;border-radius:6px;
                  text-align:center;margin:24px 0;">{otp}</div>
      <p style="color:#9ca3af;font-size:13px;">
        If you didn't request this, you can safely ignore this email.
      </p>
    </div>
    """
    _send_gmail(email, subject, html)


# ─────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────

class User(db.Model, UserMixin):
    id                = db.Column(db.Integer, primary_key=True)
    fullname          = db.Column(db.String(100), nullable=False)
    email             = db.Column(db.String(100), unique=True, nullable=False)
    phone             = db.Column(db.String(20), nullable=False)
    password_hash     = db.Column(db.String(255), nullable=False)
    role              = db.Column(db.String(20), default='customer')
    motorcycle_plate  = db.Column(db.String(20), nullable=True)
    motorcycle_model  = db.Column(db.String(100), nullable=True)
    profile_pic       = db.Column(db.String(255), nullable=True)
    email_verified    = db.Column(db.Boolean, default=False)
    bookings          = db.relationship('Booking', backref='customer', lazy=True)
    orders            = db.relationship('Order', backref='customer', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class OTPRecord(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    email      = db.Column(db.String(100), nullable=False, index=True)
    otp        = db.Column(db.String(10), nullable=False)
    purpose    = db.Column(db.String(10), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used       = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Booking(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    service          = db.Column(db.String(100), nullable=False)
    date             = db.Column(db.Date, nullable=False)
    time             = db.Column(db.Time, nullable=False)
    motorcycle_model = db.Column(db.String(100), nullable=True)
    motorcycle_plate = db.Column(db.String(20), nullable=True)
    notes            = db.Column(db.Text, nullable=True)
    status           = db.Column(db.String(20), default='pending')
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)


class Product(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    barcode     = db.Column(db.String(100), nullable=True)
    name        = db.Column(db.String(150), nullable=False)
    category    = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    price       = db.Column(db.Float, nullable=False)
    stock       = db.Column(db.Integer, default=0)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    order_items = db.relationship('OrderItem', backref='product', lazy=True)


class Order(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    status       = db.Column(db.String(20), default='pending')
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    items        = db.relationship('OrderItem', backref='order', lazy=True)


class OrderItem(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    order_id   = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity   = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)


# ── NEW: Notification Model ──
class Notification(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title      = db.Column(db.String(120), nullable=False)
    message    = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(30), default='update')
    # type options: booking, order, mechanic, cost, update, reminder
    status     = db.Column(db.String(30), nullable=True)
    # status options: pending, confirmed, inprogress, completed, cancelled, delivered
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ── HELPER: Send Notification ──
def send_notification(user_id, title, message, type='update', status=None):
    notif = Notification(
        user_id=user_id,
        title=title,
        message=message,
        type=type,
        status=status
    )
    db.session.add(notif)
    db.session.commit()


# ─────────────────────────────────────────
# OTP HELPERS
# ─────────────────────────────────────────

def _generate_otp(length=6) -> str:
    return "".join(random.choices(string.digits, k=length))


def _save_otp(email: str, purpose: str) -> str:
    OTPRecord.query.filter_by(email=email, purpose=purpose, used=False).update({"used": True})
    db.session.flush()
    otp = _generate_otp()
    record = OTPRecord(
        email=email,
        otp=otp,
        purpose=purpose,
        expires_at=datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINS)
    )
    db.session.add(record)
    db.session.commit()
    return otp


def _verify_otp(email: str, otp_input: str, purpose: str) -> dict:
    record = OTPRecord.query.filter_by(
        email=email, purpose=purpose, used=False
    ).order_by(OTPRecord.created_at.desc()).first()

    if not record:
        return {"valid": False, "message": "No OTP found. Please request a new one."}
    if datetime.utcnow() > record.expires_at:
        record.used = True
        db.session.commit()
        return {"valid": False, "message": "OTP has expired. Please request a new one."}
    if record.otp != otp_input.strip():
        return {"valid": False, "message": "Invalid OTP. Please try again."}

    record.used = True
    db.session.commit()
    return {"valid": True, "message": "OTP verified."}


# ─────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────

@app.route('/')
def home():
    return render_template('landing.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        if current_user.role == 'admin':
            return redirect(url_for('admin_dashboard'))
        elif current_user.role == 'staff':
            return redirect(url_for('staff_dashboard'))
        else:
            return redirect(url_for('customer_dashboard'))

    if request.method == 'POST':
        email    = request.form['email']
        password = request.form['password']
        user     = User.query.filter_by(email=email).first()

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
            flash('A login code has been sent to your email.', 'info')
            return redirect(url_for('verify_login_otp'))

        flash('Invalid email or password.', 'danger')
    return render_template('login.html')


@app.route('/verify-login-otp', methods=['GET', 'POST'])
def verify_login_otp():
    email = session.get('pending_login_email')
    if not email:
        return redirect(url_for('login'))

    if request.method == 'POST':
        otp_input = request.form.get('otp', '').strip()
        result    = _verify_otp(email, otp_input, purpose="login")

        if result['valid']:
            session.pop('pending_login_email', None)
            user = User.query.filter_by(email=email).first()
            login_user(user, remember=True)

            next_page = request.args.get('next') or session.pop('next', None)
            if next_page and next_page.startswith('/'):
                return redirect(next_page)

            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            elif user.role == 'staff':
                return redirect(url_for('staff_dashboard'))
            else:
                return redirect(url_for('customer_dashboard'))

        flash(result['message'], 'danger')

    return render_template('verify_otp.html', purpose='login', email=email)


@app.route('/resend-otp/<purpose>')
def resend_otp(purpose):
    if purpose == 'login':
        email = session.get('pending_login_email')
    elif purpose == 'verify':
        email = session.get('pending_verify_email')
    elif purpose == 'reset':
        email = session.get('pending_reset_email')
    else:
        email = None

    if not email:
        flash('Session expired. Please start again.', 'danger')
        return redirect(url_for('login'))

    otp = _save_otp(email, purpose=purpose)
    try:
        send_otp_email(email, otp, purpose=purpose)
        flash('A new OTP has been sent to your email.', 'info')
    except Exception as e:
        flash(f'Could not resend OTP: {e}', 'danger')

    if purpose == 'login':
        return redirect(url_for('verify_login_otp'))
    elif purpose == 'reset':
        return redirect(url_for('forgot_password_verify'))
    return redirect(url_for('verify_email_otp'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        fullname = request.form['fullname']
        email    = request.form['email']
        phone    = request.form['phone']
        password = request.form['password']
        plate    = request.form.get('plate')
        model    = request.form.get('model')

        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
            return redirect(url_for('register'))

        if not phone.isdigit():
            return redirect(url_for('register'))

        if len(phone) < 10 or len(phone) > 13:
            return redirect(url_for('register'))

        new_user = User(
            fullname=fullname,
            email=email,
            phone=phone,
            motorcycle_plate=plate,
            motorcycle_model=model,
            email_verified=False
        )
        new_user.set_password(password)
        db.session.add(new_user)
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
        otp_input = request.form.get('otp', '').strip()
        result    = _verify_otp(email, otp_input, purpose="verify")

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


# ─────────────────────────────────────────
# FORGOT PASSWORD ROUTES
# ─────────────────────────────────────────

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user  = User.query.filter_by(email=email).first()

        if user:
            otp = _save_otp(email, purpose="reset")
            try:
                send_otp_email(email, otp, purpose="reset")
            except Exception as e:
                flash(f'Could not send reset code: {e}', 'danger')
                return redirect(url_for('forgot_password'))
            session['pending_reset_email'] = email

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
        otp_input = request.form.get('otp', '').strip()
        result    = _verify_otp(email, otp_input, purpose="reset")

        if result['valid']:
            session['reset_otp_verified'] = True
            return redirect(url_for('forgot_password_reset'))

        flash(result['message'], 'danger')

    return render_template('verify_otp.html', purpose='reset', email=email)


@app.route('/forgot-password/reset', methods=['GET', 'POST'])
def forgot_password_reset():
    email    = session.get('pending_reset_email')
    verified = session.get('reset_otp_verified')

    if not email or not verified:
        flash('Session expired. Please start again.', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        new_password     = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if len(new_password) < 8:
            flash('Password must be at least 8 characters.', 'danger')
            return redirect(url_for('forgot_password_reset'))

        if new_password != confirm_password:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('forgot_password_reset'))

        user = User.query.filter_by(email=email).first()
        if user:
            user.set_password(new_password)
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


# ─────────────────────────────────────────
# CUSTOMER ROUTES
# ─────────────────────────────────────────

@app.route('/customer/dashboard')
@login_required
def customer_dashboard():
    bookings   = Booking.query.filter_by(user_id=current_user.id).order_by(Booking.created_at.desc()).all()
    orders     = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    products   = Product.query.filter(Product.stock > 0).all()
    categories = db.session.query(Product.category).distinct().order_by(Product.category).all()
    categories = [c[0] for c in categories]
    return render_template('customer_dashboard.html',
        bookings=bookings, orders=orders,
        products=products, categories=categories)


@app.route('/customer/book', methods=['POST'])
@login_required
def book_service():
    service          = request.form['service']
    date_str         = request.form['date']
    time_str         = request.form['time']
    notes            = request.form.get('notes', '')
    motorcycle_model = request.form.get('motorcycle_model', current_user.motorcycle_model)
    motorcycle_plate = request.form.get('motorcycle_plate', current_user.motorcycle_plate)

    booking = Booking(
        user_id=current_user.id,
        service=service,
        date=datetime.strptime(date_str, '%Y-%m-%d').date(),
        time=datetime.strptime(time_str, '%H:%M').time(),
        motorcycle_model=motorcycle_model,
        motorcycle_plate=motorcycle_plate,
        notes=notes
    )
    db.session.add(booking)
    db.session.commit()

    # ── Notify: Booking created ──
    send_notification(
        user_id=current_user.id,
        title='Booking Received!',
        message=f'Your {service} appointment on {booking.date.strftime("%b %d, %Y")} at {booking.time.strftime("%I:%M %p")} has been received and is pending confirmation.',
        type='booking',
        status='pending'
    )

    flash('Appointment booked successfully!', 'success')
    return redirect(url_for('customer_dashboard'))


@app.route('/customer/order', methods=['POST'])
@login_required
def place_order():
    product_id = request.form['product_id']
    quantity   = int(request.form['quantity'])
    product    = Product.query.get_or_404(product_id)

    if product.stock < quantity:
        flash('Not enough stock available.', 'danger')
        return redirect(url_for('customer_dashboard'))

    total = product.price * quantity
    order = Order(user_id=current_user.id, total_amount=total)
    db.session.add(order)
    db.session.flush()

    item = OrderItem(order_id=order.id, product_id=product.id, quantity=quantity, unit_price=product.price)
    product.stock -= quantity
    db.session.add(item)
    db.session.commit()

    # ── Notify: Order placed ──
    send_notification(
        user_id=current_user.id,
        title='Order Placed!',
        message=f'Your order for {product.name} (x{quantity}) worth ₱{total:.2f} has been placed and is now pending.',
        type='order',
        status='pending'
    )

    flash('Order placed successfully!', 'success')
    return redirect(url_for('customer_dashboard'))


@app.route('/customer/profile', methods=['GET', 'POST'])
@login_required
def customer_profile():
    if request.method == 'POST':
        current_user.fullname         = request.form['fullname']
        current_user.phone            = request.form['phone']
        current_user.motorcycle_model = request.form.get('motorcycle_model')
        current_user.motorcycle_plate = request.form.get('motorcycle_plate')
        db.session.commit()
        flash('Profile updated successfully!', 'success')
    return redirect(url_for('customer_dashboard'))


ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/profile/upload-pic', methods=['POST'])
@login_required
def upload_profile_pic():
    if 'profile_pic' not in request.files:
        flash('No file selected.', 'danger')
    else:
        file = request.files['profile_pic']
        if file.filename == '':
            flash('No file selected.', 'danger')
        elif file and allowed_file(file.filename):
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

    if current_user.role == 'admin':
        return redirect(url_for('admin_dashboard'))
    elif current_user.role == 'staff':
        return redirect(url_for('staff_dashboard'))
    else:
        return redirect(url_for('customer_dashboard'))


@app.route('/admin/update-profile', methods=['POST'])
@login_required
def update_admin_profile():
    current_user.fullname = request.form['fullname']
    current_user.phone    = request.form['phone']
    db.session.commit()
    flash('Profile updated!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/staff/update-profile', methods=['POST'])
@login_required
def update_staff_profile():
    current_user.fullname = request.form['fullname']
    current_user.phone    = request.form['phone']
    db.session.commit()
    flash('Profile updated!', 'success')
    return redirect(url_for('staff_dashboard'))


@app.route('/api/booked-slots')
@login_required
def get_booked_slots():
    year  = request.args.get('year',  type=int)
    month = request.args.get('month', type=int)
    if not year or not month:
        return jsonify({})

    from datetime import date
    start = date(year, month, 1)
    end   = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

    bookings = Booking.query.filter(
        Booking.date >= start,
        Booking.date < end,
        Booking.status != 'cancelled'
    ).all()

    result = {}
    for b in bookings:
        date_str = b.date.strftime('%Y-%m-%d')
        time_str = b.time.strftime('%H:%M')
        result.setdefault(date_str, []).append(time_str)

    return jsonify(result)


# ─────────────────────────────────────────
# NOTIFICATION ROUTES
# ─────────────────────────────────────────

@app.route('/api/notifications')
@login_required
def get_notifications():
    notifs = Notification.query.filter_by(user_id=current_user.id)\
             .order_by(Notification.created_at.desc()).limit(30).all()
    return jsonify([{
        'id': n.id,
        'title': n.title,
        'message': n.message,
        'type': n.type,
        'status': n.status,
        'is_read': n.is_read,
        'created_at': n.created_at.isoformat()
    } for n in notifs])


@app.route('/api/notifications/<int:notif_id>/read', methods=['POST'])
@login_required
def read_notification(notif_id):
    n = Notification.query.filter_by(id=notif_id, user_id=current_user.id).first_or_404()
    n.is_read = True
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/notifications/read-all', methods=['POST'])
@login_required
def read_all_notifications():
    Notification.query.filter_by(user_id=current_user.id, is_read=False)\
        .update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────
# STAFF ROUTES
# ─────────────────────────────────────────

@app.route('/staff/dashboard')
@login_required
def staff_dashboard():
    if current_user.role != 'staff':
        return redirect(url_for('customer_dashboard'))
    all_bookings        = Booking.query.order_by(Booking.created_at.desc()).all()
    all_orders          = Order.query.order_by(Order.created_at.desc()).all()
    all_customers       = User.query.filter_by(role='customer').all()
    all_products        = Product.query.all()
    recent_bookings     = Booking.query.order_by(Booking.created_at.desc()).limit(5).all()
    booking_status_counts = dict(db.session.query(Booking.status, func.count(Booking.id)).group_by(Booking.status).all())
    order_status_counts   = dict(db.session.query(Order.status,   func.count(Order.id)  ).group_by(Order.status).all())
    return render_template('staff_dashboard.html',
        all_bookings=all_bookings, all_orders=all_orders,
        all_customers=all_customers, all_products=all_products,
        recent_bookings=recent_bookings, today=date_today.today(),
        booking_status_counts=booking_status_counts,
        order_status_counts=order_status_counts)


# ─────────────────────────────────────────
# ADMIN ROUTES
# ─────────────────────────────────────────

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if current_user.role not in ['admin', 'staff']:
        flash('Access denied.', 'danger')
        return redirect(url_for('customer_dashboard'))

    total_bookings = Booking.query.count()
    total_orders   = Order.query.count()
    total_users    = User.query.count()
    total_revenue  = db.session.query(func.sum(Order.total_amount)).scalar() or 0

    booking_status_counts = dict(
        db.session.query(Booking.status, func.count(Booking.id)).group_by(Booking.status).all()
    )
    order_status_counts = dict(
        db.session.query(Order.status, func.count(Order.id)).group_by(Order.status).all()
    )
    top_services = (
        db.session.query(Booking.service, func.count(Booking.id).label('count'))
        .group_by(Booking.service).order_by(func.count(Booking.id).desc()).limit(5).all()
    )

    today           = date_today.today()
    new_users_today = User.query.filter(func.date(User.id) == today).count()
    recent_bookings = Booking.query.order_by(Booking.created_at.desc()).limit(5).all()
    all_bookings    = Booking.query.order_by(Booking.created_at.desc()).all()
    all_orders      = Order.query.order_by(Order.created_at.desc()).all()
    all_products    = Product.query.all()
    all_users       = User.query.all()

    return render_template('admin_dashboard.html',
        total_bookings=total_bookings,
        total_orders=total_orders,
        total_revenue=f'{total_revenue:,.2f}',
        total_users=total_users,
        booking_status_counts=booking_status_counts,
        order_status_counts=order_status_counts,
        top_services=top_services,
        new_users_today=new_users_today,
        recent_bookings=recent_bookings,
        all_bookings=all_bookings,
        all_orders=all_orders,
        all_products=all_products,
        all_users=all_users,
    )


@app.route('/admin/booking/<int:booking_id>/status', methods=['POST'])
@login_required
def update_booking_status(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    new_status = request.form['status']
    booking.status = new_status
    db.session.commit()

    # ── Notify customer of booking status change ──
    status_messages = {
        'confirmed':  f'Your {booking.service} appointment on {booking.date.strftime("%b %d, %Y")} has been confirmed.',
        'inprogress': f'Your {booking.service} is now in progress. Our mechanic is working on your motorcycle.',
        'completed':  f'Your {booking.service} appointment has been completed. Thank you!',
        'cancelled':  f'Your {booking.service} appointment on {booking.date.strftime("%b %d, %Y")} has been cancelled.',
    }
    status_titles = {
        'confirmed':  'Booking Confirmed!',
        'inprogress': 'Service In Progress',
        'completed':  'Service Completed!',
        'cancelled':  'Booking Cancelled',
    }
    if new_status in status_messages:
        send_notification(
            user_id=booking.user_id,
            title=status_titles[new_status],
            message=status_messages[new_status],
            type='booking',
            status=new_status
        )

    flash('Booking status updated!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/order/<int:order_id>/status', methods=['POST'])
@login_required
def update_order_status(order_id):
    order = Order.query.get_or_404(order_id)
    new_status = request.form['status']
    order.status = new_status
    db.session.commit()

    # ── Notify customer of order status change ──
    status_messages = {
        'confirmed':   f'Your order ORD-{order.id:03d} has been confirmed and is being prepared.',
        'processing':  f'Your order ORD-{order.id:03d} is now being processed.',
        'shipped':     f'Your order ORD-{order.id:03d} is out for delivery!',
        'delivered':   f'Your order ORD-{order.id:03d} has been delivered. Enjoy!',
        'cancelled':   f'Your order ORD-{order.id:03d} has been cancelled.',
    }
    status_titles = {
        'confirmed':  'Order Confirmed!',
        'processing': 'Order Processing',
        'shipped':    'Order Out for Delivery!',
        'delivered':  'Order Delivered!',
        'cancelled':  'Order Cancelled',
    }
    if new_status in status_messages:
        send_notification(
            user_id=order.user_id,
            title=status_titles[new_status],
            message=status_messages[new_status],
            type='order',
            status=new_status
        )

    flash('Order status updated!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/product/add', methods=['POST'])
@login_required
def add_product():
    product = Product(
        name=request.form['name'],
        category=request.form['category'],
        description=request.form.get('description', ''),
        price=float(request.form['price']),
        stock=int(request.form['stock'])
    )
    db.session.add(product)
    db.session.commit()
    flash('Product added successfully!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/add-staff', methods=['POST'])
@login_required
def add_staff():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    email = request.form['email']
    if User.query.filter_by(email=email).first():
        flash('Email already exists.', 'danger')
        return redirect(url_for('admin_dashboard'))

    staff = User(
        fullname=request.form['fullname'],
        email=email,
        phone=request.form['phone'],
        role='staff',
        email_verified=True
    )
    staff.set_password(request.form['password'])
    db.session.add(staff)
    db.session.commit()
    flash(f'Staff account for {staff.fullname} created!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/product/<int:product_id>/edit', methods=['POST'])
@login_required
def edit_product(product_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    product             = Product.query.get_or_404(product_id)
    product.barcode     = request.form.get('barcode', '').strip() or None
    product.name        = request.form['name']
    product.category    = request.form['category']
    product.price       = float(request.form['price'])
    product.stock       = int(request.form['stock'])
    product.description = request.form.get('description', '')
    db.session.commit()
    flash(f'Product "{product.name}" updated!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/product/<int:product_id>/delete', methods=['POST'])
@login_required
def delete_product(product_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    product = Product.query.get_or_404(product_id)
    name    = product.name
    db.session.delete(product)
    db.session.commit()
    flash(f'Product "{name}" deleted!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/import-products', methods=['POST'])
@login_required
def import_products():
    if current_user.role != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json()
    if not data or 'products' not in data:
        return jsonify({'success': False, 'error': 'No data received'}), 400

    rows = data['products']
    if not rows:
        return jsonify({'success': False, 'error': 'Empty product list'}), 400

    imported, errors = 0, []
    OrderItem.query.delete()
    Order.query.delete()
    Product.query.delete()
    db.session.flush()

    for i, row in enumerate(rows):
        try:
            name        = str(row.get('name', '')).strip()
            category    = str(row.get('category', '')).strip()
            price       = float(row.get('price', 0))
            stock       = int(float(row.get('stock', 0)))
            description = str(row.get('description', '')).strip() or None
            barcode     = str(row.get('barcode', '')).strip() or None

            if not name or not category:
                errors.append(f'Row {i+1}: missing name or category')
                continue

            db.session.add(Product(
                barcode=barcode, name=name, category=category,
                price=price, stock=stock, description=description
            ))
            imported += 1
        except (ValueError, TypeError) as e:
            errors.append(f'Row {i+1}: {str(e)}')

    if imported == 0:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'No valid products. ' + '; '.join(errors)}), 400

    db.session.commit()
    return jsonify({'success': True, 'count': imported, 'errors': errors})


# ─────────────────────────────────────────
# ADMIN: DELETE USER
# ─────────────────────────────────────────

@app.route('/admin/user/<int:user_id>/delete', methods=['POST'])
@login_required
def delete_user(user_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))

    user = User.query.get_or_404(user_id)

    if user.id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if user.role == 'admin':
        flash('Admin accounts cannot be deleted.', 'danger')
        return redirect(url_for('admin_dashboard'))

    name = user.fullname

    OTPRecord.query.filter_by(email=user.email).delete()

    for order in user.orders:
        OrderItem.query.filter_by(order_id=order.id).delete()
    Order.query.filter_by(user_id=user.id).delete()

    Booking.query.filter_by(user_id=user.id).delete()
    Notification.query.filter_by(user_id=user.id).delete()

    db.session.delete(user)
    db.session.commit()
    flash(f'User "{name}" has been deleted successfully.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/api/notifications/<int:notif_id>/delete', methods=['POST'])
@login_required
def delete_notification(notif_id):
    n = Notification.query.filter_by(id=notif_id, user_id=current_user.id).first_or_404()
    db.session.delete(n)
    db.session.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────
# TEMP: CREATE ADMIN
# ─────────────────────────────────────────

@app.route('/create-admin')
def create_admin():
    existing = User.query.filter_by(email='admin@mototyre.com').first()
    if existing:
        return 'Admin already exists!'
    admin = User(
        fullname='Admin',
        email='admin@mototyre.com',
        phone='09204434180',
        role='admin',
        email_verified=True
    )
    admin.set_password('admin123')
    db.session.add(admin)
    db.session.commit()
    return 'Admin created! Now delete this route.'


#with app.app_context():
#    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)