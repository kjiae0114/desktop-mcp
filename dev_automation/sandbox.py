"""모델이 쓴 코드를 Docker 컨테이너 안에서만 테스트한다.

작업 폴더를 직접 마운트하지 않고 복사본을 마운트한다. 컨테이너가 `.git` 포인터 파일을
바꾸면 이후 호스트의 git 명령으로 탈출할 수 있기 때문이다. 복사본은 실행 후 버리며,
컨테이너가 쓴 내용을 호스트로 가져오는 경로는 없다.
"""

from collections.abc import Sequence
from dataclasses import replace
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import secrets
import shutil
import tempfile

from dev_automation.runner import (
    FAILED, REJECTED, SUCCEEDED, CommandSpec, RunResult, run_command,
)

IMAGE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?")
BUILD_COMMAND = "docker build -t desktop-mcp-sandbox:py312 -f sandbox/python.Dockerfile ."
DOCKER_ENV = frozenset({
    "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "DOCKER_HOST", "DOCKER_CONFIG",
    "PROGRAMDATA", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
})
CHECK_TIMEOUT_SECONDS = 60
CLEANUP_TIMEOUT_SECONDS = 30
DOCKER_ERROR_CODE = 125
COPY_EXCLUDES = frozenset({
    ".git", ".venv", ".venv-new", "runs", "__pycache__", ".pytest_cache",
})


def valid_image(image: object) -> bool:
    return isinstance(image, str) and IMAGE_PATTERN.fullmatch(image) is not None


def _ignore(directory: str, names: list[str]) -> set[str]:
    skipped = {name for name in names if name in COPY_EXCLUDES}
    # Windows 정션은 심볼릭 링크로 보이지 않아 복사가 링크를 따라가므로 건너뛴다.
    skipped |= {name for name in names if os.path.isjunction(os.path.join(directory, name))}
    return skipped


def _open_permissions(root: Path) -> None:
    """컨테이너의 uid 1000이 복사본에 쓸 수 있게 한다. Windows에서는 필요 없다."""
    if os.name == "nt":
        return
    for current, dirs, files in os.walk(root):
        for name in dirs:
            if not os.path.islink(os.path.join(current, name)):
                os.chmod(os.path.join(current, name), 0o777)
        for name in files:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                os.chmod(path, os.stat(path).st_mode | 0o666)
    os.chmod(root, 0o777)


def _container_dir(relative_cwd: str) -> str | None:
    """컨테이너 안의 작업 디렉터리. 작업 폴더 밖을 가리키면 None."""
    normalized = relative_cwd.replace("\\", "/")
    if PureWindowsPath(relative_cwd).drive or PurePosixPath(normalized).is_absolute():
        return None
    parts = [part for part in PurePosixPath(normalized).parts if part != "."]
    if ".." in parts or any(not part or part.startswith("-") for part in parts):
        return None
    return "/".join(["/work", *parts])


class DockerSandbox:
    def __init__(self, image: str, memory: str = "2g", cpus: str = "4", pids_limit: int = 256):
        if not valid_image(image):
            raise ValueError(f"올바르지 않은 이미지 이름입니다: {image!r}")
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit

    def _docker(self, *args: str) -> CommandSpec:
        return CommandSpec("docker", args, allowed_env=DOCKER_ENV)

    def check(self) -> str | None:
        """Docker 데몬과 이미지에 문제가 있으면 안내 문구를, 정상이면 None을 반환한다."""
        spec = self._docker("image", "inspect", self.image)
        result = run_command("docker", {"docker": spec}, Path.cwd(), CHECK_TIMEOUT_SECONDS)
        if result.ok:
            return None
        if result.returncode is None:
            return "Docker를 실행하지 못했습니다. Docker Desktop을 설치하고 실행하세요."
        detail = result.stderr.lower()
        if "no such image" in detail or "no such object" in detail:
            return f"테스트 이미지가 없습니다. 다음 명령으로 만드세요.\n  {BUILD_COMMAND}"
        return (
            "Docker에 연결하지 못했습니다. Docker Desktop이 실행 중인지 확인하세요. "
            f"이미지가 없다면 다음 명령으로 만드세요.\n  {BUILD_COMMAND}"
        )

    def run_args(
        self, name: str, source: Path, workdir: str, args: Sequence[str], pytest_args: Sequence[str]
    ) -> list[str]:
        return [
            "run", "--rm", "--name", name,
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.pids_limit),
            "--memory", self.memory,
            "--memory-swap", self.memory,
            "--cpus", self.cpus,
            "--read-only",
            "--tmpfs", "/tmp",
            "--user", "1000:1000",
            "-e", "HOME=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "--mount", f"type=bind,source={source},target=/work",
            "-w", workdir,
            self.image, "python", *args, *pytest_args,
        ]

    def run_tests(
        self,
        source: Path,
        relative_cwd: str,
        args: Sequence[str],
        pytest_args: Sequence[str],
        timeout: float,
    ) -> RunResult:
        workdir = _container_dir(relative_cwd)
        if workdir is None:
            return RunResult("pytest", REJECTED, error=f"올바르지 않은 작업 경로입니다: {relative_cwd}")

        name = f"desktop-mcp-sandbox-{secrets.token_hex(6)}"
        result: RunResult | None = None
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            copy = Path(temp) / "work"
            if "," in str(copy):
                return RunResult("pytest", REJECTED, error=f"임시 경로에 쉼표가 있습니다: {copy}")
            try:
                shutil.copytree(source, copy, symlinks=True, ignore=_ignore)
                _open_permissions(copy)
                spec = self._docker(*self.run_args(name, copy, workdir, args, pytest_args))
                result = run_command("pytest", {"pytest": spec}, copy, timeout)
            finally:
                # CLI를 죽여도 컨테이너는 살아 있으므로 정상 종료가 아니면 직접 지운다.
                if result is None or result.status not in (SUCCEEDED, FAILED) or result.error:
                    self._remove(name, copy)
        if result.returncode == DOCKER_ERROR_CODE:
            detail = result.stderr.strip()[-500:]
            result = replace(result, error=f"Docker 실행 실패: {detail}")
        return result

    def _remove(self, name: str, cwd: Path) -> None:
        try:
            spec = self._docker("rm", "-f", name)
            run_command("rm", {"rm": spec}, cwd, CLEANUP_TIMEOUT_SECONDS)
        except Exception:
            pass
