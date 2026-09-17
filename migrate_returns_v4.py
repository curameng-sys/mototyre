"""Adds the shop-only internal notes box behind the admin review drawer —
never surfaced to the customer, separate from decision_reason which they do
see."""

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

run("ALTER TABLE return_request ADD COLUMN internal_notes TEXT", "internal_notes")

conn.commit()
conn.close()
print("Done.")
