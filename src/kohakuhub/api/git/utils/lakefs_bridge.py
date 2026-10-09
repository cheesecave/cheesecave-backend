"""Git-LakeFS bridge - Pure Python implementation (no pygit2, no file I/O)."""

from datetime import datetime
import asyncio
import fnmatch
import hashlib

from kohakuhub.config import cfg
from kohakuhub.constants import (
    DEFAULT_COMMIT_MESSAGE,
    DEFAULT_EMAIL,
    GIT_ATTRIBUTES_FILE,
)
from kohakuhub.db import File
from kohakuhub.db_operations import get_repository, should_use_lfs
from kohakuhub.logger import get_logger
from kohakuhub.utils.lakefs import get_lakefs_client
from kohakuhub.api.git.utils.objects import (
    build_nested_trees,
    create_blob_object,
    create_commit_object,
    create_pack_file,
)
from kohakuhub.api.git.utils.server import create_empty_pack

logger = get_logger("GIT_LAKEFS")


def create_lfs_pointer(sha256: str, size: int) -> bytes:
    """Create Git LFS pointer file content."""
    pointer = f"""version https://git-lfs.github.com/spec/v1
oid sha256:{sha256}
size {size}
"""
    return pointer.encode("utf-8")


def generate_lfsconfig(base_url: str, namespace: str, name: str) -> bytes:
    """Generate .lfsconfig."""
    lfs_url = f"{base_url}/{namespace}/{name}.git/info/lfs"
    config = f"""[lfs]
\turl = {lfs_url}
"""
    return config.encode("utf-8")


class GitLakeFSBridge:
    """Git-LakeFS bridge - Pure Python, in-memory only (no pygit2, no temp files)."""

    def __init__(
        self,
        repo_type: str,
        namespace: str,
        name: str,
        lakefs_repo: str,
    ):
        # Pass the id resolved from the Repository row (`resolve_lakefs_repo`). Deriving
        # it here would address the generation-0 repository, which is wrong for any row
        # allocated later, so an empty id is refused rather than replaced.
        if not lakefs_repo:
            raise ValueError("lakefs_repo is required; resolve it with resolve_lakefs_repo")
        self.repo_type = repo_type
        self.namespace = namespace
        self.name = name
        self.repo_id = f"{namespace}/{name}"
        self.lakefs_repo = lakefs_repo
        self.lakefs_client = get_lakefs_client()

    async def get_refs(self, branch: str = "main") -> dict[str, str]:
        """Get Git refs - pure logical, no temp files."""
        try:
            # Get branch info
            branch_info = await self.lakefs_client.get_branch(
                repository=self.lakefs_repo, branch=branch
            )

            commit_id = branch_info.get("commit_id")
            if not commit_id:
                return {}

            # Build commit SHA-1 in memory
            commit_sha1 = await self._build_commit_sha1(branch, commit_id)

            if not commit_sha1:
                return {}

            return {
                f"refs/heads/{branch}": commit_sha1,
                "HEAD": commit_sha1,
            }

        except Exception as e:
            logger.exception(f"Failed to get refs for {self.repo_id}", e)
            return {}

    async def _build_commit_sha1(self, branch: str, commit_id: str) -> str | None:
        """Build Git commit SHA-1 purely in memory - no files created."""
        try:
            # Step 1: Get all objects from LakeFS
            all_objects = []
            after = ""
            has_more = True

            while has_more:
                result = await self.lakefs_client.list_objects(
                    repository=self.lakefs_repo,
                    ref=branch,
                    prefix="",
                    after=after,
                    amount=1000,
                )
                all_objects.extend(result.get("results", []))
                pagination = result.get("pagination", {})
                has_more = pagination.get("has_more", False)
                if has_more:
                    after = pagination.get("next_offset", "")

            file_objects = [
                obj for obj in all_objects if obj.get("path_type") == "object"
            ]
            logger.info(f"Found {len(file_objects)} files in LakeFS")

            if not file_objects:
                return None

            # Step 2: Build blob objects (in memory, no download for large files!)
            blob_data = await self._build_blob_sha1s(file_objects, branch)

            if not blob_data:
                return None

            # Step 3: Build nested tree structure (pure logic, no I/O)
            flat_entries = [
                (mode, path, sha1) for path, (sha1, _, mode) in blob_data.items()
            ]
            root_tree_sha1, tree_objects = build_nested_trees(flat_entries)

            logger.success(
                f"Built tree structure: {len(tree_objects)} tree objects, root={root_tree_sha1[:8]}"
            )

            # Step 4: Build commit object (in memory)
            commit_info = await self.lakefs_client.get_commit(
                repository=self.lakefs_repo, commit_id=commit_id
            )

            author_name = commit_info.get("committer", "KohakuHub")
            message = commit_info.get("message", DEFAULT_COMMIT_MESSAGE)
            timestamp = commit_info.get("creation_date", 0)

            # Parse timestamp
            if isinstance(timestamp, str):
                try:
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    timestamp = int(dt.timestamp())
                except (ValueError, AttributeError, TypeError):
                    timestamp = 0

            # Create commit object
            commit_sha1, commit_data = create_commit_object(
                tree_sha1=root_tree_sha1,
                parent_sha1s=[],  # No parents for now
                author_name=author_name,
                author_email=DEFAULT_EMAIL,
                committer_name=author_name,
                committer_email=DEFAULT_EMAIL,
                author_timestamp=timestamp,
                committer_timestamp=timestamp,
                timezone="+0000",
                message=message,
            )

            logger.success(f"Created commit: {commit_sha1}")

            return commit_sha1

        except Exception as e:
            logger.exception("Failed to build commit SHA-1", e)
            return None

    async def _build_blob_sha1s(
        self, file_objects: list[dict], branch: str
    ) -> dict[str, tuple[str, bytes, str]]:
        """Build blob SHA-1s AND data for all files (LFS pointers for large files).

        Returns:
            Dict of path -> (blob_sha1, blob_data, mode)
        """

        # Get repository and File table records for LFS tracking
        repo = get_repository(self.repo_type, self.namespace, self.name)
        if not repo:
            return {}

        file_records = {
            f.path_in_repo: f
            for f in File.select().where(
                (File.repository == repo) & (File.is_deleted == False)
            )
        }

        # Check for .gitattributes and parse LFS patterns
        existing_lfs_patterns = set()
        gitattributes_obj = None
        gitattributes_content = None

        for obj in file_objects:
            if obj["path"] == GIT_ATTRIBUTES_FILE:
                gitattributes_obj = obj
                try:
                    gitattributes_content = await self.lakefs_client.get_object(
                        repository=self.lakefs_repo, ref=branch, path=".gitattributes"
                    )
                    existing_lfs_patterns = self._parse_gitattributes(
                        gitattributes_content.decode("utf-8")
                    )
                    logger.info(
                        f"Found .gitattributes with {len(existing_lfs_patterns)} LFS patterns"
                    )
                except Exception as e:
                    logger.warning(f"Failed to parse .gitattributes: {e}")
                break

        # Classify files
        small_files = []
        large_files = []

        for obj in file_objects:
            if obj["path"] == GIT_ATTRIBUTES_FILE:
                continue

            path = obj["path"]
            size = obj.get("size_bytes", 0)
            file_record = file_records.get(path)

            # Should be LFS if:
            # 1. Marked in File table, OR
            # 2. Size >= threshold OR matches suffix rules, OR
            # 3. Matches existing LFS pattern
            should_be_lfs = (
                (file_record and file_record.lfs)
                or should_use_lfs(repo, path, size)
                or self._matches_pattern(path, existing_lfs_patterns)
            )

            if should_be_lfs:
                large_files.append((obj, file_record))
            else:
                small_files.append((obj, file_record))

        logger.info(
            f"Classified: {len(small_files)} regular files, {len(large_files)} LFS files"
        )

        # Process files concurrently - return (path, sha1, data, mode)
        async def process_small(obj, file_record):
            """Download and create blob."""
            path = obj["path"]
            try:
                content = await self.lakefs_client.get_object(
                    repository=self.lakefs_repo, ref=branch, path=path
                )
                sha1, blob_with_header = create_blob_object(content)
                logger.debug(f"Blob {path}: {len(content)} bytes → {sha1[:8]}")
                return path, sha1, blob_with_header, "100644", False
            except Exception as e:
                logger.warning(f"Failed to download {path}: {e}")
                return None

        async def process_large(obj, file_record):
            """Create LFS pointer blob."""
            path = obj["path"]
            try:
                # Use File table SHA256
                if file_record and file_record.lfs:
                    sha256 = file_record.sha256
                    size = file_record.size
                else:
                    # Fallback to LakeFS stat
                    stat = await self.lakefs_client.stat_object(
                        repository=self.lakefs_repo, ref=branch, path=path
                    )
                    size = stat.get("size_bytes", 0)
                    checksum = stat.get("checksum", "")
                    sha256 = (
                        checksum.replace("sha256:", "")
                        if checksum.startswith("sha256:")
                        else ""
                    )

                    if not sha256:
                        # Last resort: download
                        content = await self.lakefs_client.get_object(
                            repository=self.lakefs_repo, ref=branch, path=path
                        )
                        sha256 = hashlib.sha256(content).hexdigest()
                        size = len(content)

                # Create LFS pointer
                pointer = create_lfs_pointer(sha256, size)
                sha1, blob_with_header = create_blob_object(pointer)
                logger.debug(f"LFS {path}: {size} bytes → pointer {sha1[:8]}")
                return path, sha1, blob_with_header, "100644", True
            except Exception as e:
                logger.warning(f"Failed to create LFS pointer for {path}: {e}")
                return None

        # Process concurrently
        small_results = await asyncio.gather(
            *[process_small(obj, rec) for obj, rec in small_files]
        )
        large_results = await asyncio.gather(
            *[process_large(obj, rec) for obj, rec in large_files]
        )

        # Collect results
        blob_data = {}  # path -> (sha1, blob_data_with_header, mode)
        lfs_paths = []

        for result in small_results:
            if result:
                path, sha1, data, mode, is_lfs = result
                blob_data[path] = (sha1, data, mode)

        for result in large_results:
            if result:
                path, sha1, data, mode, is_lfs = result
                blob_data[path] = (sha1, data, mode)
                lfs_paths.append(path)

        # Add .gitattributes
        if gitattributes_obj and gitattributes_content:
            sha1, blob_with_header = create_blob_object(gitattributes_content)
            blob_data[".gitattributes"] = (sha1, blob_with_header, "100644")
        elif lfs_paths:
            gitattributes = self._generate_gitattributes(lfs_paths)
            sha1, blob_with_header = create_blob_object(gitattributes)
            blob_data[".gitattributes"] = (sha1, blob_with_header, "100644")

        # Add .lfsconfig
        if lfs_paths:
            lfsconfig = generate_lfsconfig(cfg.app.base_url, self.namespace, self.name)
            sha1, blob_with_header = create_blob_object(lfsconfig)
            blob_data[".lfsconfig"] = (sha1, blob_with_header, "100644")

        return blob_data

    def _parse_gitattributes(self, content: str) -> set[str]:
        """Parse LFS patterns from .gitattributes."""
        patterns = set()
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "filter=lfs" in line:
                parts = line.split()
                if parts:
                    patterns.add(parts[0])
        return patterns

    def _matches_pattern(self, path: str, patterns: set[str]) -> bool:
        """Check if path matches any LFS pattern."""
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
            # Also check basename for *.ext patterns
            if pattern.startswith("*."):
                basename = path.split("/")[-1]
                if fnmatch.fnmatch(basename, pattern):
                    return True
        return False

    def _generate_gitattributes(self, lfs_paths: list[str]) -> bytes:
        """Generate .gitattributes for specific LFS files."""
        lines = ["# Git LFS tracking (auto-generated)\n\n"]
        for path in sorted(lfs_paths):
            lines.append(f"{path} filter=lfs diff=lfs merge=lfs -text\n")
        return "".join(lines).encode("utf-8")

    async def build_pack_file(
        self, wants: list[str], haves: list[str], branch: str = "main"
    ) -> bytes:
        """Build Git pack file - pure in-memory, no temp dirs.

        Args:
            wants: Commit SHAs client wants
            haves: Commit SHAs client has (ignored for now)
            branch: Branch name

        Returns:
            Pack file bytes
        """
        try:
            # Get all objects from LakeFS
            all_objects = []
            after = ""
            has_more = True

            while has_more:
                result = await self.lakefs_client.list_objects(
                    repository=self.lakefs_repo,
                    ref=branch,
                    prefix="",
                    after=after,
                    amount=1000,
                )
                all_objects.extend(result.get("results", []))
                pagination = result.get("pagination", {})
                has_more = pagination.get("has_more", False)
                if has_more:
                    after = pagination.get("next_offset", "")

            file_objects = [
                obj for obj in all_objects if obj.get("path_type") == "object"
            ]

            if not file_objects:
                logger.warning("No files found, returning empty pack")
                return create_empty_pack()

            # Build blob objects with data (in memory, LFS pointers for large files!)
            blob_data = await self._build_blob_sha1s(file_objects, branch)

            if not blob_data:
                logger.warning("No blobs created, returning empty pack")
                return create_empty_pack()

            # Build tree objects (pure logic, no I/O)
            flat_entries = [
                (mode, path, sha1) for path, (sha1, _, mode) in blob_data.items()
            ]
            root_tree_sha1, tree_objects = build_nested_trees(flat_entries)

            logger.info(
                f"Built {len(tree_objects)} tree objects, root={root_tree_sha1[:8]}"
            )

            # Get branch info for commit
            branch_info = await self.lakefs_client.get_branch(
                repository=self.lakefs_repo, branch=branch
            )

            commit_id = branch_info.get("commit_id")
            if not commit_id:
                logger.warning("No commit_id in branch, using defaults")
                author_name = "KohakuHub"
                message = "Initial commit"
                timestamp = 0
            else:
                # Build commit object
                commit_info = await self.lakefs_client.get_commit(
                    repository=self.lakefs_repo, commit_id=commit_id
                )

                author_name = commit_info.get("committer", "KohakuHub")
                message = commit_info.get("message", DEFAULT_COMMIT_MESSAGE)
                timestamp = commit_info.get("creation_date", 0)

                if isinstance(timestamp, str):
                    try:
                        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        timestamp = int(dt.timestamp())
                    except (ValueError, AttributeError, TypeError):
                        timestamp = 0

            commit_sha1, commit_with_header = create_commit_object(
                tree_sha1=root_tree_sha1,
                parent_sha1s=[],
                author_name=author_name,
                author_email=DEFAULT_EMAIL,
                committer_name=author_name,
                committer_email=DEFAULT_EMAIL,
                author_timestamp=timestamp,
                committer_timestamp=timestamp,
                timezone="+0000",
                message=message,
            )

            # Build pack file (in memory) - ALL objects
            pack_objects = []

            # Add commit (type 1)
            pack_objects.append((1, commit_with_header))

            # Add trees (type 2)
            pack_objects.extend(tree_objects)

            # Add blobs (type 3) - includes LFS pointers!
            for path, (sha1, blob_with_header, mode) in blob_data.items():
                pack_objects.append((3, blob_with_header))

            logger.info(
                f"Pack contains: {len(pack_objects)} objects (1 commit + {len(tree_objects)} trees + {len(blob_data)} blobs)"
            )

            # Create pack
            pack_bytes = create_pack_file(pack_objects)

            logger.success(f"Created pack: {len(pack_bytes)} bytes")

            return pack_bytes

        except Exception as e:
            logger.exception("Failed to build pack file", e)
            return create_empty_pack()
