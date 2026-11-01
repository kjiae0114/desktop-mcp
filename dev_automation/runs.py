"""백그라운드 실행 이력을 디스크에 기록하고 관리한다."""

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import threading
from uuid import uuid4

from dev_automation.projects import Project, run_project_command
from dev_automation.runner import CANCELLED, FAILED

QUEUED = "queued"
RUNNING = "running"

RUN_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
LOG_STREAMS = frozenset({"stdout", "stderr"})
MAX_LOG_READ_BYTES = 1_048_576
STATUS_FILE = "status.json"
ACTIVE_STATUSES = frozenset({QUEUED, RUNNING})


class RunNotFoundError(ValueError):
    """run_id가 잘못되었거나 해당 기록이 없을 때 발생한다."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunManager:
    def __init__(
        self,
        projects: Mapping[str, Project],
        runs_dir: str | Path,
        max_workers: int = 2,
        keep_runs: int = 200,
    ) -> None:
        self._projects = projects
        self._runs_dir = Path(runs_dir)
        self._keep_runs = keep_runs
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="run-manager"
        )
        self._recover_interrupted()
        self._prune_old_runs()

    def _run_path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
            raise RunNotFoundError(f"잘못된 실행 ID입니다: {run_id!r}")
        return self._runs_dir / run_id

    def _load(self, run_id: str) -> dict:
        """호출자가 락을 잡고 있어야 한다."""
        path = self._run_path(run_id) / STATUS_FILE
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RunNotFoundError(f"실행 기록을 찾을 수 없습니다: {run_id}") from exc
        if not isinstance(record, dict):
            raise RunNotFoundError(f"실행 기록이 올바르지 않습니다: {run_id}")
        return record

    def _save(self, record: dict) -> None:
        """호출자가 락을 잡고 있어야 한다. 임시 파일에 쓴 뒤 교체해 부분 기록을 막는다."""
        directory = self._run_path(record["run_id"])
        temp_path = directory / f"{STATUS_FILE}.tmp"
        temp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, directory / STATUS_FILE)

    def _update(self, run_id: str, **changes) -> dict:
        with self._lock:
            record = self._load(run_id)
            record.update(changes)
            self._save(record)
            return record

    def _recover_interrupted(self) -> None:
        for path in self._runs_dir.iterdir():
            if not RUN_ID_PATTERN.fullmatch(path.name):
                continue
            try:
                record = self._load(path.name)
            except RunNotFoundError:
                continue
            if record.get("status") in ACTIVE_STATUSES:
                record.update(
                    status=FAILED,
                    error="서버 재시작으로 중단되었습니다.",
                    finished_at=_now(),
                )
                self._save(record)

    def _prune_old_runs(self) -> None:
        """종료된 기록 중 생성 시각 기준 최신 keep_runs개만 남긴다."""
        finished = []
        for path in self._runs_dir.iterdir():
            if not RUN_ID_PATTERN.fullmatch(path.name):
                continue
            try:
                record = self._load(path.name)
            except RunNotFoundError:
                continue
            if record.get("status") not in ACTIVE_STATUSES and isinstance(
                record.get("created_at"), str
            ):
                finished.append((record["created_at"], path))

        finished.sort(key=lambda item: item[0], reverse=True)
        for _, path in finished[self._keep_runs:]:
            shutil.rmtree(path, ignore_errors=True)

    def start(
        self,
        project_id: str,
        command_id: str,
        timeout: float,
        cwd: str | None = None,
        extra_args: Sequence[str] = (),
    ) -> str:
        """작업을 대기열에 넣고 run_id를 반환한다. 요청 검증은 워커가 맡는다."""
        run_id = uuid4().hex
        extra_args = list(extra_args)
        event = threading.Event()
        record = {
            "run_id": run_id,
            "project_id": project_id,
            "command_id": command_id,
            "status": QUEUED,
            "returncode": None,
            "error": None,
            "truncated": False,
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "extra_args": extra_args,
            "cwd": cwd,
        }
        with self._lock:
            self._run_path(run_id).mkdir()
            self._save(record)
            self._events[run_id] = event

        try:
            self._executor.submit(
                self._work, run_id, event, project_id, command_id, timeout, cwd, extra_args
            )
        except RuntimeError:
            self._finish_failed(run_id, "작업을 제출하지 못했습니다.")
            with self._lock:
                self._events.pop(run_id, None)
            raise
        return run_id

    def _finish_failed(self, run_id: str, error: str) -> None:
        try:
            self._update(run_id, status=FAILED, error=error, finished_at=_now())
        except Exception:
            pass

    def _work(
        self,
        run_id: str,
        event: threading.Event,
        project_id: str,
        command_id: str,
        timeout: float,
        cwd: str | None,
        extra_args: list[str],
    ) -> None:
        try:
            if event.is_set():
                self._update(
                    run_id,
                    status=CANCELLED,
                    error="실행 전에 취소되었습니다.",
                    finished_at=_now(),
                )
                return

            self._update(run_id, status=RUNNING, started_at=_now())
            result = run_project_command(
                self._projects,
                project_id,
                command_id,
                timeout,
                cwd,
                extra_args,
                cancel_event=event,
                log_dir=self._run_path(run_id),
            )
            self._update(
                run_id,
                status=result.status,
                returncode=result.returncode,
                error=result.error,
                truncated=result.truncated,
                finished_at=_now(),
            )
        except Exception as exc:
            self._finish_failed(run_id, f"예상하지 못한 오류: {exc}")
        finally:
            with self._lock:
                self._events.pop(run_id, None)

    def get(self, run_id: str) -> dict:
        with self._lock:
            return self._load(run_id)

    def list(self, limit: int = 20) -> list[dict]:
        if limit < 1:
            raise ValueError("limit은 1 이상이어야 합니다.")
        records = []
        with self._lock:
            for path in self._runs_dir.iterdir():
                if not RUN_ID_PATTERN.fullmatch(path.name):
                    continue
                try:
                    record = self._load(path.name)
                except RunNotFoundError:
                    continue
                if isinstance(record.get("created_at"), str):
                    records.append(record)
        records.sort(key=lambda record: record["created_at"], reverse=True)
        return records[:limit]

    def read_log(
        self, run_id: str, stream: str, offset: int = 0, max_bytes: int = 65536
    ) -> dict:
        """로그를 offset부터 최대 max_bytes 읽는다. eof는 현재 파일 끝에 도달했는지를 뜻한다."""
        if stream not in LOG_STREAMS:
            raise ValueError(f"stream은 stdout 또는 stderr여야 합니다: {stream!r}")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset은 0 이상의 정수여야 합니다.")
        if not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_LOG_READ_BYTES:
            raise ValueError(f"max_bytes는 1 이상 {MAX_LOG_READ_BYTES} 이하여야 합니다.")

        with self._lock:
            self._load(run_id)
            path = self._run_path(run_id) / f"{stream}.log"
            try:
                size = path.stat().st_size
                with open(path, "rb") as file:
                    file.seek(offset)
                    data = file.read(max_bytes)
            except OSError:
                size, data = 0, b""

        next_offset = offset + len(data)
        return {
            "data": data.decode("utf-8", errors="replace"),
            "next_offset": next_offset,
            "eof": next_offset >= size,
        }

    def cancel(self, run_id: str) -> dict:
        with self._lock:
            record = self._load(run_id)
            if record.get("status") in ACTIVE_STATUSES:
                event = self._events.get(run_id)
                if event is not None:
                    event.set()
            return record

    def shutdown(self, cancel_running: bool = True) -> None:
        if cancel_running:
            with self._lock:
                for event in self._events.values():
                    event.set()
        self._executor.shutdown(wait=True)
