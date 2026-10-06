#!/usr/bin/env python3
"""Build static archives and use CheeseCave's restricted SSH deployment protocol."""

import argparse
import contextlib
from datetime import datetime, timezone
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile


LIMITS = {"web": 128 * 1024**2, "admin": 128 * 1024**2, "backend": 1024**3}
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class DeploymentError(Exception):
    pass


def validate_commit(value):
    if not COMMIT.fullmatch(value):
        raise DeploymentError("A full lowercase 40-character commit is required")
    return value


def static_members(root):
    """Reject links, special files and archive paths that can escape a release."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise DeploymentError("Static source must be a real directory")
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise DeploymentError("Static archives cannot contain links or special files")
        name = path.relative_to(root).as_posix()
        if "\\" in name or any(ord(char) < 32 for char in name):
            raise DeploymentError("Static archive contains an unsafe filename")
        if stat.S_ISREG(mode):
            yield path, name


def package_static(component, commit, source, output):
    validate_commit(commit)
    if component not in ("web", "admin"):
        raise DeploymentError("Only web and admin have static archives")
    source = Path(source).resolve()
    dist = source / "dist"
    members = list(static_members(dist))
    if not (dist / "index.html").is_file() or not (dist / "index.html").stat().st_size:
        raise DeploymentError("Static build has no entry point")
    if (dist / "provenance.json").exists() or (dist / "legal").exists():
        raise DeploymentError("Static build conflicts with reserved release metadata")
    for name in ("LICENSE", "LICENSING.md", "NOTICE.md"):
        path = source / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise DeploymentError("A required license notice is missing")
        members.append((path, "legal/" + name))
    for path, name in static_members(source / "provenance"):
        members.append((path, "legal/provenance/" + name))
    if component == "web":
        path = source / "src/components/DatasetViewer/LICENSE"
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise DeploymentError("DatasetViewer license is missing")
        members.append((path, "legal/DatasetViewer.LICENSE"))
    metadata = json.dumps({
        "component": component, "git_sha": commit,
        "repository": "cheesecave/cheesecave-" + component,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }, sort_keys=True).encode()
    output = Path(output).resolve()
    if output == dist or dist in output.parents:
        raise DeploymentError("Archive output must be outside dist")
    with output.open("xb") as stream, gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|") as archive:
            for path, name in members:
                info = tarfile.TarInfo(name)
                info.size = path.stat().st_size
                info.mode = 0o644
                with path.open("rb") as content:
                    archive.addfile(info, content)
            import io
            info = tarfile.TarInfo("provenance.json")
            info.size, info.mode = len(metadata), 0o644
            archive.addfile(info, io.BytesIO(metadata))
    if output.stat().st_size > LIMITS[component]:
        raise DeploymentError("Static archive exceeds upload size limit")


def connection_settings(env):
    host = env.get("DEPLOY_HOST", "")
    if not host or len(host) > 253 or any(c.isspace() for c in host):
        raise DeploymentError("Invalid DEPLOY_HOST")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise DeploymentError("Invalid DEPLOY_HOST") from None
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in host.split(".")):
            raise DeploymentError("Invalid DEPLOY_HOST")
    user = env.get("DEPLOY_USER", "")
    if user != "cheesecave_deploy":
        raise DeploymentError("DEPLOY_USER must be cheesecave_deploy")
    port = env.get("DEPLOY_PORT", "") or "22"
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise DeploymentError("Invalid DEPLOY_PORT")
    for key in ("DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS"):
        if not env.get(key, "").strip():
            raise DeploymentError("Missing " + key)
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
        # Child SSH processes never inherit the private key in their environment.
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("DEPLOY_")}
        checks = [
            ["ssh-keygen", "-y", "-P", "", "-f", str(key)],
            ["ssh-keygen", "-F", host if port == "22" else "[" + host + "]:" + port, "-f", str(known)],
        ]
        for command in checks:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=clean_env, timeout=15)
            if result.returncode:
                raise DeploymentError("Noninteractive key or pinned host-key validation failed")
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


def remote_call(connection, operation, commit=None, digest=None, artifact=None, size=None):
    if operation == "status":
        command = "status"
    elif operation in ("upload", "verify", "publish"):
        validate_commit(commit or "")
        if not DIGEST.fullmatch(digest or ""):
            raise DeploymentError("Invalid archive digest")
        command = operation + " " + commit + " " + digest
        if operation == "upload":
            if artifact is None or not isinstance(size, int) or size <= 0:
                raise DeploymentError("Upload requires an archive and positive size")
            command += " " + str(size)
    else:
        raise DeploymentError("Unsupported remote operation")
    argv, env = connection
    result = subprocess.run(argv + [command], stdin=artifact or subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, timeout=3600 if operation == "publish" else 1800)
    # Never print SSH stderr, which can include a host, username or credential path.
    try:
        receipt = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError):
        raise DeploymentError("SSH returned no valid deployment receipt") from None
    if not isinstance(receipt, dict):
        raise DeploymentError("Invalid deployment receipt")
    return result.returncode, receipt


def successful(code, receipt, component, commit=None, digest=None, size=None):
    if code != 0 or receipt.get("ok") is not True or receipt.get("component") != component:
        raise DeploymentError("Remote deployment operation failed")
    for name, value in (("commit", commit), ("sha256", digest), ("bytes", size)):
        if value is not None and receipt.get(name) != value:
            raise DeploymentError("Remote receipt does not match the requested artifact")


def deploy(component, commit, archive, receipt_path, env=None):
    env = os.environ if env is None else env
    validate_commit(commit)
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or env.get("GITHUB_REF") != "refs/heads/main":
        raise DeploymentError("Deployment requires a manual workflow run on main")
    if env.get("GITHUB_SHA") != commit or env.get("GITHUB_REPOSITORY") != "cheesecave/cheesecave-" + component:
        raise DeploymentError("Workflow identity does not match deployment identity")
    archive = Path(archive)
    if archive.is_symlink() or not archive.is_file():
        raise DeploymentError("Archive must be a regular local file")
    with archive.open("rb") as content:
        size = os.fstat(content.fileno()).st_size
        if not 0 < size <= LIMITS[component]:
            raise DeploymentError("Archive exceeds component upload limit or is empty")
        if content.read(2) != b"\x1f\x8b":
            raise DeploymentError("Archive must use gzip")
        content.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: content.read(1024**2), b""):
            digest.update(chunk)
        digest = digest.hexdigest()
        with ssh_connection(env) as connection:
            code, status = remote_call(connection, "status")
            successful(code, status, component)
            if status.get("publishing_enabled") is not True:
                raise DeploymentError("Server has not enabled publishing")
            code, verified = remote_call(connection, "verify", commit, digest)
            if code == 3 and verified.get("ok") is False and verified.get("error") == "artifact_not_found" and verified.get("component") == component:
                content.seek(0)
                code, uploaded = remote_call(connection, "upload", commit, digest, content, size)
                successful(code, uploaded, component, commit, digest, size)
                code, verified = remote_call(connection, "verify", commit, digest)
            successful(code, verified, component, commit, digest, size)
            code, published = remote_call(connection, "publish", commit, digest)
            successful(code, published, component, commit, digest)
    # Keep only non-sensitive protocol identity in the CI artifact and console.
    receipt = {"ok": True, "component": component, "commit": commit, "sha256": digest,
               "bytes": size, "verified": True, "published": True}
    Path(receipt_path).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package-static")
    package.add_argument("--component", required=True, choices=("web", "admin"))
    package.add_argument("--commit", required=True)
    package.add_argument("--source", default=".")
    package.add_argument("--output", required=True)
    transfer = commands.add_parser("deploy")
    transfer.add_argument("--component", required=True, choices=tuple(LIMITS))
    transfer.add_argument("--commit", required=True)
    transfer.add_argument("--archive", required=True)
    transfer.add_argument("--receipt", default="deployment-receipt.json")
    args = parser.parse_args()
    try:
        if args.command == "package-static":
            package_static(args.component, args.commit, args.source, args.output)
        else:
            deploy(args.component, args.commit, args.archive, args.receipt)
    except (DeploymentError, OSError, subprocess.SubprocessError) as error:
        # Avoid emitting exception arguments containing SSH argv or file paths.
        print("Deployment error: " + (str(error) if isinstance(error, DeploymentError) else type(error).__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
