"""허용된 명령만 shell 없이 실행하고 결과를 수집한다."""

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

# 상태 값
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMED_OUT = "timed_out"
REJECTED = "rejected"  # 실행 전에 차단됨
START_FAILED = "start_failed"  # 프로세스를 시작하지 못함
CANCELLED = "cancelled"  # 호출자가 취소함

MAX_TIMEOUT_SECONDS = 3600.0
MAX_OUTPUT_CHARS = 100_000
# 프로세스 트리 종료 후 프로세스가 끝나기를 기다리는 시간
KILL_GRACE_SECONDS = 5.0
# 종료 여부, 시간 제한, 취소 요청을 확인하는 간격
POLL_INTERVAL_SECONDS = 0.1

# CreateProcess는 배치 파일을 cmd.exe로 실행하므로 shell=False의 의미가 사라진다.
BLOCKED_SUFFIXES = {".bat", ".cmd"}

# 프로세스 실행에 필요한 최소 환경 변수. 그 외 값은 명령별로 허용해야 전달된다.
BASE_ENV_VARS = frozenset({
    "COMSPEC", "PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "WINDIR",
})


@dataclass(frozen=True)
class CommandSpec:
    """사전에 등록된 명령. 실행 파일과 인자를 분리해 보관한다."""

    program: str
    args: tuple[str, ...] = ()
    # 호출자가 추가할 수 있는 인자. 정확히 일치하는 값만 허용한다.
    allowed_extra_args: frozenset[str] = frozenset()
    # 기본 환경 변수 외에 자식 프로세스로 전달할 환경 변수 이름.
    allowed_env: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RunResult:
    command_id: str
    status: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    duration_seconds: float = 0.0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status == SUCCEEDED


def _decode(data: bytes | None) -> str:
    text = (data or b"").decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n")


def _read_head(path: Path) -> tuple[str, bool]:
    """로그 파일의 앞부분만 읽어 (텍스트, 잘림 여부)를 반환한다."""
    limit = MAX_OUTPUT_CHARS * 4
    try:
        size = path.stat().st_size
        with open(path, "rb") as file:
            data = file.read(limit)
    except OSError:
        return "", False
    text = _decode(data)
    return text[:MAX_OUTPUT_CHARS], size > limit or len(text) > MAX_OUTPUT_CHARS


def _build_env(spec: CommandSpec) -> dict[str, str]:
    """허용된 환경 변수만 골라 비밀정보가 자식 프로세스로 넘어가지 않게 한다."""
    allowed = {name.upper() for name in BASE_ENV_VARS | spec.allowed_env}
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() in allowed
    }


def _kill_process_tree(process: subprocess.Popen) -> None:
    """프로세스와 자식 프로세스를 함께 종료한다."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=KILL_GRACE_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            # start_new_session=True로 시작했으므로 pgid는 pid와 같다.
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass

    # taskkill이 실패한 경우에도 직접 실행한 프로세스는 반드시 종료한다.
    process.kill()


def _validate(
    command_id: str,
    commands: Mapping[str, CommandSpec],
    extra_args: Sequence[str],
    cwd: str | Path,
    timeout: float,
) -> str | None:
    """요청이 잘못되었으면 차단 사유를, 정상이면 None을 반환한다."""
    spec = commands.get(command_id)
    if spec is None:
        return f"허용되지 않은 명령입니다: {command_id}"

    if Path(spec.program).suffix.lower() in BLOCKED_SUFFIXES:
        return f"배치 파일은 실행할 수 없습니다: {spec.program}"

    for arg in extra_args:
        if arg not in spec.allowed_extra_args:
            return f"허용되지 않은 인자입니다: {arg}"

    if not Path(cwd).is_dir():
        return f"작업 디렉터리가 존재하지 않습니다: {cwd}"

    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        return f"시간 제한은 0초 초과 {MAX_TIMEOUT_SECONDS:.0f}초 이하여야 합니다."

    return None


def run_command(
    command_id: str,
    commands: Mapping[str, CommandSpec],
    cwd: str | Path,
    timeout: float,
    extra_args: Sequence[str] = (),
    *,
    cancel_event: threading.Event | None = None,
    log_dir: str | Path | None = None,
) -> RunResult:
    """등록된 명령을 실행한다. 실행 관련 실패는 예외 대신 RunResult로 반환한다.

    출력은 항상 파일로 받는다. log_dir이 없으면 임시 폴더를 쓰고 끝나면 지운다.
    """
    reason = _validate(command_id, commands, extra_args, cwd, timeout)
    if reason is not None:
        return RunResult(command_id, REJECTED, error=reason)

    if cancel_event is not None and cancel_event.is_set():
        return RunResult(command_id, CANCELLED, error="실행 전에 취소되었습니다.")

    with ExitStack() as stack:
        if log_dir is None:
            # 자손이 로그 파일을 잡고 있어도 삭제 실패로 죽지 않게 한다.
            directory = Path(
                stack.enter_context(tempfile.TemporaryDirectory(ignore_cleanup_errors=True))
            )
        else:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)

        return _run_with_logs(
            command_id, commands[command_id], cwd, timeout, extra_args, cancel_event, directory
        )


def _run_with_logs(
    command_id: str,
    spec: CommandSpec,
    cwd: str | Path,
    timeout: float,
    extra_args: Sequence[str],
    cancel_event: threading.Event | None,
    directory: Path,
) -> RunResult:
    argv = [spec.program, *spec.args, *extra_args]
    stdout_path = directory / "stdout.log"
    stderr_path = directory / "stderr.log"
    started = time.monotonic()

    try:
        with open(stdout_path, "wb") as stdout_file, open(stderr_path, "wb") as stderr_file:
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=_build_env(spec),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=os.name != "nt",
            )
    except OSError as exc:
        return RunResult(command_id, START_FAILED, error=str(exc))

    error = None
    deadline = started + timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            status = CANCELLED
            error = "호출자가 실행을 취소했습니다."
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            status = TIMED_OUT
            error = f"시간 제한 {timeout}초를 초과했습니다."
            break
        try:
            process.wait(timeout=min(POLL_INTERVAL_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            continue
        status = SUCCEEDED if process.returncode == 0 else FAILED
        break

    if status in (CANCELLED, TIMED_OUT):
        _kill_process_tree(process)
        try:
            process.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            error += " 프로세스가 종료되지 않았을 수 있습니다."

    stdout_text, stdout_truncated = _read_head(stdout_path)
    stderr_text, stderr_truncated = _read_head(stderr_path)

    return RunResult(
        command_id=command_id,
        status=status,
        returncode=process.returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        error=error,
        duration_seconds=time.monotonic() - started,
        truncated=stdout_truncated or stderr_truncated,
    )
