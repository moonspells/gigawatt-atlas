from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from atlas.net import (
    DEFAULT_USER_AGENT,
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
