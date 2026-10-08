"""Configuration management for Kohaku Hub."""

import os
from functools import lru_cache

from pydantic import BaseModel, Field

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib

# Environment names: CHEESE_CAVE_<NAME> wins, KOHAKU_HUB_<NAME> is the fallback.
ENV_PREFIXES = ("CHEESE_CAVE_", "KOHAKU_HUB_")


def read_env(name: str) -> str | None:
    """Return the value of CHEESE_CAVE_<name>, else KOHAKU_HUB_<name>, else None.

    An empty value still counts as set, the same as the existing ``in os.environ`` checks.
    """
    for prefix in ENV_PREFIXES:
        if prefix + name in os.environ:
            return os.environ[prefix + name]
    return None


# Default configuration values
_DEFAULT_S3_ENDPOINT = "http://localhost:9000"


class S3Config(BaseModel):
    public_endpoint: str = _DEFAULT_S3_ENDPOINT
    endpoint: str = _DEFAULT_S3_ENDPOINT
    access_key: str = "test-access-key"
    secret_key: str = "test-secret-key"
    bucket: str = "test-bucket"
    region: str = "us-east-1"  # auto (recommended), us-east-1, or specific AWS region
    force_path_style: bool = True
    signature_version: str | None = None  # s3v4 (R2, AWS S3) or None/s3v2 (MinIO)


class LakeFSConfig(BaseModel):
    endpoint: str = "http://localhost:8000"
    access_key: str = "test-access-key"
    secret_key: str = "test-secret-key"
    repo_namespace: str = "hf"
    # Concurrent LakeFS reads one branch operation (reset, revert, merge) makes
    # while recording the regular files it changed. More does not go faster:
    # measured on LakeFS 1.87, requests saturate around 8, and other requests
    # slow down past it (PR #117).
    operation_concurrency: int = Field(default=8, ge=1)


class SMTPConfig(BaseModel):
    enabled: bool = False
    host: str = "localhost"
    port: int = 587
    username: str = ""
    password: str = ""
    from_email: str = "noreply@localhost"
    use_tls: bool = True


class AuthConfig(BaseModel):
    require_email_verification: bool = False
    invitation_only: bool = False  # Disable public registration, require invitation
    session_secret: str = "change-me-in-production"
    session_expire_hours: int = 168  # 7 days
    token_expire_days: int = 365


class AdminConfig(BaseModel):
    """Admin API configuration."""

    enabled: bool = True
    secret_token: str = "change-me-in-production"


class QuotaConfig(BaseModel):
    """Storage quota configuration."""

    default_user_private_quota_bytes: int | None = None  # None = unlimited
    default_user_public_quota_bytes: int | None = None  # None = unlimited
    default_org_private_quota_bytes: int | None = None  # None = unlimited
    default_org_public_quota_bytes: int | None = None  # None = unlimited


class FallbackConfig(BaseModel):
    """Fallback source configuration."""

    enabled: bool = True  # Enable fallback system
    cache_ttl_seconds: int = 30  # Cache TTL for repo→source mappings (default 30s post-#78)
    timeout_seconds: int = 10  # HTTP request timeout for external sources
    max_concurrent_requests: int = 5  # Max concurrent requests to external sources
    require_auth: bool = False  # Require authenticated user for fallback access
    # Global fallback sources (JSON list)
    # Format: [{"url": "https://huggingface.co", "token": "", "priority": 1, "name": "HF", "source_type": "huggingface"}]
    sources: list[dict] = []


class CacheConfig(BaseModel):
    """L2 cache (Valkey/Redis) configuration.

    Pure cache, no business state. When ``enabled`` is False the cache layer
    silently degrades and every call falls back to its source. See
    ``docs/development/cache.md`` for the full design.
    """

    # Disabled by default so existing deployments don't acquire a hard
    # dependency on Valkey unintentionally. Local-dev .env.dev.example flips
    # this to True so contributors surface cache bugs early.
    enabled: bool = False
    url: str = "redis://localhost:6379/0"
    # Namespace prefix isolates cache keys when multiple deployments share a
    # single Valkey instance (rare in production, common in CI runners that
    # reuse a managed Redis between jobs).
    namespace: str = "kh"
    # Default TTL applied by ``cache_set_json`` when callers don't pass one.
    default_ttl_seconds: int = 300
    # ±jitter fraction applied to every TTL inside the helper. Set to 0 to
    # disable jitter (only useful for deterministic tests).
    jitter_fraction: float = 0.15
    # Connection pool size; tuned for ~4 uvicorn workers each running a
    # handful of concurrent requests. Bump if a deployment scales beyond.
    max_connections: int = 50
    # Socket timeouts. Cache must never become a latency cliff — short
    # timeouts so a flaky Valkey degrades to "cache miss" within ms, not s.
    socket_timeout_seconds: float = 0.5
    socket_connect_timeout_seconds: float = 0.5


class WorkerConfig(BaseModel):
    """Background task worker (``python -m kohakuhub.worker``)."""

    concurrency: int = 4  # Handlers running at once per worker process
    lease_seconds: int = 60  # A task is reclaimed if its worker stops renewing
    poll_interval_seconds: float = 1.0  # Idle wait between claim attempts
    shutdown_grace_seconds: float = 30.0  # Drain time on SIGTERM before cancelling
    # How often a running task's progress and logs are stored (and cancellation
    # is noticed); the lease is renewed at least every lease_seconds / 3 anyway.
    flush_interval_seconds: float = 5.0
    log_max_bytes_per_attempt: int = 10 * 1024 * 1024  # Later records are dropped
    succeeded_retention_days: int = 7
    failed_retention_days: int = 30
    queues: list[str] = []  # Empty = consume every queue
    # Optional prefix for the name shown in the admin panel ("<name>-<hostname>");
    # the hostname alone tells replicas apart, so leaving it empty is fine.
    name: str = ""


class AppConfig(BaseModel):
    base_url: str = "http://localhost:48888"
    # Allows local dev to expose frontend-facing URLs while backend self-calls stay direct.
    internal_base_url: str | None = None
    api_base: str = "/api"
    db_backend: str = "sqlite"
    # Repository history operations (#99); each can be switched off. They
    # are only available with db_backend = "postgres", and Reset only with
    # a LakeFS it works with (kohakuhub.lakefs_compat).
    repository_revert_enabled: bool = True
    repository_reset_enabled: bool = True
    repository_squash_enabled: bool = True
    database_url: str = "sqlite:///./hub.db"
    database_key: str = (
        ""  # Encryption key for external tokens (generate with: openssl rand -hex 32)
    )
    # Lower threshold to 5MB to account for base64 encoding overhead (~33%)
    # 5MB file -> ~6.7MB base64, leaving room for multiple files in one commit
    lfs_threshold_bytes: int = 5 * 1000 * 1000
    debug_log_payloads: bool = False
    # LFS Multipart Upload settings
    lfs_multipart_threshold_bytes: int = (
        100 * 1000 * 1000
    )  # 100 MB - use multipart for files larger than this
    lfs_multipart_chunk_size_bytes: int = (
        50 * 1000 * 1000
    )  # 50 MB - size of each part (S3 minimum is 5MB except last part)
    # LFS Garbage Collection settings
    lfs_keep_versions: int = 5  # Keep last K versions of each file
    lfs_auto_gc: bool = False  # Collect LFS versions beyond lfs_keep_versions (background)
    # Storage usage is kept up to date as repositories change; a periodic
    # full recount (kohakuhub.usage) is a safety net. 0 = off
    usage_recount_interval_hours: float = 0
    # Download tracking settings
    download_time_bucket_seconds: int = 900  # 15 minutes - session deduplication window
    download_session_cleanup_threshold: int = 100  # Trigger cleanup when sessions > this
    download_keep_sessions_days: int = 30  # Keep sessions from last N days
    # LFS Suffix Rules - File extensions that should ALWAYS use LFS
    # These are server-wide defaults that apply to ALL repositories
    # Repositories can add their own additional suffix rules
    lfs_suffix_rules_default: list[str] = [
        # ML Model Formats
        ".safetensors",  # SafeTensors (most common for HF models)
        ".bin",  # PyTorch binary weights
        ".pt",  # PyTorch checkpoint
        ".pth",  # PyTorch checkpoint
        ".ckpt",  # PyTorch Lightning checkpoint
        ".onnx",  # ONNX model
        ".pb",  # TensorFlow protobuf
        ".h5",  # Keras/HDF5 model
        ".tflite",  # TensorFlow Lite
        ".gguf",  # GGUF quantized models (llama.cpp)
        ".ggml",  # GGML models
        ".msgpack",  # MessagePack serialization
        # Compressed Archives
        ".zip",  # ZIP archive
        ".tar",  # TAR archive
        ".gz",  # GZIP compressed
        ".bz2",  # BZIP2 compressed
        ".xz",  # XZ compressed
        ".7z",  # 7-Zip archive
        ".rar",  # RAR archive
        # Data Files
        ".npy",  # NumPy array
        ".npz",  # NumPy compressed archive
        ".arrow",  # Apache Arrow
        ".parquet",  # Apache Parquet
        # Media Files
        ".mp4",  # Video
        ".avi",  # Video
        ".mkv",  # Video
        ".mov",  # Video
        ".wav",  # Audio
        ".mp3",  # Audio
        ".flac",  # Audio
        # Images (large formats)
        ".tiff",  # TIFF image
        ".tif",  # TIFF image
    ]
    # Site identification
    site_name: str = "CheeseCave"  # Configurable site name (e.g., "MyCompany Hub")
    # Log settings
    log_level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    log_format: str = "file"  # Output logs to "file" or "terminal" (maybe sql in future)
    log_dir: str = "logs/"  # Path to log file (if log_format is "file")


class Config(BaseModel):
    s3: S3Config
    lakefs: LakeFSConfig
    smtp: SMTPConfig = SMTPConfig()
    auth: AuthConfig = AuthConfig()
    admin: AdminConfig = AdminConfig()
    quota: QuotaConfig = QuotaConfig()
    fallback: FallbackConfig = FallbackConfig()
    cache: CacheConfig = CacheConfig()
    worker: WorkerConfig = WorkerConfig()
    app: AppConfig

    def validate_production_safety(self) -> list[str]:
        """Check if configuration uses unsafe default values.

        Returns:
            List of warning messages for unsafe defaults
        """
        warnings = []

        # S3 credentials
        if self.s3.access_key == "test-access-key":
            warnings.append("S3 access_key is using test default value")
        if self.s3.secret_key == "test-secret-key":
            warnings.append("S3 secret_key is using test default value")
        if self.s3.bucket == "test-bucket":
            warnings.append("S3 bucket is using test default value")

        # LakeFS credentials
        if self.lakefs.access_key == "test-access-key":
            warnings.append("LakeFS access_key is using test default value")
        if self.lakefs.secret_key == "test-secret-key":
            warnings.append("LakeFS secret_key is using test default value")

        # Auth secrets
        if self.auth.session_secret == "change-me-in-production":
            warnings.append("Session secret is using default value - SECURITY RISK!")
        if self.admin.secret_token == "change-me-in-production":
            warnings.append("Admin secret token is using default value - SECURITY RISK!")

        # LFS GC settings validation
        if self.app.lfs_keep_versions < 2:
            warnings.append(
                f"LFS keep_versions={self.app.lfs_keep_versions} is too low! "
                f"Minimum recommended: 5. Revert/reset operations will likely fail. "
                f"Set CHEESE_CAVE_LFS_KEEP_VERSIONS=5 (or KOHAKU_HUB_LFS_KEEP_VERSIONS=5) or higher."
            )

        # LFS threshold validation
        if self.app.lfs_threshold_bytes < 1000 * 1000:  # Less than 1MB
            warnings.append(
                f"LFS threshold is very low ({self.app.lfs_threshold_bytes} bytes). "
                f"Consider setting to at least 5MB (5242880 bytes)."
            )

        return warnings


def update_recursive(d: dict, u: dict) -> dict:
    """Recursively update a dictionary."""
    for k, v in u.items():
        if isinstance(v, dict):
            # get node or create one
            d[k] = update_recursive(d.get(k, {}), v)
        else:
            d[k] = v
    return d


def _parse_quota(value: str | None) -> int | None:
    """Parse quota value from environment variable."""
    if value is None or value.lower() in ("", "none", "unlimited"):
        return None
    return int(value)


def _parse_fallback_sources(value: str | None) -> list[dict]:
    """Parse fallback sources from JSON environment variable."""
    import json

    if not value:
        return []
    try:
        sources = json.loads(value)
        if not isinstance(sources, list):
            return []
        return sources
    except json.JSONDecodeError:
        return []


@lru_cache(maxsize=1)
def load_config(path: str = None) -> Config:
    # 1. Determine config file path: explicit path, HUB_CONFIG env, or default "config.toml"
    config_path = path or os.environ.get("HUB_CONFIG") or "config.toml"

    # 2. Load from TOML file if it exists
    config_from_file = {}
    if os.path.exists(config_path):
        with open(config_path, "rb") as f:
            config_from_file = tomllib.load(f)

    # 3. Load from environment variables, building a nested dict
    config_from_env = {}

    # S3
    s3_env = {}
    if read_env("S3_PUBLIC_ENDPOINT") is not None:
        s3_env["public_endpoint"] = read_env("S3_PUBLIC_ENDPOINT")
    if read_env("S3_ENDPOINT") is not None:
        s3_env["endpoint"] = read_env("S3_ENDPOINT")
    if read_env("S3_ACCESS_KEY") is not None:
        s3_env["access_key"] = read_env("S3_ACCESS_KEY")
    if read_env("S3_SECRET_KEY") is not None:
        s3_env["secret_key"] = read_env("S3_SECRET_KEY")
    if read_env("S3_BUCKET") is not None:
        s3_env["bucket"] = read_env("S3_BUCKET")
    if read_env("S3_REGION") is not None:
        s3_env["region"] = read_env("S3_REGION")
    if read_env("S3_SIGNATURE_VERSION") is not None:
        s3_env["signature_version"] = read_env("S3_SIGNATURE_VERSION")
    if s3_env:
        config_from_env["s3"] = s3_env

    # LakeFS
    lakefs_env = {}
    if read_env("LAKEFS_ENDPOINT") is not None:
        lakefs_env["endpoint"] = read_env("LAKEFS_ENDPOINT")
    if read_env("LAKEFS_ACCESS_KEY") is not None:
        lakefs_env["access_key"] = read_env("LAKEFS_ACCESS_KEY")
    if read_env("LAKEFS_SECRET_KEY") is not None:
        lakefs_env["secret_key"] = read_env("LAKEFS_SECRET_KEY")
    if read_env("LAKEFS_REPO_NAMESPACE") is not None:
        lakefs_env["repo_namespace"] = read_env("LAKEFS_REPO_NAMESPACE")
    if read_env("LAKEFS_OPERATION_CONCURRENCY") is not None:
        lakefs_env["operation_concurrency"] = int(
            read_env("LAKEFS_OPERATION_CONCURRENCY")
        )
    if lakefs_env:
        config_from_env["lakefs"] = lakefs_env

    # SMTP
    smtp_env = {}
    if read_env("SMTP_ENABLED") is not None:
        smtp_env["enabled"] = read_env("SMTP_ENABLED").lower() == "true"
    if read_env("SMTP_HOST") is not None:
        smtp_env["host"] = read_env("SMTP_HOST")
    if read_env("SMTP_PORT") is not None:
        smtp_env["port"] = int(read_env("SMTP_PORT"))
    if read_env("SMTP_USERNAME") is not None:
        smtp_env["username"] = read_env("SMTP_USERNAME")
    if read_env("SMTP_PASSWORD") is not None:
        smtp_env["password"] = read_env("SMTP_PASSWORD")
    if read_env("SMTP_FROM") is not None:
        smtp_env["from_email"] = read_env("SMTP_FROM")
    if read_env("SMTP_TLS") is not None:
        smtp_env["use_tls"] = read_env("SMTP_TLS").lower() == "true"
    if smtp_env:
        config_from_env["smtp"] = smtp_env

    # Auth
    auth_env = {}
    if read_env("REQUIRE_EMAIL_VERIFICATION") is not None:
        auth_env["require_email_verification"] = (
            read_env("REQUIRE_EMAIL_VERIFICATION").lower() == "true"
        )
    if read_env("INVITATION_ONLY") is not None:
        auth_env["invitation_only"] = read_env("INVITATION_ONLY").lower() == "true"
    if read_env("SESSION_SECRET") is not None:
        auth_env["session_secret"] = read_env("SESSION_SECRET")
    if read_env("SESSION_EXPIRE_HOURS") is not None:
        auth_env["session_expire_hours"] = int(read_env("SESSION_EXPIRE_HOURS"))
    if read_env("TOKEN_EXPIRE_DAYS") is not None:
        auth_env["token_expire_days"] = int(read_env("TOKEN_EXPIRE_DAYS"))
    if auth_env:
        config_from_env["auth"] = auth_env

    # Admin
    admin_env = {}
    if read_env("ADMIN_ENABLED") is not None:
        admin_env["enabled"] = read_env("ADMIN_ENABLED").lower() == "true"
    if read_env("ADMIN_SECRET_TOKEN") is not None:
        admin_env["secret_token"] = read_env("ADMIN_SECRET_TOKEN")
    if admin_env:
        config_from_env["admin"] = admin_env

    # Quota
    quota_env = {}
    if read_env("DEFAULT_USER_PRIVATE_QUOTA_BYTES") is not None:
        quota_env["default_user_private_quota_bytes"] = _parse_quota(
            read_env("DEFAULT_USER_PRIVATE_QUOTA_BYTES")
        )
    if read_env("DEFAULT_USER_PUBLIC_QUOTA_BYTES") is not None:
        quota_env["default_user_public_quota_bytes"] = _parse_quota(
            read_env("DEFAULT_USER_PUBLIC_QUOTA_BYTES")
        )
    if read_env("DEFAULT_ORG_PRIVATE_QUOTA_BYTES") is not None:
        quota_env["default_org_private_quota_bytes"] = _parse_quota(
            read_env("DEFAULT_ORG_PRIVATE_QUOTA_BYTES")
        )
    if read_env("DEFAULT_ORG_PUBLIC_QUOTA_BYTES") is not None:
        quota_env["default_org_public_quota_bytes"] = _parse_quota(
            read_env("DEFAULT_ORG_PUBLIC_QUOTA_BYTES")
        )
    if quota_env:
        config_from_env["quota"] = quota_env

    # Cache (L2 / Valkey)
    cache_env = {}
    if read_env("CACHE_ENABLED") is not None:
        cache_env["enabled"] = read_env("CACHE_ENABLED").lower() == "true"
    if read_env("CACHE_URL") is not None:
        cache_env["url"] = read_env("CACHE_URL")
        # Implicit-enable: if the operator set a CACHE_URL but did not
        # set CACHE_ENABLED, treat the URL as opt-in. This matters for
        # dev environments whose .env.dev predates the cache feature —
        # they get a fresh CHEESE_CAVE_CACHE_URL line (e.g. via
        # ``cp .env.dev.example .env.dev``) without remembering to also
        # set ENABLED. Explicit ``CHEESE_CAVE_CACHE_ENABLED=false`` still
        # wins over this default.
        if read_env("CACHE_ENABLED") is None:
            cache_env["enabled"] = True
    if read_env("CACHE_NAMESPACE") is not None:
        cache_env["namespace"] = read_env("CACHE_NAMESPACE")
    if read_env("CACHE_DEFAULT_TTL") is not None:
        cache_env["default_ttl_seconds"] = int(read_env("CACHE_DEFAULT_TTL"))
    if read_env("CACHE_JITTER_FRACTION") is not None:
        cache_env["jitter_fraction"] = float(read_env("CACHE_JITTER_FRACTION"))
    if read_env("CACHE_MAX_CONNECTIONS") is not None:
        cache_env["max_connections"] = int(read_env("CACHE_MAX_CONNECTIONS"))
    if read_env("CACHE_SOCKET_TIMEOUT") is not None:
        cache_env["socket_timeout_seconds"] = float(read_env("CACHE_SOCKET_TIMEOUT"))
    if read_env("CACHE_SOCKET_CONNECT_TIMEOUT") is not None:
        cache_env["socket_connect_timeout_seconds"] = float(
            read_env("CACHE_SOCKET_CONNECT_TIMEOUT")
        )
    if cache_env:
        config_from_env["cache"] = cache_env

    # Worker
    worker_env = {}
    for env_name, key, parse in (
        ("WORKER_CONCURRENCY", "concurrency", int),
        ("WORKER_LEASE_SECONDS", "lease_seconds", int),
        ("WORKER_POLL_INTERVAL_SECONDS", "poll_interval_seconds", float),
        ("WORKER_SHUTDOWN_GRACE_SECONDS", "shutdown_grace_seconds", float),
        ("WORKER_FLUSH_INTERVAL_SECONDS", "flush_interval_seconds", float),
        ("WORKER_LOG_MAX_BYTES_PER_ATTEMPT", "log_max_bytes_per_attempt", int),
        ("WORKER_SUCCEEDED_RETENTION_DAYS", "succeeded_retention_days", int),
        ("WORKER_FAILED_RETENTION_DAYS", "failed_retention_days", int),
    ):
        if read_env(env_name) is not None:
            worker_env[key] = parse(read_env(env_name))
    if read_env("WORKER_NAME") is not None:
        worker_env["name"] = read_env("WORKER_NAME").strip()
    if read_env("WORKER_QUEUES") is not None:
        worker_env["queues"] = [
            queue.strip()
            for queue in read_env("WORKER_QUEUES").split(",")
            if queue.strip()
        ]
    if worker_env:
        config_from_env["worker"] = worker_env

    # Fallback
    fallback_env = {}
    if read_env("FALLBACK_ENABLED") is not None:
        fallback_env["enabled"] = read_env("FALLBACK_ENABLED").lower() == "true"
    if read_env("FALLBACK_CACHE_TTL") is not None:
        fallback_env["cache_ttl_seconds"] = int(read_env("FALLBACK_CACHE_TTL"))
    if read_env("FALLBACK_TIMEOUT") is not None:
        fallback_env["timeout_seconds"] = int(read_env("FALLBACK_TIMEOUT"))
    if read_env("FALLBACK_MAX_CONCURRENT") is not None:
        fallback_env["max_concurrent_requests"] = int(
            read_env("FALLBACK_MAX_CONCURRENT")
        )
    if read_env("FALLBACK_REQUIRE_AUTH") is not None:
        fallback_env["require_auth"] = (
            read_env("FALLBACK_REQUIRE_AUTH").lower() == "true"
        )
    if read_env("FALLBACK_SOURCES") is not None:
        fallback_env["sources"] = _parse_fallback_sources(
            read_env("FALLBACK_SOURCES")
        )
    if fallback_env:
        config_from_env["fallback"] = fallback_env

    # App
    app_env = {}
    if read_env("BASE_URL") is not None:
        app_env["base_url"] = read_env("BASE_URL")
    if read_env("INTERNAL_BASE_URL") is not None:
        app_env["internal_base_url"] = read_env("INTERNAL_BASE_URL")
    if read_env("API_BASE") is not None:
        app_env["api_base"] = read_env("API_BASE")
    if read_env("REPOSITORY_REVERT_ENABLED") is not None:
        app_env["repository_revert_enabled"] = (
            read_env("REPOSITORY_REVERT_ENABLED").lower() == "true"
        )
    if read_env("REPOSITORY_RESET_ENABLED") is not None:
        app_env["repository_reset_enabled"] = (
            read_env("REPOSITORY_RESET_ENABLED").lower() == "true"
        )
    if read_env("REPOSITORY_SQUASH_ENABLED") is not None:
        app_env["repository_squash_enabled"] = (
            read_env("REPOSITORY_SQUASH_ENABLED").lower() == "true"
        )
    if read_env("DB_BACKEND") is not None:
        app_env["db_backend"] = read_env("DB_BACKEND")
    if read_env("DATABASE_URL") is not None:
        app_env["database_url"] = read_env("DATABASE_URL")
    if read_env("DATABASE_KEY") is not None:
        app_env["database_key"] = read_env("DATABASE_KEY")
    if read_env("LFS_THRESHOLD_BYTES") is not None:
        app_env["lfs_threshold_bytes"] = int(read_env("LFS_THRESHOLD_BYTES"))
    if read_env("LFS_MULTIPART_THRESHOLD_BYTES") is not None:
        app_env["lfs_multipart_threshold_bytes"] = int(
            read_env("LFS_MULTIPART_THRESHOLD_BYTES")
        )
    if read_env("LFS_MULTIPART_CHUNK_SIZE_BYTES") is not None:
        app_env["lfs_multipart_chunk_size_bytes"] = int(
            read_env("LFS_MULTIPART_CHUNK_SIZE_BYTES")
        )
    if read_env("LFS_KEEP_VERSIONS") is not None:
        app_env["lfs_keep_versions"] = int(read_env("LFS_KEEP_VERSIONS"))
    if read_env("LFS_AUTO_GC") is not None:
        app_env["lfs_auto_gc"] = read_env("LFS_AUTO_GC").lower() == "true"
    if read_env("USAGE_RECOUNT_INTERVAL_HOURS") is not None:
        app_env["usage_recount_interval_hours"] = float(
            read_env("USAGE_RECOUNT_INTERVAL_HOURS")
        )
    if read_env("SITE_NAME") is not None:
        app_env["site_name"] = read_env("SITE_NAME")
    if read_env("DEBUG_LOG_PAYLOADS") is not None:
        app_env["debug_log_payloads"] = (
            read_env("DEBUG_LOG_PAYLOADS").lower() == "true"
        )
    if read_env("LOG_LEVEL") is not None:
        app_env["log_level"] = read_env("LOG_LEVEL")
    if read_env("LOG_FORMAT") is not None:
        app_env["log_format"] = read_env("LOG_FORMAT")
    if read_env("LOG_DIR") is not None:
        app_env["log_dir"] = read_env("LOG_DIR")
    if app_env:
        config_from_env["app"] = app_env

    # 4. Merge: Start with file config, then recursively update with env config
    merged_config = update_recursive(config_from_file, config_from_env)

    # 5. Instantiate config models, allowing Pydantic to handle defaults
    s3_config = S3Config(**merged_config.get("s3", {}))
    lakefs_config = LakeFSConfig(**merged_config.get("lakefs", {}))
    smtp_config = SMTPConfig(**merged_config.get("smtp", {}))
    auth_config = AuthConfig(**merged_config.get("auth", {}))
    admin_config = AdminConfig(**merged_config.get("admin", {}))
    quota_config = QuotaConfig(**merged_config.get("quota", {}))
    fallback_config = FallbackConfig(**merged_config.get("fallback", {}))
    cache_config = CacheConfig(**merged_config.get("cache", {}))
    worker_config = WorkerConfig(**merged_config.get("worker", {}))
    app_config = AppConfig(**merged_config.get("app", {}))

    return Config(
        s3=s3_config,
        lakefs=lakefs_config,
        smtp=smtp_config,
        auth=auth_config,
        admin=admin_config,
        quota=quota_config,
        fallback=fallback_config,
        cache=cache_config,
        worker=worker_config,
        app=app_config,
    )


cfg = load_config()
