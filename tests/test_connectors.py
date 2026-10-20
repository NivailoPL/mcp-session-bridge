"""Connector sign-in safety: who may connect, what the owner sees, and how to cut access.

The scenario these guard against: someone registers their own client with their
own redirect URI and sends the owner a sign-in link on the owner's Bridge. The
consent page must say where access goes, the optional lock must refuse such a
first sign-in unless the owner opened a window, and the owner must be able to
disconnect any harness afterwards.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from app.connectors import RedirectUriError, redirect_kind, redirect_label
from app.rate_limits import LoginGuard, SlidingWindowLimiter
from app.security import pkce_s256
from app.storage import Store
from tests.conftest import ADMIN_BASE_URL, OWNER_PASSWORD, OWNER_USERNAME

WEB_REDIRECT = "https://harness.example.test/oauth/callback"
LOCAL_REDIRECT = "http://127.0.0.1:53682/callback"
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"


@pytest.fixture
def bridge(load_main) -> tuple[TestClient, Any]:
    main = load_main()
    return TestClient(main.app, base_url=ADMIN_BASE_URL), main


# ---------- redirect URI rules ----------


@pytest.mark.parametrize(
    ("uri", "kind"),
    [
        ("https://claude.ai/api/mcp/auth_callback", "web"),
        ("https://chatgpt.com/connector_platform_oauth_redirect", "web"),
        ("https://127.attacker.example/cb", "web"),
        ("http://127.0.0.1:53682/callback", "local"),
        ("http://localhost:8080/callback", "local"),
        ("http://[::1]:9000/cb", "local"),
        ("https://localhost/cb", "local"),
        ("cursor://anysphere.cursor-retrieval/oauth/callback", "app"),
        ("com.example.app:/oauth2redirect", "app"),
    ],
)
def test_redirect_kinds(uri: str, kind: str) -> None:
    assert redirect_kind(uri) == kind


@pytest.mark.parametrize(
    "uri",
    [
        "http://harness.example.test/cb",
        "http://localhost.attacker.example/cb",
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///etc/passwd",
        "https://harness.example.test/cb#fragment",
        "https:///no-host",
        "/relative/path",
        "https://harness.example.test/a b",
        "https://" + "a" * 2001 + ".test/",
    ],
)
def test_unusable_redirect_uris_are_refused(uri: str) -> None:
    with pytest.raises(RedirectUriError):
        redirect_kind(uri)


def test_redirect_labels_show_the_destination_not_the_path() -> None:
    assert redirect_label("https://claude.ai/api/mcp/auth_callback") == "claude.ai"
    assert redirect_label("https://harness.example.test:8443/cb") == "harness.example.test:8443"
    assert redirect_label("http://127.0.0.1:53682/callback") == "127.0.0.1:53682"
    assert redirect_label("http://[::1]:9000/cb") == "[::1]:9000"
    assert redirect_label("cursor://anysphere.cursor-retrieval/cb") == "cursor://"


def test_registration_refuses_unusable_redirects_and_cleans_the_name(bridge) -> None:
    client, main = bridge

    plain_http = client.post("/oauth/register", json={"redirect_uris": ["http://harness.example.test/cb"]})
    assert plain_http.status_code == 400
    assert plain_http.json()["error"] == "invalid_redirect_uri"

    too_many = client.post(
        "/oauth/register",
        json={"redirect_uris": [f"https://harness.example.test/cb{i}" for i in range(11)]},
    )
    assert too_many.status_code == 400

    not_a_list = client.post(
        "/oauth/register", json={"redirect_uris": [WEB_REDIRECT], "grant_types": "authorization_code"}
    )
    assert not_a_list.status_code == 400
    assert not_a_list.json()["error"] == "invalid_client_metadata"

    named = client.post(
        "/oauth/register",
        json={"redirect_uris": [WEB_REDIRECT], "client_name": "  Claude\n\x00  " + "x" * 300},
    )
    assert named.status_code == 201
    name = named.json()["client_name"]
    assert name.startswith("Claude x") and len(name) == 120 and "\n" not in name
    assert main.store.get_client(named.json()["client_id"]).client_name == name


# ---------- consent page ----------


def test_consent_page_says_where_access_goes_and_cannot_be_framed(bridge) -> None:
    client, _ = bridge
    registration = _register(client, client_name='<script>alert("x")</script>Claude')
    challenge = _authorize(client, registration["client_id"])

    page = client.get(f"/oauth/login?challenge={challenge}")

    assert page.status_code == 200
    assert "Access will be sent to" in page.text
    assert "<strong>harness.example.test</strong>" in page.text
    assert "First time" in page.text
    assert "<script>" not in page.text
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;Claude" in page.text
    assert 'class="login-logo"' in page.text and "<svg" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"


def test_consent_page_recognises_hosts_the_owner_approved_before(bridge) -> None:
    client, _ = bridge
    first = _register(client)
    _sign_in(client, first["client_id"])

    second = _register(client)
    page = client.get(f"/oauth/login?challenge={_authorize(client, second['client_id'])}")
    assert "Known address" in page.text

    again = client.get(f"/oauth/login?challenge={_authorize(client, first['client_id'])}")
    assert "Connected before" in again.text


def test_consent_page_marks_apps_on_this_computer(bridge) -> None:
    client, _ = bridge
    registration = _register(client, redirect_uris=[LOCAL_REDIRECT])
    page = client.get(
        f"/oauth/login?challenge={_authorize(client, registration['client_id'], LOCAL_REDIRECT)}"
    )
    assert "This computer" in page.text
    assert "127.0.0.1:53682" in page.text


def test_unknown_or_missing_challenge_shows_the_expired_page(bridge) -> None:
    client, _ = bridge
    page = client.get("/oauth/login?challenge=nope")
    assert page.status_code == 400
    assert "expired" in page.text.lower()


# ---------- one-click approval ----------


def test_a_browser_signed_in_to_admin_approves_with_one_click(bridge, admin_client) -> None:
    _, main = bridge
    browser, _ = admin_client(main)
    consent_cookie = next(c for c in browser.cookies.jar if c.name == "mcp_bridge_consent")
    assert consent_cookie.path == "/oauth"

    registration = _register(browser)
    challenge = _authorize(browser, registration["client_id"])
    page = browser.get(f"/oauth/login?challenge={challenge}")
    assert ">Allow<" in page.text and ">Deny<" in page.text
    assert 'type="password"' not in page.text
    assert "harness.example.test" in page.text

    allowed = browser.post(
        "/oauth/login",
        data={"challenge": challenge, "approval": _approval(page.text), "action": "allow"},
        follow_redirects=False,
    )
    assert allowed.status_code == 302
    code = parse_qs(urlsplit(allowed.headers["location"]).query)["code"][0]
    assert _redeem(browser, registration["client_id"], code).status_code == 200


def test_one_click_approval_needs_this_browsers_cookie_and_this_sign_in(bridge, admin_client) -> None:
    _, main = bridge
    browser, _ = admin_client(main)
    registration = _register(browser)
    first = _authorize(browser, registration["client_id"])
    token = _approval(browser.get(f"/oauth/login?challenge={first}").text)
    second = _authorize(browser, registration["client_id"])

    reused = browser.post("/oauth/login", data={"challenge": second, "approval": token}, follow_redirects=False)
    assert reused.status_code == 401
    assert 'type="password"' in reused.text

    stranger = TestClient(main.app, base_url=ADMIN_BASE_URL)
    no_cookie = stranger.post("/oauth/login", data={"challenge": first, "approval": token}, follow_redirects=False)
    assert no_cookie.status_code == 401
    assert "location" not in no_cookie.headers


def test_password_sign_in_remembers_the_browser_and_sign_out_forgets_it(bridge, admin_client) -> None:
    client, main = bridge
    registration = _register(client)
    _sign_in(client, registration["client_id"])

    again = client.get(f"/oauth/login?challenge={_authorize(client, registration['client_id'])}")
    assert ">Allow<" in again.text

    browser, _ = admin_client(main)
    browser.post("/admin/logout", follow_redirects=False)
    after = browser.get(f"/oauth/login?challenge={_authorize(browser, registration['client_id'])}")
    assert 'type="password"' in after.text


def test_one_click_approval_lasts_as_long_as_an_admin_session(bridge, admin_client, monkeypatch) -> None:
    _, main = bridge
    browser, _ = admin_client(main)
    registration = _register(browser)

    later = time.time() + 12 * 60 * 60 + 1
    monkeypatch.setattr(main.oauth.consent, "clock", lambda: later)
    page = browser.get(f"/oauth/login?challenge={_authorize(browser, registration['client_id'])}")
    assert 'type="password"' in page.text


def test_deny_returns_access_denied_and_ends_the_sign_in(bridge) -> None:
    client, _ = bridge
    registration = _register(client)
    response = client.get(
        "/oauth/authorize", params={**_authorize_params(registration["client_id"]), "state": "s-1"}, follow_redirects=False
    )
    challenge = parse_qs(urlsplit(response.headers["location"]).query)["challenge"][0]
    page = client.get(f"/oauth/login?challenge={challenge}")
    assert 'value="deny" formnovalidate' in page.text

    denied = client.post("/oauth/login", data={"challenge": challenge, "action": "deny"}, follow_redirects=False)

    location = urlsplit(denied.headers["location"])
    assert denied.status_code == 302
    assert f"{location.scheme}://{location.netloc}{location.path}" == WEB_REDIRECT
    query = parse_qs(location.query)
    assert query["error"] == ["access_denied"] and query["state"] == ["s-1"] and "code" not in query
    assert client.get(f"/oauth/login?challenge={challenge}").status_code == 400


# ---------- the connection lock ----------


def test_lock_is_off_by_default_so_a_new_harness_connects_straight_away(bridge) -> None:
    client, main = bridge
    assert main.connection_lock.enabled() is False
    registration = _register(client)
    code = _sign_in(client, registration["client_id"])
    assert _redeem(client, registration["client_id"], code).status_code == 200


def test_locked_bridge_refuses_a_new_web_harness_without_storing_anything(bridge, admin_client) -> None:
    client, main = bridge
    admin, csrf = admin_client(main)
    assert _admin(admin, csrf, "PUT", "/admin/api/connectors/lock", {"enabled": True}).status_code == 200
    registration = _register(client)

    response = client.get("/oauth/authorize", params=_authorize_params(registration["client_id"]), follow_redirects=False)

    assert response.status_code == 403
    assert "Connections locked" in response.text
    assert "harness.example.test" in response.text
    assert 'href="/oauth/authorize?' in response.text
    assert 'href="/admin/sessions?settings=connectors"' in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert main.store.count_pending_oauth()["challenges"] == 0


def test_open_window_lets_a_new_harness_in_and_closing_it_locks_again(bridge, admin_client) -> None:
    client, main = bridge
    admin, csrf = admin_client(main)
    _admin(admin, csrf, "PUT", "/admin/api/connectors/lock", {"enabled": True})

    opened = _admin(admin, csrf, "POST", "/admin/api/connectors/window", {"minutes": 10}).json()
    assert opened["window_open"] is True
    assert 590 <= opened["window_remaining_seconds"] <= 600

    inside = _register(client)
    code = _sign_in(client, inside["client_id"])
    assert _redeem(client, inside["client_id"], code).status_code == 200

    closed = _admin(admin, csrf, "DELETE", "/admin/api/connectors/window").json()
    assert closed["window_open"] is False
    outside = _register(client)
    blocked = client.get("/oauth/authorize", params=_authorize_params(outside["client_id"]), follow_redirects=False)
    assert blocked.status_code == 403

    # Once approved, a harness signs in again without a window.
    assert _sign_in(client, inside["client_id"])


def test_lock_never_blocks_harnesses_on_this_computer(bridge, admin_client) -> None:
    client, main = bridge
    admin, csrf = admin_client(main)
    _admin(admin, csrf, "PUT", "/admin/api/connectors/lock", {"enabled": True})
    registration = _register(client, redirect_uris=[LOCAL_REDIRECT])
    assert _sign_in(client, registration["client_id"], LOCAL_REDIRECT)


def test_window_expires_by_itself(bridge, admin_client, monkeypatch) -> None:
    client, main = bridge
    admin, csrf = admin_client(main)
    _admin(admin, csrf, "PUT", "/admin/api/connectors/lock", {"enabled": True})
    _admin(admin, csrf, "POST", "/admin/api/connectors/window", {"minutes": 10})

    later = time.time() + 601
    monkeypatch.setattr(main.connection_lock, "clock", lambda: later)

    registration = _register(client)
    response = client.get("/oauth/authorize", params=_authorize_params(registration["client_id"]), follow_redirects=False)
    assert response.status_code == 403


def test_window_controls_validate_their_input(bridge, admin_client) -> None:
    _, main = bridge
    admin, csrf = admin_client(main)

    assert _admin(admin, csrf, "POST", "/admin/api/connectors/window", {"minutes": 10}).status_code == 409
    _admin(admin, csrf, "PUT", "/admin/api/connectors/lock", {"enabled": True})
    assert _admin(admin, csrf, "POST", "/admin/api/connectors/window", {"minutes": 5}).status_code == 400
    assert _admin(admin, csrf, "POST", "/admin/api/connectors/window", {"minutes": "10"}).status_code == 400
    assert _admin(admin, csrf, "PUT", "/admin/api/connectors/lock", {"enabled": "yes"}).status_code == 400

    _admin(admin, csrf, "POST", "/admin/api/connectors/window", {"minutes": 60})
    turned_off = _admin(admin, csrf, "PUT", "/admin/api/connectors/lock", {"enabled": False}).json()
    assert turned_off["lock_enabled"] is False and turned_off["window_open"] is False

    no_csrf = admin.put("/admin/api/connectors/lock", json={"enabled": True})
    assert no_csrf.status_code == 403
    anonymous = TestClient(main.app, base_url=ADMIN_BASE_URL).get("/admin/api/connectors")
    assert anonymous.status_code == 401


# ---------- disconnecting ----------


def test_disconnect_ends_access_at_once(bridge, admin_client) -> None:
    client, main = bridge
    admin, csrf = admin_client(main)
    registration = _register(client, client_name="Harness")
    tokens = _redeem(client, registration["client_id"], _sign_in(client, registration["client_id"])).json()
    assert _verify(main, tokens["access_token"]) is not None

    overview = admin.get("/admin/api/connectors").json()
    assert [c["client_id"] for c in overview["clients"]] == [registration["client_id"]]
    listed = overview["clients"][0]
    assert listed["name"] == "Harness"
    assert listed["destinations"] == [{"label": "harness.example.test", "kind": "web"}]
    assert listed["signed_in"] is True and listed["last_active_at"]

    removed = _admin(admin, csrf, "DELETE", f"/admin/api/connectors/{registration['client_id']}")
    assert removed.status_code == 200
    assert removed.json()["clients"] == []
    assert _verify(main, tokens["access_token"]) is None
    refresh = client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": registration["client_id"]},
    )
    assert refresh.status_code == 401

    again = _admin(admin, csrf, "DELETE", f"/admin/api/connectors/{registration['client_id']}")
    assert again.status_code == 404


def test_last_activity_outlives_the_purge_of_expired_access_tokens(bridge, monkeypatch) -> None:
    client, main = bridge
    registration = _register(client)
    _redeem(client, registration["client_id"], _sign_in(client, registration["client_id"]))

    later = time.time() + 2 * 60 * 60
    monkeypatch.setattr("app.storage.time.time", lambda: later)
    assert main.store.purge_oauth()["access_tokens"] == 1

    [listed] = main.store.list_connected_clients()
    assert listed["last_active_at"] and listed["has_live_token"] is True


def test_clients_that_never_signed_in_are_not_listed(bridge, admin_client) -> None:
    client, main = bridge
    admin, _ = admin_client(main)
    _register(client)
    assert admin.get("/admin/api/connectors").json()["clients"] == []


# ---------- sign-in limits ----------


def test_wrong_passwords_lock_the_address_out_of_both_sign_in_forms(bridge) -> None:
    client, _ = bridge
    registration = _register(client)
    challenge = _authorize(client, registration["client_id"])

    for _ in range(5):
        wrong = client.post("/oauth/login", data={"challenge": challenge, "username": OWNER_USERNAME, "password": "nope"})
        assert wrong.status_code == 401

    blocked = client.post(
        "/oauth/login",
        data={"challenge": challenge, "username": OWNER_USERNAME, "password": OWNER_PASSWORD},
        follow_redirects=False,
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert "Too many sign-in attempts" in blocked.text

    admin_blocked = client.post(
        "/admin/login",
        data={"username": OWNER_USERNAME, "password": OWNER_PASSWORD, "next": "/admin/sessions"},
        follow_redirects=False,
    )
    assert admin_blocked.status_code == 429


def test_a_successful_sign_in_clears_earlier_failures() -> None:
    guard = LoginGuard()
    hashed = _password_hash("right")

    async def attempts() -> list[bool]:
        results = []
        for password in ("wrong", "wrong", "wrong", "wrong", "right", "wrong", "wrong", "wrong", "wrong"):
            results.append(await guard.check("1.2.3.4", "owner", password, "owner", hashed))
        return results

    asyncio.run(attempts())
    assert guard.retry_after("1.2.3.4") == 0


def test_failures_from_many_addresses_slow_everyone_down() -> None:
    now = [1000.0]
    limiter = SlidingWindowLimiter(3, 60, clock=lambda: now[0])
    for _ in range(3):
        limiter.hit("*")
    assert limiter.retry_after("*") == 60
    now[0] += 61
    assert limiter.retry_after("*") == 0


def test_sliding_window_limiter_forgets_old_keys() -> None:
    now = [0.0]
    limiter = SlidingWindowLimiter(1, 10, clock=lambda: now[0], max_keys=3)
    for key in ("a", "b", "c", "d"):
        limiter.hit(key)
    assert len(limiter._hits) <= 3


def test_registration_is_rate_limited_per_address(bridge) -> None:
    client, _ = bridge
    for _ in range(20):
        assert client.post("/oauth/register", json={"redirect_uris": [WEB_REDIRECT]}).status_code == 201
    limited = client.post("/oauth/register", json={"redirect_uris": [WEB_REDIRECT]})
    assert limited.status_code == 429
    assert limited.json()["error"] == "temporarily_unavailable"


@pytest.mark.parametrize("path", ["/oauth/register", "/oauth/login", "/oauth/token", "/admin/login"])
def test_sign_in_endpoints_refuse_oversized_bodies(bridge, path: str) -> None:
    client, _ = bridge
    response = client.post(path, content=b"a=" + b"x" * 20_000, headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413


# ---------- storage ----------


def test_existing_clients_with_tokens_stay_connected_after_the_upgrade(tmp_path: Path) -> None:
    db = tmp_path / "bridge.sqlite3"
    Store(db)
    with sqlite3.connect(db) as conn:
        for client_id in ("mcp_used", "mcp_abandoned"):
            conn.execute(
                "INSERT INTO clients (client_id, auth_method, redirect_uris, grant_types, response_types, scope, created_at)"
                " VALUES (?, 'none', ?, '[]', '[]', 'bridge', 100)",
                (client_id, f'["{WEB_REDIRECT}"]'),
            )
        conn.execute(
            "INSERT INTO refresh_tokens (token_hash, client_id, scopes, created_at) VALUES ('h', 'mcp_used', '[]', 100)"
        )
        conn.execute("ALTER TABLE clients DROP COLUMN approved_at")

    store = Store(db)

    assert store.get_client("mcp_used").approved_at == 100
    assert store.get_client("mcp_abandoned").approved_at is None
    # A later start must not approve clients that registered after the upgrade.
    Store(db)
    assert store.get_client("mcp_abandoned").approved_at is None


def test_purge_drops_expired_state_and_abandoned_clients(bridge, monkeypatch) -> None:
    client, main = bridge
    approved = _register(client)
    _sign_in(client, approved["client_id"])
    abandoned = _register(client)
    _authorize(client, abandoned["client_id"])

    later = time.time() + 2 * 24 * 60 * 60
    monkeypatch.setattr("app.storage.time.time", lambda: later)
    removed = main.store.purge_oauth()

    assert removed["challenges"] >= 1 and removed["codes"] >= 1 and removed["clients"] == 1
    assert main.store.get_client(abandoned["client_id"]) is None
    assert main.store.get_client(approved["client_id"]) is not None


# ---------- helpers ----------


def _register(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post("/oauth/register", json={"redirect_uris": [WEB_REDIRECT], **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def _authorize_params(client_id: str, redirect_uri: str = WEB_REDIRECT) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": pkce_s256(VERIFIER),
        "code_challenge_method": "S256",
    }


def _authorize(client: TestClient, client_id: str, redirect_uri: str = WEB_REDIRECT) -> str:
    response = client.get("/oauth/authorize", params=_authorize_params(client_id, redirect_uri), follow_redirects=False)
    assert response.status_code == 302, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["challenge"][0]


def _sign_in(client: TestClient, client_id: str, redirect_uri: str = WEB_REDIRECT) -> str:
    challenge = _authorize(client, client_id, redirect_uri)
    response = client.post(
        "/oauth/login",
        data={"challenge": challenge, "username": OWNER_USERNAME, "password": OWNER_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["code"][0]


def _redeem(client: TestClient, client_id: str, code: str, redirect_uri: str = WEB_REDIRECT) -> Any:
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        },
    )


def _admin(admin: TestClient, csrf: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    return admin.request(method, path, json=payload, headers={"x-csrf-token": csrf})


def _verify(main: Any, access_token: str) -> Any:
    return asyncio.run(main.BridgeTokenVerifier().verify_token(access_token))


def _approval(page: str) -> str:
    return page.split('name="approval" value="', 1)[1].split('"', 1)[0]


def _password_hash(password: str) -> str:
    from app.security import password_hash

    return password_hash(password)
