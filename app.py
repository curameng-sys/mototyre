from flask import Flask, render_template, redirect, url_for, flash, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-dev-key')
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://root:@localhost/mototyre_db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    fullname = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='customer')
    motorcycle_plate = db.Column(db.String(20), nullable=True)
    motorcycle_model = db.Column(db.String(100), nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

with app.app_context():
    db.create_all()

@app.route('/')
def base():
    return render_template('base.html')

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
        flash('Invalid credentials', 'danger')
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
    return redirect(url_for('base'))

@app.route('/customer/dashboard')
@login_required
def customer_dashboard():
    return f"Welcome {current_user.fullname} (Customer Dashboard)"


@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    users = User.query.all()
    return render_template('admin_dashboard.html',
        total_bookings=0,      # replace with real DB queries later
        total_orders=0,
        total_revenue='0',
        total_users=len(users),
        users=users
    )
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

if __name__ == '__main__':
    app.run(debug=True)