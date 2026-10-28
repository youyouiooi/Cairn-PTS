from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from fastapi.testclient import TestClient

USERNAME = "admin"
PASSWORD = "correct-horse-battery"
TOKEN = "cairn_test_token_aaaaaaaaaaaaaaaa"
ORIGIN = "http://testserver"
API_KEY = "sk-test-1234567890abcd"
BASE_URL = "https://api.example.com/anthropic"


def _prepare_env(root: Path) -> None:
    os.environ["CAIRN_AUTH_USERNAME"] = USERNAME
    os.environ["CAIRN_AUTH_PASSWORD"] = PASSWORD
    os.environ["CAIRN_API_TOKEN"] = TOKEN
    os.environ["CAIRN_CREDENTIALS_FILE"] = str(root / "credentials")
    os.environ["CAIRN_DB_PATH"] = str(root / "cairn.db")
    os.environ.pop("CAIRN_TRUST_PROXY", None)


def _create_body(**overrides) -> dict:
    payload = {
        "name": "default",
        "enabled": True,
        "protocol": "anthropic",
        "base_url": BASE_URL,
        "model": "deepseek-chat",
        "small_fast_model": "",
        "headers": {},
        "api_key": API_KEY,
    }
    payload.update(overrides)
    return payload


def _probe_body(**overrides) -> dict:
    payload = {
        "protocol": "anthropic",
        "base_url": BASE_URL,
        "model": "deepseek-chat",
        "headers": {},
        "api_key": API_KEY,
    }
    payload.update(overrides)
    return payload


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: bytes = b""):
        self.status_code = status_code
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size: int = 1024):
        for start in range(0, len(self._body), max(1, chunk_size)):
            yield self._body[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class LlmConfigApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        _prepare_env(root)
        from cairn.server import auth, db

        db.reset()
        db.configure(root / "cairn.db")
        auth.bootstrap_auth()
        auth.reset_login_attempts()
        from cairn.server.app import app

        self.auth = auth
        self.client = TestClient(app)
        self._login()

    def tearDown(self) -> None:
        self.client.close()
        from cairn.server import db

        db.reset()
        self._tmp.cleanup()

    # helpers ---------------------------------------------------------------

    def _login(self) -> None:
        response = self.client.post(
            "/auth/login", json={"username": USERNAME, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()["csrf_token"]

    def _session_headers(self, **extra) -> dict[str, str]:
        headers = {"Origin": ORIGIN, "X-CSRF-Token": self.csrf}
        headers.update(extra)
        return headers

    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}"}

    def _get(self, path: str):
        return self.client.get(path)

    def _post(self, path: str, payload: dict):
        return self.client.post(path, json=payload, headers=self._session_headers())

    def _put(self, path: str, payload: dict):
        return self.client.put(path, json=payload, headers=self._session_headers())

    def _delete(self, path: str):
        return self.client.delete(path, headers=self._session_headers())

    def _runtime(self):
        return self.client.get("/llm-configs/runtime", headers=self._bearer())

    def _create(self, **overrides):
        return self._post("/llm-configs", _create_body(**overrides))

    def _create_ok(self, **overrides) -> dict:
        response = self._create(**overrides)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _list(self) -> list[dict]:
        response = self._get("/llm-configs")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["configs"]

    def _probe(self, payload: dict):
        return self._post("/llm-configs/test", payload)

    # 1. auth channels ------------------------------------------------------

    def test_unauthenticated_is_rejected(self) -> None:
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/llm-configs").status_code, 401)
        self.assertEqual(
            self.client.post("/llm-configs", json=_create_body()).status_code, 401
        )
        self.assertEqual(
            self.client.get(
                "/llm-configs/runtime", headers=self._bearer()
            ).status_code,
            200,
        )

    def test_session_endpoints_reject_bearer_token(self) -> None:
        self.client.cookies.clear()
        bearer = self._bearer()
        update_body = {
            "name": "default",
            "enabled": True,
            "protocol": "anthropic",
            "base_url": BASE_URL,
            "model": "m",
            "headers": {},
        }
        requests_to_check = [
            ("GET", "/llm-configs", None),
            ("POST", "/llm-configs", _create_body()),
            ("PUT", "/llm-configs/1", update_body),
            ("DELETE", "/llm-configs/1", None),
            ("POST", "/llm-configs/reorder", {"ids": [1]}),
            ("POST", "/llm-configs/test", _probe_body()),
        ]
        for method, path, payload in requests_to_check:
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, json=payload, headers=bearer)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertIn("logged-in session", response.json()["detail"])

        # The channel check runs before body validation, so even body-less bearer
        # calls report 403 rather than 422.
        for method, path in [
            ("POST", "/llm-configs"),
            ("PUT", "/llm-configs/1"),
            ("POST", "/llm-configs/reorder"),
            ("POST", "/llm-configs/test"),
        ]:
            with self.subTest(method=method, path=path, body="missing"):
                response = self.client.request(method, path, headers=bearer)
                self.assertEqual(response.status_code, 403, response.text)

    def test_runtime_endpoint_is_token_only(self) -> None:
        response = self._get("/llm-configs/runtime")
        self.assertEqual(response.status_code, 403)
        self.assertIn("dispatcher", response.json()["detail"])
        self.assertEqual(self._runtime().status_code, 200)

    # 2. empty list + create ------------------------------------------------

    def test_empty_list_then_create_returns_masked_view(self) -> None:
        self.assertEqual(self._list(), [])

        response = self._create()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertIsInstance(body["id"], int)
        self.assertEqual(body["name"], "default")
        self.assertTrue(body["enabled"])
        self.assertEqual(body["protocol"], "anthropic")
        self.assertEqual(body["base_url"], BASE_URL)
        self.assertEqual(body["model"], "deepseek-chat")
        self.assertEqual(body["priority"], 0)
        self.assertTrue(body["updated_at"])
        self.assertTrue(body["has_api_key"])
        self.assertTrue(body["api_key_hint"])
        self.assertNotIn("api_key", body)
        self.assertNotIn(API_KEY, response.text)
        self.assertNotIn(API_KEY[3:-4], response.text)

        listed = self._list()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], body["id"])
        self.assertNotIn(API_KEY, self._get("/llm-configs").text)

    def test_create_assigns_priority_in_steps_of_ten(self) -> None:
        first = self._create_ok(name="first")
        second = self._create_ok(name="second")
        third = self._create_ok(name="third")
        self.assertEqual([first["priority"], second["priority"], third["priority"]], [0, 10, 20])

    # 3. name conflicts -----------------------------------------------------

    def test_duplicate_name_conflicts(self) -> None:
        first = self._create_ok(name="prod")
        duplicate = self._create(name="prod")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["detail"], "LLM config name already exists")

        whitespace = self._create(name="  prod  ")
        self.assertEqual(whitespace.status_code, 409)

        other = self._create_ok(name="staging")
        rename = self._put(
            f"/llm-configs/{other['id']}",
            {
                "name": "prod",
                "enabled": True,
                "protocol": "anthropic",
                "base_url": BASE_URL,
                "model": "m",
                "headers": {},
            },
        )
        self.assertEqual(rename.status_code, 409)
        self.assertEqual(rename.json()["detail"], "LLM config name already exists")
        self.assertEqual(self._list()[0]["id"], first["id"])

    # 4. validation (422) ---------------------------------------------------

    def test_validation_errors(self) -> None:
        bad_header_name = self._create(headers={"Bad Name": "x"})
        self.assertEqual(bad_header_name.status_code, 422, bad_header_name.text)

        crlf_value = self._create(headers={"X-Test": "a\r\nInjected: 1"})
        self.assertEqual(crlf_value.status_code, 422, crlf_value.text)

        too_many = self._create(
            headers={f"X-H{index}": "v" for index in range(21)}
        )
        self.assertEqual(too_many.status_code, 422, too_many.text)

        missing_key = self._create(api_key="")
        self.assertEqual(missing_key.status_code, 422, missing_key.text)

        empty_name = self._create(name="   ")
        self.assertEqual(empty_name.status_code, 422, empty_name.text)

        bad_url = self._create(base_url="ftp://api.example.com")
        self.assertEqual(bad_url.status_code, 422, bad_url.text)

        self.assertEqual(self._list(), [])

    # 5. PUT key retention / clearing / target guard ------------------------

    def test_put_keeps_key_clears_key_and_guards_target_changes(self) -> None:
        created = self._create_ok()
        config_id = created["id"]

        kept = self._put(
            f"/llm-configs/{config_id}",
            {
                "name": "default",
                "enabled": True,
                "protocol": "anthropic",
                "base_url": BASE_URL,
                "model": "another-model",
                "headers": {"X-Tenant": "acme"},
            },
        )
        self.assertEqual(kept.status_code, 200, kept.text)
        self.assertTrue(kept.json()["has_api_key"])
        self.assertEqual(kept.json()["headers"], {"X-Tenant": "acme"})
        self.assertEqual(
            self._runtime().json()["configs"][0]["api_key"], API_KEY
        )

        clear_while_enabled = self._put(
            f"/llm-configs/{config_id}",
            {
                "name": "default",
                "enabled": True,
                "protocol": "anthropic",
                "base_url": BASE_URL,
                "model": "another-model",
                "api_key": "",
            },
        )
        self.assertEqual(clear_while_enabled.status_code, 400)
        self.assertEqual(
            clear_while_enabled.json()["detail"], "API key is required when enabled"
        )

        cleared = self._put(
            f"/llm-configs/{config_id}",
            {
                "name": "default",
                "enabled": False,
                "protocol": "anthropic",
                "base_url": BASE_URL,
                "model": "another-model",
                "api_key": "",
            },
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertFalse(cleared.json()["has_api_key"])
        self.assertEqual(cleared.json()["api_key_hint"], "")

        changed_url = self._put(
            f"/llm-configs/{config_id}",
            {
                "name": "default",
                "enabled": True,
                "protocol": "anthropic",
                "base_url": "https://attacker.example.com",
                "model": "another-model",
            },
        )
        self.assertEqual(changed_url.status_code, 400)
        self.assertEqual(
            changed_url.json()["detail"],
            "base_url/protocol changed; re-enter the API key",
        )

        changed_protocol = self._put(
            f"/llm-configs/{config_id}",
            {
                "name": "default",
                "enabled": False,
                "protocol": "openai_chat",
                "base_url": BASE_URL,
                "model": "another-model",
            },
        )
        self.assertEqual(changed_protocol.status_code, 400)
        self.assertEqual(
            changed_protocol.json()["detail"],
            "base_url/protocol changed; re-enter the API key",
        )

        missing = self._put("/llm-configs/9999", {
            "name": "ghost",
            "enabled": False,
            "protocol": "anthropic",
            "base_url": "",
            "model": "",
        })
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"], "LLM config not found")

    # 6. reorder ------------------------------------------------------------

    def test_reorder_takes_effect_and_requires_full_coverage(self) -> None:
        names = ["alpha", "beta", "gamma"]
        ids = [self._create_ok(name=name)["id"] for name in names]
        self.assertEqual([item["name"] for item in self._list()], names)

        new_order = [ids[2], ids[0], ids[1]]
        response = self._post("/llm-configs/reorder", {"ids": new_order})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [item["name"] for item in response.json()["configs"]],
            ["gamma", "alpha", "beta"],
        )
        listed = self._list()
        self.assertEqual([item["name"] for item in listed], ["gamma", "alpha", "beta"])
        self.assertEqual([item["priority"] for item in listed], [0, 10, 20])

        partial = self._post("/llm-configs/reorder", {"ids": new_order[:2]})
        self.assertEqual(partial.status_code, 400)
        self.assertEqual(partial.json()["detail"], "ids must cover all configurations")

        unknown = self._post(
            "/llm-configs/reorder", {"ids": [ids[0], ids[1], 9999]}
        )
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()["detail"], "ids must cover all configurations")

        duplicates = self._post(
            "/llm-configs/reorder", {"ids": [ids[0], ids[0], ids[1]]}
        )
        self.assertEqual(duplicates.status_code, 422)

        empty = self._post("/llm-configs/reorder", {"ids": []})
        self.assertEqual(empty.status_code, 422)

    # 7. delete -------------------------------------------------------------

    def test_delete_removes_config_and_missing_returns_404(self) -> None:
        created = self._create_ok(name="temp")
        response = self._delete(f"/llm-configs/{created['id']}")
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.text, "")
        self.assertEqual(self._list(), [])

        again = self._delete(f"/llm-configs/{created['id']}")
        self.assertEqual(again.status_code, 404)
        self.assertEqual(again.json()["detail"], "LLM config not found")

    # 8. runtime ------------------------------------------------------------

    def test_runtime_returns_enabled_pool_with_plaintext_keys(self) -> None:
        empty = self._runtime()
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json(), {"configs": []})

        first = self._create_ok(name="first", headers={"X-Tenant": "acme"})
        self._create_ok(name="disabled", enabled=False, api_key="")
        third = self._create_ok(name="third")

        runtime = self._runtime()
        self.assertEqual(runtime.status_code, 200, runtime.text)
        configs = runtime.json()["configs"]
        self.assertEqual([item["name"] for item in configs], ["first", "third"])
        self.assertEqual(configs[0]["api_key"], API_KEY)
        self.assertEqual(configs[0]["headers"], {"X-Tenant": "acme"})
        self.assertEqual(configs[0]["protocol"], "anthropic")
        self.assertEqual([item["priority"] for item in configs], [0, 20])
        self.assertNotIn("disabled", [item["name"] for item in configs])

        reorder = self._post(
            "/llm-configs/reorder",
            {"ids": [third["id"], first["id"]]},
        )
        # The disabled config still has to be part of the full id set.
        self.assertEqual(reorder.status_code, 400)

        full = self._post(
            "/llm-configs/reorder",
            {
                "ids": [
                    third["id"],
                    first["id"],
                    [item for item in self._list() if item["name"] == "disabled"][0]["id"],
                ]
            },
        )
        self.assertEqual(full.status_code, 200, full.text)
        self.assertEqual(
            [item["name"] for item in self._runtime().json()["configs"]],
            ["third", "first"],
        )

    # 9. test/probe endpoint ------------------------------------------------

    def test_probe_builds_protocol_specific_requests(self) -> None:
        cases = [
            (
                "anthropic",
                "https://api.example.com/anthropic",
                "claude-3",
                "https://api.example.com/anthropic/v1/messages",
            ),
            (
                "openai_responses",
                "https://api.example.com/v1",
                "gpt-5",
                "https://api.example.com/v1/responses",
            ),
            (
                "openai_chat",
                "https://api.example.com/v1",
                "gpt-4o",
                "https://api.example.com/v1/chat/completions",
            ),
            (
                "gemini",
                "https://generativelanguage.googleapis.com/v1beta",
                "gemini-pro",
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent",
            ),
        ]
        for protocol, base_url, model, expected_url in cases:
            with self.subTest(protocol=protocol):
                fake = _FakeResponse(200)
                with patch(
                    "cairn.server.routers.llm_config.requests.post",
                    return_value=fake,
                ) as post:
                    response = self._probe(
                        _probe_body(
                            protocol=protocol, base_url=base_url, model=model
                        )
                    )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertTrue(body["ok"])
                self.assertEqual(body["status_code"], 200)
                self.assertIsNone(body["error"])
                self.assertGreaterEqual(body["latency_ms"], 0)
                self.assertTrue(fake.closed)

                args, kwargs = post.call_args
                self.assertEqual(args[0], expected_url)
                self.assertEqual(kwargs["timeout"], (3.05, 10))
                self.assertFalse(kwargs["allow_redirects"])
                self.assertTrue(kwargs["stream"])
                headers = kwargs["headers"]
                sent = kwargs["json"]
                if protocol == "anthropic":
                    self.assertEqual(sent["model"], model)
                    self.assertEqual(headers["Authorization"], f"Bearer {API_KEY}")
                    self.assertEqual(headers["anthropic-version"], "2023-06-01")
                    self.assertEqual(sent["max_tokens"], 10)
                    self.assertEqual(
                        sent["messages"], [{"role": "user", "content": "ping"}]
                    )
                elif protocol == "openai_responses":
                    self.assertEqual(sent["model"], model)
                    self.assertEqual(headers["Authorization"], f"Bearer {API_KEY}")
                    self.assertEqual(sent["max_output_tokens"], 16)
                    self.assertFalse(sent["stream"])
                    self.assertEqual(
                        sent["input"], [{"role": "user", "content": "ping"}]
                    )
                elif protocol == "openai_chat":
                    self.assertEqual(sent["model"], model)
                    self.assertEqual(headers["Authorization"], f"Bearer {API_KEY}")
                    self.assertEqual(sent["max_tokens"], 10)
                    self.assertFalse(sent["stream"])
                    self.assertEqual(
                        sent["messages"], [{"role": "user", "content": "ping"}]
                    )
                else:
                    self.assertEqual(headers["x-goog-api-key"], API_KEY)
                    self.assertNotIn("Authorization", headers)
                    self.assertEqual(
                        sent["contents"], [{"parts": [{"text": "ping"}]}]
                    )
                    self.assertEqual(sent["generationConfig"], {"maxOutputTokens": 10})
                self.assertEqual(headers["content-type"], "application/json")

    def test_probe_custom_headers_override_protocol_defaults(self) -> None:
        with patch(
            "cairn.server.routers.llm_config.requests.post",
            return_value=_FakeResponse(200),
        ) as post:
            response = self._probe(
                _probe_body(
                    protocol="gemini",
                    base_url="https://generativelanguage.googleapis.com/v1beta",
                    model="gemini-pro",
                    headers={"X-Goog-Api-Key": "custom-key", "X-Tenant": "acme"},
                )
            )
        self.assertTrue(response.json()["ok"])
        headers = post.call_args.kwargs["headers"]
        goog_keys = [key for key in headers if key.lower() == "x-goog-api-key"]
        self.assertEqual(goog_keys, ["X-Goog-Api-Key"])
        self.assertEqual(headers["X-Goog-Api-Key"], "custom-key")
        self.assertEqual(headers["X-Tenant"], "acme")

        with patch(
            "cairn.server.routers.llm_config.requests.post",
            return_value=_FakeResponse(200),
        ) as post:
            self._probe(
                _probe_body(headers={"authorization": "Token custom"})
            )
        headers = post.call_args.kwargs["headers"]
        auth_keys = [key for key in headers if key.lower() == "authorization"]
        self.assertEqual(auth_keys, ["authorization"])
        self.assertEqual(headers["authorization"], "Token custom")

    def test_probe_reports_non_2xx_and_redacts_key(self) -> None:
        body = ("{\"error\":\"invalid key %s\"}" % API_KEY).encode()
        fake = _FakeResponse(401, body)
        with patch(
            "cairn.server.routers.llm_config.requests.post",
            return_value=fake,
        ):
            response = self._probe(_probe_body())
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status_code"], 401)
        self.assertEqual(payload["error"], "HTTP 401")
        self.assertIn("invalid key", payload["response_preview"])
        self.assertIn("***", payload["response_preview"])
        self.assertNotIn(API_KEY, response.text)
        self.assertTrue(fake.closed)

    def test_probe_survives_body_read_failure(self) -> None:
        class _BrokenBodyResponse(_FakeResponse):
            def iter_content(self, chunk_size: int = 1024):
                raise requests.exceptions.ChunkedEncodingError(
                    f"connection broken while reading {API_KEY}"
                )

        fake = _BrokenBodyResponse(502, b"ignored")
        with patch(
            "cairn.server.routers.llm_config.requests.post",
            return_value=fake,
        ):
            response = self._probe(_probe_body())
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status_code"], 502)
        self.assertIsNone(payload["response_preview"])
        self.assertIn("HTTP 502", payload["error"])
        self.assertIn("***", payload["error"])
        self.assertNotIn(API_KEY, response.text)
        self.assertTrue(fake.closed)

    def test_probe_reports_exceptions_and_redacts_key(self) -> None:
        with patch(
            "cairn.server.routers.llm_config.requests.post",
            side_effect=requests.ConnectionError(f"connection failed for {API_KEY}"),
        ) as post:
            response = self._probe(_probe_body())
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["status_code"])
        self.assertIn("***", payload["error"])
        self.assertNotIn(API_KEY, response.text)
        post.assert_called_once()

    def test_probe_does_not_persist_anything(self) -> None:
        with patch(
            "cairn.server.routers.llm_config.requests.post",
            return_value=_FakeResponse(200),
        ):
            self._probe(_probe_body())
        self.assertEqual(self._list(), [])

    def test_probe_reuses_stored_key_only_for_the_same_target(self) -> None:
        created = self._create_ok()
        config_id = created["id"]

        with patch(
            "cairn.server.routers.llm_config.requests.post",
            return_value=_FakeResponse(200),
        ) as post:
            reused = self._probe(
                _probe_body(
                    id=config_id,
                    model="different-model",
                    api_key=None,
                )
            )
        self.assertTrue(reused.json()["ok"])
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"], f"Bearer {API_KEY}"
        )

        with patch("cairn.server.routers.llm_config.requests.post") as post:
            different_base = self._probe(
                _probe_body(
                    id=config_id,
                    base_url="https://attacker.example.com",
                    api_key=None,
                )
            )
        self.assertEqual(different_base.status_code, 400)
        self.assertEqual(different_base.json()["detail"], "API key required")
        post.assert_not_called()

        with patch("cairn.server.routers.llm_config.requests.post") as post:
            different_protocol = self._probe(
                _probe_body(id=config_id, protocol="openai_chat", api_key=None)
            )
        self.assertEqual(different_protocol.status_code, 400)
        self.assertEqual(different_protocol.json()["detail"], "API key required")
        post.assert_not_called()

        without_id = self._probe(_probe_body(api_key=None))
        self.assertEqual(without_id.status_code, 400)
        self.assertEqual(without_id.json()["detail"], "API key required")

        missing_id = self._probe(_probe_body(id=9999, api_key=None))
        self.assertEqual(missing_id.status_code, 400)
        self.assertEqual(missing_id.json()["detail"], "API key required")

        with patch(
            "cairn.server.routers.llm_config.requests.post",
            return_value=_FakeResponse(200),
        ) as post:
            explicit = self._probe(
                _probe_body(id=config_id, api_key="sk-explicit-000000")
            )
        self.assertTrue(explicit.json()["ok"])
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer sk-explicit-000000",
        )


if __name__ == "__main__":
    unittest.main()
