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
import os, uuid, random, string, base64

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
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    status       = db.Column(db.String(20), default='pending')
    payment_method = db.Column(db.String(20), default='cash')
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    items        = db.relationship('OrderItem', backref='order', lazy=True)

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
    categories = [c[0] for c in db.session.query(Product.category).distinct().order_by(Product.category).all()]
    return render_template('customer_dashboard.html', bookings=bookings, orders=orders,
                           products=products, categories=categories)


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
    order = Order(user_id=current_user.id, total_amount=total)
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=quantity, unit_price=product.price))
    product.stock -= quantity
    db.session.commit()
    send_notification(
        current_user.id, 'Order Placed!',
        f'Your order for {product.name} (x{quantity}) worth ₱{total:.2f} is now pending.',
        type='order', status='pending'
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

    OrderItem.query.delete()
    Order.query.delete()
    Product.query.delete()
    db.session.flush()

    imported, errors = 0, []
    for i, row in enumerate(data['products']):
        try:
            name, category = str(row.get('name', '')).strip(), str(row.get('category', '')).strip()
            if not name or not category:
                errors.append(f'Row {i+1}: missing name or category')
                continue
            db.session.add(Product(
                barcode=str(row.get('barcode', '')).strip() or None,
                name=name, category=category,
                price=float(row.get('price', 0)),
                stock=int(float(row.get('stock', 0))),
                description=str(row.get('description', '')).strip() or None
            ))
            imported += 1
        except (ValueError, TypeError) as e:
            errors.append(f'Row {i+1}: {e}')

    if imported == 0:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'No valid products. ' + '; '.join(errors)}), 400

    db.session.commit()
    return jsonify({'success': True, 'count': imported, 'errors': errors})

# ─── POS (Point of Sale) ─────────────────────────────────────────────────────

@app.route('/admin/pos')
@login_required
def pos():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('customer_dashboard'))
    products = Product.query.filter(Product.stock > 0).order_by(Product.category, Product.name).all()
    categories = [c[0] for c in db.session.query(Product.category).distinct().order_by(Product.category).all()]
    customers = User.query.filter_by(role='customer').order_by(User.fullname).all()
    return render_template('pos.html', products=products, categories=categories, customers=customers)


@app.route('/admin/pos/checkout', methods=['POST'])
@login_required
def pos_checkout():
    if current_user.role != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data received'}), 400

    cart        = data.get('cart', [])         # [{ product_id, quantity, unit_price }]
    services    = data.get('services', [])     # [{ name, price }]
    customer_id = data.get('customer_id')      # optional
    payment_ref = data.get('payment_ref', '').strip()
    total       = float(data.get('total', 0))

    if not cart and not services:
        return jsonify({'success': False, 'error': 'Cart is empty'}), 400

    # --- validate stock ---
    for item in cart:
        product = Product.query.get(item['product_id'])
        if not product:
            return jsonify({'success': False, 'error': f'Product ID {item["product_id"]} not found'}), 400
        if product.stock < item['quantity']:
            return jsonify({'success': False, 'error': f'Insufficient stock for {product.name}'}), 400

    # --- create order if there are products ---
    order_id = None
    if cart:
        uid   = int(customer_id) if customer_id else current_user.id
        order = Order(user_id=uid, total_amount=total, status='completed', payment_method=data.get('payment_method', 'cash'))
        db.session.add(order)
        db.session.flush()
        order_id = order.id

        for item in cart:
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
                f'Your in-store order ORD-{order.id:03d} worth ₱{total:,.2f} has been completed.',
                type='order', status='completed'
            )

    # --- create walk-in bookings for services ---
    booking_ids = []
    for svc in services:
        booking = Booking(
            user_id=int(customer_id) if customer_id else current_user.id,
            service=svc['name'],
            date=date.today(),
            time=datetime.now().time(),
            motorcycle_model=svc.get('motorcycle_model', ''),
            motorcycle_plate=svc.get('motorcycle_plate', ''),
            notes=f'Walk-in POS. GCash Ref: {payment_ref}' if payment_ref else 'Walk-in POS.',
            status='completed',
            payment_method=data.get('payment_method', 'cash')
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