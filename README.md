# HTTP/1.1 keep-alive calculator

**Author:** Parvam Shah · Network Architecture course project

A calculator you talk to over HTTP/1.1, where the TCP connection stays open between
requests. It is written directly on a `socket` with Python's standard library: a byte buffer
and a hand-written parser, with no framework and no `http.server`.

```
GET /add?a=2&b=3   -> 200  5
GET /sub?a=10&b=4  -> 200  6
GET /mul?a=6&b=7   -> 200  42
GET /div?a=9&b=3   -> 200  3

GET /div?a=1&b=0   -> 400      division by zero
GET /add?a=x&b=3   -> 400      not a number
GET /pow?a=2&b=8   -> 404      no such operation
POST /add          -> 405      wrong method
GET /add (no Host) -> 400      HTTP/1.1 requires Host
```

## Run it

Python 3.8 or newer. Nothing to install.

```sh
python3 server.py                       # listens on 127.0.0.1:8080 and [::1]:8080
python3 server.py -v                    # also log every connection and request
python3 server.py --port 9000 --idle-timeout 30
```

In a second terminal:

```sh
python3 grade.py                        # the marking check: six requests, one socket
python3 -m unittest -v                  # the full test suite (33 tests)
```

`grade.py` prints:

```
connected to localhost:8080 from local port 55523

  1. GET  /add?a=2&b=3    -> 200  5
  2. GET  /sub?a=10&b=4   -> 200  6
  3. GET  /mul?a=6&b=7    -> 200  42
  4. GET  /div?a=1&b=0    -> 400
  5. GET  /pow?a=2&b=8    -> 404
  6. POST /add            -> 405

  socket still open after the last response: yes
  6/6 responses correct, all on 1 TCP connection
  PASS
```

and with `-v` the server shows the same six requests arriving from one client port:

```
[::1:55523] connected
[::1:55523] "GET /add?a=2&b=3" 200
...
[::1:55523] "POST /add" 405
[::1:55523] closed after 6 request(s)
```

You can also see the reuse with curl, which reports how many new connections it opened:

```sh
curl -s -w ' [new connections: %{num_connects}]\n' \
  'http://localhost:8080/add?a=1&b=2' 'http://localhost:8080/mul?a=3&b=4'
# 3
#  [new connections: 1]
# 12
#  [new connections: 0]
```

## The real problem: finding where a request ends

In HTTP/1.0 the server closed the socket after each response, so "end of request" and "end
of connection" were the same thing. Once the connection is kept open, many requests arrive
as one continuous stream of bytes and TCP gives no hint where one stops. A single `recv()`
may return half a request, exactly one, or three and a half. The server has to work out the
boundaries from the bytes themselves.

Each connection has one `bytearray`, and the loop in `Connection.loop()` is:

1. `recv()` and append to the buffer.
2. Call `parse_request(buf)`. It answers one question: is there a complete request at the
   front of the buffer, and if so, how many bytes long is it?
   * No blank line yet → the head is incomplete → return `None`.
   * Head complete → the headers say how the body is framed:
     * `Content-Length: N` → the request ends exactly N bytes after the blank line.
     * `Transfer-Encoding: chunked` → it ends after the zero-size chunk and its trailers.
     * neither → there is no body; it ends at the blank line.
   * If the body has not all arrived → return `None`.
   * Otherwise return the request and its length `n`.
3. `del buf[:n]`. That removes this request and not one byte more. Whatever is left is the
   beginning of the next request.
4. Go back to step 2. Only when it returns `None` does the loop go back to `recv()`.

Step 4 is why **pipelining** costs nothing extra: if fifty requests arrive in one segment,
step 2 succeeds fifty times in a row and fifty responses go out in order.

The same problem exists in the other direction, so **every response has a `Content-Length`**.
That is the only way the client can tell where a response ends without the server hanging up.

### Two kinds of error

What the server does after an error depends on one thing: does it still know where the next
request starts?

| The request was read completely | The framing cannot be trusted |
|---|---|
| bad or missing `a`/`b`, division by zero (`400`) | malformed request line or header line (`400`) |
| unknown path (`404`) | `Content-Length` that is not plain digits, or two that disagree (`400`) |
| wrong method (`405`) | `Content-Length` **and** `Transfer-Encoding` together (`400`) |
| missing or repeated `Host` (`400`) | bad chunk size or chunk terminator (`400`) |
| | head over 8 KiB (`431`), body over 1 MiB (`413`) |
| | unsupported transfer coding (`501`), HTTP version (`505`) |
| **→ respond, keep the connection open** | **→ respond with `Connection: close`, then close** |

The left column is why the marker's `400`, `404` and `405` do not cost it the socket. The
right column is the defence against request smuggling: if the server guessed a boundary and
guessed differently from a proxy in front of it, the leftover bytes would be treated as a
second request the proxy never saw. So it never guesses.

A test makes this concrete: a `POST` whose body is the text of a `GET` request. The server
must answer once (`405`), not twice, because those bytes are body.

## Stretch goals (all four are implemented)

| Goal | What the server does |
|---|---|
| **`Connection: close`** | Answers, sends `Connection: close`, and closes. Anything pipelined after that request is not processed. HTTP/1.0 requests close by default and stay open only with `Connection: keep-alive`. |
| **Idle timeout** | 10 s with no request → the socket is closed quietly. A request that is started but not finished within 10 s gets `408` and the socket is closed. Responses advertise it: `Keep-Alive: timeout=10, max=99`. |
| **Chunked bodies** | `Transfer-Encoding: chunked` request bodies are decoded, including chunk extensions and trailers, and chunks that arrive split across reads. |
| **Pipelining** | Any number of requests in one write are answered in order. |

**Why 10 seconds.** An open connection holds a thread and a file descriptor even when it is
doing nothing, so the timeout is a trade between two costs. If it is too short, a client that
pauses between requests pays for a new TCP handshake each time, which is exactly what
keep-alive is meant to avoid. If it is too long, idle and abandoned clients accumulate until
the server runs out of threads or descriptors. Ten seconds comfortably covers a program
sending a burst of requests, and a person typing a request into `nc`, while still returning
resources quickly. For comparison, Apache's default is 5 s and nginx's is 75 s. Change it with
`--idle-timeout`.

**The timeout is per request, not per `recv()`.** A simple `settimeout(10)` restarts on every
byte, so a client sending one byte every nine seconds would hold a thread forever (the
"slowloris" attack). Here the clock starts when the first byte of a request arrives and is
not reset by later bytes. There is a test for this.

## Other details

* **Numbers.** Arithmetic uses `decimal.Decimal`, not floating point, so
  `/add?a=0.1&b=0.2` is `0.3` and not `0.30000000000000004`. Addition, subtraction and
  multiplication are exact; division is rounded to 28 significant digits. Whole results print
  without a decimal point (`9/3` → `3`, `10/4` → `2.5`). Inputs are plain decimals up to 64
  characters; `nan`, `inf`, exponents and hex are rejected with `400`.
* **`a` and `b`** must each appear exactly once. Other query parameters are ignored.
* **`HEAD`** returns the headers `GET` would, with no body. **`405`** includes
  `Allow: GET, HEAD`.
* **Limits.** 8 KiB of request head, 1 MiB of body, 100 requests per connection. Without
  limits one client could make the server buffer without bound.
* **Strict where it matters, lenient where it is safe.** Obsolete line folding and whitespace
  before a header's colon are rejected, as RFC 9112 requires. Bare `\n` line endings and
  blank lines between requests are accepted, so typing into `nc` works.
* **Closing cleanly.** Before closing after an error, the server shuts down its sending side
  and briefly reads what the client is still sending. Closing a socket with unread input
  makes TCP send a reset, which can wipe out the error response before the client reads it.
* **Concurrency.** One thread per connection, so a slow or stuck client delays nobody else.
* **Both loopback addresses.** The server listens on `127.0.0.1` and `::1`, because
  `localhost` means one on some machines and the other elsewhere.

## Files

| File | What it is |
|---|---|
| `server.py` | The server: request parser, calculator, connection loop |
| `grade.py` | The marking check: six requests on one socket |
| `test_server.py` | 33 tests: the calculator, request framing, and each stretch goal |
