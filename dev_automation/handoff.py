"""바깥 AI(ChatGPT 등)에게 테스트 작성을 맡기고, 로컬 에이전트가 구현하는 반자동 흐름.

  brief      요구사항 + 저장소 지도로 요청문을 만든다 → 사람이 ChatGPT에 붙여넣는다
  implement  ChatGPT 답장에서 테스트 파일만 꺼내 검사하고, 잠근 테스트로 로컬 에이전트를 돌린다
  retry      구현이 실패하면 실패 내용을 붙인 재요청문을 만든다(테스트가 틀렸을 수도 있으므로)

바깥 AI가 쓴 테스트는 믿지 않는 입력으로 다룬다:
- tests/ 아래 test_*.py 경로만, 크기 제한, 구문 검사
- 실행은 Docker 샌드박스(네트워크 없음) 안에서만 한다. sandbox가 없는 프로젝트에는 쓰지 않는다.
기록은 runs/briefs/<brief_id>/ 에 남는다(brief.json, prompt.md, reply-N.md, retry-N.md, tests/...).
"""

import ast
from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets

from dev_automation.context import build_context

BRIEF_ID_PATTERN = re.compile(r"brief-\d{8}-\d{6}-[0-9a-f]{4}")
MAX_TEST_BYTES = 100 * 1024
MAX_FILES = 10
FILE_MARK = re.compile(r"^\s*#\s*(?:파일|file)\s*:\s*(\S+)\s*$", re.I)
HEADING_PATH = re.compile(r"(tests/[\w./-]+\.py)")
BLOCK = re.compile(r"```[ \t]*(?:python|py)?[ \t]*\n(.*?)```", re.S)

PROMPT_TEMPLATE = """\
당신은 QA 엔지니어입니다. 아래 요구사항을 검증하는 pytest 테스트 파일만 작성해 주세요.
구현은 제 PC의 다른 프로그램이 이 테스트를 통과하도록 따로 합니다. 그래서 테스트가 곧 명세입니다.

## 규칙
1. 구현 코드는 쓰지 마세요. 테스트 파일만 씁니다.
2. 파일마다 ```python 코드 블록 하나로 쓰고, 블록의 첫 줄에 `# 파일: tests/test_이름.py` 를 적으세요.
   경로는 tests/ 아래 test_로 시작하는 .py만 됩니다. 파일은 1~3개로 충분합니다.
3. 아래 저장소 지도에 있는 실제 모듈 경로, 클래스·함수 이름, 인자 이름을 그대로 쓰세요.
   요구사항에 새로 나온 이름은 요구사항에 적힌 그대로 import하세요.
4. 요구사항의 조건 하나하나를 테스트로 확인하세요(기본값, 예외, 정렬, 빈 경우, 출력 문구 등).
   요구사항에 없는 동작은 테스트하지 마세요. 애매한 부분은 테스트 위에 주석으로 가정을 적으세요.
5. pytest와 표준 라이브러리만 쓰세요. 네트워크, 외부 패키지, 실제 시간·난수에 기대는 테스트는 안 됩니다.
   파일이 필요하면 tmp_path를 쓰세요.
6. 답에는 코드 블록 말고 짧은 설명만 덧붙이세요.

## 요구사항
{requirement}

## 저장소 정보 (프로젝트: {project_id})
{context}
"""

RETRY_TEMPLATE = """\
앞에서 써 주신 테스트로 구현을 시도했지만 실패했습니다. 실패 내용을 보고 판단해 주세요.

- 테스트가 요구사항과 다르거나, 저장소 구조(모듈 경로·이름)와 맞지 않거나, 문법 오류가 있으면
  고친 테스트 파일 전체를 같은 형식(```python 블록, 첫 줄 `# 파일: tests/...`)으로 다시 보내 주세요.
- 테스트가 맞다면 테스트는 그대로 다시 보내 주세요. 구현 쪽을 다시 시도합니다.

## 요구사항
{requirement}

## 지금 테스트
{tests}

## 실패 내용 (로컬 에이전트 결과)
{failure}
"""


class HandoffError(ValueError):
    """요청문·답장·기록을 처리하지 못했을 때 발생한다. 메시지는 사람에게 그대로 보여 준다."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def brief_dir(briefs_dir: Path, brief_id: str) -> Path:
    if not isinstance(brief_id, str) or not BRIEF_ID_PATTERN.fullmatch(brief_id):
        raise HandoffError(f"잘못된 요청 ID입니다: {brief_id!r}")
    path = Path(briefs_dir) / brief_id
    if not (path / "brief.json").is_file():
        raise HandoffError(f"요청 기록을 찾을 수 없습니다: {brief_id}")
    return path


def load(path: Path) -> dict:
    return json.loads((Path(path) / "brief.json").read_text(encoding="utf-8"))


def save(path: Path, record: dict) -> None:
    temp = Path(path) / "brief.json.tmp"
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, Path(path) / "brief.json")


def create_brief(
    briefs_dir: Path, project_id: str, project_root: Path, requirement: str, context_chars: int = 12000
) -> tuple[str, str]:
    """요청문을 만들어 저장하고 (brief_id, 요청문)을 돌려준다."""
    requirement = requirement.strip()
    if not requirement:
        raise HandoffError("요구사항이 비어 있습니다.")
    brief_id = f"brief-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    path = Path(briefs_dir) / brief_id
    path.mkdir(parents=True)
    prompt = PROMPT_TEMPLATE.format(
        requirement=requirement,
        project_id=project_id,
        context=build_context(Path(project_root), [], context_chars),
    )
    (path / "prompt.md").write_text(prompt, encoding="utf-8")
    save(path, {
        "brief_id": brief_id, "project_id": project_id, "requirement": requirement,
        "created_at": _now(), "round": 0, "status": "waiting_reply", "tests": [],
        "latest_prompt": "prompt.md", "history": [],
    })
    return brief_id, prompt


def _safe_test_path(raw: str) -> str:
    path = raw.strip().strip("`'\"").replace("\\", "/")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or re.match(r"^[A-Za-z]:", path):
        raise HandoffError(f"허용되지 않는 경로입니다: {raw}")
    if len(pure.parts) < 2 or pure.parts[0] != "tests":
        raise HandoffError(f"테스트 파일은 tests/ 아래에만 둘 수 있습니다: {raw}")
    if not (pure.name.startswith("test_") and pure.suffix == ".py"):
        raise HandoffError(f"파일 이름은 test_*.py 여야 합니다: {raw}")
    if any(part.startswith(".") for part in pure.parts):
        raise HandoffError(f"숨김 경로는 허용되지 않습니다: {raw}")
    return pure.as_posix()


def parse_reply(text: str) -> list[tuple[str, str]]:
    """답장에서 (경로, 코드) 목록을 꺼낸다. 형식이나 내용이 이상하면 HandoffError."""
    files: list[tuple[str, str]] = []
    for match in BLOCK.finditer(text or ""):
        code = match.group(1)
        lines = code.splitlines()
        path = None
        if lines and FILE_MARK.match(lines[0]):
            path = FILE_MARK.match(lines[0]).group(1)
        else:
            # 블록 바로 위 줄(예: "### tests/test_x.py")에 경로를 적은 경우
            before = text[: match.start()].rstrip().splitlines()[-1:] or [""]
            found = HEADING_PATH.search(before[0])
            path = found.group(1) if found else None
        if path is None:
            continue  # 설명용 코드 조각 등은 무시
        relative = _safe_test_path(path)
        body = code if code.endswith("\n") else code + "\n"
        if len(body.encode("utf-8")) > MAX_TEST_BYTES:
            raise HandoffError(f"테스트 파일이 너무 큽니다(100KB 초과): {relative}")
        try:
            tree = ast.parse(body)
        except SyntaxError as exc:
            raise HandoffError(f"{relative}에 문법 오류가 있습니다: {exc.msg} (줄 {exc.lineno})") from exc
        if not any(isinstance(n, ast.FunctionDef) and n.name.startswith("test_") for n in ast.walk(tree)):
            raise HandoffError(f"{relative}에 test_로 시작하는 함수가 없습니다.")
        if any(relative.lower() == done.lower() for done, _ in files):
            raise HandoffError(f"같은 파일이 두 번 나왔습니다: {relative}")
        files.append((relative, body))
    if not files:
        raise HandoffError(
            "답장에서 테스트 파일을 찾지 못했습니다. ```python 블록 첫 줄에 '# 파일: tests/test_이름.py'가 "
            "있는지 확인하세요."
        )
    if len(files) > MAX_FILES:
        raise HandoffError(f"테스트 파일이 너무 많습니다({len(files)}개, 최대 {MAX_FILES}개).")
    return files


def record_reply(path: Path, reply: str, files: list[tuple[str, str]]) -> dict:
    """답장과 테스트 사본을 기록하고 갱신된 기록을 돌려준다."""
    record = load(path)
    record["round"] += 1
    n = record["round"]
    (path / f"reply-{n}.md").write_text(reply, encoding="utf-8")
    copy_dir = path / f"tests-{n}"
    for relative, body in files:
        target = copy_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    record.update(status="implementing", tests=[relative for relative, _ in files])
    record["history"].append({"round": n, "at": _now(), "tests": record["tests"]})
    save(path, record)
    return record


def place_tests(project_root: Path, files: list[tuple[str, str]], allowed_existing: set[str]) -> list[str]:
    """프로젝트에 테스트 파일을 쓴다. 이미 있는 파일은 이 흐름이 전에 쓴 것(allowed_existing)만 덮어쓴다."""
    root = Path(project_root).resolve()
    written = []
    for relative, _ in files:
        target = (root / relative).resolve()
        if not target.is_relative_to(root / "tests"):
            raise HandoffError(f"허용되지 않는 경로입니다: {relative}")
        if target.exists() and relative not in allowed_existing:
            raise HandoffError(
                f"이미 있는 파일이라 덮어쓰지 않습니다: {relative}. ChatGPT에 다른 파일 이름을 요청하세요."
            )
    for relative, body in files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        written.append(relative)
    return written


def remove_tests(project_root: Path, relatives: list[str], expected: dict[str, str]) -> None:
    """에이전트가 끝난 뒤 프로젝트에 둔 테스트를 지운다. 내용이 바뀌었으면(사람이 손댔으면) 남긴다."""
    root = Path(project_root)
    for relative in relatives:
        target = root / relative
        try:
            if target.read_text(encoding="utf-8") == expected.get(relative):
                target.unlink()
        except OSError:
            pass


def write_retry(path: Path, failure: str) -> str:
    """실패 내용을 붙인 재요청문을 만들어 저장하고 그 내용을 돌려준다."""
    record = load(path)
    n = record["round"]
    copy_dir = path / f"tests-{n}"
    blocks = []
    for relative in record["tests"]:
        try:
            body = (copy_dir / relative).read_text(encoding="utf-8")
        except OSError:
            continue
        blocks.append(f"```python\n# 파일: {relative}\n{body.rstrip()}\n```")
    text = RETRY_TEMPLATE.format(
        requirement=record["requirement"],
        tests="\n\n".join(blocks) or "(없음)",
        failure=failure.strip()[-4000:] or "(출력 없음)",
    )
    name = f"retry-{n}.md"
    (path / name).write_text(text, encoding="utf-8")
    record.update(status="waiting_reply", latest_prompt=name)
    save(path, record)
    return text


def mark_done(path: Path, branch: str) -> None:
    record = load(path)
    record.update(status="succeeded", branch=branch, finished_at=_now())
    save(path, record)


def latest_prompt(path: Path) -> str:
    record = load(path)
    return (Path(path) / record.get("latest_prompt", "prompt.md")).read_text(encoding="utf-8")


def list_briefs(briefs_dir: Path, limit: int = 20) -> list[dict]:
    briefs_dir = Path(briefs_dir)
    if not briefs_dir.is_dir():
        return []
    records = []
    for path in sorted(briefs_dir.iterdir(), reverse=True):
        if BRIEF_ID_PATTERN.fullmatch(path.name) and (path / "brief.json").is_file():
            try:
                records.append(load(path))
            except (OSError, ValueError):
                continue
        if len(records) >= limit:
            break
    return records
