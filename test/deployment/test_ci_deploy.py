"""Offline deployment boundary tests. Never contact SSH or Docker."""

from contextlib import contextmanager
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_deploy", ROOT / "scripts/ci_deploy.py")
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)
SHA = "a" * 40


class DeploymentBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "artifact.tar.gz"
        with gzip.open(self.archive, "wb") as stream:
            stream.write(b"test artifact")
        self.size = self.archive.stat().st_size
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.env = {
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": SHA, "GITHUB_REPOSITORY": "cheesecave/cheesecave-web",
            "DEPLOY_HOST": "deploy.example.test", "DEPLOY_USER": "cheesecave_deploy",
            "DEPLOY_SSH_KEY": "private-key-fixture", "DEPLOY_KNOWN_HOSTS": "known-host-fixture",
        }
        self.receipt = self.root / "receipt.json"

    def success(self):
        return 0, {"ok": True, "component": "web", "commit": SHA,
                   "sha256": self.digest, "bytes": self.size}

    def invoke(self, responses, *, env=None):
        calls = []

        @contextmanager
        def connection(environment):
            yield object()

        def remote(connection, operation, commit=None, digest=None, artifact=None, size=None):
            calls.append(operation)
            if operation == "upload":
                self.assertEqual(artifact.read(), self.archive.read_bytes())
                self.assertEqual(size, self.size)
            if not responses:
                self.fail("Unexpected remote operation: " + operation)
            return responses.pop(0)

        with patch.object(CLIENT, "ssh_connection", connection), patch.object(CLIENT, "remote_call", remote), patch("sys.stdout", new_callable=io.StringIO):
            CLIENT.deploy("web", SHA, self.archive, self.receipt, env or self.env)
        self.assertEqual(responses, [])
        return calls

    def test_manual_deploy_uploads_verifies_then_publishes(self):
        responses = [(0, {"ok": True, "component": "web", "publishing_enabled": True}),
                     (3, {"ok": False, "component": "web", "error": "artifact_not_found"}),
                     self.success(), self.success(), self.success()]
        self.assertEqual(self.invoke(responses), ["status", "verify", "upload", "verify", "publish"])
        self.assertTrue(json.loads(self.receipt.read_text())["published"])

    def test_verified_archive_is_reused_before_publish(self):
        responses = [(0, {"ok": True, "component": "web", "publishing_enabled": True}),
                     self.success(), self.success()]
        self.assertEqual(self.invoke(responses), ["status", "verify", "publish"])
        receipt = self.receipt.read_text()
        self.assertTrue(json.loads(receipt)["published"])
        self.assertNotIn("private-key-fixture", receipt)
        self.assertNotIn("deploy.example.test", receipt)

    def test_existing_corrupt_or_incomplete_archive_is_not_overwritten(self):
        for error in ("artifact_incomplete", "artifact_digest_mismatch", "permission_denied"):
            responses = [(0, {"ok": True, "component": "web", "publishing_enabled": True}),
                         (2, {"ok": False, "component": "web", "error": error})]
            with self.subTest(error=error), self.assertRaises(CLIENT.DeploymentError):
                self.invoke(responses)
            self.assertEqual(responses, [])

    def test_exit_three_without_exact_missing_receipt_cannot_upload(self):
        responses = [(0, {"ok": True, "component": "web", "publishing_enabled": True}),
                     (3, {"ok": False, "component": "admin", "error": "artifact_not_found"})]
        with self.assertRaises(CLIENT.DeploymentError):
            self.invoke(responses)

    def test_verify_must_bind_component_commit_digest_and_size(self):
        for field, value in (("component", "admin"), ("commit", "b" * 40),
                             ("sha256", "c" * 64), ("bytes", self.size + 1), ("ok", "true")):
            response = self.success()[1]
            response[field] = value
            with self.subTest(field=field), self.assertRaises(CLIENT.DeploymentError):
                self.invoke([(0, {"ok": True, "component": "web", "publishing_enabled": True}), (0, response)])

    def test_disabled_server_never_receives_publish(self):
        with self.assertRaises(CLIENT.DeploymentError):
            self.invoke([(0, {"ok": True, "component": "web", "publishing_enabled": False})])

    def test_only_exact_manual_main_workflow_identity_can_deploy(self):
        for field, value in (("GITHUB_EVENT_NAME", "push"), ("GITHUB_REF", "refs/heads/feature"),
                             ("GITHUB_REF", "refs/tags/main"), ("GITHUB_SHA", "b" * 40),
                             ("GITHUB_REPOSITORY", "fork/cheesecave-web")):
            with self.subTest(field=field, value=value), patch.object(CLIENT, "ssh_connection") as connect:
                with self.assertRaises(CLIENT.DeploymentError):
                    CLIENT.deploy("web", SHA, self.archive, self.receipt, {**self.env, field: value})
                connect.assert_not_called()

    def test_archive_size_limit_and_gzip_are_checked_before_connecting(self):
        for size_limit in (0, self.size - 1):
            with patch.dict(CLIENT.LIMITS, web=size_limit), patch.object(CLIENT, "ssh_connection") as connect:
                with self.assertRaises(CLIENT.DeploymentError):
                    CLIENT.deploy("web", SHA, self.archive, self.receipt, self.env)
                connect.assert_not_called()
        self.archive.write_bytes(b"not compressed")
        with patch.object(CLIENT, "ssh_connection") as connect:
            with self.assertRaises(CLIENT.DeploymentError):
                CLIENT.deploy("web", SHA, self.archive, self.receipt, self.env)
            connect.assert_not_called()

    def test_remote_grammar_rejects_shell_injection_before_process(self):
        with patch.object(CLIENT.subprocess, "run") as run:
            for commit in (SHA + ";id", "$(id)", "a" * 39, "A" * 40):
                with self.subTest(commit=commit), self.assertRaises(CLIENT.DeploymentError):
                    CLIENT.remote_call(([], {}), "verify", commit, "a" * 64)
            for operation in ("bash", "verify;id", "scp -t /tmp"):
                with self.assertRaises(CLIENT.DeploymentError):
                    CLIENT.remote_call(([], {}), operation, SHA, "a" * 64)
            run.assert_not_called()

    def test_connection_rejects_host_and_port_option_injection(self):
        for field, value in (("DEPLOY_HOST", "-oProxyCommand=id"), ("DEPLOY_HOST", "a;id"),
                             ("DEPLOY_HOST", "a\nHost *"), ("DEPLOY_PORT", "22 -L 80"),
                             ("DEPLOY_PORT", "65536"), ("DEPLOY_USER", "root")):
            with self.subTest(field=field), self.assertRaises(CLIENT.DeploymentError):
                CLIENT.connection_settings({**self.env, field: value})

    def test_private_key_is_temporary_and_never_a_process_argument(self):
        observed = []

        def key_check(argv, **kwargs):
            self.assertNotIn("private-key-fixture", str(argv))
            self.assertFalse(any(key.startswith("DEPLOY_") for key in kwargs["env"]))
            if "-y" in argv:
                key = Path(argv[-1])
                self.assertEqual(key.read_text(), "private-key-fixture\n")
                if os.name == "posix":
                    self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                observed.append(key)
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(CLIENT.subprocess, "run", key_check):
            with CLIENT.ssh_connection(self.env) as (argv, env):
                for option in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "IdentityAgent=none"):
                    self.assertIn(option, argv)
                self.assertNotIn("private-key-fixture", str(argv))
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())

    def static_source(self):
        for name, value in {
            "dist/index.html": "<script src='/assets/app.js'></script>", "dist/assets/app.js": "test",
            "LICENSE": "license", "LICENSING.md": "licensing", "NOTICE.md": "notice",
            "provenance/README.md": "upstream", "src/components/DatasetViewer/LICENSE": "viewer",
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        return self.root

    def test_static_archive_has_release_identity_and_all_legal_notices(self):
        source = self.static_source()
        target = self.root / "static.tar.gz"
        CLIENT.package_static("web", SHA, source, target)
        with tarfile.open(target, "r:gz") as archive:
            names = archive.getnames()
            self.assertIn("index.html", names)
            self.assertNotIn("dist/index.html", names)
            for name in ("legal/LICENSE", "legal/LICENSING.md", "legal/NOTICE.md",
                         "legal/provenance/README.md", "legal/DatasetViewer.LICENSE"):
                self.assertIn(name, names)
            metadata = json.load(archive.extractfile("provenance.json"))
            self.assertEqual(metadata["git_sha"], SHA)
            self.assertEqual(metadata["component"], "web")
            self.assertEqual(metadata["repository"], "cheesecave/cheesecave-web")
            self.assertTrue(all(member.isfile() for member in archive.getmembers()))

    def test_conflicting_metadata_and_missing_legal_notice_are_rejected(self):
        source = self.static_source()
        (source / "dist/provenance.json").write_text("{}")
        with self.assertRaises(CLIENT.DeploymentError):
            CLIENT.package_static("admin", SHA, source, self.root / "static.tar.gz")
        (source / "dist/provenance.json").unlink()
        (source / "NOTICE.md").unlink()
        with self.assertRaises(CLIENT.DeploymentError):
            CLIENT.package_static("admin", SHA, source, self.root / "static.tar.gz")

    def test_static_symlink_cannot_escape_archive(self):
        source = self.static_source()
        try:
            (source / "dist/secret").symlink_to(source / "LICENSE")
        except OSError:
            self.skipTest("Creating symlinks is unavailable on this host")
        with self.assertRaises(CLIENT.DeploymentError):
            CLIENT.package_static("web", SHA, source, self.root / "static.tar.gz")


if __name__ == "__main__":
    unittest.main()
