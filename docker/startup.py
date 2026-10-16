#!/usr/bin/env python3
import os
import sys
import time
import httpx
import subprocess
from pathlib import Path

from kohakuhub.envnames import env_or, has_env

CRED_FILE = Path("/hub-api-creds/credentials.env")
# Settings are read under CHEESE_CAVE_<NAME> first, then KOHAKU_HUB_<NAME> (kohakuhub.envnames).
LAKEFS_ENDPOINT = env_or("LAKEFS_ENDPOINT", "http://lakefs:28000")
ADMIN_USER = os.getenv("LAKEFS_ADMIN_USER", "admin")


def worker_count() -> int:
    return int(env_or("WORKERS", "4"))


def lakefs_credentials_in_env() -> bool:
    return has_env("LAKEFS_ACCESS_KEY")


def wait_for_lakefs():
    url = f"{LAKEFS_ENDPOINT}/_health"
    print(f"[startup] Waiting for lakeFS at {url}...")
    while True:
        try:
            r = httpx.get(url, timeout=2)
            if r.status_code == 200:
                print("[startup] lakeFS is up.")
                return
        except Exception:
            pass
        time.sleep(2)


def is_initialized(client: httpx.Client):
    """Call GET /setup_lakefs to see if already setup."""
    url = f"{LAKEFS_ENDPOINT}/api/v1/setup_lakefs"
    try:
        r = client.get(url, timeout=5)
    except Exception as e:
        print(f"[startup] error calling GET {url}: {e}")
        return False
    print(f"[startup] GET {url} responded {r.status_code} {r.text}")
    if r.status_code == 200:
        try:
            j = r.json()
            if j.get("initialized", False) is True:
                return True
        except Exception:
            pass
    return False


def do_setup(client: httpx.Client):
    url = f"{LAKEFS_ENDPOINT}/api/v1/setup_lakefs"
    payload = {
        "username": ADMIN_USER,
    }
    r = client.post(
        url, json=payload, headers={"accept": "application/json"}, timeout=10
    )
    print(f"[startup] POST {url} responded {r.status_code} {r.text}")
    body = r.json()
    if r.status_code in (200, 201):
        return body["access_key_id"], body["secret_access_key"]
    else:
        print("[startup] Setup failed:", r.status_code, r.text)
        sys.exit(1)


def write_credentials(access_key, secret_key):
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Write then rename so khub-worker never reads a half-written file.
    tmp_file = CRED_FILE.with_suffix(".tmp")
    with open(tmp_file, "w") as f:
        f.write(f"KOHAKU_HUB_LAKEFS_ACCESS_KEY={access_key}\n")
        f.write(f"KOHAKU_HUB_LAKEFS_SECRET_KEY={secret_key}\n")
    os.replace(tmp_file, CRED_FILE)
    print(f"[startup] Saved credentials to {CRED_FILE}")


def load_credentials():
    if lakefs_credentials_in_env():
        return
    with open(CRED_FILE) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                os.environ[k] = v
    print(f"[startup] Loaded credentials from {CRED_FILE}")


def init_garage():
    """Check if Garage is being used and provide setup instructions."""
    s3_endpoint = env_or("S3_ENDPOINT", "")

    # Check if we're using Garage (not MinIO or external S3)
    if "garage" not in s3_endpoint.lower():
        return

    separator = "[startup] ============================================"
    print(separator)
    print("[startup] Detected Garage S3 storage backend")
    print(separator)
    print("[startup] ")
    print("[startup] Garage requires manual initialization from the HOST machine.")
    print(
        "[startup] Documentation: https://garagehq.deuxfleurs.fr/documentation/quick-start/"
    )
    print(separator)
    print("[startup] ")


def run_migrations():
    """Run database migrations before starting server."""
    migrations_script = Path(__file__).parent / "scripts" / "run_migrations.py"

    if not migrations_script.exists():
        print("[startup] No migration script found, skipping migrations")
        return

    print("[startup] Running database migrations...")
    result = subprocess.run(
        [sys.executable, str(migrations_script)],
        env=os.environ,
        capture_output=True,
        text=True,
    )

    # Print migration output
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if result.returncode != 0:
        print("[startup] ✗ Migrations failed! Exiting...")
        sys.exit(1)

    print("[startup] ✓ Migrations completed successfully\n")


def run_worker():
    """Start khub-worker. hub-api owns LakeFS bootstrap and migrations."""
    if not lakefs_credentials_in_env():
        while not CRED_FILE.exists():
            print(f"[startup] Waiting for hub-api to write {CRED_FILE}...")
            time.sleep(2)
        load_credentials()
    print("[startup] Starting background task worker...")
    os.execvp(sys.executable, [sys.executable, "-m", "kohakuhub.worker"])


def main():
    if sys.argv[1:] == ["worker"]:
        run_worker()
        return

    wait_for_lakefs()

    if CRED_FILE.exists() or (
        lakefs_credentials_in_env() and has_env("LAKEFS_SECRET_KEY")
    ):
        load_credentials()
    else:
        with httpx.Client() as client:
            if is_initialized(client):
                print(
                    "[startup] lakeFS is already initialized (by GET). But no local credentials file -> cannot proceed."
                )
                sys.exit(1)
            access_key = secret_key = "123"
            try:
                access_key, secret_key = do_setup(client)
                write_credentials(access_key, secret_key)
            except Exception as e:
                print(f"[startup] Setup failed: {e}")
            os.environ["KOHAKU_HUB_LAKEFS_ACCESS_KEY"] = access_key
            os.environ["KOHAKU_HUB_LAKEFS_SECRET_KEY"] = secret_key

    # Initialize Garage if needed
    init_garage()

    # Run database migrations
    run_migrations()

    # Get worker count from environment
    workers = worker_count()
    print(f"[startup] Starting API server with {workers} worker(s)...")
    subprocess.run(
        [
            "uvicorn",
            "kohakuhub.main:app",
            "--workers",
            str(workers),
            "--host",
            "0.0.0.0",
            "--port",
            "48888",
        ],
        check=True,
        env=os.environ,
    )


if __name__ == "__main__":
    main()
