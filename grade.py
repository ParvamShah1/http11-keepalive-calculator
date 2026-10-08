#!/usr/bin/env python3
"""
The marking check: six requests down ONE socket.      Author: Parvam Shah

    python3 server.py            # terminal 1
    python3 grade.py [port]      # terminal 2

It opens a single TCP connection, sends each request only after reading the
previous response, and finds the end of every response from Content-Length.
If the server closed the socket after any response, the next request fails,
so passing proves the connection was kept alive.
"""

import socket
import sys

HOST = "localhost"

CASES = [
    # method, target, expected status, expected body (None = any)
    ("GET", "/add?a=2&b=3", 200, "5"),
    ("GET", "/sub?a=10&b=4", 200, "6"),
    ("GET", "/mul?a=6&b=7", 200, "42"),
    ("GET", "/div?a=1&b=0", 400, None),
    ("GET", "/pow?a=2&b=8", 404, None),
    ("POST", "/add", 405, None),
]


class Reader:
    """Reads responses off a socket, keeping whatever arrives early."""

    def __init__(self, sock):
        self.sock = sock
        self.pending = b""

    def _fill(self):
        data = self.sock.recv(4096)
        if not data:
            raise ConnectionError("the server closed the connection")
        self.pending += data

    def response(self):
        while b"\r\n\r\n" not in self.pending:
            self._fill()
        head, self.pending = self.pending.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers["content-length"])
        while len(self.pending) < length:
            self._fill()
        body, self.pending = self.pending[:length], self.pending[length:]
        return status, headers, body.decode("utf-8")


def still_open(sock):
    """True unless the peer has closed: a closed socket reads as b"" at once."""
    sock.setblocking(False)
    try:
        return sock.recv(1, socket.MSG_PEEK) != b""
    except BlockingIOError:
        return True
    except OSError:
        return False


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    try:
        sock = socket.create_connection((HOST, port), timeout=5)
    except OSError as exc:
        sys.exit("cannot connect to %s:%d (%s) - is server.py running?" % (HOST, port, exc))
    reader = Reader(sock)
    print("connected to %s:%d from local port %d\n" % (HOST, port, sock.getsockname()[1]))

    passed = 0
    for number, (method, target, want_status, want_body) in enumerate(CASES, 1):
        extra = "Content-Length: 0\r\n" if method == "POST" else ""
        sock.sendall(("%s %s HTTP/1.1\r\nHost: %s\r\n%s\r\n" % (method, target, HOST, extra)).encode())
        try:
            status, _, body = reader.response()
        except (ConnectionError, OSError) as exc:
            print("  %d. %-4s %-15s -> no response: %s" % (number, method, target, exc))
            break
        body = body.strip()
        ok = status == want_status and (want_body is None or body == want_body)
        passed += ok
        shown = body if status == 200 else ""
        note = "" if ok else "   <-- expected %d %s" % (want_status, want_body or "")
        print("  %d. %-4s %-15s -> %d  %s%s" % (number, method, target, status, shown, note))

    open_now = still_open(sock)
    sock.close()
    print("\n  socket still open after the last response: %s" % ("yes" if open_now else "NO"))
    print("  %d/%d responses correct, all on 1 TCP connection" % (passed, len(CASES)))
    good = passed == len(CASES) and open_now
    print("  " + ("PASS" if good else "FAIL"))
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
