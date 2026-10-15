"""The limit on a path inside a repository is counted in UTF-8 bytes; what a path may not be (#99)."""

from kohakuhub.utils.repo_paths import MAX_PATH_BYTES, path_problem, too_long


def test_the_limit_counts_bytes_not_characters():
    assert MAX_PATH_BYTES == 1024
    assert not too_long("a" * 1024)
    assert too_long("a" * 1025)
    assert not too_long("長" * 341)  # 1023 bytes
    assert too_long("長" * 342)  # 1026 bytes, 342 characters
    assert not too_long("_pathtest3/" + "x" * 250 + ".txt")  # 265 characters


def test_a_control_character_is_refused_in_any_path():
    for path in ("a\x00b", "\x00", "a\nb", "a\tb", "a\x1fb", "a\x7fb"):
        for creating in (True, False):
            assert "control character" in path_problem(path, creating=creating), (path, creating)


def test_what_only_a_creating_operation_may_not_be():
    for path in (".", "..", "../x", "a/../b", "a/b/..", ".git", ".git/x", "a/.git", "a/.GIT/b", "a/.Git/b/c"):
        assert path_problem(path, creating=True), path
        assert path_problem(path, creating=False) is None, path  # a delete may still clear what is stored


def test_what_is_kept():
    for path in (
        "",
        "a",
        "a/b.txt",
        "a..b",
        "..a",
        "a../b",
        "...",
        "a/.../b",
        ".gitignore",
        ".gitattributes",
        ".github/workflows/ci.yml",
        ".gitkeep",
        "a.git/x",
        "git/x",
        ".cache/x",
        ".hidden",
        "a//b",
        "a/./b",
        "./a",
        "/a",
        "a/",
        "a b",
        "a\\b",
    ):
        for creating in (True, False):
            assert path_problem(path, creating=creating) is None, (path, creating)


def test_what_is_wrong_is_said_without_the_path():
    assert ".." in path_problem("../x", creating=True)
    assert ".git" in path_problem(".git/x", creating=True)
    assert "'.'" in path_problem(".", creating=True)
