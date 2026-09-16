"""Creates the daily_capacity table — per-date overrides for the shop's
capacity controls (how many mechanics are rostered, and the daily booking
cap). A missing row for a date means "use the defaults"."""

import pymysql

conn = pymysql.connect(host='localhost', user='root', password='', database='mototyre')
cursor = conn.cursor()

try:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_capacity (
            id INT AUTO_INCREMENT PRIMARY KEY,
            date DATE NOT NULL UNIQUE,
            mechanic_count INT NULL,
            daily_cap INT NULL
        )
    """)
    print("daily_capacity table ready.")
except Exception as e:
    print(f"daily_capacity: {e}")

conn.commit()
conn.close()
print("Done.")
