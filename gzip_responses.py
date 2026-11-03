"""Compress text responses (pages, JSON, CSS, JS) with gzip when the browser accepts it.

The customer and admin pages are about 700 KB of HTML each. Sent as they are, every page load downloads all of it, which is what
makes the screens slow to appear on a normal connection. Gzip brings that down by about 85%. Nothing about the content changes.
Only complete, plain text responses are touched: files sent from disk, streams, and anything already encoded are left as they are.
"""
import gzip

_TEXT_TYPES = ('text/html', 'text/css', 'text/plain', 'text/javascript', 'application/javascript', 'application/json', 'image/svg+xml')
MIN_BYTES = 1024          # smaller than this is not worth compressing


def register(flask_app):
    @flask_app.after_request
    def _gzip(resp):
        try:
            from flask import request
            if (resp.status_code < 200 or resp.status_code >= 300 or resp.direct_passthrough or 'Content-Encoding' in resp.headers
                    or 'gzip' not in request.headers.get('Accept-Encoding', '').lower()
                    or (resp.mimetype or '') not in _TEXT_TYPES):
                return resp
            data = resp.get_data()
            if len(data) < MIN_BYTES:
                return resp
            resp.set_data(gzip.compress(data, compresslevel=5))
            resp.headers['Content-Encoding'] = 'gzip'
            resp.headers['Content-Length'] = str(len(resp.get_data()))
            vary = resp.headers.get('Vary', '')
            if 'accept-encoding' not in vary.lower():
                resp.headers['Vary'] = (vary + ', ' if vary else '') + 'Accept-Encoding'
        except Exception:
            pass                        # never let compression break a response
        return resp
