"""로컬 모델이 격리된 git 워크트리에서 코드를 고치고, 테스트를 통과하면 별도 브랜치에 커밋한다.

사용자의 작업 폴더는 수정하지 않고 push나 병합도 하지 않는다. 결과는 사용자가 검토한다.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
import secrets
import shutil

from dev_automation.agent_tools import TOOL_SCHEMAS, ToolBox, is_blocked
from dev_automation.llm import LLMClient, LLMError
from dev_automation.paths import PathPolicyError, resolve_within
from dev_automation.projects import Project
from dev_automation.runner import REJECTED, CommandSpec, RunResult, run_command

SUCCEEDED = "succeeded"
FAILED = "failed"
NO_CHANGES = "no_changes"

MAX_CONTEXT_CHARS = 24000
FEEDBACK_CHARS = 4000
GIT_TIMEOUT_SECONDS = 120
GIT_ENV = frozenset({"USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH"})
TEST_ARGS = ("-q", "--tb=short")

SECRET_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"ghp_",
        r"github_pat_",
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN .*PRIVATE KEY",
        r"sk-[A-Za-z0-9]{20,}",
    )
]

SYSTEM_PROMPT = (
    "당신은 코드를 고치는 에이전트입니다. 주어진 도구로 파일을 읽고 필요한 부분만 고치세요.\n"
    "- 먼저 list_files와 read_file로 관련 코드를 확인합니다.\n"
    "- 큰 파일은 write_file로 통째로 다시 쓰지 말고 replace_in_file로 일부만 바꿉니다.\n"
    "- 고친 뒤 run_tests로 확인합니다.\n"
    "- 작업이 끝나면 도구를 호출하지 말고 무엇을 바꿨는지 짧게 요약만 답하세요."
)


@dataclass
class AgentResult:
    status: str
    branch: str
    message: str
    commit: str | None = None
    changed_files: list[str] = field(default_factory=list)
    diff_path: Path | None = None


def _git(cwd: Path, *args: str) -> RunResult:
    """고정된 인자로만 git을 실행한다. 모델이 git을 직접 실행하는 경로는 없다."""
    spec = CommandSpec("git", ("-c", "core.quotepath=off", *args), allowed_env=GIT_ENV)
    return run_command("git", {"git": spec}, cwd, GIT_TIMEOUT_SECONDS)


def _trim_messages(messages: list[dict]) -> list[dict]:
    """시스템·작업 메시지는 두고, 오래된 메시지 묶음부터 버려 전체 길이를 제한한다."""
    fixed, rest = messages[:2], messages[2:]

    # assistant의 tool_calls와 뒤따르는 tool 결과가 짝을 이루도록 묶음 단위로 다룬다.
    groups: list[list[dict]] = []
    for message in rest:
        if message["role"] == "tool" and groups:
            groups[-1].append(message)
        else:
            groups.append([message])

    def size(items: list[dict]) -> int:
        return sum(len(json.dumps(m, ensure_ascii=False)) for m in items)

    total = size(fixed)
    kept: list[list[dict]] = []
    for group in reversed(groups):
        total += size(group)
        if kept and total > MAX_CONTEXT_CHARS:
            break
        kept.append(group)
    return fixed + [m for group in reversed(kept) for m in group]


def _feedback(result: RunResult) -> str:
    output = f"{result.error or ''}\n{result.stdout}\n{result.stderr}".strip()
    return (
        "테스트가 실패했습니다. 아래 출력을 보고 코드를 고친 뒤 다시 확인하세요.\n"
        + output[-FEEDBACK_CHARS:]
    )


def _clear_bytecode(root: Path) -> None:
    """옛 .pyc가 실행되지 않게 지운다. 크기가 같은 수정을 같은 초에 하면 Python이 변경을 알아채지 못한다."""
    for path in list(root.rglob("__pycache__")):
        shutil.rmtree(path, ignore_errors=True)


def _has_secret(diff_text: str) -> bool:
    return any(
        pattern.search(line)
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
        for pattern in SECRET_PATTERNS
    )


def run_task(
    project: Project,
    task: str,
    llm: LLMClient,
    runs_dir: Path,
    *,
    max_steps: int = 25,
    max_fix_attempts: int = 3,
    test_timeout: float = 600,
    on_progress: Callable[[str], None] = print,
) -> AgentResult:
    task_id = f"agent-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    branch = f"agent/{task_id}"
    # git은 작업 폴더마다 다른 cwd에서 실행되므로 절대 경로로 고정한다.
    task_dir = Path(runs_dir).resolve() / task_id
    worktree = task_dir / "worktree"
    diff_path = task_dir / "final.diff"
    messages: list[dict] = []
    result = AgentResult(FAILED, branch, "작업을 시작하지 못했습니다.")
    created = False
    diff_saved = False

    def run_tests() -> RunResult:
        try:
            cwd = resolve_within(worktree, project.default_cwd)
        except PathPolicyError as exc:
            return RunResult("pytest", REJECTED, error=str(exc))
        _clear_bytecode(worktree)
        return run_command("pytest", project.commands, cwd, test_timeout, TEST_ARGS)

    def snapshot_diff() -> str:
        """새 파일을 포함한 HEAD 기준 전체 변경을 final.diff로 저장하고 내용을 반환한다."""
        nonlocal diff_saved
        _git(worktree, "add", "-A")
        _git(worktree, "diff", "--cached", "HEAD", f"--output={diff_path}")
        diff_saved = True
        try:
            return diff_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    try:
        task_dir.mkdir(parents=True)
        if _git(project.root, "status", "--porcelain").stdout.strip():
            on_progress("경고: 커밋되지 않은 변경은 에이전트 작업에 포함되지 않습니다.")

        added = _git(project.root, "worktree", "add", "-b", branch, str(worktree), "HEAD")
        if not added.ok:
            result.message = f"워크트리를 만들지 못했습니다: {added.stderr.strip()}"
            return result
        created = True
        on_progress(f"작업 브랜치: {branch}")

        on_progress("기준 테스트 실행 중...")
        baseline = run_tests()
        if not baseline.ok:
            result.message = "작업 전부터 테스트가 실패합니다.\n" + _feedback(baseline)
            return result

        toolbox = ToolBox(worktree, run_tests)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        fix_attempts = 0

        for _ in range(max_steps):
            reply = llm.chat(_trim_messages(messages), TOOL_SCHEMAS)
            calls = reply["tool_calls"]
            assistant = {"role": "assistant", "content": reply["content"]}
            if calls:
                assistant["tool_calls"] = [
                    {"function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in calls
                ]
            messages.append(assistant)

            if calls:
                for call in calls:
                    target = call["arguments"].get("path", "")
                    on_progress(f"도구: {call['name']} {target}".rstrip())
                    output = toolbox.call(call["name"], call["arguments"])
                    messages.append(
                        {"role": "tool", "tool_name": call["name"], "content": output}
                    )
                continue

            # 모델의 성공 주장은 믿지 않고 테스트 결과로만 판단한다.
            if not _git(worktree, "status", "--porcelain").stdout.strip():
                result.status, result.message = NO_CHANGES, "변경된 내용이 없습니다."
                return result

            on_progress("변경 확인을 위해 테스트 실행 중...")
            tests = run_tests()
            if not tests.ok:
                fix_attempts += 1
                if fix_attempts > max_fix_attempts:
                    result.message = "수정 후에도 테스트가 계속 실패했습니다.\n" + _feedback(tests)
                    return result
                on_progress(f"테스트 실패. 수정 재시도 {fix_attempts}/{max_fix_attempts}")
                messages.append({"role": "user", "content": _feedback(tests)})
                continue

            diff_text = snapshot_diff()
            names = _git(worktree, "diff", "--cached", "--name-only").stdout.split("\n")
            files = [name for name in names if name]
            blocked = [name for name in files if is_blocked(name)]
            if blocked:
                result.message = f"차단된 경로의 파일이 변경되어 커밋하지 않습니다: {blocked}"
                return result
            if _has_secret(diff_text):
                result.message = "비밀정보로 보이는 내용이 있어 커밋하지 않습니다."
                return result

            summary = (task.strip().splitlines() or [""])[0][:60]
            committed = _git(worktree, "commit", "-m", f"에이전트 작업: {summary}")
            if not committed.ok:
                reason = committed.stderr.strip() or committed.stdout.strip()
                result.message = f"커밋하지 못했습니다: {reason}"
                return result
            commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
            result = AgentResult(SUCCEEDED, branch, reply["content"] or "완료", commit, files)
            return result

        result.message = f"최대 단계({max_steps})를 넘었습니다."
        return result
    except LLMError as exc:
        result.message = f"모델 호출 실패: {exc}"
        return result
    except Exception as exc:
        result.message = f"예상하지 못한 오류: {exc}"
        return result
    finally:
        if created and not diff_saved:
            try:
                snapshot_diff()
            except Exception:
                pass
        if created and diff_path.exists():
            result.diff_path = diff_path
        try:
            (task_dir / "conversation.json").write_text(
                json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
        if created:
            _cleanup(project.root, worktree, branch, result.status == SUCCEEDED, on_progress)


def _cleanup(
    root: Path,
    worktree: Path,
    branch: str,
    keep_branch: bool,
    on_progress: Callable[[str], None],
) -> None:
    """정리 실패는 결과를 바꾸지 않고 경고만 한다."""
    try:
        _git(root, "worktree", "remove", "--force", str(worktree))
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)
            _git(root, "worktree", "prune")
        if not keep_branch:
            _git(root, "branch", "-D", branch)
    except Exception as exc:
        on_progress(f"경고: 정리하지 못했습니다: {exc}")
