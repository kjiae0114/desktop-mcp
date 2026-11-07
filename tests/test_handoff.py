"""ChatGPT 반자동 흐름(dev_automation.handoff)과 CLI 연결 테스트."""

from types import SimpleNamespace

import pytest

from dev_automation import __main__ as cli
from dev_automation import agent, handoff

GOOD_REPLY = '''요구사항을 테스트로 옮겼습니다.

```python
# 파일: tests/test_low_stock.py
from inventory.store import Store


def test_low(tmp_path):
    assert Store(tmp_path / "db.json").low_stock(5) == []
```

참고로 기본값은 5로 가정했습니다.
'''


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / "inventory").mkdir(parents=True)
    (root / "inventory" / "store.py").write_text("class Store:\n    pass\n", encoding="utf-8")
    return root


# --- 답장 해석 ----------------------------------------------------------------

def test_parse_reply_with_file_mark():
    files = handoff.parse_reply(GOOD_REPLY)

    assert [path for path, _ in files] == ["tests/test_low_stock.py"]
    assert files[0][1].startswith("# 파일: tests/test_low_stock.py\nfrom inventory.store import Store")


def test_parse_reply_with_heading_and_multiple_files_and_ignores_snippets():
    reply = (
        "예시 사용법:\n```python\nprint('설명용')\n```\n"
        "### tests/test_a.py\n```python\ndef test_a():\n    assert 1\n```\n"
        "```py\n# file: tests/sub/test_b.py\ndef test_b():\n    assert 2\n```\n"
    )

    files = handoff.parse_reply(reply)

    assert [path for path, _ in files] == ["tests/test_a.py", "tests/sub/test_b.py"]


@pytest.mark.parametrize("path", [
    "../evil.py", "tests/../evil.py", "C:/x/tests/test_a.py", "/tests/test_a.py",
    "src/test_a.py", "tests/helper.py", "tests/test_a.txt", "tests/.hidden/test_a.py",
    "tests\\..\\..\\x\\test_a.py",
])
def test_parse_reply_rejects_unsafe_paths(path):
    reply = f"```python\n# 파일: {path}\ndef test_a():\n    assert 1\n```"

    with pytest.raises(handoff.HandoffError):
        handoff.parse_reply(reply)


@pytest.mark.parametrize("reply, message", [
    ("그냥 설명만 있는 답장", "찾지 못했습니다"),
    ("```python\n# 파일: tests/test_a.py\ndef test_a(:\n```", "문법 오류"),
    ("```python\n# 파일: tests/test_a.py\ndef helper():\n    pass\n```", "test_로 시작하는 함수가 없습니다"),
    ("```python\n# 파일: tests/test_a.py\ndef test_a():\n    pass\n```\n"
     "```python\n# 파일: tests/test_a.py\ndef test_b():\n    pass\n```", "두 번"),
])
def test_parse_reply_rejects_bad_content(reply, message):
    with pytest.raises(handoff.HandoffError, match=message):
        handoff.parse_reply(reply)


def test_parse_reply_rejects_huge_file():
    body = "def test_a():\n    assert 1\n" + "# x\n" * 30000
    with pytest.raises(handoff.HandoffError, match="너무 큽니다"):
        handoff.parse_reply(f"```python\n# 파일: tests/test_a.py\n{body}```")


# --- 요청 기록 ---------------------------------------------------------------

def test_create_brief_writes_prompt_with_requirement_and_map(tmp_path):
    root = _project(tmp_path)

    brief_id, prompt = handoff.create_brief(tmp_path / "briefs", "demo", root, "재고 부족 알림")

    path = handoff.brief_dir(tmp_path / "briefs", brief_id)
    assert "재고 부족 알림" in prompt and "class Store:" in prompt
    assert "# 파일: tests/test_이름.py" in prompt
    assert (path / "prompt.md").read_text(encoding="utf-8") == prompt
    assert handoff.load(path)["status"] == "waiting_reply"
    with pytest.raises(handoff.HandoffError):
        handoff.create_brief(tmp_path / "briefs", "demo", root, "   ")


@pytest.mark.parametrize("bad", ["../x", "brief-1", None])
def test_brief_dir_rejects_bad_ids(tmp_path, bad):
    with pytest.raises(handoff.HandoffError):
        handoff.brief_dir(tmp_path, bad)


def test_place_and_remove_tests(tmp_path):
    root = _project(tmp_path)
    files = handoff.parse_reply(GOOD_REPLY)

    written = handoff.place_tests(root, files, set())

    target = root / "tests" / "test_low_stock.py"
    assert written == ["tests/test_low_stock.py"] and target.exists()
    with pytest.raises(handoff.HandoffError, match="이미 있는 파일"):
        handoff.place_tests(root, files, set())
    handoff.place_tests(root, files, {"tests/test_low_stock.py"})  # 이 흐름이 쓴 파일은 덮어쓴다

    handoff.remove_tests(root, written, {"tests/test_low_stock.py": "다른 내용"})
    assert target.exists()  # 사람이 바꾼 것으로 보이면 지우지 않는다
    handoff.remove_tests(root, written, dict(files))
    assert not target.exists()


def test_record_reply_and_retry_round_trip(tmp_path):
    root = _project(tmp_path)
    brief_id, _ = handoff.create_brief(tmp_path / "briefs", "demo", root, "재고 부족 알림")
    path = handoff.brief_dir(tmp_path / "briefs", brief_id)

    record = handoff.record_reply(path, GOOD_REPLY, handoff.parse_reply(GOOD_REPLY))
    text = handoff.write_retry(path, "AttributeError: 'Store' object has no attribute 'low_stock'")

    assert record["round"] == 1 and record["tests"] == ["tests/test_low_stock.py"]
    assert (path / "reply-1.md").exists() and (path / "tests-1" / "tests" / "test_low_stock.py").exists()
    assert "AttributeError" in text and "# 파일: tests/test_low_stock.py" in text and "재고 부족 알림" in text
    assert handoff.latest_prompt(path) == text
    assert handoff.load(path)["status"] == "waiting_reply"


# --- CLI ---------------------------------------------------------------------

def _patch(monkeypatch, tmp_path, run_task, sandbox="img"):
    root = _project(tmp_path)
    project = SimpleNamespace(root=root, sandbox=sandbox)
    monkeypatch.setattr(cli, "BRIEFS_DIR", tmp_path / "briefs")
    monkeypatch.setattr(cli, "load_projects", lambda path: {"demo": project})
    monkeypatch.setattr(cli, "DockerSandbox", lambda image: object())
    monkeypatch.setattr(cli, "OllamaClient", lambda model: SimpleNamespace(unload=lambda: None))
    monkeypatch.setattr(cli, "run_task", run_task)
    monkeypatch.setattr(cli, "_copy_to_clipboard", lambda text: True)
    return root


def _brief(capsys):
    assert cli.main(["brief", "demo", "재고 부족 알림"]) == 0
    out = capsys.readouterr().out
    return out.split("요청문을 만들었습니다: ")[1].split(" ")[0]


def test_cli_implement_success_locks_tests_and_cleans_up(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_run_task(project, task, llm, runs_dir, protected_tests, **kwargs):
        seen["locked"] = list(protected_tests)
        seen["exists"] = (project.root / "tests" / "test_low_stock.py").exists()
        seen["task"] = task
        return agent.AgentResult(agent.SUCCEEDED, "agent/x", "완료", "abc", ["inventory/store.py"])

    root = _patch(monkeypatch, tmp_path, fake_run_task)
    brief_id = _brief(capsys)
    reply = tmp_path / "reply.md"
    reply.write_text(GOOD_REPLY, encoding="utf-8")

    code = cli.main(["implement", brief_id, "--reply", str(reply)])

    assert code == 0
    assert seen == {"locked": ["tests/test_low_stock.py"], "exists": True, "task": "재고 부족 알림"}
    assert not (root / "tests" / "test_low_stock.py").exists()  # 끝나면 치운다(브랜치에는 커밋돼 있음)
    record = handoff.load(handoff.brief_dir(tmp_path / "briefs", brief_id))
    assert record["status"] == "succeeded" and record["branch"] == "agent/x"


def test_cli_implement_failure_writes_retry(monkeypatch, tmp_path, capsys):
    root = _patch(monkeypatch, tmp_path, lambda *a, **k: agent.AgentResult(
        agent.FAILED, "agent/x", "수정 후에도 테스트가 계속 실패했습니다.\nE   AssertionError"))
    brief_id = _brief(capsys)
    monkeypatch.setattr(cli, "_read_clipboard", lambda: GOOD_REPLY)

    code = cli.main(["implement", brief_id])

    out = capsys.readouterr().out
    assert code == 1
    assert f"python -m dev_automation copy {brief_id}" in out
    path = handoff.brief_dir(tmp_path / "briefs", brief_id)
    assert "AssertionError" in handoff.latest_prompt(path)
    assert not (root / "tests" / "test_low_stock.py").exists()

    # 같은 파일 이름으로 다시 답장해도 된다(이 흐름이 전에 쓴 파일)
    assert cli.main(["implement", brief_id]) == 1
    assert handoff.load(path)["round"] == 2


def test_cli_implement_rejects_bad_reply_without_touching_project(monkeypatch, tmp_path, capsys):
    root = _patch(monkeypatch, tmp_path, lambda *a, **k: pytest.fail("실행되면 안 됨"))
    brief_id = _brief(capsys)
    monkeypatch.setattr(cli, "_read_clipboard", lambda: "```python\n# 파일: ../evil.py\ndef test_a():\n    pass\n```")

    assert cli.main(["implement", brief_id]) == 1
    assert "답장을 쓸 수 없습니다" in capsys.readouterr().out
    assert not (root / "tests").exists()


def test_cli_implement_requires_sandbox(monkeypatch, tmp_path, capsys):
    _patch(monkeypatch, tmp_path, lambda *a, **k: pytest.fail("실행되면 안 됨"), sandbox=None)
    brief_id = _brief(capsys)
    monkeypatch.setattr(cli, "_read_clipboard", lambda: GOOD_REPLY)

    assert cli.main(["implement", brief_id]) == 1
    assert "Docker sandbox" in capsys.readouterr().out


def test_cli_copy_and_briefs(monkeypatch, tmp_path, capsys):
    copied = []
    _patch(monkeypatch, tmp_path, lambda *a, **k: None)
    brief_id = _brief(capsys)
    monkeypatch.setattr(cli, "_copy_to_clipboard", lambda text: copied.append(text) or True)

    assert cli.main(["copy", brief_id]) == 0
    assert "재고 부족 알림" in copied[0]
    assert cli.main(["briefs"]) == 0
    assert brief_id in capsys.readouterr().out
