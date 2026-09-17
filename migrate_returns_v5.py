"""Back jobs become real bookings: links a return_request's back-job slot to
the actual, zero-charge Booking row written into the shop's own calendar."""

import pymysql

from db_conn import get_pymysql_connection
conn = get_pymysql_connection()
cursor = conn.cursor()

def run(sql, label):
    try:
        cursor.execute(sql)
        print(f"{label}: ok")
    except Exception as e:
        print(f"{label}: {e}")

run("ALTER TABLE return_request ADD COLUMN redo_booking_id INT NULL", "redo_booking_id")

conn.commit()
conn.close()
print("Done.")
