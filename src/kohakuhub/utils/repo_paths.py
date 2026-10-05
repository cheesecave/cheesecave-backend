"""The limit on a path inside a repository."""

# S3 caps object keys at 1024 bytes, and so LakeFS's S3 gateway; the indexes
# over the path columns (TEXT) hold well over it.
MAX_PATH_BYTES = 1024


def too_long(path: str) -> bool:
    return len(path.encode("utf-8")) > MAX_PATH_BYTES
