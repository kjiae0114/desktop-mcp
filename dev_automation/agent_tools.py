"""모델에게 제공하는 파일 도구. 오류도 예외 대신 문자열로 돌려줘 모델이 스스로 고치게 한다."""

from collections.abc import Callable
from pathlib import Path, PureWindowsPath
import fnmatch

from dev_automation.paths import PathPolicyError, resolve_new_within, resolve_within, validate_relative
from dev_automation.runner import RunResult

MAX_RESULT_CHARS = 8000
MAX_READ_BYTES = 200 * 1024
MAX_WRITE_BYTES = 1024 * 1024

BLOCKED_DIRS = frozenset({".git", ".venv", ".venv-new", "runs", "__pycache__"})
BLOCKED_FILE_PATTERNS = (".env", ".env.*", "*.pem", "*.key")


def is_blocked(relative_path: str) -> bool:
    """차단 경로인지 확인한다(대소문자 무시). 읽기, 쓰기, 스테이징 검사에 함께 쓴다."""
    parts = [part.lower() for part in PureWindowsPath(relative_path).parts]
    if any(part in BLOCKED_DIRS for part in parts):
        return True
    return bool(parts) and any(fnmatch.fnmatchcase(parts[-1], p) for p in BLOCKED_FILE_PATTERNS)


def _path_prop(description: str) -> dict:
    return {"type": "string", "description": description}


def _schema(name: str, description: str, properties: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
            },
        },
    }


TOOL_SCHEMAS = [
    _schema("list_files", "폴더의 파일과 하위 폴더 목록을 본다.",
            {"path": _path_prop("프로젝트 기준 상대 경로. 루트는 '.'")}),
    _schema("read_file", "텍스트 파일을 읽는다.", {"path": _path_prop("상대 경로")}),
    _schema("write_file", "파일 전체를 새로 쓴다. 작은 파일이나 새 파일에만 사용한다.",
            {"path": _path_prop("상대 경로"), "content": {"type": "string"}}),
    _schema("replace_in_file", "파일에서 old가 정확히 한 번 나오는 부분을 new로 바꾼다.",
            {"path": _path_prop("상대 경로"), "old": {"type": "string"},
             "new": {"type": "string"}}),
    _schema("run_tests", "프로젝트 테스트를 실행하고 결과를 본다.", {}),
]


TOOL_NAMES = frozenset(schema["function"]["name"] for schema in TOOL_SCHEMAS)


def _limit(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n...(결과가 {MAX_RESULT_CHARS}자를 넘어 잘렸습니다)"


class ToolBox:
    def __init__(self, root: Path, run_tests: Callable[[], RunResult]) -> None:
        self._root = root
        self._run_suite = run_tests

    def call(self, name: str, arguments: dict) -> str:
        handler = getattr(self, f"_{name}", None)
        if name not in TOOL_NAMES or handler is None:
            return _limit(f"오류: 알 수 없는 도구입니다: {name}")
        try:
            return _limit(handler(**arguments))
        except PathPolicyError as exc:
            return _limit(f"오류: {exc}")
        except TypeError as exc:
            return _limit(f"오류: 인자가 올바르지 않습니다: {exc}")
        except (OSError, ValueError) as exc:
            return _limit(f"오류: {exc}")

    def _check(self, path: object) -> str:
        if not isinstance(path, str):
            raise PathPolicyError("path는 문자열이어야 합니다.")
        validate_relative(path)
        if is_blocked(path):
            raise PathPolicyError(f"접근이 차단된 경로입니다: {path}")
        return path

    def _resolve(self, path: object) -> Path:
        resolved = resolve_within(self._root, self._check(path))
        # 링크로 차단 경로에 들어가는 것도 막는다.
        relative = resolved.relative_to(self._root.resolve())
        if is_blocked(str(relative)):
            raise PathPolicyError(f"접근이 차단된 경로입니다: {path}")
        return resolved

    def _list_files(self, path: str = ".") -> str:
        directory = self._resolve(path)
        if not directory.is_dir():
            return f"오류: 폴더가 아닙니다: {path}"
        lines = []
        for entry in sorted(directory.iterdir(), key=lambda e: e.name):
            if is_blocked(entry.name):
                continue
            lines.append(entry.name + ("/" if entry.is_dir() else ""))
        return "\n".join(lines) or "(비어 있음)"

    def _read_file(self, path: str) -> str:
        file = self._resolve(path)
        if not file.is_file():
            return f"오류: 파일이 아닙니다: {path}"
        if file.stat().st_size > MAX_READ_BYTES:
            return f"오류: 파일이 {MAX_READ_BYTES // 1024}KB를 넘어 읽을 수 없습니다."
        return file.read_text(encoding="utf-8", errors="replace")

    def _write_file(self, path: str, content: str) -> str:
        target = self._writable(path)
        if not isinstance(content, str):
            return "오류: content는 문자열이어야 합니다."
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            return f"오류: 내용이 {MAX_WRITE_BYTES // 1024 // 1024}MB를 넘습니다."
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return f"{path}에 {len(data)}바이트를 썼습니다."

    def _replace_in_file(self, path: str, old: str, new: str) -> str:
        file = self._resolve(path)
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            return "오류: old와 new는 문자열이어야 하고 old는 비어 있을 수 없습니다."
        if not file.is_file():
            return f"오류: 파일이 아닙니다: {path}"
        # 줄바꿈이 바뀌지 않도록 바이트로 읽고 쓴다.
        text = file.read_bytes().decode("utf-8")
        count = text.count(old)
        if count != 1:
            return f"오류: old가 {count}번 나옵니다. 정확히 한 번 나오도록 더 길게 지정하세요."
        updated = text.replace(old, new)
        if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
            return f"오류: 결과가 {MAX_WRITE_BYTES // 1024 // 1024}MB를 넘습니다."
        file.write_bytes(updated.encode("utf-8"))
        return f"{path}을(를) 수정했습니다."

    def _writable(self, path: object) -> Path:
        """없는 파일도 허용하되 존재하는 가장 가까운 상위 폴더를 검증한다."""
        target = resolve_new_within(self._root, self._check(path))
        if is_blocked(str(target.relative_to(self._root.resolve()))):
            raise PathPolicyError(f"접근이 차단된 경로입니다: {path}")
        if target.is_symlink():
            raise PathPolicyError(f"링크에는 쓸 수 없습니다: {path}")
        if target.is_dir():
            raise PathPolicyError(f"폴더에는 쓸 수 없습니다: {path}")
        return target

    def _run_tests(self) -> str:
        result = self._run_suite()
        return (
            f"상태: {result.status} (종료 코드 {result.returncode})\n"
            f"{result.error or ''}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
