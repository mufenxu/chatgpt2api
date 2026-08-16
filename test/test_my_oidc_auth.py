from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import unittest
import urllib.parse
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from api.my_auth import create_router, install_my_auth_middleware
import api.support as support_module
from services.my_oidc_service import (
    FLOW_COOKIE,
    SESSION_COOKIE,
    HttpResult,
    MyOidcError,
    MyOidcService,
    map_my_role,
)


ISSUER = "https://issuer.example"
CLIENT_ID = "client-id"
CLIENT_SECRET = "test-client-secret"
REDIRECT_URI = "http://127.0.0.1/auth/my/callback"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/token"
JWKS_URI = f"{ISSUER}/jwks"


def _json_result(payload: dict[str, object], status_code: int = 200, content_type: str = "application/json") -> HttpResult:
    return HttpResult(status_code, content_type, json.dumps(payload).encode("utf-8"))


def _jwk(private_key: Ed25519PrivateKey, kid: str) -> dict[str, str]:
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "use": "sig",
        "alg": "EdDSA",
        "kid": kid,
        "x": base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii"),
    }


class FakeOidcTransport:
    def __init__(self) -> None:
        self.signing_key = Ed25519PrivateKey.generate()
        self.kid = "current-key"
        self.discovery_result = _json_result(
            {
                "issuer": ISSUER,
                "authorization_endpoint": AUTHORIZATION_ENDPOINT,
                "token_endpoint": TOKEN_ENDPOINT,
                "jwks_uri": JWKS_URI,
            }
        )
        self.jwks_results = [_json_result({"keys": [_jwk(self.signing_key, self.kid)]})]
        self.token_calls: list[dict[str, object]] = []
        self.jwks_calls = 0
        self.nonce = ""
        self.role = "operator"
        self.claim_overrides: dict[str, object] = {}
        self.token_signing_key: Ed25519PrivateKey | None = None
        self.token_kid: str | None = None

    def _id_token(self) -> str:
        now = int(time.time())
        claims: dict[str, object] = {
            "sub": "user-123",
            "preferred_username": "operator@example.com",
            "role": self.role,
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": self.nonce,
            "token_use": "id",
        }
        claims.update(self.claim_overrides)
        return jwt.encode(
            claims,
            self.token_signing_key or self.signing_key,
            algorithm="EdDSA",
            headers={"kid": self.token_kid or self.kid},
        )

    def __call__(self, url: str, method: str, body: bytes | None, headers: dict[str, str]) -> HttpResult:
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return self.discovery_result
        if url == JWKS_URI:
            result = self.jwks_results[min(self.jwks_calls, len(self.jwks_results) - 1)]
            self.jwks_calls += 1
            return result
        if url == TOKEN_ENDPOINT:
            form = urllib.parse.parse_qs((body or b"").decode("ascii"), keep_blank_values=True)
            self.token_calls.append({"method": method, "form": form, "headers": dict(headers)})
            return _json_result(
                {
                    "access_token": "access-token-value",
                    "id_token": self._id_token(),
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "scope": "openid profile roles",
                }
            )
        raise AssertionError(f"Unexpected URL: {url}")


class MyOidcTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(
            os.environ,
            {
                "MY_ISSUER": ISSUER,
                "MY_CLIENT_ID": CLIENT_ID,
                "MY_CLIENT_SECRET": CLIENT_SECRET,
                "MY_REDIRECT_URI": REDIRECT_URI,
                "SESSION_SECRET": "test-session-secret-with-enough-entropy",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.transport = FakeOidcTransport()
        self.service = MyOidcService(self.transport)

    @staticmethod
    def _query(authorize_url: str) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(authorize_url).query)

    def _start(self, return_to: str = "/accounts") -> tuple[str, str, dict[str, list[str]]]:
        authorize_url, flow_id = self.service.start(return_to)
        query = self._query(authorize_url)
        self.transport.nonce = query["nonce"][0]
        return authorize_url, flow_id, query

    def test_discovery_fails_closed_for_non_json_and_issuer_mismatch(self) -> None:
        self.transport.discovery_result = HttpResult(404, "text/html", b"not found")
        with self.assertRaisesRegex(MyOidcError, "无效的 HTTP 响应") as non_json:
            self.service.start()
        self.assertEqual(non_json.exception.code, "discovery_invalid")

        self.transport.discovery_result = _json_result(
            {
                "issuer": "https://wrong.example",
                "authorization_endpoint": AUTHORIZATION_ENDPOINT,
                "token_endpoint": TOKEN_ENDPOINT,
                "jwks_uri": JWKS_URI,
            }
        )
        with self.assertRaises(MyOidcError) as mismatch:
            self.service.start()
        self.assertEqual(mismatch.exception.code, "discovery_issuer_mismatch")

    def test_authorization_parameters_pkce_and_controlled_return_to(self) -> None:
        authorize_url, flow_id, query = self._start("https://attacker.example/path")

        self.assertEqual(urllib.parse.urlsplit(authorize_url)._replace(query="").geturl(), AUTHORIZATION_ENDPOINT)
        self.assertEqual(
            set(query),
            {
                "response_type",
                "client_id",
                "redirect_uri",
                "scope",
                "state",
                "nonce",
                "code_challenge",
                "code_challenge_method",
            },
        )
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(query["scope"], ["openid profile roles"])
        self.assertEqual(query["code_challenge_method"], ["S256"])

        session_id, return_to = self.service.finish(flow_id, query["state"][0], "one-time-code")
        self.assertTrue(session_id)
        self.assertEqual(return_to, "/accounts")
        token_call = self.transport.token_calls[0]
        form = token_call["form"]
        verifier = form["code_verifier"][0]  # type: ignore[index]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        self.assertTrue(43 <= len(verifier) <= 128)
        self.assertEqual(challenge, query["code_challenge"][0])
        self.assertEqual(form["grant_type"], ["authorization_code"])  # type: ignore[index]
        self.assertEqual(form["redirect_uri"], [REDIRECT_URI])  # type: ignore[index]
        expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        self.assertEqual(token_call["headers"]["Authorization"], f"Basic {expected_basic}")  # type: ignore[index]

    def test_state_and_nonce_failures_are_rejected(self) -> None:
        _, flow_id, query = self._start()
        with self.assertRaises(MyOidcError) as state_error:
            self.service.finish(flow_id, "wrong-state", "one-time-code")
        self.assertEqual(state_error.exception.code, "state_mismatch")
        self.assertEqual(self.transport.token_calls, [])

        _, flow_id, query = self._start()
        self.transport.claim_overrides["nonce"] = "wrong-nonce"
        with self.assertRaises(MyOidcError) as nonce_error:
            self.service.finish(flow_id, query["state"][0], "one-time-code")
        self.assertEqual(nonce_error.exception.code, "nonce_mismatch")

    def test_id_token_signature_is_verified(self) -> None:
        _, flow_id, query = self._start()
        self.transport.token_signing_key = Ed25519PrivateKey.generate()
        with self.assertRaises(MyOidcError) as error:
            self.service.finish(flow_id, query["state"][0], "one-time-code")
        self.assertEqual(error.exception.code, "id_token_invalid")

    def test_unknown_kid_refreshes_jwks_once(self) -> None:
        old_key = Ed25519PrivateKey.generate()
        self.transport.jwks_results = [
            _json_result({"keys": [_jwk(old_key, "old-key")]}),
            _json_result({"keys": [_jwk(self.transport.signing_key, self.transport.kid)]}),
        ]
        _, flow_id, query = self._start()

        session_id, _ = self.service.finish(flow_id, query["state"][0], "one-time-code")

        self.assertTrue(session_id)
        self.assertEqual(self.transport.jwks_calls, 2)

    def test_authorization_code_flow_cannot_be_replayed(self) -> None:
        _, flow_id, query = self._start()
        self.service.finish(flow_id, query["state"][0], "one-time-code")

        with self.assertRaises(MyOidcError) as replay:
            self.service.finish(flow_id, query["state"][0], "one-time-code")
        self.assertEqual(replay.exception.code, "state_mismatch")
        self.assertEqual(len(self.transport.token_calls), 1)

    def test_role_mapping_allows_admin_roles_and_denies_other_roles(self) -> None:
        self.assertEqual(map_my_role("operator"), "admin")
        self.assertEqual(map_my_role("super_admin"), "admin")
        for role in ("viewer", "owner", ""):
            with self.subTest(role=role), self.assertRaises(MyOidcError):
                map_my_role(role)

    def test_callback_error_is_handled_before_state_and_consumes_flow(self) -> None:
        _, flow_id, query = self._start()
        app = FastAPI()
        install_my_auth_middleware(app, self.service)
        app.include_router(create_router(self.service))
        client = TestClient(app)
        client.cookies.set(FLOW_COOKIE, flow_id)

        response = client.get(
            "/auth/my/callback",
            params={"error": "access_denied", "error_description": "cancelled", "state": "wrong-state"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error"], "access_denied")
        with self.assertRaises(MyOidcError):
            self.service.finish(flow_id, query["state"][0], "one-time-code")

    def test_local_session_logout_and_health(self) -> None:
        app = FastAPI()
        install_my_auth_middleware(app, self.service)
        app.include_router(create_router(self.service))

        @app.get("/admin-only")
        async def admin_only(authorization: str | None = Header(default=None)):
            return support_module.require_admin(authorization)

        client = TestClient(app)

        start = client.get("/auth/my/start?returnTo=%2Fsettings", follow_redirects=False)
        self.assertEqual(start.status_code, 302)
        query = self._query(start.headers["location"])
        self.transport.nonce = query["nonce"][0]
        callback = client.get(
            "/auth/my/callback",
            params={"code": "one-time-code", "state": query["state"][0]},
            follow_redirects=False,
        )
        self.assertEqual(callback.status_code, 303)
        self.assertEqual(callback.headers["location"], "/settings")
        session_id = client.cookies.get(SESSION_COOKIE)
        self.assertIsNotNone(session_id)
        self.assertEqual(self.service.get_identity(str(session_id))["role"], "admin")  # type: ignore[index]
        with mock.patch.object(support_module, "my_oidc_service", self.service):
            self.assertEqual(client.get("/admin-only").json()["role"], "admin")
        self.assertNotIn("one-time-code", callback.text)

        logout = client.post("/auth/logout")
        self.assertEqual(logout.status_code, 200)
        self.assertIsNone(self.service.get_identity(str(session_id)))
        with mock.patch.object(support_module, "my_oidc_service", self.service):
            self.assertEqual(client.get("/admin-only").status_code, 401)
        self.assertEqual(client.get("/health").json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
