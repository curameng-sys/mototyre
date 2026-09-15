"""Customer-facing wording for order tracking updates.

Shared by app.py (customer site) and admin_app.py (admin/staff site) so a status
change reads the same no matter which app wrote it.

Ship-to-address orders walk these tracking steps:

    pending -> confirmed -> processing -> shipped (on the way) -> delivered

and the customer is notified at every one of them. Every message ends with the
exact date and time of the update, because the notification panel only shows a
short timestamp and the message itself is what the customer keeps.
"""

from datetime import datetime, timedelta

STAMP_FORMAT = '%b %d, %Y at %I:%M %p'

SHOP_NAME = 'MotoTyre North Caloocan'

# Per-status label for the timestamp line, so the stamp reads naturally.
_STAMP_LABELS = {
    'shipped':   'Shipped on',
    'delivered': 'Delivered on',
    'completed': 'Completed on',
    'cancelled': 'Cancelled on',
}


def ph_now():
    """Philippine time (UTC+8) — matches the ph_now() in both apps."""
    return datetime.utcnow() + timedelta(hours=8)


def format_stamp(when=None):
    """'Sep 15, 2026 at 3:42 PM' — no leading zero on the hour."""
    return (when or ph_now()).strftime(STAMP_FORMAT).replace(' at 0', ' at ')


def order_ref(order_id):
    return f'ORD-{order_id:03d}'


def shipping_destination(ship_address):
    """Street line out of the stored address block (name / mobile / street...)."""
    lines = [ln.strip() for ln in (ship_address or '').split('\n') if ln.strip()]
    return lines[2] if len(lines) >= 3 else ''


def stamp_line(status, when=None, is_ship=True):
    """The 'Shipped on: Sep 15, 2026 at 3:42 PM' line appended to every message."""
    # 'shipped' means "ready for pickup" on a pick-up order, so it gets its own label.
    label = ('Ready on' if status == 'shipped' and not is_ship
             else _STAMP_LABELS.get(status, 'Updated'))
    return f'{label}: {format_stamp(when)}'


def with_stamp(message, status='update', when=None, is_ship=True):
    """Append the exact update time to an already-composed message."""
    return f'{message}\n\n{stamp_line(status, when, is_ship)}'


def _ship_messages(ref, ship_address):
    dest    = shipping_destination(ship_address)
    to_dest = f' to {dest}' if dest else ''
    return {
        'pending': (
            'Order Placed 📝',
            f'Your order {ref} has been placed and is waiting for confirmation. '
            f'It will be shipped{to_dest} once confirmed.'),
        'awaiting_payment': (
            'Awaiting Payment 💳',
            f'Your order {ref} is waiting for payment. We will start preparing it '
            f'for delivery{to_dest} as soon as your payment clears.'),
        'confirmed': (
            'Order Confirmed! ✅',
            f'Your order {ref} is confirmed. We are getting it ready to ship{to_dest}.'),
        'processing': (
            'Order Being Packed 📦',
            f'Your order {ref} is being packed and prepared for hand-over to the courier.'),
        'shipped': (
            'Your Order Is On The Way! 🚚',
            f'Your order {ref} has left {SHOP_NAME} and is on its way{to_dest}. '
            f'We will notify you again the moment it is delivered.'),
        'delivered': (
            'Order Delivered! 🎉',
            f'Your order {ref} has been delivered{to_dest}. Please check the items and '
            f'confirm receipt on your dashboard.'),
        'completed': (
            'Order Completed! 🎉',
            f'Your order {ref} is complete. Thank you for shopping with MotoTyre!'),
        'cancelled': (
            'Order Cancelled ❌',
            f'Your order {ref} has been cancelled and will no longer be shipped.'),
    }


def _pickup_messages(ref):
    return {
        'pending': (
            'Order Placed 📝',
            f'Your order {ref} has been placed and is waiting for confirmation.'),
        'awaiting_payment': (
            'Awaiting Payment 💳',
            f'Your order {ref} is waiting for payment.'),
        'confirmed': (
            'Order Confirmed!',
            f'Your order {ref} is confirmed and being prepared.'),
        'processing': (
            'Order Processing',
            f'Your order {ref} is being processed.'),
        'shipped': (
            'Order Ready for Pickup! 📦',
            f'Your order {ref} is ready for pickup at {SHOP_NAME}.'),
        'delivered': (
            'Order Delivered!',
            f'Your order {ref} has been delivered.'),
        'completed': (
            'Order Completed!',
            f'Your order {ref} has been completed. Thank you!'),
        'cancelled': (
            'Order Cancelled',
            f'Your order {ref} has been cancelled.'),
    }


def order_status_message(status, order_id, delivery_method='pickup', ship_address='', when=None):
    """(title, message) for a tracking step, or None when the status has no
    customer-facing update. The message always ends with the exact update time."""
    ref      = order_ref(order_id)
    is_ship  = (delivery_method or 'pickup') == 'ship'
    messages = _ship_messages(ref, ship_address) if is_ship else _pickup_messages(ref)
    if status not in messages:
        return None
    title, body = messages[status]
    return title, with_stamp(body, status, when, is_ship)
