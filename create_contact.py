from app import app, db
from sqlalchemy import text

with app.app_context():
    with db.engine.connect() as conn:
        conn.execute(text('ALTER TABLE booking ADD COLUMN contact_name VARCHAR(100)'))
        conn.execute(text('ALTER TABLE booking ADD COLUMN contact_mobile VARCHAR(20)'))
        conn.execute(text('ALTER TABLE booking ADD COLUMN contact_email VARCHAR(100)'))
        conn.commit()
        print('Done!')