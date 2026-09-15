"""Splits Booking's single mechanic_name/mechanic_specialization into two
independent pairs:
  - assigned_mechanic_name/specialization  — who is actually doing the work;
    the shop can change this freely (reassignment, reschedule, conflict fix).
  - preferred_mechanic_name/specialization — who the customer asked for, or
    NULL if they left it to the shop. Set once at booking creation and never
    touched again by any admin action.

Existing rows can't be told apart retroactively (we don't know whether the
current mechanic_name was the customer's own request or a shop assignment),
so as a one-time backfill this copies the existing value into BOTH new
columns — preserving today's data rather than discarding it. Going forward,
the app keeps them independent."""

import pymysql

conn = pymysql.connect(host='localhost', user='root', password='', database='mototyre')
cursor = conn.cursor()

# Rename the existing columns to assigned_*
for old_col, new_col, definition in [
    ('mechanic_name', 'assigned_mechanic_name', 'VARCHAR(100) NULL'),
    ('mechanic_specialization', 'assigned_mechanic_specialization', 'VARCHAR(100) NULL'),
]:
    try:
        cursor.execute(f"ALTER TABLE `booking` CHANGE `{old_col}` `{new_col}` {definition}")
        print(f"booking.{old_col} renamed to {new_col}.")
    except Exception as e:
        print(f"{old_col} -> {new_col}: {e}")

# Add the new preferred_* columns
for col, definition in [
    ('preferred_mechanic_name', 'VARCHAR(100) NULL'),
    ('preferred_mechanic_specialization', 'VARCHAR(100) NULL'),
]:
    try:
        cursor.execute(f"ALTER TABLE `booking` ADD COLUMN `{col}` {definition}")
        print(f"booking.{col} added.")
    except Exception as e:
        print(f"{col}: {e}")

# One-time backfill: existing assigned_mechanic_* becomes the initial
# preferred_mechanic_* too, so history isn't silently lost.
try:
    cursor.execute("""
        UPDATE `booking`
        SET preferred_mechanic_name = assigned_mechanic_name,
            preferred_mechanic_specialization = assigned_mechanic_specialization
        WHERE assigned_mechanic_name IS NOT NULL AND preferred_mechanic_name IS NULL
    """)
    print(f"Backfilled preferred_mechanic_* on {cursor.rowcount} existing booking(s).")
except Exception as e:
    print(f"backfill: {e}")

conn.commit()
conn.close()
print("Done.")
