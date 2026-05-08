#!/usr/bin/env python3
"""
Ford Workshop Manual local web server.

Handles three concerns:
  1. Static file serving from extracted/ with arc-aware path resolution.
  2. /renderers/2colframeset.asp and /tpsasps/2colframeset.asp — synthesises
     the two-column <frameset> that Ford's ASP server would have generated.
  3. /renderers/wiringsvg/ep_main.asp — locates and serves wiring diagram SVGs
     by CELL number and book code.

Usage:
  python3 serve.py [port]          (default port 8080)

Then open http://localhost:8080/ in a browser.
To jump straight to the S197 Mustang:
  http://localhost:8080/SEB/SEBALPHAINDEX.HTM
"""

import os
import sys
import mimetypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

EXTRACTED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'extracted')

# ---------------------------------------------------------------------------
# File index: lower-case basename  →  full absolute path
# Built once at startup so lookups are O(1).
# ---------------------------------------------------------------------------
_file_index: dict[str, str] = {}
_arc_dirs:   dict[str, str] = {}   # arc name (upper) → arc directory path


def build_index(root: str) -> None:
    for arc in os.listdir(root):
        arc_dir = os.path.join(root, arc)
        if not os.path.isdir(arc_dir):
            continue
        _arc_dirs[arc.upper()] = arc_dir
        for fname in os.listdir(arc_dir):
            _file_index[fname.lower()] = os.path.join(arc_dir, fname)


def find_file(name: str) -> str | None:
    """Return the absolute path for a bare filename, or None."""
    return _file_index.get(name.lower())


def file_url(full_path: str) -> str:
    """Convert an absolute extracted path to a server-relative URL /ARC/file."""
    rel = os.path.relpath(full_path, EXTRACTED_DIR)
    return '/' + rel.replace(os.sep, '/')


# ---------------------------------------------------------------------------
# Arc catalogue: arc code → (label, alpha-index filename or None)
# Wiring arcs (EE*) share the last two letters with their manual arc (SE*/SD*/SC*).
# R* arcs are field service actions; they are grouped separately.
# ---------------------------------------------------------------------------

ARC_CATALOGUE = {
    # Workshop manuals — vehicles
    'SE2': ('2014 F-150',                        'SE2ALPHAINDEX.HTM'),
    'SEB': ('2014 Mustang',                       'SEBALPHAINDEX.HTM'),
    'SCA': ('2012–2014 Focus Electric',           None),
    'SCY': ('2012–2014 Focus',                    None),
    'SDS': ('2013–2014 Fusion',                   'SDSALPHAINDEX.HTM'),
    'SDE': ('2013–2014 Fusion Hybrid/Energi',     'SDEALPHAINDEX.HTM'),
    'SDG': ('2013–2014 C-MAX Hybrid/Energi',      'SDGALPHAINDEX.HTM'),
    'SDL': ('2013–2014 MKZ',                      'SDLALPHAINDEX.HTM'),
    'SDW': ('2013–2014 MKZ Hybrid',               'SDWALPHAINDEX.HTM'),
    'SDZ': ('2013–2014 Escape',                   'SDZALPHAINDEX.HTM'),
    'SED': ('2014 Flex',                          'SEDALPHAINDEX.HTM'),
    'SEF': ('2014 Taurus / Police Interceptor Sedan', 'SEFALPHAINDEX.HTM'),
    'SEH': ('2014 MKS',                           'SEHALPHAINDEX.HTM'),
    'SEI': ('2014 F-53 Motorhome / F-59 Stripped Chassis', 'SEIALPHAINDEX.HTM'),
    'SEJ': ('2014 Expedition / Navigator',        'SEJALPHAINDEX.HTM'),
    'SEM': ('2014 E-Series',                      'SEMALPHAINDEX.HTM'),
    'SEN': ('2014 Explorer / Police Interceptor Utility', 'SENALPHAINDEX.HTM'),
    'SEO': ('2014 F-250/350/450/550 Super Duty',  'SEOALPHAINDEX.HTM'),
    'SEP': ('2014 MKT',                           'SEPALPHAINDEX.HTM'),
    'SER': ('2014 Fiesta',                        'SERALPHAINDEX.HTM'),
    'SEV': ('2014 Edge / MKX',                    'SEVALPHAINDEX.HTM'),
    # Specialty manuals
    'SIB': ('Noise, Vibration & Harshness',       'SIBALPHAINDEX.HTM'),
    'SIA': ('7.3L DI Turbo Diesel (1997–2015)',   None),
    'VE2': ('Gasoline Engines Reference',         None),
    'VEF': ('6.7L Diesel Reference',              None),
    'VEM': ('Hybrid Reference',                   None),
}

# Wiring arc suffix → workshop arc suffix (EE* → SE*/SD*/SC*)
WIRING_SUFFIX_MAP = {
    '2': 'SE2',  # F-150
    'A': 'SCA',  # Focus Electric
    'B': 'SEB',  # Mustang
    'D': 'SED',  # Flex
    'F': 'SEF',  # Taurus
    'G': 'SDG',  # C-MAX
    'H': 'SEH',  # MKS
    'J': 'SEJ',  # Expedition/Navigator
    'K': 'SEK',  # (unknown)
    'M': 'SEM',  # E-Series
    'N': 'SEN',  # Explorer
    'O': 'SEO',  # Super Duty
    'P': 'SEP',  # MKT
    'R': 'SER',  # Fiesta
    'S': 'SDS',  # Fusion
    'T': 'SDZ',  # Escape (best guess by process of elimination)
    'V': 'SEV',  # Edge/MKX
    'Y': 'SCY',  # Focus
}


def _build_home_page() -> str:
    """Generate the vehicle navigation home page from present arc directories."""
    # Group arcs into: manuals, wiring, recalls, other
    present = set(_arc_dirs.keys())

    # Build vehicle rows: each known manual arc + its wiring counterpart if present
    suffix_to_wiring = {}  # manual-arc-suffix → wiring arc code
    for ee_suffix, manual_arc in WIRING_SUFFIX_MAP.items():
        wiring_arc = 'EE' + ee_suffix
        if wiring_arc in present:
            suffix_to_wiring[manual_arc] = wiring_arc

    rows = []
    for arc, (label, index_file) in ARC_CATALOGUE.items():
        if arc not in present:
            continue
        if index_file:
            full = os.path.join(EXTRACTED_DIR, arc, index_file)
            if os.path.isfile(full):
                manual_link = f'<a href="/{arc}/{index_file}">{label}</a>'
            else:
                manual_link = f'<a href="/{arc}/">{label}</a>'
        else:
            manual_link = f'<a href="/{arc}/">{label}</a>'

        wiring_arc = suffix_to_wiring.get(arc)
        if wiring_arc:
            wiring_link = f'<a href="/{wiring_arc}/" class="sub">+ Wiring ({wiring_arc})</a>'
        else:
            wiring_link = ''

        rows.append(f'<tr><td class="arc">{arc}</td>'
                    f'<td>{manual_link}{(" &nbsp; " + wiring_link) if wiring_link else ""}</td></tr>')

    # Recalls: group R* arcs
    recall_arcs = sorted(a for a in present if a.startswith('R'))
    recall_html = ''
    if recall_arcs:
        items = ''.join(
            f'<li><a href="/{a}/">{a}</a></li>'
            for a in recall_arcs
        )
        recall_html = f'<h2>Field Service Actions / Recalls</h2><ul class="recalls">{items}</ul>'

    # EE* arcs not matched to any manual
    unmatched_ee = sorted(
        a for a in present
        if a.startswith('EE') and a not in suffix_to_wiring.values()
    )
    unmatched_html = ''
    if unmatched_ee:
        items = ''.join(f'<li><a href="/{a}/">{a}</a></li>' for a in unmatched_ee)
        unmatched_html = f'<h2>Wiring Diagram Arcs (unmatched)</h2><ul class="recalls">{items}</ul>'

    table_rows = '\n'.join(rows) or '<tr><td colspan="2">No workshop manuals found.</td></tr>'

    return f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Ford Workshop Manual</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Arial, sans-serif; background: #f4f4f4; color: #222; }}
  header {{ background: #002060; color: #fff; padding: 16px 24px; }}
  header h1 {{ font-size: 20px; font-weight: bold; letter-spacing: 1px; }}
  header p {{ font-size: 12px; opacity: .7; margin-top: 4px; }}
  main {{ padding: 24px; max-width: 900px; }}
  h2 {{ font-size: 15px; color: #002060; margin: 24px 0 8px; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
  table {{ border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
  td {{ padding: 9px 14px; border-bottom: 1px solid #eee; font-size: 14px; vertical-align: middle; }}
  td.arc {{ color: #888; font-family: monospace; font-size: 13px; width: 70px; }}
  a {{ color: #003399; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  a.sub {{ color: #666; font-size: 12px; }}
  ul.recalls {{ list-style: none; columns: 4; background: #fff; padding: 12px 16px;
                box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
  ul.recalls li a {{ font-size: 12px; font-family: monospace; color: #555; }}
  ul.recalls li a:hover {{ color: #003399; text-decoration: underline; }}
</style>
</head>
<body>
<header>
  <h1>Ford Workshop Manual</h1>
  <p>Offline service information &mdash; 2012&ndash;2014 model years</p>
</header>
<main>
<h2>Workshop Manuals</h2>
<table>
{table_rows}
</table>
{unmatched_html}
{recall_html}
</main>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------

FRAMESET_HTML = """\
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Frameset//EN">
<html>
<head><title>Ford Workshop Manual</title></head>
<frameset cols="30%,*">
  <frame src="{left_url}" name="leftside">
  <frame src="{right_url}" name="rightside">
</frameset>
</html>"""

WIRING_HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Wiring Diagram &mdash; {book} Cell {cell}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Arial, sans-serif; background: #f0f0f0; }}
  .bar {{ background: #003366; color: #fff; padding: 6px 12px; font-size: 13px; }}
  .wrap {{ width: 100%; height: calc(100vh - 30px); overflow: auto; background: #fff; }}
  .wrap svg {{ width: 100%; height: auto; }}
  .err {{ padding: 24px; color: #900; }}
</style>
</head>
<body>
<div class="bar">Wiring Diagram &mdash; {book} &nbsp;|&nbsp; Cell {cell_pad}</div>
<div class="wrap">{body}</div>
</body>
</html>"""

DIR_HTML = """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: Arial, sans-serif; padding: 16px; }}
  h2 {{ margin-bottom: 12px; }}
  ul {{ list-style: none; columns: 3; }}
  li a {{ text-decoration: none; color: #036; font-size: 13px; }}
  li a:hover {{ text-decoration: underline; }}
</style></head>
<body><h2>{title}</h2><ul>{items}</ul></body>
</html>"""


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Uncomment for access logging:
        # super().log_message(fmt, *args)
        pass

    # --- response helpers ---

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str, status: int = 200) -> None:
        self.send_bytes(html.encode('utf-8'), 'text/html; charset=utf-8', status)

    def send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header('Location', location)
        self.end_headers()

    # --- routing ---

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path   = unquote(parsed.path)
        qs     = parse_qs(parsed.query, keep_blank_values=True)

        # ── ASP renderers ────────────────────────────────────────────────────

        if path in ('/renderers/2colframeset.asp', '/tpsasps/2colframeset.asp'):
            self._handle_frameset(qs)
            return

        if path == '/renderers/wiringsvg/ep_main.asp':
            self._handle_wiring(qs)
            return

        # ── Static files ─────────────────────────────────────────────────────

        rel = path.lstrip('/')

        # Root → vehicle navigation home page
        if rel == '':
            self.send_html(_build_home_page())
            return

        # Try /ARC/filename  (keeps relative URLs working correctly)
        parts = rel.split('/', 1)
        if len(parts) == 2:
            arc, fname = parts[0].upper(), parts[1]
            candidate = os.path.join(EXTRACTED_DIR, arc, fname)
            if os.path.isfile(candidate):
                self._serve_file(candidate)
                return
            # Arc dir exists but file missing → try global index
            if arc in _arc_dirs and fname:
                full = find_file(fname)
                if full:
                    self.send_redirect(file_url(full))
                    return

        # /ARC with no trailing slash → arc directory listing
        if len(parts) == 1:
            arc_dir = os.path.join(EXTRACTED_DIR, parts[0].upper())
            if os.path.isdir(arc_dir):
                self._serve_dir(arc_dir, '/' + parts[0].upper() + '/')
                return

        # Bare filename anywhere in the index
        full = find_file(os.path.basename(rel))
        if full:
            self.send_redirect(file_url(full))
            return

        self.send_html(f'<h2>404 Not Found</h2><p>{path}</p>', 404)

    # ── ASP handlers ─────────────────────────────────────────────────────────

    def _handle_frameset(self, qs: dict) -> None:
        """Synthesise a two-column frameset from leftside/rightside params."""
        leftside  = qs.get('leftside',  [''])[0]
        rightside = qs.get('rightside', [''])[0]

        left_url  = self._resolve_url(leftside)
        right_url = self._resolve_url(rightside)

        self.send_html(FRAMESET_HTML.format(left_url=left_url, right_url=right_url))

    def _handle_wiring(self, qs: dict) -> None:
        """Serve a wiring diagram SVG for ep_main.asp?CELL=NNN&book=XXX."""
        cell = qs.get('CELL', [''])[0]
        book = qs.get('book', [''])[0].upper()

        cell_pad = cell.zfill(3)
        prefix   = (book + cell_pad).lower()

        # SVGs are named like EEB042001.SVG — find all with matching prefix
        matches = sorted(
            v for k, v in _file_index.items()
            if k.startswith(prefix) and k.endswith('.svg')
        )

        if matches:
            # Embed the first SVG directly; add thumbnails for extras
            svg_body = self._inline_svg(matches[0])
            if len(matches) > 1:
                thumbs = ''.join(
                    f'<p><a href="{file_url(m)}">{os.path.basename(m)}</a></p>'
                    for m in matches[1:]
                )
                svg_body += f'<div style="padding:8px;font-size:12px;">Additional sheets:{thumbs}</div>'
            body = svg_body
        else:
            body = f'<div class="err">No wiring diagram found for {book} cell {cell}.<br>'\
                   f'(Looked for files matching <code>{prefix}*.svg</code>)</div>'

        self.send_html(WIRING_HTML.format(
            book=book, cell=cell, cell_pad=cell_pad, body=body
        ))

    # ── File serving ─────────────────────────────────────────────────────────

    def _serve_file(self, full_path: str) -> None:
        mime, _ = mimetypes.guess_type(full_path)
        if not mime:
            mime = 'application/octet-stream'
        if mime.startswith('text/') and 'charset' not in mime:
            mime += '; charset=utf-8'

        with open(full_path, 'rb') as f:
            body = f.read()

        self.send_bytes(body, mime)

    def _serve_dir(self, dir_path: str, url_prefix: str) -> None:
        title = url_prefix or '/'
        items_html = ''.join(
            f'<li><a href="{url_prefix.rstrip("/")}/{name}">{name}</a></li>'
            for name in sorted(os.listdir(dir_path))
            if not name.startswith('.')
        )
        self.send_html(DIR_HTML.format(title=title, items=items_html))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_url(self, filename: str) -> str:
        """Return a server-relative URL for a bare filename."""
        if not filename:
            return '/'
        full = find_file(filename)
        return file_url(full) if full else '/' + filename

    def _inline_svg(self, svg_path: str) -> str:
        """Read an SVG file and return it as an inline HTML string."""
        with open(svg_path, 'rb') as f:
            raw = f.read()
        # Strip XML declaration if present so it embeds cleanly
        text = raw.decode('utf-8', errors='replace')
        if text.startswith('<?xml'):
            text = text[text.index('?>') + 2:].lstrip()
        return text


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

    if not os.path.isdir(EXTRACTED_DIR):
        sys.exit(f'Error: extracted directory not found at {EXTRACTED_DIR}')

    print(f'Building file index from {EXTRACTED_DIR} ...')
    build_index(EXTRACTED_DIR)
    print(f'Indexed {len(_file_index):,} files across {len(_arc_dirs)} arc directories')
    print()
    print(f'Server running on  http://0.0.0.0:{port}/')
    print(f'S197 Mustang:      http://localhost:{port}/SEB/SEBALPHAINDEX.HTM')
    print()

    try:
        HTTPServer(('', port), Handler).serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
