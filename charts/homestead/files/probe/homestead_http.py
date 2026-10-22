"""Bounded HTTP connections and deadlines, including slow trickle clients.

Two limits: MAX_CONNECTIONS requests may be handled at once (a request holds
its slot until it is answered, so consoles and relays count for as long as
they run), and a server keeps at most four times as many connections open. An
idle keep-alive connection holds no request slot. When a limit is reached the
client gets 503 with Retry-After, not a closed socket, and when most of the
open connections are in use idle ones are let go after BUSY_IDLE_SECONDS."""
import selectors
import socket
import re
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IDLE_SECONDS = 15
BUSY_IDLE_SECONDS = 2
BUSY_SHARE = 0.75       # share of max_open in use from which idle connections are let go sooner
HEADER_SECONDS = 15
BODY_SECONDS = 30
MAX_CONNECTIONS = 64
REFUSED = (b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 1\r\nContent-Length: 0\r\n"
           b"Connection: close\r\n\r\n")


@contextmanager
def deadline(connection, seconds):
    def expire():
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, max_connections=MAX_CONNECTIONS, max_open=None, **kwargs):
        self._slots = threading.BoundedSemaphore(max_connections)
        self.max_open = max_open or 4 * max_connections
        self._count = threading.Lock()
        self.open = 0       # connections open, idle or not
        self.active = 0     # requests being handled
        # Readable while the server is busy, so idle waits wake once when it
        # becomes busy instead of polling for it.
        self.pressure, self._pressure_w = socket.socketpair()
        self.pressure.setblocking(False)
        self._busy = False
        super().__init__(*args, **kwargs)

    def server_close(self):
        super().server_close()
        for end in (self.pressure, self._pressure_w):
            try:
                end.close()
            except OSError:
                pass

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(IDLE_SECONDS)
        return connection, address

    def idle_timeout(self):
        """How long an idle keep-alive connection may wait for its next request."""
        return BUSY_IDLE_SECONDS if self.open >= BUSY_SHARE * self.max_open else IDLE_SECONDS

    def _counted(self, change):
        """Change the open count and keep the pressure signal in step (holds _count)."""
        self.open += change
        busy = self.open >= BUSY_SHARE * self.max_open
        if busy == self._busy:
            return
        self._busy = busy
        try:
            if busy:
                self._pressure_w.send(b"!")
            else:
                self.pressure.recv(16)
        except OSError:
            pass

    def take_slot(self):
        if not self._slots.acquire(blocking=False):
            return False
        with self._count:
            self.active += 1
        return True

    def give_slot(self):
        with self._count:
            self.active -= 1
        self._slots.release()

    def _refuse(self, request):
        try:
            request.settimeout(0.5)
            request.sendall(REFUSED)
        except OSError:
            pass
        self.shutdown_request(request)

    def process_request(self, request, client_address):
        with self._count:
            full = self.open >= self.max_open
            if not full:
                self._counted(+1)
        if full:
            self._refuse(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._count:
                self._counted(-1)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._count:
                self._counted(-1)


class LimitedHandler(BaseHTTPRequestHandler):
    """For standalone helpers: header deadline plus strict small body framing.

    after_request, when set, is called once per request whose request line
    arrived, after its answer is sent and its slot given back, with _status
    (the status sent, None if its headers never came) and _started set; what
    it raises is ignored. It must return at once: a connection refused at the
    cap, a request line too long or never sent reaches no hook. Everything known
    about a request is cleared before the next one on a kept-alive connection,
    so nothing from one request is read as the next one's."""
    timeout = IDLE_SECONDS
    max_body = 4096
    _slot = False
    _served = False
    after_request = None
    _status = _started = None
    PER_REQUEST = ("user", "role", "_access", "_status", "_started", "_raw", "_fleet_from", "api_key",
                   "headers", "path")

    def _next_request_arrives(self):
        """Wait, holding no request slot, for the client's next request on a
        kept-alive connection. One wait for the whole allowance; it wakes early
        only if the server becomes busy, and then shortens to the busy
        allowance. A selector (epoll/poll), not select(), so descriptor numbers
        above 1024 work. (Pipelined requests already read into the buffer are
        not seen here; browsers do not pipeline.)"""
        idle = getattr(self.server, "idle_timeout", None)
        if idle is None:
            return True
        started = time.monotonic()
        with selectors.DefaultSelector() as waiting:
            try:
                waiting.register(self.connection, selectors.EVENT_READ)
                waiting.register(self.server.pressure, selectors.EVENT_READ)
            except (OSError, ValueError, KeyError):
                return False
            while True:
                left = idle() - (time.monotonic() - started)
                if left <= 0:
                    return False
                ready = {key.fileobj for key, _ in waiting.select(left)}
                if self.connection in ready:
                    return True
                if self.server.pressure in ready:   # busy now: stop watching, shorter allowance
                    waiting.unregister(self.server.pressure)

    def handle_one_request(self):
        # The first request's header deadline counts from the connection's
        # opening; only later requests on a kept-alive connection wait idle.
        if self._served and not self._next_request_arrives():
            self.close_connection = True
            return
        self._served = True
        for name in self.PER_REQUEST:
            setattr(self, name, None)
        self._header_deadline = deadline(self.connection, HEADER_SECONDS)
        self._header_deadline.__enter__()
        try:
            super().handle_one_request()
        except OSError:
            self.close_connection = True
        finally:
            if self._header_deadline:
                self._header_deadline.__exit__(None, None, None)
            if self._slot:
                self._slot = False
                self.server.give_slot()
            if self.after_request is not None and self._started is not None:
                try:
                    self.after_request()
                except Exception:
                    pass    # a hook never changes what was answered

    def _take_slot(self):
        """A request holds a slot until it is answered; with none free it gets 503."""
        take = getattr(self.server, "take_slot", None)
        if take is None or take():
            self._slot = take is not None
            return True
        self.send_response(503)
        self.send_header("Retry-After", "1")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        return False

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def parse_request(self):
        self._started = time.monotonic()
        valid = super().parse_request()
        self._header_deadline.__exit__(None, None, None)
        self._header_deadline = None
        if not valid:
            return False
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") or len(lengths) > 1 or
                lengths and not re.fullmatch(r"[0-9]+", lengths[0])):
            self.send_error(400, "invalid request framing")
            self.close_connection = True
            return False
        if lengths and int(lengths[0]) > self.max_body:
            self.send_error(413)
            self.close_connection = True
            return False
        if self.command not in ("POST", "PUT", "PATCH") and lengths and int(lengths[0]):
            self.send_error(400, "unexpected request body")
            self.close_connection = True
            return False
        return self._take_slot()
