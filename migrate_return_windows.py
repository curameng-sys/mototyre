"""Adds the timestamps the return/warranty eligibility windows are measured
from: order.delivered_at (7-day return window) and booking.completed_at
(15-day warranty window). Backfills existing delivered/completed rows with
a best-guess timestamp so pre-existing data isn't stuck showing an unknown
window."""

import pymysql

from db_conn import get_pymysql_connection
conn = get_pymysql_connection()
cursor = conn.cursor()

try:
    cursor.execute("ALTER TABLE `order` ADD COLUMN delivered_at DATETIME NULL")
    print("order.delivered_at added.")
except Exception as e:
    print(f"order.delivered_at: {e}")

try:
    cursor.execute("ALTER TABLE booking ADD COLUMN completed_at DATETIME NULL")
    print("booking.completed_at added.")
except Exception as e:
    print(f"booking.completed_at: {e}")

try:
    cursor.execute("""
        UPDATE `order` SET delivered_at = created_at
        WHERE status IN ('delivered', 'completed') AND delivered_at IS NULL
    """)
    print(f"Backfilled delivered_at on {cursor.rowcount} order(s).")
except Exception as e:
    print(f"backfill order.delivered_at: {e}")

try:
    cursor.execute("""
        UPDATE booking SET completed_at = TIMESTAMP(date, time)
        WHERE status = 'completed' AND completed_at IS NULL
    """)
    print(f"Backfilled completed_at on {cursor.rowcount} booking(s).")
except Exception as e:
    print(f"backfill booking.completed_at: {e}")

conn.commit()
conn.close()
print("Done.")
