import pymysql

conn = pymysql.connect(host='localhost', user='root', password='', database='mototyre')
cursor = conn.cursor()

for col, definition in [
    ('account_status', "VARCHAR(20) DEFAULT 'active'"),
    ('is_flagged', 'BOOLEAN DEFAULT FALSE'),
]:
    try:
        cursor.execute(f"ALTER TABLE `user` ADD COLUMN {col} {definition}")
        print(f"{col} column added.")
    except Exception as e:
        print(f"{col}: {e}")

# Backfill existing rows so NULL doesn't get treated as an unknown status.
cursor.execute("UPDATE `user` SET account_status = 'active' WHERE account_status IS NULL")
cursor.execute("UPDATE `user` SET is_flagged = FALSE WHERE is_flagged IS NULL")

conn.commit()
conn.close()
print("Done.")
