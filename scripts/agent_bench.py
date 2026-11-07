"""에이전트 벤치마크: 실제 에이전트(run_task + Docker 샌드박스)로 난이도별 과제를 풀게 하고 성공률을 잰다.

모델을 바꾸거나 에이전트를 고쳤을 때 "정말 나아졌는지"를 같은 과제로 비교하려고 만든다.
과제마다 작은 연습용 저장소(재고 관리 앱)를 임시 폴더에 새로 만들고, 잠근 테스트를 통과하는지로 판정한다.

사용:
  python scripts/agent_bench.py --model qwen3.5:4b-q4_K_M --repeat 2
  python scripts/agent_bench.py --model qwen2.5-coder:7b --tasks T1,T4
결과: workspace/agent_bench/<모델>-<시각>.json 과 화면 표
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dev_automation.agent import CONTEXT_CHARS_WHEN_ON, SUCCEEDED, run_task  # noqa: E402
from dev_automation.llm import OllamaClient  # noqa: E402
from dev_automation.projects import COMMAND_PROFILES, Project  # noqa: E402
from dev_automation.runner import CommandSpec  # noqa: E402
from dev_automation.sandbox import DockerSandbox  # noqa: E402
from dev_automation.spec import SpecError, write_spec_tests  # noqa: E402

IMAGE = "desktop-mcp-sandbox:py312"


def _d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


# --- 연습용 저장소 -----------------------------------------------------------
# 일부러 버그 하나(remove_item)를 넣어 둔다. T2가 이걸 고친다.

BASE_FILES = {
    "inventory/__init__.py": "",
    "inventory/models.py": _d('''
        from dataclasses import dataclass


        @dataclass
        class Item:
            name: str
            quantity: int
            price: int  # 원 단위

            def value(self) -> int:
                return self.quantity * self.price
    '''),
    "inventory/store.py": _d('''
        import json
        from pathlib import Path

        from inventory.models import Item


        class Store:
            """품목을 JSON 파일에 저장한다."""

            def __init__(self, path):
                self.path = Path(path)
                self.items: dict[str, Item] = {}
                if self.path.exists():
                    for row in json.loads(self.path.read_text(encoding="utf-8")):
                        self.items[row["name"]] = Item(row["name"], row["quantity"], row["price"])

            def save(self) -> None:
                rows = [
                    {"name": i.name, "quantity": i.quantity, "price": i.price}
                    for i in self.items.values()
                ]
                self.path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

            def add_item(self, name: str, quantity: int, price: int) -> Item:
                if quantity < 0 or price < 0:
                    raise ValueError("수량과 가격은 0 이상이어야 합니다.")
                if name in self.items:
                    self.items[name].quantity += quantity
                else:
                    self.items[name] = Item(name, quantity, price)
                return self.items[name]

            def remove_item(self, name: str, quantity: int) -> Item:
                """수량을 줄인다. 재고보다 많이 빼면 ValueError, 0이 되면 품목을 지운다."""
                item = self.items[name]
                item.quantity -= quantity
                return item
    '''),
    "inventory/cli.py": _d('''
        import argparse

        from inventory.store import Store


        def main(argv=None, out=print):
            parser = argparse.ArgumentParser(prog="inventory")
            parser.add_argument("--db", default="inventory.json")
            sub = parser.add_subparsers(dest="command", required=True)
            add = sub.add_parser("add")
            add.add_argument("name")
            add.add_argument("quantity", type=int)
            add.add_argument("price", type=int)
            sub.add_parser("list")
            args = parser.parse_args(argv)

            store = Store(args.db)
            if args.command == "add":
                store.add_item(args.name, args.quantity, args.price)
                store.save()
                out(f"추가: {args.name}")
            elif args.command == "list":
                for item in store.items.values():
                    out(f"{item.name} {item.quantity}개 {item.price}원")
            return 0
    '''),
    "tests/__init__.py": "",
    "tests/test_store.py": _d('''
        from inventory.store import Store


        def test_add_and_reload(tmp_path):
            store = Store(tmp_path / "db.json")
            store.add_item("사과", 3, 1000)
            store.add_item("사과", 2, 1000)
            store.save()
            again = Store(tmp_path / "db.json")
            assert again.items["사과"].quantity == 5
    '''),
    "tests/test_cli.py": _d('''
        from inventory.cli import main


        def test_add_then_list(tmp_path):
            db = str(tmp_path / "db.json")
            lines = []
            main(["--db", db, "add", "배", "2", "1500"], out=lines.append)
            main(["--db", db, "list"], out=lines.append)
            assert lines == ["추가: 배", "배 2개 1500원"]
    '''),
    "pytest.ini": "[pytest]\ntestpaths = tests\n",
    ".gitignore": "__pycache__/\n.pytest_cache/\n*.json\n",
}


# --- 과제 --------------------------------------------------------------------
# level: 1 쉬운 함수, 2 버그 수정, 3 기존 코드를 쓰는 함수, 4 파일 2개 기능, 5 파일 3개 기능

TASKS = {
    "T1": {
        "level": "1 새 함수",
        "prompt": "inventory/textutil.py에 slugify(text) 함수를 만들어줘. 앞뒤 공백을 없애고 소문자로 바꾸고, 공백은 '-'로 바꿔.",
        "tests": {"tests/test_textutil.py": _d('''
            from inventory.textutil import slugify


            def test_slugify():
                assert slugify("  Hello World ") == "hello-world"
                assert slugify("ABC") == "abc"
        ''')},
    },
    "T2": {
        "level": "2 버그 수정",
        "prompt": "Store.remove_item이 docstring대로 동작하지 않아. 재고보다 많이 빼면 ValueError, 0이 되면 품목을 지우도록 고쳐줘.",
        "tests": {"tests/test_remove.py": _d('''
            import pytest

            from inventory.store import Store


            def test_remove(tmp_path):
                store = Store(tmp_path / "db.json")
                store.add_item("사과", 3, 1000)
                store.remove_item("사과", 1)
                assert store.items["사과"].quantity == 2
                with pytest.raises(ValueError):
                    store.remove_item("사과", 5)
                assert store.items["사과"].quantity == 2
                store.remove_item("사과", 2)
                assert "사과" not in store.items
        ''')},
    },
    "T3": {
        "level": "3 기존 코드 활용",
        "prompt": "inventory/report.py에 total_value(store) 함수를 만들어줘. Store에 있는 모든 품목의 재고 가치(수량×가격) 합계를 돌려줘. 품목이 없으면 0.",
        "tests": {"tests/test_report.py": _d('''
            from inventory.report import total_value
            from inventory.store import Store


            def test_total_value(tmp_path):
                store = Store(tmp_path / "db.json")
                assert total_value(store) == 0
                store.add_item("사과", 3, 1000)
                store.add_item("배", 2, 1500)
                assert total_value(store) == 6000
        ''')},
    },
    "T4": {
        "level": "4 기능(파일 2개)",
        "prompt": (
            "재고 부족 알림 기능을 붙여줘. Store에 low_stock(threshold) 메서드를 추가해서 수량이 threshold 미만인 "
            "품목 이름을 이름순 리스트로 돌려주고, CLI에 'low --threshold N' 명령(기본값 5)을 추가해서 "
            "부족한 품목 이름을 한 줄에 하나씩 출력해줘. 없으면 '부족한 품목 없음'을 출력해."
        ),
        "tests": {"tests/test_low_stock.py": _d('''
            from inventory.cli import main
            from inventory.store import Store


            def test_store_low_stock(tmp_path):
                store = Store(tmp_path / "db.json")
                store.add_item("배", 1, 1500)
                store.add_item("사과", 10, 1000)
                store.add_item("귤", 4, 500)
                assert store.low_stock(5) == ["귤", "배"]
                assert store.low_stock(1) == []


            def test_cli_low(tmp_path):
                db = str(tmp_path / "db.json")
                lines = []
                main(["--db", db, "add", "배", "1", "1500"], out=lines.append)
                main(["--db", db, "add", "사과", "10", "1000"], out=lines.append)
                lines.clear()
                main(["--db", db, "low"], out=lines.append)
                assert lines == ["배"]
                lines.clear()
                main(["--db", db, "low", "--threshold", "1"], out=lines.append)
                assert lines == ["부족한 품목 없음"]
        ''')},
    },
    "T5": {
        "level": "5 기능(파일 3개)",
        "prompt": (
            "품목에 분류(category) 기능을 붙여줘. Item에 category 필드를 추가하고 기본값은 '기타'로 해. "
            "Store.add_item에 category 인자(기본 '기타')를 추가하고 저장·불러오기에 포함해. category가 없는 "
            "옛 JSON 파일도 불러올 수 있어야 해(기본값 '기타'). inventory/report.py에 value_by_category(store) "
            "함수를 만들어서 분류별 재고 가치 합계를 딕셔너리로 돌려줘."
        ),
        "tests": {"tests/test_category.py": _d('''
            import json

            from inventory.models import Item
            from inventory.report import value_by_category
            from inventory.store import Store


            def test_item_default_category():
                assert Item("a", 1, 1).category == "기타"


            def test_category_saved_and_old_file_loads(tmp_path):
                db = tmp_path / "db.json"
                db.write_text(json.dumps([{"name": "옛것", "quantity": 1, "price": 100}]), encoding="utf-8")
                store = Store(db)
                assert store.items["옛것"].category == "기타"
                store.add_item("사과", 3, 1000, category="과일")
                store.save()
                again = Store(db)
                assert again.items["사과"].category == "과일"


            def test_value_by_category(tmp_path):
                store = Store(tmp_path / "db.json")
                store.add_item("사과", 3, 1000, category="과일")
                store.add_item("배", 2, 1500, category="과일")
                store.add_item("휴지", 1, 2000)
                assert value_by_category(store) == {"과일": 6000, "기타": 2000}
        ''')},
    },
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(base: Path, task: dict, hidden: bool = False) -> Path:
    repo = base / "repo"

    def write(files: dict) -> None:
        for rel, text in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    write(BASE_FILES)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "bench")
    _git(repo, "config", "user.email", "bench@example.com")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    # 정답지는 커밋하지 않는다. 실제 사용처럼 에이전트가 기준 테스트 뒤에 복사해 넣는다.
    # 숨김 모드에서는 아예 주지 않고, 끝난 뒤 채점에만 쓴다.
    if not hidden:
        write(task["tests"])
    return repo


def judge_hidden(base: Path, repo: Path, branch: str, task: dict) -> tuple[bool, str]:
    """모델이 본 적 없는 테스트를 결과 브랜치에 넣고 샌드박스에서 전체 테스트를 돌린다."""
    check = base / "judge"
    _git(repo, "worktree", "add", "-q", str(check), branch)
    for rel, text in task["tests"].items():
        path = check / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    result = DockerSandbox(IMAGE).run_tests(
        check, ".", COMMAND_PROFILES["pytest"].args, ("-q", "--tb=line"), 600
    )
    return result.status == SUCCEEDED, (result.stdout or result.error or "")[-300:]


def run_once(
    model: str, task_id: str, task: dict, hidden: bool = False, agent_options: dict | None = None
) -> dict:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        base = Path(temp)
        repo = make_repo(base, task, hidden)
        profile = COMMAND_PROFILES["pytest"]
        spec = CommandSpec(sys.executable, profile.args, profile.allowed_extra_args)
        project = Project("bench", repo, ".", {"pytest": spec}, IMAGE)
        llm = OllamaClient(model)
        log: list[str] = []
        options = dict(agent_options or {})
        spec_first = options.pop("spec_first", False)
        started = time.time()
        try:
            locked = [] if hidden else list(task["tests"])
            if spec_first and hidden:
                try:
                    locked = [write_spec_tests(llm, repo, task["prompt"])]
                except SpecError as exc:
                    llm.unload()
                    return {"task": task_id, "ok": False, "seconds": round(time.time() - started, 1),
                            "steps": 0, "tools": 0, "message": f"정답지 만들기 실패: {exc}"}
            result = run_task(
                project, task["prompt"], llm, base / "runs",
                protected_tests=locked, sandbox=DockerSandbox(IMAGE),
                on_progress=log.append, **options,
            )
        finally:
            llm.unload()
        seconds = round(time.time() - started, 1)
        conv_path = result.diff_path.parent / "conversation.json" if result.diff_path else None
        steps = 0
        if conv_path and conv_path.exists():
            steps = sum(1 for m in json.loads(conv_path.read_text(encoding="utf-8")) if m["role"] == "assistant")
        ok, message = result.status == SUCCEEDED, result.message
        if hidden and ok:
            ok, detail = judge_hidden(base, repo, result.branch, task)
            message = "숨긴 테스트 통과" if ok else "숨긴 테스트 실패: " + detail
        return {
            "task": task_id, "ok": ok, "seconds": seconds, "steps": steps,
            "tools": sum(1 for line in log if line.startswith("도구:")),
            "message": message[:300],
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--tasks", default=",".join(TASKS))
    parser.add_argument("--label", default="", help="결과 파일 이름에 붙일 설명(예: repomap)")
    parser.add_argument(
        "--hidden", action="store_true",
        help="정답지 테스트를 모델에게 주지 않고, 끝난 뒤 채점에만 쓴다(요구사항 문장만으로 푸는 실력).",
    )
    parser.add_argument("--context", action="store_true", help="저장소 요약을 첫 메시지에 넣는다.")
    parser.add_argument("--self-test", action="store_true", help="잠근 테스트가 없을 때 모델이 테스트를 직접 쓰게 한다.")
    parser.add_argument("--max-steps", type=int, default=25, help="모델 호출 최대 횟수")
    parser.add_argument(
        "--spec-first", action="store_true",
        help="(블라인드 모드) 모델이 요구사항으로 테스트를 먼저 쓰고, 그걸 잠근 채 구현한다.",
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    sandbox = DockerSandbox(IMAGE)
    problem = sandbox.check()
    if problem:
        print(problem)
        return 2

    task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
    runs = []
    for task_id in task_ids:
        for n in range(args.repeat):
            print(f"[{task_id} {TASKS[task_id]['level']}] {n + 1}/{args.repeat} ...", flush=True)
            options = {
                "max_steps": args.max_steps,
                "spec_first": args.spec_first,
                "context_chars": CONTEXT_CHARS_WHEN_ON if args.context else 0,
                "self_test": args.self_test,
            }
            outcome = run_once(args.model, task_id, TASKS[task_id], args.hidden, options)
            runs.append(outcome)
            mark = "성공" if outcome["ok"] else "실패"
            print(f"  {mark} {outcome['seconds']}초, 모델 호출 {outcome['steps']}번 | {outcome['message'][:80]}", flush=True)

    summary = {}
    for task_id in task_ids:
        mine = [r for r in runs if r["task"] == task_id]
        ok = [r for r in mine if r["ok"]]
        summary[task_id] = {
            "level": TASKS[task_id]["level"],
            "passed": f"{len(ok)}/{len(mine)}",
            "avg_seconds_ok": round(sum(r["seconds"] for r in ok) / len(ok), 1) if ok else None,
        }
    total_ok = sum(r["ok"] for r in runs)
    print(f"\n모델: {args.model} {args.label}".rstrip())
    for task_id, row in summary.items():
        print(f"  {task_id} {row['level']:<16} {row['passed']:>5}  평균 {row['avg_seconds_ok']}초")
    print(f"  합계 {total_ok}/{len(runs)}")

    out_dir = ROOT / "workspace" / "agent_bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^a-z0-9.]+", "_", args.model.lower())
    label = (f"-{args.label}" if args.label else "") + ("-hidden" if args.hidden else "")
    out = out_dir / f"{name}{label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({
        "model": args.model, "label": args.label, "repeat": args.repeat, "hidden": args.hidden,
        "context": args.context, "self_test": args.self_test,
        "max_steps": args.max_steps, "spec_first": args.spec_first,
        "summary": summary, "total": f"{total_ok}/{len(runs)}", "runs": runs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"결과: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
