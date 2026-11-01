import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from dev_automation import agent
from dev_automation.agent import _trim_messages, run_task
from dev_automation.llm import LLMError
from dev_automation.projects import Project
from dev_automation.runner import CommandSpec


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout


def call(name: str, **arguments) -> dict:
    return {"content": "", "tool_calls": [{"name": name, "arguments": arguments}]}


def done(text: str = "완료했습니다.") -> dict:
    return {"content": text, "tool_calls": []}


BREAK = call("replace_in_file", path="mod.py", old="a + b", new="a - b")
FIX = call("replace_in_file", path="mod.py", old="a - b", new="b + a")
REFACTOR = call("replace_in_file", path="mod.py", old="a + b", new="b + a")


class FakeLLM:
    """미리 정한 응답을 차례로 돌려주고, 받은 메시지를 기록한다."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def chat(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    (path / "mod.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (path / "test_mod.py").write_text(
        "from mod import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    git(path, "init", "-q")
    git(path, "config", "user.name", "tester")
    git(path, "config", "user.email", "tester@example.com")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def project(repo):
    pytest_spec = CommandSpec(sys.executable, ("-m", "pytest"), frozenset({"-q", "--tb=short"}))
    return Project("demo", repo, ".", {"pytest": pytest_spec})


@pytest.fixture
def runs_dir(tmp_path):
    return tmp_path / "runs"


def run(project, runs_dir, llm, task="add를 정리", **kwargs):
    messages = []
    result = run_task(project, task, llm, runs_dir, on_progress=messages.append, **kwargs)
    return result, messages


def assert_clean_worktrees(repo: Path):
    assert len(git(repo, "worktree", "list").strip().splitlines()) == 1


def branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--list", "agent/*").split()


def test_success_commits_on_branch_only(project, repo, runs_dir):
    head = git(repo, "rev-parse", "HEAD")

    result, _ = run(project, runs_dir, FakeLLM(call("read_file", path="mod.py"), REFACTOR, done()))

    assert result.status == agent.SUCCEEDED
    assert result.changed_files == ["mod.py"]
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == ""
    assert "a + b" in (repo / "mod.py").read_text(encoding="utf-8")
    assert git(repo, "rev-parse", result.branch).strip() == result.commit
    assert git(repo, "log", "-1", "--format=%s", result.branch).startswith("에이전트 작업: add를 정리")
    assert "b + a" in git(repo, "show", f"{result.branch}:mod.py")
    assert "b + a" in result.diff_path.read_text(encoding="utf-8")
    assert (result.diff_path.parent / "conversation.json").exists()
    assert_clean_worktrees(repo)


def test_same_size_edit_in_same_second_is_detected(repo):
    # 소스의 크기와 수정 시각이 그대로면 Python은 옛 .pyc를 실행한다.
    module = repo / "mod.py"
    stat = module.stat()
    check = [sys.executable, "-c", "import mod; print(mod.add(3, 1))"]
    subprocess.run(check, cwd=repo, check=True, capture_output=True)
    module.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    os.utime(module, (stat.st_atime, stat.st_mtime))

    agent._clear_bytecode(repo)

    output = subprocess.run(check, cwd=repo, check=True, capture_output=True, text=True).stdout
    assert output.strip() == "2"


def test_relative_runs_dir_keeps_diff_and_commits(project, repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result, _ = run(project, Path("relative-runs"), FakeLLM(REFACTOR, done()))

    assert result.status == agent.SUCCEEDED
    assert result.diff_path.is_absolute()
    assert "b + a" in result.diff_path.read_text(encoding="utf-8")
    assert_clean_worktrees(repo)


def test_new_files_appear_in_diff_and_commit(project, repo, runs_dir):
    llm = FakeLLM(call("write_file", path="pkg/new.py", content="X = 1\n"), done())

    result, _ = run(project, runs_dir, llm)

    assert result.changed_files == ["pkg/new.py"]
    assert "pkg/new.py" in result.diff_path.read_text(encoding="utf-8")


def test_retry_after_failing_edit(project, repo, runs_dir):
    llm = FakeLLM(BREAK, done("고쳤습니다"), FIX, done("다시 고쳤습니다"))

    result, _ = run(project, runs_dir, llm)

    assert result.status == agent.SUCCEEDED
    feedback = llm.seen[2][-1]
    assert feedback["role"] == "user" and "테스트가 실패" in feedback["content"]
    assert "assert" in feedback["content"]
    assert "b + a" in git(repo, "show", f"{result.branch}:mod.py")
    assert_clean_worktrees(repo)


def test_too_many_failed_fixes(project, repo, runs_dir):
    llm = FakeLLM(BREAK, done(), done(), done())

    result, _ = run(project, runs_dir, llm, max_fix_attempts=1)

    assert result.status == agent.FAILED
    assert branches(repo) == []
    assert "a - b" in result.diff_path.read_text(encoding="utf-8")
    assert_clean_worktrees(repo)


def test_no_changes(project, repo, runs_dir):
    result, _ = run(project, runs_dir, FakeLLM(call("list_files", path="."), done("할 일 없음")))

    assert result.status == agent.NO_CHANGES
    assert branches(repo) == []
    assert result.diff_path.exists()
    assert_clean_worktrees(repo)


def test_baseline_failure(project, repo, runs_dir):
    (repo / "mod.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "깨뜨림")
    llm = FakeLLM()

    result, _ = run(project, runs_dir, llm)

    assert result.status == agent.FAILED
    assert "작업 전부터" in result.message
    assert llm.seen == []
    assert branches(repo) == []
    assert_clean_worktrees(repo)


def test_max_steps_exceeded(project, repo, runs_dir):
    llm = FakeLLM(*[call("list_files", path=".") for _ in range(5)])

    result, _ = run(project, runs_dir, llm, max_steps=3)

    assert result.status == agent.FAILED
    assert "최대 단계" in result.message
    assert len(llm.seen) == 3
    assert branches(repo) == []
    assert_clean_worktrees(repo)


def test_llm_error(project, repo, runs_dir):
    result, _ = run(project, runs_dir, FakeLLM(LLMError("Ollama가 실행 중인지 확인")))

    assert result.status == agent.FAILED
    assert "Ollama" in result.message
    assert branches(repo) == []
    assert_clean_worktrees(repo)


def test_unexpected_exception_still_cleans_up(project, repo, runs_dir):
    result, _ = run(project, runs_dir, FakeLLM(RuntimeError("boom")))

    assert result.status == agent.FAILED
    assert branches(repo) == []
    assert_clean_worktrees(repo)


def test_secret_in_diff_is_not_committed(project, repo, runs_dir):
    token = "ghp_" + "a" * 36
    llm = FakeLLM(call("write_file", path="config.py", content=f"TOKEN = '{token}'\n"), done())

    result, _ = run(project, runs_dir, llm)

    assert result.status == agent.FAILED
    assert "비밀정보" in result.message
    assert branches(repo) == []
    assert git(repo, "log", "--oneline").count("\n") == 1
    assert_clean_worktrees(repo)


def test_path_escape_attempts_are_rejected(project, repo, runs_dir):
    git_config = (repo / ".git" / "config").read_text(encoding="utf-8")
    llm = FakeLLM(
        call("write_file", path="../evil.txt", content="x"),
        call("write_file", path="../../evil.txt", content="x"),
        call("write_file", path=".git/config", content="x"),
        done(),
    )

    result, _ = run(project, runs_dir, llm)

    assert result.status == agent.NO_CHANGES
    assert not list(runs_dir.rglob("evil.txt"))
    assert not (runs_dir.parent / "evil.txt").exists()
    assert (repo / ".git" / "config").read_text(encoding="utf-8") == git_config
    tool_outputs = [m["content"] for m in llm.seen[-1] if m["role"] == "tool"]
    assert len(tool_outputs) == 3 and all(out.startswith("오류") for out in tool_outputs)
    assert_clean_worktrees(repo)


def test_uncommitted_user_changes_are_preserved(project, repo, runs_dir):
    (repo / "mod.py").write_text("def add(a, b):\n    return a + b  # 내 수정\n", encoding="utf-8")
    (repo / "notes.txt").write_text("메모", encoding="utf-8")
    before = git(repo, "status", "--porcelain")

    result, messages = run(project, runs_dir, FakeLLM(REFACTOR, done()))

    assert result.status == agent.SUCCEEDED
    assert git(repo, "status", "--porcelain") == before
    assert "내 수정" in (repo / "mod.py").read_text(encoding="utf-8")
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "메모"
    assert any("경고" in m for m in messages)
    assert_clean_worktrees(repo)


def test_progress_reports_tool_and_path(project, runs_dir):
    _, messages = run(project, runs_dir, FakeLLM(call("read_file", path="mod.py"), done()))

    assert "도구: read_file mod.py" in messages


def test_conversation_is_saved(project, runs_dir):
    result, _ = run(project, runs_dir, FakeLLM(call("read_file", path="mod.py"), done()))

    saved = json.loads((result.diff_path.parent / "conversation.json").read_text(encoding="utf-8"))
    assert [m["role"] for m in saved] == ["system", "user", "assistant", "tool", "assistant"]


def test_trim_keeps_pairs_and_limits_size():
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for index in range(30):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": str(index)}}}]})
        messages.append({"role": "tool", "tool_name": "read_file", "content": "x" * 3000})

    trimmed = _trim_messages(messages)

    assert trimmed[:2] == messages[:2]
    assert trimmed[2]["role"] == "assistant"
    assert trimmed[-1] is messages[-1]
    assert sum(len(json.dumps(m, ensure_ascii=False)) for m in trimmed) <= agent.MAX_CONTEXT_CHARS
    assert len(trimmed) < len(messages)
