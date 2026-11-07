"""정답지 먼저 쓰기(dev_automation.spec) 테스트."""

import pytest

from dev_automation.spec import DEFAULT_SPEC_PATH, SpecError, write_spec_tests


class ReplyLLM:
    def __init__(self, *contents):
        self.contents = list(contents)
        self.seen = []

    def chat(self, messages, tools):
        self.seen.append((list(messages), tools))
        return {"content": self.contents.pop(0), "tool_calls": []}


GOOD = "```python\nfrom pkg.mod import f\n\n\ndef test_f():\n    assert f() == 1\n```"


def _repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def g():\n    return 0\n", encoding="utf-8")
    return tmp_path


def test_writes_extracted_code_and_sends_requirement_with_map(tmp_path):
    root = _repo(tmp_path)
    llm = ReplyLLM("설명입니다.\n" + GOOD)

    relative = write_spec_tests(llm, root, "f()는 1을 돌려준다")

    assert relative == DEFAULT_SPEC_PATH
    assert (root / relative).read_text(encoding="utf-8").startswith("from pkg.mod import f")
    messages, tools = llm.seen[0]
    assert tools == []  # 테스트만 쓰는 단계에서는 도구를 주지 않는다
    assert "구현 코드는 절대 쓰지 않습니다" in messages[0]["content"]
    assert "f()는 1을 돌려준다" in messages[1]["content"] and "def g()" in messages[1]["content"]


def test_retries_once_on_unusable_answer(tmp_path):
    root = _repo(tmp_path)
    llm = ReplyLLM("```python\ndef helper():\n    pass\n```", GOOD)

    write_spec_tests(llm, root, "요구사항")

    assert "test_로 시작하는 함수가 없습니다" in llm.seen[1][0][-1]["content"]


def test_gives_up_after_attempts(tmp_path):
    root = _repo(tmp_path)
    llm = ReplyLLM("```python\ndef (:\n```", "그냥 글")

    with pytest.raises(SpecError):
        write_spec_tests(llm, root, "요구사항")
    assert not (root / DEFAULT_SPEC_PATH).exists()


def test_refuses_to_overwrite_existing_file(tmp_path):
    root = _repo(tmp_path)
    (root / "tests").mkdir()
    (root / DEFAULT_SPEC_PATH).write_text("x", encoding="utf-8")

    with pytest.raises(SpecError):
        write_spec_tests(ReplyLLM(GOOD), root, "요구사항")
