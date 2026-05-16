import pymysql

conn = pymysql.connect(host='localhost', user='root', password='', database='mototyre')
cursor = conn.cursor()
cursor.execute("UPDATE booking SET status = 'confirmed' WHERE status = 'pending'")
print(f"{cursor.rowcount} booking(s) updated from pending to confirmed.")
conn.commit()
conn.close()
