"""End-to-end coverage for the OAuth authorization-code flow.

These exercise the connector handshake the way a real MCP client performs it:
dynamic registration, authorize, owner login, code redemption and refresh. The
negative cases are the point -- a code must not be redeemable twice, by another
client, without its PKCE verifier, or against an unregistered redirect_uri.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from app.oauth import OAuthHandlers
from app.security import pkce_s256
from app.settings import Settings
from app.storage import Store
from tests.conftest import ADMIN_BASE_URL, OWNER_PASSWORD, OWNER_USERNAME

REDIRECT_URI = "https://connector.example.test/callback"
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = pkce_s256(VERIFIER)


@pytest.fixture
def oauth(load_main) -> tuple[TestClient, Any]:
    main = load_main()
    return TestClient(main.app, base_url=ADMIN_BASE_URL), main


def test_authorization_code_flow_issues_a_working_access_token(oauth) -> None:
    client, main = oauth
    registration = _register(client)
    assert registration["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in registration

    code, state = _authorize_and_login(client, registration["client_id"], state="xyz-123")
    assert state == "xyz-123"

    tokens = _redeem(client, registration["client_id"], code)
    assert tokens.status_code == 200
    body = tokens.json()
    assert body["token_type"] == "Bearer"
    assert body["scope"] == "bridge"
    assert body["expires_in"] == 1800
    assert tokens.headers["cache-control"] == "no-store"

    verified = _verify(main, body["access_token"])
    assert verified is not None
    assert verified.client_id == registration["client_id"]
    assert verified.scopes == ["bridge"]

    # The bearer token is opaque: only its hash may reach the database.
    assert _stored_token_column(main, body["access_token"]) == 0


def test_authorization_code_cannot_be_redeemed_twice(oauth) -> None:
    client, main = oauth
    registration = _register(client)
    code, _ = _authorize_and_login(client, registration["client_id"])

    first = _redeem(client, registration["client_id"], code)
    assert first.status_code == 200

    replay = _redeem(client, registration["client_id"], code)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    # The token minted by the legitimate first redemption stays valid.
    assert _verify(main, first.json()["access_token"]) is not None


def test_authorization_code_rejects_wrong_or_missing_pkce_verifier(oauth) -> None:
    client, _ = oauth
    registration = _register(client)
    code, _ = _authorize_and_login(client, registration["client_id"])

    wrong = _redeem(client, registration["client_id"], code, verifier="not-the-verifier")
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_grant"

    missing = _redeem(client, registration["client_id"], code, verifier="")
    assert missing.status_code == 400

    # A failed verifier must not burn the code -- the honest client can retry.
    assert _redeem(client, registration["client_id"], code).status_code == 200


def test_authorization_code_is_bound_to_the_client_that_requested_it(oauth) -> None:
    client, _ = oauth
    victim = _register(client)
    attacker = _register(client, redirect_uris=["https://attacker.example.test/callback"])
    code, _ = _authorize_and_login(client, victim["client_id"])

    stolen = _redeem(client, attacker["client_id"], code)

    assert stolen.status_code == 400
    assert stolen.json()["error"] == "invalid_grant"
    assert _redeem(client, victim["client_id"], code).status_code == 200


def test_authorize_rejects_a_redirect_uri_that_was_never_registered(oauth) -> None:
    client, _ = oauth
    registration = _register(client)

    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": "https://attacker.example.test/steal",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_token_rejects_a_redirect_uri_that_differs_from_the_code(oauth) -> None:
    client, _ = oauth
    registration = _register(
        client, redirect_uris=[REDIRECT_URI, "https://connector.example.test/other"]
    )
    code, _ = _authorize_and_login(client, registration["client_id"])

    response = _redeem(
        client,
        registration["client_id"],
        code,
        redirect_uri="https://connector.example.test/other",
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_authorize_requires_pkce_s256(oauth) -> None:
    client, _ = oauth
    registration = _register(client)
    base = {
        "response_type": "code",
        "client_id": registration["client_id"],
        "redirect_uri": REDIRECT_URI,
    }

    plain = client.get(
        "/oauth/authorize",
        params={**base, "code_challenge": CHALLENGE, "code_challenge_method": "plain"},
        follow_redirects=False,
    )
    assert plain.status_code == 400

    absent = client.get(
        "/oauth/authorize",
        params={**base, "code_challenge_method": "S256"},
        follow_redirects=False,
    )
    assert absent.status_code == 400
    assert absent.json()["error"] == "invalid_request"


def test_owner_login_rejects_bad_credentials_without_issuing_a_code(oauth) -> None:
    client, _ = oauth
    registration = _register(client)
    challenge = _authorize(client, registration["client_id"])

    response = client.post(
        "/oauth/login",
        data={"challenge": challenge, "username": OWNER_USERNAME, "password": "wrong"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "location" not in response.headers
    assert "Wrong username or password." in response.text
    # The password must never be echoed back into the retry form.
    assert "wrong" not in response.text


def test_expired_authorization_challenge_cannot_be_completed(oauth, monkeypatch) -> None:
    client, main = oauth
    registration = _register(client)
    challenge = _authorize(client, registration["client_id"])

    expired = time.time() + main.settings.auth_challenge_seconds + 1
    monkeypatch.setattr("app.oauth.time.time", lambda: expired)

    response = client.post(
        "/oauth/login",
        data={
            "challenge": challenge,
            "username": OWNER_USERNAME,
            "password": OWNER_PASSWORD,
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "expired" in response.text.lower()


def test_expired_authorization_code_is_not_redeemable(oauth, monkeypatch) -> None:
    client, main = oauth
    registration = _register(client)
    code, _ = _authorize_and_login(client, registration["client_id"])

    expired = time.time() + main.settings.auth_code_seconds + 1
    monkeypatch.setattr("app.oauth.time.time", lambda: expired)

    response = _redeem(client, registration["client_id"], code)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_refresh_rotates_the_token_and_retires_the_old_one(oauth) -> None:
    client, main = oauth
    registration = _register(client)
    code, _ = _authorize_and_login(client, registration["client_id"])
    first = _redeem(client, registration["client_id"], code).json()

    second = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": registration["client_id"],
        },
    )

    assert second.status_code == 200
    rotated = second.json()
    assert rotated["refresh_token"] != first["refresh_token"]
    assert rotated["access_token"] != first["access_token"]
    assert _verify(main, rotated["access_token"]) is not None

    replay = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": registration["client_id"],
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_refresh_cannot_widen_the_scope_it_was_granted(oauth) -> None:
    client, _ = oauth
    registration = _register(client)
    code, _ = _authorize_and_login(client, registration["client_id"])
    granted = _redeem(client, registration["client_id"], code).json()
    assert granted["scope"] == "bridge"

    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": granted["refresh_token"],
            "client_id": registration["client_id"],
            "scope": "bridge offline_access",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"


def test_confidential_client_must_present_its_secret(oauth) -> None:
    client, _ = oauth
    registration = _register(client, token_endpoint_auth_method="client_secret_post")
    secret = registration["client_secret"]
    assert secret

    code, _ = _authorize_and_login(client, registration["client_id"])

    without = _redeem(client, registration["client_id"], code)
    assert without.status_code == 401
    assert without.json()["error"] == "invalid_client"

    wrong = _redeem(
        client, registration["client_id"], code, extra={"client_secret": "guessed"}
    )
    assert wrong.status_code == 401

    correct = _redeem(
        client, registration["client_id"], code, extra={"client_secret": secret}
    )
    assert correct.status_code == 200


def test_token_endpoint_rejects_unknown_clients_and_grant_types(oauth) -> None:
    client, _ = oauth
    registration = _register(client)

    unknown = client.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "client_id": "mcp_does_not_exist"},
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"] == "invalid_client"

    unsupported = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": registration["client_id"],
        },
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["error"] == "unsupported_grant_type"


def test_registration_rejects_metadata_the_bridge_cannot_honour(oauth) -> None:
    client, _ = oauth

    for payload, expected in (
        ({}, "invalid_redirect_uri"),
        ({"redirect_uris": []}, "invalid_redirect_uri"),
        ({"redirect_uris": [REDIRECT_URI], "scope": "admin"}, "invalid_scope"),
        (
            {"redirect_uris": [REDIRECT_URI], "token_endpoint_auth_method": "private_key_jwt"},
            "invalid_client_metadata",
        ),
        (
            {"redirect_uris": [REDIRECT_URI], "grant_types": ["client_credentials"]},
            "invalid_client_metadata",
        ),
    ):
        response = client.post("/oauth/register", json=payload)
        assert response.status_code == 400, payload
        assert response.json()["error"] == expected, payload


def test_authorize_rejects_a_resource_outside_this_bridge(oauth) -> None:
    client, _ = oauth
    registration = _register(client)

    response = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": "https://someone-else.example.test/mcp",
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_target"


def test_discovery_documents_describe_the_flow_the_bridge_implements(oauth) -> None:
    client, main = oauth

    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"] == main.settings.issuer_url
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert metadata["authorization_endpoint"] == main.settings.authz_endpoint

    resource = client.get("/.well-known/oauth-protected-resource").json()
    assert resource["resource"] == main.settings.resource_url
    assert resource["authorization_servers"] == [main.settings.issuer_url]


def test_oauth_accepts_public_base_url_as_resource_alias(tmp_path: Path) -> None:
    handler = OAuthHandlers(_settings(tmp_path), Store(tmp_path / "bridge.sqlite3"))

    assert handler._canonical_resource("https://mcp.example.test") == "https://mcp.example.test/mcp"
    assert handler._canonical_resource("https://mcp.example.test/") == "https://mcp.example.test/mcp"
    assert handler._canonical_resource("https://mcp.example.test/mcp") == "https://mcp.example.test/mcp"
    assert handler._canonical_resource("https://other.example.test") is None


def _register(client: TestClient, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"redirect_uris": [REDIRECT_URI], **overrides}
    response = client.post("/oauth/register", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _authorize(client: TestClient, client_id: str, state: str | None = None) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
    }
    if state is not None:
        params["state"] = state
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 302, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["challenge"][0]


def _authorize_and_login(
    client: TestClient, client_id: str, state: str | None = None
) -> tuple[str, str | None]:
    challenge = _authorize(client, client_id, state)
    response = client.post(
        "/oauth/login",
        data={
            "challenge": challenge,
            "username": OWNER_USERNAME,
            "password": OWNER_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT_URI
    query = parse_qs(location.query)
    returned_state = query["state"][0] if "state" in query else None
    return query["code"][0], returned_state


def _redeem(
    client: TestClient,
    client_id: str,
    code: str,
    *,
    verifier: str = VERIFIER,
    redirect_uri: str = REDIRECT_URI,
    extra: dict[str, str] | None = None,
) -> Any:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        **(extra or {}),
    }
    return client.post("/oauth/token", data=data)


def _verify(main: Any, access_token: str) -> Any:
    import asyncio

    return asyncio.run(main.BridgeTokenVerifier().verify_token(access_token))


def _stored_token_column(main: Any, access_token: str) -> int:
    """Count rows anywhere in the database holding the raw bearer token."""
    with main.store._connect() as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
        hits = 0
        for table in tables:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for column in columns:
                hits += connection.execute(
                    f"SELECT count(*) FROM {table} WHERE {column} = ?", (access_token,)
                ).fetchone()[0]
    return hits


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        public_base_url="https://mcp.example.test",
        resource_path="/mcp",
        db_path=tmp_path / "bridge.sqlite3",
        context_packs_dir=tmp_path / "context-packs",
        default_context_pack_id="manual-context",
        transcript_chunk_max_lines=180,
        transcript_chunk_max_chars=12000,
        owner_username="owner",
        owner_password_hash="hash",
        secret_key="secret",
        access_token_seconds=1800,
        refresh_token_seconds=2592000,
        auth_code_seconds=300,
        auth_challenge_seconds=600,
        scope="bridge",
        transport_allowed_hosts=["mcp.example.test"],
        transport_allowed_origins=["https://chatgpt.com"],
    )
