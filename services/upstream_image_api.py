from __future__ import annotations

import base64
import re
import threading
import time
from typing import Any

import requests

from services.config import config

_MAX_EDIT_IMAGES = 10


def _current_settings() -> dict[str, Any]:
    return config.get_image_upstream_settings()


def _clean_models(models: object) -> list[str]:
    result: list[str] = []
    if isinstance(models, list):
        for item in models:
            model = str(item or "").strip()
            if model and model not in result:
                result.append(model)
    return result


def should_use_generic_upstream(model: str) -> bool:
    """判断模型是否应路由到通用 OpenAI 兼容图片上游。

    image_upstream.enabled 且 models 为空时，所有图片模型都走通用上游（全局切换）；
    指定 models 时仅列出的模型走通用上游，其余模型仍走 ChatGPT 账号链路。
    """
    settings = _current_settings()
    if not settings.get("enabled"):
        return False
    if not str(settings.get("base_url") or "").strip() or not str(settings.get("api_key") or "").strip():
        # 配置不完整时回退到 ChatGPT 账号链路，避免误配置导致生图全部失败
        return False
    models = _clean_models(settings.get("models"))
    return not models or str(model or "").strip() in models


_semaphore: threading.BoundedSemaphore | None = None
_semaphore_capacity = 0
_semaphore_lock = threading.Lock()


def _acquire_semaphore() -> threading.BoundedSemaphore:
    global _semaphore, _semaphore_capacity
    concurrency = max(1, int(_current_settings().get("concurrency") or 4))
    with _semaphore_lock:
        if _semaphore is None or _semaphore_capacity != concurrency:
            _semaphore = threading.BoundedSemaphore(concurrency)
            _semaphore_capacity = concurrency
        return _semaphore


class _UpstreamRetryableError(Exception):
    """网络错误、5xx、429、504 等可重试的上游错误。"""


def _sanitize_error_message(message: str) -> str:
    text = str(message or "").strip()
    return re.sub(r"https?://\S+", "[upstream]", text)


def _extract_task_id(response: requests.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("task_id", "taskId", "id", "request_id", "requestId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_error_message(response_or_payload: Any) -> str:
    if isinstance(response_or_payload, requests.Response):
        try:
            payload = response_or_payload.json()
        except Exception:
            return ""
    else:
        payload = response_or_payload
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or payload.get("message")
            return str(message or "").strip()
        if isinstance(error, str):
            return error.strip()
        message = payload.get("message")
        if isinstance(message, str):
            return message.strip()
    return ""


def _extract_image_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        candidates: Any = payload
    elif isinstance(payload, dict):
        candidates = payload.get("data") or payload.get("images") or []
    else:
        return []
    items: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if item.get("b64_json") or item.get("b64") or item.get("url") or item.get("image_url"):
            items.append(item)
    return items


def _extract_task_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates: list[Any] = []
    for key in ("tasks", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    if isinstance(payload.get("data"), list):
        candidates.extend(payload["data"])
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").strip().lower()
        if status in {"failed", "error", "cancelled", "canceled"}:
            message = entry.get("error") or entry.get("message")
            return str(message or "upstream image task failed").strip()
    return ""


def _extract_task_items(payload: Any) -> list[dict[str, Any]]:
    direct = _extract_image_items(payload)
    if direct:
        return direct
    if isinstance(payload, dict):
        for key in ("tasks", "items", "results"):
            value = payload.get(key)
            if not isinstance(value, list):
                continue
            merged: list[dict[str, Any]] = []
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                status = str(entry.get("status") or "").strip().lower()
                if status in {"success", "succeeded", "completed", "done", ""}:
                    merged.extend(_extract_image_items(entry))
            if merged:
                return merged
    return []


def _download_image_bytes(session: requests.Session, url: str, headers: dict[str, str]) -> bytes:
    try:
        response = session.get(url, timeout=30)
        if response.status_code in (401, 403):
            response = session.get(url, headers=headers, timeout=30)
        if response.status_code == 200 and response.content:
            return response.content
    except Exception:
        return b""
    return b""


def _build_result_items(
    items: list[dict[str, Any]],
    session: requests.Session,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items:
        b64 = item.get("b64_json") or item.get("b64")
        if not b64:
            url = item.get("url") or item.get("image_url")
            if not url:
                continue
            data = _download_image_bytes(session, str(url), headers)
            if not data:
                continue
            b64 = base64.b64encode(data).decode("ascii")
        result.append({
            "b64_json": b64,
            "revised_prompt": str(item.get("revised_prompt") or "").strip(),
        })
    return result


def _request_body(request: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": request.model,
        "prompt": request.prompt,
        "n": 1,
        "response_format": "url",
    }
    if request.size:
        body["size"] = request.size
    if request.quality and request.quality != "auto":
        body["quality"] = request.quality
    return body


def _poll_upstream_task(
    session: requests.Session,
    request: Any,
    index: int,
    total: int,
    settings: dict[str, Any],
    task_id: str,
) -> list[Any]:
    from services.protocol.conversation import ImageGenerationError, ImageOutput, format_image_result

    base_url = str(settings.get("base_url") or "").rstrip("/")
    api_key = str(settings.get("api_key") or "")
    task_query_path = str(settings.get("task_query_path") or "").strip()
    if not task_query_path.startswith("/"):
        task_query_path = "/" + task_query_path
    ids_param = str(settings.get("task_query_ids_param") or "ids").strip() or "ids"
    poll_interval = max(0.5, float(settings.get("poll_interval_secs") or 3.0))
    timeout_secs = max(1, int(settings.get("timeout_secs") or 120))
    verify_ssl = bool(settings.get("verify_ssl", True))
    headers = {"Authorization": f"Bearer {api_key}"}
    if request.progress_callback:
        request.progress_callback("waiting_upstream_task")
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        try:
            response = session.get(
                f"{base_url}{task_query_path}",
                params={ids_param: task_id},
                headers=headers,
                timeout=min(30, max(1, timeout_secs)),
                verify=verify_ssl,
            )
        except requests.RequestException:
            # 轮询期间的网络错误视为瞬时抖动，继续轮询直到超时，避免重复提交生成任务
            time.sleep(poll_interval)
            continue
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                time.sleep(poll_interval)
                continue
            error_text = _extract_task_error(payload)
            if error_text:
                raise ImageGenerationError(
                    _sanitize_error_message(error_text),
                    status_code=400,
                    error_type="invalid_request_error",
                    code="content_policy_violation",
                )
            items = _extract_task_items(payload)
            if items:
                downloaded = _build_result_items(items, session, headers)
                if downloaded:
                    result = format_image_result(
                        downloaded,
                        request.prompt,
                        request.response_format,
                        request.base_url,
                    )
                    return [ImageOutput(kind="result", model=request.model, index=index, total=total, data=result["data"])]
                raise ImageGenerationError(
                    "generic image upstream returned empty image data",
                    status_code=502,
                    error_type="server_error",
                    code="no_image_generated",
                )
        time.sleep(poll_interval)
    # 已拿到 task_id，超时后直接失败而不是重发 POST，避免上游重复生成
    raise ImageGenerationError(
        "generic image upstream task timed out",
        status_code=504,
        error_type="server_error",
        code="upstream_timeout",
    )


def _call_upstream_once(
    session: requests.Session,
    request: Any,
    index: int,
    total: int,
    settings: dict[str, Any],
) -> list[Any]:
    from services.protocol.conversation import ImageGenerationError, ImageOutput, format_image_result

    base_url = str(settings.get("base_url") or "").rstrip("/")
    api_key = str(settings.get("api_key") or "")
    timeout_secs = max(1, int(settings.get("timeout_secs") or 120))
    verify_ssl = bool(settings.get("verify_ssl", True))
    headers = {"Authorization": f"Bearer {api_key}"}
    if request.progress_callback:
        request.progress_callback("calling_upstream")
    try:
        if request.images:
            files = []
            for position, image_b64 in enumerate(request.images[:_MAX_EDIT_IMAGES], start=1):
                files.append(("image", (f"image_{position}.png", base64.b64decode(image_b64), "image/png")))
            response = session.post(
                f"{base_url}/images/edits",
                data=_request_body(request),
                files=files,
                headers=headers,
                timeout=timeout_secs,
                verify=verify_ssl,
            )
        else:
            response = session.post(
                f"{base_url}/images/generations",
                json=_request_body(request),
                headers=headers,
                timeout=timeout_secs,
                verify=verify_ssl,
            )
    except requests.RequestException as exc:
        raise _UpstreamRetryableError(f"generic image upstream request failed: {exc}") from exc

    if response.status_code == 504:
        task_id = _extract_task_id(response)
        if task_id and str(settings.get("task_query_path") or "").strip():
            return _poll_upstream_task(session, request, index, total, settings, task_id)
        raise _UpstreamRetryableError("generic image upstream timed out (HTTP 504)")

    if response.status_code == 429:
        raise _UpstreamRetryableError("generic image upstream rate limited (HTTP 429)")

    if response.status_code >= 500:
        raise _UpstreamRetryableError(f"generic image upstream error (HTTP {response.status_code})")

    if response.status_code >= 400:
        message = _extract_error_message(response)
        raise ImageGenerationError(
            _sanitize_error_message(message) or "generic image upstream rejected the request",
            status_code=400,
            error_type="invalid_request_error",
            code="upstream_rejected",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise _UpstreamRetryableError("generic image upstream returned an invalid response") from exc

    items = _extract_image_items(payload)
    if items:
        downloaded = _build_result_items(items, session, headers)
        if downloaded:
            result = format_image_result(
                downloaded,
                request.prompt,
                request.response_format,
                request.base_url,
            )
            return [ImageOutput(kind="result", model=request.model, index=index, total=total, data=result["data"])]
        raise ImageGenerationError(
            "generic image upstream returned empty image data",
            status_code=502,
            error_type="server_error",
            code="no_image_generated",
        )

    message = _extract_error_message(payload)
    if message:
        if request.message_as_error:
            raise ImageGenerationError(
                _sanitize_error_message(message),
                status_code=400,
                error_type="invalid_request_error",
                code="content_policy_violation",
            )
        return [ImageOutput(kind="message", model=request.model, index=index, total=total, text=message)]
    raise ImageGenerationError(
        "generic image upstream returned no images",
        status_code=502,
        error_type="server_error",
        code="no_image_generated",
    )


def stream_generic_upstream_outputs(request: Any, index: int, total: int) -> list[Any]:
    from services.protocol.conversation import ImageGenerationError

    settings = _current_settings()
    semaphore = _acquire_semaphore()
    session = requests.Session()
    try:
        with semaphore:
            max_retries = max(0, int(settings.get("max_retries") or 2))
            for attempt in range(max_retries + 1):
                try:
                    return _call_upstream_once(session, request, index, total, settings)
                except _UpstreamRetryableError as exc:
                    if attempt < max_retries:
                        time.sleep(min(2.0 * (attempt + 1), 8.0))
                        continue
                    raise ImageGenerationError(
                        _sanitize_error_message(str(exc)),
                        status_code=502,
                        error_type="server_error",
                        code="upstream_error",
                    ) from exc
    finally:
        session.close()
