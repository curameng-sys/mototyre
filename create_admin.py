"""Create (or reset) an admin account. Usage: python create_admin.py"""
from app import app, db
from app import User  # User model lives in app.py

ADMIN_EMAIL    = "mototyre0505@gmail.com"
ADMIN_PASSWORD = "Admin@123"
ADMIN_NAME     = "MotoTyre Admin"
ADMIN_PHONE    = "09000000000"

with app.app_context():
    db.create_all()
    user = User.query.filter_by(email=ADMIN_EMAIL).first()
    if user:
        user.role = "admin"
        user.set_password(ADMIN_PASSWORD)
        user.email_verified = True
        action = "updated"
    else:
        user = User(
            fullname=ADMIN_NAME,
            email=ADMIN_EMAIL,
            phone=ADMIN_PHONE,
            role="admin",
            email_verified=True,
        )
        user.set_password(ADMIN_PASSWORD)
        db.session.add(user)
        action = "created"
    db.session.commit()
    print(f"Admin {action}: {ADMIN_EMAIL} / {ADMIN_PASSWORD}")
