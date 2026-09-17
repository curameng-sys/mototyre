"""Reworks return_request for the full claim flow (Screens 1 & 2):
- drops the old single order_item_id/category/description columns
- adds multi-select reasons + other_reason_text, requested_mechanic_name,
  requested_refund_amount, cancelled_at
- adds 'cancelled' as a real status this table can hold
- adds return_request_item for partial, per-line-item, per-quantity returns

This is dev data — no return requests exist yet worth preserving through a
column rename, so this drops and re-adds rather than migrating values."""

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

run("ALTER TABLE return_request DROP COLUMN order_item_id", "drop order_item_id")
run("ALTER TABLE return_request DROP COLUMN category", "drop category")
run("ALTER TABLE return_request DROP COLUMN description", "drop description")
run("ALTER TABLE return_request ADD COLUMN reasons VARCHAR(300)", "add reasons")
run("ALTER TABLE return_request ADD COLUMN other_reason_text TEXT", "add other_reason_text")
run("ALTER TABLE return_request ADD COLUMN requested_mechanic_name VARCHAR(100)", "add requested_mechanic_name")
run("ALTER TABLE return_request ADD COLUMN requested_refund_amount FLOAT", "add requested_refund_amount")
run("ALTER TABLE return_request ADD COLUMN cancelled_at DATETIME", "add cancelled_at")

run("""
    CREATE TABLE IF NOT EXISTS return_request_item (
        id INT AUTO_INCREMENT PRIMARY KEY,
        return_request_id INT NOT NULL,
        order_item_id INT NOT NULL,
        quantity INT NOT NULL,
        FOREIGN KEY (return_request_id) REFERENCES return_request(id)
    )
""", "create return_request_item")

conn.commit()
conn.close()
print("Done.")
