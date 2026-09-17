"""Service duration + time-slot scheduling helpers.

Shared by app.py (customer booking flow) and admin_app.py (service management,
migration seed data, admin-side finish-time display) so a job's estimated length
and every finish time computed from it agree everywhere — this module is the only
place that turns a (start, duration) pair into a finish time.

The shop is modeled as a single queue (one bay/mechanic-agnostic capacity) — the
existing calendar/booked-slots system already worked this way (one booking blocks
a time slot for everyone), we're just making the block duration-aware instead of a
fixed one-hour box. "Preferred mechanic" stays a preference, not a separate lane.
"""

from datetime import datetime, timedelta, time as _time

# ── Shop hours ────────────────────────────────────────────────────────────────
SHOP_OPEN_MIN  = 8 * 60         # 8:00 AM
SHOP_CLOSE_MIN = 18 * 60 + 30   # 6:30 PM — every job must FINISH by this, not just start.
                                 # A 40-minute job can't take the 6:00 PM slot (would end 6:40).
SLOT_GRANULARITY_MIN = 60  # candidate start times, on the hour — 8,9,10,11 AM, 1-6 PM once the break is excluded

# Mechanics' lunch break — no job may START in this window, and a job that starts
# before it and would still be running at BREAK_START_MIN pauses there and resumes
# at BREAK_END_MIN, landing BREAK_DURATION_MIN later than raw start+duration math.
BREAK_START_MIN    = 12 * 60        # 12:00 PM
BREAK_END_MIN       = 12 * 60 + 30  # 12:30 PM
BREAK_DURATION_MIN = BREAK_END_MIN - BREAK_START_MIN  # 30

DEFAULT_DURATION_MIN = 60  # fallback for legacy rows / unmatched service names
SERVICE_SEPARATOR = ', '

# Duration used to block the shop's queue for a multi-day job's drop-off/intake —
# the actual repair happens over several days in the back, it doesn't occupy the
# front queue for that whole time.
MULTIDAY_INTAKE_MIN = 30
MULTIDAY_MIN_DAYS = 3   # both multi-day services currently share this range —
MULTIDAY_MAX_DAYS = 5   # if a future one needs a different range, move these onto the Service row
MULTIDAY_LABEL = f'{MULTIDAY_MIN_DAYS}–{MULTIDAY_MAX_DAYS} working days'

# The six mechanic specializations, matched against the service catalogue.
# The overlaps are deliberate — coverage is scored, not exclusive (e.g. FI
# Cleaning sits under both General Service and Electronics & Diagnostics).
# This is the single source both the admin specialization dropdown and the
# customer booking flow's "best mechanic for this job" recommendation read —
# changing a mechanic's specialization changes what they're recommended for
# immediately, since neither side caches a copy of this mapping.
MECHANIC_SPECIALIZATIONS = [
    'General Service', 'Engine Overhaul', 'Electronics & Diagnostics',
    'Electrical', 'Brakes', 'Suspension & Steering',
]
SPECIALIZATION_COVERAGE = {
    'General Service': ['Change Oil', 'Full Maintenance Package', 'CVT Upgrade', 'CVT Cleaning', 'FI Cleaning'],
    'Engine Overhaul': ['Overhaul', 'Top Overhaul', 'Tune-Up', 'Throttle Body Cleaning'],
    'Electronics & Diagnostics': ['Diagnostic (API Tech / MST)', 'Remap', 'FI Cleaning', 'Throttle Body Cleaning'],
    'Electrical': ['General Rewiring', 'Horn Installation', 'Diagnostic (API Tech / MST)'],
    'Brakes': ['Brake Cleaning', 'Change Brake Pad'],
    'Suspension & Steering': ['Suspension Tuning', 'Ball Race Installation', 'Rubber Link Stopper'],
}


def ph_now():
    return datetime.utcnow() + timedelta(hours=8)


# ── Seed data: (name, duration_minutes, is_multiday, duration_label) ──────────
# Matched onto the shop's EXISTING Service catalog by closest name — see
# migrate_service_duration.py for exactly which DB row each maps to. Existing
# names/prices are left untouched; only duration fields are set/updated.
SEED_DURATIONS = [
    ('FI Cleaning',                    60,  False, None),
    ('Throttle Body Cleaning',         60,  False, None),
    ('Tune-Up',                        60,  False, None),
    ('Ball Race Installation',         30,  False, None),
    ('General Rewiring',               240, False, None),
    ('CVT Cleaning',                   20,  False, None),
    ('CVT Upgrade',                    20,  False, None),
    ('Diagnostic (API Tech / MST)',    10,  False, None),
    ('Suspension Tuning',              20,  False, None),
    ('Overhaul',                       MULTIDAY_INTAKE_MIN, True, MULTIDAY_LABEL),
    ('Top Overhaul',                   MULTIDAY_INTAKE_MIN, True, MULTIDAY_LABEL),
    ('Brake Cleaning',                 15,  False, None),
    ('Remap',                          30,  False, None),
    ('Change Oil',                     15,  False, None),
    ('Horn Installation',              90,  False, None),
    ('Rubber Link Stopper',            20,  False, None),
    ('Change Brake Pad',               15,  False, None),
    ('Full Maintenance Package',       60,  False, None),
]


def format_duration(minutes):
    """15 -> '15 min', 60 -> '1 hour', 90 -> '1.5 hours', 240 -> '4 hours'."""
    if not minutes or minutes <= 0:
        return ''
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} min"
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} hour" if h == 1 else f"{h} hours"
    hours_str = f"{minutes / 60:.1f}".rstrip('0').rstrip('.')
    return f"{hours_str} hours"


def minutes_to_ampm(m):
    """510 -> '8:30 AM'"""
    h, mm = divmod(int(m), 60)
    period = 'AM' if h < 12 else 'PM'
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d} {period}"


def hhmm_to_minutes(hhmm):
    """'08:30' -> 510"""
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def minutes_to_hhmm(m):
    """510 -> '08:30'"""
    h, mm = divmod(int(m), 60)
    return f"{h:02d}:{mm:02d}"


def combined_service_name(names):
    return SERVICE_SEPARATOR.join(names)


def split_service_names(combined):
    return [n.strip() for n in (combined or '').split(',') if n.strip()]


def combine_services(services):
    """services: list of Service rows (must have .name, .duration_minutes,
    .is_multiday, .duration_label). Returns a dict describing the combined job.

    If any selected service is multi-day (Full/Top Overhaul), the whole booking
    switches to the drop-off path: total_minutes is just the 30-minute intake —
    every other ticked service is folded in for free (the bike's already there
    for days, a quick extra job doesn't extend that), and the duration_label
    becomes the day-range instead of a clock-time estimate."""
    if not services:
        return {'total_minutes': 0, 'is_multiday': False, 'duration_label': '', 'names': [],
                'min_days': None, 'max_days': None}
    names = [s.name for s in services]
    multiday = next((s for s in services if s.is_multiday), None)
    if multiday:
        return {
            'total_minutes': multiday.duration_minutes or MULTIDAY_INTAKE_MIN,
            'is_multiday': True,
            'duration_label': multiday.duration_label or MULTIDAY_LABEL,
            'names': names,
            'min_days': MULTIDAY_MIN_DAYS,
            'max_days': MULTIDAY_MAX_DAYS,
        }
    total = sum((s.duration_minutes or DEFAULT_DURATION_MIN) for s in services)
    return {
        'total_minutes': total,
        'is_multiday': False,
        'duration_label': format_duration(total),
        'names': names,
        'min_days': None,
        'max_days': None,
    }


# ── Finish-time math — the one place a (start, duration) pair becomes a finish ─

def compute_finish_minutes(start_minutes, duration_minutes):
    """The actual finish minute-of-day for a job of this length starting at
    start_minutes, accounting for the mechanics' 12:00-12:30 break: a job that
    starts before the break and is still running at noon pauses there and
    resumes at 12:30, landing BREAK_DURATION_MIN later than start+duration would
    say. Every finish time in the app — customer-side slot generation, booking
    creation, and the admin-side display — must go through this function so the
    two can never disagree."""
    d = duration_minutes if duration_minutes and duration_minutes > 0 else DEFAULT_DURATION_MIN
    naive_end = int(start_minutes) + d
    if start_minutes < BREAK_START_MIN and naive_end > BREAK_START_MIN:
        return naive_end + BREAK_DURATION_MIN
    return naive_end


def compute_finish_time(start_time, duration_minutes):
    """start_time: a datetime.time. Returns the break-adjusted finish as a
    datetime.time — the time-object counterpart of compute_finish_minutes()."""
    start_minutes  = start_time.hour * 60 + start_time.minute
    finish_minutes = compute_finish_minutes(start_minutes, duration_minutes) % 1440
    h, m = divmod(finish_minutes, 60)
    return _time(h, m)


def real_end_minutes(start_minutes, duration_minutes, overrun_minutes=0):
    """A booking's actual end, including any counter-staff-recorded overrun on
    top of its scheduled duration — what the mechanic timeline draws bars
    against and what downstream clash/delay checks read, instead of the
    original estimate alone."""
    return compute_finish_minutes(start_minutes, duration_minutes) + (overrun_minutes or 0)


def all_slot_starts():
    """The fixed list of hourly start times the shop always offers as cards —
    independent of any particular job's duration: 8, 9, 10, 11 AM, 1, 2, 3, 4,
    5, 6 PM (the mechanics' 12:00-12:30 break excludes noon; nothing may start
    in that window). A slot is never silently left off this list — whether a
    given job actually fits it is a separate, per-job question answered by
    slot_statuses() below."""
    starts = []
    t = SHOP_OPEN_MIN
    while t < SHOP_CLOSE_MIN:
        if not (BREAK_START_MIN <= t < BREAK_END_MIN):
            starts.append(t)
        t += SLOT_GRANULARITY_MIN
    return starts


def _overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


# A mechanic needs a few minutes to wrap up one job and get set up for the
# next — so two of THEIR jobs must clear this much gap, on top of not
# literally overlapping. This does not apply to the shop's general queue slot
# (that's a different, mechanic-agnostic capacity question).
MECHANIC_TURNOVER_MIN = 15


def mechanic_overlaps(a_start, a_end, b_start, b_end, turnover=MECHANIC_TURNOVER_MIN):
    """True if a candidate job [a_start, a_end) is too close to an existing job
    [b_start, b_end) assigned to the SAME mechanic — i.e. they'd overlap once
    each is given `turnover` minutes of breathing room on both sides. Used
    everywhere a specific mechanic's availability is checked: the picker's
    busy flag, the "next in line" preview, and the final confirm-time check —
    so what the customer sees never disagrees with what actually gets enforced."""
    return a_start < (b_end + turnover) and (b_start - turnover) < a_end


def slot_statuses(duration_minutes, existing_intervals, now_minutes=None):
    """Every fixed slot from all_slot_starts(), always — each one marked
    available or not, with why. A card is never omitted just because it
    doesn't work for this job; the customer sees the reason instead.

    existing_intervals: iterable of (start_min, end_min) already-booked windows
    for the day — end_min should already be break-adjusted (see
    compute_finish_minutes) so a job that straddles the break correctly blocks
    the whole time it's actually paused/running. now_minutes: if given (booking
    for today), starts at/before now are marked 'past'.

    Returns a list of {'start', 'end', 'available', 'reason'} dicts, in order.
    reason is one of None (available), 'past', 'too_long', 'booked'."""
    d = duration_minutes if duration_minutes and duration_minutes > 0 else DEFAULT_DURATION_MIN
    result = []
    for start in all_slot_starts():
        end = compute_finish_minutes(start, d)
        if now_minutes is not None and start <= now_minutes:
            result.append({'start': start, 'end': end, 'available': False, 'reason': 'past'})
            continue
        if end > SHOP_CLOSE_MIN:
            result.append({'start': start, 'end': end, 'available': False, 'reason': 'too_long'})
            continue
        if any(_overlaps(start, end, bs, be) for bs, be in existing_intervals):
            result.append({'start': start, 'end': end, 'available': False, 'reason': 'booked'})
            continue
        result.append({'start': start, 'end': end, 'available': True, 'reason': None})
    return result


MAX_BOOKINGS_PER_DAY = 20  # shop policy cap — independent of whatever technically fits the hours


def validate_booking(start_minutes, duration_minutes, shop_intervals, daily_count,
                      require_slot_grid=True, mechanic_name=None, mechanic_status=None,
                      mechanic_intervals=None, daily_cap=MAX_BOOKINGS_PER_DAY):
    """THE routine every booking path calls before it's allowed to touch the
    schedule — customer self-booking, an admin reschedule, a mechanic
    reassignment, a walk-in. The admin UI pre-filtering what it shows is a
    convenience for a human, never the enforcement; whatever slips past that
    (a stale page, a second admin acting at the same moment, a direct request)
    still has to clear this. Returns (True, None) if the booking may proceed,
    or (False, reason) with a plain-English reason — never a silent fallback,
    never a different outcome than what was asked for.

    Enforces, in order:
      1. open 8:00 AM
      2. must land on the shop's fixed hourly grid — skip only for immediate,
         same-moment walk-in service, which isn't reserving a future slot
      3. hard 6:30 PM finish (the lunch pause is already baked into every
         finish time by compute_finish_minutes, not checked separately here)
      4. the shop's daily booking cap
      5. no collision with another booking already on the shop's queue that day
      6. if a specific mechanic is named: they must be on today's on-duty
         roster, and free for the whole window plus the 15-minute turnover
         gap on both sides against everything else already on their day."""
    end_minutes = compute_finish_minutes(start_minutes, duration_minutes)

    if require_slot_grid and start_minutes not in all_slot_starts():
        return False, 'That is not a valid time slot.'
    if start_minutes < SHOP_OPEN_MIN:
        return False, f'The shop opens at {minutes_to_ampm(SHOP_OPEN_MIN)}.'
    if end_minutes > SHOP_CLOSE_MIN:
        return False, (f'This job needs {format_duration(duration_minutes)} — that would run past '
                        f'closing ({minutes_to_ampm(SHOP_CLOSE_MIN)}). Please pick an earlier time.')
    if daily_count >= daily_cap:
        return False, f'This day already has the most bookings the shop takes ({daily_cap}). Please choose another date.'
    for b_start, b_end in shop_intervals:
        if start_minutes < b_end and b_start < end_minutes:
            return False, (f'That time slot is already taken '
                            f'(booked {minutes_to_ampm(b_start)}–{minutes_to_ampm(b_end)}).')
    if mechanic_name:
        if mechanic_status != 'available':
            return False, f'{mechanic_name} is not on duty for this booking.'
        for b_start, b_end in (mechanic_intervals or []):
            if mechanic_overlaps(start_minutes, end_minutes, b_start, b_end):
                return False, (f'{mechanic_name} is already booked between '
                                f'{minutes_to_ampm(b_start)} and {minutes_to_ampm(b_end)}.')
    return True, None


def get_ph_holidays(year):
    """Philippine regular + special non-working holidays. Mirrors
    customer_dashboard.html's getPHHolidays() exactly — the calendar that greys
    out dates and this server-side release-window math must never disagree
    about which days the shop is actually closed."""
    return {
        f'{year}-01-01': "New Year's Day",
        f'{year}-04-09': "Araw ng Kagitingan",
        f'{year}-05-01': "Labor Day",
        f'{year}-06-12': "Independence Day",
        f'{year}-08-25': "National Heroes Day",
        f'{year}-11-01': "All Saints' Day",
        f'{year}-11-30': "Bonifacio Day",
        f'{year}-12-25': "Christmas Day",
        f'{year}-12-30': "Rizal Day",
        f'{year}-02-25': "EDSA Revolution Anniversary",
        f'{year}-08-21': "Ninoy Aquino Day",
        f'{year}-11-02': "All Souls' Day",
        f'{year}-12-08': "Feast of the Immaculate Conception",
        f'{year}-12-24': "Christmas Eve",
        f'{year}-12-31': "New Year's Eve",
        f'{year}-04-17': "Maundy Thursday",
        f'{year}-04-18': "Good Friday",
        f'{year}-04-19': "Black Saturday",
        f'{year}-03-31': "Eid'l Fitr (approx)",
        f'{year}-06-07': "Eid'l Adha (approx)",
    }


def is_working_day(d):
    """A 'working day' is any day the shop is actually open — Sunday and PH
    holidays are out, same rule the customer calendar uses to grey out dates."""
    return d.weekday() != 6 and d.isoformat() not in get_ph_holidays(d.year)


def add_working_days(start_date, days):
    """Steps forward `days` working days from start_date — turns '3-5 working
    days' into real calendar dates for a multi-day job's release window."""
    d = start_date
    counted = 0
    while counted < days:
        d = d + timedelta(days=1)
        if is_working_day(d):
            counted += 1
    return d


def working_days_between(start_date, end_date):
    """How many working days have elapsed from start_date up to (and
    including) end_date — used for a multi-day job's 'day N of 3-5' progress."""
    if end_date <= start_date:
        return 0
    count = 0
    d = start_date
    while d < end_date:
        d = d + timedelta(days=1)
        if is_working_day(d):
            count += 1
    return count


def multiday_progress(dropoff_date, today, min_days=MULTIDAY_MIN_DAYS, max_days=MULTIDAY_MAX_DAYS):
    """Everything the 'In the bay' strip needs for one open drop-off: how many
    working days in, the min-max range, and the real release window computed
    from the actual drop-off date."""
    day_count = max(1, working_days_between(dropoff_date, today) + 1)
    release_from = add_working_days(dropoff_date, min_days)
    release_to = add_working_days(dropoff_date, max_days)
    return {
        'day_count': day_count,
        'min_days': min_days,
        'max_days': max_days,
        'label': f'day {day_count} of {min_days}–{max_days}',
        'release_from': release_from,
        'release_to': release_to,
    }


def mechanic_origin_note(assigned_name, preferred_name):
    """Where an assigned mechanic came from, in the customer's own words — used
    on both the booking confirmation email (app.py) and the reassignment
    notification/email (admin_app.py) so the phrasing never drifts between them.
    Returns None if no one is assigned yet."""
    if not assigned_name:
        return None
    if preferred_name and assigned_name == preferred_name:
        return 'the mechanic you asked for'
    if not preferred_name:
        return 'assigned by the shop'
    return f'assigned by the shop — {preferred_name} was not free at this time'


# ── Capacity analysis ───────────────────────────────────────────────────────
# A deliberately separate, hypothetical calculation from the real single-queue
# booking engine above — it answers a business question ("what's actually
# limiting us"), not a scheduling one, so it's allowed to imagine mechanics
# working independent, parallel lanes even though real bookings never do.

def max_jobs_one_mechanic(shortest_duration_minutes):
    """Greedily packs one mechanic's whole day with back-to-back jobs of the
    shop's shortest service, honoring the same lunch pause and 15-minute
    turnover every real booking has to — the ceiling on what ONE mechanic
    could physically get through in a day if nothing else ever got in the way."""
    d = shortest_duration_minutes if shortest_duration_minutes and shortest_duration_minutes > 0 else DEFAULT_DURATION_MIN
    count = 0
    t = SHOP_OPEN_MIN
    while True:
        finish = compute_finish_minutes(t, d)
        if finish > SHOP_CLOSE_MIN:
            break
        count += 1
        t = finish + MECHANIC_TURNOVER_MIN
    return count


def capacity_bottleneck(shortest_duration_minutes, mechanic_count, daily_cap):
    """Which lever is actually limiting the day: the staff on duty, or the
    booking cap policy. Ignores the cap while sizing what the staff could
    absorb, then compares that ceiling against the cap to say which one would
    actually move if raised — the number that answers "should I hire, or
    should I just raise the cap" for the owner."""
    per_mechanic = max_jobs_one_mechanic(shortest_duration_minutes)
    staff_capacity = per_mechanic * max(mechanic_count, 0)
    if staff_capacity > daily_cap:
        binding = 'cap'
        message = (f'Daily cap is binding — {mechanic_count} mechanic{"s" if mechanic_count != 1 else ""} could take '
                    f'about {staff_capacity} jobs, but the cap stops you at {daily_cap}. Raising staff will not add bookings.')
    else:
        binding = 'staff'
        message = (f'Mechanics on duty are binding — the cap allows up to {daily_cap} but '
                    f'{mechanic_count} mechanic{"s" if mechanic_count != 1 else ""} can only cover about {staff_capacity}. '
                    f'The day fills when the last mechanic runs out of time.')
    return {'staff_capacity': staff_capacity, 'daily_cap': daily_cap, 'binding': binding, 'message': message}
