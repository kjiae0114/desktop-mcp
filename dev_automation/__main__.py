"""명령줄 진입점.

  python -m dev_automation agent <project_id> "<작업>" [--background]
  python -m dev_automation jobs              # 백그라운드 작업 목록
  python -m dev_automation log <job_id> [-f] # 출력 보기 (-f: 끝날 때까지 따라가기)
  python -m dev_automation cancel <job_id>   # 취소 요청

  ChatGPT에 테스트 작성을 맡기는 반자동 흐름:
  python -m dev_automation brief <project_id> "<요구사항>"   # 요청문을 만들어 클립보드에 복사
  python -m dev_automation implement <brief_id> [--background] # 클립보드의 답장으로 구현
  python -m dev_automation copy <brief_id>                     # 마지막 요청문(재요청 포함) 다시 복사
  python -m dev_automation briefs                              # 요청 목록
"""

import argparse
from datetime import datetime
from pathlib import Path
import sys
import time

from dev_automation import handoff, jobs
from dev_automation.agent import CONTEXT_CHARS_WHEN_ON, SUCCEEDED, run_task
from dev_automation.llm import OllamaClient
from dev_automation.projects import ProjectConfigError, load_projects
from dev_automation.sandbox import DockerSandbox

REPO_ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = REPO_ROOT / "runs" / "jobs"
BRIEFS_DIR = REPO_ROOT / "runs" / "briefs"
DEFAULT_CONFIG = str(REPO_ROOT / "config" / "projects.json")
DEFAULT_MODEL = "qwen3.5:4b-q4_K_M"


def _copy_to_clipboard(text: str) -> bool:
    try:
        import pyperclip

        pyperclip.copy(text)
        return True
    except Exception:
        return False


def _read_clipboard() -> str:
    import pyperclip

    return pyperclip.paste() or ""


def _progress(message: str) -> None:
    print(message, flush=True)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m dev_automation")
    commands = parser.add_subparsers(dest="command", required=True)
    agent = commands.add_parser("agent", help="로컬 모델로 코드 수정 작업을 실행한다.")
    agent.add_argument("project_id")
    agent.add_argument("task")
    agent.add_argument("--model", default=DEFAULT_MODEL)
    agent.add_argument("--config", default=DEFAULT_CONFIG)
    agent.add_argument("--runs-dir", default=str(REPO_ROOT / "runs" / "agent"))
    agent.add_argument("--max-steps", type=int, default=25)
    agent.add_argument(
        "--test", action="append", default=[], metavar="경로",
        help="수정을 금지할 테스트 파일(프로젝트 루트 기준). 반복 가능",
    )
    agent.add_argument(
        "--unsafe-host-tests", action="store_true",
        help="Docker 격리 없이 모델이 쓴 코드를 이 PC에서 직접 실행한다. sandbox 설정이 없을 때만 쓴다.",
    )
    agent.add_argument(
        "--context", action="store_true",
        help="(실험) 저장소 지도와 관련 파일 내용을 첫 메시지에 넣는다. 4B 기준 효과는 측정되지 않았다.",
    )
    agent.add_argument(
        "--self-test", action="store_true",
        help="(실험) --test가 없을 때 모델이 요구사항 테스트를 직접 쓰게 한다. 4B 기준 효과는 측정되지 않았다.",
    )
    agent.add_argument(
        "--background", action="store_true",
        help="백그라운드 프로세스로 실행하고 바로 돌아온다. 터미널을 닫아도 계속 돈다.",
    )
    # 백그라운드 자식 프로세스에만 붙는 내부 옵션
    agent.add_argument("--job-dir", help=argparse.SUPPRESS)
    # implement가 넘기는 내부 옵션: 끝난 뒤 테스트 정리와 재요청문 작성에 쓴다
    agent.add_argument("--brief-id", help=argparse.SUPPRESS)

    brief = commands.add_parser("brief", help="ChatGPT에 보낼 테스트 작성 요청문을 만든다.")
    brief.add_argument("project_id")
    brief.add_argument("requirement")
    brief.add_argument("--config", default=DEFAULT_CONFIG)
    brief.add_argument("--no-copy", action="store_true", help="클립보드에 복사하지 않는다.")
    implement = commands.add_parser(
        "implement", help="ChatGPT 답장의 테스트로 로컬 에이전트를 돌린다(답장은 클립보드에서 읽음)."
    )
    implement.add_argument("brief_id")
    implement.add_argument("--reply", metavar="파일", help="클립보드 대신 이 파일에서 답장을 읽는다.")
    implement.add_argument("--background", action="store_true")
    implement.add_argument("--model", default=DEFAULT_MODEL)
    implement.add_argument("--max-steps", type=int, default=25)
    implement.add_argument("--config", default=DEFAULT_CONFIG)
    copy = commands.add_parser("copy", help="요청의 마지막 요청문(재요청 포함)을 다시 복사한다.")
    copy.add_argument("brief_id")
    commands.add_parser("briefs", help="요청 목록을 본다.")

    commands.add_parser("jobs", help="백그라운드 작업 목록을 본다.")
    log = commands.add_parser("log", help="백그라운드 작업의 출력을 본다.")
    log.add_argument("job_id")
    log.add_argument("-f", "--follow", action="store_true", help="작업이 끝날 때까지 출력을 따라간다.")
    cancel = commands.add_parser("cancel", help="백그라운드 작업을 취소한다.")
    cancel.add_argument("job_id")
    return parser.parse_args(argv)


def _elapsed(record: dict) -> str:
    try:
        start = datetime.fromisoformat(record["created_at"])
        finished = record.get("finished_at")
        end = datetime.fromisoformat(finished) if finished else datetime.now()
        return f"{int((end - start).total_seconds())}초"
    except (KeyError, TypeError, ValueError):
        return "?"


def _cmd_jobs() -> int:
    records = jobs.list_jobs(JOBS_DIR)
    if not records:
        _progress("백그라운드 작업이 없습니다.")
        return 0
    for record in records:
        task = record.get("task", "")
        task = task if len(task) <= 40 else task[:39] + "…"
        _progress(f"{record['job_id']}  {record['status']:<11}  {_elapsed(record):>6}  {task}")
        if record.get("branch") and record["status"] == jobs.SUCCEEDED:
            _progress(f"    브랜치: {record['branch']}")
    return 0


def _cmd_log(job_id: str, follow: bool) -> int:
    try:
        path = jobs.log_path(JOBS_DIR, job_id)
    except jobs.JobNotFoundError as exc:
        _progress(str(exc))
        return 1
    offset = 0
    try:
        while True:
            with open(path, "rb") as file:
                file.seek(offset)
                data = file.read()
            if data:
                offset += len(data)
                sys.stdout.write(data.decode("utf-8", errors="replace"))
                sys.stdout.flush()
            if not follow or jobs.get(JOBS_DIR, job_id)["status"] != jobs.RUNNING:
                if follow:
                    # 상태가 바뀌기 직전에 쓰인 마지막 출력까지 보여 준다.
                    with open(path, "rb") as file:
                        file.seek(offset)
                        sys.stdout.write(file.read().decode("utf-8", errors="replace"))
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    record = jobs.get(JOBS_DIR, job_id)
    _progress(f"\n[작업 상태: {record['status']}]")
    return 0


def _cmd_cancel(job_id: str) -> int:
    try:
        record = jobs.request_cancel(JOBS_DIR, job_id)
    except jobs.JobNotFoundError as exc:
        _progress(str(exc))
        return 1
    if record["status"] != jobs.RUNNING:
        _progress(f"이미 끝난 작업입니다: {record['status']}")
        return 0
    _progress(
        "취소를 요청했습니다. 다음 진행 지점에서 멈추고 작업 브랜치와 워크트리를 정리합니다.\n"
        "테스트 실행 중이었다면 그 테스트가 끝난 뒤 멈춥니다. 확인: "
        f"python -m dev_automation log {job_id} -f"
    )
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    try:
        projects = load_projects(args.config)
    except ProjectConfigError as exc:
        _progress(f"설정 오류: {exc}")
        return 1
    project = projects.get(args.project_id)
    if project is None:
        _progress(f"등록되지 않은 프로젝트입니다: {args.project_id}")
        return 1
    try:
        brief_id, prompt = handoff.create_brief(BRIEFS_DIR, args.project_id, project.root, args.requirement)
    except handoff.HandoffError as exc:
        _progress(str(exc))
        return 1
    copied = not args.no_copy and _copy_to_clipboard(prompt)
    _progress(f"요청문을 만들었습니다: {brief_id} ({len(prompt)}자)")
    _progress(f"  파일: {BRIEFS_DIR / brief_id / 'prompt.md'}")
    _progress("  클립보드에 복사했습니다." if copied else "  클립보드 복사는 하지 않았습니다. 위 파일을 열어 복사하세요.")
    _progress("\n다음 순서:")
    _progress("  1. ChatGPT에 붙여넣고 보냅니다.")
    _progress("  2. 답장 전체를 복사합니다(답장 아래 복사 버튼).")
    _progress(f"  3. python -m dev_automation implement {brief_id}")
    return 0


def _cmd_copy(brief_id: str) -> int:
    try:
        text = handoff.latest_prompt(handoff.brief_dir(BRIEFS_DIR, brief_id))
    except (handoff.HandoffError, OSError) as exc:
        _progress(str(exc))
        return 1
    if _copy_to_clipboard(text):
        _progress(f"클립보드에 복사했습니다({len(text)}자). ChatGPT의 같은 대화에 붙여넣으세요.")
        return 0
    _progress("클립보드에 복사하지 못했습니다.")
    return 1


def _cmd_briefs() -> int:
    records = handoff.list_briefs(BRIEFS_DIR)
    if not records:
        _progress("요청이 없습니다.")
        return 0
    for record in records:
        text = record["requirement"].splitlines()[0]
        text = text if len(text) <= 40 else text[:39] + "…"
        _progress(f"{record['brief_id']}  {record['status']:<14}  {record['round']}회  {text}")
        if record.get("branch"):
            _progress(f"    브랜치: {record['branch']}")
    return 0


def _cmd_implement(args: argparse.Namespace) -> int:
    try:
        path = handoff.brief_dir(BRIEFS_DIR, args.brief_id)
        record = handoff.load(path)
        projects = load_projects(args.config)
    except (handoff.HandoffError, ProjectConfigError, OSError) as exc:
        _progress(str(exc))
        return 1
    project = projects.get(record["project_id"])
    if project is None:
        _progress(f"등록되지 않은 프로젝트입니다: {record['project_id']}")
        return 1
    if not project.sandbox:
        # 바깥 AI가 쓴 테스트를 이 PC에서 직접 실행하지 않는다.
        _progress("이 흐름은 Docker sandbox가 설정된 프로젝트에서만 씁니다(바깥에서 받은 코드를 격리해 실행).")
        return 1
    try:
        if args.reply:
            reply = Path(args.reply).read_text(encoding="utf-8")
        else:
            reply = _read_clipboard()
        files = handoff.parse_reply(reply)
    except (handoff.HandoffError, OSError) as exc:
        _progress(f"답장을 쓸 수 없습니다: {exc}")
        return 1
    previous = {name for item in record.get("history", []) for name in item.get("tests", [])}
    try:
        handoff.place_tests(project.root, files, previous)
    except handoff.HandoffError as exc:
        _progress(str(exc))
        return 1
    record = handoff.record_reply(path, reply, files)
    _progress(f"테스트 {len(files)}개를 받았습니다({record['round']}회차):")
    for relative, body in files:
        _progress(f"  {relative} ({body.count('def test_')}개 테스트)")

    agent_argv = [
        "agent", record["project_id"], record["requirement"],
        "--model", args.model, "--max-steps", str(args.max_steps), "--config", args.config,
        "--brief-id", args.brief_id,
    ]
    for relative, _ in files:
        agent_argv += ["--test", relative]
    if args.background:
        agent_argv.append("--background")
    return _run_agent(_parse_args(agent_argv), agent_argv)


def _finish_brief(args: argparse.Namespace, project, result, say) -> None:
    """implement로 시작한 실행의 뒷정리: 둔 테스트 지우기, 성공 기록 또는 재요청문 쓰기."""
    path = handoff.brief_dir(BRIEFS_DIR, args.brief_id)
    record = handoff.load(path)
    copy_dir = path / f"tests-{record['round']}"
    expected = {}
    for relative in record["tests"]:
        try:
            expected[relative] = (copy_dir / relative).read_text(encoding="utf-8")
        except OSError:
            pass
    handoff.remove_tests(project.root, record["tests"], expected)
    if result is not None and result.status == SUCCEEDED:
        handoff.mark_done(path, result.branch)
        return
    failure = result.message if result is not None else "에이전트가 중간에 멈췄습니다."
    handoff.write_retry(path, failure)
    say("\n구현에 실패해 ChatGPT에 보낼 재요청문을 만들었습니다(테스트가 틀렸을 수도 있습니다).")
    say(f"  python -m dev_automation copy {args.brief_id}   # 복사해서 같은 대화에 붙여넣기")
    say(f"  python -m dev_automation implement {args.brief_id}   # 새 답장으로 다시 구현")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parse_args(argv)
    if args.command == "jobs":
        return _cmd_jobs()
    if args.command == "log":
        return _cmd_log(args.job_id, args.follow)
    if args.command == "cancel":
        return _cmd_cancel(args.job_id)
    if args.command == "brief":
        return _cmd_brief(args)
    if args.command == "copy":
        return _cmd_copy(args.brief_id)
    if args.command == "briefs":
        return _cmd_briefs()
    if args.command == "implement":
        return _cmd_implement(args)
    if args.job_dir:
        return _run_job(args)
    return _run_agent(args, argv)


def _run_job(args: argparse.Namespace) -> int:
    """백그라운드 자식 프로세스: 취소 확인을 끼워 에이전트를 돌리고 결과를 job.json에 남긴다."""
    path = Path(args.job_dir)
    check = jobs.cancel_checker(path)
    outcome: dict = {"code": 1, "branch": None, "message": None}

    def progress(message: str) -> None:
        _progress(message)
        check()

    try:
        outcome["code"] = _run_agent(args, None, progress, outcome)
    except jobs.JobCancelled as exc:
        outcome["message"] = str(exc)
        _progress(f"\n{exc}")
    except Exception as exc:
        outcome["message"] = f"예상하지 못한 오류: {exc}"
        _progress(outcome["message"])
    finally:
        jobs.finish(path, outcome["code"], outcome["branch"], outcome["message"])
    return outcome["code"]


def _run_agent(
    args: argparse.Namespace,
    argv: list[str] | None,
    say=_progress,
    outcome: dict | None = None,
) -> int:
    try:
        projects = load_projects(args.config)
    except ProjectConfigError as exc:
        say(f"설정 오류: {exc}")
        return 1
    project = projects.get(args.project_id)
    if project is None:
        say(f"등록되지 않은 프로젝트입니다: {args.project_id}")
        return 1

    sandbox = None
    if project.sandbox:
        sandbox = DockerSandbox(project.sandbox)
        say(f"테스트 격리: Docker({project.sandbox})")
    elif args.unsafe_host_tests:
        say("경고: 테스트 격리 없음. 모델이 쓴 코드가 이 PC에서 직접 실행됩니다.")
    else:
        say(
            "이 프로젝트에는 sandbox(Docker 이미지) 설정이 없어 실행하지 않습니다. "
            "config/projects.json에 sandbox를 추가하거나, 위험을 감수하고 "
            "--unsafe-host-tests를 지정하세요."
        )
        return 1

    if args.background:
        running = [r for r in jobs.list_jobs(JOBS_DIR) if r["status"] == jobs.RUNNING]
        child_argv = [arg for arg in argv if arg != "--background"]
        record = jobs.start(JOBS_DIR, child_argv, args.task, REPO_ROOT)
        say(f"\n백그라운드로 시작했습니다: {record['job_id']} (PID {record['pid']})")
        if running:
            say(
                f"참고: 이미 도는 작업이 {len(running)}개 있습니다. 모델은 하나를 같이 쓰므로 "
                "응답이 번갈아 처리되어 느려집니다."
            )
        say("확인 방법:")
        say("  python -m dev_automation jobs")
        say(f"  python -m dev_automation log {record['job_id']} -f")
        say(f"  python -m dev_automation cancel {record['job_id']}")
        return 0

    llm = OllamaClient(args.model)
    result = None
    try:
        result = run_task(
            project,
            args.task,
            llm,
            Path(args.runs_dir),
            max_steps=args.max_steps,
            context_chars=CONTEXT_CHARS_WHEN_ON if args.context else 0,
            self_test=args.self_test,
            protected_tests=args.test,
            sandbox=sandbox,
            on_progress=say,
        )
    finally:
        # 끝난 뒤 5분 동안 VRAM을 붙잡지 않도록 모델을 바로 내린다.
        llm.unload()
        if args.brief_id and result is None:
            # 예외로 끝났어도 프로젝트에 둔 테스트는 치운다.
            _finish_brief(args, project, result, say)

    if outcome is not None:
        outcome["branch"] = result.branch if result.status == SUCCEEDED else None
        outcome["message"] = result.message
    say(f"\n상태: {result.status}")
    say(f"메시지: {result.message}")
    say(f"브랜치: {result.branch}")
    say(f"변경 파일: {', '.join(result.changed_files) or '(없음)'}")
    say(f"diff: {result.diff_path or '(없음)'}")
    if result.tests_changed:
        say("경고: 모델이 수정한 테스트가 있습니다. 테스트를 약화하지 않았는지 검토하세요.")
        for name in result.tests_changed:
            say(f"  {name}")
    if result.status == SUCCEEDED:
        say("\n검토 후 다음 명령을 사용하세요.")
        say(f"  git diff main..{result.branch}")
        say(f"  git merge {result.branch}")
    if args.brief_id:
        # 다음에 할 일(재요청)이 출력 맨 끝에 보이도록 결과 요약 뒤에 정리한다.
        _finish_brief(args, project, result, say)
    return 0 if result.status == SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
