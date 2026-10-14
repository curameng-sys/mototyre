import re

SRC = r'D:\CAPS\mototyre\diagrams_artifact.html'
OUT_DIR = r'D:\CAPS\mototyre'

TOKENS = {
    'var(--surface-2)': '#efe9e7',
    'var(--surface)': '#ffffff',
    'var(--accent-soft)': '#fbe4e7',
    'var(--accent)': '#c8001f',
    'var(--line)': '#d9d1ce',
    'var(--muted)': '#6e6360',
    'var(--ink)': '#221c1b',
}

STYLE_BLOCK = """<style>
      text { font-family: 'DM Sans', Arial, sans-serif; }
      .dtitle   { font-family: 'DM Sans', Arial, sans-serif; font-weight: 700; font-size: 14px; fill: currentColor; }
      .dsub     { font-family: 'JetBrains Mono', 'Courier New', monospace; font-size: 10.5px; fill: #6e6360; }
      .dattr    { font-family: 'JetBrains Mono', 'Courier New', monospace; font-size: 11.5px; fill: currentColor; }
      .dattr-m  { font-family: 'JetBrains Mono', 'Courier New', monospace; font-size: 11.5px; fill: #6e6360; }
      .dlabel   { font-family: 'DM Sans', Arial, sans-serif; font-size: 11.5px; fill: #6e6360; }
      .dlabel-b { font-family: 'DM Sans', Arial, sans-serif; font-weight: 700; font-size: 11.5px; fill: #c8001f; }
      .dnode    { font-family: 'DM Sans', Arial, sans-serif; font-size: 12.5px; fill: currentColor; }
      .dnode-b  { font-family: 'DM Sans', Arial, sans-serif; font-weight: 700; font-size: 13px; fill: currentColor; }
      .dfoot    { font-family: 'JetBrains Mono', 'Courier New', monospace; font-size: 10.5px; fill: #6e6360; }
    </style>"""

with open(SRC, encoding='utf-8') as f:
    html = f.read()

svgs = re.findall(r'<svg\b.*?</svg>', html, re.S)
assert len(svgs) == 3, f'expected 3 svg blocks, found {len(svgs)}'

names = ['diagram_01_data_flow', 'diagram_02_erd', 'diagram_03_process_flow']

for name, svg in zip(names, svgs):
    s = svg
    for token, hexval in TOKENS.items():
        s = s.replace(token, hexval)

    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', s)
    vb_w, vb_h = m.group(1), m.group(2)

    # inject width/height (helps apps that don't infer size from viewBox alone)
    s = s.replace('<svg viewBox=', f'<svg width="{vb_w}" height="{vb_h}" viewBox=', 1)

    # insert style block + white background rect right after the opening <svg ...> tag
    open_tag_end = s.index('>') + 1
    bg_rect = f'\n      <rect x="0" y="0" width="{vb_w}" height="{vb_h}" fill="#ffffff"/>'
    s = s[:open_tag_end] + '\n    ' + STYLE_BLOCK + bg_rect + s[open_tag_end:]

    out = '<?xml version="1.0" encoding="UTF-8"?>\n' + s + '\n'
    out_path = f'{OUT_DIR}\\{name}.svg'
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(out)
    print('wrote', out_path, len(out), 'bytes')
