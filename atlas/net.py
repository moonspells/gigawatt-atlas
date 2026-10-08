"""Guarded HTTP for every importer (10 §11.8, 07 §5.3).

make_client builds an httpx.Client whose request hook refuses non-HTTP schemes and hosts that
resolve to private, loopback, link-local, reserved, multicast or unspecified addresses. The guard
is a hook, not a transport, because environment proxies mount their own transport.

fetch adds the crawler policy on top: robots.txt, a per-host delay, at most 5 redirects (each one
re-checked by the hook), a byte cap on the decoded body, a content-type allow-list, and retries
with backoff that honor Retry-After.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import time
import urllib.robotparser
import weakref
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

DEFAULT_USER_AGENT = "moonspells-atlas/1.0 (+https://moonspells.dev/atlas/about)"
ROBOTS_AGENT = "moonspells-atlas"
MAX_REDIRECTS = 5
BACKOFF_SECONDS = (10.0, 30.0, 60.0)
MAX_RETRY_AFTER_SECONDS = 600.0
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
ACCEPT_ENCODING = "gzip, deflate"  # what _decoded_body can decode
_DECODED_CHUNK = 1 << 16

Resolver = Callable[[str], list[str]]


class FetchError(Exception):
    """A request failed or was refused. status is the final HTTP status when there was one."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class BlockedAddressError(FetchError):
    """The URL's scheme is not http(s), or its host resolves to a non-public address."""


class RobotsDisallowed(FetchError):  # noqa: N818  (name fixed by the interface)
    """robots.txt disallows the URL for the moonspells-atlas agent."""


class TooLarge(FetchError):  # noqa: N818  (name fixed by the interface)
    """The body is larger than max_bytes."""


class OfflineError(FetchError):
    """A request was made through OfflineTransport (`atlas import --offline`)."""


class BadContentType(FetchError):  # noqa: N818  (name fixed by the interface)
    """The response's content type is not in allowed_types."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    content: bytes
    sha256: str
    retrieved_at: datetime


def user_agent_from_env() -> str:
    """ATLAS_CRAWLER_UA if set, else DEFAULT_USER_AGENT."""
    return os.environ.get("ATLAS_CRAWLER_UA") or DEFAULT_USER_AGENT


def system_resolver(host: str) -> list[str]:
    """Every address getaddrinfo returns for host."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def is_public_address(address: str) -> bool:
    """False for private, loopback, link-local, reserved, multicast and unspecified addresses."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def check_url(url: httpx.URL, resolver: Resolver) -> None:
    """Raise BlockedAddressError unless url is http(s) and its host resolves to public addresses."""
    if url.scheme not in ("http", "https"):
        raise BlockedAddressError(f"scheme {url.scheme!r} is not allowed: {url}")
    host = url.host
    if not host:
        raise BlockedAddressError(f"no host in {url}")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = resolver(host)
        except (OSError, UnicodeError) as e:
            raise BlockedAddressError(f"cannot resolve {host}: {e}") from e
    if not addresses:
        raise BlockedAddressError(f"{host} resolves to no address")
    blocked = [a for a in addresses if not is_public_address(a)]
    if blocked:
        raise BlockedAddressError(f"{host} resolves to a non-public address ({', '.join(blocked)})")


class OfflineTransport(httpx.BaseTransport):
    """The transport behind `atlas import --offline`: every request fails."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise OfflineError(f"offline: no network request allowed ({request.method} {request.url})")


def make_client(
    *,
    user_agent: str | None = None,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    timeout: float = 30.0,
) -> httpx.Client:
    """An httpx.Client with the address guard on every request.

    follow_redirects is off (fetch follows them itself, re-checking each hop), and trust_env is on,
    so proxies from the environment work.
    """
    resolve = resolver or system_resolver

    def guard(request: httpx.Request) -> None:
        check_url(request.url, resolve)

    return httpx.Client(
        headers={
            "User-Agent": user_agent or user_agent_from_env(),
            "Accept-Encoding": ACCEPT_ENCODING,
        },
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
        trust_env=True,
        event_hooks={"request": [guard]},
    )


@dataclass
class _ClientState:
    last_request: dict[str, datetime] = field(default_factory=dict)
    robots: dict[str, urllib.robotparser.RobotFileParser | bool] = field(default_factory=dict)


_STATE: weakref.WeakKeyDictionary[httpx.Client, _ClientState] = weakref.WeakKeyDictionary()


def _state(client: httpx.Client) -> _ClientState:
    state = _STATE.get(client)
    if state is None:
        state = _ClientState()
        _STATE[client] = state
    return state


def _host_key(url: httpx.URL) -> str:
    return f"{url.scheme}://{url.netloc.decode('ascii')}"


def _decoded_body(response: httpx.Response, url: httpx.URL, max_bytes: int) -> Iterator[bytes]:
    """The body, decoded from at most one Content-Encoding (gzip or deflate), in pieces of at most
    _DECODED_CHUNK bytes, so the caller's byte cap stops a compression bomb early.

    httpx's own decoders expand a whole network chunk at once and apply stacked codings in turn: a
    1.8 KB body sent as "gzip, gzip" took 2 GB of memory before the first size check (10 §11.8,
    T19). Stacked and unknown codings are refused, and the raw bytes are capped too.
    """
    header = response.headers.get("content-encoding", "")
    codings = [c.strip().lower() for c in header.split(",")]
    codings = [c for c in codings if c not in ("", "identity")]
    if len(codings) > 1 or (codings and codings[0] not in ("gzip", "deflate")):
        raise FetchError(f"{url}: Content-Encoding {header!r} is not supported")
    if response.is_stream_consumed:
        # Built from content= (a test transport) and already decoded; a network body never is.
        yield response.content
        return
    if not codings:
        yield from response.iter_raw()
        return
    coding = codings[0]
    decoder = zlib.decompressobj(zlib.MAX_WBITS | 16 if coding == "gzip" else zlib.MAX_WBITS)
    first = coding == "deflate"  # like httpx, fall back to raw deflate if the zlib header is bad
    raw = 0
    for data in response.iter_raw():
        raw += len(data)
        if raw > max_bytes:
            raise TooLarge(f"{url}: body larger than {max_bytes} bytes")
        while not decoder.eof:  # bytes after the end of the stream are ignored, as httpx does
            try:
                piece = decoder.decompress(data, _DECODED_CHUNK)
            except zlib.error as e:
                if first:
                    decoder, first = zlib.decompressobj(-zlib.MAX_WBITS), False
                    continue
                raise FetchError(f"{url}: cannot decode the {coding} body: {e}") from e
            first = False
            data = decoder.unconsumed_tail
            if piece:
                yield piece
            # A full piece may leave output pending even when no input is left.
            if not data and len(piece) < _DECODED_CHUNK:
                break
    try:
        tail = decoder.flush()
    except zlib.error as e:
        raise FetchError(f"{url}: cannot decode the {coding} body: {e}") from e
    if tail:
        yield tail


def _retry_after(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


class _Fetcher:
    def __init__(
        self,
        client: httpx.Client,
        *,
        min_interval: float,
        retries: int,
        sleep: Callable[[float], None],
        now: Callable[[], datetime],
        timeout: float | None,
    ) -> None:
        self.client = client
        self.state = _state(client)
        self.min_interval = min_interval
        self.retries = retries
        self.sleep = sleep
        self.now = now
        self.timeout = timeout

    def _wait_for_host(self, url: httpx.URL) -> None:
        key = _host_key(url)
        last = self.state.last_request.get(key)
        if last is not None:
            elapsed = (self.now() - last).total_seconds()
            if elapsed < self.min_interval:
                self.sleep(self.min_interval - elapsed)
        self.state.last_request[key] = self.now()

    def _send_once(
        self,
        method: str,
        url: httpx.URL,
        *,
        data: bytes | Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        max_bytes: int,
    ) -> tuple[int, httpx.Headers, bytes, httpx.URL]:
        self._wait_for_host(url)
        kwargs: dict[str, Any] = {"headers": dict(headers or {})}
        if isinstance(data, bytes):
            kwargs["content"] = data
        elif data is not None:
            kwargs["data"] = dict(data)
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        with self.client.stream(method, url, **kwargs) as response:
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise TooLarge(f"{url}: Content-Length {declared} > {max_bytes} bytes")
            chunks: list[bytes] = []
            total = 0
            if response.status_code not in _REDIRECT_CODES:
                for chunk in _decoded_body(response, url, max_bytes):
                    total += len(chunk)
                    if total > max_bytes:
                        raise TooLarge(f"{url}: body larger than {max_bytes} bytes")
                    chunks.append(chunk)
            return response.status_code, response.headers, b"".join(chunks), response.url

    def send(
        self,
        method: str,
        url: httpx.URL,
        *,
        data: bytes | Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        max_bytes: int,
    ) -> tuple[int, httpx.Headers, bytes, httpx.URL]:
        """One request with retries on 429, 5xx and connection errors."""
        attempt = 0
        while True:
            try:
                status, resp_headers, body, final = self._send_once(
                    method, url, data=data, headers=headers, max_bytes=max_bytes
                )
            except httpx.TransportError as e:
                if attempt >= self.retries:
                    raise FetchError(f"{method} {url}: {type(e).__name__}: {e}") from e
                self.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
                attempt += 1
                continue
            except httpx.HTTPError as e:
                # Not a network failure (a body that cannot be decoded, say): retrying gets the
                # same answer, and callers handle only FetchError.
                raise FetchError(f"{method} {url}: {type(e).__name__}: {e}") from e
            if status == 429 or status >= 500:
                if attempt >= self.retries:
                    raise FetchError(f"{method} {url}: HTTP {status}", status=status)
                wait = _retry_after(resp_headers.get("retry-after"), self.now())
                if wait is None:
                    wait = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                if wait > MAX_RETRY_AFTER_SECONDS:
                    raise FetchError(
                        f"{method} {url}: HTTP {status}, Retry-After {wait:.0f} s is too long",
                        status=status,
                    )
                self.sleep(wait)
                attempt += 1
                continue
            return status, resp_headers, body, final

    def robots_allows(self, url: httpx.URL) -> bool:
        key = _host_key(url)
        cached = self.state.robots.get(key)
        if cached is None:
            cached = self._load_robots(url)
            self.state.robots[key] = cached
        if isinstance(cached, bool):
            return cached
        return cached.can_fetch(ROBOTS_AGENT, str(url))

    def _load_robots(self, url: httpx.URL) -> urllib.robotparser.RobotFileParser | bool:
        """4xx means allow everything; 5xx or a network error means disallow everything."""
        target = httpx.URL(_host_key(url) + "/robots.txt")
        try:
            status, _, body, _ = self.follow(
                "GET", target, data=None, headers=None, max_bytes=500_000, robots=False
            )
        except (BlockedAddressError, RobotsDisallowed, OfflineError):
            raise
        except FetchError as e:
            return e.status is not None and 400 <= e.status < 500
        if 400 <= status < 500:
            return True
        if status >= 300:
            return False
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(body.decode("utf-8", errors="replace").splitlines())
        return parser

    def follow(
        self,
        method: str,
        url: httpx.URL,
        *,
        data: bytes | Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        max_bytes: int,
        robots: bool,
    ) -> tuple[int, httpx.Headers, bytes, httpx.URL]:
        """Send, following at most MAX_REDIRECTS redirects; every hop passes the guard again."""
        hops = 0
        while True:
            if robots and not self.robots_allows(url):
                raise RobotsDisallowed(f"robots.txt disallows {url} for {ROBOTS_AGENT}")
            status, resp_headers, body, final = self.send(
                method, url, data=data, headers=headers, max_bytes=max_bytes
            )
            location = resp_headers.get("location")
            if status not in _REDIRECT_CODES or not location:
                return status, resp_headers, body, final
            hops += 1
            if hops > MAX_REDIRECTS:
                raise FetchError(f"more than {MAX_REDIRECTS} redirects from {url}", status=status)
            url = final.join(location)
            if status == 303 or (status in (301, 302) and method == "POST"):
                method, data = "GET", None


def fetch(
    client: httpx.Client,
    url: str,
    *,
    method: str = "GET",
    data: bytes | Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    max_bytes: int = 20_000_000,
    timeout: float | None = None,
    allowed_types: tuple[str, ...] = (),
    robots: bool = True,
    min_interval: float = 2.0,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> FetchResult:
    """Fetch url under the crawler policy.

    Raises FetchError (status set) for a final HTTP status of 400 or more, and its subclasses for
    blocked addresses, robots.txt, oversized bodies and content types outside allowed_types (an
    empty tuple allows any type). now is the clock for retrieved_at and the per-host delay.
    """
    clock = now or (lambda: datetime.now(UTC))
    fetcher = _Fetcher(
        client, min_interval=min_interval, retries=retries, sleep=sleep, now=clock, timeout=timeout
    )
    status, resp_headers, body, final = fetcher.follow(
        method.upper(),
        httpx.URL(url),
        data=data,
        headers=headers,
        max_bytes=max_bytes,
        robots=robots,
    )
    if status >= 400:
        raise FetchError(f"{method} {url}: HTTP {status}", status=status)
    if allowed_types and 200 <= status < 300:
        media = resp_headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media not in {t.lower() for t in allowed_types}:
            raise BadContentType(f"{url}: content type {media or '(none)'} not in {allowed_types}")
    return FetchResult(
        url=url,
        final_url=str(final),
        status=status,
        headers={k.lower(): v for k, v in resp_headers.items()},
        content=body,
        sha256=hashlib.sha256(body).hexdigest(),
        retrieved_at=clock(),
    )
