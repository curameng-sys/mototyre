"""Where a shipped replacement part is actually allowed to go. Barangay 176 in
North Caloocan has no plain form — it's split into 176-A through 176-F — so
that's 6 entries, not 1, in the Caloocan list below."""

NORTH_CALOOCAN_BARANGAYS = [
    '165', '166', '167', '168', '169', '170', '171', '172', '173', '174', '175',
    '176-A', '176-B', '176-C', '176-D', '176-E', '176-F',
    '177', '178', '179', '180', '181', '182', '183', '184', '185', '186', '187', '188',
]  # 29

NORTHERN_QC_BARANGAYS = [
    # District 2
    'Bagong Silangan', 'Batasan Hills', 'Commonwealth', 'Holy Spirit', 'Payatas',
    # District 5
    'Bagbag', 'Capri', 'Fairview', 'Gulod', 'Greater Lagro', 'Kaligayahan',
    'Nagkaisang Nayon', 'North Fairview', 'Novaliches Proper', 'Pasong Putik Proper',
    'San Agustin', 'San Bartolome', 'Sta. Lucia', 'Sta. Monica',
    # District 6
    'Apolonio Samson', 'Baesa', 'Balon Bato', 'Culiat', 'New Era', 'Pasong Tamo',
    'Sangandaan', 'Sauyo', 'Talipapa', 'Tandang Sora', 'Unang Sigaw',
]  # 30

DELIVERY_ZONE_BARANGAYS = {
    'Caloocan City (North)': NORTH_CALOOCAN_BARANGAYS,
    'Quezon City': NORTHERN_QC_BARANGAYS,
}

DELIVERY_ZONE_CITIES = list(DELIVERY_ZONE_BARANGAYS.keys())


def is_in_delivery_zone(city, barangay):
    return barangay in DELIVERY_ZONE_BARANGAYS.get(city or '', [])


def format_delivery_address(name, mobile, street, barangay, city, zip_code):
    """One line, the way it reads in a status email/SMS: who, then where."""
    line2 = ', '.join(p for p in [street, f'Brgy. {barangay}' if barangay else '', city, zip_code] if p)
    return f'{name} ({mobile}) — {line2}' if name else line2


# ── Zip codes, display names and groups for the delivery form ──────────────────────────────────────
# Stored values stay the short keys above ('165', '176-A', 'Sauyo'); these are only for display.
BARANGAY_ZIPS = {
    'Caloocan City (North)': {
        '165': '1420', '166': '1420', '167': '1422', '168': '1420', '169': '1420', '170': '1420',
        '171': '1421', '172': '1422', '173': '1422', '174': '1422', '175': '1422',
        '176-A': '1428', '176-B': '1428', '176-C': '1428', '176-D': '1428', '176-E': '1428', '176-F': '1428',
        '177': '1422', '178': '1422', '179': '1425', '180': '1427', '181': '1427', '182': '1427', '183': '1427',
        '184': '1427', '185': '1427', '186': '1427', '187': '1427', '188': '1427',
    },
    'Quezon City': {
        # District 2
        'Bagong Silangan': '1119', 'Batasan Hills': '1126', 'Commonwealth': '1121', 'Holy Spirit': '1127', 'Payatas': '1119',
        # District 5
        'Bagbag': '1116', 'Capri': '1117', 'Fairview': '1118', 'Greater Lagro': '1118', 'Gulod': '1117',
        'Kaligayahan': '1124', 'Nagkaisang Nayon': '1125', 'North Fairview': '1121', 'Novaliches Proper': '1123',
        'Pasong Putik Proper': '1118', 'San Agustin': '1117', 'San Bartolome': '1116', 'Sta. Lucia': '1117', 'Sta. Monica': '1117',
        # District 6
        'Apolonio Samson': '1106', 'Baesa': '1106', 'Balon Bato': '1106', 'Culiat': '1128', 'New Era': '1107',
        'Pasong Tamo': '1107', 'Sangandaan': '1116', 'Sauyo': '1116', 'Talipapa': '1116', 'Tandang Sora': '1116', 'Unang Sigaw': '1106',
    },
}

CALOOCAN_BARANGAY_NAMES = {
    '165': 'Bagbaguin', '166': 'Kaybiga', '167': 'Llano', '168': 'Deparo', '169': 'BF Homes Caloocan', '170': 'Deparo 2',
    '171': 'Bagumbong', '172': 'Urduja Village', '173': 'Congress', '174': 'Camarin', '175': 'Camarin',
    '176-A': 'Bagong Silang', '176-B': 'Bagong Silang', '176-C': 'Bagong Silang', '176-D': 'Bagong Silang',
    '176-E': 'Bagong Silang', '176-F': 'Bagong Silang', '177': 'Camarin, Cielito', '178': 'Camarin, Kiko',
    '179': 'Amparo', '180': 'Tala', '181': 'Pangarap Village', '183': 'Tala',
}

# What the Barangay dropdown shows, grouped.
DELIVERY_ZONE_GROUPS = {
    'Caloocan City (North)': [('North Caloocan (Barangays 165–188)', NORTH_CALOOCAN_BARANGAYS)],
    'Quezon City': [
        ('District 2', ['Bagong Silangan', 'Batasan Hills', 'Commonwealth', 'Holy Spirit', 'Payatas']),
        ('District 5', ['Bagbag', 'Capri', 'Fairview', 'Greater Lagro', 'Gulod', 'Kaligayahan', 'Nagkaisang Nayon',
                        'North Fairview', 'Novaliches Proper', 'Pasong Putik Proper', 'San Agustin', 'San Bartolome',
                        'Sta. Lucia', 'Sta. Monica']),
        ('District 6', ['Apolonio Samson', 'Baesa', 'Balon Bato', 'Culiat', 'New Era', 'Pasong Tamo', 'Sangandaan',
                        'Sauyo', 'Talipapa', 'Tandang Sora', 'Unang Sigaw']),
    ],
}


def zip_for(city, barangay):
    return BARANGAY_ZIPS.get(city or '', {}).get(barangay or '', '')


def barangay_label(city, barangay):
    """'165' -> 'Barangay 165 (Bagbaguin)'; 'Sauyo' -> 'Barangay Sauyo'."""
    if city == 'Caloocan City (North)':
        name = CALOOCAN_BARANGAY_NAMES.get(barangay)
        return f'Barangay {barangay} ({name})' if name else f'Barangay {barangay}'
    return f'Barangay {barangay}'


def city_short(city):
    return 'Caloocan City' if city == 'Caloocan City (North)' else (city or '')


def full_address_line(street, barangay, city, zip_code=None):
    """'123 Rizal St., Barangay Sauyo, Quezon City, 1116'"""
    z = zip_code or zip_for(city, barangay)
    parts = [street, barangay_label(city, barangay), city_short(city)]
    return ', '.join(p for p in parts if p) + (f', {z}' if z else '')
