from app import app, db
with app.app_context():
    with db.engine.connect() as conn:
        conn.execute(db.text("ALTER TABLE user ADD COLUMN email_verified BOOLEAN DEFAULT TRUE"))
        conn.commit()
    print("Migration done!")
    