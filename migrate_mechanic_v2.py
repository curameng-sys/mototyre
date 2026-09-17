"""Adds the manual assignment fallback fields to Mechanic — shown in the
Working On columns only when there is no real booking, and always labelled
'entered manually' so they're never mistaken for schedule data."""

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

run("ALTER TABLE mechanic ADD COLUMN manual_customer VARCHAR(200) NULL", "manual_customer")
run("ALTER TABLE mechanic ADD COLUMN manual_service VARCHAR(300) NULL", "manual_service")

conn.commit()
conn.close()
print("Done.")
