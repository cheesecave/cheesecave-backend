"""The limit on a path inside a repository, and what a path may not be."""

# S3 caps object keys at 1024 bytes, and so LakeFS's S3 gateway; the indexes
# over the path columns (TEXT) hold well over it.
MAX_PATH_BYTES = 1024


def too_long(path: str) -> bool:
    return len(path.encode("utf-8")) > MAX_PATH_BYTES


def path_problem(path: str, *, creating: bool) -> str | None:
    """What is wrong with ``path``, or ``None`` (#99).

    The rules are the ones ``huggingface_hub`` applies before it sends a path (``_validate_path_in_repo``), plus the
    control characters it does not look at. What it leaves alone, a server leaves alone: ``a//b``, ``a/./b``, a leading
    or trailing ``/``, a backslash and the client's own ``.cache`` folder are kept, because the client sends them as they
    are.

    - A control character (NUL, a line break, ...) is refused in every path of every operation: NUL makes the database
      raise, and none of them is a name.
    - A path that is ``.``, a ``..`` segment (a clone or a download writes it outside its directory) and a ``.git``
      segment in any case (git refuses to check such an entry out) are refused only for
      what a commit **puts** somewhere (``creating``): a delete may still clear what an earlier commit stored.
    """
    if any(ord(char) < 0x20 or char == "\x7f" for char in path):
        return "A path has a control character (NUL, a line break, ...), which no name has"
    if not creating:
        return None
    segments = path.split("/")
    if path == ".":
        return "A path cannot be '.'"
    if ".." in segments:
        return "A path has a '..' segment, which is not allowed (`huggingface_hub` refuses it too)"
    if any(segment.lower() == ".git" for segment in segments):
        return "A path has a '.git' folder, which is not allowed (`huggingface_hub` refuses it too)"
    return None
