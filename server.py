#!/usr/bin/env python3
"""
HTTP/1.1 keep-alive calculator.            Author: Parvam Shah

A calculator served over persistent HTTP/1.1 connections, written directly on
a TCP socket. Standard library only: no framework and no http.server.

    GET /add?a=2&b=3   -> 200  5
    GET /sub?a=10&b=4  -> 200  6
    GET /mul?a=6&b=7   -> 200  42
    GET /div?a=9&b=3   -> 200  3
    GET /div?a=1&b=0   -> 400
    GET /pow?a=2&b=8   -> 404
    POST /add          -> 405

How it is put together:

    parse_request()   finds where the request at the front of a buffer ends
    respond()         turns one request into a status and a body
    Connection.run()  the loop: receive, parse as many requests as are
                      complete, answer each, keep the socket open

The server never guesses where a request ends. If the framing of a request
cannot be trusted, it answers and closes the connection.
"""

import argparse
import re
import socket
import sys
import threading
import time
from decimal import Context, Decimal
from email.utils import formatdate
from urllib.parse import parse_qsl, urlsplit

SERVER_NAME = "keepalive-calc/1.0"

MAX_HEAD = 8 * 1024        # request line + headers
MAX_BODY = 1024 * 1024     # decoded request body
MAX_BUFFER = 4 * MAX_BODY  # raw bytes held for one unfinished request
MAX_REQUESTS = 100         # requests served on one connection
IDLE_TIMEOUT = 10.0        # seconds; see the README for why

REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    413: "Content Too Large",
    431: "Request Header Fields Too Large",
    501: "Not Implemented",
    505: "HTTP Version Not Supported",
}


class HttpError(Exception):
    """An error response. fatal=True means the request's framing is unknown,
    so the connection must be closed after the response is sent."""

    def __init__(self, status, detail, fatal=False, headers=()):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.fatal = fatal
        self.headers = list(headers)


class Request:
    def __init__(self, method, target, version, headers, body):
        self.method = method
        self.target = target
        self.version = version    # (major, minor)
        self.headers = headers    # lowercase name -> list of values
        self.body = body

    def tokens(self, name):
        """The comma-separated tokens of a list-valued header, lowercased."""
        return {t.strip().lower() for v in self.headers.get(name, []) for t in v.split(",")}

    @property
    def keep_alive(self):
        if self.version >= (1, 1):
            return "close" not in self.tokens("connection")
        return "keep-alive" in self.tokens("connection")


# ------------------------------------------------------------------- parsing

TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
HEAD_END = re.compile(rb"\r?\n\r?\n")
LINE_END = re.compile(r"\r?\n")
REQUEST_LINE = re.compile(r"(%s) (\S+) HTTP/([0-9])\.([0-9])" % TOKEN)
FIELD_LINE = re.compile(r"(%s):[ \t]*([^\r\n\x00]*?)[ \t]*" % TOKEN)
CHUNK_SIZE = re.compile(r"[0-9A-Fa-f]{1,8}")
DIGITS = re.compile(r"[0-9]+")


def parse_request(buf):
    """Look for one complete request at the front of buf.

    Returns (Request, n) where n is the number of bytes that request occupies,
    or None if more bytes are needed. Nothing is removed from buf: the caller
    deletes buf[:n], and whatever follows is the start of the next request.
    """
    end = HEAD_END.search(buf)
    if end is None:
        if len(buf) > MAX_HEAD:
            raise HttpError(431, "request head is larger than %d bytes" % MAX_HEAD, fatal=True)
        return None
    if end.start() > MAX_HEAD:
        raise HttpError(431, "request head is larger than %d bytes" % MAX_HEAD, fatal=True)

    lines = LINE_END.split(bytes(buf[:end.start()]).decode("latin-1"))
    first = REQUEST_LINE.fullmatch(lines[0])
    if first is None:
        raise HttpError(400, "malformed request line", fatal=True)
    method, target = first.group(1), first.group(2)
    version = (int(first.group(3)), int(first.group(4)))
    if version[0] != 1:
        raise HttpError(505, "only HTTP/1.x is supported", fatal=True)

    headers = {}
    for line in lines[1:]:
        if line[:1] in (" ", "\t"):
            raise HttpError(400, "obsolete header line folding", fatal=True)
        field = FIELD_LINE.fullmatch(line)
        if field is None:
            raise HttpError(400, "malformed header line", fatal=True)
        headers.setdefault(field.group(1).lower(), []).append(field.group(2))

    framed = read_body(buf, end.end(), headers, version)
    if framed is None:
        return None
    body, stop = framed
    return Request(method, target, version, headers, body), stop


def read_body(buf, pos, headers, version):
    """Work out how the body is framed and return (body, end offset), or None."""
    lengths = headers.get("content-length")
    encodings = headers.get("transfer-encoding")

    if encodings is not None:
        if lengths is not None:
            # Two answers to "where does this request end" is how request
            # smuggling works. Refuse to pick one.
            raise HttpError(400, "both Content-Length and Transfer-Encoding", fatal=True)
        if version < (1, 1):
            raise HttpError(400, "Transfer-Encoding in an HTTP/1.0 request", fatal=True)
        codings = [c.strip().lower() for c in ",".join(encodings).split(",")]
        if codings != ["chunked"]:
            raise HttpError(501, "only the chunked transfer coding is supported", fatal=True)
        return read_chunked(buf, pos)

    if lengths is None:
        return b"", pos
    if len(set(lengths)) != 1 or DIGITS.fullmatch(lengths[0]) is None:
        raise HttpError(400, "invalid Content-Length", fatal=True)
    length = int(lengths[0])
    if length > MAX_BODY:
        raise HttpError(413, "body is larger than %d bytes" % MAX_BODY, fatal=True)
    if len(buf) - pos < length:
        return None
    return bytes(buf[pos:pos + length]), pos + length


def read_line(buf, pos, limit=1024):
    """Return (line, next offset), or (None, pos) if the line is not complete."""
    nl = buf.find(b"\n", pos)
    if nl < 0:
        if len(buf) - pos > limit:
            raise HttpError(400, "line too long in chunked body", fatal=True)
        return None, pos
    if nl - pos > limit:
        raise HttpError(400, "line too long in chunked body", fatal=True)
    return bytes(buf[pos:nl]).decode("latin-1").rstrip("\r"), nl + 1


def read_chunked(buf, pos):
    """Decode a chunked body starting at pos. Returns (body, end offset) or None."""
    body = bytearray()
    while True:
        line, pos = read_line(buf, pos)
        if line is None:
            return None
        size_text = line.split(";", 1)[0].strip()   # drop any chunk extension
        if CHUNK_SIZE.fullmatch(size_text) is None:
            raise HttpError(400, "invalid chunk size", fatal=True)
        size = int(size_text, 16)
        if size == 0:
            break
        if len(body) + size > MAX_BODY:
            raise HttpError(413, "body is larger than %d bytes" % MAX_BODY, fatal=True)
        if len(buf) - pos < size + 1:
            return None
        body += buf[pos:pos + size]
        pos += size
        if buf[pos:pos + 2] == b"\r\n":
            pos += 2
        elif buf[pos:pos + 1] == b"\n":
            pos += 1
        elif bytes(buf[pos:]) == b"\r":
            return None                              # the LF has not arrived yet
        else:
            raise HttpError(400, "chunk data is not followed by CRLF", fatal=True)

    trailer_bytes = 0
    while True:                                      # trailer section, then a blank line
        line, pos = read_line(buf, pos)
        if line is None:
            return None
        if line == "":
            return bytes(body), pos
        trailer_bytes += len(line)
        if trailer_bytes > MAX_HEAD:
            raise HttpError(431, "trailer section too large", fatal=True)


# ---------------------------------------------------------------- calculator

# Plain decimal notation only. No exponents, so the size of a result is
# bounded by the size of the inputs, and no "nan" or "inf".
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
MAX_NUMBER_CHARS = 64

EXACT = Context(prec=4 * MAX_NUMBER_CHARS)   # wide enough that + - * never round
DIVISION = Context(prec=28)

OPERATIONS = {
    "/add": lambda a, b: EXACT.add(a, b),
    "/sub": lambda a, b: EXACT.subtract(a, b),
    "/mul": lambda a, b: EXACT.multiply(a, b),
    "/div": lambda a, b: DIVISION.divide(a, b),
}


def parse_number(name, text):
    text = text.strip()
    if len(text) > MAX_NUMBER_CHARS or NUMBER.fullmatch(text) is None:
        raise HttpError(400, "%s must be a number such as 7, -3 or 2.5" % name)
    return Decimal(text)


def format_number(value):
    if value == 0:
        return "0"                                   # never "-0" or "0.00"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def calculate(path, query):
    values = {"a": [], "b": []}
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key in values:
            values[key].append(value)
    if len(values["a"]) != 1 or len(values["b"]) != 1:
        raise HttpError(400, "give exactly one a and one b, as in %s?a=2&b=3" % path)
    a = parse_number("a", values["a"][0])
    b = parse_number("b", values["b"][0])
    if path == "/div" and b == 0:
        raise HttpError(400, "division by zero")
    return format_number(OPERATIONS[path](a, b))


def respond(request):
    """Answer one well-framed request. Returns (status, body text, extra headers).

    Every error raised here is non-fatal: the request was read completely, so
    the next one starts in the right place and the connection can stay open.
    """
    if request.version >= (1, 1) and len(request.headers.get("host", [])) != 1:
        raise HttpError(400, "an HTTP/1.1 request needs exactly one Host header")
    try:
        url = urlsplit(request.target)
    except ValueError:
        raise HttpError(400, "malformed request target") from None
    if not url.path.startswith("/"):
        raise HttpError(400, "malformed request target")
    if url.path not in OPERATIONS:
        raise HttpError(404, "no such operation; try /add, /sub, /mul or /div")
    if request.method not in ("GET", "HEAD"):
        raise HttpError(405, "use GET", headers=[("Allow", "GET, HEAD")])
    return 200, calculate(url.path, url.query) + "\n", []


def error_body(error):
    return "%d %s: %s\n" % (error.status, REASONS[error.status], error.detail)


def build_response(status, text, extra=(), close=False, head_only=False, keep_alive_hint=None):
    body = text.encode("utf-8")
    lines = [
        "HTTP/1.1 %d %s" % (status, REASONS[status]),
        "Date: " + formatdate(usegmt=True),
        "Server: " + SERVER_NAME,
        "Content-Type: text/plain; charset=utf-8",
        # Always present: it is how the client finds the end of this response
        # on a connection that is not going to be closed.
        "Content-Length: %d" % len(body),
    ]
    lines += ["%s: %s" % pair for pair in extra]
    if close:
        lines.append("Connection: close")
    else:
        lines.append("Connection: keep-alive")
        if keep_alive_hint:
            lines.append("Keep-Alive: " + keep_alive_hint)
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    return head if head_only else head + body


# ---------------------------------------------------------------- connection

class Connection:
    def __init__(self, server, sock, peer):
        self.server = server
        self.sock = sock
        self.peer = "%s:%s" % peer[:2]
        self.buf = bytearray()
        self.served = 0

    def log(self, message):
        if self.server.verbose:
            print("[%s] %s" % (self.peer, message), file=sys.stderr, flush=True)

    def run(self):
        self.log("connected")
        try:
            self.loop()
        except OSError as exc:
            self.log("socket error: %s" % exc)
        finally:
            self.sock.close()
            self.log("closed after %d request(s)" % self.served)

    def loop(self):
        partial_since = None   # when the unfinished request in buf started arriving
        while True:
            # Answer every request that is already complete in the buffer.
            # With pipelining this runs several times per recv().
            while True:
                while self.buf[:1] in (b"\r", b"\n"):
                    del self.buf[:1]                 # stray blank lines between requests
                try:
                    parsed = parse_request(self.buf)
                except HttpError as error:
                    self.finish(error)
                    return
                if parsed is None:
                    break
                request, used = parsed
                del self.buf[:used]                  # this request and not one byte more
                partial_since = None
                if not self.answer(request):
                    return

            if len(self.buf) > MAX_BUFFER:
                self.finish(HttpError(413, "request is too large", fatal=True))
                return

            # Wait for more bytes. An idle connection gets the idle timeout per
            # wait; a half-received request gets one deadline for the whole
            # request, so trickling a byte at a time does not hold the socket.
            if self.buf:
                if partial_since is None:
                    partial_since = time.monotonic()
                wait = partial_since + self.server.idle_timeout - time.monotonic()
            else:
                partial_since = None
                wait = self.server.idle_timeout
            data = b""
            if wait > 0:
                self.sock.settimeout(wait)
                try:
                    data = self.sock.recv(65536)
                except socket.timeout:
                    wait = 0
            if wait <= 0:
                if self.buf:
                    self.finish(HttpError(408, "request not completed in time", fatal=True))
                else:
                    self.log("idle timeout")
                return
            if not data:
                self.log("client closed the connection")
                return
            self.buf += data

    def answer(self, request):
        """Send the response to one request. Returns False if the connection should close."""
        self.served += 1
        keep = (request.keep_alive and self.served < self.server.max_requests
                and not self.server.stopping)
        try:
            status, text, extra = respond(request)
        except HttpError as error:
            status, text, extra = error.status, error_body(error), error.headers
        hint = "timeout=%d, max=%d" % (self.server.idle_timeout,
                                       self.server.max_requests - self.served)
        self.sock.settimeout(self.server.idle_timeout)
        self.sock.sendall(build_response(status, text, extra, close=not keep,
                                         head_only=request.method == "HEAD", keep_alive_hint=hint))
        self.log('"%s %s" %d%s' % (request.method, request.target, status,
                                   "" if keep else " (closing)"))
        if not keep:
            self.linger()
        return keep

    def finish(self, error):
        """Send a fatal error and close: the next request boundary is unknown."""
        self.log("%d %s; closing" % (error.status, error.detail))
        self.sock.settimeout(self.server.idle_timeout)
        self.sock.sendall(build_response(error.status, error_body(error), error.headers, close=True))
        self.linger()

    def linger(self):
        """Close our side, then read briefly. Closing a socket that still has
        unread input makes TCP send a reset, which can destroy the response we
        just wrote before the client has read it."""
        try:
            self.sock.shutdown(socket.SHUT_WR)
            self.sock.settimeout(0.5)
            drained = 0
            while drained < 256 * 1024:
                data = self.sock.recv(65536)
                if not data:
                    break
                drained += len(data)
        except OSError:
            pass


class CalcServer:
    def __init__(self, host=None, port=8080, idle_timeout=IDLE_TIMEOUT,
                 max_requests=MAX_REQUESTS, verbose=False):
        self.idle_timeout = idle_timeout
        self.max_requests = max_requests
        self.verbose = verbose
        self.stopping = False
        self.connections = 0     # TCP connections accepted so far
        self.listeners = []
        self.skipped = []        # (address, reason) for loopback addresses we could not bind

        # By default listen on both loopback addresses, because "localhost"
        # resolves to ::1 first on some systems and to 127.0.0.1 on others.
        for address in ([host] if host else ["127.0.0.1", "::1"]):
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            try:
                listener = socket.create_server((address, port), family=family)
            except OSError as exc:
                if not self.listeners:
                    raise
                self.skipped.append((address, exc.strerror or str(exc)))
                continue
            listener.settimeout(0.2)
            port = listener.getsockname()[1]         # port 0: reuse the port we were given
            self.listeners.append(listener)
        self.host = host or "127.0.0.1"
        self.port = port

    def accept_loop(self, listener):
        while not self.stopping:
            try:
                sock, peer = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections += 1
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # One thread per connection, so a slow client only delays itself.
            threading.Thread(target=Connection(self, sock, peer).run, daemon=True).start()
        listener.close()

    def serve_forever(self):
        for listener in self.listeners[1:]:
            threading.Thread(target=self.accept_loop, args=(listener,), daemon=True).start()
        self.accept_loop(self.listeners[0])

    def start(self):
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread

    def stop(self):
        self.stopping = True


def main():
    parser = argparse.ArgumentParser(description="HTTP/1.1 keep-alive calculator")
    parser.add_argument("--host", help="address to listen on (default: 127.0.0.1 and ::1)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT, metavar="SECONDS")
    parser.add_argument("-v", "--verbose", action="store_true", help="log connections and requests")
    args = parser.parse_args()

    try:
        server = CalcServer(args.host, args.port, args.idle_timeout, verbose=args.verbose)
    except OSError as exc:
        sys.exit("cannot listen on port %d: %s" % (args.port, exc.strerror or exc))
    where = " and ".join("%s:%d" % (("[%s]" if ":" in l.getsockname()[0] else "%s") % l.getsockname()[0],
                                    server.port) for l in server.listeners)
    print("listening on %s  (idle timeout %gs, Ctrl-C to stop)" % (where, server.idle_timeout), flush=True)
    for address, reason in server.skipped:
        print("warning: could not listen on [%s]:%d (%s). If another program owns that address,\n"
              "         clients that connect to \"localhost\" may reach it instead of this server;\n"
              "         use 127.0.0.1 or pick another --port."
              % (address, server.port, reason), file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
