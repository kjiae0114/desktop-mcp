"""정답지 먼저 쓰기: 요구사항 문장만 보고 테스트를 먼저 만든다. 구현은 그 테스트를 잠근 뒤에 한다.

같은 모델이라도 '테스트만 쓰는 단계'와 '구현하는 단계'를 나누면, 자기 코드에 맞춘 테스트를
쓰는 일을 막을 수 있다. 만든 테스트는 커밋하지 않은 새 파일로 두고 run_task의 protected_tests로 넘긴다.
"""

import ast
from pathlib import Path
import re

from dev_automation.context import build_context
from dev_automation.llm import LLMClient

DEFAULT_SPEC_PATH = "tests/test_spec_generated.py"
SPEC_SYSTEM = (
    "당신은 요구사항을 pytest 테스트로 옮기는 QA 엔지니어입니다. 구현 코드는 절대 쓰지 않습니다.\n"
    "- 요구사항의 조건 하나하나를 각각 테스트 함수로 확인합니다. 예외, 기본값, 정렬, 빈 경우, 출력 문구를 빠뜨리지 마세요.\n"
    "- 저장소 지도에 있는 실제 모듈 경로와 함수·클래스 이름, 인자 이름을 그대로 씁니다. 요구사항에 나온 새 이름은 그대로 import합니다.\n"
    "- 파일이 필요하면 pytest의 tmp_path를 씁니다. 네트워크와 외부 패키지는 쓰지 않습니다.\n"
    "- 답은 ```python 코드 블록 하나만 출력합니다."
)
_BLOCK = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


class SpecError(RuntimeError):
    """쓸 만한 테스트를 만들지 못했을 때 발생한다."""


def _extract(content: str) -> str:
    blocks = _BLOCK.findall(content)
    return (max(blocks, key=len) if blocks else content).strip() + "\n"


def _valid(code: str) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"구문 오류: {exc}"
    tests = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    if not tests:
        return "test_로 시작하는 함수가 없습니다."
    return None


def write_spec_tests(
    llm: LLMClient,
    root: Path,
    task: str,
    relative: str = DEFAULT_SPEC_PATH,
    context_chars: int = 8000,
    attempts: int = 2,
) -> str:
    """요구사항으로 테스트 파일을 만들어 root/relative에 쓰고 relative를 돌려준다."""
    root = Path(root)
    target = root / relative
    if target.exists():
        raise SpecError(f"이미 있는 파일입니다: {relative}")
    messages = [
        {"role": "system", "content": SPEC_SYSTEM},
        {"role": "user", "content": f"요구사항:\n{task}\n\n{build_context(root, [], context_chars)}"},
    ]
    problem = "응답이 없습니다."
    for _ in range(attempts):
        reply = llm.chat(messages, [])
        code = _extract(reply.get("content") or "")
        problem = _valid(code)
        if problem is None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
            return relative
        messages += [
            {"role": "assistant", "content": reply.get("content") or ""},
            {"role": "user", "content": f"쓸 수 없는 답입니다({problem}). ```python 코드 블록 하나로 다시 쓰세요."},
        ]
    raise SpecError(f"테스트를 만들지 못했습니다: {problem}")
