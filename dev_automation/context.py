"""작은 모델이 탐색에 단계를 낭비하지 않도록, 첫 메시지에 넣을 저장소 요약을 만든다.

구성:
1. 저장소 지도: 파일 목록과 .py 파일의 클래스·함수 선언(첫 줄 설명 포함)
2. 잠근 테스트 전문: 모델이 맞춰야 할 정답지
3. 잠근 테스트가 import하는 프로젝트 파일 전문: 고칠 가능성이 가장 높은 파일

전체 길이는 budget 글자로 제한한다. 넘치면 3 → 1 순으로 줄인다.
"""

import ast
import subprocess
from pathlib import Path

from dev_automation.agent_tools import BLOCKED_DIRS, is_blocked

MAX_FILES_LISTED = 200
MAX_FILE_CHARS = 6000


def _git_files(root: Path) -> list[str] | None:
    """Git 저장소면 추적 중이거나 무시되지 않는 파일만 돌려준다(.gitignore 존중). 아니면 None."""
    if not (root / ".git").exists():
        return None
    try:
        done = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    names = done.stdout.decode("utf-8", errors="replace").splitlines()
    return sorted({name.strip() for name in names if name.strip() and (root / name.strip()).is_file()})


def _project_files(root: Path) -> list[str]:
    tracked = _git_files(root)
    if tracked is not None:
        return [
            relative for relative in tracked
            if not is_blocked(relative)
            and not any(part in BLOCKED_DIRS or part.startswith(".") for part in relative.split("/"))
        ]
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in BLOCKED_DIRS or part.startswith(".") for part in relative.split("/")[:-1]):
            continue
        if is_blocked(relative) or path.name.startswith("."):
            continue
        files.append(relative)
    return files


def _signatures(source: str, public_only: bool = False) -> list[str]:
    """클래스·함수 선언 목록. public_only면 _로 시작하는 이름과 설명 주석을 뺀다(짧은 지도용)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ["  (구문 오류로 해석하지 못함)"]
    lines = []

    def describe(node, indent: str) -> None:
        if public_only and node.name.startswith("_") and node.name != "__init__":
            return
        doc = None if public_only else ast.get_docstring(node)
        note = f"  # {doc.strip().splitlines()[0][:60]}" if doc else ""
        if isinstance(node, ast.ClassDef):
            lines.append(f"{indent}class {node.name}:{note}")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    describe(child, indent + "    ")
        else:
            args = ast.unparse(node.args)
            returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            lines.append(f"{indent}def {node.name}({args}){returns}{note}")

    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            describe(node, "  ")
    return lines


def _imported_files(root: Path, source: str) -> list[str]:
    """`from a.b import x`, `import a.b`를 프로젝트 안의 a/b.py(또는 a/b/__init__.py)로 바꾼다."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    found = []
    for module in modules:
        base = module.replace(".", "/")
        for candidate in (f"{base}.py", f"{base}/__init__.py"):
            if (root / candidate).is_file() and candidate not in found:
                found.append(candidate)
                break
    return found


def _read(root: Path, relative: str, limit: int | None = MAX_FILE_CHARS) -> str:
    """파일을 읽는다. 선언을 뽑을 때는 limit=None으로 전체를 읽어야 구문 해석이 깨지지 않는다."""
    try:
        text = (root / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if limit is not None and len(text) > limit:
        text = text[:MAX_FILE_CHARS] + "\n... (생략)"
    return text


def _block(relative: str, text: str) -> str:
    return f"--- {relative} ---\n{text.rstrip()}\n"


def build_context(root: Path, locked: list[str], budget: int = 12000) -> str:
    root = Path(root)
    files = _project_files(root)

    def make_map(with_test_signatures: bool, public_only: bool = False) -> str:
        lines = []
        for relative in files[:MAX_FILES_LISTED]:
            lines.append(relative)
            is_test = relative.startswith("tests/") or relative.rsplit("/", 1)[-1].startswith("test_")
            if relative.endswith(".py") and (with_test_signatures or not is_test):
                lines.extend(_signatures(_read(root, relative, None), public_only))
        if len(files) > MAX_FILES_LISTED:
            lines.append(f"... 외 {len(files) - MAX_FILES_LISTED}개 파일")
        return "\n".join(lines)

    repo_map = make_map(True)

    tests = [_block(rel, _read(root, rel)) for rel in locked if (root / rel).is_file()]
    related: list[str] = []
    for rel in locked:
        for imported in _imported_files(root, _read(root, rel, None)):
            if imported not in related and imported not in locked:
                related.append(imported)
    sources = [_block(rel, _read(root, rel)) for rel in related]

    def render(map_text: str, source_blocks: list[str]) -> str:
        parts = ["## 저장소 지도 (파일과 선언)\n" + map_text]
        if tests:
            parts.append("## 잠근 테스트 (이 테스트를 통과시키는 것이 목표)\n" + "\n".join(tests))
        if source_blocks:
            parts.append("## 테스트가 사용하는 파일의 현재 내용\n" + "\n".join(source_blocks))
        return "\n\n".join(parts)

    text = render(repo_map, sources)
    while len(text) > budget and sources:
        sources.pop()
        text = render(repo_map, sources)
    if len(text) > budget:
        # 테스트 파일의 선언부터 뺀다. 고칠 대상은 대개 테스트가 아닌 코드다.
        text = render(make_map(False), [])
    if len(text) > budget:
        # 공개 선언만, 설명 없이.
        text = render(make_map(False, public_only=True), [])
    if len(text) > budget:
        # 지도는 파일 이름만 남긴다.
        text = render("\n".join(files[:MAX_FILES_LISTED]), [])
    if len(text) > budget:
        text = text[:budget] + "\n... (생략)"
    return text
