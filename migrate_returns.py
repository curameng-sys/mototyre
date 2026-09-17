"""Creates the return_request table — the returns/warranty feature. A
customer claim against either an order (product arrived wrong) or a booking
(service didn't hold), tracked through submitted -> decided -> resolved."""

import pymysql

from db_conn import get_pymysql_connection
conn = get_pymysql_connection()
cursor = conn.cursor()

try:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS return_request (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            kind VARCHAR(10) NOT NULL,
            order_id INT NULL,
            order_item_id INT NULL,
            booking_id INT NULL,
            category VARCHAR(30) NOT NULL,
            description TEXT NOT NULL,
            desired_outcome VARCHAR(20) NOT NULL,
            photos VARCHAR(500),
            status VARCHAR(20) DEFAULT 'submitted',
            decision_reason TEXT,
            resolution VARCHAR(20),
            refund_amount FLOAT,
            created_at DATETIME,
            decided_at DATETIME,
            resolved_at DATETIME,
            FOREIGN KEY (user_id) REFERENCES user(id)
        )
    """)
    print("return_request table ready.")
except Exception as e:
    print(f"return_request: {e}")

conn.commit()
conn.close()
print("Done.")
