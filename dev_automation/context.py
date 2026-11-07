"""작은 모델이 탐색에 단계를 낭비하지 않도록, 첫 메시지에 넣을 저장소 요약을 만든다.

구성:
1. 저장소 지도: 파일 목록과 .py 파일의 클래스·함수 선언(첫 줄 설명 포함)
2. 잠근 테스트 전문: 모델이 맞춰야 할 정답지
3. 잠근 테스트가 import하는 프로젝트 파일 전문: 고칠 가능성이 가장 높은 파일

전체 길이는 budget 글자로 제한한다. 넘치면 3 → 1 순으로 줄인다.
"""

import ast
from pathlib import Path

from dev_automation.agent_tools import BLOCKED_DIRS, is_blocked

MAX_FILES_LISTED = 200
MAX_FILE_CHARS = 6000


def _project_files(root: Path) -> list[str]:
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


def _signatures(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ["  (구문 오류로 해석하지 못함)"]
    lines = []

    def describe(node, indent: str) -> None:
        doc = ast.get_docstring(node)
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


def _read(root: Path, relative: str) -> str:
    try:
        text = (root / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + "\n... (생략)"
    return text


def _block(relative: str, text: str) -> str:
    return f"--- {relative} ---\n{text.rstrip()}\n"


def build_context(root: Path, locked: list[str], budget: int = 12000) -> str:
    root = Path(root)
    files = _project_files(root)

    map_lines = []
    for relative in files[:MAX_FILES_LISTED]:
        map_lines.append(relative)
        if relative.endswith(".py"):
            map_lines.extend(_signatures(_read(root, relative)))
    if len(files) > MAX_FILES_LISTED:
        map_lines.append(f"... 외 {len(files) - MAX_FILES_LISTED}개 파일")
    repo_map = "\n".join(map_lines)

    tests = [_block(rel, _read(root, rel)) for rel in locked if (root / rel).is_file()]
    related: list[str] = []
    for rel in locked:
        for imported in _imported_files(root, _read(root, rel)):
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
        # 지도는 파일 이름만 남긴다.
        text = render("\n".join(files[:MAX_FILES_LISTED]), [])
    if len(text) > budget:
        text = text[:budget] + "\n... (생략)"
    return text
