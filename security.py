"""
security.py — Input validation & sanitization utilities for MotoTyre.

SQLAlchemy ORM already parameterizes queries, but these helpers defend
against bad data, unexpected crashes, and enum-field tampering.
"""

import re
from flask import abort

# ─── WHITELISTS ──────────────────────────────────────────────────────────────

ALLOWED_BOOKING_STATUSES = {'pending', 'confirmed', 'in_progress', 'inprogress', 'completed', 'cancelled'}
ALLOWED_ORDER_STATUSES   = {'pending', 'confirmed', 'processing', 'shipped', 'delivered', 'completed', 'cancelled'}
ALLOWED_OTP_PURPOSES     = {'login', 'verify', 'reset'}

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
