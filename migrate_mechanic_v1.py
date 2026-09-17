"""Adds phone and a short profile note to the Mechanic table, for the
Mechanic Management data table's Phone column and the note shown under a
mechanic's name."""

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

run("ALTER TABLE mechanic ADD COLUMN phone VARCHAR(20) NULL", "phone")
run("ALTER TABLE mechanic ADD COLUMN note TEXT NULL", "note")

conn.commit()
conn.close()
print("Done.")
