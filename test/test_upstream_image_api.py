from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import upstream_image_api
from services.config import DEFAULT_IMAGE_UPSTREAM
from services.protocol.conversation import ConversationRequest, ImageGenerationError


def _settings(**overrides: object) -> dict[str, object]:
    value = dict(DEFAULT_IMAGE_UPSTREAM)
    value.update(overrides)
    return value


def _image_b64(data: bytes = b"fake-image-bytes") -> str:
    return base64.b64encode(data).decode("ascii")


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: object | None = None, content: bytes = b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeSession:
    def __init__(
        self,
        post_responses: list[FakeResponse] | None = None,
        get_responses: list[FakeResponse] | None = None,
    ):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls: list[tuple[str, dict[str, object]]] = []
        self.get_calls: list[tuple[str, dict[str, object]]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.post_calls.append((url, kwargs))
        if self.post_responses:
            return self.post_responses.pop(0)
        return FakeResponse(200, {})

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.get_calls.append((url, kwargs))
        if self.get_responses:
            return self.get_responses.pop(0)
        return FakeResponse(200, {})

    def close(self) -> None:
        return None


class UpstreamRoutingTests(unittest.TestCase):
    def test_disabled_uses_account_chain(self) -> None:
        with mock.patch.object(
            upstream_image_api.config,
            "get_image_upstream_settings",
            return_value=_settings(enabled=False),
        ):
            self.assertFalse(upstream_image_api.should_use_generic_upstream("gpt-image-2"))

    def test_enabled_empty_models_routes_all(self) -> None:
        with mock.patch.object(
            upstream_image_api.config,
            "get_image_upstream_settings",
            return_value=_settings(enabled=True, base_url="https://up.example/v1", api_key="test-key"),
        ):
            self.assertTrue(upstream_image_api.should_use_generic_upstream("gpt-image-2"))
            self.assertTrue(upstream_image_api.should_use_generic_upstream("custom-image-model"))

    def test_enabled_specific_models_only(self) -> None:
        with mock.patch.object(
            upstream_image_api.config,
            "get_image_upstream_settings",
            return_value=_settings(
                enabled=True,
                base_url="https://up.example/v1",
                api_key="test-key",
                models=["gpt-image-2"],
            ),
        ):
            self.assertTrue(upstream_image_api.should_use_generic_upstream("gpt-image-2"))
            self.assertFalse(upstream_image_api.should_use_generic_upstream("codex-gpt-image-2"))

    def test_enabled_but_incomplete_config_falls_back(self) -> None:
        with mock.patch.object(
            upstream_image_api.config,
            "get_image_upstream_settings",
            return_value=_settings(enabled=True, base_url="", api_key=""),
        ):
            self.assertFalse(upstream_image_api.should_use_generic_upstream("gpt-image-2"))


class UpstreamGenerationTests(unittest.TestCase):
    def _run(self, session: FakeSession, request: ConversationRequest, settings: dict[str, object]):
        with (
            mock.patch.object(
                upstream_image_api.config,
                "get_image_upstream_settings",
                return_value=settings,
            ),
            mock.patch.object(upstream_image_api.requests, "Session", return_value=session),
            mock.patch.object(upstream_image_api.time, "sleep", return_value=None),
            mock.patch(
                "services.protocol.conversation.save_image_bytes",
                return_value="http://local.example/images/x.png",
            ),
        ):
            return upstream_image_api.stream_generic_upstream_outputs(request, 1, 1)

    def test_generations_b64_json(self) -> None:
        image_b64 = _image_b64()
        session = FakeSession(post_responses=[FakeResponse(200, {"data": [{"b64_json": image_b64}]})])
        request = ConversationRequest(model="gpt-image-2", prompt="hello", response_format="b64_json")
        outputs = self._run(
            session,
            request,
            _settings(enabled=True, base_url="https://up.example/v1", api_key="test-key"),
        )

        url, kwargs = session.post_calls[0]
        self.assertEqual(url, "https://up.example/v1/images/generations")
        self.assertEqual(kwargs["json"]["prompt"], "hello")
        self.assertEqual(kwargs["json"]["n"], 1)
        self.assertEqual(kwargs["json"]["response_format"], "url")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(outputs[0].data[0]["b64_json"], image_b64)

    def test_generations_url_downloads_and_saves(self) -> None:
        session = FakeSession(
            post_responses=[FakeResponse(200, {"data": [{"url": "https://cdn.example/a.png"}]})],
            get_responses=[FakeResponse(200, content=b"downloaded-bytes")],
        )
        request = ConversationRequest(model="gpt-image-2", prompt="hello", response_format="url")
        outputs = self._run(
            session,
            request,
            _settings(enabled=True, base_url="https://up.example/v1", api_key="test-key"),
        )

        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(outputs[0].data[0]["url"], "http://local.example/images/x.png")
        self.assertEqual(session.get_calls[0][0], "https://cdn.example/a.png")

    def test_edits_uploads_multipart_image(self) -> None:
        image_b64 = _image_b64()
        session = FakeSession(post_responses=[FakeResponse(200, {"data": [{"b64_json": image_b64}]})])
        request = ConversationRequest(
            model="gpt-image-2",
            prompt="make it red",
            images=[image_b64],
            response_format="b64_json",
        )
        outputs = self._run(session, request, _settings(enabled=True, base_url="https://up.example/v1"))

        url, kwargs = session.post_calls[0]
        self.assertEqual(url, "https://up.example/v1/images/edits")
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(kwargs["data"]["response_format"], "url")
        files = kwargs["files"]
        self.assertEqual(files[0][0], "image")
        self.assertEqual(files[0][1][0], "image_1.png")

    def test_rejection_maps_to_bad_request(self) -> None:
        session = FakeSession(post_responses=[FakeResponse(400, {"error": {"message": "bad prompt"}})])
        request = ConversationRequest(model="gpt-image-2", prompt="bad")
        with self.assertRaises(ImageGenerationError) as ctx:
            self._run(session, request, _settings(enabled=True, base_url="https://up.example/v1"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.code, "upstream_rejected")

    def test_200_with_message_maps_to_content_policy(self) -> None:
        session = FakeSession(post_responses=[FakeResponse(200, {"message": "内容包含敏感信息，无法生成"})])
        request = ConversationRequest(model="gpt-image-2", prompt="bad", message_as_error=True)
        with self.assertRaises(ImageGenerationError) as ctx:
            self._run(session, request, _settings(enabled=True, base_url="https://up.example/v1"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.code, "content_policy_violation")

    def test_retries_then_succeeds(self) -> None:
        image_b64 = _image_b64()
        session = FakeSession(
            post_responses=[
                FakeResponse(500),
                FakeResponse(200, {"data": [{"b64_json": image_b64}]}),
            ]
        )
        request = ConversationRequest(model="gpt-image-2", prompt="hello")
        outputs = self._run(
            session,
            request,
            _settings(enabled=True, base_url="https://up.example/v1", max_retries=1),
        )
        self.assertEqual(len(session.post_calls), 2)
        self.assertEqual(outputs[0].kind, "result")

    def test_504_with_task_id_polls_task_endpoint(self) -> None:
        image_b64 = _image_b64()
        session = FakeSession(
            post_responses=[FakeResponse(504, {"task_id": "task-123"})],
            get_responses=[
                FakeResponse(200, {"tasks": [{"status": "pending", "data": []}]}),
                FakeResponse(200, {"tasks": [{"status": "success", "data": [{"b64_json": image_b64}]}]}),
            ],
        )
        request = ConversationRequest(model="gpt-image-2", prompt="hello")
        outputs = self._run(
            session,
            request,
            _settings(
                enabled=True,
                base_url="https://up.example/v1",
                task_query_path="/api/image-tasks",
                poll_interval_secs=0.5,
                timeout_secs=30,
            ),
        )

        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(session.get_calls[0][0], "https://up.example/v1/api/image-tasks")
        self.assertEqual(session.get_calls[0][1]["params"], {"ids": "task-123"})
        self.assertEqual(outputs[0].kind, "result")
        self.assertEqual(outputs[0].data[0]["b64_json"], image_b64)


class UpstreamConfigTests(unittest.TestCase):
    def test_list_models_includes_upstream_models(self) -> None:
        from services.protocol import openai_v1_models

        with (
            mock.patch.object(
                openai_v1_models.model_catalog_service,
                "list_models",
                return_value={"object": "list", "data": []},
            ),
            mock.patch.object(openai_v1_models.account_service, "list_accounts", return_value=[]),
            mock.patch.object(
                openai_v1_models.config,
                "get_image_upstream_settings",
                return_value=_settings(enabled=True, models=["flux-image", "gpt-image-2"]),
            ),
        ):
            result = openai_v1_models.list_models()

        owners = {str(item["id"]): str(item["owned_by"]) for item in result["data"]}
        self.assertEqual(owners.get("flux-image"), "upstream")
        self.assertEqual(owners.get("gpt-image-2"), "upstream")

    def test_normalize_preserves_existing_api_key_when_empty(self) -> None:
        from services.config import _normalize_image_upstream_settings

        result = _normalize_image_upstream_settings(
            {"enabled": True, "base_url": "https://up.example/v1", "api_key": "", "_existing_api_key": "keep-me"}
        )
        self.assertEqual(result["api_key"], "keep-me")
        self.assertEqual(result["base_url"], "https://up.example/v1")

    def test_normalize_models_dedupe(self) -> None:
        from services.config import _normalize_image_upstream_settings

        result = _normalize_image_upstream_settings({"models": ["a", " a ", "b"]})
        self.assertEqual(result["models"], ["a", "b"])

    def test_validate_requires_base_url_and_api_key(self) -> None:
        from services.config import _validate_image_upstream_settings

        with self.assertRaises(ValueError):
            _validate_image_upstream_settings(_settings(enabled=True))
        with self.assertRaises(ValueError):
            _validate_image_upstream_settings(_settings(enabled=True, base_url="not-a-url"))
        # 未启用时不校验
        _validate_image_upstream_settings(_settings(enabled=False))

    def test_update_preserves_api_key_when_submitted_empty(self) -> None:
        from services.config import ConfigStore

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(
                json.dumps({
                    "auth-key": "test-auth",
                    "image_upstream": {
                        "enabled": True,
                        "base_url": "https://up.example/v1",
                        "api_key": "secret-1",
                    },
                }),
                encoding="utf-8",
            )
            store = ConfigStore(config_path)
            result = store.update({
                "image_upstream": {
                    "enabled": True,
                    "base_url": "https://up.example/v1",
                    "api_key": "",
                },
            })
            public_upstream = result["image_upstream"]
            self.assertEqual(public_upstream["has_api_key"], True)
            self.assertEqual(public_upstream["api_key"], "")
            self.assertEqual(store.get_image_upstream_settings()["api_key"], "secret-1")


if __name__ == "__main__":
    unittest.main()
