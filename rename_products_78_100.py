"""Rename products #78-#100. Changes ONLY the `name` column; price, stock, category, photo, barcode are untouched.

Safe by default: running it with no flags is a dry run that connects, reads, and prints  id | current name -> new name
and writes nothing. Nothing is saved until you add --apply, and before saving it writes the old names to a JSON file
so the change can be undone with --undo <that file>.

    set DATABASE_URL=mysql+pymysql://user:pass@host:port/dbname        (the same value the Render service uses)
    python rename_products_78_100.py              # preview only
    python rename_products_78_100.py --apply      # save
    python rename_products_78_100.py --undo rename_backup_YYYYMMDD_HHMMSS.json
"""
import json
import sys
from datetime import datetime

from db_conn import get_pymysql_connection

NEW_NAMES = {
    78: 'Vee Rubber Tire 80/80-14 431',
    79: 'Vee Rubber Tire 90/80-14 431',
    80: 'Vee Rubber Tire 100/80-14 431',
    81: 'Vee Rubber Tire 110/80-14 431',
    82: 'Vee Rubber Tire 120/70-14 431',
    83: 'Vee Rubber Tire 130/70-13 431',
    84: 'Vee Rubber Tire 140/70-14',
    85: 'Vee Rubber Tire 100/80-14',
    86: 'Vee Rubber Tire 110/70-13 459',
    87: 'Vee Rubber Tire 130/70-13 459',
    88: 'Vee Rubber Tire 120/70-14 459',
    89: 'Vee Rubber Tire 140/70-14 281',
    90: 'Vee Rubber Tire 110/80-14',
    91: 'Vee Rubber Tire 130/70-13',
    92: 'Beast Tire 80/80-14',
    93: 'Beast Tire 90/80-14',
    94: 'Beast Tire 100/80-14',
    95: 'Beast Tire 140/70-14',
    96: 'Beast Tire 130/70-13',
    97: 'Beast Tire 110/70-13',
    98: 'Beast Tire 70/90-14',
    99: 'Beast Tire 120/70-14',
    100: 'Maxxis Tire 110/80-14 Victra S98',
}
NAME_MAX = 150   # Product.name is VARCHAR(150)


def main():
    args = sys.argv[1:]
    conn = get_pymysql_connection()
    cur = conn.cursor()

    if args and args[0] == '--undo':
        old = {int(k): v for k, v in json.load(open(args[1], encoding='utf8')).items()}
        for pid, name in old.items():
            cur.execute('UPDATE product SET name = %s WHERE id = %s', (name, pid))
        conn.commit()
        print(f'Restored {len(old)} names from {args[1]}.')
        return

    apply = '--apply' in args
    ids = sorted(NEW_NAMES)
    cur.execute(f'SELECT id, name FROM product WHERE id IN ({",".join(["%s"] * len(ids))})', ids)
    current = {row[0]: row[1] for row in cur.fetchall()}

    missing = [i for i in ids if i not in current]
    too_long = [i for i in ids if len(NEW_NAMES[i]) > NAME_MAX]
    print(f'{"id":>4} | {"current name":<46} -> new name')
    print('-' * 100)
    for pid in ids:
        old = current.get(pid, '(NOT FOUND)')
        mark = '' if old != NEW_NAMES[pid] else '   (already this name)'
        print(f'{pid:>4} | {old:<46} -> {NEW_NAMES[pid]}{mark}')
    if missing:
        print(f'\nNot found in this database: {missing}. Nothing was changed.')
        return
    if too_long:
        print(f'\nToo long for the name column: {too_long}. Nothing was changed.')
        return

    if not apply:
        print('\nDRY RUN: nothing saved. Re-run with --apply to save these names.')
        return

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = f'rename_backup_{stamp}.json'
    json.dump({str(k): v for k, v in current.items()}, open(backup, 'w', encoding='utf8'), ensure_ascii=False, indent=1)
    changed = 0
    for pid in ids:
        if current[pid] != NEW_NAMES[pid]:
            cur.execute('UPDATE product SET name = %s WHERE id = %s', (NEW_NAMES[pid], pid))
            changed += 1
    conn.commit()
    print(f'\nSaved. {changed} names changed. Old names saved to {backup} (undo: python rename_products_78_100.py --undo {backup}).')


if __name__ == '__main__':
    main()
