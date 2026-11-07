"""에이전트 작업을 백그라운드 프로세스로 돌리고, 목록·로그·취소를 제공한다.

작업마다 runs/jobs/<job_id>/ 폴더를 만든다.
- job.json: 작업 정보와 상태 (부모가 만들고, 자식이 끝날 때 결과를 기록)
- output.log: 자식 프로세스의 출력 전체
- cancel: 이 파일이 생기면 자식이 다음 진행 지점에서 작업을 멈추고 정리한다
"""

from collections.abc import Callable, Sequence
import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

from dev_automation.agent import TaskCancelled

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

JOB_FILE = "job.json"
LOG_FILE = "output.log"
CANCEL_FILE = "cancel"
JOB_ID_PATTERN = re.compile(r"job-\d{8}-\d{6}-[0-9a-f]{4}")


class JobNotFoundError(ValueError):
    """job_id가 잘못되었거나 기록이 없을 때 발생한다."""


class JobCancelled(TaskCancelled):
    """취소 요청을 받아 작업을 멈출 때 발생한다. 에이전트는 이걸 받으면 정리 후 실패로 끝낸다."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def job_dir(jobs_dir: Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or not JOB_ID_PATTERN.fullmatch(job_id):
        raise JobNotFoundError(f"잘못된 작업 ID입니다: {job_id!r}")
    path = Path(jobs_dir) / job_id
    if not (path / JOB_FILE).is_file():
        raise JobNotFoundError(f"작업을 찾을 수 없습니다: {job_id}")
    return path


def _read(path: Path) -> dict:
    return json.loads((path / JOB_FILE).read_text(encoding="utf-8"))


def _write(path: Path, record: dict) -> None:
    # 임시 파일에 쓴 뒤 교체해, 읽는 쪽이 반쯤 쓴 파일을 보지 않게 한다.
    temp = path / f"{JOB_FILE}.tmp"
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path / JOB_FILE)


def pid_alive(pid: int) -> bool:
    """프로세스가 살아 있는지 확인한다. Windows에서 os.kill(pid, 0)은 프로세스를 죽이므로 쓰지 않는다."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def start(jobs_dir: Path, agent_argv: Sequence[str], task: str, cwd: Path) -> dict:
    """agent_argv(= `agent ...` 인자)를 분리된 자식 프로세스로 실행하고 작업 기록을 반환한다."""
    jobs_dir = Path(jobs_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = f"job-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    path = jobs_dir / job_id
    path.mkdir()
    record = {
        "job_id": job_id,
        "task": task,
        "argv": list(agent_argv),
        "status": RUNNING,
        "pid": None,
        "created_at": _now(),
        "finished_at": None,
        "exit_code": None,
        "branch": None,
        "message": None,
    }
    _write(path, record)

    command = [sys.executable, "-m", "dev_automation", *agent_argv, "--job-dir", str(path)]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    flags = 0
    if sys.platform == "win32":
        # 창 없이, 이 터미널을 닫아도 계속 돌도록 분리한다.
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    with open(path / LOG_FILE, "wb") as log:
        kwargs = dict(
            cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, close_fds=True,
        )
        try:
            # 터미널이 Job Object로 자식까지 정리하는 환경에서도 살아남도록 시도한다.
            process = subprocess.Popen(
                command, creationflags=flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0),
                **kwargs,
            )
        except OSError:
            process = subprocess.Popen(command, creationflags=flags, **kwargs)

    record["pid"] = process.pid
    _write(path, record)
    return record


def refresh(path: Path) -> dict:
    """기록을 읽고, '실행 중'인데 프로세스가 없으면 비정상 종료로 고친다."""
    record = _read(path)
    if record.get("status") == RUNNING and record.get("pid") and not pid_alive(record["pid"]):
        # 자식이 결과를 쓰는 순간과 겹쳤을 수 있으니 한 번 더 읽는다.
        record = _read(path)
        if record.get("status") == RUNNING:
            record["status"] = INTERRUPTED
            record["finished_at"] = _now()
            record["message"] = "프로세스가 결과를 남기지 않고 끝났습니다. output.log를 확인하세요."
            _write(path, record)
    return record


def list_jobs(jobs_dir: Path, limit: int = 20) -> list[dict]:
    jobs_dir = Path(jobs_dir)
    if not jobs_dir.is_dir():
        return []
    records = []
    for path in sorted(jobs_dir.iterdir(), reverse=True):
        if not JOB_ID_PATTERN.fullmatch(path.name) or not (path / JOB_FILE).is_file():
            continue
        try:
            records.append(refresh(path))
        except (OSError, ValueError):
            continue
        if len(records) >= limit:
            break
    return records


def get(jobs_dir: Path, job_id: str) -> dict:
    return refresh(job_dir(jobs_dir, job_id))


def log_path(jobs_dir: Path, job_id: str) -> Path:
    return job_dir(jobs_dir, job_id) / LOG_FILE


def request_cancel(jobs_dir: Path, job_id: str) -> dict:
    path = job_dir(jobs_dir, job_id)
    record = refresh(path)
    if record.get("status") == RUNNING:
        (path / CANCEL_FILE).write_text(_now(), encoding="utf-8")
    return record


def cancel_checker(path: Path) -> Callable[[], None]:
    """취소 파일이 있으면 한 번만 JobCancelled를 던지는 함수를 돌려준다.

    한 번만 던지는 이유: 에이전트의 정리 단계(워크트리 삭제, 모델 내리기)도 진행 메시지를 쓰므로,
    거기서 또 던지면 정리가 중간에 끊긴다.
    """
    state = {"raised": False}

    def check() -> None:
        if not state["raised"] and (Path(path) / CANCEL_FILE).exists():
            state["raised"] = True
            raise JobCancelled("사용자가 취소했습니다.")

    return check


def finish(path: Path, exit_code: int, branch: str | None, message: str | None) -> None:
    """자식 프로세스가 끝날 때 결과를 기록한다."""
    record = _read(path)
    cancelled = (Path(path) / CANCEL_FILE).exists()
    if cancelled and exit_code != 0:
        status = CANCELLED
    else:
        status = SUCCEEDED if exit_code == 0 else FAILED
    record.update(
        status=status, exit_code=exit_code, branch=branch, message=message, finished_at=_now()
    )
    _write(path, record)
