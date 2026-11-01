import json
import os
from pathlib import Path
import sys

import pytest

from dev_automation import runner
from dev_automation.paths import PathPolicyError, resolve_within
from dev_automation.projects import (
    COMMAND_PROFILES,
    ProjectConfigError,
    load_projects,
    run_project_command,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def project_root(tmp_path):
    root = tmp_path / "root" / "project"
    (root / "src").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "file.txt").write_text("x")
    # 실제 인터프리터 대신 존재 여부 검증에 쓰는 파일
    (root / "bin" / "python.exe").write_text("")
    (root / "test_ok.py").write_text("def test_ok():\n    pass\n")
    return root


def project_data(**overrides) -> dict:
    data = {
        "path": "project",
        "python": "bin/python.exe",
        "default_cwd": ".",
        "commands": ["pytest"],
        "allowed_env": [],
    }
    data.update(overrides)
    return data


def write_config(project_root: Path, projects: dict, version: int = 1) -> Path:
    path = project_root.parent / "projects.json"
    path.write_text(json.dumps({"version": version, "projects": projects}))
    return path


@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        "..",
        "src/../..",
        "..\\..",
        "C:\\Windows",
        "C:Windows",
        "\\Windows",
        "/Windows",
        "\\\\server\\share",
        "file.txt:stream",
    ],
)
def test_resolve_within_rejects_escapes(project_root, bad_path):
    with pytest.raises(PathPolicyError):
        resolve_within(project_root, bad_path)


def test_resolve_within_accepts_inner_paths(project_root):
    assert resolve_within(project_root, ".") == project_root.resolve()
    assert resolve_within(project_root, "src") == (project_root / "src").resolve()


def test_resolve_within_rejects_missing_path(project_root):
    with pytest.raises(PathPolicyError):
        resolve_within(project_root, "missing")


@pytest.mark.skipif(os.name != "nt", reason="Windows 정션 전용")
def test_resolve_within_rejects_junction_escape(project_root, tmp_path):
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(project_root / "link"))

    with pytest.raises(PathPolicyError):
        resolve_within(project_root, "link")


def test_resolve_within_rejects_symlink_escape(project_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (project_root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("심볼릭 링크 생성 권한 없음")

    with pytest.raises(PathPolicyError):
        resolve_within(project_root, "link")


def test_load_project(project_root):
    projects = load_projects(write_config(project_root, {"demo": project_data()}))

    project = projects["demo"]
    spec = project.commands["pytest"]
    assert project.root == project_root.resolve()
    assert Path(spec.program) == (project_root / "bin" / "python.exe").resolve()
    assert spec.args == COMMAND_PROFILES["pytest"].args


def test_sandbox_is_optional(project_root):
    projects = load_projects(write_config(project_root, {"a": project_data()}))
    assert projects["a"].sandbox is None

    data = project_data(sandbox="desktop-mcp-sandbox:py312")
    projects = load_projects(write_config(project_root, {"a": data}))
    assert projects["a"].sandbox == "desktop-mcp-sandbox:py312"


@pytest.mark.parametrize("missing", ["path", "python", "default_cwd", "commands", "allowed_env"])
def test_required_keys_are_still_required(project_root, missing):
    data = project_data()
    del data[missing]

    with pytest.raises(ProjectConfigError):
        load_projects(write_config(project_root, {"demo": data}))


def test_allowed_env_reaches_command_spec(project_root):
    data = project_data(allowed_env=["MY_VAR"])

    projects = load_projects(write_config(project_root, {"demo": data}))

    assert projects["demo"].commands["pytest"].allowed_env == {"MY_VAR"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"commands": ["powershell -c whoami"]},
        {"commands": "pytest"},
        {"python": "../outside.exe"},
        {"python": "missing.exe"},
        {"python": "src"},
        {"default_cwd": ".."},
        {"default_cwd": "file.txt"},
        {"path": "missing"},
        {"path": 1},
        {"allowed_env": "PATH"},
        {"shell_command": "rm -rf ."},
        {"sandbox": "-v"},
        {"sandbox": "UPPER:tag"},
        {"sandbox": "img name"},
        {"sandbox": 1},
        {"sandbox": None},
    ],
)
def test_invalid_project_settings_are_rejected(project_root, overrides):
    config = write_config(project_root, {"demo": project_data(**overrides)})

    with pytest.raises(ProjectConfigError):
        load_projects(config)


@pytest.mark.parametrize("project_id", ["", "../x", "Has Space", "UPPER"])
def test_invalid_project_id_is_rejected(project_root, project_id):
    config = write_config(project_root, {project_id: project_data()})

    with pytest.raises(ProjectConfigError):
        load_projects(config)


def test_invalid_config_files_are_rejected(project_root):
    with pytest.raises(ProjectConfigError):
        load_projects(project_root / "missing.json")

    broken = project_root / "broken.json"
    broken.write_text("{")
    with pytest.raises(ProjectConfigError):
        load_projects(broken)

    with pytest.raises(ProjectConfigError):
        load_projects(write_config(project_root, {}, version=2))


def test_unregistered_project_is_rejected(project_root):
    projects = load_projects(write_config(project_root, {"demo": project_data()}))

    result = run_project_command(projects, "other", "pytest", 30)

    assert result.status == runner.REJECTED


def test_unlisted_command_is_rejected(project_root):
    projects = load_projects(write_config(project_root, {"demo": project_data()}))

    result = run_project_command(projects, "demo", "git_push", 30)

    assert result.status == runner.REJECTED


def test_cwd_escape_is_rejected(project_root):
    projects = load_projects(write_config(project_root, {"demo": project_data()}))

    result = run_project_command(projects, "demo", "pytest", 30, cwd="..")

    assert result.status == runner.REJECTED


def test_unlisted_pytest_arg_is_rejected(project_root):
    projects = load_projects(write_config(project_root, {"demo": project_data()}))

    result = run_project_command(
        projects, "demo", "pytest", 30, extra_args=["-c", "evil.ini"]
    )

    assert result.status == runner.REJECTED


def test_runs_registered_command_end_to_end(project_root):
    # 등록된 python 자리에 현재 인터프리터를 넣어 실제 pytest 실행까지 확인한다.
    projects = load_projects(write_config(project_root, {"demo": project_data()}))
    project = projects["demo"]
    spec = project.commands["pytest"]
    project.commands["pytest"] = runner.CommandSpec(
        sys.executable, spec.args, spec.allowed_extra_args
    )

    result = run_project_command(projects, "demo", "pytest", 120, extra_args=["-q"])

    assert result.ok, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_repository_config_is_valid():
    # 에이전트 워크트리처럼 .venv-new가 없는 체크아웃에서는 검증할 수 없다.
    if not (REPO_ROOT / ".venv-new").exists():
        pytest.skip(".venv-new가 없습니다.")
    projects = load_projects(REPO_ROOT / "config" / "projects.json")

    project = projects["desktop-mcp"]
    assert project.root == REPO_ROOT
    assert set(project.commands) == {"pytest"}
    assert project.sandbox == "desktop-mcp-sandbox:py312"
