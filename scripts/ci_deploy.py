#!/usr/bin/env python3
"""Trigger a backend build and deployment through the restricted server account."""

import argparse
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

COMMIT = re.compile(r"[0-9a-f]{40}\Z")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def safe_identifier(value, default):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else default


class DeploymentError(Exception):
    def __init__(self, error, stage="client"):
        self.error = safe_identifier(error, "remote_failure")
        self.stage = safe_identifier(stage, "unknown")
        super().__init__("backend deploy failed: error=" + self.error + " stage=" + self.stage)


def connection_settings(env):
    host = env.get("DEPLOY_HOST", "")
    if not host or len(host) > 253 or any(c.isspace() for c in host):
        raise DeploymentError("invalid_host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise DeploymentError("invalid_host") from None
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in host.split(".")):
            raise DeploymentError("invalid_host")
    user = env.get("DEPLOY_USER", "")
    if user != "cheesecave_deploy":
        raise DeploymentError("invalid_deploy_user")
    port = env.get("DEPLOY_PORT", "") or "22"
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise DeploymentError("invalid_port")
    if not env.get("DEPLOY_SSH_KEY", "").strip() or not env.get("DEPLOY_KNOWN_HOSTS", "").strip():
        raise DeploymentError("missing_ssh_credentials")
    return host, user, str(int(port))


@contextlib.contextmanager
def ssh_connection(env):
    host, user, port = connection_settings(env)
    with tempfile.TemporaryDirectory(prefix="cheesecave-ssh-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        key, known = directory / "key", directory / "known_hosts"
        for path, value in ((key, env["DEPLOY_SSH_KEY"]), (known, env["DEPLOY_KNOWN_HOSTS"])):
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
                file.write(value.replace("\r", "").rstrip() + "\n")
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("DEPLOY_")}
        checks = [
            ["ssh-keygen", "-y", "-P", "", "-f", str(key)],
            ["ssh-keygen", "-F", host if port == "22" else "[" + host + "]:" + port, "-f", str(known)],
        ]
        for command in checks:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=clean_env, timeout=15)
            if result.returncode:
                raise DeploymentError("ssh_key_or_host_pin_invalid")
        argv = ["ssh", "-T", "-F", os.devnull, "-i", str(key), "-p", port]
        for option in (
            "BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none",
            "StrictHostKeyChecking=yes", "UserKnownHostsFile=" + str(known),
            "GlobalKnownHostsFile=" + os.devnull, "ClearAllForwardings=yes",
            "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
            "ConnectTimeout=20", "ServerAliveInterval=15", "ServerAliveCountMax=4",
        ):
            argv.extend(["-o", option])
        argv.append(user + "@" + host)
        yield argv, clean_env


def deploy(commit, receipt_path, env=None):
    env = os.environ if env is None else env
    receipt = {"ok": False, "component": "backend", "operation": "deploy"}
    try:
        if not COMMIT.fullmatch(commit):
            raise DeploymentError("invalid_commit")
        receipt["commit"] = commit
        if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or env.get("GITHUB_REF") != "refs/heads/main":
            raise DeploymentError("manual_main_required")
        if env.get("GITHUB_SHA") != commit or env.get("GITHUB_REPOSITORY") != "cheesecave/cheesecave-backend":
            raise DeploymentError("workflow_identity_mismatch")
        with ssh_connection(env) as (argv, clean_env):
            result = subprocess.run(argv + ["deploy " + commit], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=clean_env, timeout=4800)
        # The server keeps detailed build logs. Never echo arbitrary SSH output.
        try:
            response = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError):
            raise DeploymentError("invalid_server_receipt", "ssh") from None
        if not isinstance(response, dict):
            raise DeploymentError("invalid_server_receipt", "ssh")
        if result.returncode != 0 or response.get("ok") is not True:
            raise DeploymentError(response.get("error"), response.get("stage"))
        if response.get("component") != "backend" or response.get("commit") != commit:
            raise DeploymentError("server_identity_mismatch", "receipt")
        receipt["ok"] = True
    except DeploymentError as error:
        receipt.update(error=error.error, stage=error.stage)
        raise
    except (OSError, subprocess.SubprocessError) as error:
        code = "ssh_timeout" if isinstance(error, subprocess.TimeoutExpired) else "ssh_execution_failed"
        receipt.update(error=code, stage="ssh")
        raise DeploymentError(code, "ssh") from None
    finally:
        Path(receipt_path).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--receipt", default="deployment-receipt.json")
    args = parser.parse_args()
    try:
        deploy(args.commit, args.receipt)
    except DeploymentError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("backend deploy failed: error=receipt_write_failed stage=client", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
