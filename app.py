from flask import Flask, render_template, redirect, url_for, flash, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from flask import jsonify
from sqlalchemy import func
from datetime import date as date_today
from flask import jsonify, request
from werkzeug.utils import secure_filename
import os
import uuid

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'mysql+pymysql://root:@localhost/mototyre_db')
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
    profile_pic = db.Column(db.String(255), nullable=True)  # ← ADD THIS
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
    barcode = db.Column(db.String(100), nullable=True)
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
    return db.session.get(User, int(user_id))

with app.app_context():
    db.create_all()

# ─────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────

@app.route('/')
def home():
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            elif user.role == 'staff':                        
                return redirect(url_for('staff_dashboard'))   
            else:
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
        
        if not phone.isdigit():
            return redirect(url_for('register'))

        if len(phone) < 10 or len(phone) > 13:
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
    categories = db.session.query(Product.category).distinct().order_by(Product.category).all()
    categories = [c[0] for c in categories]
    return render_template('customer_dashboard.html',
    bookings=bookings,
    orders=orders,
    products=products,
    categories=categories
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
            ext = file.filename.rsplit('.', 1)[1].lower()
            filename = f"{current_user.id}_{uuid.uuid4().hex}.{ext}"
            upload_folder = os.path.join(app.root_path, 'static', 'profile_pics')
            os.makedirs(upload_folder, exist_ok=True)
            file.save(os.path.join(upload_folder, filename))
            current_user.profile_pic = filename
            db.session.commit()
            db.session.refresh(current_user)  # ← forces fresh user data
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
    current_user.phone = request.form['phone']
    db.session.commit()
    flash('Profile updated!', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/staff/update-profile', methods=['POST'])
@login_required
def update_staff_profile():
    current_user.fullname = request.form['fullname']
    current_user.phone = request.form['phone']
    db.session.commit()
    flash('Profile updated!', 'success')
    return redirect(url_for('staff_dashboard'))

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
# STAFF ROUTES
# ─────────────────────────────────────────

@app.route('/staff/dashboard')
@login_required
def staff_dashboard():
    if current_user.role != 'staff':
        return redirect(url_for('customer_dashboard'))
    all_bookings = Booking.query.order_by(Booking.created_at.desc()).all()
    all_orders = Order.query.order_by(Order.created_at.desc()).all()
    all_customers = User.query.filter_by(role='customer').all()
    all_products = Product.query.all()
    recent_bookings = Booking.query.order_by(Booking.created_at.desc()).limit(5).all()
    booking_status_counts = dict(db.session.query(Booking.status, func.count(Booking.id)).group_by(Booking.status).all())
    order_status_counts = dict(db.session.query(Order.status, func.count(Order.id)).group_by(Order.status).all())
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

@app.route('/admin/booking/<int:booking_id>/status', methods=['POST'])
@login_required
def update_booking_status(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    booking.status = request.form['status']
    db.session.commit()
    flash('Booking status updated!', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/order/<int:order_id>/status', methods=['POST'])
@login_required
def update_order_status(order_id):
    order = Order.query.get_or_404(order_id)
    order.status = request.form['status']
    db.session.commit()
    flash('Order status updated!', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/product/add', methods=['POST'])
@login_required
def add_product():
    name = request.form['name']
    category = request.form['category']
    description = request.form.get('description', '')
    price = float(request.form['price'])
    stock = int(request.form['stock'])

    product = Product(
        name=name,
        category=category,
        description=description,
        price=price,
        stock=stock
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

    fullname = request.form['fullname']
    email    = request.form['email']
    phone    = request.form['phone']
    password = request.form['password']

    if User.query.filter_by(email=email).first():
        flash('Email already exists.', 'danger')
        return redirect(url_for('admin_dashboard'))

    staff = User(
        fullname=fullname,
        email=email,
        phone=phone,
        role='staff'
    )
    staff.set_password(password)
    db.session.add(staff)
    db.session.commit()
    flash(f'Staff account for {fullname} created!', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/product/<int:product_id>/edit', methods=['POST']) 
@login_required
def edit_product(product_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('admin_dashboard'))
    product = Product.query.get_or_404(product_id)
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
    name = product.name
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

    imported = 0
    errors = []

    # Delete order items first, then products (foreign key constraint)
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

            product = Product(
                barcode=barcode,
                name=name,
                category=category,
                price=price,
                stock=stock,
                description=description
            )
            db.session.add(product)
            imported += 1

        except (ValueError, TypeError) as e:
            errors.append(f'Row {i+1}: {str(e)}')
            continue

    if imported == 0:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'No valid products to import. ' + '; '.join(errors)}), 400

    db.session.commit()
    return jsonify({
        'success': True,
        'count': imported,
        'errors': errors
    })

    
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