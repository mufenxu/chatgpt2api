from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import Lock
from typing import Callable

import jwt


FLOW_COOKIE = "chatgpt2api_my_flow"
SESSION_COOKIE = "chatgpt2api_my_session"
FLOW_TTL_SECONDS = 10 * 60
SESSION_TTL_SECONDS = 12 * 60 * 60
HTTP_TIMEOUT_SECONDS = 5
MAX_JSON_BYTES = 1024 * 1024
ALLOWED_RETURN_PATHS = ("/accounts", "/image", "/image-manager", "/logs", "/settings", "/debug")


class MyOidcError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class MyOidcSettings:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    session_secret: str

    @classmethod
    def from_environment(cls) -> "MyOidcSettings":
        settings = cls(
            issuer=os.getenv("MY_ISSUER", "").strip().rstrip("/"),
            client_id=os.getenv("MY_CLIENT_ID", "").strip(),
            client_secret=os.getenv("MY_CLIENT_SECRET", "").strip(),
            redirect_uri=os.getenv("MY_REDIRECT_URI", "").strip(),
            session_secret=os.getenv("SESSION_SECRET", "").strip(),
        )
        missing = [
            name
            for name, value in (
                ("MY_ISSUER", settings.issuer),
                ("MY_CLIENT_ID", settings.client_id),
                ("MY_CLIENT_SECRET", settings.client_secret),
                ("MY_REDIRECT_URI", settings.redirect_uri),
                ("SESSION_SECRET", settings.session_secret),
            )
            if not value
        ]
        if missing:
            raise MyOidcError(
                "oidc_not_configured",
                f"MY OIDC 配置不完整，缺少环境变量：{', '.join(missing)}",
                status_code=503,
            )
        if len(settings.session_secret) < 32:
            raise MyOidcError(
                "oidc_configuration_invalid",
                "SESSION_SECRET 至少需要 32 个字符",
                status_code=503,
            )
        issuer = urllib.parse.urlsplit(settings.issuer)
        if issuer.scheme != "https" or not issuer.netloc or issuer.query or issuer.fragment:
            raise MyOidcError("oidc_configuration_invalid", "MY_ISSUER 必须是 HTTPS Issuer URL", status_code=503)
        redirect = urllib.parse.urlsplit(settings.redirect_uri)
        loopback_http = redirect.scheme == "http" and redirect.hostname in {"127.0.0.1", "localhost", "::1"}
        if (redirect.scheme != "https" and not loopback_http) or not redirect.netloc or redirect.fragment:
            raise MyOidcError(
                "oidc_configuration_invalid",
                "MY_REDIRECT_URI 必须使用 HTTPS，本机开发可使用 loopback HTTP",
                status_code=503,
            )
        return settings

    @property
    def secure_cookie(self) -> bool:
        return urllib.parse.urlsplit(self.redirect_uri).scheme.lower() == "https"


@dataclass(frozen=True)
class HttpResult:
    status_code: int
    content_type: str
    body: bytes


@dataclass(frozen=True)
class OidcFlow:
    state: str
    nonce: str
    code_verifier: str
    return_to: str
    issuer: str
    client_id: str
    redirect_uri: str
    token_endpoint: str
    jwks_uri: str
    expires_at: float


@dataclass(frozen=True)
class LocalSession:
    claims: dict[str, str]
    expires_at: float


Transport = Callable[[str, str, bytes | None, dict[str, str]], HttpResult]


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _controlled_return_to(value: str) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return "/accounts"
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return "/accounts"
    if parsed.path != "/" and not any(
        parsed.path == prefix or parsed.path.startswith(f"{prefix}/") for prefix in ALLOWED_RETURN_PATHS
    ):
        return "/accounts"
    return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))


def map_my_role(role: object) -> str:
    normalized = str(role or "").strip().lower()
    if normalized in {"operator", "super_admin"}:
        return "admin"
    raise MyOidcError("role_not_allowed", "当前 MY 账号没有本项目管理员权限", status_code=403)


class MyOidcService:
    def __init__(self, transport: Transport | None = None):
        self._transport = transport or self._default_transport
        self._lock = Lock()
        self._flows: dict[str, OidcFlow] = {}
        self._sessions: dict[str, LocalSession] = {}
        self._jwks_cache: dict[str, dict[str, object]] = {}
        self._request_identity: ContextVar[dict[str, object] | None] = ContextVar(
            "my_oidc_request_identity",
            default=None,
        )

    @staticmethod
    def _default_transport(url: str, method: str, body: bytes | None, headers: dict[str, str]) -> HttpResult:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                payload = response.read(MAX_JSON_BYTES + 1)
                if len(payload) > MAX_JSON_BYTES:
                    raise MyOidcError("oidc_response_too_large", "MY OIDC 响应超过大小限制", status_code=502)
                return HttpResult(
                    status_code=int(response.status),
                    content_type=str(response.headers.get("Content-Type") or ""),
                    body=payload,
                )
        except urllib.error.HTTPError as exc:
            return HttpResult(
                status_code=int(exc.code),
                content_type=str(exc.headers.get("Content-Type") or "") if exc.headers else "",
                body=b"",
            )
        except (OSError, urllib.error.URLError) as exc:
            raise MyOidcError("oidc_unavailable", "无法连接 MY OIDC 服务", status_code=503) from exc

    def _request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        form: dict[str, str] | None = None,
        basic_auth: tuple[str, str] | None = None,
        error_code: str,
    ) -> dict[str, object]:
        headers = {"Accept": "application/json", "User-Agent": "chatgpt2api-oidc/1.0"}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if basic_auth is not None:
            credentials = base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {credentials}"
        result = self._transport(url, method, body, headers)
        if result.status_code != 200 or "application/json" not in result.content_type.lower():
            raise MyOidcError(error_code, "MY OIDC 返回了无效的 HTTP 响应", status_code=502)
        try:
            payload = json.loads(result.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MyOidcError(error_code, "MY OIDC 返回的不是有效 JSON", status_code=502) from exc
        if not isinstance(payload, dict):
            raise MyOidcError(error_code, "MY OIDC 返回的 JSON 结构无效", status_code=502)
        return payload

    def _settings(self) -> MyOidcSettings:
        return MyOidcSettings.from_environment()

    def _lookup_key(self, kind: str, value: str, settings: MyOidcSettings | None = None) -> str:
        if not value:
            return ""
        active_settings = settings or self._settings()
        return hmac.new(
            active_settings.session_secret.encode("utf-8"),
            f"{kind}:{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _cleanup_locked(self, now: float) -> None:
        self._flows = {key: item for key, item in self._flows.items() if item.expires_at > now}
        self._sessions = {key: item for key, item in self._sessions.items() if item.expires_at > now}

    def discovery(self, settings: MyOidcSettings) -> dict[str, str]:
        discovery_url = f"{settings.issuer}/.well-known/openid-configuration"
        payload = self._request_json(discovery_url, error_code="discovery_invalid")
        if payload.get("issuer") != settings.issuer:
            raise MyOidcError("discovery_issuer_mismatch", "MY OIDC Discovery issuer 与配置不匹配", status_code=502)
        required = {}
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = str(payload.get(name) or "").strip()
            if not value:
                raise MyOidcError("discovery_invalid", f"MY OIDC Discovery 缺少 {name}", status_code=502)
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
                raise MyOidcError("discovery_invalid", f"MY OIDC Discovery 的 {name} 不是安全端点", status_code=502)
            required[name] = value
        return required

    def start(self, return_to: str = "") -> tuple[str, str]:
        settings = self._settings()
        endpoints = self.discovery(settings)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        if not 43 <= len(code_verifier) <= 128:
            raise MyOidcError("pkce_generation_failed", "无法生成有效的 PKCE verifier", status_code=500)
        flow_id = secrets.token_urlsafe(32)
        code_challenge = _base64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
        flow = OidcFlow(
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            return_to=_controlled_return_to(return_to),
            issuer=settings.issuer,
            client_id=settings.client_id,
            redirect_uri=settings.redirect_uri,
            token_endpoint=endpoints["token_endpoint"],
            jwks_uri=endpoints["jwks_uri"],
            expires_at=time.time() + FLOW_TTL_SECONDS,
        )
        with self._lock:
            self._cleanup_locked(time.time())
            self._flows[self._lookup_key("flow", flow_id, settings)] = flow
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": settings.client_id,
                "redirect_uri": settings.redirect_uri,
                "scope": "openid profile roles",
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoints['authorization_endpoint']}?{query}", flow_id

    def discard_flow(self, flow_id: str) -> None:
        if not flow_id:
            return
        try:
            key = self._lookup_key("flow", flow_id)
        except MyOidcError:
            return
        with self._lock:
            self._flows.pop(key, None)

    def _consume_flow(self, flow_id: str, state: str) -> OidcFlow:
        try:
            key = self._lookup_key("flow", flow_id)
        except MyOidcError as exc:
            raise MyOidcError("state_mismatch", "登录状态无效或已过期，请重新登录") from exc
        with self._lock:
            self._cleanup_locked(time.time())
            flow = self._flows.pop(key, None)
        if flow is None or not state or not secrets.compare_digest(flow.state, state):
            raise MyOidcError("state_mismatch", "登录状态无效或已过期，请重新登录")
        return flow

    def _get_jwks(self, uri: str, *, force_refresh: bool = False) -> dict[str, object]:
        if not force_refresh:
            with self._lock:
                cached = self._jwks_cache.get(uri)
            if cached is not None:
                return cached
        payload = self._request_json(uri, error_code="jwks_invalid")
        if not isinstance(payload.get("keys"), list):
            raise MyOidcError("jwks_invalid", "MY OIDC JWKS 结构无效", status_code=502)
        with self._lock:
            self._jwks_cache[uri] = payload
        return payload

    @staticmethod
    def _matching_jwk(jwks: dict[str, object], kid: str) -> dict[str, object] | None:
        matches = [
            item
            for item in jwks.get("keys", [])
            if isinstance(item, dict) and secrets.compare_digest(str(item.get("kid") or ""), kid)
        ]
        if len(matches) != 1:
            return None
        key = matches[0]
        if key.get("kty") != "OKP" or key.get("crv") != "Ed25519":
            return None
        if key.get("alg") not in {None, "EdDSA"} or key.get("use") not in {None, "sig"}:
            return None
        return key

    def _validate_id_token(self, token: str, flow: OidcFlow) -> dict[str, str]:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise MyOidcError("id_token_invalid", "MY ID Token 格式无效") from exc
        kid = str(header.get("kid") or "")
        if header.get("alg") != "EdDSA" or not kid:
            raise MyOidcError("id_token_invalid", "MY ID Token 算法或 kid 无效")
        jwk = self._matching_jwk(self._get_jwks(flow.jwks_uri), kid)
        if jwk is None:
            jwk = self._matching_jwk(self._get_jwks(flow.jwks_uri, force_refresh=True), kid)
        if jwk is None:
            raise MyOidcError("id_token_unknown_kid", "MY ID Token 使用了未知的 kid")
        try:
            key = jwt.PyJWK.from_dict(jwk, algorithm="EdDSA").key
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["EdDSA"],
                audience=flow.client_id,
                issuer=flow.issuer,
                leeway=30,
                options={"require": ["sub", "iss", "aud", "exp", "iat", "nonce", "token_use"]},
            )
        except (jwt.PyJWTError, ValueError) as exc:
            raise MyOidcError("id_token_invalid", "MY ID Token 验签或声明校验失败") from exc
        nonce = str(claims.get("nonce") or "")
        if not nonce or not secrets.compare_digest(nonce, flow.nonce):
            raise MyOidcError("nonce_mismatch", "MY ID Token nonce 校验失败")
        if claims.get("token_use") != "id":
            raise MyOidcError("id_token_invalid", "MY ID Token token_use 无效")
        preferred_username = str(claims.get("preferred_username") or "").strip()
        subject = str(claims.get("sub") or "").strip()
        claimed_role = str(claims.get("role") or "").strip().lower()
        if not subject or not preferred_username or not claimed_role:
            raise MyOidcError("id_token_invalid", "MY ID Token 缺少用户声明")
        map_my_role(claimed_role)
        return {"sub": subject, "preferred_username": preferred_username, "role": claimed_role}

    def finish(self, flow_id: str, state: str, code: str, previous_session_id: str = "") -> tuple[str, str]:
        flow = self._consume_flow(flow_id, state)
        if not code:
            raise MyOidcError("missing_code", "MY 登录回调缺少授权码")
        settings = self._settings()
        if settings.issuer != flow.issuer or settings.client_id != flow.client_id:
            raise MyOidcError("oidc_configuration_changed", "MY OIDC 配置在登录过程中发生变化，请重新登录", status_code=503)
        token_payload = self._request_json(
            flow.token_endpoint,
            method="POST",
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": flow.redirect_uri,
                "code_verifier": flow.code_verifier,
            },
            basic_auth=(settings.client_id, settings.client_secret),
            error_code="token_exchange_failed",
        )
        required_fields = ("access_token", "id_token", "token_type", "expires_in", "scope")
        if any(not token_payload.get(name) for name in required_fields):
            raise MyOidcError("token_response_invalid", "MY Token 响应缺少必需字段", status_code=502)
        if str(token_payload.get("token_type") or "").lower() != "bearer":
            raise MyOidcError("token_response_invalid", "MY Token 类型无效", status_code=502)
        scopes = set(str(token_payload.get("scope") or "").split())
        if not {"openid", "profile", "roles"}.issubset(scopes):
            raise MyOidcError("token_response_invalid", "MY Token scope 不完整", status_code=502)
        claims = self._validate_id_token(str(token_payload["id_token"]), flow)
        new_session_id = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._cleanup_locked(now)
            if previous_session_id:
                self._sessions.pop(self._lookup_key("session", previous_session_id, settings), None)
            self._sessions[self._lookup_key("session", new_session_id, settings)] = LocalSession(
                claims=claims,
                expires_at=now + SESSION_TTL_SECONDS,
            )
        return new_session_id, flow.return_to

    def get_identity(self, session_id: str) -> dict[str, object] | None:
        if not session_id:
            return None
        try:
            key = self._lookup_key("session", session_id)
        except MyOidcError:
            return None
        with self._lock:
            self._cleanup_locked(time.time())
            session = self._sessions.get(key)
        if session is None:
            return None
        try:
            local_role = map_my_role(session.claims.get("role"))
        except MyOidcError:
            return None
        return {
            "id": session.claims["sub"],
            "name": session.claims["preferred_username"],
            "role": local_role,
        }

    def logout(self, session_id: str) -> None:
        if not session_id:
            return
        try:
            key = self._lookup_key("session", session_id)
        except MyOidcError:
            return
        with self._lock:
            self._sessions.pop(key, None)

    def bind_request_identity(self, session_id: str) -> Token[dict[str, object] | None]:
        return self._request_identity.set(self.get_identity(session_id))

    def reset_request_identity(self, token: Token[dict[str, object] | None]) -> None:
        self._request_identity.reset(token)

    def current_identity(self) -> dict[str, object] | None:
        return self._request_identity.get()


my_oidc_service = MyOidcService()
