"""Monthly Orders & Sales Report (PDF, landscape A4).

Only draws; the caller works the numbers out from the saved orders and passes plain data in, so this file never touches the
database. Shares its page set-up, fonts and footer with booking_report.py so both reports look the same.

data = {
  'month_label', 'range_label', 'generated', 'admin',
  'summary': {'total', 'online', 'instore', 'completed', 'cancelled', 'item_sales', 'item_sales_online', 'item_sales_instore',
              'shipping', 'deliveries'},
  'ship':    [{'ref','ordered','customer','address','items','delivered','rider','status','items_total','shipping','total'}],
  'pickup':  [{'ref','ordered','customer','items','picked_up','status','payment','items_total'}],
  'instore': [{'ref','when','customer','items','payment','amount','status'}],
  'ship_totals': {'items','shipping','total'}, 'pickup_total': float, 'instore_total': float,
  'by_type': [(label, orders, item_sales), ...], 'payments': [('GCash', orders, amt), ('Cash', orders, amt)], 'top_items': [(name, qty, amt), ...],
}
"""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from booking_report import DARK, GREEN, LIGHT, LINE, MARGIN, MUTED, PAGE, RED, WIDTH, _NumberedCanvas, _p, php

TH = ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=6.6, textColor=colors.white, leading=8)
DASH = '—'


def _status(r):
    done = r['status'] == 'Completed'
    return _p(f"<b>{escape(r['status'])}</b>", textColor=GREEN if done else MUTED), done


def _money(v, bold=False, done=True):
    if not done:
        return _p(DASH, alignment=2, textColor=MUTED)
    txt = php(v)
    return _p(f'<b>{txt}</b>' if bold else txt, alignment=2)


def _two(top, bottom):
    return _p(f"<b>{escape(top)}</b>" + (f"<br/><font color='#6b7280'>{escape(bottom)}</font>" if bottom else ''))


def _table(heads, rows, widths, total_row=None, total_span=0):
    body = [[Paragraph(h, TH) for h in heads]] + rows
    if total_row:
        body.append(total_row)
    t = Table(body, colWidths=widths, repeatRows=1)
    last = len(body) - 1
    st = [('BACKGROUND', (0, 0), (-1, 0), DARK), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
          ('GRID', (0, 0), (-1, -1), 0.4, LINE),
          ('ROWBACKGROUNDS', (0, 1), (-1, last - (1 if total_row else 0)), [colors.white, LIGHT]),
          ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
          ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5)]
    if total_row:
        st += [('SPAN', (0, last), (total_span, last)), ('BACKGROUND', (0, last), (-1, last), LIGHT),
               ('LINEABOVE', (0, last), (-1, last), 1.2, DARK)]
    t.setStyle(TableStyle(st))
    return t


def _fit(widths, flex_index):
    widths = list(widths)
    widths[flex_index] += WIDTH - sum(widths)
    return widths


def _part(title, empty_text, table):
    out = [_p(f'<b>{escape(title)}</b>', fontSize=9.5, leading=12), Spacer(1, 4)]
    out.append(table if table is not None else _p(empty_text, fontSize=7.5, textColor=MUTED))
    out.append(Spacer(1, 12))
    return out


def build_order_report_pdf(data):
    footer_left = f"MotoTyres North Caloocan  ·  Monthly Orders & Sales Report  ·  {data['month_label']}"
    canvas_cls = type('FooterCanvas', (_NumberedCanvas,), {'footer_left': footer_left})
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=26, bottomMargin=44,
                            title=f"Monthly Orders & Sales Report - {data['month_label']}", author='MotoTyres North Caloocan')
    story = []

    # ── heading ──
    left = [_p('MOTO<font color="#d0021b">TYRES</font>', fontName='Helvetica-Bold', fontSize=22, leading=24),
            _p('North Caloocan Branch', fontSize=8, textColor=MUTED)]
    right = [_p('MONTHLY ORDERS &amp; SALES REPORT', fontName='Helvetica-Bold', fontSize=13, leading=16, alignment=2),
             _p(escape(f"{data['month_label']} ({data['range_label']})"), fontSize=8.5, alignment=2, textColor=MUTED),
             _p(escape(f"Generated {data['generated']} by {data['admin']}"), fontSize=7.5, alignment=2, textColor=MUTED)]
    head = Table([[left, right]], colWidths=[WIDTH / 2, WIDTH / 2])
    head.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 5), HRFlowable(width=WIDTH, thickness=2, color=RED, spaceAfter=10)]

    # ── five summary boxes ──
    s = data['summary']

    def box(label, value, sub=None):
        cell = [_p(label, fontName='Helvetica-Bold', fontSize=6.5, textColor=MUTED),
                _p(value, fontName='Helvetica-Bold', fontSize=16, leading=20)]
        if sub:
            cell.append(_p(sub, fontSize=6.5, textColor=MUTED))
        return cell

    gap = 9
    bw = (WIDTH - 4 * gap) / 5
    boxes = Table([[box('TOTAL ORDERS', str(s['total']), f"Online {s['online']}  ·  In-store {s['instore']}"), '',
                    box('COMPLETED', str(s['completed'])), '',
                    box('CANCELLED', str(s['cancelled'])), '',
                    box('ITEM SALES', php(s['item_sales']),
                        f"Online {php(s['item_sales_online'])}  ·  In-store {php(s['item_sales_instore'])}"), '',
                    box('SHIPPING FEES', php(s['shipping']),
                        f"{s['deliveries']} courier deliver{'y' if s['deliveries'] == 1 else 'ies'}")]],
                  colWidths=[bw, gap, bw, gap, bw, gap, bw, gap, bw])
    bs = [('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
          ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 7)]
    for c in (0, 2, 4, 6, 8):
        bs.append(('BOX', (c, 0), (c, 0), 0.8, LINE))
    boxes.setStyle(TableStyle(bs))
    story += [boxes, Spacer(1, 14)]

    # ── Part 1: Ship to Address ──
    table = None
    if data['ship']:
        rows = []
        for r in data['ship']:
            st, done = _status(r)
            rows.append([
                _two(r['ref'], r['ordered']),
                _two(r['customer'], r['address']),
                _p(escape(r['items'])),
                _p(escape(r['delivered']) if done and r['delivered'] else DASH, textColor=DARK if done and r['delivered'] else MUTED),
                _p(escape(r['rider']).replace('\n', '<br/>') if done and r['rider'] else DASH, textColor=DARK if done and r['rider'] else MUTED),
                st, _money(r['items_total'], done=done), _money(r['shipping'], done=done), _money(r['total'], bold=True, done=done)])
        t = data['ship_totals']
        total_row = [_p('<b>TOTAL (COMPLETED ORDERS)</b>', alignment=2)] + [''] * 5 + \
                    [_money(t['items'], bold=True), _money(t['shipping'], bold=True), _p(f"<b>{php(t['total'])}</b>", alignment=2, textColor=RED)]
        table = _table(['ORDER #', 'CUSTOMER / DELIVERY ADDRESS', 'ITEMS', 'DELIVERED', 'COURIER RIDER', 'STATUS',
                        'ITEMS TOTAL', 'SHIPPING FEE', 'TOTAL PAID'],
                       rows, _fit([78, 150, 150, 72, 100, 54, 62, 58, 66], 2), total_row, 5)
    story += _part('Online Orders: Ship to Address', 'No Ship to Address orders this month.', table)

    # ── Part 2: Shop Pickup ──
    table = None
    if data['pickup']:
        rows = []
        for r in data['pickup']:
            st, done = _status(r)
            rows.append([_p(f"<b>{escape(r['ref'])}</b>"), _p(escape(r['ordered'])), _p(escape(r['customer'])), _p(escape(r['items'])),
                         _p(escape(r['picked_up']) if done and r['picked_up'] else DASH, textColor=DARK if done and r['picked_up'] else MUTED),
                         st, _p(escape(r['payment']) if done else DASH, textColor=DARK if done else MUTED),
                         _money(r['items_total'], bold=True, done=done)])
        total_row = [_p('<b>TOTAL (COMPLETED ORDERS)</b>', alignment=2)] + [''] * 6 + \
                    [_p(f"<b>{php(data['pickup_total'])}</b>", alignment=2, textColor=RED)]
        table = _table(['ORDER #', 'ORDERED', 'CUSTOMER', 'ITEMS', 'PICKED UP', 'STATUS', 'PAYMENT', 'ITEMS TOTAL'],
                       rows, _fit([62, 80, 120, 200, 80, 56, 52, 72], 3), total_row, 6)
    story += _part('Online Orders: Shop Pickup', 'No Shop Pickup orders this month.', table)

    # ── Part 3: In-Store Sales ──
    table = None
    if data['instore']:
        rows = []
        for r in data['instore']:
            st, done = _status(r)
            rows.append([_p(f"<b>{escape(r['ref'])}</b>"), _p(escape(r['when'])), _p(escape(r['customer'])), _p(escape(r['items'])),
                         st, _p(escape(r['payment']) if done else DASH, textColor=DARK if done else MUTED),
                         _money(r['amount'], bold=True, done=done)])
        total_row = [_p('<b>TOTAL (COMPLETED SALES)</b>', alignment=2)] + [''] * 5 + \
                    [_p(f"<b>{php(data['instore_total'])}</b>", alignment=2, textColor=RED)]
        table = _table(['ORDER #', 'DATE & TIME', 'CUSTOMER', 'ITEMS', 'STATUS', 'PAYMENT', 'AMOUNT'],
                       rows, _fit([62, 84, 140, 300, 56, 60, 80], 3), total_row, 5)
    story += _part('In-Store Sales', 'No in-store sales this month.', table)

    story += [_p('Online orders are paid through GCash. Shipping fees are what the customer paid for the courier delivery, '
                 'so they are not counted as item sales. Cancelled orders have no amount.', fontSize=6.8, textColor=MUTED),
              Spacer(1, 12)]

    # ── three small tables side by side ──
    def small(title, head_cols, rows, widths, right_cols=(), total_row=None):
        body = [[Paragraph(h, TH) for h in head_cols]]
        for r in rows:
            body.append([_p(escape(str(c)), alignment=2 if i in right_cols else 0) for i, c in enumerate(r)])
        if not rows:
            body.append([_p(DASH, textColor=MUTED)] + [''] * (len(head_cols) - 1))
        if total_row:
            body.append([_p(f'<b>{escape(str(c))}</b>', alignment=2 if i in right_cols else 0) for i, c in enumerate(total_row)])
        tb = Table(body, colWidths=widths)
        st = [('BACKGROUND', (0, 0), (-1, 0), DARK), ('GRID', (0, 0), (-1, -1), 0.4, LINE), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
              ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, LIGHT]),
              ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
              ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5)]
        if total_row:
            st += [('LINEABOVE', (0, -1), (-1, -1), 1, DARK), ('BACKGROUND', (0, -1), (-1, -1), LIGHT)]
        tb.setStyle(TableStyle(st))
        return [_p(f'<b>{title}</b>', fontSize=7.6), Spacer(1, 3), tb]

    g = 16
    w1 = w2 = 236
    w3 = WIDTH - w1 - w2 - 2 * g
    by_type = data['by_type']
    a = small('SALES BY TYPE', ['TYPE', 'ORDERS', 'ITEM SALES'], [(t, n, php(v)) for t, n, v in by_type],
              [w1 - 50 - 84, 50, 84], right_cols=(1, 2),
              total_row=('Total', sum(n for _t, n, _v in by_type), php(sum(v for _t, _n, v in by_type))))
    pays = data['payments']
    b = small('PAYMENTS RECEIVED (ITEMS + SHIPPING)', ['METHOD', 'ORDERS', 'AMOUNT'], [(m, n, php(v)) for m, n, v in pays],
              [w2 - 50 - 90, 50, 90], right_cols=(1, 2),
              total_row=('Total', sum(n for _m, n, _v in pays), php(sum(v for _m, _n, v in pays))))
    c = small('TOP-SELLING ITEMS', ['ITEM', 'QTY', 'AMOUNT'], [(n, q, php(v)) for n, q, v in data['top_items']],
              [w3 - 36 - 80, 36, 80], right_cols=(1, 2))
    trio = Table([[a, '', b, '', c]], colWidths=[w1, g, w2, g, w3])
    trio.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                              ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))

    sw = (WIDTH - 80) / 2
    sig = Table([['', '', ''], [_p(f"Prepared by: {escape(data['admin'])}", fontSize=8), '', _p('Noted by: Branch Owner', fontSize=8)]],
                colWidths=[sw, 80, sw], rowHeights=[28, None])
    sig.setStyle(TableStyle([('LINEABOVE', (0, 1), (0, 1), 0.8, DARK), ('LINEABOVE', (2, 1), (2, 1), 0.8, DARK),
                             ('LEFTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 1), (-1, 1), 3)]))
    story.append(KeepTogether([trio, Spacer(1, 6), sig]))

    doc.build(story, canvasmaker=canvas_cls)
    return buf.getvalue()
