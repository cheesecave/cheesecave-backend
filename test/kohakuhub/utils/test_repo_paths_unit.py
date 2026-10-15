"""The limit on a path inside a repository is counted in UTF-8 bytes."""

from kohakuhub.utils.repo_paths import MAX_PATH_BYTES, has_parent_segment, too_long


def test_the_limit_counts_bytes_not_characters():
    assert MAX_PATH_BYTES == 1024
    assert not too_long("a" * 1024)
    assert too_long("a" * 1025)
    assert not too_long("長" * 341)  # 1023 bytes
    assert too_long("長" * 342)  # 1026 bytes, 342 characters
    assert not too_long("_pathtest3/" + "x" * 250 + ".txt")  # 265 characters


def test_only_a_segment_that_is_dot_dot_is_a_parent_segment():
    for path in ("..", "../x", "a/../b", "a/b/..", "a/../../b"):
        assert has_parent_segment(path), path
    for path in ("", "a", "a/b.txt", "a..b", "..a", "a../b", "...", "a/.../b", ".gitkeep", "a//b", "/a"):
        assert not has_parent_segment(path), path
