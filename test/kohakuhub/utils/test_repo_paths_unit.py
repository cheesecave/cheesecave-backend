"""The limit on a path inside a repository is counted in UTF-8 bytes."""

from kohakuhub.utils.repo_paths import MAX_PATH_BYTES, too_long


def test_the_limit_counts_bytes_not_characters():
    assert MAX_PATH_BYTES == 1024
    assert not too_long("a" * 1024)
    assert too_long("a" * 1025)
    assert not too_long("長" * 341)  # 1023 bytes
    assert too_long("長" * 342)  # 1026 bytes, 342 characters
    assert not too_long("_pathtest3/" + "x" * 250 + ".txt")  # 265 characters
