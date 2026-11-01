import os
import subprocess

import pytest

from dev_automation.agent_tools import MAX_RESULT_CHARS, TOOL_SCHEMAS, ToolBox, is_blocked
from dev_automation.runner import SUCCEEDED, RunResult


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "proj"
    (path / "src").mkdir(parents=True)
    (path / "src" / "a.py").write_text("x = 1\ny = 1\n", encoding="utf-8")
    (path / ".git").mkdir()
    (path / ".git" / "config").write_text("secret", encoding="utf-8")
    (path / ".env").write_text("KEY=1", encoding="utf-8")
    (path / "id.PEM").write_text("k", encoding="utf-8")
    return path


@pytest.fixture
def box(root):
    return ToolBox(root, lambda: RunResult("pytest", SUCCEEDED, 0, stdout="1 passed"))


def test_schemas_cover_all_tools():
    names = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
    assert names == {"list_files", "read_file", "write_file", "replace_in_file", "run_tests"}


def test_list_files_hides_blocked(box):
    listing = box.call("list_files", {"path": "."})

    assert "src/" in listing
    assert ".git" not in listing and ".env" not in listing and "id.PEM" not in listing


def test_read_file(box):
    assert box.call("read_file", {"path": "src/a.py"}) == "x = 1\ny = 1\n"


def test_read_file_size_limit(box, root):
    (root / "big.txt").write_bytes(b"a" * (200 * 1024 + 1))

    assert "오류" in box.call("read_file", {"path": "big.txt"})


def test_write_file_creates_parents(box, root):
    box.call("write_file", {"path": "new/dir/b.py", "content": "z = 2\n"})

    assert (root / "new" / "dir" / "b.py").read_text(encoding="utf-8") == "z = 2\n"


def test_write_file_size_limit(box, root):
    result = box.call("write_file", {"path": "big.txt", "content": "a" * (1024 * 1024 + 1)})

    assert "오류" in result
    assert not (root / "big.txt").exists()


def test_replace_requires_exactly_one_match(box, root):
    assert "2번" in box.call("replace_in_file", {"path": "src/a.py", "old": "1", "new": "2"})
    assert "0번" in box.call("replace_in_file", {"path": "src/a.py", "old": "q", "new": "2"})
    assert (root / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\ny = 1\n"

    box.call("replace_in_file", {"path": "src/a.py", "old": "x = 1", "new": "x = 9"})
    assert (root / "src" / "a.py").read_text(encoding="utf-8") == "x = 9\ny = 1\n"


def test_replace_preserves_crlf(box, root):
    (root / "w.txt").write_bytes(b"a\r\nb\r\n")

    box.call("replace_in_file", {"path": "w.txt", "old": "a", "new": "c"})

    assert (root / "w.txt").read_bytes() == b"c\r\nb\r\n"


@pytest.mark.parametrize(
    "path",
    ["../x.txt", "src/../../x.txt", "/abs.txt", "C:/x.txt", "C:\\x.txt", "a:b.txt", "..\\x.txt"],
)
def test_escaping_paths_rejected(box, root, path):
    assert box.call("write_file", {"path": path, "content": "x"}).startswith("오류")
    assert box.call("read_file", {"path": path}).startswith("오류")
    assert not (root.parent / "x.txt").exists()


@pytest.mark.parametrize(
    "path",
    [
        ".git/config", ".GIT/config", ".env", ".ENV.local", ".env.example", "id.PEM", "k.key",
        "src/__pycache__/x", ".venv/a", ".venv-new/a", "runs/a", "a/Runs/b",
    ],
)
def test_blocked_paths_rejected(box, root, path):
    assert is_blocked(path)
    assert box.call("read_file", {"path": path}).startswith("오류")
    assert box.call("write_file", {"path": path, "content": "x"}).startswith("오류")
    assert (root / ".git" / "config").read_text(encoding="utf-8") == "secret"
    assert not (root / "runs").exists()
    assert not (root / "src" / "__pycache__").exists()


def test_result_is_truncated(box, root):
    (root / "long.txt").write_text("a" * 20000, encoding="utf-8")

    result = box.call("read_file", {"path": "long.txt"})

    assert "잘렸" in result
    assert len(result) < MAX_RESULT_CHARS + 100


def test_unknown_tool_and_bad_arguments_return_errors(box):
    assert box.call("rm", {}).startswith("오류")
    assert box.call("read_file", {"nope": 1}).startswith("오류")
    assert box.call("read_file", {"path": 5}).startswith("오류")
    assert box.call("read_file", {"path": "missing.py"}).startswith("오류")


def test_run_tests_returns_output(box):
    assert "1 passed" in box.call("run_tests", {})


@pytest.mark.skipif(os.name != "nt", reason="정션 테스트는 Windows 전용")
def test_junction_escape_rejected(box, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)],
        check=True,
        capture_output=True,
    )

    assert box.call("write_file", {"path": "link/x.txt", "content": "x"}).startswith("오류")
    assert not (outside / "x.txt").exists()


@pytest.mark.parametrize(
    "path",
    [
        "src/a.py",
        pytest.param(
            "SRC/A.PY",
            marks=pytest.mark.skipif(os.name != "nt", reason="대소문자를 구분하지 않는 파일 시스템 전용"),
        ),
        pytest.param(
            "src\\a.py",
            marks=pytest.mark.skipif(os.name != "nt", reason="역슬래시 구분자는 Windows 전용"),
        ),
        "./src/a.py",
    ],
)
def test_protected_file_is_readable_but_not_writable(root, path):
    box = ToolBox(root, lambda: None, ["src/a.py"])

    assert box.call("read_file", {"path": path}) == "x = 1\ny = 1\n"
    for result in (
        box.call("write_file", {"path": path, "content": "z"}),
        box.call("replace_in_file", {"path": path, "old": "x = 1", "new": "x = 2"}),
    ):
        assert result == "오류: 이 테스트는 수정할 수 없습니다. 코드를 고치세요."
    assert (root / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\ny = 1\n"


def test_unprotected_files_remain_writable(root):
    box = ToolBox(root, lambda: None, ["src/a.py"])

    box.call("write_file", {"path": "src/b.py", "content": "q"})

    assert (root / "src" / "b.py").read_text(encoding="utf-8") == "q"
