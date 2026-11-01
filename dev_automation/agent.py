"""로컬 모델이 격리된 git 워크트리에서 코드를 고치고, 테스트를 통과하면 별도 브랜치에 커밋한다.

사용자의 작업 폴더는 수정하지 않고 push나 병합도 하지 않는다. 결과는 사용자가 검토한다.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import shutil

from dev_automation.agent_tools import TOOL_SCHEMAS, ToolBox, is_blocked, normalize_path
from dev_automation.llm import LLMClient, LLMError
from dev_automation.paths import PathPolicyError, resolve_new_within, resolve_within
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
MAX_REPEATED_EDITS = 3
EDIT_TOOLS = frozenset({"write_file", "replace_in_file"})
REPEAT_WARNING = "같은 수정을 반복하고 있습니다. 오류 메시지를 다시 읽고 다른 방법을 쓰세요."
REPEAT_ABORT = "같은 수정을 반복해 중단했습니다."

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
PROTECTED_PROMPT = "\n- 테스트 파일은 고칠 수 없습니다. 테스트를 통과하도록 코드만 고치세요."


@dataclass
class AgentResult:
    status: str
    branch: str
    message: str
    commit: str | None = None
    changed_files: list[str] = field(default_factory=list)
    diff_path: Path | None = None
    tests_changed: list[str] = field(default_factory=list)


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


def _digest(path: Path, *, ignore_eol: bool = False) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if ignore_eol:
        # 줄바꿈만 다른 같은 내용은 같은 상태로 본다.
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def _edit_digest(worktree: Path, path: object) -> str | None:
    if not isinstance(path, str):
        return None
    try:
        return _digest(resolve_within(worktree, path), ignore_eol=True)
    except (PathPolicyError, OSError):
        return None


def _count_call(counts: dict[str, int], call: dict) -> int:
    key = call["name"] + json.dumps(call["arguments"], sort_keys=True, ensure_ascii=False)
    counts[key] = counts.get(key, 0) + 1
    return counts[key]


def _is_test_file(relative: str, locked: set[str]) -> bool:
    if normalize_path(relative) in locked:
        return False
    parts = [part.lower() for part in PureWindowsPath(relative).parts]
    return "tests" in parts or parts[-1].startswith("test_")


def _validate_protected(
    project: Project, paths: Sequence[str]
) -> tuple[list[tuple[str, Path]], str | None]:
    """잠근 테스트를 검증해 (정규화된 상대 경로, 원본 파일) 목록을 만든다."""
    root = project.root.resolve()
    checked: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for path in paths:
        try:
            if is_blocked(path) or path.startswith("-"):
                return [], f"잠글 수 없는 테스트 경로입니다: {path}"
            source = resolve_within(project.root, path)
            relative = source.relative_to(root).as_posix()
        except (PathPolicyError, ValueError, OSError) as exc:
            return [], f"잠글 테스트 경로가 올바르지 않습니다: {path} ({exc})"
        if is_blocked(relative):
            return [], f"잠글 수 없는 테스트 경로입니다: {path}"
        if not source.is_file():
            return [], f"잠글 테스트가 파일이 아닙니다: {path}"
        if relative.lower() not in seen:
            seen.add(relative.lower())
            checked.append((relative, source))
    return checked, None


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
    protected_tests: Sequence[str] = (),
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
    staged_files: list[str] = []
    locked: list[tuple[str, Path]] = []
    locked_hashes: dict[str, str | None] = {}

    def run_tests(only_locked: bool = False) -> RunResult:
        try:
            cwd = resolve_within(worktree, project.default_cwd)
        except PathPolicyError as exc:
            return RunResult("pytest", REJECTED, error=str(exc))
        _clear_bytecode(worktree)
        commands, extra = project.commands, TEST_ARGS
        if only_locked and locked and "pytest" in commands:
            paths = tuple(
                Path(os.path.relpath(worktree / rel, cwd)).as_posix() for rel, _ in locked
            )
            spec = commands["pytest"]
            commands = {
                **commands,
                "pytest": replace(
                    spec, allowed_extra_args=spec.allowed_extra_args | frozenset(paths)
                ),
            }
            extra = TEST_ARGS + paths
        return run_command("pytest", commands, cwd, test_timeout, extra)

    def run_locked_tests() -> RunResult:
        return run_tests(only_locked=True)

    def snapshot_diff() -> str:
        """새 파일을 포함한 HEAD 기준 전체 변경을 final.diff로 저장하고 내용을 반환한다."""
        nonlocal diff_saved
        _git(worktree, "add", "-A")
        _git(worktree, "diff", "--cached", "HEAD", f"--output={diff_path}")
        names = _git(worktree, "diff", "--cached", "--name-only").stdout.split("\n")
        staged_files[:] = [name for name in names if name]
        diff_saved = True
        try:
            return diff_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    try:
        task_dir.mkdir(parents=True)
        locked, problem = _validate_protected(project, protected_tests)
        if problem:
            result.message = problem
            return result
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

        system_prompt, user_task = SYSTEM_PROMPT, task
        if locked:
            # 기준 테스트가 끝난 뒤에 복사한다. 새 테스트는 아직 실패하는 게 정상이다.
            for relative, source in locked:
                target = resolve_new_within(worktree, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                locked_hashes[relative] = _digest(target)
            system_prompt += PROTECTED_PROMPT
            names = "\n".join(f"- {relative}" for relative, _ in locked)
            user_task += f"\n\n잠근 테스트(수정 불가):\n{names}"
            on_progress("잠근 테스트: " + ", ".join(relative for relative, _ in locked))

        toolbox = ToolBox(
            worktree, run_locked_tests if locked else run_tests, [rel for rel, _ in locked]
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_task},
        ]
        fix_attempts = 0
        call_counts: dict[str, int] = {}
        state_counts: dict[tuple[str, str | None], int] = {}

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
                    is_edit = call["name"] in EDIT_TOOLS
                    edit_path = call["arguments"].get("path") if is_edit else None
                    before = _edit_digest(worktree, edit_path)
                    output = toolbox.call(call["name"], call["arguments"])
                    if is_edit:
                        repeats = _count_call(call_counts, call)
                        after = _edit_digest(worktree, edit_path)
                        if after != before and isinstance(edit_path, str):
                            key = (normalize_path(edit_path), after)
                            state_counts[key] = state_counts.get(key, 0) + 1
                            repeats = max(repeats, state_counts[key])
                        if repeats >= MAX_REPEATED_EDITS:
                            result.message = REPEAT_ABORT
                            return result
                        if repeats >= 2:
                            output += "\n" + REPEAT_WARNING
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
            files = list(staged_files)
            blocked = [name for name in files if is_blocked(name)]
            if blocked:
                result.message = f"차단된 경로의 파일이 변경되어 커밋하지 않습니다: {blocked}"
                return result
            if _has_secret(diff_text):
                result.message = "비밀정보로 보이는 내용이 있어 커밋하지 않습니다."
                return result

            altered = [r for r, _ in locked if _digest(worktree / r) != locked_hashes[r]]
            if altered:
                result.message = f"잠근 테스트가 변경되어 커밋하지 않습니다: {altered}"
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
        if created:
            if not result.changed_files:
                result.changed_files = list(staged_files)
            skip = {normalize_path(relative) for relative, _ in locked}
            result.tests_changed = [
                name for name in result.changed_files if _is_test_file(name, skip)
            ]
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
