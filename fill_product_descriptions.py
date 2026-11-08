"""Give every product that has NO description a first draft built from its name, category and 'fits' column.

Safe by default: with no flags it only READS and prints  id | name -> draft description  (add --csv file.csv to save the
list). Nothing is written until you add --apply. Only products with NO real description are touched: empty, a few letters,
or one that only repeats the product name. A description an admin really wrote is never changed. Before saving it writes
each product's old text to a JSON file so --undo can put it back exactly.

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

TIRE_RE = re.compile(r'(\d{2,3})/(\d{2,3})\s*[-xX–]\s*(\d{2})')     # 80/90-17 or 90/90X14
GRADE_RE = re.compile(r'\b(\d{1,2}W-?\d{2,3})\b', re.I)
VOLUME_RE = re.compile(r'\b(\d+(?:\.\d+)?)\s?(L|LT|LTR|ML)\b', re.I)

# (words to look for in the category + name, what the part is, what it does). The first rule that matches is used.
TYPE_RULES = [
    (('AIR FILTER', 'AIR CLEANER'), 'Air filter', 'Keeps dust and dirt out of the engine so it breathes clean air.'),
    (('OIL FILTER',), 'Oil filter', 'Catches dirt and metal particles so the engine oil stays clean.'),
    (('SPARK PLUG',), 'Spark plug', 'Ignites the fuel and air mix inside the cylinder.'),
    (('BATTERY',), 'Battery', 'Powers the starter, lights and electrical parts.'),
    (('BRAKE PAD', 'DISC PAD'), 'Brake pad', 'Presses on the disc to stop the wheel.'),
    (('BRAKE SHOE',), 'Brake shoe', 'Presses on the drum to stop the wheel.'),
    (('BRAKE',), 'Brake part', 'Part of the braking system that helps the motorcycle stop.'),
    (('BALL RACE',), 'Ball race', 'Steering bearing set that lets the handlebar turn smoothly.'),
    (('BEARING',), 'Bearing', 'Lets a wheel or shaft spin smoothly with less friction.'),
    (('GASKET',), 'Gasket', 'Seals two engine surfaces together to prevent leaks.'),
    (('ROCKER',), 'Rocker arm', 'Part of the valve train that opens and closes the engine valves.'),
    (('AIR SCREW',), 'Air screw', 'Adjusting screw on the carburetor that sets the air and fuel mix.'),
    (('OIL SEAL', ' SEAL'), 'Oil seal', 'Keeps oil and grease from leaking out around a shaft or fork.'),
    (('PISTON RING', 'RING SET'), 'Piston ring', 'Seals the piston against the cylinder wall.'),
    (('FORK',), 'Front fork part', 'Part of the front suspension that absorbs bumps.'),
    (('SHOCK',), 'Shock absorber', 'Suspension part that absorbs bumps and keeps the ride smooth.'),
    (('CARBURETOR', 'CARB '), 'Carburetor part', 'Mixes air and fuel before it goes into the engine.'),
    (('THROTTLE',), 'Throttle part', 'Part of the throttle control that sets the engine speed.'),
    (('MUFFLER', 'EXHAUST'), 'Exhaust part', 'Carries exhaust gas away from the engine and reduces noise.'),
    (('STATOR', 'CDI', 'RECTIFIER', 'REGULATOR', 'IGNITION COIL'), 'Electrical part', 'Part of the charging or ignition system of the motorcycle.'),
    (('HORN',), 'Horn', 'Warns people around you.'),
    (('SIGNAL', 'BLINKER'), 'Turn signal', 'Shows other road users where you are turning.'),
    (('STARTER', 'KICK'), 'Starter part', 'Part used to start the engine.'),
    (('SIDE STAND', 'CENTER STAND', 'STAND'), 'Stand', 'Holds the motorcycle upright when parked.'),
    (('FOOTREST', 'FOOT STEP', 'FOOTPEG'), 'Footrest', 'Place for the rider or passenger to rest their feet.'),
    (('SEAT',), 'Seat', 'Seat for the rider or passenger.'),
    (('LEVER',), 'Lever', 'Hand or foot control lever.'),
    (('BUSHING', 'BUSH '), 'Bushing', 'Cushions and guides a moving part.'),
    (('STICKER', 'DECAL'), 'Sticker / decal', 'Body decoration.'),
    (('COVER', 'FAIRING'), 'Body cover', 'Protects or covers a part of the motorcycle body.'),
    (('BOLT', 'NUT', 'WASHER', 'SCREW'), 'Fastener', 'Holds motorcycle parts together.'),
    (('FUEL',), 'Fuel part', 'Part of the fuel system that carries or controls the fuel.'),
    (('VALVE',), 'Valve part', 'Part of the engine valve system.'),
    (('CHAIN', 'SPROCKET'), 'Drive part', 'Carries power from the engine to the rear wheel.'),
    (('CVT', 'PULLEY', 'ROLLER', 'DRIVE BELT'), 'CVT part', 'Part of the scooter transmission (CVT) that moves power to the rear wheel.'),
    (('CLUTCH',), 'Clutch part', 'Connects and disconnects engine power to the transmission.'),
    (('PISTON',), 'Piston part', 'Part of the piston and cylinder assembly of the engine.'),
    (('BULB', 'HEADLIGHT', 'LED'), 'Lighting', 'Gives light for the road and keeps you visible.'),
    (('WIRE', 'CABLE'), 'Wire / cable', 'Electrical wire or control cable for repairs and installation.'),
    (('MIRROR',), 'Mirror', 'Lets the rider see traffic behind.'),
    (('GRIP',), 'Handle grip', 'Rubber grip for a firm hold on the handlebar.'),
    (('YAMALUBE', 'ENGINE OIL', 'GEAR OIL', 'LUBRICANT', 'OIL'), 'Lubricant', 'Lubricates and protects moving engine parts.'),
    (('TIRE', 'TYRE'), 'Tire', 'A motorcycle tire for everyday riding.'),
    (('BACK PLATE',), 'Back plate', 'Replacement back plate. Check the "Fits" line before you buy.'),
]
KNOWN_BRANDS = ('Vee Rubber', 'Speed Motor', 'Honda', 'Yamaha', 'Suzuki', 'Kawasaki', 'Kymco', 'Michelin', 'Pirelli', 'Dunlop', 'Maxxis', 'Motolite',
                'NGK', 'Denso', 'Bosch', 'Kryon', 'GPC', 'Apido', 'Gateway', 'Yamalube', 'Motul', 'Castrol', 'Shell', 'Petron', 'Repsol', 'Silock')
PART_NO_RE = re.compile(r'\b(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,}\b', re.I)


def _type_of(text):
    """The (label, sentence) of the first kind of part whose words appear in the text as WHOLE words, or None. (So "EUROGRIP"
    is not a "GRIP" and "STANDARD" is not a "STAND".)"""
    hay = ' ' + re.sub(r'[^A-Z0-9]+', ' ', (text or '').upper()) + ' '
    for words, lab, sent in TYPE_RULES:
        for w in words:
            w = re.sub(r'[^A-Z0-9]+', ' ', w.upper()).strip()
            if w and re.search(r'(?<![A-Z0-9])' + re.escape(w) + r'(S|ES)?(?![A-Z0-9])', hay):
                return lab, sent
    return None


def draft(name, category, motor_model):
    label, sentence = None, None
    if TIRE_RE.search(name):
        label, sentence = 'Tire', 'A motorcycle tire for everyday riding.'
    else:
        # the name says what the part is more exactly than the category ("Brake pad" vs "Brakes"), so it is read first
        found = _type_of(name) or _type_of(category)
        if found:
            label, sentence = found
    if not label:
        label = (category or 'Spare part').strip().title()
        sentence = 'A spare part for your motorcycle.' if 'spare' in label.lower() or not category else f'{label} for your motorcycle.'
    specs = [('Product', name.strip()), ('Type', label), ('Category', (category or '').strip().title())]
    t = TIRE_RE.search(name)
    if t:
        w, a, rim = t.groups()
        parts = [x.strip() for x in name.split(',')] if ',' in name else [name]       # also "BRAND, TIRE, SIZE, MODEL"
        kept = [re.sub(r'\b(tire|tyre)\b', '', TIRE_RE.sub('', x), flags=re.I).strip(' -/') for x in parts]
        brand = ' '.join(x for x in kept if x)
        if brand.isupper():
            brand = brand.title()
        if brand and brand.lower() != name.strip().lower():
            specs.insert(1, ('Brand / model', brand))
        specs += [('Size', f'{w}/{a}-{rim} ({w} mm wide, {a}% aspect ratio, {rim}-inch rim)')]
    else:
        m = [x for x in PART_NO_RE.findall(name) if not GRADE_RE.fullmatch(x)]
        if m:
            specs.append(('Part no.', m[0].upper()))
    g = GRADE_RE.search(name)
    if g:
        specs.append(('Viscosity grade', g.group(1).upper()))
    v = VOLUME_RE.search(name)
    if v:
        unit = 'ml' if v.group(2).lower() == 'ml' else 'L'
        specs.append(('Volume', f'{v.group(1)} {unit}'))
    if not t:
        low = name.strip().lower()
        for b in sorted(KNOWN_BRANDS, key=len, reverse=True):
            if low.startswith(b.lower() + ' '):
                specs.insert(1, ('Brand', b))
                break
    if motor_model and motor_model.strip():
        specs.append(('Fits', motor_model.strip()))
    # Sentences, not a table that repeats the title: what the part is for, then what the name itself says about it (brand,
    # size, part number, grade, volume, the motorcycles it fits). The product name and category are not repeated.
    details = ' '.join(f'{k}: {val}.' for k, val in specs if k not in ('Product', 'Type', 'Category'))
    return (sentence + ('\n' + details if details else ''))[:1500]


def has_specific_type(name, category):
    """False when the product matched none of the part types above and so only got the general line."""
    return bool(TIRE_RE.search(name)) or bool(_type_of(name) or _type_of(category))


def just_the_name(name, description):
    """True when the description is empty, very short, or only repeats the product name (a title typed into both boxes).
    The customer pages use the same rule, so what is saved here is what they would have shown anyway."""
    norm = lambda x: re.sub(r'[^a-z0-9]+', '', (x or '').lower())
    own, nm = norm(description), norm(name)
    if len(own) < 10:
        return True
    return own == nm or own in nm or (nm in own and len(own) <= len(nm) + 12)


def main():
    args = sys.argv[1:]
    from db_conn import get_pymysql_connection
    conn = get_pymysql_connection()
    cur = conn.cursor()

    if args and args[0] == '--undo':
        saved = json.load(open(args[1], encoding='utf8'))
        # a backup of this version is {id: old text}; an older one is only a list of ids (those were empty before)
        old = saved if isinstance(saved, dict) else {str(i): None for i in saved}
        for pid, text in old.items():
            cur.execute("UPDATE product SET description = %s WHERE id = %s", (text, int(pid)))
        conn.commit()
        print(f'Put back the old description of {len(old)} products from {args[1]}.')
        return

    cur.execute("SELECT id, name, category, motor_model, description FROM product ORDER BY id")
    todo = [(i, n, c, m, draft(n, c, m), old) for i, n, c, m, old in cur.fetchall() if just_the_name(n, old)]
    cur.execute("SELECT COUNT(*) FROM product")
    total = cur.fetchone()[0]
    for i, n, c, m, d, old in todo:
        print(f'{i} | {n}\n{d}\n')
    print(f'{len(todo)} of {total} products have no real description (empty, very short, or only the name).')
    general = [(n, c) for i, n, c, m, d, old in todo if not has_specific_type(n, c)]
    print(f'{len(todo) - len(general)} got a line for their kind of part; {len(general)} only got the general line "A spare part for your motorcycle."')
    for cat in sorted({(c or '(no category)') for n, c in general}):
        print('   general line:', cat, '-', ', '.join(n for n, c in general if (c or '(no category)') == cat)[:120])
    if '--csv' in args:
        path = args[args.index('--csv') + 1]
        with open(path, 'w', newline='', encoding='utf8') as f:
            w = csv.writer(f); w.writerow(['id', 'name', 'category', 'fits', 'draft'])
            for row in todo: w.writerow(row[:5])
        print('Saved', path)
    if '--apply' not in args:
        print('Preview only. Nothing was saved. Add --apply to save.')
        return
    backup = f'description_backup_{datetime.now():%Y%m%d_%H%M%S}.json'
    json.dump({str(r[0]): r[5] for r in todo}, open(backup, 'w', encoding='utf8'))
    for i, n, c, m, d, old in todo:
        # only if it is still what we read a moment ago, so a description someone just wrote is never overwritten
        cur.execute("UPDATE product SET description = %s WHERE id = %s AND (description <=> %s)", (d, i, old))
    conn.commit()
    print(f'Saved {len(todo)} descriptions. To undo: python fill_product_descriptions.py --undo {backup}')


if __name__ == '__main__':
    main()
