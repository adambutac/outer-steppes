"""OAuth for the grok.com custom connector: authorize, token, refresh, bearer."""

from __future__ import annotations

import base64
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest

from outer_steppes import oauth, registry
from outer_steppes.db import transaction
from outer_steppes.oauth import MSG_CONNECTION_INVALID, OAuthServer, pkce_s256

from .conftest import CALLER, NOW, Clock, make_config, register

CALLBACK = "https://grok.com/connectors-oauth-exchange-code/"
RESOURCE = "https://bridge.example/mcp"


@pytest.fixture
def config(db_path):
    return make_config(database_path=db_path)


@pytest.fixture
def server(conn, config, clock):
    return OAuthServer(conn, config, clock)


@pytest.fixture
def client(conn):
    registration_id = register(conn)
    with transaction(conn):
        client_id, secret = oauth.issue_client_in_tx(conn, registration_id, NOW)
    return client_id, secret


def authorize_params(client_id, verifier, **overrides):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "st-123",
              "code_challenge": pkce_s256(verifier), "code_challenge_method": "S256", "resource": RESOURCE,
              "scope": "mcp"}
    params.update(overrides)
    return {k: v for k, v in params.items() if v is not None}


def redirect_query(result):
    parts = urlsplit(result.location)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == CALLBACK
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


def get_code(server, client_id, verifier):
    result = server.authorize(authorize_params(client_id, verifier))
    assert result.status == 302
    return redirect_query(result)["code"]


def exchange(server, client_id, secret, code, verifier, **overrides):
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier,
            "resource": RESOURCE, "client_id": client_id, "client_secret": secret}
    form.update(overrides)
    return server.token({k: v for k, v in form.items() if v is not None}, None)


VERIFIER = "v" * 43


# ------------------------------------------------------------------ issuing clients

def test_client_secret_is_stored_hashed(conn, client):
    client_id, secret = client
    assert client_id.startswith("osc_") and secret.startswith("oss_")
    stored = conn.execute("SELECT secret_hash FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()[0]
    assert secret not in stored and stored == oauth._hash(secret)


# ------------------------------------------------------------------ authorize

def test_authorize_redirects_with_code_and_state(server, conn, client):
    result = server.authorize(authorize_params(client[0], VERIFIER))
    query = redirect_query(result)
    assert query["state"] == "st-123" and len(query["code"]) >= 40
    stored = conn.execute("SELECT code_hash, expires_at FROM oauth_codes").fetchone()
    assert stored["code_hash"] == oauth._hash(query["code"]) and stored["expires_at"] == NOW + 60


@pytest.mark.parametrize("redirect", ["https://evil.example/cb", "https://grok.com/connectors-oauth-exchange-code",
                                      None])
def test_bad_callback_never_redirects(server, client, redirect):
    result = server.authorize(authorize_params(client[0], VERIFIER, redirect_uri=redirect))
    assert (result.status, result.location) == (400, None)


def test_unknown_or_revoked_client_never_redirects(server, conn, client):
    assert server.authorize(authorize_params("osc_nope", VERIFIER)).status == 400
    conn.execute("UPDATE registrations SET revoked_at = ?", (NOW,))
    assert server.authorize(authorize_params(client[0], VERIFIER)).status == 400


@pytest.mark.parametrize("overrides,error", [
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"code_challenge_method": None}, "invalid_request"),
    ({"code_challenge": None}, "invalid_request"),
    ({"code_challenge": "short"}, "invalid_request"),
    ({"resource": "https://evil.example/mcp"}, "invalid_target"),
    ({"scope": "mcp admin"}, "invalid_scope"),
])
def test_authorize_errors_redirect_with_state(server, conn, client, overrides, error):
    result = server.authorize(authorize_params(client[0], VERIFIER, **overrides))
    query = redirect_query(result)
    assert (query["error"], query["state"]) == (error, "st-123")
    assert "code" not in query
    assert conn.execute("SELECT count(*) FROM oauth_codes").fetchone()[0] == 0


@pytest.mark.parametrize("scope", ["mcp", "", None])
def test_scope_may_be_mcp_or_absent(server, client, scope):
    assert "code" in redirect_query(server.authorize(authorize_params(client[0], VERIFIER, scope=scope)))


def test_resource_may_be_absent(server, client):
    assert "code" in redirect_query(server.authorize(authorize_params(client[0], VERIFIER, resource=None)))


# ------------------------------------------------------------------ token: authorization code

def test_code_exchange_issues_tokens_stored_hashed(server, conn, client):
    client_id, secret = client
    result = exchange(server, client_id, secret, get_code(server, client_id, VERIFIER), VERIFIER)
    assert result.status == 200
    body = result.body
    assert body["token_type"] == "Bearer" and body["scope"] == "mcp" and body["expires_in"] == 3_600
    assert body["access_token"].startswith("mcp_") and body["refresh_token"].startswith("mcpr_")
    rows = {r["kind"]: r for r in conn.execute("SELECT * FROM oauth_tokens")}
    assert rows["access"]["token_hash"] == oauth._hash(body["access_token"])
    assert rows["refresh"]["expires_at"] == NOW + 180 * 86_400
    assert server.registration_for_access_token("Bearer " + body["access_token"]) == "reg-" + CALLER


def test_client_secret_basic_is_accepted(server, client):
    client_id, secret = client
    code = get_code(server, client_id, VERIFIER)
    basic = "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": VERIFIER}
    assert server.token(form, basic).status == 200


def test_code_is_single_use_even_when_the_first_attempt_fails(server, conn, client):
    client_id, secret = client
    code = get_code(server, client_id, VERIFIER)
    assert exchange(server, client_id, secret, code, "w" * 43).body == {"error": "invalid_grant"}  # wrong verifier
    assert exchange(server, client_id, secret, code, VERIFIER).body == {"error": "invalid_grant"}  # now gone


@pytest.mark.parametrize("overrides", [
    {"redirect_uri": "https://grok.com/other/"},
    {"resource": "https://evil.example/mcp"},
    {"code_verifier": "short"},
])
def test_code_exchange_rejections(server, client, overrides):
    client_id, secret = client
    code = get_code(server, client_id, VERIFIER)
    assert exchange(server, client_id, secret, code, VERIFIER, **overrides).status == 400


def test_expired_code(server, client, clock):
    client_id, secret = client
    code = get_code(server, client_id, VERIFIER)
    clock.now = NOW + 61
    assert exchange(server, client_id, secret, code, VERIFIER).body == {"error": "invalid_grant"}


def test_code_bound_to_its_client(server, conn, client):
    client_id, secret = client
    code = get_code(server, client_id, VERIFIER)
    with transaction(conn):
        other_id, other_secret = oauth.issue_client_in_tx(conn, "reg-" + CALLER, NOW)
    assert exchange(server, other_id, other_secret, code, VERIFIER).body == {"error": "invalid_grant"}


@pytest.mark.parametrize("secret_override", ["oss_wrong", ""])
def test_bad_client_credentials(server, client, secret_override):
    client_id, _ = client
    code = get_code(server, client_id, VERIFIER)
    result = exchange(server, client_id, "x", code, VERIFIER, client_secret=secret_override or None)
    assert (result.status, result.body) == (401, {"error": "invalid_client"})
    assert result.www_authenticate.startswith("Basic")


def test_both_client_auth_methods_at_once_is_rejected(server, client):
    client_id, secret = client
    basic = "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    form = {"grant_type": "authorization_code", "code": "x", "client_id": client_id, "client_secret": secret}
    assert server.token(form, basic).status == 401


def test_unsupported_grant(server, client):
    client_id, secret = client
    form = {"grant_type": "password", "client_id": client_id, "client_secret": secret}
    assert server.token(form, None).body == {"error": "unsupported_grant_type"}


# ------------------------------------------------------------------ refresh

def test_refresh_rotates(server, client):
    client_id, secret = client
    first = exchange(server, client_id, secret, get_code(server, client_id, VERIFIER), VERIFIER).body
    form = {"grant_type": "refresh_token", "refresh_token": first["refresh_token"],
            "client_id": client_id, "client_secret": secret}
    second = server.token(form, None)
    assert second.status == 200 and second.body["refresh_token"] != first["refresh_token"]
    assert server.token(form, None).body == {"error": "invalid_grant", "error_description": MSG_CONNECTION_INVALID}  # spent
    assert server.registration_for_access_token("Bearer " + first["access_token"]) is not None  # still valid


def test_expired_refresh(server, client, clock):
    client_id, secret = client
    first = exchange(server, client_id, secret, get_code(server, client_id, VERIFIER), VERIFIER).body
    clock.now = NOW + 180 * 86_400
    form = {"grant_type": "refresh_token", "refresh_token": first["refresh_token"],
            "client_id": client_id, "client_secret": secret}
    assert server.token(form, None).body == {"error": "invalid_grant", "error_description": MSG_CONNECTION_INVALID}


# ------------------------------------------------------------------ bearer

def test_bearer_rules(server, conn, client, clock):
    client_id, secret = client
    tokens = exchange(server, client_id, secret, get_code(server, client_id, VERIFIER), VERIFIER).body
    assert server.registration_for_access_token(None) is None
    assert server.registration_for_access_token("Bearer mcp_nope") is None
    assert server.registration_for_access_token("Bearer " + tokens["refresh_token"]) is None  # wrong kind
    assert server.registration_for_access_token("bearer " + tokens["access_token"]) is None
    clock.now = NOW + 3_600
    assert server.registration_for_access_token("Bearer " + tokens["access_token"]) is None  # expired


def test_revoking_the_registration_kills_clients_and_tokens(server, conn, client):
    client_id, secret = client
    tokens = exchange(server, client_id, secret, get_code(server, client_id, VERIFIER), VERIFIER).body
    with transaction(conn):
        registry.revoke_in_tx(conn, "reg-" + CALLER, NOW)
    assert server.registration_for_access_token("Bearer " + tokens["access_token"]) is None
    assert conn.execute("SELECT count(*) FROM oauth_tokens").fetchone()[0] == 0


def test_deleting_a_client_kills_its_tokens(server, conn, client):
    client_id, secret = client
    tokens = exchange(server, client_id, secret, get_code(server, client_id, VERIFIER), VERIFIER).body
    with transaction(conn):
        assert oauth.delete_client_in_tx(conn, client_id)
    assert server.registration_for_access_token("Bearer " + tokens["access_token"]) is None


def test_expired_rows_are_cleaned_up(server, conn, client, clock):
    client_id, secret = client
    get_code(server, client_id, VERIFIER)  # an unused code
    exchange(server, client_id, secret, get_code(server, client_id, VERIFIER), VERIFIER)
    with transaction(conn):
        assert oauth.delete_expired_in_tx(conn, NOW + 365 * 86_400) == 3  # 1 code + access + refresh


# ------------------------------------------------------------------ PR 8d-2: what a dead connection is told

def test_the_description_names_both_causes_because_leaving_deletes_the_tokens():
    assert "membership ended" in MSG_CONNECTION_INVALID and "expired" in MSG_CONNECTION_INVALID
    assert "Reconnect" in MSG_CONNECTION_INVALID
    # RFC 6749 section 5.2 (and RFC 6750 section 3) allow an error_description only the printable ASCII
    # characters %x20-21, %x23-5B and %x5D-7E: no double quote, no backslash, and nothing outside ASCII
    # (so no line break or accented letter either). The bridge also puts it into a header unescaped.
    assert all(0x20 <= ord(c) <= 0x7E and c not in '"\\' for c in MSG_CONNECTION_INVALID), MSG_CONNECTION_INVALID


def test_an_unknown_refresh_token_gets_the_description(server, client):
    client_id, secret = client
    result = server.token({"grant_type": "refresh_token", "refresh_token": "rt_made_up",
                           "client_id": client_id, "client_secret": secret}, None)
    assert (result.status, result.body) == (400, {"error": "invalid_grant", "error_description": MSG_CONNECTION_INVALID})


def test_a_failed_code_exchange_keeps_a_bare_invalid_grant(server, client):
    """Only a refresh means "your connection is gone"; a bad code has no such meaning."""
    client_id, secret = client
    result = exchange(server, client_id, secret, "not-a-code", VERIFIER)
    assert result.body == {"error": "invalid_grant"}
