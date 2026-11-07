"""저장소 요약(dev_automation.context) 테스트."""

from dev_automation.context import build_context


def _write(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


FILES = {
    "pkg/__init__.py": "",
    "pkg/store.py": (
        "class Store:\n"
        "    \"\"\"품목 저장소.\"\"\"\n"
        "    def add(self, name: str, qty: int) -> None:\n"
        "        pass\n\n"
        "def helper(x):\n    return x\n"
    ),
    "pkg/other.py": "def unrelated():\n    return 1\n",
    "tests/test_store.py": "from pkg.store import Store\n\ndef test_x():\n    assert Store()\n",
    "README.md": "# 설명\n",
    ".env": "SECRET=1\n",
    ".git/config": "[core]\n",
    "runs/old.py": "x = 1\n",
}


def test_map_lists_files_and_signatures_but_not_blocked(tmp_path):
    _write(tmp_path, FILES)

    text = build_context(tmp_path, [])

    assert "pkg/store.py" in text and "README.md" in text
    assert "class Store:  # 품목 저장소." in text
    assert "def add(self, name: str, qty: int) -> None" in text
    assert "def helper(x)" in text
    assert ".env" not in text and ".git" not in text and "runs/old.py" not in text


def test_locked_tests_and_imported_sources_are_included_in_full(tmp_path):
    _write(tmp_path, FILES)

    text = build_context(tmp_path, ["tests/test_store.py"])

    assert "## 잠근 테스트" in text and "assert Store()" in text
    assert "--- pkg/store.py ---" in text and "return x" in text
    assert "--- pkg/other.py ---" not in text  # 테스트가 import하지 않는 파일은 전문을 넣지 않는다


def test_budget_drops_sources_first_then_map_details(tmp_path):
    _write(tmp_path, FILES)
    full = build_context(tmp_path, ["tests/test_store.py"])

    smaller = build_context(tmp_path, ["tests/test_store.py"], budget=len(full) - 1)

    assert "--- pkg/store.py ---" not in smaller
    assert "class Store" in smaller
    assert len(build_context(tmp_path, ["tests/test_store.py"], budget=300)) <= 300 + len("\n... (생략)")


def test_syntax_error_file_does_not_break(tmp_path):
    _write(tmp_path, {"bad.py": "def (:\n"})

    assert "구문 오류" in build_context(tmp_path, [])


def test_git_repo_respects_gitignore(tmp_path):
    import subprocess

    _write(tmp_path, {"app.py": "def run():\n    pass\n", "workspace/out.json": "{}", ".gitignore": "workspace/\n"})
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    text = build_context(tmp_path, [])

    assert "app.py" in text and "def run()" in text
    assert "workspace" not in text


def test_big_file_signatures_are_not_cut(tmp_path):
    body = "".join(f"def f{i}(x):\n    return x  # {'긴 주석' * 20}\n\n" for i in range(200))
    _write(tmp_path, {"big.py": body})

    text = build_context(tmp_path, [], budget=100000)

    assert "구문 오류" not in text and "def f199(x)" in text


def test_tight_budget_keeps_public_signatures_before_names_only(tmp_path):
    many = "".join(
        f"def _private{i}(a, b, c):\n    '''{'설명' * 30}'''\n\n" for i in range(100)
    ) + "def public_api(x):\n    '''공개 함수.'''\n"
    _write(tmp_path, {"pkg/core.py": many, "tests/test_core.py": "def test_x():\n    pass\n"})

    text = build_context(tmp_path, [], budget=800)

    assert "def public_api(x)" in text
    assert "_private" not in text
