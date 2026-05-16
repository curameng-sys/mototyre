import pymysql

conn = pymysql.connect(host='localhost', user='root', password='', database='mototyre')
cursor = conn.cursor()

# Add new columns if they don't exist
for col, definition in [('address', 'VARCHAR(255)'), ('motorcycle_model', 'VARCHAR(100)')]:
    try:
        cursor.execute(f"ALTER TABLE `user` ADD COLUMN {col} {definition}")
        print(f"{col} column added.")
    except Exception as e:
        print(f"{col}: {e}")

# Remove motorcycle_plate only
try:
    cursor.execute("ALTER TABLE `user` DROP COLUMN motorcycle_plate")
    print("motorcycle_plate column removed.")
except Exception as e:
    print(f"motorcycle_plate: {e}")

conn.commit()
conn.close()
print("Done.")
