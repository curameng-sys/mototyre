from app import app, db
from sqlalchemy import text
with app.app_context():
    with db.engine.connect() as conn:
        conn.execute(text("ALTER TABLE booking ADD COLUMN payment_method VARCHAR(20) DEFAULT 'cash'"))
        conn.commit()
print('Done')