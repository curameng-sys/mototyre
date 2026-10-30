"""Real-time push over WebSocket, shared by the customer app and the admin app.

Why this exists: every open page used to ask the server "anything new?" every 2 seconds, and each of those asks ran a
pile of database queries. Now the page keeps one WebSocket open and the server tells it when something changed.

How it works (one hub per app process):
  * connected pages are kept in a set;
  * ONE background thread (not one per page) computes a small "what changed" fingerprint every few seconds, but only
    while somebody is connected, and pushes it to everyone only when it is different from the last one;
  * the two apps are separate processes that only share the database, so each hub notices the other app's changes
    through that same fingerprint (a booking the customer makes shows up on the admin page a moment later).

If the socket can't connect (old browser, proxy, server without threads) the pages fall back to a slow poll, so
nothing breaks. On a server that handles one request at a time the socket is refused straight away, because an open
socket would otherwise freeze every other request.
"""
import json
import threading
import time


class LiveHub:
    def __init__(self, app, compute, interval=3.0, name='live'):
        self.app = app
        self.compute = compute          # () -> JSON-able dict, run inside an app context
        self.interval = interval
        self.name = name
        self.clients = set()
        self.last = None                # last payload (as a JSON string)
        self._lock = threading.Lock()
        self._thread = None
        self._wake = threading.Event()

    # ── connection bookkeeping ──
    def add(self, ws):
        with self._lock:
            self.clients.add(ws)
        self._ensure_thread()

    def remove(self, ws):
        with self._lock:
            self.clients.discard(ws)

    def snapshot(self):
        """The current fingerprint, so a page that just connected is up to date straight away."""
        if self.last is None:
            self.last = self._compute_json()
        return self.last

    def nudge(self):
        """Something in THIS process just changed data: check now instead of waiting for the next tick."""
        self._wake.set()

    # ── internals ──
    def _compute_json(self):
        with self.app.app_context():
            try:
                return json.dumps(self.compute(), sort_keys=True, default=str)
            finally:
                try:
                    from flask import current_app
                    ext = current_app.extensions.get('sqlalchemy')
                    if ext is not None:
                        ext.session.remove()        # never leave a connection checked out by the background thread
                except Exception:
                    pass

    def _broadcast(self, text):
        dead = []
        with self._lock:
            targets = list(self.clients)
        for ws in targets:
            try:
                ws.send(text)
            except Exception:
                dead.append(ws)
        if dead:
            with self._lock:
                for ws in dead:
                    self.clients.discard(ws)

    def _ensure_thread(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name=f'{self.name}-hub', daemon=True)
            self._thread.start()

    def _loop(self):
        while True:
            self._wake.wait(self.interval)
            self._wake.clear()
            with self._lock:
                if not self.clients:
                    self._thread = None            # nobody connected: stop, and start again on the next connection
                    return
            try:
                text = self._compute_json()
            except Exception:
                time.sleep(self.interval)
                continue
            if text != self.last:
                self.last = text
                self._broadcast(text)


def attach(app, compute, can_connect, interval=3.0, name='live', path='/ws/live'):
    """Adds the WebSocket route to a Flask app and returns the hub.
    can_connect() -> bool decides who may listen (it runs with the request's session, like any other route)."""
    from flask_sock import Sock
    sock = Sock(app)
    hub = LiveHub(app, compute, interval=interval, name=name)

    @sock.route(path)
    def _ws(ws):
        from flask import request
        if not request.environ.get('wsgi.multithread'):
            # A server running one request at a time (plain gunicorn "sync" worker) would be frozen by this open
            # socket: every other request, like saving an order status, would wait behind it. Hand the page back
            # to its slow poll instead; 4000 tells the page not to keep retrying.
            ws.close(reason=4000, message='Server has no threads')
            return
        if not can_connect():
            ws.close(reason=1008, message='Not allowed')
            return
        hub.add(ws)
        try:
            ws.send(hub.snapshot())
            while True:
                msg = ws.receive(timeout=40)     # the page pings every ~25 s so idle proxies don't drop the socket
                if msg is None:
                    continue
                if msg == 'ping':
                    ws.send('pong')
        except Exception:
            pass                                  # closed by the browser or the network
        finally:
            hub.remove(ws)

    @app.after_request
    def _nudge_after_change(resp):
        """Something in THIS app was just saved (a status, a booking, a payment...): push now, don't wait for the next 3 s tick."""
        from flask import request
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and resp.status_code < 400 and hub.clients:
            hub.nudge()
        return resp

    return hub
