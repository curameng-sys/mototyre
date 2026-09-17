"""Adds estimated-duration fields to `service` and `booking`, then seeds durations
onto the shop's EXISTING service rows (matched by closest name — see the mapping
comment in service_duration.SEED_DURATIONS). Existing names/prices are left as-is;
only the new duration columns are set."""

import pymysql
from service_duration import SEED_DURATIONS, DEFAULT_DURATION_MIN

from db_conn import get_pymysql_connection
conn = get_pymysql_connection()
cursor = conn.cursor()

for table, col, definition in [
    ('service', 'duration_minutes', f'INT DEFAULT {DEFAULT_DURATION_MIN}'),
    ('service', 'is_multiday',      'BOOLEAN DEFAULT FALSE'),
    ('service', 'duration_label',   'VARCHAR(30) NULL'),
    ('booking', 'duration_minutes', f'INT DEFAULT {DEFAULT_DURATION_MIN}'),
    ('booking', 'end_time',         'TIME NULL'),
    ('booking', 'is_multiday',      'BOOLEAN DEFAULT FALSE'),
]:
    try:
        cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN {col} {definition}")
        print(f"{table}.{col} added.")
    except Exception as e:
        print(f"{table}.{col}: {e}")

# service.name is unique but wasn't long enough for some names either — leave as is,
# these are short. booking.service needs more room for combined service names.
try:
    cursor.execute("ALTER TABLE `booking` MODIFY service VARCHAR(300) NOT NULL")
    print("booking.service widened to VARCHAR(300).")
except Exception as e:
    print(f"booking.service widen: {e}")

matched, missing = [], []
for name, minutes, is_multiday, label in SEED_DURATIONS:
    cursor.execute("SELECT id FROM service WHERE name = %s", (name,))
    row = cursor.fetchone()
    if row:
        cursor.execute(
            "UPDATE service SET duration_minutes = %s, is_multiday = %s, duration_label = %s WHERE id = %s",
            (minutes, is_multiday, label, row[0])
        )
        matched.append(name)
    else:
        missing.append(name)

conn.commit()
conn.close()

print(f"\nDuration set on {len(matched)} existing service(s): {', '.join(matched)}")
if missing:
    print(f"No matching service row found for: {', '.join(missing)} (skipped — add them manually in the admin panel if needed)")
print("Done.")
