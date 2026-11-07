"""명령줄 진입점: python -m dev_automation agent <project_id> "<작업>" """

import argparse
from pathlib import Path
import sys

from dev_automation.agent import SUCCEEDED, run_task
from dev_automation.llm import OllamaClient
from dev_automation.projects import ProjectConfigError, load_projects
from dev_automation.sandbox import DockerSandbox

REPO_ROOT = Path(__file__).resolve().parent.parent


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = _parse_args(argv)

    try:
        projects = load_projects(args.config)
    except ProjectConfigError as exc:
        _progress(f"설정 오류: {exc}")
        return 1
    project = projects.get(args.project_id)
    if project is None:
        _progress(f"등록되지 않은 프로젝트입니다: {args.project_id}")
        return 1

    sandbox = None
    if project.sandbox:
        sandbox = DockerSandbox(project.sandbox)
        _progress(f"테스트 격리: Docker({project.sandbox})")
    elif args.unsafe_host_tests:
        _progress("경고: 테스트 격리 없음. 모델이 쓴 코드가 이 PC에서 직접 실행됩니다.")
    else:
        _progress(
            "이 프로젝트에는 sandbox(Docker 이미지) 설정이 없어 실행하지 않습니다. "
            "config/projects.json에 sandbox를 추가하거나, 위험을 감수하고 "
            "--unsafe-host-tests를 지정하세요."
        )
        return 1

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
            on_progress=_progress,
        )
    finally:
        # 끝난 뒤 5분 동안 VRAM을 붙잡지 않도록 모델을 바로 내린다.
        llm.unload()

    _progress(f"\n상태: {result.status}")
    _progress(f"메시지: {result.message}")
    _progress(f"브랜치: {result.branch}")
    _progress(f"변경 파일: {', '.join(result.changed_files) or '(없음)'}")
    _progress(f"diff: {result.diff_path or '(없음)'}")
    if result.tests_changed:
        _progress("경고: 모델이 수정한 테스트가 있습니다. 테스트를 약화하지 않았는지 검토하세요.")
        for name in result.tests_changed:
            _progress(f"  {name}")
    if result.status == SUCCEEDED:
        _progress("\n검토 후 다음 명령을 사용하세요.")
        _progress(f"  git diff main..{result.branch}")
        _progress(f"  git merge {result.branch}")
    return 0 if result.status == SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
