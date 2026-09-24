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
