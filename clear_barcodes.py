"""Remove product barcodes so a product is just its plain name. Changes ONLY the `barcode` column (set to empty);
names, prices, stock, categories and photos are untouched.

Safe by default: with no flags it is a dry run (reads and prints, writes nothing). Before saving it writes the old
barcodes to a JSON file so the change can be undone.

    set DATABASE_URL=mysql+pymysql://user:pass@host:port/dbname
    python clear_barcodes.py --scope mtt              # preview: only the auto-generated MTT0000000### codes
    python clear_barcodes.py --scope all              # preview: every barcode, including real part numbers
    python clear_barcodes.py --scope mtt --apply      # save
    python clear_barcodes.py --undo barcode_backup_YYYYMMDD_HHMMSS.json
"""
import json
import re
import sys
from datetime import datetime

from db_conn import get_pymysql_connection

MTT = re.compile(r'MTT\d{10}')


def main():
    args = sys.argv[1:]
    conn = get_pymysql_connection()
    cur = conn.cursor()

    if args and args[0] == '--undo':
        old = {int(k): v for k, v in json.load(open(args[1], encoding='utf8')).items()}
        for pid, code in old.items():
            cur.execute('UPDATE product SET barcode = %s WHERE id = %s', (code, pid))
        conn.commit()
        print(f'Restored {len(old)} barcodes from {args[1]}.')
        return

    scope = args[args.index('--scope') + 1] if '--scope' in args else None
    if scope not in ('mtt', 'all'):
        print('Choose a scope: --scope mtt   (only the auto-generated MTT codes)   or   --scope all')
        return
    apply = '--apply' in args

    cur.execute("SELECT id, name, barcode FROM product WHERE barcode IS NOT NULL AND TRIM(barcode) <> '' ORDER BY id")
    rows = [r for r in cur.fetchall() if scope == 'all' or MTT.fullmatch(r[2] or '')]
    print(f'{len(rows)} product(s) would have their barcode removed (scope: {scope}).\n')
    print(f'{"id":>4} | {"name (kept as is)":<46} | barcode removed')
    print('-' * 90)
    for pid, name, code in rows:
        print(f'{pid:>4} | {name[:46]:<46} | {code}')

    if not apply:
        print('\nDRY RUN: nothing saved. Re-run with --apply to save.')
        return

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = f'barcode_backup_{stamp}.json'
    json.dump({str(r[0]): r[2] for r in rows}, open(backup, 'w', encoding='utf8'), ensure_ascii=False, indent=1)
    for pid, _name, _code in rows:
        cur.execute('UPDATE product SET barcode = NULL WHERE id = %s', (pid,))
    conn.commit()
    print(f'\nSaved. {len(rows)} barcodes removed. Old values saved to {backup} (undo: python clear_barcodes.py --undo {backup}).')


if __name__ == '__main__':
    main()
