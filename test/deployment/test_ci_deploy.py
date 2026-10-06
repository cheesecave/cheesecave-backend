"""Offline checks for the restricted backend SSH trigger. No network or Docker."""

from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_deploy", ROOT / "scripts/ci_deploy.py")
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)
SHA = "a" * 40


class BackendDeployTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.receipt = self.root / "receipt.json"
        self.env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": SHA, "GITHUB_REPOSITORY": "cheesecave/cheesecave-backend",
            "DEPLOY_HOST": "deploy.example.test", "DEPLOY_USER": "cheesecave_deploy",
            "DEPLOY_SSH_KEY": "private-key-fixture", "DEPLOY_KNOWN_HOSTS": "known-host-fixture",
        }

    def run_client(self, response, code=0):
        @contextmanager
        def connection(env):
            yield ["ssh", "test-destination"], {}

        result = subprocess.CompletedProcess([], code, json.dumps(response).encode(), b"private stderr")
        with patch.object(CLIENT, "ssh_connection", connection), patch.object(CLIENT.subprocess, "run", return_value=result) as run:
            output, errors = io.StringIO(), io.StringIO()
            argv = ["ci_deploy.py", "--commit", SHA, "--receipt", str(self.receipt)]
            with patch.dict(os.environ, self.env), patch.object(CLIENT.sys, "argv", argv), patch("sys.stdout", output), patch("sys.stderr", errors):
                exit_code = CLIENT.main()
        self.assertEqual(run.call_args.args[0][-1], "deploy " + SHA)
        return exit_code, output.getvalue() + errors.getvalue(), json.loads(self.receipt.read_text())

    def test_manual_trigger_uses_only_commit_and_records_success(self):
        code, log, receipt = self.run_client({"ok": True, "component": "backend", "commit": SHA,
                                            "build": {"private": "ignored detail"}, "release": "/private/path"})
        self.assertEqual(code, 0)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["commit"], SHA)
        self.assertNotIn("ignored detail", log + json.dumps(receipt))
        self.assertNotIn("/private/path", log + json.dumps(receipt))

    def test_safe_server_error_and_stage_are_visible_in_log_and_receipt(self):
        code, log, receipt = self.run_client({"ok": False, "error": "build_failed", "stage": "build"}, 2)
        self.assertEqual(code, 1)
        self.assertIn("error=build_failed stage=build", log)
        self.assertEqual(receipt["error"], "build_failed")
        self.assertEqual(receipt["stage"], "build")
        self.assertFalse(receipt["ok"])

    def test_untrusted_server_strings_are_not_logged_or_saved(self):
        for malicious in ("https://secret.example/token", "error\nPRIVATE_KEY=secret", "x" * 65,
                          {"secret": "value"}, ["private"], "::error::injected"):
            with self.subTest(value=malicious):
                code, log, receipt = self.run_client({"ok": False, "error": malicious, "stage": malicious,
                                                    "details": "private-key-fixture"}, 2)
                self.assertEqual(code, 1)
                self.assertEqual(receipt["error"], "remote_failure")
                self.assertEqual(receipt["stage"], "unknown")
                self.assertNotIn("private-key-fixture", log + json.dumps(receipt))
                self.assertNotIn("private stderr", log + json.dumps(receipt))
                if isinstance(malicious, str):
                    self.assertNotIn(malicious, log + json.dumps(receipt))

    def test_success_must_identify_backend_and_requested_commit(self):
        for field, value in (("component", "web"), ("commit", "b" * 40)):
            response = {"ok": True, "component": "backend", "commit": SHA, field: value}
            code, log, receipt = self.run_client(response)
            self.assertEqual(code, 1)
            self.assertEqual(receipt["error"], "server_identity_mismatch")

    def test_only_exact_manual_main_identity_reaches_ssh(self):
        for field, value in (("GITHUB_EVENT_NAME", "push"), ("GITHUB_REF", "refs/heads/feature"),
                             ("GITHUB_SHA", "b" * 40), ("GITHUB_REPOSITORY", "fork/backend")):
            with self.subTest(field=field), patch.object(CLIENT, "ssh_connection") as connect:
                with self.assertRaises(CLIENT.DeploymentError):
                    CLIENT.deploy(SHA, self.receipt, {**self.env, field: value})
                connect.assert_not_called()
                self.assertFalse(json.loads(self.receipt.read_text())["ok"])

    def test_commit_injection_is_rejected_before_ssh_and_redacted_in_receipt(self):
        for value in (SHA + ";id", "$(id)", "A" * 40, "a" * 39):
            with self.subTest(value=value), patch.object(CLIENT, "ssh_connection") as connect:
                with self.assertRaises(CLIENT.DeploymentError):
                    CLIENT.deploy(value, self.receipt, self.env)
                connect.assert_not_called()
                self.assertNotIn("commit", json.loads(self.receipt.read_text()))

    def test_connection_rejects_host_user_and_port_injection(self):
        for field, value in (("DEPLOY_HOST", "-oProxyCommand=id"), ("DEPLOY_HOST", "host;id"),
                             ("DEPLOY_PORT", "22 -L 80"), ("DEPLOY_PORT", "65536"), ("DEPLOY_USER", "root")):
            with self.subTest(field=field), self.assertRaises(CLIENT.DeploymentError):
                CLIENT.connection_settings({**self.env, field: value})

    def test_private_key_is_pinned_temporary_and_not_a_process_argument(self):
        keys = []

        def check(argv, **kwargs):
            self.assertNotIn("private-key-fixture", str(argv))
            self.assertFalse(any(name.startswith("DEPLOY_") for name in kwargs["env"]))
            if "-y" in argv:
                key = Path(argv[-1])
                self.assertEqual(key.read_text(), "private-key-fixture\n")
                if os.name == "posix":
                    self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                keys.append(key)
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(CLIENT.subprocess, "run", check):
            with CLIENT.ssh_connection(self.env) as (argv, env):
                for option in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "IdentityAgent=none"):
                    self.assertIn(option, argv)
        self.assertEqual(len(keys), 1)
        self.assertFalse(keys[0].exists())


if __name__ == "__main__":
    unittest.main()
