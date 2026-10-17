"""Clears the shop's OLD bookings and orders (and what hangs off them) so numbering and lists start fresh.

KEEPS: users, mechanics, products (and their stock), services, settings (daily capacity, blocked slots),
       feedback, walk-in job orders and their payments.
CLEARS: bookings, orders + their items, return/warranty requests + their items, the quotations attached to
        bookings (and their items), and notifications.

Reads the same DATABASE_URL the apps use. Shows what it will remove, saves a backup file first, and asks you
to type CLEAN before it deletes anything. Everything is deleted in one transaction: if anything fails, nothing changes.

    python clean_old_data.py          # asks for confirmation
    python clean_old_data.py --yes    # no question (use only when you are sure which database it points at)
    python clean_old_data.py --all    # ALSO clears walk-in job orders + payments, all quotations, feedback, attendance
                                      # logs, login codes, per-day capacity overrides and blocked slots. Still keeps
                                      # users, mechanics, products and the service menu.
"""
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

load_dotenv()
URL = os.getenv('DATABASE_URL', 'mysql+pymysql://root:@localhost:3306/mototyre').strip()

# Child tables first, so nothing is left pointing at a deleted row.
# (table, optional WHERE to keep walk-in quotations etc.)
EXTRA_FOR_ALL = [
    ('payment', None),
    ('job_order_item', None),
    ('job_order', None),
    ('quotation_item', None),
    ('quotation', None),
    ('walkin_customer', None),
    ('mechanic_attendance', None),
    ('feedback', None),
    ('otp_record', None),
    ('daily_capacity', None),
    ('blocked_slot', None),
]

PLAN = [
    ('return_request_item', None),
    ('return_request', None),
    ('quotation_item', 'quotation_id IN (SELECT id FROM quotation WHERE booking_id IS NOT NULL)'),
    ('quotation', 'booking_id IS NOT NULL'),
    ('order_item', None),
    ('order', None),
    ('booking', None),
    ('notification', None),
]


def masked(url):
    return re.sub(r'//([^:/]+):[^@]*@', r'//\1:****@', url)


def jsonable(v):
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return f'<{len(v)} bytes>'
    return v


def main():
    engine = create_engine(URL, connect_args={'ssl': {}} if 'localhost' not in URL and 'sqlite' not in URL else {})
    q = engine.dialect.identifier_preparer.quote
    existing = set(inspect(engine).get_table_names())
    base = PLAN
    if '--all' in sys.argv:
        # the --all list replaces the booking-only quotation rules and adds the walk-in side
        extra_tables = {t for t, _ in EXTRA_FOR_ALL}
        base = [(t, w) for t, w in PLAN if t not in extra_tables] + EXTRA_FOR_ALL
        order = ['return_request_item', 'return_request', 'payment', 'job_order_item', 'job_order', 'quotation_item',
                 'quotation', 'order_item', 'order', 'booking', 'walkin_customer', 'mechanic_attendance', 'feedback',
                 'otp_record', 'daily_capacity', 'blocked_slot', 'notification']
        base = sorted(base, key=lambda tw: order.index(tw[0]))
    plan = [(t, w) for t, w in base if t in existing]

    print('Database:', masked(URL))
    with engine.connect() as conn:
        counts = {}
        for t, w in plan:
            sql = f'SELECT COUNT(*) FROM {q(t)}' + (f' WHERE {w}' if w else '')
            counts[t] = conn.execute(text(sql)).scalar()
        keep = ['user', 'mechanic', 'product', 'service']
        print('\nWill be REMOVED:')
        for t, n in counts.items():
            print(f'  {t:22s} {n}')
        print('\nWill be KEPT:')
        for t in keep:
            if t in existing:
                print(f'  {t:22s} {conn.execute(text(f"SELECT COUNT(*) FROM {q(t)}")).scalar()}')

        if not any(counts.values()):
            print('\nNothing to clean.')
            return

        if '--yes' not in sys.argv:
            if input('\nType CLEAN to delete these rows: ').strip() != 'CLEAN':
                print('Cancelled. Nothing was changed.')
                return

        # 1) backup, before touching anything
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup = os.path.join(os.path.dirname(os.path.abspath(__file__)), f'backup_before_clean_{stamp}.json')
        dump = {}
        for t, w in plan:
            rows = conn.execute(text(f'SELECT * FROM {q(t)}' + (f' WHERE {w}' if w else '')))
            dump[t] = [{k: jsonable(v) for k, v in r._mapping.items()} for r in rows]
        with open(backup, 'w', encoding='utf8') as f:
            json.dump(dump, f, ensure_ascii=False, indent=1)
        print('\nBackup saved:', backup)

    # 2) delete, all or nothing
    with engine.begin() as conn:
        for t, w in plan:
            conn.execute(text(f'DELETE FROM {q(t)}' + (f' WHERE {w}' if w else '')))
        if engine.dialect.name == 'mysql':             # numbering starts again at 1 (BKG-001, ORD-001, ...)
            for t in ('return_request', 'order', 'booking', 'notification', 'quotation', 'job_order', 'payment', 'walkin_customer', 'feedback', 'mechanic_attendance'):
                if t in existing:
                    left = conn.execute(text(f'SELECT COUNT(*) FROM {q(t)}')).scalar()
                    if left == 0:
                        conn.execute(text(f'ALTER TABLE {q(t)} AUTO_INCREMENT = 1'))
    print('Done. Old bookings and orders are cleared. Users, mechanics and products were not touched.')


if __name__ == '__main__':
    main()
