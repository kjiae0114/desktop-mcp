"""명령줄 진입점.

  python -m dev_automation agent <project_id> "<작업>" [--background]
  python -m dev_automation jobs              # 백그라운드 작업 목록
  python -m dev_automation log <job_id> [-f] # 출력 보기 (-f: 끝날 때까지 따라가기)
  python -m dev_automation cancel <job_id>   # 취소 요청
"""

import argparse
from datetime import datetime
from pathlib import Path
import sys
import time

from dev_automation import jobs
from dev_automation.agent import SUCCEEDED, run_task
from dev_automation.llm import OllamaClient
from dev_automation.projects import ProjectConfigError, load_projects
from dev_automation.sandbox import DockerSandbox

REPO_ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = REPO_ROOT / "runs" / "jobs"


def _progress(message: str) -> None:
    print(message, flush=True)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m dev_automation")
    commands = parser.add_subparsers(dest="command", required=True)
    agent = commands.add_parser("agent", help="로컬 모델로 코드 수정 작업을 실행한다.")
    agent.add_argument("project_id")
    agent.add_argument("task")
    agent.add_argument("--model", default="qwen3.5:4b-q4_K_M")
    agent.add_argument("--config", default=str(REPO_ROOT / "config" / "projects.json"))
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
        "--background", action="store_true",
        help="백그라운드 프로세스로 실행하고 바로 돌아온다. 터미널을 닫아도 계속 돈다.",
    )
    # 백그라운드 자식 프로세스에만 붙는 내부 옵션
    agent.add_argument("--job-dir", help=argparse.SUPPRESS)

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
    try:
        result = run_task(
            project,
            args.task,
            llm,
            Path(args.runs_dir),
            max_steps=args.max_steps,
            protected_tests=args.test,
            sandbox=sandbox,
            on_progress=say,
        )
    finally:
        # 끝난 뒤 5분 동안 VRAM을 붙잡지 않도록 모델을 바로 내린다.
        llm.unload()

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
    return 0 if result.status == SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
