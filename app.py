from flask import Flask, render_template, redirect, url_for, flash, request, session, jsonify
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
import os, uuid, random, string, base64, requests
import requests
import base64

app = Flask(__name__)
app.config.update(
    SECRET_KEY='mototyre-fixed-secret-key-xK9mP2qL7rZ3wN8vB4',
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

# ─── GMAIL CONFIG ───────────────────────────────────────────────────────────

GMAIL_SCOPES     = ["https://www.googleapis.com/auth/gmail.send"]
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_CREDS_FILE = "credentials.json"
GMAIL_SENDER     = os.getenv("GMAIL_SENDER", "mackycastanales05@gmail.com")
OTP_EXPIRY_MINS  = 10

# ─── PAYMONGO CONFIG ────────────────────────────────────────────────────────

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
                "payment_method_types": ["card", "gcash"],
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
            creds.refresh(Request())
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


# ─── MODELS ─────────────────────────────────────────────────────────────────

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)


class Product(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    barcode     = db.Column(db.String(100))
    name        = db.Column(db.String(150), nullable=False)
    category    = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    price       = db.Column(db.Float, nullable=False)
    stock       = db.Column(db.Integer, default=0)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    order_items = db.relationship('OrderItem', backref='product', lazy=True)


class Order(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total_amount    = db.Column(db.Float, nullable=False)
    status          = db.Column(db.String(20), default='pending')
    payment_method  = db.Column(db.String(20), default='cash')
    delivery_method = db.Column(db.String(20), default='pickup')
    ship_address    = db.Column(db.Text)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    items           = db.relationship('OrderItem', backref='order', lazy=True)


class OrderItem(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    order_id   = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity   = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)


class Notification(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title      = db.Column(db.String(120), nullable=False)
    message    = db.Column(db.Text, nullable=False)
    type       = db.Column(db.String(30), default='update')
    status     = db.Column(db.String(30))
    is_read    = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ─── HELPERS ────────────────────────────────────────────────────────────────

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
        expires_at=datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MINS)
    ))
    db.session.commit()
    return otp


def _verify_otp(email, otp_input, purpose):
    record = OTPRecord.query.filter_by(email=email, purpose=purpose, used=False)\
                            .order_by(OTPRecord.created_at.desc()).first()
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


ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def cleanup_abandoned_gcash_orders():
    """Cancel GCash orders that haven't been paid within 30 minutes."""
    cutoff = datetime.utcnow() - timedelta(minutes=30)
    abandoned = Order.query.filter(
        Order.status == 'awaiting_payment',
        Order.payment_method == 'gcash',
        Order.created_at < cutoff
    ).all()
    
    for order in abandoned:
        # Restore stock
        for item in order.items:
            product = Product.query.get(item.product_id)
            if product:
                product.stock += item.quantity
        
        # Delete order items and order
        OrderItem.query.filter_by(order_id=order.id).delete()
        db.session.delete(order)
    
    db.session.commit()
    return len(abandoned)


# ─── AUTH ────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return render_template('landing.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for(f'{current_user.role}_dashboard'))

    if request.method == 'POST':
        email, password = request.form['email'], request.form['password']
        user = User.query.filter_by(email=email).first()

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
        result = _verify_otp(email, request.form.get('otp', ''), purpose="login")
        if result['valid']:
            session.pop('pending_login_email', None)
            user = User.query.filter_by(email=email).first()
            login_user(user, remember=True)
            next_page = request.args.get('next') or session.pop('next', None)
            if next_page and next_page.startswith('/'):
                return redirect(next_page)
            return redirect(url_for(f'{user.role}_dashboard'))
        flash(result['message'], 'danger')

    return render_template('verify_otp.html', purpose='login', email=email)


@app.route('/resend-otp/<purpose>')
def resend_otp(purpose):
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
        email = request.form['email']
        phone = request.form['phone']

        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
            return redirect(url_for('register'))
        if not phone.isdigit() or not (10 <= len(phone) <= 13):
            flash('Invalid phone number.', 'danger')
            return redirect(url_for('register'))

        user = User(
            fullname=request.form['fullname'], email=email, phone=phone,
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


# ─── FORGOT PASSWORD ─────────────────────────────────────────────────────────

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


# ─── CUSTOMER ────────────────────────────────────────────────────────────────

@app.route('/customer/dashboard')
@login_required
def customer_dashboard():
    bookings   = Booking.query.filter_by(user_id=current_user.id).order_by(Booking.created_at.desc()).all()
    orders     = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    products   = Product.query.filter(Product.stock > 0).all()
    categories = ['Tires', 'Engine Oil', 'Air Filter', 'Brake Shoe']
    return render_template('customer_dashboard.html', bookings=bookings, orders=orders,
                           products=products)


@app.route('/customer/book', methods=['POST'])
@login_required
def book_service():
    service = request.form['service']
    booking = Booking(
        user_id=current_user.id,
        service=service,
        date=datetime.strptime(request.form['date'], '%Y-%m-%d').date(),
        time=datetime.strptime(request.form['time'], '%H:%M').time(),
        motorcycle_model=request.form.get('motorcycle_model', current_user.motorcycle_model),
        motorcycle_plate=request.form.get('motorcycle_plate', current_user.motorcycle_plate),
        notes=request.form.get('notes', '')
    )
    db.session.add(booking)
    db.session.commit()
    send_notification(
        current_user.id, 'Booking Received!',
        f'Your {service} appointment on {booking.date.strftime("%b %d, %Y")} at {booking.time.strftime("%I:%M %p")} is pending confirmation.',
        type='booking', status='pending'
    )
    flash('Appointment booked successfully!', 'success')
    return redirect(url_for('customer_dashboard'))


@app.route('/customer/order', methods=['POST'])
@login_required
def place_order():
    product  = Product.query.get_or_404(request.form['product_id'])
    quantity = int(request.form['quantity'])
    if product.stock < quantity:
        flash('Not enough stock available.', 'danger')
        return redirect(url_for('customer_dashboard'))
    
    total = product.price * quantity
    payment_method = request.form.get('payment_method', 'cash')
    delivery_method = request.form.get('delivery_method', 'pickup')
    
    # Build shipping address if delivery method is ship
    ship_address = ''
    if delivery_method == 'ship':
        ship_name = request.form.get('ship_name', '')
        ship_mobile = request.form.get('ship_mobile', '')
        ship_street = request.form.get('ship_street', '')
        ship_city = request.form.get('ship_city', '')
        ship_province = request.form.get('ship_province', '')
        ship_zip = request.form.get('ship_zip', '')
        ship_address = f"{ship_name}\n{ship_mobile}\n{ship_street}, {ship_city}, {ship_province} {ship_zip}"
        
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
    
    # Notification
    delivery_text = "for pickup" if delivery_method == 'pickup' else "for delivery"
    send_notification(
        current_user.id, 'Order Placed!',
        f'Your order for {product.name} (x{quantity}) worth ₱{total:.2f} is now pending {delivery_text}.',
        type='order', status='pending'
    )
    
    # If GCash payment, redirect to PayMongo
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
        current_user.fullname         = request.form['fullname']
        current_user.phone            = request.form['phone']
        current_user.motorcycle_model = request.form.get('motorcycle_model')
        current_user.motorcycle_plate = request.form.get('motorcycle_plate')
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
    dest = {'admin': 'admin_dashboard', 'staff': 'staff_dashboard'}.get(current_user.role, 'customer_dashboard')
    return redirect(url_for(dest))


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


# ─── NOTIFICATIONS ───────────────────────────────────────────────────────────

@app.route('/api/notifications')
@login_required
def get_notifications():
    notifs = Notification.query.filter_by(user_id=current_user.id)\
                               .order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify([{
        'id': n.id, 'title': n.title, 'message': n.message,
        'type': n.type, 'status': n.status,
        'is_read': n.is_read, 'created_at': n.created_at.isoformat()
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


# ─── PROFILE UPDATES ─────────────────────────────────────────────────────────

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


# ─── STAFF ───────────────────────────────────────────────────────────────────

@app.route('/staff/dashboard')
@login_required
def staff_dashboard():
    if current_user.role != 'staff':
        return redirect(url_for('customer_dashboard'))
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


# ─── ADMIN ───────────────────────────────────────────────────────────────────

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if current_user.role not in ['admin', 'staff']:
        flash('Access denied.', 'danger')
        return redirect(url_for('customer_dashboard'))
    
    cleanup_abandoned_gcash_orders()
    
    return render_template('admin_dashboard.html',
        total_bookings=Booking.query.count(),
        total_orders=Order.query.count(),
        total_users=User.query.count(),
        total_revenue=f'{db.session.query(func.sum(Order.total_amount)).scalar() or 0:,.2f}',
        booking_status_counts=dict(db.session.query(Booking.status, func.count(Booking.id)).group_by(Booking.status).all()),
        order_status_counts=dict(db.session.query(Order.status, func.count(Order.id)).group_by(Order.status).all()),
        top_services=db.session.query(Booking.service, func.count(Booking.id).label('count')).group_by(Booking.service).order_by(func.count(Booking.id).desc()).limit(5).all(),
        new_users_today=User.query.filter(func.date(User.id) == date.today()).count(),
        recent_bookings=Booking.query.order_by(Booking.created_at.desc()).limit(5).all(),
        all_bookings=Booking.query.order_by(Booking.created_at.desc()).all(),
        all_orders=Order.query.order_by(Order.created_at.desc()).all(),
        all_products=Product.query.all(),
        all_users=User.query.all(),
    )


@app.route('/admin/booking/<int:bid>/status', methods=['POST'])
@login_required
def update_booking_status(bid):
    booking    = Booking.query.get_or_404(bid)
    new_status = request.form['status']
    booking.status = new_status
    db.session.commit()

    messages = {
        'confirmed':  ('Booking Confirmed!',  f'Your {booking.service} on {booking.date.strftime("%b %d, %Y")} is confirmed.'),
        'inprogress': ('Service In Progress', f'Your {booking.service} is now in progress.'),
        'completed':  ('Service Completed!',  f'Your {booking.service} has been completed. Thank you!'),
        'cancelled':  ('Booking Cancelled',   f'Your {booking.service} on {booking.date.strftime("%b %d, %Y")} was cancelled.'),
    }
    if new_status in messages:
        title, msg = messages[new_status]
        send_notification(booking.user_id, title, msg, type='booking', status=new_status)

    flash('Booking status updated!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/order/<int:oid>/status', methods=['POST'])
@login_required
def update_order_status(oid):
    order      = Order.query.get_or_404(oid)
    new_status = request.form['status']
    order.status = new_status
    db.session.commit()

    messages = {
        'confirmed':  ('Order Confirmed!',        f'Your order ORD-{order.id:03d} is confirmed and being prepared.'),
        'processing': ('Order Processing',        f'Your order ORD-{order.id:03d} is being processed.'),
        'shipped':    ('Order Out for Delivery!', f'Your order ORD-{order.id:03d} is on its way!'),
        'delivered':  ('Order Delivered!',        f'Your order ORD-{order.id:03d} has been delivered.'),
        'cancelled':  ('Order Cancelled',         f'Your order ORD-{order.id:03d} has been cancelled.'),
    }
    if new_status in messages:
        title, msg = messages[new_status]
        send_notification(order.user_id, title, msg, type='order', status=new_status)

    flash('Order status updated!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/product/add', methods=['POST'])
@login_required
def add_product():
    db.session.add(Product(
        name=request.form['name'], category=request.form['category'],
        description=request.form.get('description', ''),
        price=float(request.form['price']), stock=int(request.form['stock'])
    ))
    db.session.commit()
    flash('Product added successfully!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/product/<int:pid>/edit', methods=['POST'])
@login_required
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


@app.route('/admin/product/<int:pid>/delete', methods=['POST'])
@login_required
def delete_product(pid):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    p = Product.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    flash(f'Product "{p.name}" deleted!', 'success')
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
    staff = User(fullname=request.form['fullname'], email=email,
                 phone=request.form['phone'], role='staff', email_verified=True)
    staff.set_password(request.form['password'])
    db.session.add(staff)
    db.session.commit()
    flash(f'Staff account for {staff.fullname} created!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/user/<int:uid>/delete', methods=['POST'])
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


@app.route('/admin/import-products', methods=['POST'])
@login_required
def import_products():
    if current_user.role != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    if not data or not data.get('products'):
        return jsonify({'success': False, 'error': 'No data received'}), 400

    try:
        # Clear existing products (optional - remove these 3 lines if you want to keep existing)
        OrderItem.query.delete()
        Order.query.delete()
        Product.query.delete()
        db.session.flush()

        imported = 0
        for row in data['products'][:500]:  # Limit to 500
            try:
                name = str(row.get('name', '')).strip()
                category = str(row.get('category', '')).strip()
                if not name or not category:
                    continue
                
                price_str = str(row.get('price', '0')).replace(',', '').strip()
                price = float(price_str) if price_str else 0
                
                stock_str = str(row.get('stock', '0')).replace(',', '').strip()
                stock = int(float(stock_str)) if stock_str else 0
                
                db.session.add(Product(
                    barcode=str(row.get('barcode', '')).strip() or None,
                    name=name,
                    category=category,
                    price=price,
                    stock=stock,
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


# ─── POS (Point of Sale) ─────────────────────────────────────────────────────

@app.route('/admin/pos')
@login_required
def pos():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('customer_dashboard'))
    products = Product.query.filter(Product.stock > 0).order_by(Product.category, Product.name).all()
    customers = User.query.filter_by(role='customer').order_by(User.fullname).all()
    return render_template('pos.html', products=products, customers=customers)


@app.route('/admin/pos/checkout', methods=['POST'])
@login_required
def pos_checkout():
    if current_user.role not in ['admin', 'staff']:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

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

    # Validate stock for non-pending items (new products added at POS)
    for item in cart:
        if not item.get('source_order_id'):  # Only check stock for new items
            product = Product.query.get(item['product_id'])
            if not product:
                return jsonify({'success': False, 'error': f'Product ID {item["product_id"]} not found'}), 400
            if product.stock < item['quantity']:
                return jsonify({'success': False, 'error': f'Insufficient stock for {product.name}'}), 400

    order_id = None
    completed_order_ids = set()
    completed_booking_ids = set()

    # ─── HANDLE PRODUCTS ────────────────────────────────────────────────────
    
    # Separate items: those from existing orders vs new POS items
    new_cart_items = [item for item in cart if not item.get('source_order_id')]
    pending_cart_items = [item for item in cart if item.get('source_order_id')]

    # Mark source orders as completed
    for item in pending_cart_items:
        source_order_id = item.get('source_order_id')
        if source_order_id and source_order_id not in completed_order_ids:
            source_order = Order.query.get(source_order_id)
            if source_order:
                source_order.status = 'completed'
                source_order.payment_method = payment_method
                completed_order_ids.add(source_order_id)
                
                # Notify customer
                if source_order.user_id:
                    send_notification(
                        source_order.user_id,
                        'Order Completed!',
                        f'Your order ORD-{source_order.id:03d} has been completed and paid.',
                        type='order', status='completed'
                    )

    # Create new order for POS-added items (not from pending orders)
    if new_cart_items:
        uid = int(customer_id) if customer_id else current_user.id
        new_total = sum(item['quantity'] * float(item['unit_price']) for item in new_cart_items)
        order = Order(user_id=uid, total_amount=new_total, status='completed', payment_method=payment_method)
        db.session.add(order)
        db.session.flush()
        order_id = order.id

        for item in new_cart_items:
            product = Product.query.get(item['product_id'])
            db.session.add(OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=item['quantity'],
                unit_price=float(item['unit_price'])
            ))
            product.stock -= item['quantity']

        if customer_id:
            send_notification(
                int(customer_id),
                'Order Completed (In-Store)',
                f'Your in-store order ORD-{order.id:03d} worth ₱{new_total:,.2f} has been completed.',
                type='order', status='completed'
            )

    # ─── HANDLE SERVICES ────────────────────────────────────────────────────
    
    booking_ids = []
    for svc in services:
        source_booking_id = svc.get('source_booking_id')
        
        if source_booking_id:
            # Update existing booking to completed
            source_booking = Booking.query.get(source_booking_id)
            if source_booking:
                source_booking.status = 'completed'
                source_booking.payment_method = payment_method
                completed_booking_ids.add(source_booking_id)
                booking_ids.append(source_booking_id)
                
                # Notify customer
                if source_booking.user_id:
                    send_notification(
                        source_booking.user_id,
                        'Service Completed!',
                        f'Your {source_booking.service} service has been completed. Thank you!',
                        type='booking', status='completed'
                    )
        else:
            # Create new booking for walk-in service
            booking = Booking(
                user_id=int(customer_id) if customer_id else current_user.id,
                service=svc['name'],
                date=date.today(),
                time=datetime.now().time(),
                motorcycle_model=svc.get('motorcycle_model', ''),
                motorcycle_plate=svc.get('motorcycle_plate', ''),
                notes='Walk-in POS service.',
                status='completed',
                payment_method=payment_method
            )
            db.session.add(booking)
            db.session.flush()
            booking_ids.append(booking.id)

            if customer_id:
                send_notification(
                    int(customer_id),
                    'Service Completed (In-Store)',
                    f'Your {svc["name"]} walk-in service has been recorded.',
                    type='booking', status='completed'
                )

    db.session.commit()

    return jsonify({
        'success': True,
        'order_id': order_id,
        'booking_ids': booking_ids,
        'completed_orders': list(completed_order_ids),
        'completed_bookings': list(completed_booking_ids),
        'message': 'Transaction completed successfully.'
    })


@app.route('/admin/pos/transactions')
@login_required
def pos_transactions():
    """Recent POS transactions (completed orders + walk-in bookings today)."""
    if current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    today = date.today()
    orders = (Order.query
              .filter(Order.status == 'completed', func.date(Order.created_at) == today)
              .order_by(Order.created_at.desc()).limit(20).all())

    result = []
    for o in orders:
        customer = db.session.get(User, o.user_id)
        result.append({
            'type': 'order',
            'id': f'ORD-{o.id:03d}',
            'customer': customer.fullname if customer else 'Walk-in',
            'total': o.total_amount,
            'time': o.created_at.strftime('%I:%M %p'),
            'items': [f'{i.product.name} x{i.quantity}' for i in o.items]
        })

    return jsonify(result)


@app.route('/admin/pos/customer/<int:cid>/items')
@login_required
def pos_customer_items(cid: int):
    if current_user.role not in ['admin', 'staff']:
        return jsonify({'error': 'Unauthorized'}), 403

    customer = User.query.get_or_404(cid)

    pending_orders = Order.query.filter(
    Order.user_id == cid,
    Order.status.in_(['pending', 'processing', 'awaiting_payment']),
    Order.payment_method != 'gcash',
    Order.delivery_method == 'pickup'
).order_by(Order.created_at.desc()).all()

    orders_data = []
    for order in pending_orders:
        items = []
        for oi in order.items:
            product = db.session.get(Product, oi.product_id)
            items.append({
                'product_id':   oi.product_id,
                'product_name': product.name if product else 'Unknown Product',
                'quantity':     oi.quantity,
                'unit_price':   oi.unit_price,
            })
        orders_data.append({
            'order_id':   order.id,
            'status':     order.status,
            'total':      order.total_amount,
            'created_at': order.created_at.isoformat(),
            'items':      items,
        })

    pending_bookings = Booking.query.filter(
        Booking.user_id == cid,
        Booking.status.in_(['in_progress'])
    ).order_by(Booking.created_at.desc()).all()

    bookings_data = []
    for b in pending_bookings:
        bookings_data.append({
            'booking_id': b.id,
            'service':    b.service,
            'date':       b.date.strftime('%Y-%m-%d'),
            'time':       b.time.strftime('%H:%M'),
            'status':     b.status,
            'created_at': b.created_at.isoformat(),
        })

    return jsonify({
        'customer_id':   cid,
        'customer_name': customer.fullname,
        'orders':        orders_data,
        'bookings':      bookings_data,
    })

# ─── PAYMONGO PAYMENTS ───────────────────────────────────────────────────────

@app.route('/pay/order/<int:oid>')
@login_required
def pay_order(oid):
    """Generate GCash payment link for an order."""
    order = Order.query.get_or_404(oid)
    
    # Verify ownership
    if order.user_id != current_user.id:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('customer_dashboard'))
    
    # Check if already paid
    if order.status not in ['pending']:
        flash('This order cannot be paid online.', 'warning')
        return redirect(url_for('customer_dashboard'))
    
    # Get order items for description
    items_desc = ", ".join([f"{item.product.name} x{item.quantity}" for item in order.items])
    description = f"MotoTyre Order #{order.id:03d}: {items_desc[:100]}"
    
    result = create_gcash_payment(
        amount=order.total_amount,
        description=description,
        order_id=order.id
    )
    
    if result["success"]:
        # Store payment reference
        order.payment_method = 'gcash'
        order.notes = f"PayMongo: {result['reference']}"
        db.session.commit()
        return redirect(result["checkout_url"])
    else:
        flash('Could not create payment. Please try again.', 'danger')
        return redirect(url_for('customer_dashboard'))


@app.route('/pay/booking/<int:bid>')
@login_required
def pay_booking(bid):
    """Generate GCash payment link for a booking."""
    booking = Booking.query.get_or_404(bid)
    
    # Verify ownership
    if booking.user_id != current_user.id:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('customer_dashboard'))
    
    # Check if can be paid
    if booking.status not in ['pending', 'confirmed']:
        flash('This booking cannot be paid online.', 'warning')
        return redirect(url_for('customer_dashboard'))
    
    # Get service price (you may need to adjust this based on your pricing)
    SERVICE_PRICES = {
        'Oil Change': 350,
        'Tire Change – Front': 150,
        'Tire Change – Rear': 150,
        'Tire Change – Both': 250,
        'Brake Inspection': 100,
        'Brake Pad Replacement': 300,
        'Chain Cleaning & Lube': 150,
        'Chain Replacement': 400,
        'Spark Plug Replacement': 200,
        'Battery Check & Replacement': 250,
        'General Checkup': 200,
        'Full Tune-up': 800,
        'CVT Cleaning': 500,
        'FI Cleaning': 600,
        'ECU Remapping': 1500,
        'Full Overhaul': 3000,
        'Overhaul': 2500,
    }
    
    # Find matching price (case-insensitive partial match)
    amount = 500  # Default service fee
    for service_name, price in SERVICE_PRICES.items():
        if service_name.lower() in booking.service.lower():
            amount = price
            break
    
    description = f"MotoTyre Booking #{booking.id}: {booking.service}"
    
    result = create_gcash_payment(
        amount=amount,
        description=description,
        booking_id=booking.id
    )
    
    if result["success"]:
        booking.payment_method = 'gcash'
        booking.notes = (booking.notes or '') + f" | PayMongo: {result['reference']}"
        db.session.commit()
        return redirect(result["checkout_url"])
    else:
        flash('Could not create payment. Please try again.', 'danger')
        return redirect(url_for('customer_dashboard'))


@app.route('/payment/success')
def payment_success():
    """Handle successful payment redirect."""
    order_id = request.args.get('order_id')
    booking_id = request.args.get('booking_id')
    
    if order_id:
        try:
            order = Order.query.get(int(order_id))
            if order and order.status == 'awaiting_payment':
                order.status = 'confirmed'
                db.session.commit()
                send_notification(
                    order.user_id,
                    'Payment Received!',
                    f'Your GCash payment for Order ORD-{order.id:03d} has been confirmed.',
                    type='order', status='confirmed'
                )
        except:
            pass
    
    if booking_id:
        try:
            booking = Booking.query.get(int(booking_id))
            if booking and booking.status == 'pending':
                booking.status = 'confirmed'
                db.session.commit()
        except:
            pass
    
    return redirect('http://127.0.0.1:5000/customer/dashboard')


@app.route('/webhook/paymongo', methods=['POST'])
def paymongo_webhook():
    """Handle PayMongo webhook for payment status updates."""
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'No data'}), 400
    
    event_type = data.get('data', {}).get('attributes', {}).get('type')
    resource = data.get('data', {}).get('attributes', {}).get('data', {})
    
    if event_type == 'link.payment.paid':
        metadata = resource.get('attributes', {}).get('metadata', {})
        
        # Update order if this was an order payment
        order_id = metadata.get('order_id')
        if order_id:
            order = Order.query.get(int(order_id))
            if order and order.status in ['pending', 'awaiting_payment']:
                order.status = 'confirmed'
                order.payment_method = 'gcash'
                db.session.commit()
                send_notification(
                    order.user_id,
                    'Payment Received!',
                    f'Your payment for Order #{order.id:03d} has been confirmed.',
                    type='order', status='confirmed'
                )
        
        # Update booking if this was a booking payment
        booking_id = metadata.get('booking_id')
        if booking_id:
            booking = Booking.query.get(int(booking_id))
            if booking and booking.status == 'pending':
                booking.status = 'confirmed'
                booking.payment_method = 'gcash'
                db.session.commit()
                send_notification(
                    booking.user_id,
                    'Booking Payment Received!',
                    f'Your payment for {booking.service} on {booking.date.strftime("%b %d")} has been confirmed.',
                    type='booking', status='confirmed'
                )
    
    return jsonify({'success': True})

@app.route('/test-paymongo')
@login_required
def test_paymongo():
    """Test PayMongo connection."""
    result = create_gcash_payment(
        amount=100,
        description="Test Payment",
        order_id=9999
    )
    return jsonify(result)

@app.route('/payment/failed')
def payment_failed():
    """Handle cancelled/failed payment - remove the order."""
    order_id = request.args.get('order_id')
    booking_id = request.args.get('booking_id')
    
    if order_id:
        try:
            order = Order.query.get(int(order_id))
            if order and order.status == 'awaiting_payment':
                # Restore stock
                for item in order.items:
                    product = Product.query.get(item.product_id)
                    if product:
                        product.stock += item.quantity
                
                # Delete order items and order
                OrderItem.query.filter_by(order_id=order.id).delete()
                db.session.delete(order)
                db.session.commit()
        except:
            pass
    
    if booking_id:
        try:
            booking = Booking.query.get(int(booking_id))
            if booking and booking.status == 'pending':
                db.session.delete(booking)
                db.session.commit()
        except:
            pass
    
    return redirect('http://127.0.0.1:5000/customer/dashboard')


# ─── TEMP: CREATE ADMIN ──────────────────────────────────────────────────────

@app.route('/create-admin')
def create_admin():
    if User.query.filter_by(email='admin@mototyre.com').first():
        return 'Admin already exists!'
    admin = User(fullname='Admin', email='admin@mototyre.com',
                 phone='09204434180', role='admin', email_verified=True)
    admin.set_password('admin123')
    db.session.add(admin)
    db.session.commit()
    return 'Admin created! Now delete this route.'


if __name__ == '__main__':
    app.run(debug=True)