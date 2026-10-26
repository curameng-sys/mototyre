"""Monthly Booking Report (PDF, landscape A4).

Only draws. The numbers are worked out by the caller from the bookings, quotations and billing already saved, and handed in as
plain data, so this file never touches the database and can be tested on its own.

data = {
  'month_label': 'September 2026', 'range_label': 'Sep 1 - 30, 2026', 'generated': 'Oct 17, 2026, 8:30 PM', 'admin': 'Admin',
  'summary': {'total': 11, 'completed': 9, 'cancelled': 2, 'paid': 8110.0, 'cash': 5160.0, 'gcash': 2950.0, 'unpaid_completed': 0},
  'rows': [{'ref': '#351', 'date': 'Sep 2, 2026', 'time': '8:00 AM', 'customer': 'Liza Ramos', 'motorcycle': 'Honda BeAT',
            'service': 'Tune-Up', 'extra_note': '+ Front brake pad replacement' | 'Extra work declined' | '',
            'mechanic': 'Boyet Santos', 'status': 'Completed' | 'Cancelled', 'payment': 'Cash' | 'GCash' | 'Unpaid' | '',
            'booked': 450.0, 'extra': 0.0, 'materials': 400.0, 'total': 850.0}, ...],
  'totals': {'booked': ..., 'extra': ..., 'materials': ..., 'total': ...},
  'by_service': [('Change Oil', 2), ...], 'by_mechanic': [('Boyet Santos', 3), ...],
  'materials': [('Ball Race Set', 1, 450.0), ...], 'materials_total': 3110.0,
}
"""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

RED = colors.HexColor('#d0021b')
DARK = colors.HexColor('#1a1d23')
LIGHT = colors.HexColor('#f3f4f6')
MUTED = colors.HexColor('#6b7280')
GREEN = colors.HexColor('#15803d')
LINE = colors.HexColor('#e5e7eb')

PAGE = landscape(A4)
MARGIN = 28
WIDTH = PAGE[0] - 2 * MARGIN


def php(n):
    return f'Php {float(n or 0):,.2f}'


def _p(text, **kw):
    kw.setdefault('fontName', 'Helvetica')
    kw.setdefault('fontSize', 7.5)
    kw.setdefault('leading', kw['fontSize'] * 1.3)
    kw.setdefault('textColor', DARK)
    return Paragraph(text, ParagraphStyle('x', **kw))


class _NumberedCanvas(canvas.Canvas):
    """Draws the footer on every page, which needs the total page count, so pages are kept until the end."""
    footer_left = ''

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total):
        self.setStrokeColor(LINE)
        self.setLineWidth(0.6)
        self.line(MARGIN, 30, PAGE[0] - MARGIN, 30)
        self.setFont('Helvetica', 7)
        self.setFillColor(MUTED)
        self.drawString(MARGIN, 19, self.footer_left)
        self.drawRightString(PAGE[0] - MARGIN, 19, f'Page {self._pageNumber} of {total}')


def build_booking_report_pdf(data):
    footer_left = f"MotoTyres North Caloocan  ·  Monthly Booking Report  ·  {data['month_label']}"
    canvas_cls = type('FooterCanvas', (_NumberedCanvas,), {'footer_left': footer_left})
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=26, bottomMargin=44,
                            title=f"Monthly Booking Report - {data['month_label']}", author='MotoTyres North Caloocan')
    story = []

    # ── heading ──
    left = [_p('MOTO<font color="#d0021b">TYRES</font>', fontName='Helvetica-Bold', fontSize=22, leading=24),
            _p('North Caloocan Branch', fontSize=8, textColor=MUTED)]
    right = [_p('MONTHLY BOOKING REPORT', fontName='Helvetica-Bold', fontSize=13, leading=16, alignment=2),
             _p(escape(f"{data['month_label']} ({data['range_label']})"), fontSize=8.5, alignment=2, textColor=MUTED),
             _p(escape(f"Generated {data['generated']} by {data['admin']}"), fontSize=7.5, alignment=2, textColor=MUTED)]
    head = Table([[left, right]], colWidths=[WIDTH / 2, WIDTH / 2])
    head.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 5), HRFlowable(width=WIDTH, thickness=2, color=RED, spaceAfter=10)]

    # ── four summary boxes ──
    s = data['summary']

    def box(label, value, sub=None, color=DARK):
        cell = [_p(label, fontName='Helvetica-Bold', fontSize=6.5, textColor=MUTED),
                _p(value, fontName='Helvetica-Bold', fontSize=18, leading=22, textColor=color)]
        if sub:
            cell.append(_p(sub, fontSize=6.5, textColor=MUTED))
        return cell

    gap = 10
    bw = (WIDTH - 3 * gap) / 4
    boxes = Table([[box('TOTAL BOOKINGS', str(s['total'])), '', box('COMPLETED', str(s['completed'])), '',
                    box('CANCELLED', str(s['cancelled'])), '',
                    box('TOTAL PAID', php(s['paid']), f"Cash {php(s['cash'])}  ·  GCash {php(s['gcash'])}")]],
                  colWidths=[bw, gap, bw, gap, bw, gap, bw])
    bs = [('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
          ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 7)]
    for c in (0, 2, 4, 6):
        bs += [('BOX', (c, 0), (c, 0), 0.8, LINE), ('ROUNDEDCORNERS', [4, 4, 4, 4])]
    boxes.setStyle(TableStyle(bs))
    story += [boxes, Spacer(1, 10)]
    if s.get('unpaid_completed'):
        story += [_p(f"{s['unpaid_completed']} completed booking(s) were not paid yet when this report was made. "
                     f"They are listed, marked Unpaid, and are not counted in Total Paid.", fontSize=7, textColor=colors.HexColor('#92400e')),
                  Spacer(1, 6)]

    # ── bookings table ──
    heads = ['BOOKING #', 'DATE & TIME', 'CUSTOMER / MOTORCYCLE', 'SERVICE', 'MECHANIC', 'STATUS', 'PAYMENT',
             'BOOKED SERVICE', 'EXTRA WORK', 'MATERIALS USED', 'TOTAL']
    cw = [44, 68, 112, 138, 78, 54, 46, 62, 56, 62, 66]
    cw[3] += WIDTH - sum(cw)                                    # the service column takes whatever width is left
    th = ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=6.6, textColor=colors.white, leading=8)
    table = [[Paragraph(h, th) for h in heads]]
    num = lambda v: _p(php(v), alignment=2)
    dash = lambda bold=False: _p('—', alignment=2, textColor=MUTED)
    for r in data['rows']:
        done = r['status'] == 'Completed'
        service = f"<b>{escape(r['service'])}</b>" if False else escape(r['service'])
        if r.get('extra_note'):
            service += f'<br/><font size="6.4" color="#6b7280">{escape(r["extra_note"])}</font>'
        status = _p(f"<b>{escape(r['status'])}</b>", textColor=GREEN if done else MUTED)
        pay = _p(escape(r['payment']) if (done and r.get('payment')) else '—', textColor=DARK if done else MUTED)
        row = [
            _p(f"<b>{escape(r['ref'])}</b>"),
            _p(f"{escape(r['date'])}<br/><font color='#6b7280'>{escape(r['time'])}</font>"),
            _p(f"<b>{escape(r['customer'])}</b><br/><font color='#6b7280'>{escape(r['motorcycle'] or '')}</font>"),
            _p(service),
            _p(escape(r['mechanic'] or '—')),
            status, pay,
        ]
        if done:
            row += [num(r['booked']) if r['booked'] else dash(),
                    num(r['extra']) if r['extra'] else dash(),
                    num(r['materials']) if r['materials'] else dash(),
                    _p(f"<b>{php(r['total'])}</b>", alignment=2)]
        else:
            row += [dash(), dash(), dash(), dash()]
        table.append(row)
    t = data['totals']
    table.append([_p('<b>TOTAL (COMPLETED BOOKINGS)</b>', alignment=2)] + [''] * 6 +
                 [_p(f"<b>{php(t['booked'])}</b>", alignment=2), _p(f"<b>{php(t['extra'])}</b>", alignment=2),
                  _p(f"<b>{php(t['materials'])}</b>", alignment=2), _p(f"<b>{php(t['total'])}</b>", alignment=2, textColor=RED)])
    last = len(table) - 1
    ts = [('BACKGROUND', (0, 0), (-1, 0), DARK), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
          ('ROWBACKGROUNDS', (0, 1), (-1, last - 1), [colors.white, LIGHT]),
          ('GRID', (0, 0), (-1, -1), 0.4, LINE),
          ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
          ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
          ('SPAN', (0, last), (6, last)), ('BACKGROUND', (0, last), (-1, last), LIGHT),
          ('LINEABOVE', (0, last), (-1, last), 1.2, DARK)]
    main = Table(table, colWidths=cw, repeatRows=1)
    main.setStyle(TableStyle(ts))
    story += [main, Spacer(1, 5),
              _p('Total = booked service + extra work (inspection findings the customer approved) + materials used. '
                 'Cancelled bookings have no amount.', fontSize=6.8, textColor=MUTED), Spacer(1, 12)]

    # ── three small tables side by side ──
    def small(title, head_cols, rows, widths, right_cols=(), total_row=None):
        body = [[Paragraph(h, th) for h in head_cols]]
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
    w1, w2 = 232, 232
    w3 = WIDTH - w1 - w2 - 2 * g
    a = small('COMPLETED BOOKINGS BY SERVICE', ['SERVICE', 'BOOKINGS'], data['by_service'], [w1 - 56, 56], right_cols=(1,))
    b = small('COMPLETED JOBS BY MECHANIC', ['MECHANIC', 'COMPLETED'], data['by_mechanic'], [w2 - 56, 56], right_cols=(1,))
    c = small('MATERIALS USED', ['ITEM', 'QTY', 'AMOUNT'],
              [(n, q, php(amt)) for n, q, amt in data['materials']], [w3 - 36 - 76, 36, 76], right_cols=(1, 2),
              total_row=('Total', '', php(data['materials_total'])))
    trio = Table([[a, '', b, '', c]], colWidths=[w1, g, w2, g, w3])
    trio.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                              ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))

    # ── signature lines ──
    sw = (WIDTH - 2 * 40) / 2
    sig = Table([['', '', ''], [_p(f"Prepared by: {escape(data['admin'])}", fontSize=8), '', _p('Noted by: Branch Owner', fontSize=8)]],
                colWidths=[sw, 80, sw], rowHeights=[28, None])
    sig.setStyle(TableStyle([('LINEABOVE', (0, 1), (0, 1), 0.8, DARK), ('LINEABOVE', (2, 1), (2, 1), 0.8, DARK),
                             ('LEFTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 1), (-1, 1), 3)]))
    story.append(KeepTogether([trio, Spacer(1, 6), sig]))

    doc.build(story, canvasmaker=canvas_cls)
    return buf.getvalue()
