from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from cairn.dispatcher.config import DispatchConfig


DISPATCH = """\
server: "http://127.0.0.1:8000"
api_token: ""
runtime:
  executor: local
  interval: 3
  max_workers: 1
  max_running_projects: 1
  max_project_workers: 1
  healthcheck_timeout: 2
  prompt_group: "mock"
tasks:
  bootstrap:
    timeout: 9
    conclude_timeout: 5
  reason:
    timeout: 5
    max_intents: 3
  explore:
    timeout: 9
    conclude_timeout: 5
container:
  image: "cairn-worker:arm64"
workers:
  - name: "claude-worker"
    type: "claudecode"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 0
    env:
      ANTHROPIC_MODEL: "deepseek-v4-flash"
      ANTHROPIC_BASE_URL: "https://example.invalid"
      ANTHROPIC_AUTH_TOKEN: ""
"""


class WorkerEnvFromProcessTests(unittest.TestCase):
    def test_empty_yaml_token_is_filled_from_environ(self) -> None:
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-from-env-not-yaml"
        os.environ["CAIRN_API_TOKEN"] = "cairn_test_token"
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "dispatch.yaml"
            path.write_text(DISPATCH, encoding="utf-8")
            config = DispatchConfig.load(path)
        self.assertEqual(config.workers[0].env["ANTHROPIC_AUTH_TOKEN"], "sk-from-env-not-yaml")
        self.assertNotIn("sk-from-env-not-yaml", DISPATCH)

    def test_dollar_brace_ref_is_expanded(self) -> None:
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-expanded"
        os.environ["CAIRN_API_TOKEN"] = "cairn_test_token"
        text = DISPATCH.replace('ANTHROPIC_AUTH_TOKEN: ""', 'ANTHROPIC_AUTH_TOKEN: "${ANTHROPIC_AUTH_TOKEN}"')
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "dispatch.yaml"
            path.write_text(text, encoding="utf-8")
            config = DispatchConfig.load(path)
        self.assertEqual(config.workers[0].env["ANTHROPIC_AUTH_TOKEN"], "sk-expanded")
