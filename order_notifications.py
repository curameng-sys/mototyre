"""Customer-facing wording for order tracking updates.

Shared by app.py (customer site) and admin_app.py (admin/staff site) so a status
change reads the same no matter which app wrote it.

Ship-to-address (Courier) orders walk these tracking steps:

    confirmed -> in_progress -> done_packaging -> shipping -> completed   (or cancelled / refunded)

and the customer is notified at every one of them. Every message ends with the
exact date and time of the update, because the notification panel only shows a
short timestamp and the message itself is what the customer keeps.
"""

from datetime import datetime, timedelta

STAMP_FORMAT = '%b %d, %Y at %I:%M %p'

SHOP_NAME = 'MotoTyres North Caloocan'

# Shop Pickup: how long a ready order is held for the customer. After this it is cancelled automatically, the items
# go back to stock, and a GCash payment is refunded to the same GCash account.
PICKUP_HOLD_DAYS = 3


def pickup_hold_line(deadline_text, paid_with_gcash):
    """The sentence added to the 'ready for pickup' message, so the customer knows the last day up front."""
    refund = ' Your GCash payment will then be refunded to the same GCash account.' if paid_with_gcash else ''
    return (f' We hold it until {deadline_text}. Orders not picked up by then are cancelled automatically '
            f'and the items go back on the shelf.{refund}')

# Per-status label for the timestamp line, so the stamp reads naturally.
_STAMP_LABELS = {
    'shipped':   'Shipped on',
    'shipping':  'Shipped on',
    'refunded':  'Refunded on',
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
    """Ship to Address (Courier): confirmed -> in_progress -> done_packaging -> shipping -> completed,
    plus cancelled and refunded. The older keys stay so orders placed before this flow still read correctly."""
    dest    = shipping_destination(ship_address)
    to_dest = f' to {dest}' if dest else ''
    return {
        'confirmed': ship_notice('placed', ref)[:2],
        'in_progress': ship_notice('packing', ref)[:2],
        'done_packaging': ship_notice('packed', ref)[:2],
        'shipping': (
            'Order On The Way 🚚',
            f'Your order {ref} has been booked with the courier and is on its way{to_dest}.'),
        'completed': ship_notice('completed', ref)[:2],
        'cancelled': ship_notice('cancelled', ref, by='shop')[:2],
        'refunded': (
            'Order Refunded 💸',
            f'Your order {ref} was cancelled and your payment was refunded to your GCash.'),
        # orders placed before the Courier flow
        'pending': (
            'Order Placed 📝',
            f'Your order {ref} has been placed and is waiting for confirmation. '
            f'It will be shipped{to_dest} once confirmed.'),
        'awaiting_payment': (
            'Awaiting Payment 💳',
            f'Your order {ref} is waiting for payment. We will start preparing it '
            f'for delivery{to_dest} as soon as your payment clears.'),
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
            f'Your order {ref} has been picked up. Thank you!'),
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
    if is_ship:                      # the notification window shows "Updated: ..." itself
        return title, body
    return title, with_stamp(body, status, when, is_ship)


# ═══════════════ Ship to Address (Courier): the exact customer notifications ═══════════════
# One place for every title/message so the bell, the notification window and the email never disagree.
# The notification window itself shows "Updated: [date and time]", so these messages don't repeat it.

def _peso(n):
    return f'₱{float(n):,.2f}'


def _prices_line(prices):
    prices = prices or {}
    pool = _peso(prices['pooling']) if prices.get('pooling') else 'n/a'
    return f"Priority {_peso(prices.get('priority') or 0)} · Regular {_peso(prices.get('regular') or 0)} · Pooling {pool}"


def ship_notice(kind, ref, **c):
    """Returns (title, message, status_tag) for a Ship to Address event.
    kinds: placed packing packed options options_updated two_hours paid on_the_way
           received_check completed cancelled refunded"""
    if kind == 'placed':
        return ('Order Placed 🧾',
                f"Your order {ref} was placed. Nothing has been charged yet. We'll send your shipping options once your order is packed.", 'confirmed')
    if kind == 'packing':
        return ('Order Being Packed 📦',
                f"Your order {ref} is being packed. We'll send your shipping options once it's done.", 'in_progress')
    if kind == 'packed':
        return ('Order Packed ✅',
                f"{ref} is packed. We're checking courier prices now and will send your shipping options shortly.", 'done_packaging')
    if kind in ('options', 'options_updated'):
        title = 'Choose Your Shipping 🚚' if kind == 'options' else 'Shipping Options Updated 🚚'
        return (title,
                f'{ref} is packed and goes by Courier {c.get("vehicle", "")}. {_prices_line(c.get("prices"))}. '
                f'Choose one and pay by {c.get("deadline", "")}.', 'done_packaging')
    if kind == 'two_hours':
        return ('2 Hours Left to Pay ⏰',
                f'Choose your shipping and pay for {ref} by {c.get("deadline", "")}. If not, your order will be cancelled automatically.', 'done_packaging')
    if kind == 'paid':
        return ('Payment Received 💳',
                f"We got your {_peso(c.get('total', 0))} GCash payment for {ref}. We're booking your {c.get('option', '')} Courier {c.get('vehicle', '')} now.", 'paid')
    if kind == 'on_the_way':
        who = 'Driver' if c.get('rider_type') == 'driver' else 'Rider'
        model = c.get('model') or ''
        det = f'{model}, plate {c.get("plate", "")}' if model else f'plate {c.get("plate", "")}'
        track = ' Open your order to track the delivery.' if c.get('link', True) else ''
        return ('Order On The Way 🚚',
                f'{who} {c.get("name", "")} ({det}) is bringing {ref} by Courier {c.get("vehicle", "")}.{track}', 'shipping')
    if kind == 'received_check':
        return ('Did You Get Your Order? 📦',
                f"If {ref} has arrived, open My Orders and press I received my order. If it hasn't, please contact the shop.", 'shipping')
    if kind == 'completed':
        if c.get('by_shop'):
            return ('Order Completed 🎉',
                    f"We marked {ref} as completed because the courier shows it was delivered ({c.get('proof', '')}). "
                    f"Didn't get it? Contact the shop right away.", 'completed')
        return ('Order Completed 🎉', f'{ref} is completed. Thank you for shopping with MotoTyres!', 'completed')
    if kind == 'cancelled':
        by = c.get('by')
        if by == 'auto':
            when = c.get('deadline') or '24 hours after the shipping options were sent'
            msg = (f"We didn't get your payment by {when}, so your order was cancelled automatically. "
                   f"Nothing was charged. You're welcome to order again anytime.")
        elif by == 'customer':
            msg = f'You cancelled your order {ref}. Nothing was charged.'
        else:
            why = f' Reason: {c["reason"]}.' if c.get('reason') else ''
            msg = f'The shop cancelled your order {ref}.{why} Nothing was charged.'
        return ('Order Cancelled ❌', msg, 'cancelled')
    if kind == 'refunded':
        return ('Order Refunded 💸',
                f'The shop cancelled {ref}. Reason: {c.get("reason", "")}. We refunded {_peso(c.get("amount", 0))} to your GCash through PayMongo.', 'refunded')
    raise ValueError(kind)


def _lala_logo(base_url):
    """A small truck mark on the left of an email button."""
    return '<span style="margin-right:8px;">&#128666;</span>'


def ship_email_extra(kind, **c):
    """Extra HTML under the message in the email: the shipping options, the rider details, or the refund breakdown."""
    if kind == 'refunded':
        rows = [('Items', c.get('items_total', 0)), ('Shipping fee', c.get('shipping_fee', 0))]
        tr = ''.join(f'<tr><td style="padding:5px 8px;color:#6b7280;">{k}</td><td style="padding:5px 8px;text-align:right;">{_peso(v)}</td></tr>' for k, v in rows)
        return (f'<table style="width:100%;border-collapse:collapse;font-size:14px;margin:6px 0 10px;border:1px solid #eee;">{tr}'
                f'<tr><td style="padding:7px 8px;font-weight:700;border-top:1px solid #eee;">Back to your GCash</td>'
                f'<td style="padding:7px 8px;text-align:right;font-weight:700;border-top:1px solid #eee;">{_peso(c.get("amount", 0))}</td></tr></table>'
                f'<p style="margin:0;color:#6b7280;font-size:13px;">Sent through PayMongo. It may take a few days to show in your GCash.</p>')
    if kind == 'on_the_way':
        who = 'Driver' if c.get('rider_type') == 'driver' else 'Rider'
        car = 'Car model' if c.get('rider_type') == 'driver' else 'Motorcycle model'
        rows = [(who, c.get('name')), ('Mobile', c.get('phone')), (car, c.get('model')), ('Plate number', c.get('plate')),
                ('Delivery', f"Courier {c.get('vehicle', '')} · {c.get('option', '')} shipping")]
        tr = ''.join(f'<tr><td style="padding:5px 8px;color:#6b7280;">{k}</td><td style="padding:5px 8px;text-align:right;"><strong>{v}</strong></td></tr>'
                     for k, v in rows)
        return (f'<table style="width:100%;border-collapse:collapse;font-size:14px;margin:6px 0 12px;border:1px solid #eee;">{tr}</table>'
                f'<p style="margin:0 0 12px;"><a href="{c.get("link", "")}" target="_blank" style="display:inline-block;background:#d00019;color:#fff;text-decoration:none;'
                f'font-weight:700;padding:10px 18px;border-radius:8px;">{_lala_logo(c.get("base_url"))}Track delivery</a></p>'
                f'<p style="margin:0;color:#6b7280;font-size:13px;">Shipping is already paid. Nothing to pay the {who.lower()}.</p>')
    if kind not in ('options', 'options_updated'):
        return ''
    prices = c.get('prices') or {}
    vehicle, reason = c.get('vehicle', ''), c.get('reason') or ''
    if reason:
        why = (f'<p style="margin:0 0 10px;">Your order goes by Courier <strong>{vehicle}</strong>. Reason: {reason}. '
               f'A {vehicle} costs more than a motorcycle, so the shipping fee is higher.</p>')
    else:
        why = f'<p style="margin:0 0 10px;">Your order goes by Courier <strong>{vehicle}</strong>.</p>'
    rows = [('Priority', prices.get('priority'), 'Highest fee · fastest, goes straight to you'),
            ('Regular', prices.get('regular'), 'Standard delivery')]
    if prices.get('pooling'):
        rows.append(('Pooling', prices.get('pooling'), 'Lowest fee · may take longer, can carry other orders'))
    tr = ''.join(
        f'<tr><td style="padding:6px 8px;border-bottom:1px solid #eee;"><strong>{n}</strong><br>'
        f'<span style="color:#6b7280;font-size:12px;">{d}</span></td>'
        f'<td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:right;white-space:nowrap;"><strong>{_peso(v)}</strong></td></tr>'
        for n, v, d in rows)
    return (why +
            f'<table style="width:100%;border-collapse:collapse;font-size:14px;margin:6px 0 10px;">{tr}</table>'
            f'<p style="margin:0 0 6px;">Items total: <strong>{_peso(c.get("items_total", 0))}</strong> (the shipping fee you choose is added when you pay).</p>'
            f'<p style="margin:0 0 6px;">Pay by: <strong>{c.get("deadline", "")}</strong> (24 hours). '
            f"We'll remind you 2 hours before.</p>")


def build_order_email(name, body, extra_html='', when=None):
    """The shared email shell: greeting, the same message the bell shows, optional details, and the Updated line."""
    from html import escape
    msg = escape(body).replace('\n', '<br>')
    return (f'<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;color:#111827;">'
            f'<p>Hi {escape(name or "there")},</p><p>{msg}</p>{extra_html}'
            f'<p style="color:#6b7280;font-size:12px;margin-top:14px;">Updated: {format_stamp(when)}</p>'
            f'<p>— MotoTyres North Caloocan</p></div>')


def _first_name(full):
    return (full or 'there').strip().split(' ')[0] or 'there'


def ship_on_the_way_email(customer_name, ref, c):
    """('Your order ORD-036 is on the way', html) - the rider card, the paid note, the Track delivery button."""
    from html import escape
    rider = c.get('rider_type') == 'driver'
    who = 'Driver' if rider else 'Rider'
    model_lbl = 'Car' if rider else 'Motorcycle'
    rows = [(who, c.get('name')), ('Mobile', c.get('phone')), (model_lbl, c.get('model')), ('Plate no.', c.get('plate'))]
    tr = ''.join(f'<tr><td style="padding:8px 12px;color:#4b5563;">{k}</td>'
                 f'<td style="padding:8px 12px;text-align:right;font-weight:700;">{escape(str(v or ""))}</td></tr>' for k, v in rows)
    opt = f' ({c["option"]})' if c.get('option') else ''
    html = (f'<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;color:#111827;">'
            f'<p>Hi {escape(_first_name(customer_name))},</p>'
            f'<p>Your order is on the way with Courier {escape(c.get("vehicle", ""))}{escape(opt)}.</p>'
            f'<table style="width:100%;border-collapse:collapse;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:8px;font-size:14px;margin:8px 0 14px;">{tr}</table>'
            f"<p>Shipping is already paid, so there's nothing to pay the {who.lower()}.</p>"
            + (f'<p style="margin:0 0 14px;"><a href="{c.get("link", "")}" target="_blank" style="display:inline-block;background:#d00019;color:#fff;'
               f'text-decoration:none;font-weight:700;padding:11px 18px;border-radius:8px;">{_lala_logo(c.get("base_url"))}Track delivery &#8599;</a></p>' if c.get('link') else '') +
            f'<p>Once your order arrives, open My Orders and press <strong>I received my order</strong>.</p>'
            f'<p style="color:#6b7280;font-size:12px;">- MotoTyres North Caloocan</p></div>')
    return f'Your order {ref} is on the way', html


def ship_receipt_email(customer_name, ref, items, shipping_label, shipping_fee, total):
    """('Payment received for ORD-036', html): items, the shipping line and the total."""
    from html import escape
    tr = ''.join(f'<tr><td style="padding:7px 10px;">{escape(n)} &times;{q}</td><td style="padding:7px 10px;text-align:right;">{_peso(p)}</td></tr>'
                 for n, q, p in items)
    tr += f'<tr><td style="padding:7px 10px;">{escape(shipping_label)}</td><td style="padding:7px 10px;text-align:right;">{_peso(shipping_fee)}</td></tr>'
    tr += (f'<tr><td style="padding:9px 10px;font-weight:700;border-top:1px solid #d1d5db;">Total paid</td>'
           f'<td style="padding:9px 10px;text-align:right;font-weight:700;border-top:1px solid #d1d5db;">{_peso(total)}</td></tr>')
    html = (f'<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;color:#111827;">'
            f'<p>Hi {escape(_first_name(customer_name))},</p><p>We got your GCash payment. Here\'s your receipt.</p>'
            f'<table style="width:100%;border-collapse:collapse;background:#f3f4f6;border:1px solid #e5e7eb;font-size:14px;margin:8px 0 14px;">{tr}</table>'
            f'<p style="color:#6b7280;font-size:12px;">- MotoTyres North Caloocan</p></div>')
    return f'Payment received for {ref}', html


def booking_receipt_email(customer_name, inv_ref, ref, service, items, total, method, when, receipt_no):
    """('Payment received - Receipt INV-002', html): the receipt the customer also sees in the app."""
    from html import escape
    rows = ''.join(f'<tr><td style="padding:7px 10px;border-bottom:1px solid #eee;">{escape(str(n))}</td>'
                   f'<td style="padding:7px 10px;border-bottom:1px solid #eee;text-align:right;">{_peso(p)}</td></tr>' for n, p in items)
    how = 'GCash' if method == 'gcash' else 'cash'
    html = (f'<div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;color:#111827;">'
            f'<p>Hi {escape(_first_name(customer_name))},</p>'
            f'<p>You paid {_peso(total)} by {how} for {escape(service)} ({ref}). Thank you for choosing MotoTyres North Caloocan!</p>'
            f'<div style="text-align:center;margin:14px 0;"><span style="display:inline-block;background:#dcfce7;color:#166534;font-weight:700;'
            f'font-size:12px;padding:5px 14px;border-radius:99px;">Paid &middot; Completed</span></div>'
            f'<table style="width:100%;border-collapse:collapse;font-size:14px;"><tr><td style="padding:4px 10px;color:#6b7280;">Receipt for #{inv_ref}</td>'
            f'<td style="padding:4px 10px;text-align:right;color:#6b7280;">{format_stamp(when).split(" at ")[0]}</td></tr></table>'
            f'<table style="width:100%;border-collapse:collapse;font-size:14px;border:1px solid #eee;margin-top:6px;">{rows}'
            f'<tr><td style="padding:9px 10px;font-weight:700;">Amount paid</td>'
            f'<td style="padding:9px 10px;text-align:right;font-weight:700;color:#d00019;">{_peso(total)}</td></tr></table>'
            f'<p style="color:#6b7280;font-size:12px;margin-top:12px;">Receipt no. {receipt_no}</p>'
            f'<p style="color:#6b7280;font-size:12px;">- MotoTyres North Caloocan</p></div>')
    return f'Payment received — Receipt {inv_ref}', html
