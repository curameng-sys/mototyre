"""
security.py — Input validation & sanitization utilities for MotoTyre.

SQLAlchemy ORM already parameterizes queries, but these helpers defend
against bad data, unexpected crashes, and enum-field tampering.
"""

import re
from flask import abort

# ─── WHITELISTS ──────────────────────────────────────────────────────────────

ALLOWED_BOOKING_STATUSES = {'pending', 'confirmed', 'in_progress', 'inprogress', 'completed', 'cancelled', 'awaiting_payment', 'ready_for_pickup'}
ALLOWED_ORDER_STATUSES   = {'pending', 'confirmed', 'processing', 'shipped', 'delivered', 'completed', 'cancelled'}
ALLOWED_OTP_PURPOSES     = {'login', 'verify', 'reset'}

ALLOWED_RETURN_KINDS      = {'product', 'service'}
ALLOWED_RETURN_OUTCOMES   = {'refund', 'replacement', 'redo_service'}
# The rule the whole feature rests on: every claim forces ONE mutually
# exclusive choice — put it right, or give the money back, never both.
# Which "put it right" option applies depends on kind: a spare part gets a
# replacement shipped, a service gets redone (a "back job") at no charge.
RETURN_OUTCOMES_BY_KIND = {'product': {'replacement', 'refund'}, 'service': {'redo_service', 'refund'}}
ALLOWED_RETURN_STATUSES   = {'submitted', 'under_review', 'approved', 'denied', 'resolved',
                             'replacement_arrived', 'completed', 'cancelled'}
# Every open-claim status — the ones that block filing a second claim
# against the same order/booking until this one is resolved, denied, or
# cancelled.
OPEN_RETURN_STATUSES = {'submitted', 'under_review', 'approved'}

# Reason checklist for "Why" (section 3): (code, label, needs_photo). A
# reason needs a photo when it's something a photo could actually prove —
# damage, defects, wrong items, missing parts, leaks or noise; reasons with
# nothing to photograph (fit, change of mind, wrong service) don't ask for one.
RETURN_REASONS = {
    'product': [
        ('damaged', 'Arrived damaged or broken', True),
        ('defective', 'Defective, does not work properly', True),
        ('wrong_item', 'Wrong item sent', True),
        ('no_fit', 'Does not fit my motorcycle', False),
        ('missing_parts', 'Missing parts or accessories', True),
        ('not_as_described', 'Different from the description or photo', True),
        ('changed_mind', 'Changed my mind, part is unused and sealed', False),
        ('other', 'Other — none of these fit', False),
    ],
    'service': [
        ('recurred', 'The problem came back after the service', True),
        ('unfinished', 'The work was not finished', False),
        ('new_problem', 'A new problem started after the service', True),
        ('noise_leak', 'Noise, vibration or leak after the service', True),
        ('wrong_service', 'This is not the service I booked', False),
        ('wrong_parts', 'Parts used were not what we agreed on', True),
        ('other', 'Other — none of these fit', False),
    ],
}

# ─── STRING SANITIZATION ─────────────────────────────────────────────────────

def clean_str(value, max_len=255, default=''):
    """Strip whitespace and enforce max length on a string input."""
    if not isinstance(value, str):
        return default
    value = value.strip()
    return value[:max_len]


def clean_int(value, default=0, min_val=None, max_val=None):
    """Safely convert to int; return default on failure."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    if min_val is not None and result < min_val:
        return min_val
    if max_val is not None and result > max_val:
        return max_val
    return result


def clean_float(value, default=0.0, min_val=None, max_val=None):
    """Safely convert to float; return default on failure."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if min_val is not None and result < min_val:
        return min_val
    if max_val is not None and result > max_val:
        return max_val
    return result


# ─── VALIDATION ──────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

def is_valid_email(email):
    """Return True if email matches a basic RFC-safe pattern."""
    return bool(_EMAIL_RE.match(email)) if isinstance(email, str) else False


def is_valid_phone(phone):
    """Return True for 10–11 digit phone numbers."""
    return isinstance(phone, str) and phone.isdigit() and 10 <= len(phone) <= 11


def validate_booking_status(status):
    """
    Return the status if it's in the whitelist, else abort 400.
    Prevents arbitrary values from being written to the DB.
    """
    if status not in ALLOWED_BOOKING_STATUSES:
        abort(400, description=f"Invalid booking status: '{status}'")
    return status


def validate_order_status(status):
    """Return the status if it's in the whitelist, else abort 400."""
    if status not in ALLOWED_ORDER_STATUSES:
        abort(400, description=f"Invalid order status: '{status}'")
    return status


def validate_otp_purpose(purpose):
    """Return the purpose if allowed, else abort 400."""
    if purpose not in ALLOWED_OTP_PURPOSES:
        abort(400, description=f"Invalid OTP purpose: '{purpose}'")
    return purpose


def validate_return_kind(kind):
    if kind not in ALLOWED_RETURN_KINDS:
        abort(400, description=f"Invalid return kind: '{kind}'")
    return kind


def validate_return_outcome(outcome):
    if outcome not in ALLOWED_RETURN_OUTCOMES:
        abort(400, description=f"Invalid desired outcome: '{outcome}'")
    return outcome
