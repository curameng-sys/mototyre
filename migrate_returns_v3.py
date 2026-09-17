"""Adds the return/warranty sub-states behind Screen 3 and the admin queue:
needing more info from the customer, a product's item having to come back
before its remedy is carried out, and back-job scheduling — plus the
notification deep-link to a specific claim."""

import pymysql

conn = pymysql.connect(host='localhost', user='root', password='', database='mototyre')
cursor = conn.cursor()

def run(sql, label):
    try:
        cursor.execute(sql)
        print(f"{label}: ok")
    except Exception as e:
        print(f"{label}: {e}")

run("ALTER TABLE return_request ADD COLUMN awaiting_customer_info BOOLEAN DEFAULT FALSE", "awaiting_customer_info")
run("ALTER TABLE return_request ADD COLUMN info_request_note TEXT", "info_request_note")
run("ALTER TABLE return_request ADD COLUMN info_provided_at DATETIME", "info_provided_at")
run("ALTER TABLE return_request ADD COLUMN info_provided_text TEXT", "info_provided_text")
run("ALTER TABLE return_request ADD COLUMN item_returned BOOLEAN DEFAULT FALSE", "item_returned")
run("ALTER TABLE return_request ADD COLUMN item_returned_at DATETIME", "item_returned_at")
run("ALTER TABLE return_request ADD COLUMN redo_date DATE", "redo_date")
run("ALTER TABLE return_request ADD COLUMN redo_time TIME", "redo_time")
run("ALTER TABLE return_request ADD COLUMN redo_mechanic_name VARCHAR(100)", "redo_mechanic_name")
run("ALTER TABLE notification ADD COLUMN return_request_id INT NULL", "notification.return_request_id")

conn.commit()
conn.close()
print("Done.")
