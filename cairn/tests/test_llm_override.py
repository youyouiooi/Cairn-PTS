"""Unit tests for the dispatcher-side LLM configuration pool.

Covers contract section 3.4 (protocol/worker compatibility, pool parsing and
ordering, cooldown/selection policy, per-worker env overrides, apply_pool
immutability, fingerprints) plus the scheduler rotation paths and the
adapter-side custom HTTP header rendering (contract section 3.3).
"""

from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from unittest import mock

import requests
import yaml

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.llm_override import (
    ANTHROPIC_CUSTOM_HEADERS_ENV,
    CONFIG_FAILURE_COOLDOWN_BASE_SECONDS,
    CONFIG_FAILURE_COOLDOWN_MAX_SECONDS,
    LLM_HTTP_HEADERS_ENV,
    PI_API_BY_PROTOCOL,
    LlmCandidate,
    apply_pool,
    candidates_for_worker,
    cooldown_seconds,
    next_candidate_after_failure,
    parse_pool,
    pool_fingerprint,
    protocol_supported,
    select_candidate,
    worker_env_overrides,
)
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.startup_healthcheck import StartupHealthcheckResult
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver
from cairn.dispatcher.workers.adapters.codex import CodexDriver
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.server.models import LlmConfigPayload

PROTOCOLS = ("anthropic", "openai_responses", "openai_chat", "gemini")

RAW_CONFIG = {
    "server": "http://127.0.0.1:8080",
    "api_token": "test-token",
    "runtime": {
        "max_workers": 4,
        "max_running_projects": 2,
        "max_project_workers": 2,
        "interval": 1,
        "healthcheck_timeout": 10,
        "prompt_group": "mock",
        "executor": "local",
    },
    "tasks": {
        "bootstrap": {"timeout": 10, "conclude_timeout": 10},
        "reason": {"timeout": 10, "max_intents": 3},
        "explore": {"timeout": 10, "conclude_timeout": 10},
    },
    "container": {},
    "common_env": {},
    "workers": [
        {
            "name": "cc",
            "type": "claudecode",
            "task_types": ["reason", "explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "ANTHROPIC_BASE_URL": "https://yaml.example.com/anthropic",
                "ANTHROPIC_MODEL": "yaml-model",
                "ANTHROPIC_AUTH_TOKEN": "yaml-key",
            },
        },
        {
            "name": "cx",
            "type": "codex",
            "task_types": ["reason", "explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "CODEX_BASE_URL": "https://yaml.example.com/responses",
                "CODEX_MODEL": "yaml-model",
                "OPENAI_API_KEY": "yaml-key",
            },
        },
        {
            "name": "pi",
            "type": "pi",
            "task_types": ["reason", "explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_BASE_URL": "https://yaml.example.com/pi",
                "PI_MODEL": "yaml-model",
                "PI_API_KEY": "yaml-key",
                "PI_PROVIDER_API": "anthropic-messages",
            },
        },
        {
            "name": "mock",
            "type": "mock",
            "task_types": ["bootstrap"],
            "max_running": 1,
            "priority": 0,
        },
    ],
}

_ENV_BY_TYPE = {
    "claudecode": {
        "ANTHROPIC_BASE_URL": "https://yaml.example.com/anthropic",
        "ANTHROPIC_MODEL": "yaml-model",
        "ANTHROPIC_AUTH_TOKEN": "yaml-key",
    },
    "codex": {
        "CODEX_BASE_URL": "https://yaml.example.com/responses",
        "CODEX_MODEL": "yaml-model",
        "OPENAI_API_KEY": "yaml-key",
    },
    "pi": {
        "PI_BASE_URL": "https://yaml.example.com/pi",
        "PI_MODEL": "yaml-model",
        "PI_API_KEY": "yaml-key",
        "PI_PROVIDER_API": "anthropic-messages",
    },
    "mock": {},
}


def _dispatch_config(*, extra_env: dict[str, dict[str, str]] | None = None) -> DispatchConfig:
    raw = copy.deepcopy(RAW_CONFIG)
    for name, values in (extra_env or {}).items():
        for worker in raw["workers"]:
            if worker["name"] == name:
                worker["env"].update(values)
    return DispatchConfig.model_validate(raw)


def _worker(
    worker_type: str,
    name: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> WorkerConfig:
    env = dict(_ENV_BY_TYPE[worker_type])
    env.update(env_extra or {})
    return WorkerConfig.model_validate(
        {
            "name": name or f"test-{worker_type}",
            "type": worker_type,
            "task_types": ["reason"],
            "max_running": 1,
            "priority": 0,
            "env": env,
        }
    )


def _payload(
    config_id: int,
    *,
    protocol: str = "anthropic",
    base_url: str | None = None,
    model: str | None = None,
    small_fast_model: str = "small-model",
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
    enabled: bool = True,
) -> LlmConfigPayload:
    return LlmConfigPayload(
        id=config_id,
        name=f"config-{config_id}",
        protocol=protocol,
        base_url=base_url or f"https://api{config_id}.example.com/v1",
        model=model or f"model-{config_id}",
        small_fast_model=small_fast_model,
        api_key=f"key-{config_id}" if api_key is None else api_key,
        headers=dict(headers or {}),
        enabled=enabled,
    )


def _candidate(
    config_id: int,
    *,
    protocol: str = "anthropic",
    base_url: str | None = None,
    model: str | None = None,
    small_fast_model: str = "small-model",
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
) -> LlmCandidate:
    return LlmCandidate(
        config_id=config_id,
        name=f"config-{config_id}",
        protocol=protocol,
        base_url=base_url or f"https://api{config_id}.example.com/v1",
        model=model or f"model-{config_id}",
        small_fast_model=small_fast_model,
        api_key=f"key-{config_id}" if api_key is None else api_key,
        headers=dict(headers or {}),
    )


def _startup_result(name: str, ok: bool) -> StartupHealthcheckResult:
    return StartupHealthcheckResult(
        worker_name=name,
        ok=ok,
        returncode=0 if ok else 1,
        duration_ms=1,
        http_status="200" if ok else None,
        response_preview="ok" if ok else "failed",
        stderr_preview="",
        command="healthcheck",
    )


class ProtocolMatrixTests(unittest.TestCase):
    def test_worker_protocol_matrix(self) -> None:
        expected = {
            "claudecode": {"anthropic"},
            "codex": {"openai_responses"},
            "pi": set(PROTOCOLS),
            "mock": set(),
        }
        for worker_type, supported in expected.items():
            for protocol in PROTOCOLS:
                with self.subTest(worker_type=worker_type, protocol=protocol):
                    self.assertEqual(
                        protocol_supported(worker_type, protocol),
                        protocol in supported,
                    )

    def test_unknown_worker_type_is_unsupported(self) -> None:
        for protocol in PROTOCOLS:
            self.assertFalse(protocol_supported("bogus", protocol))

    def test_pi_api_mapping(self) -> None:
        self.assertEqual(
            PI_API_BY_PROTOCOL,
            {
                "anthropic": "anthropic-messages",
                "openai_responses": "openai-responses",
                "openai_chat": "openai-completions",
                "gemini": "google-generative-ai",
            },
        )


class CandidateTests(unittest.TestCase):
    def test_candidates_keep_pool_order_and_filter_by_protocol(self) -> None:
        pool = [
            _candidate(1),
            _candidate(2, protocol="openai_responses"),
            _candidate(3),
        ]
        self.assertEqual(
            [candidate.config_id for candidate in candidates_for_worker(_worker("claudecode"), pool)],
            [1, 3],
        )
        self.assertEqual(
            [candidate.config_id for candidate in candidates_for_worker(_worker("codex"), pool)],
            [2],
        )
        self.assertEqual(
            [candidate.config_id for candidate in candidates_for_worker(_worker("pi"), pool)],
            [1, 2, 3],
        )
        self.assertEqual(candidates_for_worker(_worker("mock"), pool), [])

    def test_pi_supports_every_protocol(self) -> None:
        pool = [_candidate(index, protocol=protocol) for index, protocol in enumerate(PROTOCOLS, start=1)]
        self.assertEqual(
            [candidate.protocol for candidate in candidates_for_worker(_worker("pi"), pool)],
            list(PROTOCOLS),
        )


class ParsePoolTests(unittest.TestCase):
    def test_parse_pool_maps_fields_and_skips_disabled_or_keyless(self) -> None:
        payloads = [
            _payload(1),
            _payload(2, protocol="openai_responses", model="model-2"),
            _payload(3, enabled=False),
            _payload(4, api_key=""),
            _payload(5, protocol="gemini", headers={"X-Test": "yes"}, small_fast_model="small-5"),
        ]
        pool = parse_pool(payloads)
        self.assertEqual([candidate.config_id for candidate in pool], [1, 2, 5])
        third = pool[2]
        self.assertEqual(
            (third.name, third.protocol, third.base_url, third.model, third.small_fast_model, third.api_key),
            (
                "config-5",
                "gemini",
                "https://api5.example.com/v1",
                "model-5",
                "small-5",
                "key-5",
            ),
        )
        self.assertEqual(third.headers, {"X-Test": "yes"})

    def test_parse_pool_copies_headers(self) -> None:
        payload = _payload(1, headers={"X-Test": "yes"})
        pool = parse_pool([payload])
        payload.headers["X-Test"] = "changed"
        payload.headers["X-Later"] = "later"
        self.assertEqual(pool[0].headers, {"X-Test": "yes"})

    def test_parse_pool_preserves_server_order(self) -> None:
        pool = parse_pool([_payload(30), _payload(10), _payload(20)])
        self.assertEqual([candidate.config_id for candidate in pool], [30, 10, 20])


class CooldownTests(unittest.TestCase):
    def test_exponential_backoff_capped_at_max(self) -> None:
        cases = [
            (0, 30.0),
            (1, 30.0),
            (2, 60.0),
            (3, 120.0),
            (4, 240.0),
            (5, 300.0),
            (6, 300.0),
            (10, 300.0),
        ]
        for failures, expected in cases:
            with self.subTest(failures=failures):
                self.assertEqual(cooldown_seconds(failures), expected)

    def test_constants_match_contract(self) -> None:
        self.assertEqual(CONFIG_FAILURE_COOLDOWN_BASE_SECONDS, 30)
        self.assertEqual(CONFIG_FAILURE_COOLDOWN_MAX_SECONDS, 300)


class SelectCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = _worker("claudecode")
        self.pool = [_candidate(1), _candidate(2), _candidate(3)]

    def test_first_available_candidate_without_preference(self) -> None:
        candidate = select_candidate(self.worker, self.pool, cooldown_until={}, now=100.0)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.config_id, 1)

    def test_preferred_candidate_wins_when_available(self) -> None:
        candidate = select_candidate(self.worker, self.pool, cooldown_until={}, now=100.0, preferred_id=2)
        self.assertEqual(candidate.config_id, 2)

    def test_preferred_candidate_is_skipped_while_cooling(self) -> None:
        candidate = select_candidate(
            self.worker,
            self.pool,
            cooldown_until={1: 200.0},
            now=100.0,
            preferred_id=1,
        )
        self.assertEqual(candidate.config_id, 2)

    def test_skips_cooling_candidates(self) -> None:
        candidate = select_candidate(self.worker, self.pool, cooldown_until={1: 200.0}, now=100.0)
        self.assertEqual(candidate.config_id, 2)

    def test_expired_cooldown_is_available_again(self) -> None:
        candidate = select_candidate(self.worker, self.pool, cooldown_until={1: 50.0}, now=100.0)
        self.assertEqual(candidate.config_id, 1)

    def test_all_cooling_returns_earliest_expiry(self) -> None:
        candidate = select_candidate(
            self.worker,
            self.pool,
            cooldown_until={1: 500.0, 2: 300.0, 3: 400.0},
            now=100.0,
        )
        self.assertEqual(candidate.config_id, 2)

    def test_all_cooling_tie_keeps_pool_order(self) -> None:
        candidate = select_candidate(
            self.worker,
            self.pool,
            cooldown_until={1: 500.0, 2: 500.0, 3: 400.0},
            now=100.0,
        )
        self.assertEqual(candidate.config_id, 3)
        tied = select_candidate(
            self.worker,
            self.pool,
            cooldown_until={1: 500.0, 2: 500.0, 3: 500.0},
            now=100.0,
        )
        self.assertEqual(tied.config_id, 1)

    def test_preferred_candidate_outside_pool_is_ignored(self) -> None:
        candidate = select_candidate(self.worker, self.pool, cooldown_until={}, now=100.0, preferred_id=99)
        self.assertEqual(candidate.config_id, 1)

    def test_no_compatible_candidate_returns_none(self) -> None:
        pool = [_candidate(9, protocol="openai_responses")]
        self.assertIsNone(select_candidate(self.worker, pool, cooldown_until={}, now=100.0))


class NextCandidateAfterFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = _worker("claudecode")
        self.pool = [_candidate(1), _candidate(2), _candidate(3)]

    def test_skips_failed_candidate(self) -> None:
        candidate = next_candidate_after_failure(
            self.worker,
            self.pool,
            failed_id=1,
            cooldown_until={},
            now=100.0,
        )
        self.assertEqual(candidate.config_id, 2)

    def test_skips_cooling_alternatives(self) -> None:
        candidate = next_candidate_after_failure(
            self.worker,
            self.pool,
            failed_id=2,
            cooldown_until={1: 150.0},
            now=100.0,
        )
        self.assertEqual(candidate.config_id, 3)

    def test_all_alternatives_cooling_returns_earliest_expiry(self) -> None:
        candidate = next_candidate_after_failure(
            self.worker,
            self.pool,
            failed_id=2,
            cooldown_until={1: 150.0, 3: 120.0},
            now=100.0,
        )
        self.assertEqual(candidate.config_id, 3)

    def test_single_candidate_has_no_alternative(self) -> None:
        candidate = next_candidate_after_failure(
            self.worker,
            [_candidate(1)],
            failed_id=1,
            cooldown_until={},
            now=100.0,
        )
        self.assertIsNone(candidate)

    def test_incompatible_candidates_are_not_alternatives(self) -> None:
        pool = [_candidate(1), _candidate(9, protocol="openai_responses")]
        candidate = next_candidate_after_failure(
            self.worker,
            pool,
            failed_id=1,
            cooldown_until={},
            now=100.0,
        )
        self.assertIsNone(candidate)

    def test_empty_pool_returns_none(self) -> None:
        candidate = next_candidate_after_failure(
            self.worker,
            [],
            failed_id=1,
            cooldown_until={},
            now=100.0,
        )
        self.assertIsNone(candidate)


class WorkerEnvOverridesTests(unittest.TestCase):
    def test_claudecode_anthropic_maps_every_field_and_headers(self) -> None:
        headers = {"X-Api-Key": "secret", "X-Title": "café"}
        overrides = worker_env_overrides(_worker("claudecode"), _candidate(1, headers=headers))
        self.assertEqual(
            overrides,
            {
                "ANTHROPIC_BASE_URL": "https://api1.example.com/v1",
                "ANTHROPIC_MODEL": "model-1",
                "ANTHROPIC_AUTH_TOKEN": "key-1",
                "ANTHROPIC_SMALL_FAST_MODEL": "small-model",
                ANTHROPIC_CUSTOM_HEADERS_ENV: "X-Api-Key: secret\nX-Title: café",
                LLM_HTTP_HEADERS_ENV: json.dumps(headers, ensure_ascii=False),
            },
        )
        self.assertEqual(json.loads(overrides[LLM_HTTP_HEADERS_ENV]), headers)
        self.assertIn("café", overrides[LLM_HTTP_HEADERS_ENV])

    def test_claudecode_omits_empty_small_model_and_headers(self) -> None:
        overrides = worker_env_overrides(_worker("claudecode"), _candidate(1, small_fast_model=""))
        self.assertEqual(
            overrides,
            {
                "ANTHROPIC_BASE_URL": "https://api1.example.com/v1",
                "ANTHROPIC_MODEL": "model-1",
                "ANTHROPIC_AUTH_TOKEN": "key-1",
            },
        )

    def test_claudecode_rejects_non_anthropic(self) -> None:
        for protocol in ("openai_responses", "openai_chat", "gemini"):
            with self.subTest(protocol=protocol):
                self.assertIsNone(
                    worker_env_overrides(_worker("claudecode"), _candidate(1, protocol=protocol))
                )

    def test_codex_openai_responses(self) -> None:
        headers = {"X-Trace": "abc"}
        overrides = worker_env_overrides(
            _worker("codex"),
            _candidate(2, protocol="openai_responses", headers=headers),
        )
        self.assertEqual(
            overrides,
            {
                "CODEX_BASE_URL": "https://api2.example.com/v1",
                "CODEX_MODEL": "model-2",
                "OPENAI_API_KEY": "key-2",
                LLM_HTTP_HEADERS_ENV: json.dumps(headers),
            },
        )

    def test_codex_without_headers_has_no_header_env(self) -> None:
        overrides = worker_env_overrides(_worker("codex"), _candidate(2, protocol="openai_responses"))
        self.assertEqual(set(overrides), {"CODEX_BASE_URL", "CODEX_MODEL", "OPENAI_API_KEY"})

    def test_codex_rejects_other_protocols(self) -> None:
        for protocol in ("anthropic", "openai_chat", "gemini"):
            with self.subTest(protocol=protocol):
                self.assertIsNone(
                    worker_env_overrides(_worker("codex"), _candidate(2, protocol=protocol))
                )

    def test_pi_supports_every_protocol_through_provider_api(self) -> None:
        for protocol in PROTOCOLS:
            with self.subTest(protocol=protocol):
                candidate = _candidate(3, protocol=protocol, headers={"X-Test": protocol})
                overrides = worker_env_overrides(_worker("pi"), candidate)
                self.assertEqual(
                    overrides,
                    {
                        "PI_BASE_URL": "https://api3.example.com/v1",
                        "PI_MODEL": "model-3",
                        "PI_API_KEY": "key-3",
                        "PI_PROVIDER_API": PI_API_BY_PROTOCOL[protocol],
                        LLM_HTTP_HEADERS_ENV: json.dumps({"X-Test": protocol}),
                    },
                )

    def test_pi_without_headers_has_no_header_env(self) -> None:
        overrides = worker_env_overrides(_worker("pi"), _candidate(3, protocol="gemini"))
        self.assertEqual(
            set(overrides),
            {"PI_BASE_URL", "PI_MODEL", "PI_API_KEY", "PI_PROVIDER_API"},
        )
        self.assertEqual(overrides["PI_PROVIDER_API"], "google-generative-ai")

    def test_mock_is_never_overridden(self) -> None:
        self.assertIsNone(worker_env_overrides(_worker("mock"), _candidate(1)))


class ApplyPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = _dispatch_config()
        self.pool = [
            _candidate(1, model="model-1"),
            _candidate(2, protocol="openai_responses", model="model-2"),
            _candidate(3, protocol="gemini", model="model-3"),
        ]

    def _envs(self, result) -> dict[str, dict[str, str]]:
        return {worker.name: worker.env for worker in result.config.workers}

    def test_applies_candidates_per_worker(self) -> None:
        result = apply_pool(self.base, self.pool, {})
        self.assertEqual(result.applied_workers, ["cc", "cx", "pi"])
        self.assertEqual(result.skipped_workers, ["mock"])
        self.assertEqual(result.applied_config_ids, {"cc": 1, "cx": 2, "pi": 1})
        envs = self._envs(result)
        self.assertEqual(envs["cc"]["ANTHROPIC_MODEL"], "model-1")
        self.assertEqual(envs["cc"]["ANTHROPIC_BASE_URL"], "https://api1.example.com/v1")
        self.assertEqual(envs["cc"]["ANTHROPIC_AUTH_TOKEN"], "key-1")
        self.assertEqual(envs["cx"]["CODEX_MODEL"], "model-2")
        self.assertEqual(envs["cx"]["OPENAI_API_KEY"], "key-2")
        self.assertEqual(envs["pi"]["PI_MODEL"], "model-1")
        self.assertEqual(envs["pi"]["PI_PROVIDER_API"], "anthropic-messages")
        self.assertEqual(envs["mock"], {})

    def test_base_config_is_not_mutated_and_in_flight_workers_keep_their_env(self) -> None:
        base_cc = self.base.workers[0]
        result = apply_pool(self.base, self.pool, {})
        self.assertIsNot(result.config, self.base)
        self.assertEqual(base_cc.env["ANTHROPIC_MODEL"], "yaml-model")
        self.assertEqual(base_cc.env["ANTHROPIC_BASE_URL"], "https://yaml.example.com/anthropic")
        self.assertEqual(base_cc.env["ANTHROPIC_AUTH_TOKEN"], "yaml-key")
        result_cc = result.config.workers[0]
        self.assertIsNot(result_cc, base_cc)
        self.assertEqual(result_cc.env["ANTHROPIC_MODEL"], "model-1")
        # In-flight tasks hold the base object; it must still expose the old env.
        self.assertEqual(base_cc.env["ANTHROPIC_MODEL"], "yaml-model")

    def test_active_ids_are_preferred(self) -> None:
        result = apply_pool(self.base, self.pool, {"pi": 3})
        self.assertEqual(result.applied_config_ids["pi"], 3)
        envs = self._envs(result)
        self.assertEqual(envs["pi"]["PI_MODEL"], "model-3")
        self.assertEqual(envs["pi"]["PI_PROVIDER_API"], "google-generative-ai")

    def test_incompatible_active_id_falls_back_to_first_compatible(self) -> None:
        result = apply_pool(self.base, self.pool, {"cc": 3})  # gemini is not claudecode-compatible
        self.assertEqual(result.applied_config_ids["cc"], 1)

    def test_unknown_active_id_falls_back(self) -> None:
        result = apply_pool(self.base, self.pool, {"cc": 99})
        self.assertEqual(result.applied_config_ids["cc"], 1)

    def test_empty_pool_skips_every_worker(self) -> None:
        result = apply_pool(self.base, [], {})
        self.assertEqual(result.applied_workers, [])
        self.assertEqual(result.skipped_workers, ["cc", "cx", "pi", "mock"])
        self.assertEqual(result.applied_config_ids, {})
        envs = self._envs(result)
        self.assertEqual(envs["cc"]["ANTHROPIC_MODEL"], "yaml-model")
        self.assertEqual(envs["cx"]["CODEX_MODEL"], "yaml-model")
        self.assertEqual(envs["pi"]["PI_MODEL"], "yaml-model")

    def test_pool_without_matching_protocol_skips_worker(self) -> None:
        result = apply_pool(self.base, [_candidate(2, protocol="openai_responses")], {})
        self.assertEqual(result.applied_workers, ["cx", "pi"])
        self.assertEqual(result.skipped_workers, ["cc", "mock"])
        self.assertNotIn("cc", result.applied_config_ids)

    def test_extra_env_entries_survive_the_override(self) -> None:
        base = _dispatch_config(extra_env={"cc": {"CAIRN_TRACE": "keep"}})
        result = apply_pool(base, self.pool, {})
        env = self._envs(result)["cc"]
        self.assertEqual(env["CAIRN_TRACE"], "keep")
        self.assertEqual(env["ANTHROPIC_MODEL"], "model-1")


class PoolFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = [_candidate(1, headers={"X-A": "1"}), _candidate(2, protocol="openai_responses")]

    def test_equivalent_pools_have_equal_fingerprints(self) -> None:
        self.assertEqual(pool_fingerprint(self.pool), pool_fingerprint(list(self.pool)))

    def test_header_order_is_agnostic(self) -> None:
        left = [_candidate(1, headers={"X-A": "1", "X-B": "2"})]
        right = [_candidate(1, headers={"X-B": "2", "X-A": "1"})]
        self.assertEqual(pool_fingerprint(left), pool_fingerprint(right))

    def test_sensitive_to_every_meaningful_field(self) -> None:
        baseline = pool_fingerprint(self.pool)
        changes = {
            "config_id": 9,
            "protocol": "gemini",
            "base_url": "https://other.example.com/v1",
            "model": "other-model",
            "small_fast_model": "other-small",
            "api_key": "other-key",
            "headers": {"X-A": "2"},
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = [replace(self.pool[0], **{field: value}), self.pool[1]]
                self.assertNotEqual(pool_fingerprint(changed), baseline)

    def test_sensitive_to_candidate_order(self) -> None:
        reordered = [self.pool[1], self.pool[0]]
        self.assertNotEqual(pool_fingerprint(reordered), pool_fingerprint(self.pool))

    def test_empty_pool_fingerprint(self) -> None:
        self.assertEqual(pool_fingerprint([]), ())


class AdapterHeaderTests(unittest.TestCase):
    def test_claudecode_healthcheck_includes_custom_headers(self) -> None:
        worker = _worker(
            "claudecode",
            env_extra={LLM_HTTP_HEADERS_ENV: json.dumps({"X-Debug": "secret-value"})},
        )
        script = ClaudeCodeDriver().build_healthcheck(worker)[-1]
        self.assertIn("X-Debug: secret-value", script)
        self.assertIn("-H", script)

    def test_claudecode_healthcheck_skips_builtin_headers(self) -> None:
        headers = {
            "Authorization": "custom",
            "anthropic-version": "9",
            "Content-Type": "text/plain",
            "X-Keep": "yes",
        }
        worker = _worker("claudecode", env_extra={LLM_HTTP_HEADERS_ENV: json.dumps(headers)})
        script = ClaudeCodeDriver().build_healthcheck(worker)[-1]
        self.assertIn("X-Keep: yes", script)
        self.assertNotIn("custom", script)
        self.assertNotIn("text/plain", script)

    def test_claudecode_describe_masks_custom_header_values(self) -> None:
        worker = _worker(
            "claudecode",
            env_extra={LLM_HTTP_HEADERS_ENV: json.dumps({"X-Debug": "super-secret"})},
        )
        description = ClaudeCodeDriver().describe_startup_healthcheck(worker)
        self.assertIn("X-Debug: ***", description)
        self.assertNotIn("super-secret", description)
        self.assertIn("$ANTHROPIC_AUTH_TOKEN", description)

    def test_claudecode_describe_flags_invalid_header_json(self) -> None:
        worker = _worker("claudecode", env_extra={LLM_HTTP_HEADERS_ENV: "not-json"})
        description = ClaudeCodeDriver().describe_startup_healthcheck(worker)
        self.assertIn("invalid CAIRN_LLM_HTTP_HEADERS ignored", description)

    def test_codex_execute_appends_toml_headers_before_separator(self) -> None:
        worker = _worker("codex", env_extra={LLM_HTTP_HEADERS_ENV: json.dumps({"X-Debug": "v"})})
        argv = CodexDriver().build_execute(worker, "hi", None).argv
        self.assertEqual(
            argv[-4:],
            ["-c", 'model_providers.cairn.http_headers={ "X-Debug" = "v" }', "--", "hi"],
        )

    def test_codex_conclude_appends_toml_headers_before_separator(self) -> None:
        worker = _worker("codex", env_extra={LLM_HTTP_HEADERS_ENV: json.dumps({"X-Debug": "v"})})
        argv = CodexDriver().build_conclude(worker, "hi", "session-1")
        self.assertEqual(
            argv[-4:],
            ["-c", 'model_providers.cairn.http_headers={ "X-Debug" = "v" }', "--", "hi"],
        )

    def test_codex_without_headers_has_no_http_headers_argument(self) -> None:
        argv = CodexDriver().build_execute(_worker("codex"), "hi", None).argv
        self.assertFalse(any("http_headers" in part for part in argv))

    def test_codex_healthcheck_skips_builtin_headers(self) -> None:
        headers = {"authorization": "x", "content-type": "y", "X-Keep": "yes"}
        worker = _worker("codex", env_extra={LLM_HTTP_HEADERS_ENV: json.dumps(headers)})
        script = CodexDriver().build_healthcheck(worker)[-1]
        self.assertIn("X-Keep: yes", script)
        self.assertNotIn("authorization: x", script)

    def test_codex_describe_masks_custom_header_values(self) -> None:
        worker = _worker("codex", env_extra={LLM_HTTP_HEADERS_ENV: json.dumps({"X-Debug": "secret"})})
        description = CodexDriver().describe_startup_healthcheck(worker)
        self.assertIn("X-Debug: ***", description)
        self.assertNotIn("secret", description)

    def test_pi_models_json_script_reads_headers_env(self) -> None:
        worker = _worker("pi", env_extra={LLM_HTTP_HEADERS_ENV: json.dumps({"X-Debug": "v"})})
        argv = PiDriver().build_execute(worker, "hi", None).argv
        script = argv[2]
        self.assertIn(LLM_HTTP_HEADERS_ENV, script)
        self.assertIn('provider["headers"] = headers', script)
        self.assertNotIn("X-Debug", script)


class _LoopTestCase(unittest.TestCase):
    """Shared harness: a DispatcherLoop built from a temp dispatch.yaml.

    The runtime executor is mocked so tests never create containers; the HTTP
    client is replaced per test so no server is required.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        config_path = Path(self.tmpdir.name) / "dispatch.yaml"
        config_path.write_text(yaml.safe_dump(RAW_CONFIG), encoding="utf-8")
        executor_patcher = mock.patch("cairn.dispatcher.scheduler.loop.create_executor")
        executor_patcher.start()
        self.addCleanup(executor_patcher.stop)
        self.loop = DispatcherLoop(config_path)
        self.addCleanup(self.loop.close)
        self.loop.client = mock.Mock()

    def _install_pool(self, pool):
        result = apply_pool(self.loop.base_config, pool, {})
        self.loop._llm_pool = list(pool)
        self.loop._llm_pool_fingerprint = pool_fingerprint(pool)
        self.loop._worker_active_config = dict(result.applied_config_ids)
        self.loop.config = result.config
        return result

    def _finish(
        self,
        outcome: str,
        *,
        worker_name: str = "cc",
        task_type: str = "bootstrap",
        llm_config_id: int | None = None,
    ) -> None:
        future: Future[str] = Future()
        future.set_result(outcome)
        self.loop.futures[future] = RunningTask(
            project_id="project-1",
            task_type=task_type,
            worker_name=worker_name,
            cancellation=TaskCancellation(),
            llm_config_id=llm_config_id,
        )
        self.loop._reap_futures()

    def _worker_env(self, name: str) -> dict[str, str]:
        return next(worker.env for worker in self.loop.config.workers if worker.name == name)

    def _patch_healthchecks(self, fake) -> None:
        patcher = mock.patch(
            "cairn.dispatcher.scheduler.loop.run_startup_healthchecks",
            side_effect=fake,
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class LoopRefreshTests(_LoopTestCase):
    def test_refresh_applies_pool_and_restores_yaml_when_pool_is_empty(self) -> None:
        payloads = [
            _payload(1),
            _payload(2, protocol="openai_responses"),
            _payload(3, protocol="gemini"),
        ]
        self.loop.client.get_llm_configs.return_value = payloads
        self.loop._refresh_llm_config()
        self.loop.client.get_llm_configs.assert_called_with(timeout=5.0)
        self.assertEqual([candidate.config_id for candidate in self.loop._llm_pool], [1, 2, 3])
        self.assertEqual(self.loop._llm_pool_fingerprint, pool_fingerprint(self.loop._llm_pool))
        self.assertEqual(self.loop._worker_active_config, {"cc": 1, "cx": 2, "pi": 1})
        self.assertEqual(self._worker_env("cc")["ANTHROPIC_MODEL"], "model-1")
        self.assertEqual(self._worker_env("cx")["CODEX_MODEL"], "model-2")
        self.assertEqual(self._worker_env("pi")["PI_MODEL"], "model-1")
        self.assertEqual(self._worker_env("pi")["PI_PROVIDER_API"], "anthropic-messages")
        self.assertEqual(self._worker_env("mock"), {})
        # The pristine YAML config must never be touched.
        self.assertEqual(self.loop.base_config.workers[0].env["ANTHROPIC_MODEL"], "yaml-model")

        applied_config = self.loop.config
        self.loop._refresh_llm_config()
        self.assertIs(self.loop.config, applied_config)  # same fingerprint: no rebuild

        self.loop.client.get_llm_configs.return_value = []
        self.loop._refresh_llm_config()
        self.assertEqual(self.loop._llm_pool, [])
        self.assertEqual(self.loop._worker_active_config, {})
        self.assertEqual(self._worker_env("cc")["ANTHROPIC_MODEL"], "yaml-model")
        self.assertEqual(self._worker_env("pi")["PI_MODEL"], "yaml-model")

    def test_refresh_keeps_previous_config_on_http_error(self) -> None:
        self.loop.client.get_llm_configs.side_effect = requests.RequestException("boom")
        previous = self.loop.config
        self.loop._refresh_llm_config()
        self.assertIs(self.loop.config, previous)
        self.assertEqual(self.loop._llm_pool, [])

    def test_refresh_respects_cooldown_when_choosing_active_config(self) -> None:
        self.loop.client.get_llm_configs.return_value = [_payload(1), _payload(2)]
        self.loop._refresh_llm_config()
        self.assertEqual(self.loop._worker_active_config["cc"], 1)
        self.loop._config_failures = {1: 1}
        self.loop._config_cooldown_until = {1: time.monotonic() + 60}
        self.loop.client.get_llm_configs.return_value = [
            _payload(1, api_key="rotated"),
            _payload(2),
        ]
        self.loop._refresh_llm_config()
        self.assertEqual(self.loop._worker_active_config["cc"], 2)
        self.assertEqual(self._worker_env("cc")["ANTHROPIC_MODEL"], "model-2")

    def test_refresh_drops_state_for_configs_no_longer_in_pool(self) -> None:
        self.loop.client.get_llm_configs.return_value = [_payload(1)]
        self.loop._refresh_llm_config()
        self.loop._config_failures = {1: 1, 99: 2}
        self.loop._config_cooldown_until = {
            1: time.monotonic() + 30,
            99: time.monotonic() + 30,
        }
        self.loop.client.get_llm_configs.return_value = [_payload(1, api_key="rotated")]
        self.loop._refresh_llm_config()
        self.assertNotIn(99, self.loop._config_failures)
        self.assertNotIn(99, self.loop._config_cooldown_until)
        self.assertIn(1, self.loop._config_failures)


class LoopRotationTests(_LoopTestCase):
    def test_unhealthy_task_rotates_worker_to_next_compatible_config(self) -> None:
        pool = [_candidate(1), _candidate(2), _candidate(9, protocol="openai_responses")]
        self._install_pool(pool)
        self.assertEqual(self.loop._worker_active_config["cc"], 1)
        self._finish("unhealthy", worker_name="cc", llm_config_id=1)
        self.assertEqual(self.loop._worker_active_config["cc"], 2)
        self.assertEqual(self._worker_env("cc")["ANTHROPIC_MODEL"], "model-2")
        self.assertEqual(self.loop._config_failures[1], 1)
        self.assertGreater(self.loop._config_cooldown_until[1], time.monotonic())
        self.assertNotIn("cc", self.loop.worker_unhealthy_until)
        self.assertEqual(self.loop.futures, {})

    def test_failure_without_alternate_falls_back_to_worker_retry(self) -> None:
        self._install_pool([_candidate(1)])
        self._finish("unhealthy", worker_name="cc", llm_config_id=1)
        self.assertEqual(self.loop._worker_active_config["cc"], 1)
        self.assertIn("cc", self.loop.worker_unhealthy_until)
        self.assertEqual(self.loop._config_failures[1], 1)

    def test_unhealthy_task_without_config_id_keeps_old_retry(self) -> None:
        self._install_pool([_candidate(1), _candidate(2)])
        self._finish("unhealthy", worker_name="cc", llm_config_id=None)
        self.assertIn("cc", self.loop.worker_unhealthy_until)
        self.assertEqual(self.loop._config_failures, {})

    def test_success_clears_config_failures_and_cooldown(self) -> None:
        self._install_pool([_candidate(1), _candidate(2)])
        self.loop._config_failures = {1: 2}
        self.loop._config_cooldown_until = {1: time.monotonic() + 30}
        self._finish("success", worker_name="cc", llm_config_id=1)
        self.assertEqual(self.loop._config_failures, {})
        self.assertEqual(self.loop._config_cooldown_until, {})

    def test_record_config_failure_uses_exponential_backoff(self) -> None:
        self.assertEqual(self.loop._record_config_failure(5), 30.0)
        self.assertEqual(self.loop._record_config_failure(5), 60.0)
        self.assertEqual(self.loop._record_config_failure(5), 120.0)
        self.assertEqual(self.loop._record_config_failure(5), 240.0)
        self.assertEqual(self.loop._record_config_failure(5), 300.0)
        self.assertEqual(self.loop._record_config_failure(5), 300.0)


class LoopStartupHealthcheckTests(_LoopTestCase):
    def test_retries_per_candidate_and_starts_when_workers_pass(self) -> None:
        pool = [
            _candidate(1),
            _candidate(2, protocol="openai_responses"),
            _candidate(3),
            _candidate(4, protocol="openai_responses"),
            _candidate(5, protocol="openai_chat"),
        ]
        self._install_pool(pool)
        calls: list[list[str]] = []

        def fake(config, container_manager, *, show_commands=False, workers=None):
            names = [worker.name for worker in workers]
            calls.append(names)
            round_number = len(calls)
            return [_startup_result(name, ok=round_number == 2) for name in names]

        self._patch_healthchecks(fake)
        self.loop._run_startup_healthchecks(show_commands=False)
        self.assertEqual(calls, [["cc", "cx", "pi", "mock"], ["cc", "cx", "pi"]])
        self.assertTrue(self.loop._startup_healthchecks_checked)
        self.assertEqual(self.loop._worker_active_config["cc"], 3)
        self.assertEqual(self.loop._worker_active_config["cx"], 4)
        self.assertEqual(self.loop._worker_active_config["pi"], 3)
        self.assertEqual(self._worker_env("cc")["ANTHROPIC_MODEL"], "model-3")
        self.assertEqual(self._worker_env("cx")["CODEX_MODEL"], "model-4")
        self.assertEqual(self._worker_env("pi")["PI_MODEL"], "model-3")
        self.assertIn("mock", self.loop.worker_unhealthy_until)
        self.assertEqual(self.loop._config_failures, {1: 2, 2: 1})

    def test_raises_when_every_worker_and_candidate_fails(self) -> None:
        self._install_pool([_candidate(1)])

        def fake(config, container_manager, *, show_commands=False, workers=None):
            return [_startup_result(worker.name, ok=False) for worker in workers]

        self._patch_healthchecks(fake)
        with self.assertRaises(RuntimeError) as context:
            self.loop._run_startup_healthchecks(show_commands=False)
        self.assertIn("startup healthchecks failed", str(context.exception))
        for name in ("cc", "cx", "pi", "mock"):
            self.assertIn(name, self.loop.worker_unhealthy_until)
        self.assertEqual(self.loop._config_failures, {1: 2})

    def test_starts_when_first_round_passes(self) -> None:
        calls: list[list[str]] = []

        def fake(config, container_manager, *, show_commands=False, workers=None):
            names = [worker.name for worker in workers]
            calls.append(names)
            return [_startup_result(name, ok=True) for name in names]

        self._patch_healthchecks(fake)
        self.loop._run_startup_healthchecks(show_commands=False)
        self.assertEqual(calls, [["cc", "cx", "pi", "mock"]])
        self.assertTrue(self.loop._startup_healthchecks_checked)
        self.assertEqual(self.loop._worker_active_config, {})
