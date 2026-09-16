"""Adds what the admin Day panel needs on top of the existing booking model:
- booking.overrun_minutes: extra minutes counter staff records when a job runs
  long, on top of its scheduled duration.
- blocked_slot table: admin-blocked start times (staff meeting, parts
  delivery) that vanish from the customer booking flow immediately."""

import pymysql

conn = pymysql.connect(host='localhost', user='root', password='', database='mototyre')
cursor = conn.cursor()

try:
    cursor.execute("ALTER TABLE booking ADD COLUMN overrun_minutes INT DEFAULT 0")
    print("booking.overrun_minutes added.")
except Exception as e:
    print(f"booking.overrun_minutes: {e}")

try:
    cursor.execute("ALTER TABLE booking ADD COLUMN was_rescheduled BOOLEAN DEFAULT FALSE")
    print("booking.was_rescheduled added.")
except Exception as e:
    print(f"booking.was_rescheduled: {e}")

try:
    cursor.execute("ALTER TABLE notification ADD COLUMN priority BOOLEAN DEFAULT FALSE")
    print("notification.priority added.")
except Exception as e:
    print(f"notification.priority: {e}")

try:
    cursor.execute("ALTER TABLE notification ADD COLUMN booking_id INT NULL")
    print("notification.booking_id added.")
except Exception as e:
    print(f"notification.booking_id: {e}")

try:
    cursor.execute("ALTER TABLE booking ADD COLUMN day_before_reminder_sent BOOLEAN DEFAULT FALSE")
    print("booking.day_before_reminder_sent added.")
except Exception as e:
    print(f"booking.day_before_reminder_sent: {e}")

try:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS blocked_slot (
            id INT AUTO_INCREMENT PRIMARY KEY,
            date DATE NOT NULL,
            time TIME NOT NULL,
            reason VARCHAR(100),
            created_at DATETIME,
            UNIQUE KEY uq_blocked_slot (date, time)
        )
    """)
    print("blocked_slot table ready.")
except Exception as e:
    print(f"blocked_slot: {e}")

conn.commit()
conn.close()
print("Done.")
