"""Deadline-aware HTTPS transport for the Hasheous client.

``urlopen(timeout=...)`` bounds each *socket operation*, not the request: every
byte that arrives resets it. A server -- or a proxy -- dripping slower than the
timeout but never stopping therefore pins a lookup, and the scan job around it,
without ever raising, so the cooldown never opens either.

Everything needed to make one deadline cover a whole request lives here:
``_deadline_of`` sets it, ``_DeadlineMixin`` enforces it on both socket classes
a lookup can hold, ``_connect_with_deadline`` spends it across the addresses a
host resolves to, and ``_DeadlineHTTPSConnection``/``_DeadlineHTTPSHandler``
put those in ``_opener``'s hands. Split out of the client module because it is
a self-contained concern with its own failure modes -- and because the four
phases it covers (raw connect, proxy CONNECT, TLS handshake, response) were
found one review round at a time, each as a separate patch, before being made
one mechanism.

``_require_https`` and the redirect handler live here too: the scheme guard is
a property of the transport, and it has to re-run on every redirect hop.
"""

from __future__ import annotations

import contextlib
import http.client
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request


def _require_https(url: str) -> None:
    """Raise ValueError if *url* does not use the https scheme.

    Mirrors ``dat_sync._require_https``. A plain-http base URL would put file
    hashes on the wire in the clear.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Only https URLs are permitted; got scheme '{parsed.scheme}'")


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that would downgrade the transport.

    ``urlopen`` follows redirects on its own, and ``_require_https`` only sees
    the URL we start with -- so a misconfigured or hostile server could bounce
    a lookup to ``http://`` and put the file's SHA1 on the wire in the clear.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# The deadline for the request running on this thread, as a monotonic
# timestamp. Thread-local because lookups run in a threadpool and the socket
# below has no other way to learn which request it belongs to.
_deadline = threading.local()


class _DeadlineMixin:
    """Applies one monotonic deadline to every read the socket performs.

    ``urlopen(timeout=...)`` bounds each *socket operation*, not the request:
    every byte that arrives resets it. A server dripping slower than the
    timeout but never stopping therefore pins a lookup -- and the scan job
    around it -- indefinitely, without ever raising, so the cooldown never
    opens either. Measured against a real dripping server: an 18.5s
    ``open()`` under a 2s timeout, unbounded for a longer header.

    Mixed into both socket classes a lookup can be holding, so the plain
    socket (proxy ``CONNECT`` tunnel) and the TLS socket (handshake, status
    line, headers, body) enforce the same deadline through the same code
    rather than through four mechanisms that each cover one phase.

    ``recv_into`` is the only read override needed: every byte of either
    phase arrives through it, ``http.client`` reading its status line and
    headers via ``makefile("rb")`` -> ``SocketIO.readinto`` -> ``recv_into``.
    """

    def _apply_deadline(self) -> None:
        end = getattr(_deadline, "at", None)
        if end is None:
            return
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("lookup exceeded the overall timeout")
        self.settimeout(remaining)

    def recv_into(self, *args, **kwargs):
        self._apply_deadline()
        return super().recv_into(*args, **kwargs)


class _DeadlineSocket(_DeadlineMixin, socket.socket):
    """The raw socket, deadline-aware before TLS exists.

    This is what bounds a *dripping proxy*: with ``HTTPS_PROXY`` set,
    ``http.client._tunnel`` reads the proxy's ``CONNECT`` response off the raw
    socket before ``wrap_socket``, so a proxy answering one byte at a time
    used to pin the request exactly the way a dripping origin server did.
    """


class _DeadlineSSLSocket(_DeadlineMixin, ssl.SSLSocket):  # pylint: disable=abstract-method
    """The TLS socket, installed via ``SSLContext.sslsocket_class``.

    ``wrap_socket`` detaches the raw socket and builds a new object from its
    file descriptor, so :class:`_DeadlineSocket` cannot carry through -- hence
    two classes over one mixin rather than one class.

    The handshake needs its own hook: measured, it makes **zero**
    ``recv_into`` calls, reading through the C layer instead. Applying the
    remaining budget as the socket timeout bounds a peer that stalls or dies
    mid-handshake. A peer that drips a handshake record at a time, each within
    the remaining budget, is still not bounded -- doing that properly means
    leaving urllib, and it is documented rather than silently implied.

    ``dup()`` is abstract on ``ssl.SSLSocket`` upstream (CPython raises
    ``NotImplementedError``), hence the pylint waiver: nothing here duplicates
    the socket, and overriding it would only re-raise the same error.
    """

    def do_handshake(self, *args, **kwargs):
        self._apply_deadline()
        return super().do_handshake(*args, **kwargs)


def _connect_with_deadline(address, timeout, source_address=None) -> socket.socket:
    """``socket.create_connection`` that spends the deadline, not N x timeout.

    The stdlib helper applies its timeout to *each* address ``getaddrinfo``
    returns, so a host resolving to several blackholed addresses costs one
    full timeout apiece -- measured, three dropped addresses take 9.0s under a
    3s timeout, and a redirect opens a fresh connection with a fresh budget.
    Looping here instead keeps every attempt inside the one budget the caller
    asked for.

    What this still does not cover is DNS: ``getaddrinfo`` runs before any
    socket exists and ignores socket timeouts. A stalled resolver is bounded
    by ``/etc/resolv.conf`` (glibc default 5s x 2 attempts per nameserver),
    not by ``hasheous_timeout`` -- but unlike a drip it *raises*, so it opens
    the cooldown and the rest of a scan short-circuits. Bounding it properly
    would mean resolving on an abandonable thread; the trade is documented
    rather than taken.
    """
    host, port = address
    err: Exception | None = None
    for family, socktype, proto, _canon, sockaddr in socket.getaddrinfo(
        host, port, 0, socket.SOCK_STREAM,
    ):
        sock = _DeadlineSocket(family, socktype, proto)
        try:
            sock.settimeout(_remaining(timeout))
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            err = exc
            sock.close()
    raise err if err is not None else OSError(f"no address returned for {host}")


def _remaining(default: float | None) -> float | None:
    """Seconds left on this request's deadline, or *default* when unset."""
    end = getattr(_deadline, "at", None)
    if end is None:
        return default
    left = end - time.monotonic()
    if left <= 0:
        raise TimeoutError("lookup exceeded the overall timeout")
    return left


class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
    """Connects through :func:`_connect_with_deadline`.

    Overriding the connection factory rather than ``connect()`` keeps the
    stdlib's own TLS wiring (server_hostname, ALPN, hostname checking) as the
    single source of truth; only where the raw socket comes from changes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_with_deadline


class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    """Makes urllib open :class:`_DeadlineHTTPSConnection` instead of the stock one."""

    def https_open(self, req):
        return self.do_open(_DeadlineHTTPSConnection, req, context=self._context)


def _ssl_context() -> ssl.SSLContext:
    """The stock verifying context, with our socket class installed."""
    context = ssl.create_default_context()
    context.sslsocket_class = _DeadlineSSLSocket
    return context


_opener = urllib.request.build_opener(
    _HTTPSOnlyRedirectHandler,
    _DeadlineHTTPSHandler(context=_ssl_context()),
)


@contextlib.contextmanager
def _deadline_of(seconds: float):
    """Bound everything done inside to *seconds* total."""
    _deadline.at = time.monotonic() + seconds
    try:
        yield
    finally:
        _deadline.at = None
