from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

USERNAME = "admin"
PASSWORD = "correct-horse-battery"
TOKEN = "cairn_test_token_aaaaaaaaaaaaaaaa"
ORIGIN = "http://testserver"


def _prepare_env(root: Path) -> None:
    os.environ["CAIRN_AUTH_USERNAME"] = USERNAME
    os.environ["CAIRN_AUTH_PASSWORD"] = PASSWORD
    os.environ["CAIRN_API_TOKEN"] = TOKEN
    os.environ["CAIRN_CREDENTIALS_FILE"] = str(root / "credentials")
    os.environ["CAIRN_DB_PATH"] = str(root / "cairn.db")
    os.environ.pop("CAIRN_TRUST_PROXY", None)


class AuthFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        _prepare_env(root)
        from cairn.server import auth, db

        db.reset()
        db.configure(root / "cairn.db")
        auth.bootstrap_auth()
        auth.reset_login_attempts()
        from cairn.server.app import app

        cls.client = TestClient(app)
        cls.auth = auth

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        from cairn.server import db

        db.reset()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.auth.reset_login_attempts()
        self.client.cookies.clear()

    def test_unauthenticated_api_is_rejected(self) -> None:
        response = self.client.get("/projects")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/docs").status_code, 401)
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_index_csp_allows_alpine_eval(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        csp = response.headers["content-security-policy"]
        self.assertIn("'unsafe-eval'", csp)
        self.assertIn("script-src", csp)

    def test_wrong_password_is_rejected(self) -> None:
        response = self.client.post(
            "/auth/login",
            json={"username": USERNAME, "password": "nope"},
        )
        self.assertEqual(response.status_code, 401)

    def test_login_cookie_csrf_and_create_project(self) -> None:
        login = self.client.post(
            "/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        self.assertEqual(login.status_code, 200)
        csrf = login.json()["csrf_token"]
        self.assertTrue(csrf)
        self.assertIn("cairn_session", self.client.cookies)

        me = self.client.get("/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], USERNAME)

        denied = self.client.post(
            "/projects",
            json={"title": "t", "origin": "o", "goal": "g"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(denied.status_code, 403)

        created = self.client.post(
            "/projects",
            json={"title": " pentest ", "origin": "origin fact", "goal": "get flag"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(body["project"]["title"], "pentest")
        ids = {fact["id"] for fact in body["facts"]}
        self.assertEqual(ids, {"origin", "goal"})

        listed = self.client.get("/projects")
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(any(item["id"] == body["project"]["id"] for item in listed.json()))

    def test_bearer_token_skips_csrf(self) -> None:
        response = self.client.get(
            "/projects",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.assertEqual(response.status_code, 200)

        bad = self.client.get(
            "/projects",
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(bad.status_code, 401)

    def test_xff_cannot_bypass_rate_limit_without_trust_proxy(self) -> None:
        for index in range(5):
            response = self.client.post(
                "/auth/login",
                json={"username": USERNAME, "password": "wrong"},
                headers={"X-Forwarded-For": f"203.0.113.{index}"},
            )
            self.assertEqual(response.status_code, 401)
        blocked = self.client.post(
            "/auth/login",
            json={"username": USERNAME, "password": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.9"},
        )
        self.assertEqual(blocked.status_code, 429)


class PasswordChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        _prepare_env(root)
        os.environ.pop("CAIRN_AUTH_PASSWORD_RESET", None)
        from cairn.server import auth, db

        db.reset()
        db.configure(root / "cairn.db")
        auth.bootstrap_auth()
        auth.reset_login_attempts()
        from cairn.server.app import app

        self.auth = auth
        self.db = db
        self.app = app
        self.client = TestClient(app)
        self.credentials = root / "credentials"

    def tearDown(self) -> None:
        self.client.close()
        self.db.reset()
        self._tmp.cleanup()

    def _login(self, password: str = PASSWORD, client=None):
        target = client or self.client
        response = target.post("/auth/login", json={"username": USERNAME, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["csrf_token"]

    def _headers(self, csrf: str) -> dict[str, str]:
        return {"Origin": ORIGIN, "X-CSRF-Token": csrf}

    def test_change_password_requires_session(self) -> None:
        denied = self.client.post(
            "/auth/password",
            json={"current_password": PASSWORD, "new_password": "new-password-ok"},
        )
        self.assertEqual(denied.status_code, 401)

        token = self.client.post(
            "/auth/password",
            json={"current_password": PASSWORD, "new_password": "new-password-ok"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.assertEqual(token.status_code, 403)

    def test_change_password_and_revoke_other_sessions(self) -> None:
        csrf = self._login()
        other = TestClient(self.app)
        self._login(client=other)
        self.assertEqual(other.get("/auth/me").status_code, 200)

        wrong = self.client.post(
            "/auth/password",
            json={"current_password": "nope-nope-nope", "new_password": "new-password-ok"},
            headers=self._headers(csrf),
        )
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(self.client.get("/auth/me").status_code, 200)

        same = self.client.post(
            "/auth/password",
            json={"current_password": PASSWORD, "new_password": PASSWORD},
            headers=self._headers(csrf),
        )
        self.assertEqual(same.status_code, 400)

        short = self.client.post(
            "/auth/password",
            json={"current_password": PASSWORD, "new_password": "short"},
            headers=self._headers(csrf),
        )
        self.assertEqual(short.status_code, 422)

        changed = self.client.post(
            "/auth/password",
            json={"current_password": PASSWORD, "new_password": "new-password-ok"},
            headers=self._headers(csrf),
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(self.client.get("/auth/me").status_code, 200)
        self.assertEqual(other.get("/auth/me").status_code, 401)

        old = self.client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
        self.assertEqual(old.status_code, 401)
        fresh = TestClient(self.app)
        self._login("new-password-ok", client=fresh)
        self.assertEqual(fresh.get("/auth/me").status_code, 200)
        self.assertIn("new-password-ok", self.credentials.read_text())

    def test_ui_password_survives_env_bootstrap(self) -> None:
        csrf = self._login()
        changed = self.client.post(
            "/auth/password",
            json={"current_password": PASSWORD, "new_password": "user-chosen-pass"},
            headers=self._headers(csrf),
        )
        self.assertEqual(changed.status_code, 200, changed.text)

        self.auth.bootstrap_auth()
        self.assertTrue(self.auth.authenticate_password(USERNAME, "user-chosen-pass"))
        self.assertFalse(self.auth.authenticate_password(USERNAME, PASSWORD))

        os.environ["CAIRN_AUTH_PASSWORD_RESET"] = "1"
        try:
            self.auth.bootstrap_auth()
        finally:
            os.environ.pop("CAIRN_AUTH_PASSWORD_RESET", None)
        self.assertTrue(self.auth.authenticate_password(USERNAME, PASSWORD))
        self.assertFalse(self.auth.authenticate_password(USERNAME, "user-chosen-pass"))


class LocalExecutorPathTests(unittest.TestCase):
    def test_write_stays_in_workspace_or_prompt_tmp(self) -> None:
        from cairn.dispatcher.config import ContainerConfig
        from cairn.dispatcher.runtime.local import LocalExecutor

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            executor = LocalExecutor(ContainerConfig(), root=root)
            name = executor.ensure_running("proj_001")
            workspace = root / name
            executor.write_text_file(name, "notes/a.txt", "inside")
            self.assertEqual((workspace / "notes" / "a.txt").read_text(), "inside")

            snapshot = Path("/tmp/cairn-prompts/phase-test/graph.yaml")
            try:
                executor.write_text_file(name, str(snapshot), "yaml")
                self.assertEqual(snapshot.read_text(), "yaml")
            finally:
                if snapshot.exists():
                    snapshot.unlink()
                parent = snapshot.parent
                if parent.exists():
                    parent.rmdir()

            with self.assertRaises(ValueError):
                executor.write_text_file(name, "../escape.txt", "nope")
            with self.assertRaises(ValueError):
                executor.write_text_file(name, "/etc/cairn-escape.txt", "nope")
            self.assertFalse((root.parent / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
