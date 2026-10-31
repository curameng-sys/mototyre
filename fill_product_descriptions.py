"""Give every product that has NO description a first draft built from its name, category and 'fits' column.

Safe by default: with no flags it only READS and prints  id | name -> draft description  (add --csv file.csv to save the
list). Nothing is written until you add --apply. Only products whose description is empty are touched; a description an
admin already wrote is never changed. Before saving it writes the touched ids to a JSON file so --undo can blank them again.

    set DATABASE_URL=...                         (the same value the Render service uses)
    python fill_product_descriptions.py --csv preview.csv      # preview
    python fill_product_descriptions.py --apply                # save
    python fill_product_descriptions.py --undo description_backup_YYYYMMDD_HHMMSS.json

The drafts only use what the name already says (brand, size, grade, volume) so nothing is invented. An admin can refine
any of them afterwards under Products > Edit.
"""
import csv
import json
import re
import sys
from datetime import datetime

TIRE_RE = re.compile(r'(\d{2,3})/(\d{2,3})\s*-\s*(\d{2})')
GRADE_RE = re.compile(r'\b(\d{1,2}W-?\d{2,3})\b', re.I)
VOLUME_RE = re.compile(r'\b(\d+(?:\.\d+)?)\s?(L|LT|LTR|ML)\b', re.I)

BLURB = {
    'TIRE': 'A motorcycle tire for everyday riding.',
    'BRAKE PARTS': 'A brake part for reliable stopping power.',
    'ENGINE PARTS': 'An engine part for your motorcycle.',
    'CHAIN & SPROCKETS': 'A drive part that carries power from the engine to the rear wheel.',
    'FILTERS': 'A filter that keeps dirt out of your engine.',
    'LUBRICANTS': 'A lubricant that protects moving engine parts.',
    'ACCESSORIES': 'An accessory for your motorcycle.',
    'OTHER SPARE PARTS': 'A spare part for your motorcycle.',
}


def draft(name, category, motor_model):
    cat = (category or '').strip().upper()
    lines = [BLURB.get(cat, f'{(category or "A motorcycle part").strip().capitalize()} for your motorcycle.')]
    specs = [('Product', name.strip()), ('Category', (category or '').strip().title())]
    t = TIRE_RE.search(name)
    if t:
        w, a, rim = t.groups()
        brand = re.split(r'\s+tire\b', name, flags=re.I)[0].strip()
        if brand and brand != name.strip():
            specs.insert(1, ('Brand', brand))
        specs += [('Size', f'{w}/{a}-{rim}'), ('Section width', f'{w} mm'), ('Aspect ratio', f'{a}%'), ('Rim size', f'{rim} inch')]
    g = GRADE_RE.search(name)
    if g:
        specs.append(('Viscosity grade', g.group(1).upper()))
    v = VOLUME_RE.search(name)
    if v:
        unit = 'ml' if v.group(2).lower() == 'ml' else 'L'
        specs.append(('Volume', f'{v.group(1)} {unit}'))
    if motor_model and motor_model.strip():
        specs.append(('Fits', motor_model.strip()))
    return '\n'.join(lines + ['Specifications:'] + [f'• {k}: {val}' for k, val in specs])[:1500]


def main():
    args = sys.argv[1:]
    from db_conn import get_pymysql_connection
    conn = get_pymysql_connection()
    cur = conn.cursor()

    if args and args[0] == '--undo':
        ids = json.load(open(args[1], encoding='utf8'))
        for pid in ids:
            cur.execute("UPDATE product SET description = NULL WHERE id = %s", (pid,))
        conn.commit()
        print(f'Blanked the description of {len(ids)} products from {args[1]}.')
        return

    cur.execute("SELECT id, name, category, motor_model FROM product WHERE description IS NULL OR TRIM(description) = '' ORDER BY id")
    todo = [(i, n, c, m, draft(n, c, m)) for i, n, c, m in cur.fetchall()]
    cur.execute("SELECT COUNT(*) FROM product")
    total = cur.fetchone()[0]
    for i, n, c, m, d in todo:
        print(f'{i} | {n}\n{d}\n')
    print(f'{len(todo)} of {total} products have no description.')
    if '--csv' in args:
        path = args[args.index('--csv') + 1]
        with open(path, 'w', newline='', encoding='utf8') as f:
            w = csv.writer(f); w.writerow(['id', 'name', 'category', 'fits', 'draft'])
            for row in todo: w.writerow(row)
        print('Saved', path)
    if '--apply' not in args:
        print('Preview only. Nothing was saved. Add --apply to save.')
        return
    backup = f'description_backup_{datetime.now():%Y%m%d_%H%M%S}.json'
    json.dump([r[0] for r in todo], open(backup, 'w', encoding='utf8'))
    for i, n, c, m, d in todo:
        cur.execute("UPDATE product SET description = %s WHERE id = %s AND (description IS NULL OR TRIM(description) = '')", (d, i))
    conn.commit()
    print(f'Saved {len(todo)} descriptions. To undo: python fill_product_descriptions.py --undo {backup}')


if __name__ == '__main__':
    main()
