"""등록된 프로젝트와 실행 프로파일을 관리한다."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading

from dev_automation.paths import PathPolicyError, resolve_within
from dev_automation.runner import REJECTED, CommandSpec, RunResult, run_command
from dev_automation.sandbox import valid_image

CONFIG_VERSION = 1
PROJECT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
REQUIRED_KEYS = {"path", "python", "default_cwd", "commands", "allowed_env"}
OPTIONAL_KEYS = {"sandbox"}


@dataclass(frozen=True)
class CommandProfile:
    """프로젝트의 Python으로 실행하는 사전 정의 명령."""

    args: tuple[str, ...]
    allowed_extra_args: frozenset[str] = frozenset()


# 설정 파일은 이 프로파일의 이름만 참조한다. 임의의 명령은 설정으로 등록할 수 없다.
COMMAND_PROFILES = {
    "pytest": CommandProfile(
        args=("-m", "pytest"),
        allowed_extra_args=frozenset({"-q", "-v", "-x", "--tb=short"}),
    ),
}


class ProjectConfigError(ValueError):
    """프로젝트 설정이 잘못되었을 때 발생한다."""


@dataclass(frozen=True)
class Project:
    project_id: str
    root: Path
    default_cwd: str
    commands: Mapping[str, CommandSpec]
    # 테스트를 실행할 Docker 이미지. 없으면 Docker 격리를 쓰지 않는다.
    sandbox: str | None = None


def _string_list(project_id: str, data: dict, key: str) -> list[str]:
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProjectConfigError(f"{project_id}: {key}는 문자열 목록이어야 합니다.")
    return value


def _load_project(project_id: str, data: object, config_dir: Path) -> Project:
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise ProjectConfigError(f"잘못된 프로젝트 ID입니다: {project_id!r}")

    if (
        not isinstance(data, dict)
        or not REQUIRED_KEYS <= set(data)
        or not set(data) <= REQUIRED_KEYS | OPTIONAL_KEYS
    ):
        raise ProjectConfigError(
            f"{project_id}: 설정 항목은 {sorted(REQUIRED_KEYS)}이고, "
            f"선택 항목은 {sorted(OPTIONAL_KEYS)}뿐입니다."
        )

    for key in ("path", "python", "default_cwd"):
        if not isinstance(data[key], str):
            raise ProjectConfigError(f"{project_id}: {key}는 문자열이어야 합니다.")

    # 상대 경로는 설정 파일 위치를 기준으로 해석한다.
    root = (config_dir / data["path"]).resolve()
    if not root.is_dir():
        raise ProjectConfigError(f"{project_id}: 프로젝트 폴더가 없습니다: {root}")

    try:
        python = resolve_within(root, data["python"])
        default_cwd = resolve_within(root, data["default_cwd"])
    except PathPolicyError as exc:
        raise ProjectConfigError(f"{project_id}: {exc}") from exc

    if not python.is_file():
        raise ProjectConfigError(f"{project_id}: python은 파일이어야 합니다.")
    if not default_cwd.is_dir():
        raise ProjectConfigError(f"{project_id}: default_cwd는 폴더여야 합니다.")

    sandbox = data.get("sandbox")
    if "sandbox" in data and not valid_image(sandbox):
        raise ProjectConfigError(f"{project_id}: sandbox는 올바른 Docker 이미지 이름이어야 합니다.")

    allowed_env = frozenset(_string_list(project_id, data, "allowed_env"))

    commands = {}
    for name in _string_list(project_id, data, "commands"):
        profile = COMMAND_PROFILES.get(name)
        if profile is None:
            raise ProjectConfigError(f"{project_id}: 알 수 없는 프로파일입니다: {name}")
        commands[name] = CommandSpec(
            str(python),
            profile.args,
            profile.allowed_extra_args,
            allowed_env,
        )

    return Project(project_id, root, data["default_cwd"], commands, sandbox)


def load_projects(config_path: str | Path) -> dict[str, Project]:
    """설정 파일을 읽고 검증한다. 하나라도 잘못되면 전체를 거부한다."""
    path = Path(config_path).resolve()

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectConfigError(f"설정 파일을 읽을 수 없습니다: {exc}") from exc

    if not isinstance(config, dict) or set(config) != {"version", "projects"}:
        raise ProjectConfigError("설정 파일 최상위 항목은 version, projects여야 합니다.")
    if config["version"] != CONFIG_VERSION:
        raise ProjectConfigError(f"지원하지 않는 설정 버전입니다: {config['version']}")
    if not isinstance(config["projects"], dict):
        raise ProjectConfigError("projects는 객체여야 합니다.")

    return {
        project_id: _load_project(project_id, data, path.parent)
        for project_id, data in config["projects"].items()
    }


def run_project_command(
    projects: Mapping[str, Project],
    project_id: str,
    command_id: str,
    timeout: float,
    cwd: str | None = None,
    extra_args: Sequence[str] = (),
    *,
    cancel_event: threading.Event | None = None,
    log_dir: str | Path | None = None,
) -> RunResult:
    """등록된 프로젝트 안에서 그 프로젝트에 허용된 명령을 실행한다."""
    project = projects.get(project_id)
    if project is None:
        return RunResult(
            command_id, REJECTED, error=f"등록되지 않은 프로젝트입니다: {project_id}"
        )

    try:
        resolved_cwd = resolve_within(project.root, cwd or project.default_cwd)
    except PathPolicyError as exc:
        return RunResult(command_id, REJECTED, error=str(exc))

    return run_command(
        command_id,
        project.commands,
        resolved_cwd,
        timeout,
        extra_args,
        cancel_event=cancel_event,
        log_dir=log_dir,
    )
