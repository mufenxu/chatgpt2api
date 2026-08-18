from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import APIRouter, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response

from services.my_oidc_service import (
    FLOW_COOKIE,
    SESSION_COOKIE,
    MyOidcError,
    MyOidcService,
    my_oidc_service,
)


def _error_response(exc: MyOidcError) -> JSONResponse:
    response = JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"error": exc.code, "message": exc.message}},
    )
    if exc.retry_after:
        response.headers["Retry-After"] = exc.retry_after
    return response


def _delete_flow_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(FLOW_COOKIE, path="/", httponly=True, secure=secure, samesite="lax")


def install_my_auth_middleware(app: FastAPI, service: MyOidcService = my_oidc_service) -> None:
    @app.middleware("http")
    async def my_auth_context(request: Request, call_next):
        if request.url.path == "/auth/my/callback":
            # Uvicorn builds its access-log request target from this scope after the response.
            raw_query = bytes(request.scope.get("query_string") or b"")
            parsed = parse_qs(raw_query.decode("utf-8", errors="replace"), keep_blank_values=True)
            request.scope["my_oidc_callback"] = {
                name: str(values[0]) if values else ""
                for name in ("code", "state", "error", "error_description")
                if (values := parsed.get(name)) is not None
            }
            request.scope["query_string"] = b""
        token = service.bind_request_identity(request.cookies.get(SESSION_COOKIE, ""))
        try:
            return await call_next(request)
        finally:
            service.reset_request_identity(token)


def create_router(service: MyOidcService = my_oidc_service) -> APIRouter:
    router = APIRouter()

    @router.get("/auth/my/start")
    async def start_my_login(returnTo: str = ""):
        try:
            authorize_url, flow_id = await run_in_threadpool(service.start, returnTo)
            secure = service._settings().secure_cookie
        except MyOidcError as exc:
            return _error_response(exc)
        response = RedirectResponse(authorize_url, status_code=302)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.set_cookie(
            FLOW_COOKIE,
            flow_id,
            max_age=10 * 60,
            path="/",
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        return response

    @router.get("/auth/my/callback")
    async def finish_my_login(request: Request):
        params = request.scope.get("my_oidc_callback")
        if not isinstance(params, dict):
            params = {name: request.query_params.get(name, "") for name in ("code", "state", "error", "error_description")}
        flow_id = request.cookies.get(FLOW_COOKIE, "")
        try:
            secure = service._settings().secure_cookie
        except MyOidcError:
            secure = request.url.scheme == "https"
        error = str(params.get("error") or "").strip()
        if error:
            await run_in_threadpool(service.discard_flow, flow_id)
            response = JSONResponse(
                status_code=400,
                content={"detail": {"error": error[:80], "message": "MY 登录未完成，请重新登录"}},
            )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            _delete_flow_cookie(response, secure=secure)
            return response
        try:
            session_id, return_to = await run_in_threadpool(
                service.finish,
                flow_id,
                str(params.get("state") or ""),
                str(params.get("code") or ""),
                request.cookies.get(SESSION_COOKIE, ""),
            )
        except MyOidcError as exc:
            response = _error_response(exc)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            _delete_flow_cookie(response, secure=secure)
            return response
        response = RedirectResponse(return_to, status_code=303)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        _delete_flow_cookie(response, secure=secure)
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=12 * 60 * 60,
            path="/",
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        return response

    @router.post("/auth/logout")
    async def logout(request: Request):
        await run_in_threadpool(service.logout, request.cookies.get(SESSION_COOKIE, ""))
        response = JSONResponse({"ok": True})
        try:
            secure = service._settings().secure_cookie
        except MyOidcError:
            secure = request.url.scheme == "https"
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=secure, samesite="lax")
        return response

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    return router
