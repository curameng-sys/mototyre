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
    (('ENGINE OIL', 'GEAR OIL'), 'Lubricant', 'Lubricates and protects moving engine parts.'),
    (('FORK OIL',), 'Fork oil', 'Oil inside the front fork that smooths out bumps.'),
    (('TERMINAL',), 'Terminal', 'An electrical terminal that connects a wire.'),
    (('MASTER REPAIR KIT',), 'Master cylinder repair kit', 'Replacement seals and parts to repair the brake master cylinder.'),
    (('CYLINDER SET MASTER', 'MASTER CYLINDER'), 'Brake master cylinder', 'Pushes brake fluid to the caliper when you squeeze the brake lever.'),
    (('REPAIR KIT',), 'Repair kit', 'Replacement seals and small parts to repair the unit.'),
    (('O RING',), 'O-ring', 'A rubber ring that seals a joint so oil or fuel does not leak.'),
    (('CALIPER',), 'Brake caliper', 'Holds the brake pads and squeezes them against the disc.'),
    (('BRG', 'RADIAL BALL'), 'Bearing', 'Lets a wheel or shaft spin smoothly with less friction.'),
    (('BOSHING', 'BUSHING'), 'Bushing', 'Cushions and guides a moving part.'),
    (('FLY BALL',), 'Fly ball', 'CVT weight that moves outward as the engine speeds up.'),
    (('DRIVE FACE',), 'CVT drive face', 'Part of the front pulley of a scooter CVT.'),
    (('BELT',), 'Drive belt', 'Carries power between the pulleys of a scooter CVT.'),
    (('CONNECTING ROD',), 'Connecting rod', 'Connects the piston to the crankshaft.'),
    (('CRANK SHAFT', 'CRANKSHAFT'), 'Crankshaft', 'Turns the up-and-down motion of the pistons into rotation.'),
    (('CRANK CASE', 'CRANKCASE'), 'Crankcase part', 'Part of the engine crankcase.'),
    (('CYLINDER',), 'Cylinder part', 'Part of the cylinder where the piston moves.'),
    (('CAMSHAFT', 'CAMS'), 'Cam', 'Engine cam part that opens and closes the valves.'),
    (('INJECTOR',), 'Fuel injector', 'Sprays fuel into the engine.'),
    (('JETTING',), 'Carburetor jet', 'Controls how much fuel goes through the carburetor.'),
    (('TPS',), 'Throttle position sensor', 'Tells the engine computer how far the throttle is open.'),
    (('LIFTER', 'TENSIONER'), 'Cam chain tensioner', 'Keeps the timing chain at the right tension.'),
    (('CAM CHAIN',), 'Cam chain part', 'Part of the timing chain that drives the camshaft.'),
    (('CHAIN GUIDE',), 'Chain guide', 'Guides the chain so it stays in place.'),
    (('CARBON BRUSH',), 'Carbon brush', 'Carries electricity inside the starter motor and wears down with use.'),
    (('CIR CLIP', 'CIRCLIP'), 'Circlip', 'Keeps a part from sliding off its shaft or groove.'),
    (('COLLAR',), 'Collar', 'A spacer that sets the distance between two parts.'),
    (('CAP TANK', 'TANK CAP'), 'Fuel tank cap', 'Closes the fuel tank.'),
    (('DRAIN PLUG',), 'Drain plug', 'Closes the drain hole so oil does not leak out.'),
    (('HEAD LIGHT',), 'Lighting', 'Gives light for the road and keeps you visible.'),
    (('COOLANT',), 'Coolant', 'Helps keep the engine from overheating.'),
    (('DEGREASER', 'DE RUST', 'DISINFECTING', 'CLEANER'), 'Cleaning product', 'For cleaning and caring for motorcycle parts. Read the label before use.'),
    (('PUSH PIN',), 'Push pin', 'A fastener that holds covers in place.'),
    (('SPRING',), 'Spring', 'A spring that returns or holds a part in place.'),
    (('GEAR BOX',), 'Gear box part', 'Part of the transmission gears.'),
    (('CP HOLDER',), 'Phone holder', 'Holds a phone on the handlebar.'),
    (('MAGS', 'MAG WHEEL'), 'Mag wheel', 'A cast alloy wheel for the motorcycle.'),
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


# ── Different sentences for the same kind of part ────────────────────────────────────────────────────────────────────
# Every kind of part has a few ways to say what it is for, and a few short, true tips. Which one a product gets is picked from
# its name (always the same one for the same product), so two tires are not word for word the same, yet each still says what
# its own part does. The tips are general advice, never a specification.
VARIANTS = {
    'Tire': ['A motorcycle tire for everyday riding.', 'A tire that gives your motorcycle grip on the road.',
             'A replacement tire for your motorcycle, made for daily riding.', 'A road tire for motorcycles and scooters.',
             'Fresh rubber for better grip when your old tire is worn.'],
    'Air filter': ['Keeps dust and dirt out of the engine so it breathes clean air.', 'Cleans the air going into your engine.',
                   'Filters the air before it reaches the engine.', 'Helps your engine breathe clean air and run smoothly.'],
    'Oil filter': ['Catches dirt and metal particles so the engine oil stays clean.', 'Cleans the engine oil as it circulates.',
                   'Keeps harmful particles out of the engine oil.'],
    'Piston ring': ['Seals the piston against the cylinder wall.', 'Keeps compression in the cylinder and oil out of the combustion chamber.',
                    'Part of the piston set that seals the cylinder.'],
    'Brake pad': ['Presses on the disc to stop the wheel.', 'Grips the brake disc to slow down and stop your motorcycle.',
                  'The part that squeezes the disc when you brake.'],
    'Brake shoe': ['Presses on the drum to stop the wheel.', 'Grips the brake drum to slow down and stop your motorcycle.',
                   'The part that pushes against the drum when you brake.'],
    'Bearing': ['Lets a wheel or shaft spin smoothly with less friction.', 'Helps rotating parts turn smoothly and quietly.',
                'Reduces friction on a spinning shaft or wheel.'],
    'Ball race': ['Steering bearing set that lets the handlebar turn smoothly.', 'Lets the handlebar and front fork turn smoothly.',
                  'The bearings at the steering head of your motorcycle.'],
    'Drive belt': ['Carries power between the pulleys of a scooter CVT.', 'The drive belt of a scooter that moves power to the rear wheel.',
                   'Transfers power from the engine to the rear wheel in a CVT scooter.'],
    'Oil seal': ['Keeps oil and grease from leaking out around a shaft or fork.', 'A rubber seal that stops oil leaks around a shaft.',
                 'Prevents oil from escaping where a shaft passes through.'],
    'Gasket': ['Seals two engine surfaces together to prevent leaks.', 'Stops oil, gas or coolant from leaking between engine parts.',
               'A seal that sits between two engine parts.'],
    'Lubricant': ['Lubricates and protects moving engine parts.', 'Keeps engine parts running smoothly and reduces wear.',
                  'Oil that reduces friction inside the engine.'],
    'O-ring': ['A rubber ring that seals a joint so oil or fuel does not leak.', 'Seals a joint to stop leaks of oil, fuel or air.'],
    'Battery': ['Powers the starter, lights and electrical parts.', 'Stores the power your motorcycle needs to start and run its lights.'],
    'CVT part': ['Part of the scooter transmission (CVT) that moves power to the rear wheel.', 'Part of a scooter CVT that changes the drive ratio by itself.'],
    'Spark plug': ['Ignites the fuel and air mix inside the cylinder.', 'Creates the spark that starts each engine cycle.'],
    'Clutch part': ['Connects and disconnects engine power to the transmission.', 'Lets the engine power reach the wheel smoothly when you pull away.'],
    'Fastener': ['Holds motorcycle parts together.', 'Keeps parts of your motorcycle fastened in place.'],
}
TIPS = {
    'Tire': ['Check the size printed on the side of your current tire before ordering.', 'Check the tire pressure regularly so it lasts longer.',
             'Replace a tire that is cracked or worn down to the wear marks.'],
    'Air filter': ['Change it on the schedule in your motorcycle manual.', 'A clogged filter can make the engine feel weak, so replace it on time.',
                   'Change it more often if you ride on dusty roads.'],
    'Oil filter': ['Change it every time you change the engine oil, if your manual says so.'],
    'Piston ring': ['Replace the rings together as a set.', 'Best fitted by a mechanic after the cylinder is checked.'],
    'Brake pad': ['Replace the pads when they are worn thin.', 'Check the thickness of your pads regularly.'],
    'Brake shoe': ['Replace the shoes when they are worn thin.', 'Check the shoes regularly, especially before long rides.'],
    'Bearing': ['Replace a bearing that is noisy or rough when turned.', 'Have the mechanic fit it so it sits straight.'],
    'Ball race': ['Replace it when the steering feels notchy or loose.'],
    'Drive belt': ['Check it for cracks and wear on schedule.', 'Replace it when it shows cracks or has worn thin.'],
    'Oil seal': ['Replace it when you see oil leaking.', 'Fit a new seal whenever the part is taken apart.'],
    'Gasket': ['Use a new gasket every time the engine part is opened.', 'Clean both surfaces before fitting.'],
    'Lubricant': ['Use the grade your motorcycle manual recommends.', 'Change it on schedule for a smoother engine.', 'Check the oil level regularly.'],
    'O-ring': ['Replace O-rings whenever the part is opened.'],
    'Battery': ['Check that the size and terminals match your motorcycle.', 'Keep the terminals clean and tight.'],
}
GENERIC_TIPS = ['Ask the shop if you are not sure it fits your motorcycle.', 'Check that it matches the part you are replacing before ordering.',
                'Have a mechanic fit it if you are not sure how.']


def _pick(options, name, slot):
    """The same option every time for the same product name; different products spread over the options."""
    import hashlib
    h = hashlib.md5((name or '').strip().lower().encode('utf8')).hexdigest()
    return options[int(h[slot * 8:slot * 8 + 8], 16) % len(options)]


def draft(name, category, motor_model=None):
    """Two short sentences: what the part is for (one of a few ways to say it, from its kind: the name is read first, then the
    category), and a general tip. Nothing about the brand, size, part number or fit: those are in the product's own fields."""
    if TIRE_RE.search(name):
        label, base = 'Tire', 'A motorcycle tire for everyday riding.'
    else:
        found = _type_of(name) or _type_of(category)
        label, base = found if found else (None, 'A spare part for your motorcycle.')
    purpose = _pick(VARIANTS.get(label) or [base], name, 0)
    tip = _pick((TIPS.get(label) or []) + GENERIC_TIPS, name, 1)
    return purpose + ' ' + tip


def has_specific_type(name, category):
    """False when the product matched none of the part types above and so only got the general line."""
    return bool(TIRE_RE.search(name)) or bool(_type_of(name) or _type_of(category))


def just_the_name(name, description, category=None):
    """True when the description is not a real one: empty, very short, made only of the words of the name and category (in any order,
    with repeats, whatever the sizes look like: "BEAST TIRE, TIRE, 80/90-14", "KENDA, TIRE, 70/90X14, K488"), or an imported catalog
    line in capitals with commas ("HONDA, BELT, 23100-K35-V01, CLICK 125 ( THAILAND)"). A description with any other word in it,
    written the way a person writes, is a real one. The customer pages use the same rule."""
    def words(x):
        x = re.sub(r'(?<=\d)[xX](?=\d)', ' ', x or '')                 # 90X14 is 90 and 14, not a word "x"
        return set(re.findall(r'[a-z]+', x.lower()))
    d = (description or '').strip()
    if len(re.sub(r'[^a-z0-9]+', '', d.lower())) < 10:
        return True
    if not re.search(r'[a-z]{2,}', d) and d.count(',') >= 2:           # BRAND, TYPE, PART NO, FITS in capitals (a lone small letter, as in MIOi): an imported line
        return True
    return words(d) <= (words(name) | words(category))


def with_listed_fit(text, old, motor_model):
    """An imported catalog line may say a little more about the fit than the Fits column ("... (THAILAND)", "... - REAR"). Keep that."""
    d = (old or '').strip()
    if re.search(r'[a-z]{2,}', d) or d.count(',') < 2:
        return text
    last = d.split(',')[-1].strip()
    mm = (motor_model or '').strip().lower()
    if last and len(last) > 2 and last.lower() != mm and last.lower() not in mm and re.search(r'[A-Za-z]', last) and mm and mm in last.lower():
        return text + (' ' if '\n' in text else '\n') + f'Listed fit: {last}.'
    return text


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
    todo = [(i, n, c, m, draft(n, c, m), old) for i, n, c, m, old in cur.fetchall() if just_the_name(n, old, c)]
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
