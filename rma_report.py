"""Monthly Returns & Warranty (RMA) Report (PDF, landscape A4).

Only draws; the caller works everything out from the saved claims and passes plain data, so this file never touches the
database. Same page set-up, fonts and footer as the booking and order reports.

data = {
  'month_label', 'range_label', 'generated', 'admin', 'partial_note' ('' for a finished month),
  'summary': {'claims', 'parts', 'services', 'replacements', 'replacement_value', 'back_jobs', 'refunds', 'refund_total',
              'refund_gcash', 'refund_cash', 'rejected'},
  'replacements': [{'ref','filed','customer','phone','item','origin','reason','decided','replaced','status','value'}],
  'back_jobs':    [{'ref','filed','customer','motorcycle','service','origin','original_mechanic','reason','back_job','back_job_date',
                    'done_by','status','charged'}],
  'refunds':      [{'ref','filed','customer','phone','item','origin','type','reason','method','released','status','amount'}],
  'rejected':     [{'ref','filed','customer','type','item','origin','asked','why','decided','status'}],
  'included': {'replacements': bool, 'back_jobs': bool, 'refunds': bool, 'rejected': bool},
  'by_outcome': [(label, claims, amount_text)], 'by_mechanic': [(name, n)], 'top_items': [(name, n)],
}
"""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from booking_report import DARK, GREEN, LIGHT, LINE, MARGIN, MUTED, PAGE, RED, WIDTH, _NumberedCanvas, _p, php
from order_report import TH, _fit, _table

BLUE_BG = colors.HexColor('#eff6ff')
BLUE_LINE = colors.HexColor('#bfdbfe')
BLUE_TEXT = colors.HexColor('#1e3a8a')


def _two(top, bottom, bold=True):
    t = f"<b>{escape(top)}</b>" if bold else escape(top)
    return _p(t + (f"<br/><font color='#6b7280' size='6.6'>{escape(bottom)}</font>" if bottom else ''))


def _status(text, good=True):
    return _p(f'<b>{escape(text)}</b>', textColor=GREEN if good else MUTED)


def _part(title, hint, table, empty_text):
    head = _p(f'<b>{escape(title)}</b> <font size="7" color="#6b7280">({escape(hint)})</font>', fontSize=9.5, leading=12)
    return [KeepTogether([head, Spacer(1, 4), table if table is not None else _p(empty_text, fontSize=7.5, textColor=MUTED)]), Spacer(1, 12)]


def build_rma_report_pdf(data):
    footer_left = f"MotoTyres North Caloocan  ·  Returns & Warranty  ·  {data['month_label']}"
    canvas_cls = type('FooterCanvas', (_NumberedCanvas,), {'footer_left': footer_left})
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=26, bottomMargin=44,
                            title=f"Monthly Returns & Warranty Report - {data['month_label']}", author='MotoTyres North Caloocan')
    story = []

    left = [_p('MOTO<font color="#d0021b">TYRES</font>', fontName='Helvetica-Bold', fontSize=22, leading=24),
            _p('North Caloocan Branch', fontSize=8, textColor=MUTED)]
    right = [_p('MONTHLY RETURNS &amp; WARRANTY REPORT', fontName='Helvetica-Bold', fontSize=13, leading=16, alignment=2),
             _p(escape(f"{data['month_label']} ({data['range_label']})"), fontSize=8.5, alignment=2, textColor=MUTED),
             _p(escape(f"Generated {data['generated']} by {data['admin']}"), fontSize=7.5, alignment=2, textColor=MUTED)]
    head = Table([[left, right]], colWidths=[WIDTH / 2, WIDTH / 2])
    head.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 5), HRFlowable(width=WIDTH, thickness=2, color=RED, spaceAfter=8)]

    if data.get('partial_note'):
        note = Table([[_p(escape(data['partial_note']), fontSize=7.8, textColor=BLUE_TEXT)]], colWidths=[WIDTH])
        note.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), BLUE_BG), ('BOX', (0, 0), (-1, -1), 0.8, BLUE_LINE),
                                  ('LEFTPADDING', (0, 0), (-1, -1), 8), ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
        story += [note, Spacer(1, 8)]

    s = data['summary']

    def box(label, value, sub=None):
        cell = [_p(label, fontName='Helvetica-Bold', fontSize=6.5, textColor=MUTED),
                _p(value, fontName='Helvetica-Bold', fontSize=16, leading=20)]
        if sub:
            cell.append(_p(sub, fontSize=6.5, textColor=MUTED))
        return cell

    gap = 9
    bw = (WIDTH - 4 * gap) / 5
    boxes = Table([[box('CLAIMS IN REPORT', str(s['claims']), f"Parts {s['parts']}  ·  Services {s['services']}"), '',
                    box('PART REPLACEMENTS', str(s['replacements']), f"Replaced in-store  ·  {php(s['replacement_value'])}"), '',
                    box('BACK JOBS', str(s['back_jobs']), 'Service redone, free'), '',
                    box('REFUNDS', php(s['refund_total']),
                        f"{s['refunds']} refund(s)  ·  GCash {php(s['refund_gcash'])}  ·  Cash {php(s['refund_cash'])}"), '',
                    box('REJECTED', str(s['rejected']), 'Not approved after review')]],
                  colWidths=[bw, gap, bw, gap, bw, gap, bw, gap, bw])
    bs = [('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
          ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 7)]
    for c in (0, 2, 4, 6, 8):
        bs.append(('BOX', (c, 0), (c, 0), 0.8, LINE))
    boxes.setStyle(TableStyle(bs))
    story += [boxes, Spacer(1, 14)]
    inc = data['included']

    # Part 1: part replacements
    if inc['replacements']:
        rows = data['replacements']
        table = None
        if rows:
            body = [[_two(r['ref'], f"Filed {r['filed']}"), _two(r['customer'], r['phone']), _two(r['item'], r['origin'], bold=False),
                     _p(escape(r['reason'])), _p(escape(r['decided'])), _p(escape(r['replaced'])), _status(r['status']),
                     _p(php(r['value']), alignment=2)] for r in rows]
            total = [_p(f"<b>Total ({len(rows)} replaced)</b>")] + [''] * 6 + [_p(f"<b>{php(sum(r['value'] for r in rows))}</b>", alignment=2)]
            table = _table(['RMA #', 'CUSTOMER', 'ITEM (FROM ORDER)', 'REASON', 'DECIDED', 'REPLACED IN-STORE', 'STATUS', 'ITEM VALUE'],
                           body, _fit([70, 112, 160, 150, 60, 74, 62, 72], 2), total, 6)
        story += _part('PART 1: PART REPLACEMENTS', 'a part that arrived wrong, replaced in-store', table, 'No part replacements closed this month.')

    # Part 2: back jobs
    if inc['back_jobs']:
        rows = data['back_jobs']
        table = None
        if rows:
            body = [[_two(r['ref'], f"Filed {r['filed']}"), _two(r['customer'], r['motorcycle']), _two(r['service'], r['origin'], bold=False),
                     _p(escape(r['original_mechanic'] or '—')), _p(escape(r['reason'])), _two(r['back_job'], r['back_job_date'], bold=False),
                     _p(escape(r['done_by'] or '—')), _status(r['status']),
                     _p('Free' if not r['charged'] else php(r['charged']), alignment=2)] for r in rows]
            charged = sum(r['charged'] for r in rows)
            label = (f"Total: {len(rows)} back job{'s' if len(rows) != 1 else ''}, all free for the customer" if not charged
                     else f"Total: {len(rows)} back job{'s' if len(rows) != 1 else ''}")
            total = [_p(f'<b>{escape(label)}</b>')] + [''] * 7 + [_p(f"<b>{'Free' if not charged else php(charged)}</b>", alignment=2)]
            table = _table(['RMA #', 'CUSTOMER &amp; MOTORCYCLE', 'ORIGINAL SERVICE', 'ORIGINAL MECHANIC', 'REASON', 'BACK JOB', 'DONE BY',
                            'STATUS', 'CHARGED'],
                           body, _fit([70, 112, 120, 84, 140, 78, 84, 64, 54], 4), total, 7)
        story += _part('PART 2: BACK JOBS', "a service that didn't hold, redone for free", table, 'No back jobs closed this month.')

    # Part 3: refunds
    if inc['refunds']:
        rows = data['refunds']
        table = None
        if rows:
            body = [[_two(r['ref'], f"Filed {r['filed']}"), _two(r['customer'], r['phone']), _two(r['item'], r['origin'], bold=False),
                     _p(escape(r['type'])), _p(escape(r['reason'])), _p(escape(r['method'])), _p(escape(r['released'])), _status(r['status']),
                     _p(php(r['amount']), alignment=2)] for r in rows]
            total = [_p('<b>Total refunded</b>')] + [''] * 7 + [_p(f"<b>{php(sum(r['amount'] for r in rows))}</b>", alignment=2, textColor=RED)]
            table = _table(['RMA #', 'CUSTOMER', 'FOR', 'TYPE', 'REASON', 'METHOD', 'RELEASED', 'STATUS', 'AMOUNT'],
                           body, _fit([70, 112, 160, 46, 150, 52, 60, 62, 72], 2), total, 7)
        story += _part('PART 3: REFUNDS', 'money given back instead', table, 'No refunds released this month.')

    # Part 4: rejected
    if inc['rejected']:
        rows = data['rejected']
        table = None
        if rows:
            body = [[_two(r['ref'], f"Filed {r['filed']}"), _p(f"<b>{escape(r['customer'])}</b>"), _p(escape(r['type'])),
                     _two(r['item'], r['origin'], bold=False), _p(escape(r['asked'])), _p(escape(r['why'])), _p(escape(r['decided'])),
                     _status(r['status'], good=False)] for r in rows]
            table = _table(['RMA #', 'CUSTOMER', 'TYPE', 'COVERS', 'ASKED BECAUSE', 'WHY REJECTED', 'DECIDED', 'STATUS'],
                           body, _fit([70, 106, 46, 150, 130, 170, 56, 58], 5))
        story += _part('PART 4: REJECTED CLAIMS', 'not approved after review', table, 'No claims were rejected this month.')

    story += [_p('Every claim is one choice: put it right (part replacement or back job) or give the money back (refund), never both. '
                 'Part replacements are done in-store only. Back jobs are free for the customer. Open claims are not included.',
                 fontSize=6.8, textColor=MUTED), Spacer(1, 12)]

    def small(title, head_cols, rows, widths, right_cols=(), total_row=None):
        body = [[Paragraph(h, TH) for h in head_cols]]
        for r in rows:
            body.append([_p(escape(str(c)), alignment=2 if i in right_cols else 0) for i, c in enumerate(r)])
        if not rows:
            body.append([_p('—', textColor=MUTED)] + [''] * (len(head_cols) - 1))
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
    outc = data['by_outcome']
    a = small('CLAIMS BY OUTCOME', ['OUTCOME', 'CLAIMS', 'AMOUNT'], outc, [w1 - 50 - 84, 50, 84], right_cols=(1, 2),
              total_row=('Total', sum(n for _l, n, _a in outc), ''))
    b = small('BACK JOBS BY MECHANIC', ['ORIGINAL MECHANIC', 'BACK JOBS'], data['by_mechanic'], [w2 - 64, 64], right_cols=(1,))
    c = small('MOST RETURNED ITEMS', ['ITEM / SERVICE', 'CLAIMS'], data['top_items'], [w3 - 56, 56], right_cols=(1,))
    trio = Table([[a, '', b, '', c]], colWidths=[w1, g, w2, g, w3])
    trio.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                              ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))

    sw = (WIDTH - 80) / 2
    role = escape(data.get('admin_role') or 'Administrator')
    sig = Table([['', '', ''], [_p(f"Prepared by: <b>{escape(data['admin'])}</b><br/><font size='7' color='#6b7280'>{role}</font>", fontSize=8), '',
                                _p("Noted by:<br/><font size='7' color='#6b7280'>Branch Owner</font>", fontSize=8)]],
                colWidths=[sw, 80, sw], rowHeights=[28, None])
    sig.setStyle(TableStyle([('LINEABOVE', (0, 1), (0, 1), 0.8, DARK), ('LINEABOVE', (2, 1), (2, 1), 0.8, DARK),
                             ('LEFTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 1), (-1, 1), 3)]))
    story.append(KeepTogether([trio, Spacer(1, 6), sig]))

    doc.build(story, canvasmaker=canvas_cls)
    return buf.getvalue()
