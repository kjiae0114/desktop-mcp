import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

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


# ---- 잠근 테스트, 반복 감지, 결과 표시 ----

NEW_TEST = "from mod import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
MOD_WITH_MUL = (
    "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
)
FIX_MUL = call("write_file", path="mod.py", content=MOD_WITH_MUL)


@pytest.fixture
def locked_repo(repo):
    # 커밋하지 않은 새 테스트: 작업 폴더에만 있고 시작 시점에는 빨간 상태다.
    (repo / "test_new.py").write_text(NEW_TEST, encoding="utf-8")
    return repo


def worktree_dirs(runs_dir: Path) -> list[Path]:
    return list(runs_dir.glob("*/worktree"))


def last_tool_output(messages: list[dict]) -> str:
    return [m["content"] for m in messages if m["role"] == "tool"][-1]


def test_locked_test_starts_red_then_commits_with_test(project, locked_repo, runs_dir):
    repo = locked_repo
    llm = FakeLLM(call("read_file", path="test_new.py"), FIX_MUL, done())

    result, _ = run(project, runs_dir, llm, protected_tests=["test_new.py"])

    assert result.status == agent.SUCCEEDED
    assert sorted(result.changed_files) == ["mod.py", "test_new.py"]
    assert result.tests_changed == []
    assert "mul" in git(repo, "show", f"{result.branch}:test_new.py")
    assert "mul" in git(repo, "show", f"{result.branch}:mod.py")
    # 메인 작업 트리는 그대로다.
    assert git(repo, "status", "--porcelain").strip() == "?? test_new.py"
    assert "mul" not in (repo / "mod.py").read_text(encoding="utf-8")
    assert "테스트 파일은 고칠 수 없습니다" in llm.seen[0][0]["content"]
    assert "- test_new.py" in llm.seen[0][1]["content"]
    assert "def test_mul" in last_tool_output(llm.seen[1])
    assert_clean_worktrees(repo)


def test_without_locked_tests_prompt_is_unchanged(project, runs_dir):
    llm = FakeLLM(REFACTOR, done())

    run(project, runs_dir, llm)

    assert "고칠 수 없습니다" not in llm.seen[0][0]["content"]
    assert llm.seen[0][1]["content"] == "add를 정리"


@pytest.mark.parametrize(
    "path",
    [
        "test_new.py",
        pytest.param(
            "TEST_NEW.PY",
            marks=pytest.mark.skipif(os.name != "nt", reason="대소문자를 구분하지 않는 파일 시스템 전용"),
        ),
        "./test_new.py",
    ],
)
def test_locked_file_cannot_be_modified(project, locked_repo, runs_dir, path):
    llm = FakeLLM(
        call("write_file", path=path, content="TAMPERED\n"),
        call("replace_in_file", path=path, old="mul", new="TAMPERED"),
        call("read_file", path=path),
        done(),
    )

    result, _ = run(
        project, runs_dir, llm, protected_tests=["test_new.py"], max_fix_attempts=0
    )

    outputs = [m["content"] for m in llm.seen[-1] if m["role"] == "tool"]
    assert outputs[0].startswith("오류") and "수정할 수 없습니다" in outputs[0]
    assert "수정할 수 없습니다" in outputs[1]
    assert outputs[2] == NEW_TEST
    assert result.status == agent.FAILED
    assert "TAMPERED" not in result.diff_path.read_text(encoding="utf-8")
    assert (locked_repo / "test_new.py").read_text(encoding="utf-8") == NEW_TEST
    assert_clean_worktrees(locked_repo)


@pytest.mark.parametrize(
    "bad", ["missing_test.py", ".env", "../outside.py", "sub", "a/../../x.py"]
)
def test_invalid_locked_paths_do_not_create_worktree(project, repo, runs_dir, tmp_path, bad):
    (repo / ".env").write_text("K=1", encoding="utf-8")
    (repo / "sub").mkdir()
    (tmp_path / "outside.py").write_text("x", encoding="utf-8")
    llm = FakeLLM()

    result, _ = run(project, runs_dir, llm, protected_tests=["test_mod.py", bad])

    assert result.status == agent.FAILED
    assert llm.seen == []
    assert branches(repo) == []
    assert worktree_dirs(runs_dir) == []
    assert_clean_worktrees(repo)


def test_locked_run_tests_runs_only_locked_but_final_check_is_full(
    project, locked_repo, runs_dir
):
    broken_other = call(
        "write_file", path="test_other.py", content="def test_other():\n    assert False\n"
    )
    llm = FakeLLM(FIX_MUL, broken_other, call("run_tests"), done())

    result, _ = run(
        project, runs_dir, llm, protected_tests=["test_new.py"], max_fix_attempts=0
    )

    # 도구 결과: 잠근 테스트 하나만 실행되어 통과한다.
    assert len(llm.seen) == 4
    output = last_tool_output(llm.seen[3])
    assert "1 passed" in output and "failed" not in output
    # 최종 검증은 전체 테스트라 test_other 실패가 드러난다.
    assert result.status == agent.FAILED
    assert "계속 실패" in result.message
    assert branches(locked_repo) == []
    assert_clean_worktrees(locked_repo)


def test_locked_tests_with_subdirectory_cwd(repo, runs_dir):
    (repo / "pkg").mkdir()
    (repo / "pkg" / "test_base.py").write_text(
        "def test_base():\n    assert True\n", encoding="utf-8"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pkg")
    (repo / "pkg" / "test_sub.py").write_text("def test_sub():\n    assert True\n", encoding="utf-8")
    spec = CommandSpec(sys.executable, ("-m", "pytest"), frozenset({"-q", "--tb=short"}))
    sub_project = Project("demo", repo, "pkg", {"pytest": spec})
    llm = FakeLLM(call("write_file", path="pkg/x.py", content="X = 1\n"), call("run_tests"), done())

    result, _ = run(sub_project, runs_dir, llm, protected_tests=["pkg/test_sub.py"])

    assert "1 passed" in last_tool_output(llm.seen[2])
    assert result.status == agent.SUCCEEDED


def test_only_locked_test_changed_is_no_changes(project, repo, runs_dir):
    # 코드를 고치지 않았는데 이미 통과하는 잠근 테스트 파일만 커밋되면 안 된다.
    (repo / "test_new.py").write_text("def test_new():\n    assert True\n", encoding="utf-8")

    result, _ = run(project, runs_dir, FakeLLM(done("할 일 없음")), protected_tests=["test_new.py"])

    assert result.status == agent.NO_CHANGES
    assert branches(repo) == []
    assert git(repo, "log", "--oneline").count("\n") == 1
    assert_clean_worktrees(repo)


def test_tampered_locked_file_is_not_committed(project, locked_repo, runs_dir):
    class TamperingLLM(FakeLLM):
        def chat(self, messages, tools):
            if len(self.seen) == 1:
                # 코드 수정 뒤, 모델의 최종 답 직전에 잠근 파일이 몰래 바뀐 상황
                for path in runs_dir.glob("*/worktree/test_new.py"):
                    path.write_text("def test_weak():\n    assert True\n", encoding="utf-8")
            return super().chat(messages, tools)

    llm = TamperingLLM(FIX_MUL, done())

    result, _ = run(project, runs_dir, llm, protected_tests=["test_new.py"])

    assert result.status == agent.FAILED
    assert "잠근 테스트가 변경" in result.message
    assert branches(locked_repo) == []
    assert git(locked_repo, "log", "--oneline").count("\n") == 1
    assert_clean_worktrees(locked_repo)


def test_repeated_identical_edit_warns_then_aborts(project, repo, runs_dir):
    llm = FakeLLM(BREAK, BREAK, BREAK, done())

    result, _ = run(project, runs_dir, llm)

    assert result.status == agent.FAILED
    assert result.message == "같은 수정을 반복해 중단했습니다."
    assert len(llm.seen) == 3
    assert agent.REPEAT_WARNING not in last_tool_output(llm.seen[1])
    assert agent.REPEAT_WARNING in last_tool_output(llm.seen[2])
    assert branches(repo) == []
    assert_clean_worktrees(repo)


def test_content_round_trip_is_detected(project, repo, runs_dir):
    state_a = "def add(a, b):\n    return a + b\n"
    state_b = "def add(a, b):\n    return a - b\n"
    llm = FakeLLM(
        call("replace_in_file", path="mod.py", old="a + b", new="a - b"),  # B 1회
        call("write_file", path="mod.py", content=state_a),  # A 1회
        call("write_file", path="mod.py", content=state_b),  # B 2회: 경고
        call("replace_in_file", path="mod.py", old="return a - b", new="return a + b"),  # A 2회
        call("replace_in_file", path="mod.py", old="return a + b", new="return a - b"),  # B 3회
        done(),
    )

    result, _ = run(project, runs_dir, llm)

    assert result.status == agent.FAILED
    assert result.message == "같은 수정을 반복해 중단했습니다."
    assert len(llm.seen) == 5
    assert agent.REPEAT_WARNING not in last_tool_output(llm.seen[2])
    assert agent.REPEAT_WARNING in last_tool_output(llm.seen[3])
    assert_clean_worktrees(repo)


def test_failure_reports_staged_changed_files(project, runs_dir):
    result, _ = run(project, runs_dir, FakeLLM(BREAK, done(), done()), max_fix_attempts=0)

    assert result.status == agent.FAILED
    assert result.changed_files == ["mod.py"]


def test_no_changes_has_empty_changed_files(project, runs_dir):
    result, _ = run(project, runs_dir, FakeLLM(done()))

    assert result.status == agent.NO_CHANGES
    assert result.changed_files == [] and result.tests_changed == []


def test_tests_changed_lists_model_edited_tests(project, runs_dir):
    llm = FakeLLM(
        call("write_file", path="tests/helper.py", content="X = 1\n"),
        call("write_file", path="test_extra.py", content="def test_x():\n    assert True\n"),
        call("write_file", path="docs.py", content="Y = 1\n"),
        done(),
    )

    result, _ = run(project, runs_dir, llm)

    assert result.status == agent.SUCCEEDED
    assert sorted(result.tests_changed) == ["test_extra.py", "tests/helper.py"]


def _cli(monkeypatch, result):
    from dev_automation import __main__ as cli

    project = SimpleNamespace(sandbox=None)
    monkeypatch.setattr(cli, "load_projects", lambda path: {"demo": project})
    monkeypatch.setattr(cli, "OllamaClient", lambda model: SimpleNamespace(unload=lambda: None))
    captured = {}

    def fake_run_task(project, task, llm, runs_dir, **kwargs):
        captured.update(kwargs)
        return result

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    return cli, captured


def test_cli_warns_about_changed_tests_and_passes_locked(monkeypatch, capsys):
    result = agent.AgentResult(
        agent.SUCCEEDED, "agent/x", "ok", "abc", ["test_a.py"], tests_changed=["test_a.py"]
    )
    cli, captured = _cli(monkeypatch, result)

    code = cli.main(
        ["agent", "demo", "작업", "--unsafe-host-tests", "--test", "t/a.py", "--test", "t/b.py"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert captured["protected_tests"] == ["t/a.py", "t/b.py"]
    assert "경고: 모델이 수정한 테스트가 있습니다. 테스트를 약화하지 않았는지 검토하세요." in out
    assert "test_a.py" in out


def test_cli_without_changed_tests_has_no_warning(monkeypatch, capsys):
    cli, captured = _cli(monkeypatch, agent.AgentResult(agent.FAILED, "agent/x", "실패"))

    code = cli.main(["agent", "demo", "작업", "--unsafe-host-tests"])

    assert code == 1
    assert captured["protected_tests"] == []
    assert "모델이 수정한 테스트가 있습니다" not in capsys.readouterr().out


def test_cancel_from_progress_stops_and_cleans_up(project, repo, runs_dir):
    seen = []

    def progress(message):
        seen.append(message)
        if message.startswith("도구:") and not any(m == "취소됨" for m in seen):
            seen.append("취소됨")
            raise agent.TaskCancelled("사용자가 취소했습니다.")

    llm = FakeLLM(call("read_file", path="mod.py"), BREAK, done())
    result = run_task(project, "add를 정리", llm, runs_dir, on_progress=progress)

    assert result.status == agent.FAILED
    assert result.message == "사용자가 취소했습니다."
    assert len(llm.replies) == 2  # 첫 도구 호출에서 멈춤
    assert branches(repo) == []
    assert_clean_worktrees(repo)
