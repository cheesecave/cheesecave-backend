"""LakeFS client utilities and helper functions."""

import hashlib
import re
import secrets

import numpy as np

from kohakuhub.lakefs_rest_client import LakeFSRestClient, get_lakefs_rest_client


def get_lakefs_client() -> LakeFSRestClient:
    """Get configured LakeFS REST client.

    Returns:
        Configured LakeFSRestClient instance.
    """
    return get_lakefs_rest_client()


async def resolve_revision(
    client: LakeFSRestClient, lakefs_repo: str, revision: str
) -> tuple[str, dict | None]:
    """Resolve a revision (branch name or commit hash) to commit ID and info.

    HuggingFace datasets library and other clients may use either branch names
    (e.g., "main") or commit hashes as revision identifiers. This function
    handles both cases by first trying to resolve as a branch, then as a commit.

    Args:
        client: LakeFS REST client instance
        lakefs_repo: LakeFS repository name
        revision: Branch name or commit hash

    Returns:
        Tuple of (commit_id, commit_info dict or None)

    Raises:
        ValueError: If revision cannot be resolved as either branch or commit
    """
    # Try resolving as a branch first
    try:
        branch = await client.get_branch(repository=lakefs_repo, branch=revision)
        commit_id = branch["commit_id"]
        # Get commit details
        try:
            commit_info = await client.get_commit(
                repository=lakefs_repo, commit_id=commit_id
            )
        except Exception:
            commit_info = None
        return commit_id, commit_info
    except Exception as branch_error:
        # Check if it's a "not found" error (branch doesn't exist)
        error_str = str(branch_error).lower()
        if "404" not in error_str and "not found" not in error_str:
            # Some other error, re-raise
            raise branch_error

    # Branch not found, try resolving as a commit hash
    try:
        commit_info = await client.get_commit(
            repository=lakefs_repo, commit_id=revision
        )
        return commit_info["id"], commit_info
    except Exception as commit_error:
        # Neither branch nor commit found
        raise ValueError(
            f"Revision '{revision}' not found as branch or commit"
        ) from commit_error


def _base36_encode(num: int) -> str:
    """Encode integer to base36 using numpy (C-optimized).

    Alphabet: 0-9, a-z (36 chars - standard base36, LakeFS-compatible)
    Each char carries ~5.17 bits (log2(36) = 5.170)
    22 chars can hold ~113.7 bits (sufficient for 112-bit hash)

    Args:
        num: Integer to encode

    Returns:
        Base36 encoded string (lowercase)
    """
    # Use numpy's optimized base_repr (C implementation)
    encoded = np.base_repr(num, base=36).lower()
    return encoded


def _hash_to_112bit(data: str) -> int:
    """Hash string to 112-bit integer using SHA3-224 with XOR folding.

    SHA3-224 produces 224 bits (28 bytes), we fold it to 112 bits by XORing the two halves.
    This preserves entropy while reducing size to fit base37 encoding.

    Args:
        data: String to hash

    Returns:
        112-bit integer (0 to 2^112-1)
    """
    # Use SHA3-224 (produces exactly 224 bits = 28 bytes)
    hash_bytes = hashlib.sha3_224(data.encode()).digest()  # 28 bytes = 224 bits

    # Split into two 112-bit (14 byte) halves and XOR them
    half1 = int.from_bytes(hash_bytes[:14], "big")  # First 14 bytes = 112 bits
    half2 = int.from_bytes(hash_bytes[14:], "big")  # Last 14 bytes = 112 bits

    return half1 ^ half2  # XOR folding: 224 bits → 112 bits


def _sanitize_repo_id(repo_id: str) -> str:
    """Sanitize repo ID to LakeFS-safe characters.

    Allowed: a-z, 0-9, hyphen
    Replace: /, _, ., and any other special chars with hyphen

    Args:
        repo_id: Raw repo ID (e.g., "org/repo_name.v2")

    Returns:
        Sanitized ID (e.g., "org-repo-name-v2")
    """
    # Replace common separators
    safe = repo_id.replace("/", "-").replace("_", "-").replace(".", "-")

    # Remove any remaining non-alphanumeric/hyphen characters
    safe = re.sub(r"[^a-z0-9-]", "-", safe.lower())

    # Collapse consecutive hyphens
    safe = re.sub(r"-+", "-", safe)

    # Strip leading/trailing hyphens
    safe = safe.strip("-")

    return safe


def lakefs_repo_name(repo_type: str, repo_id: str, generation: int | str = 0) -> str:
    """Generate LakeFS repository name from HuggingFace repo ID.

    LakeFS naming requirements: ^[a-z0-9][a-z0-9-]{2,62}$
    - Only lowercase letters, numbers, and hyphens
    - Must start with letter or number
    - Length: 3-63 characters

    GLOBAL format (ALL repos use this):
    Format: {type}-{safe_id[:38]}-{hash[:22]}
    - 1 char: Repo type (m=model, d=dataset, s=space)
    - 38 chars: Sanitized repo_id (human-readable, may be truncated)
    - 22 chars: Base36-encoded 112-bit hash of ORIGINAL repo_id (for uniqueness)
    Total: <= 63 chars

    Why hash ORIGINAL repo_id?
    - Sanitization can make different names identical (e.g., "my_repo" vs "my.repo" → "my-repo")
    - Hash ensures uniqueness even after sanitization
    - Hash is ALWAYS included (no escaping)

    Note:
        Base36 encoding (0-9, a-z) provides ~5.17 bits/char
        22 chars × 5.17 = ~113.7 bits (fits 112-bit hash perfectly)
        Hash: SHA3-224 (224 bits) → XOR folding → 112 bits
        Uses numpy.base_repr for C-optimized performance

    Generations:
        A repo ID can outlive the LakeFS repository it maps to: renaming a repo
        deletes the old LakeFS repository, and LakeFS deletes asynchronously, so
        the derived name stays taken for a while afterwards (issue #93). The
        layout above is exactly 63 chars - the LakeFS maximum - so there is no
        room to append a generation token. The generation goes into the *hash
        input* instead:

            generation 0 -> hash(repo_id)            (legacy value, unchanged)
            generation N -> hash(f"{repo_id}#{N}")

        Generation 0 must stay byte-identical to the pre-generation scheme:
        existing rows are backfilled with it by migration 016, and any drift
        would point stored ids at LakeFS repositories that do not exist.

    Args:
        repo_type: Repository type (model/dataset/space)
        repo_id: Full repository ID (e.g., "org/repo-name")
        generation: Which incarnation of this repo ID to name. Allocate it with
            `allocate_lakefs_repo_name` rather than guessing.

    Returns:
        LakeFS-safe repository name (at most 63 chars; exactly 63 only when
        the sanitized repo id is truncated)

    Examples:
        - "model", "org/simple" → "m" + "-" + "org-simple" (38 chars) + "-" + hash(22 chars)
        - "dataset", "org/my_data.v2" → "d" + "-" + "org-my-data-v2" (38 chars) + "-" +  hash(22 chars)
        - "model", "org/very-long-repository-name-with-version-v2.3.4-final"
          → "m" + "-" + "org-very-long-repository-name-with-ver" (38 chars) + "-" +  hash(22 chars)
    """
    # Map repo type to single character
    type_char = {"model": "m", "dataset": "d", "space": "s"}.get(repo_type, "m")

    # Sanitize repo_id for human-readable part (truncate to 38 chars max)
    safe_id = _sanitize_repo_id(repo_id)[:38]

    # ALWAYS generate hash of ORIGINAL repo_id (before sanitization)
    # This ensures uniqueness even when sanitization causes collisions
    # Generation 0 hashes the bare repo_id so the name matches the pre-generation
    # scheme exactly; later generations salt it to get a distinct repository.
    hash_input = repo_id if generation == 0 else f"{repo_id}#{generation}"
    hash_int = _hash_to_112bit(hash_input)  # Hash ORIGINAL, not sanitized!

    # Encode to base36 using numpy (C-optimized)
    hash_b36 = _base36_encode(hash_int)

    # Pad to exactly 22 chars (left-pad with '0')
    hash_suffix = hash_b36.zfill(22)

    # Build final name: type(1) + safe_id(<=38) + hash(22)
    basename = f"{type_char}-{safe_id}-{hash_suffix}"

    return basename


# How many sequential generations to probe. Each costs one LakeFS lookup. A
# rename keeps the repository's LakeFS id (#107), so each time an id is renamed
# away and created again, one more of its generations stays taken for good.
MAX_LAKEFS_REPO_GENERATIONS = 16
# Past the sequential generations, allocation draws random generation tokens.
# The name carries a 112-bit hash of the token, so a draw colliding is not a
# realistic event; the bound only keeps a misbehaving LakeFS from looping us.
MAX_LAKEFS_REPO_RANDOM_DRAWS = 8


def resolve_lakefs_repo(repo) -> str:
    """Return the LakeFS repository id backing a Repository row.

    Prefers the id stored on the row, falling back to the generation-0
    derivation for rows written before migration 016 added the column.

    Always use this instead of calling `lakefs_repo_name` with a repo id: a row
    whose LakeFS repository was allocated at generation > 0 does not derive back
    to its own id, so deriving would silently address the wrong repository.

    The row must come from a full `Repository.select()`. Peewee returns None for
    a column that was not selected, which is indistinguishable here from a
    pre-migration row and would quietly fall back to the derived name. No query
    feeding this function selects a subset today; keep it that way.

    Args:
        repo: Repository row (needs `repo_type`, `full_id`, and optionally
            `lakefs_repo`).

    Returns:
        LakeFS repository id.
    """
    stored = getattr(repo, "lakefs_repo", None)
    if stored:
        return stored
    return lakefs_repo_name(repo.repo_type, repo.full_id)


async def allocate_lakefs_repo_name(
    client,
    repo_type: str,
    repo_id: str,
    max_generations: int = MAX_LAKEFS_REPO_GENERATIONS,
    exclude: set[str] | frozenset[str] = frozenset(),
) -> str:
    """Pick a LakeFS repository id for `repo_id` that LakeFS does not already hold.

    Generation 0 is the legacy derived name and is used whenever it is free, so
    the common case is unchanged. It is *not* free while a previous incarnation
    of the same repo id is still being deleted (LakeFS deletes asynchronously,
    see issue #93), or after the repository that held it was renamed away (a
    rename keeps its LakeFS id, #107); allocation then steps to the next
    generation. Once the sequential generations are exhausted it draws random
    generation tokens, so a much-recycled id still gets a fresh repository.

    The caller must persist the result on `Repository.lakefs_repo`, otherwise a
    generation > 0 repository becomes unreachable.

    Args:
        client: LakeFS client exposing `repository_exists`.
        repo_type: Repository type (model/dataset/space).
        repo_id: Full repository ID (e.g., "org/repo-name").
        max_generations: How many sequential generations to probe.
        exclude: Ids to skip even if LakeFS reports them free - e.g. an id whose
            create just failed because LakeFS is still deleting it.

    Returns:
        A LakeFS repository id that was free at probe time.

    Raises:
        RuntimeError: If every probed id is taken.
    """
    candidates = [
        lakefs_repo_name(repo_type, repo_id, generation=generation)
        for generation in range(max_generations)
    ]
    candidates += [
        # The "r" prefix keeps random tokens apart from sequential generations.
        lakefs_repo_name(repo_type, repo_id, generation=f"r{secrets.token_hex(8)}")
        for _ in range(MAX_LAKEFS_REPO_RANDOM_DRAWS)
    ]
    for candidate in candidates:
        if candidate in exclude:
            continue
        if not await client.repository_exists(candidate):
            return candidate
    raise RuntimeError(
        f"No free LakeFS repository id for {repo_type}:{repo_id} after "
        f"{max_generations} generations and {MAX_LAKEFS_REPO_RANDOM_DRAWS} random draws"
    )


if __name__ == "__main__":
    print(
        lakefs_repo_name(
            "model", "org/very-long-repository-name-with-version-v2.3.4-final"
        )
    )
