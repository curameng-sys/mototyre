from flask import Flask, render_template, redirect, url_for, flash, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from flask import jsonify
from sqlalchemy import func
from datetime import date as date_today
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-dev-key')
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://root:@localhost/mototyre_db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ─────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    fullname = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='customer')
    motorcycle_plate = db.Column(db.String(20), nullable=True)
    motorcycle_model = db.Column(db.String(100), nullable=True)
    bookings = db.relationship('Booking', backref='customer', lazy=True)
    orders = db.relationship('Order', backref='customer', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    service = db.Column(db.String(100), nullable=False)
    date = db.Column(db.Date, nullable=False)
    time = db.Column(db.Time, nullable=False)
    motorcycle_model = db.Column(db.String(100), nullable=True)
    motorcycle_plate = db.Column(db.String(20), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='pending')  # pending, confirmed, completed, cancelled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    price = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    order_items = db.relationship('OrderItem', backref='product', lazy=True)


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending, processing, shipped, completed, cancelled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    items = db.relationship('OrderItem', backref='order', lazy=True)


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

# ─────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────

@app.route('/')
def home():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            if user.role in ['admin', 'staff']:
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('customer_dashboard'))
        flash('Invalid email or password.', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        fullname = request.form['fullname']
        email = request.form['email']
        phone = request.form['phone']
        password = request.form['password']
        plate = request.form.get('plate')
        model = request.form.get('model')

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash('Email already registered.', 'danger')
            return redirect(url_for('register'))

        new_user = User(
            fullname=fullname,
            email=email,
            phone=phone,
            motorcycle_plate=plate,
            motorcycle_model=model
        )
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        flash('Account created! Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')

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
    bookings = Booking.query.filter_by(user_id=current_user.id).order_by(Booking.created_at.desc()).all()
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    products = Product.query.filter(Product.stock > 0).all()
    return render_template('customer_dashboard.html',
        bookings=bookings,
        orders=orders,
        products=products
    )

@app.route('/customer/book', methods=['POST'])
@login_required
def book_service():
    service = request.form['service']
    date_str = request.form['date']
    time_str = request.form['time']
    notes = request.form.get('notes', '')
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
    flash('Appointment booked successfully!', 'success')
    return redirect(url_for('customer_dashboard'))

@app.route('/customer/order', methods=['POST'])
@login_required
def place_order():
    product_id = request.form['product_id']
    quantity = int(request.form['quantity'])

    product = Product.query.get_or_404(product_id)

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
    flash('Order placed successfully!', 'success')
    return redirect(url_for('customer_dashboard'))

@app.route('/customer/profile', methods=['GET', 'POST'])
@login_required
def customer_profile():
    if request.method == 'POST':
        current_user.fullname = request.form['fullname']
        current_user.phone = request.form['phone']
        current_user.motorcycle_model = request.form.get('motorcycle_model')
        current_user.motorcycle_plate = request.form.get('motorcycle_plate')
        db.session.commit()
        flash('Profile updated successfully!', 'success')
        return redirect(url_for('customer_dashboard'))
    return render_template('customer_dashboard.html')

@app.route('/api/booked-slots')
@login_required
def get_booked_slots():
    """Returns booked time slots per date for a given month."""
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)

    if not year or not month:
        return jsonify({})

    # Get all bookings for this month
    from datetime import date
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    bookings = Booking.query.filter(
        Booking.date >= start,
        Booking.date < end,
        Booking.status != 'cancelled'
    ).all()

    # Group by date → list of booked times
    result = {}
    for b in bookings:
        date_str = b.date.strftime('%Y-%m-%d')
        time_str = b.time.strftime('%H:%M')
        if date_str not in result:
            result[date_str] = []
        result[date_str].append(time_str)

    return jsonify(result)

# ─────────────────────────────────────────
# ADMIN ROUTES
# ─────────────────────────────────────────

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if current_user.role not in ['admin', 'staff']:
        flash('Access denied.', 'danger')
        return redirect(url_for('customer_dashboard'))

    # Counts
    total_bookings = Booking.query.count()
    total_orders = Order.query.count()
    total_users = User.query.count()
    total_revenue = db.session.query(func.sum(Order.total_amount)).scalar() or 0

    # Booking status breakdown
    booking_status_counts = dict(
        db.session.query(Booking.status, func.count(Booking.id))
        .group_by(Booking.status).all()
    )

    # Order status breakdown
    order_status_counts = dict(
        db.session.query(Order.status, func.count(Order.id))
        .group_by(Order.status).all()
    )

    # Top services
    top_services = (
        db.session.query(Booking.service, func.count(Booking.id).label('count'))
        .group_by(Booking.service)
        .order_by(func.count(Booking.id).desc())
        .limit(5).all()
    )

    # New users today
    today = date_today.today()
    new_users_today = User.query.filter(
        func.date(User.id) == today  # rough estimate; replace with created_at if you add that column
    ).count()

    # Recent bookings (last 5)
    recent_bookings = Booking.query.order_by(Booking.created_at.desc()).limit(5).all()

    # All data for management tabs
    all_bookings = Booking.query.order_by(Booking.created_at.desc()).all()
    all_orders = Order.query.order_by(Order.created_at.desc()).all()
    all_products = Product.query.all()
    all_users = User.query.all()

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
        role='admin'
    )
    admin.set_password('admin123')
    db.session.add(admin)
    db.session.commit()
    return 'Admin created! Now delete this route.'

if __name__ == '__main__':
    app.run(debug=True)