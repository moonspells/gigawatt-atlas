from __future__ import annotations

import contextlib
import hashlib
import http.server
import ipaddress
import socket
import ssl
import threading
import tracemalloc
import zlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from atlas.net import (
    ACCEPT_ENCODING,
    DEFAULT_USER_AGENT,
    MAX_RETRY_AFTER_SECONDS,
    BadContentType,
    BlockedAddressError,
    FetchError,
    OfflineError,
    OfflineTransport,
    RobotsDisallowed,
    TooLarge,
    fetch,
    is_public_address,
    make_client,
    user_agent_from_env,
)

PUBLIC = "93.184.216.34"
Handler = Callable[[httpx.Request], httpx.Response]


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)


def resolver_for(table: dict[str, list[str]]) -> Callable[[str], list[str]]:
    def resolve(host: str) -> list[str]:
        return table.get(host, [PUBLIC])

    return resolve


def client_for(handler: Handler, table: dict[str, list[str]] | None = None) -> httpx.Client:
    return make_client(transport=httpx.MockTransport(handler), resolver=resolver_for(table or {}))


class Chunks(httpx.SyncByteStream):
    """A body as it comes off the network: raw (still encoded) bytes, in chunks."""

    def __init__(self, data: bytes, size: int = 65_536) -> None:
        self.data = data
        self.size = size

    def __iter__(self) -> Iterator[bytes]:
        for i in range(0, len(self.data), self.size):
            yield self.data[i : i + self.size]


def wire(status: int, headers: dict[str, str], body: bytes) -> httpx.Response:
    """A response whose body is decoded only when read (content= would decode it here)."""
    return httpx.Response(status, headers=headers, stream=Chunks(body))


def compress(data: bytes, wbits: int, repeat: int = 1) -> bytes:
    """data * repeat, compressed: wbits 31 is gzip, 15 zlib-wrapped deflate, -15 raw deflate."""
    c = zlib.compressobj(9, zlib.DEFLATED, wbits)
    return b"".join([*(c.compress(data) for _ in range(repeat)), c.flush()])


def site(routes: dict[str, httpx.Response], log: list[str] | None = None) -> Handler:
    """A handler that serves routes by path and answers 404 to /robots.txt by default."""

    def handle(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(f"{request.method} {request.url}")
        key = request.url.path
        if key in routes:
            r = routes[key]
            return httpx.Response(r.status_code, headers=r.headers, content=r.content)
        if key == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(404)

    return handle


@pytest.mark.parametrize(
    "address",
    [
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "fe80::1",
        "0.0.0.0",  # noqa: S104  (an address under test, not a bind)
        "224.0.0.1",
        "100.64.0.1",
        "::ffff:127.0.0.1",
    ],
)
def test_non_public_addresses(address: str) -> None:
    assert not is_public_address(address)


def test_public_address() -> None:
    assert is_public_address(PUBLIC)
    assert is_public_address("2606:4700::6810:84e5")


@pytest.mark.parametrize(
    ("url", "table"),
    [
        ("http://127.0.0.1/x", {}),
        ("http://[::1]/x", {}),
        ("https://intranet.example/x", {"intranet.example": ["10.0.0.5"]}),
        ("https://mixed.example/x", {"mixed.example": [PUBLIC, "192.168.0.10"]}),
        ("https://metadata.example/x", {"metadata.example": ["169.254.169.254"]}),
    ],
)
def test_private_and_loopback_hosts_are_refused(url: str, table: dict[str, list[str]]) -> None:
    calls: list[str] = []
    with client_for(site({}, calls), table) as client:
        with pytest.raises(BlockedAddressError):
            client.get(url)
        with pytest.raises(BlockedAddressError):
            fetch(client, url, robots=False, sleep=lambda s: None)
    assert calls == []  # refused before the transport


def test_non_http_schemes_are_refused() -> None:
    with client_for(site({})) as client, pytest.raises(BlockedAddressError, match="scheme"):
        fetch(client, "ftp://example.com/file", robots=False)


def test_unresolvable_host_is_refused() -> None:
    def fail(host: str) -> list[str]:
        raise OSError("no such host")

    with (
        make_client(transport=httpx.MockTransport(site({})), resolver=fail) as client,
        pytest.raises(BlockedAddressError, match="cannot resolve"),
    ):
        fetch(client, "https://nowhere.example/", robots=False)


# The real transport, saved before conftest's _no_network replaces it for each test.
REAL_HANDLE_REQUEST = httpx.HTTPTransport.handle_request


class LocalNetwork:
    """DNS and TCP for the connection-pinning tests, so nothing leaves the machine.

    Names resolve from dns only (each lookup takes the next answer, and the last one repeats).
    Connections are recorded in tried; an address in routes goes to that local port, 127.0.0.1
    connects, and everything else is refused.
    """

    def __init__(self) -> None:
        self.dns: dict[str, list[str]] = {}
        self.routes: dict[str, int] = {}
        self.tried: list[tuple[str, int]] = []
        self._getaddrinfo = socket.getaddrinfo
        self._connect = socket.create_connection

    def getaddrinfo(self, host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            answers = self.dns.get(host)
            if not answers:
                raise socket.gaierror(socket.EAI_NONAME, f"{host} is not in the test DNS") from None
            host = answers.pop(0) if len(answers) > 1 else answers[0]
        return self._getaddrinfo(host, port, *args, **kwargs)

    def create_connection(
        self, address: tuple[str, int], *args: Any, **kwargs: Any
    ) -> socket.socket:
        self.tried.append(address)
        host, port = address
        if host in self.routes:
            return self._connect(("127.0.0.1", self.routes[host]), *args, **kwargs)
        # A name is resolved here, as socket.create_connection does.
        ip = self.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)[0][4][0]
        if ip == "127.0.0.1":
            return self._connect((ip, port), *args, **kwargs)
        raise ConnectionRefusedError(f"{host}:{port} refused (tests stay offline)")


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> LocalNetwork:
    """make_client's real transport, with no environment proxy, over LocalNetwork."""
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", REAL_HANDLE_REQUEST)
    net = LocalNetwork()
    monkeypatch.setattr(socket, "getaddrinfo", net.getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", net.create_connection)
    return net


@contextlib.contextmanager
def local_server(body: bytes) -> Iterator[tuple[int, list[tuple[str, str]]]]:
    """An HTTP server on 127.0.0.1: its port, and the (request target, Host) of each request."""
    seen: list[tuple[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append((self.path, self.headers.get("Host", "")))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_dns_rebinding_cannot_reach_a_private_address(network: LocalNetwork) -> None:
    # The hook's lookup answers a public address and the next one 127.0.0.1 (TTL 0). The
    # connection used to resolve the name again and reach the private server (10 §11.8).
    network.dns["rebind.example"] = [PUBLIC, "127.0.0.1"]
    with (
        local_server(b'{"secret": "internal-only"}') as (port, seen),
        make_client() as client,
        pytest.raises(BlockedAddressError, match=r"127\.0\.0\.1"),
    ):
        fetch(
            client,
            f"http://rebind.example:{port}/latest/meta-data",
            robots=False,
            sleep=lambda s: None,
        )
    assert seen == []
    assert network.tried == []


def test_connections_go_to_the_checked_addresses_with_the_original_host(
    network: LocalNetwork,
) -> None:
    second = "93.184.216.35"
    with local_server(b"ok") as (port, seen):
        network.routes[second] = port  # the first address refuses, the second answers
        resolver = resolver_for({"pinned.example": [PUBLIC, second]})
        with make_client(resolver=resolver) as client:
            result = fetch(
                client, f"http://pinned.example:{port}/x", robots=False, sleep=lambda s: None
            )
    assert result.content == b"ok"
    assert network.tried == [(PUBLIC, port), (second, port)]  # never the name
    assert seen == [("/x", f"pinned.example:{port}")]


def test_tls_on_a_pinned_connection_uses_the_host_name(
    network: LocalNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    names: list[str | None] = []

    def wrap_socket(
        self: ssl.SSLContext, sock: socket.socket, *args: Any, **kwargs: Any
    ) -> ssl.SSLSocket:
        names.append(kwargs.get("server_hostname"))
        raise ssl.SSLError("stop before the handshake")

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap_socket)
    with socket.create_server(("127.0.0.1", 0)) as listener:
        network.routes[PUBLIC] = listener.getsockname()[1]
        with (
            make_client(resolver=resolver_for({})) as client,
            pytest.raises(FetchError, match="ConnectError"),
        ):
            fetch(client, "https://sni.example/x", robots=False, retries=0, sleep=lambda s: None)
    assert network.tried == [(PUBLIC, 443)]
    assert names == ["sni.example"]  # SNI and the certificate check use the name, not the IP


def test_environment_proxies_still_carry_requests(
    network: LocalNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The proxy resolves the name, so only the hook checks it; NO_PROXY hosts are pinned.
    resolver = resolver_for({"intranet.example": ["10.0.0.5"]})
    with local_server(b"via proxy") as (port, seen):
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("NO_PROXY", "direct.example")
        with make_client(resolver=resolver) as client:
            result = fetch(client, "http://public.example/data", robots=False, sleep=lambda s: None)
            assert result.content == b"via proxy"
            with pytest.raises(BlockedAddressError):
                fetch(client, "http://intranet.example/", robots=False, sleep=lambda s: None)
            with pytest.raises(FetchError, match="ConnectError"):
                fetch(
                    client, "http://direct.example/", robots=False, retries=0, sleep=lambda s: None
                )
    assert seen == [("http://public.example/data", "public.example")]
    assert network.tried == [("127.0.0.1", port), (PUBLIC, 80)]


def test_fetch_returns_body_hash_and_headers() -> None:
    clock = Clock()
    routes = {
        "/data.json": httpx.Response(
            200,
            headers={"Content-Type": "application/json; charset=utf-8", "ETag": '"v1"'},
            content=b"[]",
        )
    }
    with client_for(site(routes)) as client:
        result = fetch(
            client,
            "https://data.example/data.json",
            allowed_types=("application/json",),
            sleep=clock.sleep,
            now=clock.now,
        )
    assert result.status == 200
    assert result.content == b"[]"
    assert result.sha256 == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    assert result.headers["etag"] == '"v1"'
    assert result.retrieved_at == clock.t
    assert result.final_url == "https://data.example/data.json"


def test_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200)

    monkeypatch.delenv("ATLAS_CRAWLER_UA", raising=False)
    assert user_agent_from_env() == DEFAULT_USER_AGENT
    with client_for(handle) as client:
        client.get("https://a.example/")
    monkeypatch.setenv("ATLAS_CRAWLER_UA", "moonspells-atlas/1.0 (+https://moonspells.dev/x)")
    with client_for(handle) as client:
        client.get("https://a.example/")
    assert seen == [DEFAULT_USER_AGENT, "moonspells-atlas/1.0 (+https://moonspells.dev/x)"]


def test_redirect_hops_are_rechecked() -> None:
    routes = {"/start": httpx.Response(302, headers={"Location": "http://internal.example/admin"})}
    calls: list[str] = []
    with (
        client_for(site(routes, calls), {"internal.example": ["10.0.0.7"]}) as client,
        pytest.raises(BlockedAddressError),
    ):
        fetch(client, "https://public.example/start", robots=False, sleep=lambda s: None)
    assert calls == ["GET https://public.example/start"]


def test_redirects_are_followed_up_to_five() -> None:
    def chain(n: int) -> Handler:
        def handle(request: httpx.Request) -> httpx.Response:
            step = int(request.url.path.strip("/") or 0)
            if step < n:
                return httpx.Response(301, headers={"Location": f"/{step + 1}"})
            return httpx.Response(200, content=b"done")

        return handle

    with client_for(chain(5)) as client:
        result = fetch(client, "https://r.example/0", robots=False, sleep=lambda s: None)
        assert result.content == b"done"
        assert result.final_url == "https://r.example/5"
    with client_for(chain(6)) as client, pytest.raises(FetchError, match="redirects"):
        fetch(client, "https://r.example/0", robots=False, sleep=lambda s: None)


@pytest.mark.parametrize("status", [303, 301, 302])
def test_a_post_redirected_with_303_301_or_302_continues_as_a_get(status: int) -> None:
    # 303 asks for a GET (RFC 9110 §15.4.4); for 301 and 302 fetch does what browsers do with a
    # POST, so a form body is never re-submitted to the redirect target.
    seen: list[tuple[str, str, bytes]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.read()))
        if request.url.path == "/form":
            return httpx.Response(status, headers={"Location": "/result"})
        return httpx.Response(200, content=b"done")

    with client_for(handle) as client:
        result = fetch(
            client,
            "https://p.example/form",
            method="POST",
            data={"a": "b"},
            robots=False,
            sleep=lambda s: None,
        )
    assert result.content == b"done"
    assert seen == [("POST", "/form", b"a=b"), ("GET", "/result", b"")]


@pytest.mark.parametrize("status", [307, 308])
def test_a_post_redirected_with_307_or_308_is_repeated_with_its_body(status: int) -> None:
    seen: list[tuple[str, str, bytes]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.read()))
        if request.url.path == "/form":
            return httpx.Response(status, headers={"Location": "/result"})
        return httpx.Response(200, content=b"done")

    with client_for(handle) as client:
        result = fetch(
            client,
            "https://p.example/form",
            method="POST",
            data={"a": "b"},
            robots=False,
            sleep=lambda s: None,
        )
    assert result.content == b"done"
    assert seen == [("POST", "/form", b"a=b"), ("POST", "/result", b"a=b")]


def test_byte_cap_streaming_and_declared() -> None:
    routes = {
        "/big": httpx.Response(200, content=b"x" * 2048),
        "/declared": httpx.Response(200, headers={"Content-Length": "999999"}, content=b"x"),
    }
    with client_for(site(routes)) as client:
        with pytest.raises(TooLarge):
            fetch(
                client, "https://b.example/big", max_bytes=1024, robots=False, sleep=lambda s: None
            )
        with pytest.raises(TooLarge):
            fetch(
                client,
                "https://b.example/declared",
                max_bytes=1024,
                robots=False,
                sleep=lambda s: None,
            )
        assert (
            len(
                fetch(
                    client,
                    "https://b.example/big",
                    max_bytes=2048,
                    robots=False,
                    sleep=lambda s: None,
                ).content
            )
            == 2048
        )


CODINGS = [("gzip", 31), ("deflate", 15), ("deflate", -15)]


@pytest.mark.parametrize(("coding", "wbits"), CODINGS)
def test_compression_bomb_stops_at_the_cap_before_it_expands(coding: str, wbits: int) -> None:
    # 64 MiB of zeros in about 64 KB. httpx's decoder expanded each network chunk in full before
    # the byte cap was checked, so memory grew by the whole payload (10 §11.8, T19).
    body = compress(b"\0" * (1 << 20), wbits, repeat=64)

    def handle(request: httpx.Request) -> httpx.Response:
        return wire(200, {"Content-Encoding": coding}, body)

    with client_for(handle) as client:
        tracemalloc.start()
        try:
            with pytest.raises(TooLarge):
                fetch(
                    client,
                    "https://bomb.example/x.json",
                    max_bytes=1_000_000,
                    robots=False,
                    sleep=lambda s: None,
                )
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    assert peak < 8 << 20, f"peak {peak} bytes for a 1 MB cap"


@pytest.mark.parametrize("encoding", ["gzip, gzip", "deflate, gzip", "br", "compress"])
def test_stacked_and_unknown_content_encodings_are_refused(encoding: str) -> None:
    # Two gzip layers turned a 1.8 KB body into 2 GB of memory; an unknown coding came back as
    # the still-encoded bytes.
    body = compress(compress(b"\0" * (1 << 20), 31, repeat=64), 31)

    def handle(request: httpx.Request) -> httpx.Response:
        return wire(200, {"Content-Encoding": encoding}, body)

    with client_for(handle) as client, pytest.raises(FetchError, match="Content-Encoding"):
        fetch(client, "https://bomb.example/x.json", robots=False, sleep=lambda s: None)


@pytest.mark.parametrize(("coding", "wbits"), [*CODINGS, ("identity", 0), ("", 0)])
def test_encoded_bodies_are_decoded_in_full(coding: str, wbits: int) -> None:
    # Incompressible bytes, then a long run that decodes in many full pieces.
    data = b"".join(hashlib.sha256(b"%d" % i).digest() for i in range(20_000)) + b"\0" * 3_000_000
    body = compress(data, wbits) if wbits else data
    headers = {"Content-Encoding": coding} if coding else {}

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=Chunks(body, size=997))

    with client_for(handle) as client:
        result = fetch(
            client, "https://z.example/x", max_bytes=len(data), robots=False, sleep=lambda s: None
        )
    assert result.content == data
    assert result.sha256 == hashlib.sha256(data).hexdigest()


def test_raw_bytes_count_against_the_cap() -> None:
    # Bytes after the end of the gzip stream decode to nothing but are still read and kept.
    body = compress(b"ok", 31) + b"\0" * 2_000_000

    def handle(request: httpx.Request) -> httpx.Response:
        return wire(200, {"Content-Encoding": "gzip"}, body)

    with client_for(handle) as client, pytest.raises(TooLarge):
        fetch(
            client, "https://z.example/x", max_bytes=1_000_000, robots=False, sleep=lambda s: None
        )


def test_requests_ask_only_for_the_codings_fetch_decodes() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["accept-encoding"])
        return httpx.Response(200)

    with client_for(handle) as client:
        fetch(client, "https://z.example/x", robots=False, sleep=lambda s: None)
    assert seen == [ACCEPT_ENCODING] == ["gzip, deflate"]


def test_content_type_allow_list() -> None:
    routes = {
        "/page": httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>")
    }
    with client_for(site(routes)) as client, pytest.raises(BadContentType):
        fetch(
            client,
            "https://c.example/page",
            allowed_types=("application/json",),
            robots=False,
            sleep=lambda s: None,
        )


def test_robots_disallow() -> None:
    routes = {
        "/robots.txt": httpx.Response(
            200, content=b"User-agent: moonspells-atlas\nDisallow: /private/\n"
        ),
        "/private/data.json": httpx.Response(200, content=b"{}"),
        "/public/data.json": httpx.Response(200, content=b"{}"),
    }
    calls: list[str] = []
    with client_for(site(routes, calls)) as client:
        with pytest.raises(RobotsDisallowed):
            fetch(client, "https://d.example/private/data.json", sleep=lambda s: None)
        assert (
            fetch(client, "https://d.example/public/data.json", sleep=lambda s: None).content
            == b"{}"
        )
    # robots.txt is fetched once per host and cached.
    assert calls.count("GET https://d.example/robots.txt") == 1
    assert "GET https://d.example/private/data.json" not in calls


def test_robots_4xx_allows_and_5xx_disallows() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403 if request.url.host == "allow.example" else 503)
        return httpx.Response(200, content=b"ok")

    with client_for(handle) as client:
        assert fetch(client, "https://allow.example/x", sleep=lambda s: None).content == b"ok"
        with pytest.raises(RobotsDisallowed):
            fetch(client, "https://deny.example/x", sleep=lambda s: None)


def test_retry_after_and_backoff() -> None:
    clock = Clock()
    answers = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503),
            httpx.Response(200, content=b"ok"),
        ]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return next(answers)

    with client_for(handle) as client:
        result = fetch(
            client,
            "https://e.example/x",
            robots=False,
            sleep=clock.sleep,
            now=clock.now,
            min_interval=0,
        )
    assert result.content == b"ok"
    assert clock.sleeps == [7.0, 30.0]  # Retry-After first, then the second backoff step


@pytest.mark.parametrize("status", [503, 429])
def test_a_retry_after_above_the_cap_is_an_error_not_a_wait(status: int) -> None:
    # A server may ask for an hour; the run fails at once instead of sleeping through it.
    clock = Clock()
    calls: list[str] = []
    too_long = str(int(MAX_RETRY_AFTER_SECONDS) + 1)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(status, headers={"Retry-After": too_long})

    with (
        client_for(handle) as client,
        pytest.raises(FetchError, match=f"Retry-After {too_long} s is too long") as info,
    ):
        fetch(
            client,
            "https://g.example/x",
            robots=False,
            sleep=clock.sleep,
            now=clock.now,
            min_interval=0,
        )
    assert info.value.status == status
    assert clock.sleeps == [] and calls == ["/x"]


@pytest.mark.parametrize("wait", [5, int(MAX_RETRY_AFTER_SECONDS)])
def test_a_retry_after_up_to_the_cap_is_waited_out(wait: int) -> None:
    clock = Clock()
    answers = iter(
        [
            httpx.Response(503, headers={"Retry-After": str(wait)}),
            httpx.Response(200, content=b"ok"),
        ]
    )
    with client_for(lambda request: next(answers)) as client:
        result = fetch(
            client,
            "https://g.example/x",
            robots=False,
            sleep=clock.sleep,
            now=clock.now,
            min_interval=0,
        )
    assert result.content == b"ok"
    assert clock.sleeps == [float(wait)]


def test_retries_exhausted_and_connection_errors() -> None:
    clock = Clock()
    with client_for(lambda r: httpx.Response(500)) as client, pytest.raises(FetchError) as info:
        fetch(
            client,
            "https://f.example/x",
            robots=False,
            sleep=clock.sleep,
            now=clock.now,
            min_interval=0,
        )
    assert info.value.status == 500
    assert clock.sleeps == [10.0, 30.0, 60.0]

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    clock = Clock()
    with client_for(refuse) as client, pytest.raises(FetchError, match="ConnectError"):
        fetch(
            client,
            "https://g.example/x",
            robots=False,
            retries=1,
            sleep=clock.sleep,
            now=clock.now,
            min_interval=0,
        )
    assert clock.sleeps == [10.0]


def test_undecodable_body_is_a_fetch_error() -> None:
    # httpx raises DecodingError, which is not a TransportError; callers catch only FetchError.
    def handle(request: httpx.Request) -> httpx.Response:
        return wire(200, {"Content-Encoding": "gzip"}, b"not gzip at all")

    clock = Clock()
    with client_for(handle) as client, pytest.raises(FetchError, match=r"(?i)decod") as info:
        fetch(client, "https://msd.example/v", robots=False, sleep=clock.sleep, now=clock.now)
    assert info.value.status is None
    assert clock.sleeps == []  # not retried: the same body would come back


def test_other_httpx_errors_are_fetch_errors() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("bad encoding", request=request)

    clock = Clock()
    with client_for(fail) as client, pytest.raises(FetchError, match="DecodingError"):
        fetch(client, "https://msd.example/v", robots=False, sleep=clock.sleep, now=clock.now)
    assert clock.sleeps == []


def test_client_errors_raise_with_status() -> None:
    with client_for(site({})) as client, pytest.raises(FetchError) as info:
        fetch(client, "https://h.example/missing", robots=False, sleep=lambda s: None)
    assert info.value.status == 404


def test_per_host_delay() -> None:
    clock = Clock()
    with client_for(lambda r: httpx.Response(200)) as client:
        for _ in range(3):
            fetch(
                client,
                "https://slow.example/x",
                robots=False,
                sleep=clock.sleep,
                now=clock.now,
                min_interval=5.0,
            )
        fetch(
            client,
            "https://other.example/x",
            robots=False,
            sleep=clock.sleep,
            now=clock.now,
            min_interval=5.0,
        )
    assert clock.sleeps == [5.0, 5.0]


def test_offline_transport_refuses_everything() -> None:
    with make_client(transport=OfflineTransport(), resolver=resolver_for({})) as client:
        with pytest.raises(OfflineError, match="offline"):
            fetch(client, "https://epoch.example/data.zip", robots=False, sleep=lambda s: None)
        # robots.txt is not treated as a network failure (which would read as "disallowed").
        with pytest.raises(OfflineError):
            fetch(client, "https://epoch.example/data.zip", sleep=lambda s: None)
