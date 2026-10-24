"""Split the motor model out of each product's name into its own `motor_model` column.

Safe by default: with no flags it only READS and prints a summary (add --csv file.csv to save the full before/after list).
Nothing is written until you add --apply. Before writing it saves the old name / motor_model of every row it will touch
to a JSON file, so --undo can put them back. Tires are never touched (a tire's size / pattern is not a motor model).
Rows needing a human decision are skipped unless you ask for them:  --include-flagged  (origin / front-rear wording).

    set DATABASE_URL=...
    python split_motor_models.py --csv preview.csv       # preview
    python split_motor_models.py --apply                # save
    python split_motor_models.py --undo motor_model_backup_YYYYMMDD_HHMMSS.json
"""
import csv
import re
import sys

# Words that mean "this segment names a motorcycle". Everything else (oil volumes, grades, part numbers, tire patterns,
# brand names) is deliberately NOT treated as a model.
MODEL_WORDS = ['BEAT', 'CLICK', 'NMAX', 'AEROX', 'M3', 'MIO', 'MXI', 'MSI', 'GT', 'SNIPER', 'SPORTY', 'PCX', 'ADV', 'XRM',
               'XMAX', 'FAZZIO', 'GENIO', 'BURGMAN', 'RAIDER', 'SHOGUN', 'FURY', 'R15', 'R150', 'TMX', 'SKYDRIVE', 'SOUL',
               'SOULTY', 'VEGA', 'SIGHT', 'SZ', 'FZ', 'AIRBLADE', 'AIR BLADE', 'GRAVIS', 'FREEGO', 'UNIVERSAL', 'NMAX2020',
               'CLICK125', 'CLICK150']
MODEL_RE = re.compile(r'(?<![A-Z0-9])(' + '|'.join(sorted((re.escape(w) for w in MODEL_WORDS), key=len, reverse=True)) + r')(?![A-Z0-9])', re.I)
# text that may follow a model keyword and still be part of the model ("CLICK 125 / 150", "BEAT FI V1/V2", "M3 NMAX")
# words allowed to sit next to a model keyword inside a model segment ("BEAT FI V1/V2", "CLICK 125 ( THAILAND)", "RAIDER FI- FRONT")
MODEL_EXTRA = {'FI', 'V1', 'V2', 'V3', 'I', 'CARB', 'THAILAND', 'INDONESIA', 'FRONT', 'REAR', 'MM', 'FEET', 'FT', 'GEAR',
               'SPORT', 'MIOI', 'MSI', 'YAMAHA', 'HONDA', 'AND', 'FI110', 'AIR', 'BLADE'}
_VOCAB = {w.replace(' ', '') for w in MODEL_WORDS}


def is_model_segment(seg):
    """True when the text is ONLY motorcycle-model words (plus versions, sizes, origin): 'CLICK 125 / 150', 'BEAT FI V1/V2'.
    'RUBBER HEAD GASKET CLICK 125' is not one: it has part words in front of the model."""
    toks = [t for t in re.split(r'[\s/\-&(),]+', (seg or '').upper()) if t]
    if not toks or not MODEL_RE.search((seg or '').upper()):
        return False
    return all(t in _VOCAB or t in MODEL_EXTRA or re.fullmatch(r'\d+[A-Z]?', t) for t in toks)
KEEP_UPPER = {'OEM', 'GPC', 'RCB', 'MTRT', 'HIRC', 'ZIC', 'TPS', 'ISC', 'LED', 'CP', 'CVT', 'MM', 'ML', 'FI', 'AT', 'II', 'KYB'}


def strip_extra(m):
    """Drop wording that is not a motorcycle: where it was made (THAILAND / INDONESIA), which side (FRONT / REAR) and sizes
    (55 MM). The original text stays in the product's description."""
    m = re.sub(r'\(\s*(THAILAND|INDONESIA)\s*\)', '', m, flags=re.I)
    m = re.sub(r'\s*-\s*(FRONT|REAR|\d+\s*MM)', '', m, flags=re.I)
    return m


def tidy_model(m):
    m = strip_extra(m)
    m = re.sub(r'\s+', ' ', m).strip(' ,-')
    m = re.sub(r'\bCLICK(\d)', r'CLICK \1', m, flags=re.I)
    return m.upper()


def _cap(token):
    """Capitalise the letters of a word, keeping punctuation: '(STREET' -> '(Street', 'O-RING' -> 'O-Ring'."""
    return re.sub(r'[A-Za-z]+', lambda m: m.group(0).capitalize(), token)


def title_name(parts):
    words = []
    for w in ' '.join(parts).replace(',', ' ').split():
        bare = re.sub(r'[^A-Za-z0-9]', '', w)
        if any(ch.isdigit() for ch in w):
            words.append(w)                                  # part numbers / sizes stay exactly as written
        elif bare.upper() in KEEP_UPPER:
            words.append(w.upper())
        else:
            words.append(_cap(w.lower()))
    return re.sub(r'\s+', ' ', ' '.join(words)).strip()


def split_one(raw, category):
    """-> (clean_name, motor_model, note)"""
    raw = (raw or '').strip()
    if (category or '').strip().upper() in ('TIRE', 'TIRES'):
        return None, '', 'tire (size/pattern is not a motor model)'
    segs = [s.strip() for s in raw.split(',')]
    segs = [s for s in segs]
    model, note = '', ''
    # 1) trailing whole segments that are motorcycle models ("... , M3 / GT"  or  "... , CLICK 125 / 150, ADV, PCX")
    tail = []
    i = len(segs)
    while i > 1 and (segs[i - 1] == '' or is_model_segment(segs[i - 1])):
        if segs[i - 1] != '':
            tail.insert(0, segs[i - 1])
        elif tail:
            break
        i -= 1
    if tail:
        # a segment with a model keyword AND unrelated words (sizes, origin, FRONT/REAR) is flagged for a human look
        model = ', '.join(tail)
        if re.search(r'\b(FEET|FT)\b', model, re.I):       # CARB is a model version, kept; origin / side / size are stripped
            note = 'check: model segment also has other words'
        kept = [s for s in segs[:i] if s != '']
        return title_name(kept), tidy_model(model), note
    # 2) the model rides at the end of the last segment ("HONDA, PISTON RING 13011-K36-T00 CLICK150", "KRYON TPS M3")
    last = segs[-1]
    m = MODEL_RE.search(last)
    if m:
        # take from the keyword to the end, as long as what follows still looks like model text
        tail_txt = last[m.start():]
        if is_model_segment(tail_txt):
            head = last[:m.start()].strip(' ,-')
            kept = [s for s in segs[:-1] if s != ''] + ([head] if head else [])
            return title_name(kept), tidy_model(tail_txt), 'model was inside the last words of the name'
    return title_name([s for s in segs if s != '']), '', ''


def main():
    from db_conn import get_pymysql_connection
    cur = get_pymysql_connection().cursor()
    cur.execute('SELECT id, name, category, description FROM product ORDER BY id')
    out = []
    for pid, name, cat, desc in cur.fetchall():
        raw = desc if (desc and ',' in desc) else name       # description keeps the original "BRAND, PART, CODE, MODEL" text
        clean, model, note = split_one(raw, cat)
        out.append((pid, name, cat, clean if clean is not None else name, model, note))
    if '--csv' in sys.argv:
        path = sys.argv[sys.argv.index('--csv') + 1]
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f); w.writerow(['id', 'current_name', 'category', 'new_name', 'motor_model', 'note']); w.writerows(out)
        print('saved', path)
    return out


MANUAL = {229, 266, 398}      # the parser cannot split these safely (model inside a part code / in brackets / odd wording)


def apply_changes(rows, include_flagged, only_ids=None):
    import json
    from datetime import datetime
    from db_conn import get_pymysql_connection
    conn = get_pymysql_connection()
    cur = conn.cursor()
    cur.execute("SHOW COLUMNS FROM product LIKE 'motor_model'")
    if not cur.fetchone():
        cur.execute('ALTER TABLE product ADD COLUMN motor_model VARCHAR(255) DEFAULT NULL')
        print('Added the product.motor_model column.')
    todo = []
    for pid, name, cat, new_name, model, note in rows:
        if only_ids is not None and pid not in only_ids:
            continue
        if pid in MANUAL or (note.startswith('check') and not include_flagged):
            continue
        if (cat or '').strip().upper() in ('TIRE', 'TIRES'):
            continue
        if new_name != name or model:
            todo.append((pid, name, new_name, model))
    ids = [t[0] for t in todo]
    cur.execute(f'SELECT id, name, motor_model FROM product WHERE id IN ({",".join(["%s"] * len(ids))})', ids)
    before = {r[0]: [r[1], r[2]] for r in cur.fetchall()}
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = f'motor_model_backup_{stamp}.json'
    json.dump({str(k): v for k, v in before.items()}, open(backup, 'w', encoding='utf8'), ensure_ascii=False, indent=1)
    for pid, _old, new_name, model in todo:
        cur.execute('UPDATE product SET name = %s, motor_model = %s WHERE id = %s', (new_name[:150], model or None, pid))
    conn.commit()
    print(f'Saved {len(todo)} products. Old values saved to {backup} (undo: python split_motor_models.py --undo {backup}).')


def undo(path):
    import json
    from db_conn import get_pymysql_connection
    conn = get_pymysql_connection()
    cur = conn.cursor()
    old = json.load(open(path, encoding='utf8'))
    for pid, (name, model) in old.items():
        cur.execute('UPDATE product SET name = %s, motor_model = %s WHERE id = %s', (name, model, int(pid)))
    conn.commit()
    print(f'Restored {len(old)} products from {path}.')


if __name__ == '__main__':
    if '--undo' in sys.argv:
        undo(sys.argv[sys.argv.index('--undo') + 1])
        sys.exit()
    rows = main()
    ch = [r for r in rows if r[4]]
    print(len(rows), 'products |', len(ch), 'get a motor model |', sum(1 for r in rows if r[5].startswith('check')), 'flagged')
    if '--apply' in sys.argv:
        only = {int(x) for x in sys.argv[sys.argv.index('--ids') + 1].split(',')} if '--ids' in sys.argv else None
        apply_changes(rows, '--include-flagged' in sys.argv, only)
    else:
        print('PREVIEW ONLY: nothing was written. Add --apply to save.')
