"""Connection slots (#705): an idle keep-alive connection does not hold one of the
server's request slots, a full server answers 503 instead of closing silently,
and idle connections are let go sooner when the server is busy."""
import os
import resource
import socket
import sys
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_http as http


class Handler(http.LimitedHandler):
    protocol_version = "HTTP/1.1"
    release = None      # a threading.Event; /slow waits on it

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/boom":
            raise RuntimeError("handler failed")
        if self.path == "/slow":
            self.release.wait(5)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SlotTests(unittest.TestCase):
    def serve(self, **limits):
        Handler.release = threading.Event()
        self.addCleanup(Handler.release.set)
        listener = http.BoundedHTTPServer(("127.0.0.1", 0), Handler, **limits)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        return listener

    def client(self, listener):
        connection = HTTPConnection(*listener.server_address, timeout=3)
        self.addCleanup(connection.close)
        return connection

    def get(self, connection, path="/"):
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.getheader("Retry-After"), response.read()

    def until(self, check, seconds=2):
        end = time.monotonic() + seconds
        while not check() and time.monotonic() < end:
            time.sleep(0.01)
        return check()

    def test_idle_keep_alive_connections_do_not_hold_request_slots(self):
        listener = self.serve(max_connections=2)
        idle = [self.client(listener) for _ in range(3)]
        for connection in idle:     # each makes a request, then stays open and idle
            self.assertEqual(200, self.get(connection)[0])
        self.assertEqual((200, None, b"ok"), self.get(self.client(listener)))

    def test_a_server_whose_request_slots_are_all_busy_answers_503(self):
        listener = self.serve(max_connections=2)
        busy = [threading.Thread(target=self.get, args=(self.client(listener), "/slow"), daemon=True) for _ in range(2)]
        for thread in busy:
            thread.start()
        self.assertTrue(self.until(lambda: listener.active == 2))
        status, retry, _ = self.get(self.client(listener))
        self.assertEqual(503, status)
        self.assertTrue(retry)
        Handler.release.set()
        for thread in busy:
            thread.join(3)
        self.assertTrue(self.until(lambda: listener.active == 0), "slots come back once requests finish")
        self.assertEqual(200, self.get(self.client(listener))[0])

    def test_too_many_open_connections_get_503_not_a_silent_close(self):
        listener = self.serve(max_connections=2, max_open=3)
        held = [socket.create_connection(listener.server_address, timeout=3) for _ in range(3)]
        for sock in held:
            self.addCleanup(sock.close)
        self.assertTrue(self.until(lambda: listener.open == 3))
        with socket.create_connection(listener.server_address, timeout=3) as excess:
            reply = excess.recv(4096)
        self.assertIn(b" 503 ", reply.split(b"\r\n", 1)[0])
        self.assertIn(b"Retry-After:", reply)

    def test_a_client_that_sends_its_request_first_still_gets_503_when_full(self):
        listener = self.serve(max_connections=2, max_open=2)
        held = [socket.create_connection(listener.server_address, timeout=3) for _ in range(2)]
        for sock in held:
            self.addCleanup(sock.close)
        self.assertTrue(self.until(lambda: listener.open == 2))
        with socket.create_connection(listener.server_address, timeout=3) as excess:
            excess.sendall(b"GET / HTTP/1.1\r\nHost: fixture\r\n\r\n")
            reply = excess.recv(4096)
        self.assertIn(b" 503 ", reply.split(b"\r\n", 1)[0])

    def test_a_request_whose_handler_fails_gives_its_slot_back(self):
        listener = self.serve(max_connections=1)
        with socket.create_connection(listener.server_address, timeout=3) as sock:
            sock.sendall(b"GET /boom HTTP/1.1\r\nHost: fixture\r\n\r\n")
            sock.recv(4096)
        self.assertTrue(self.until(lambda: listener.active == 0))
        self.assertEqual(200, self.get(self.client(listener))[0])

    def test_keep_alive_works_when_descriptors_are_numbered_above_1024(self):
        # select() cannot watch descriptor numbers of 1024 and above; a busy
        # server reaches them, and the next request must still be answered.
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 1200:
            self.skipTest("this machine allows fewer than 1200 open files")
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 1200), hard))
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        filler = [os.open(os.devnull, os.O_RDONLY) for _ in range(1100)]
        self.addCleanup(lambda: [os.close(fd) for fd in filler])
        listener = self.serve(max_connections=2)
        connection = self.client(listener)
        self.assertEqual(200, self.get(connection)[0])
        self.assertEqual(200, self.get(connection)[0], "the second request on the connection is answered")

    def test_idle_connections_are_let_go_sooner_when_most_are_open(self):
        listener = self.serve(max_connections=4, max_open=4)
        self.assertEqual(http.IDLE_SECONDS, listener.idle_timeout())
        with mock.patch.object(http, "BUSY_IDLE_SECONDS", 0.2):
            held = [self.client(listener) for _ in range(3)]
            for connection in held:
                self.assertEqual(200, self.get(connection)[0])
            self.assertTrue(self.until(lambda: listener.open == 3))
            self.assertEqual(0.2, listener.idle_timeout())
            # Idle connections are let go until the server is no longer busy;
            # the ones left then get the normal wait again.
            self.assertTrue(self.until(lambda: listener.open < http.BUSY_SHARE * listener.max_open, 3),
                            "idle connections close after the short wait while the server is busy")
            self.assertEqual(http.IDLE_SECONDS, listener.idle_timeout())


if __name__ == "__main__":
    unittest.main()
