#!/usr/bin/env python3
"""
Tests for the keep-alive calculator.                  Author: Parvam Shah

    python3 -m unittest -v

Each test class starts a real server on a free port and talks to it over real
sockets, so what is tested is the bytes on the wire.
"""

import os
import socket
import subprocess
import sys
import threading
import time
import unittest

import server as calc

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 0.6   # idle timeout used by the test servers


class Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.pending = b""

    def send(self, data):
        self.sock.sendall(data.encode("latin-1") if isinstance(data, str) else data)

    def get(self, target, extra=""):
        self.send("GET %s HTTP/1.1\r\nHost: t\r\n%s\r\n" % (target, extra))
        return self.read()

    def _fill(self):
        data = self.sock.recv(65536)
        if not data:
            raise EOFError("connection closed")
        self.pending += data

    def read(self, head_only=False):
        """Read exactly one response: (status, headers, body text)."""
        while b"\r\n\r\n" not in self.pending:
            self._fill()
        head, self.pending = self.pending.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.lower()] = value.strip()
        length = 0 if head_only else int(headers["content-length"])
        while len(self.pending) < length:
            self._fill()
        body, self.pending = self.pending[:length], self.pending[length:]
        return status, headers, body.decode("utf-8")

    def is_closed(self, wait=3.0):
        """True if the server closes the socket within `wait` seconds."""
        if self.pending:
            return False
        self.sock.settimeout(wait)
        try:
            data = self.sock.recv(1)
        except socket.timeout:
            return False
        except OSError:
            return True
        self.pending += data
        return data == b""

    def close(self):
        self.sock.close()


class ServerCase(unittest.TestCase):
    max_requests = calc.MAX_REQUESTS

    @classmethod
    def setUpClass(cls):
        cls.server = calc.CalcServer(port=0, idle_timeout=TIMEOUT, max_requests=cls.max_requests)
        cls.server.start()
        cls.port = cls.server.port

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def client(self):
        c = Client(self.port)
        self.addCleanup(c.close)
        return c

    def result(self, target):
        status, _, body = self.client().get(target)
        return status, body.strip()

    def assert_fatal(self, raw, status):
        """`raw` must be answered with `status` and then the socket closed."""
        c = self.client()
        c.send(raw)
        got, headers, _ = c.read()
        self.assertEqual(got, status)
        self.assertEqual(headers["connection"], "close")
        self.assertTrue(c.is_closed(), "connection stayed open after a framing error")


class TestCalculator(ServerCase):
    MARKER = [
        ("GET", "/add?a=2&b=3", 200, "5"),
        ("GET", "/sub?a=10&b=4", 200, "6"),
        ("GET", "/mul?a=6&b=7", 200, "42"),
        ("GET", "/div?a=1&b=0", 400, None),
        ("GET", "/pow?a=2&b=8", 404, None),
        ("POST", "/add", 405, None),
    ]

    def test_marker_cases_share_one_connection(self):
        before = self.server.connections
        c = self.client()
        for method, target, want_status, want_body in self.MARKER:
            c.send("%s %s HTTP/1.1\r\nHost: t\r\n\r\n" % (method, target))
            status, headers, body = c.read()
            self.assertEqual(status, want_status, target)
            self.assertEqual(headers["connection"], "keep-alive")
            if want_body is not None:
                self.assertEqual(body.strip(), want_body)
        self.assertFalse(c.is_closed(wait=0.1))
        self.assertEqual(self.server.connections - before, 1)

    def test_grade_script_passes(self):
        run = subprocess.run([sys.executable, os.path.join(HERE, "grade.py"), str(self.port)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("PASS", run.stdout)

    def test_four_operations(self):
        self.assertEqual(self.result("/add?a=2&b=3"), (200, "5"))
        self.assertEqual(self.result("/sub?a=4&b=10"), (200, "-6"))
        self.assertEqual(self.result("/mul?a=-6&b=7"), (200, "-42"))
        self.assertEqual(self.result("/div?a=9&b=3"), (200, "3"))
        self.assertEqual(self.result("/add?b=3&a=2&unused=1"), (200, "5"))

    def test_decimal_arithmetic_is_exact(self):
        self.assertEqual(self.result("/add?a=0.1&b=0.2"), (200, "0.3"))
        self.assertEqual(self.result("/div?a=10&b=4"), (200, "2.5"))
        self.assertEqual(self.result("/mul?a=1.50&b=2"), (200, "3"))
        self.assertEqual(self.result("/sub?a=5&b=5.0"), (200, "0"))
        self.assertEqual(self.result("/mul?a=-1&b=0"), (200, "0"))
        self.assertEqual(self.result("/div?a=1&b=3"), (200, "0." + "3" * 28))
        big = 2 ** 200
        self.assertEqual(self.result("/mul?a=%d&b=%d" % (big, big)), (200, str(big * big)))

    def test_bad_arguments_are_400(self):
        for target in ["/div?a=1&b=0", "/div?a=0&b=0.0", "/add?a=x&b=3", "/add?a=2", "/add",
                       "/add?a=1&a=2&b=3", "/add?a=&b=3", "/add?a=nan&b=1", "/add?a=inf&b=1",
                       "/add?a=1e5&b=1", "/add?a=0x10&b=1", "/add?a=1_000&b=1",
                       "/add?a=%s&b=1" % ("9" * 65)]:
            self.assertEqual(self.result(target)[0], 400, target)

    def test_errors_do_not_close_the_connection(self):
        c = self.client()
        c.send("GET /add?a=x&b=1 HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertEqual(c.read()[0], 400)
        c.send("GET /nowhere HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertEqual(c.read()[0], 404)
        c.send("DELETE /add?a=1&b=1 HTTP/1.1\r\nHost: t\r\n\r\n")
        status, headers, _ = c.read()
        self.assertEqual((status, headers["allow"]), (405, "GET, HEAD"))
        c.send("GET /add?a=1&b=1 HTTP/1.1\r\n\r\n")                    # no Host
        self.assertEqual(c.read()[0], 400)
        c.send("GET /add?a=1&b=1 HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n")
        self.assertEqual(c.read()[0], 400)
        self.assertEqual(c.get("/add?a=20&b=22")[2], "42\n")

    def test_head_has_headers_but_no_body(self):
        c = self.client()
        c.send("HEAD /mul?a=6&b=7 HTTP/1.1\r\nHost: t\r\n\r\n")
        status, headers, _ = c.read(head_only=True)
        self.assertEqual((status, headers["content-length"]), (200, "3"))
        self.assertEqual(c.get("/add?a=1&b=1")[2], "2\n")   # nothing left over from HEAD

    def test_absolute_form_target(self):
        self.assertEqual(self.result("http://example.test/add?a=1&b=2"), (200, "3"))


class TestFraming(ServerCase):
    def test_body_is_consumed_exactly(self):
        # The body looks like a request. It must be treated as body, not answered.
        decoy = "GET /add?a=1&b=1 HTTP/1.1\r\nHost: t\r\n\r\n"
        c = self.client()
        c.send("POST /add HTTP/1.1\r\nHost: t\r\nContent-Length: %d\r\n\r\n%s" % (len(decoy), decoy)
               + "GET /mul?a=3&b=3 HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertEqual(c.read()[0], 405)
        self.assertEqual(c.read()[2], "9\n")
        self.assertFalse(c.is_closed(wait=0.1))

    def test_request_arriving_one_byte_at_a_time(self):
        c = self.client()
        for byte in b"GET /sub?a=9&b=4 HTTP/1.1\r\nHost: t\r\n\r\n":
            c.send(bytes([byte]))
        self.assertEqual(c.read()[2], "5\n")

    def test_boundary_in_the_middle_of_a_write(self):
        c = self.client()
        c.send("GET /add?a=1&b=1 HTTP/1.1\r\nHost: t\r\n\r\nGET /add?a=2")
        self.assertEqual(c.read()[2], "2\n")
        c.send("&b=2 HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertEqual(c.read()[2], "4\n")

    def test_bare_lf_and_blank_lines_between_requests(self):
        c = self.client()
        c.send("GET /add?a=1&b=2 HTTP/1.1\nHost: t\n\n")
        self.assertEqual(c.read()[2], "3\n")
        c.send("\r\n\r\nGET /add?a=2&b=2 HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertEqual(c.read()[2], "4\n")

    def test_duplicate_but_equal_content_length_is_fine(self):
        c = self.client()
        c.send("POST /add HTTP/1.1\r\nHost: t\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\nhi")
        self.assertEqual(c.read()[0], 405)
        self.assertEqual(c.get("/add?a=1&b=1")[0], 200)

    def test_untrustworthy_content_length_closes(self):
        for value in ["abc", "-1", "+5", "0x10", "1, 1", "", "1.0"]:
            self.assert_fatal("POST /add HTTP/1.1\r\nHost: t\r\nContent-Length: %s\r\n\r\nhello" % value, 400)
        self.assert_fatal("POST /add HTTP/1.1\r\nHost: t\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nab", 400)

    def test_content_length_with_transfer_encoding_closes(self):
        self.assert_fatal("POST /add HTTP/1.1\r\nHost: t\r\nContent-Length: 4\r\n"
                          "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n", 400)

    def test_malformed_head_closes(self):
        for raw in ["hello there\r\n\r\n",
                    "GET  /add?a=1&b=1 HTTP/1.1\r\nHost: t\r\n\r\n",
                    "GET /add?a=1&b=1 HTTP/1.1\r\nHost : t\r\n\r\n",
                    "GET /add?a=1&b=1 HTTP/1.1\r\nHost: t\r\n folded\r\n\r\n",
                    "GET /add?a=1&b=1 HTTP/1.1\r\nno colon here\r\n\r\n"]:
            self.assert_fatal(raw, 400)

    def test_unsupported_version(self):
        self.assert_fatal("GET /add?a=1&b=1 HTTP/2.0\r\nHost: t\r\n\r\n", 505)

    def test_oversized_head(self):
        self.assert_fatal("GET /add?a=1&b=1 HTTP/1.1\r\nHost: t\r\nX-Pad: %s\r\n\r\n" % ("p" * 9000), 431)
        self.assert_fatal("GET /" + "a" * 9000, 431)        # never even finishes its head

    def test_oversized_body(self):
        self.assert_fatal("POST /add HTTP/1.1\r\nHost: t\r\nContent-Length: %d\r\n\r\n"
                          % (calc.MAX_BODY + 1), 413)


class TestPersistence(ServerCase):
    def test_connection_close_is_honoured(self):
        c = self.client()
        status, headers, body = c.get("/add?a=1&b=1", "Connection: close\r\n")
        self.assertEqual((status, body, headers["connection"]), (200, "2\n", "close"))
        self.assertTrue(c.is_closed())

    def test_nothing_after_connection_close_is_processed(self):
        c = self.client()
        c.send("GET /add?a=1&b=1 HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
               "GET /add?a=5&b=5 HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertEqual(c.read()[2], "2\n")
        self.assertTrue(c.is_closed())

    def test_http10_closes_unless_asked_to_keep_alive(self):
        c = self.client()
        c.send("GET /add?a=1&b=1 HTTP/1.0\r\n\r\n")
        self.assertEqual(c.read()[2], "2\n")
        self.assertTrue(c.is_closed())

        c = self.client()
        for _ in range(2):
            c.send("GET /add?a=1&b=1 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
            status, headers, _ = c.read()
            self.assertEqual((status, headers["connection"]), (200, "keep-alive"))

    def test_idle_connection_is_closed_quietly(self):
        c = self.client()
        self.assertEqual(c.get("/add?a=1&b=1")[0], 200)
        started = time.monotonic()
        self.assertTrue(c.is_closed())           # EOF, with no response bytes first
        self.assertGreater(time.monotonic() - started, TIMEOUT * 0.7)

    def test_keep_alive_header_advertises_the_timeout(self):
        headers = self.client().get("/add?a=1&b=1")[1]
        self.assertIn("timeout=", headers["keep-alive"])

    def test_stalled_request_gets_408(self):
        c = self.client()
        c.send("GET /add?a=1&b=1 HTTP/1.1\r\nHost: t\r\n")     # never finished
        self.assertEqual(c.read()[0], 408)
        self.assertTrue(c.is_closed())

    def test_trickling_does_not_extend_the_deadline(self):
        # A byte every 0.1 s never lets a per-recv timeout fire. The request
        # as a whole has one deadline, so the 408 must still arrive on time.
        c = self.client()
        c.sock.settimeout(0.1)
        started = time.monotonic()
        status = None
        while status is None and time.monotonic() - started < 5:
            try:
                c.send("X")
                c._fill()
            except socket.timeout:
                continue
            except (EOFError, OSError):
                break
            if b"\r\n\r\n" in c.pending:
                status = c.read()[0]
        self.assertEqual(status, 408)
        self.assertLess(time.monotonic() - started, TIMEOUT * 3)

    def test_chunked_body(self):
        c = self.client()
        c.send("POST /add HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
               "5\r\nhello\r\n6;note=x\r\n world\r\n0\r\nX-Trailer: 1\r\n\r\n"
               "GET /add?a=7&b=8 HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertEqual(c.read()[0], 405)
        self.assertEqual(c.read()[2], "15\n")

    def test_chunked_body_split_everywhere(self):
        raw = ("POST /add HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
               "A\r\n0123456789\r\n3\r\nabc\r\n0\r\n\r\n"
               "GET /add?a=1&b=1 HTTP/1.1\r\nHost: t\r\n\r\n").encode()
        c = self.client()
        for byte in raw:
            c.send(bytes([byte]))
        self.assertEqual(c.read()[0], 405)
        self.assertEqual(c.read()[2], "2\n")

    def test_bad_chunked_framing_closes(self):
        head = "POST /add HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
        self.assert_fatal(head + "zz\r\nhello\r\n0\r\n\r\n", 400)
        self.assert_fatal(head + "-5\r\nhello\r\n0\r\n\r\n", 400)
        self.assert_fatal(head + "3\r\nhelloXX0\r\n\r\n", 400)
        self.assert_fatal("POST /add HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: gzip\r\n\r\n", 501)

    def test_pipelining_answers_in_order(self):
        c = self.client()
        c.send("".join("GET /mul?a=%d&b=%d HTTP/1.1\r\nHost: t\r\n\r\n" % (n, n) for n in range(50)))
        self.assertEqual([c.read()[2] for _ in range(50)], ["%d\n" % (n * n) for n in range(50)])

    def test_pipelined_errors_stay_in_step(self):
        c = self.client()
        c.send("GET /add?a=1&b=1 HTTP/1.1\r\nHost: t\r\n\r\n"
               "GET /div?a=1&b=0 HTTP/1.1\r\nHost: t\r\n\r\n"
               "POST /sub HTTP/1.1\r\nHost: t\r\nContent-Length: 3\r\n\r\nabc"
               "GET /pow?a=1&b=1 HTTP/1.1\r\nHost: t\r\n\r\n"
               "GET /sub?a=1&b=1 HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertEqual([c.read()[0] for _ in range(5)], [200, 400, 405, 404, 200])

    def test_clients_do_not_block_each_other(self):
        stuck = self.client()
        stuck.send("GET /add?a=1")                 # holds a half-finished request
        results = []

        def work(n):
            c = Client(self.port)
            results.append([c.get("/add?a=%d&b=%d" % (n, i))[2] for i in range(5)]
                           == ["%d\n" % (n + i) for i in range(5)])
            c.close()

        threads = [threading.Thread(target=work, args=(n,)) for n in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(results, [True] * 20)


class TestRequestLimit(ServerCase):
    max_requests = 3

    def test_connection_closes_after_the_last_allowed_request(self):
        c = self.client()
        seen = [c.get("/add?a=1&b=1")[1]["connection"] for _ in range(3)]
        self.assertEqual(seen, ["keep-alive", "keep-alive", "close"])
        self.assertTrue(c.is_closed())


if __name__ == "__main__":
    unittest.main(verbosity=2)
