import json
from pathlib import Path
import sys
import time

import pytest

from dev_automation import runner
from dev_automation.projects import Project
from dev_automation.runner import CommandSpec
from dev_automation.runs import QUEUED, RUNNING, RunManager, RunNotFoundError

TERMINAL = {
    runner.SUCCEEDED,
    runner.FAILED,
    runner.TIMED_OUT,
    runner.REJECTED,
    runner.START_FAILED,
    runner.CANCELLED,
}


def make_project(root: Path, **codes: str) -> Project:
    commands = {
        name: CommandSpec(sys.executable, ("-c", code)) for name, code in codes.items()
    }
    return Project("demo", root, ".", commands)


def wait_until(predicate, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("시간 안에 조건이 충족되지 않았습니다.")


def wait_finished(manager: RunManager, run_id: str) -> dict:
    return wait_until(
        lambda: (r := manager.get(run_id))["status"] in TERMINAL and r
    )


def wait_status(manager: RunManager, run_id: str, status: str) -> dict:
    return wait_until(lambda: (r := manager.get(run_id))["status"] == status and r)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def runs_dir(tmp_path):
    return tmp_path / "runs"


@pytest.fixture
def make_manager(root, runs_dir):
    managers = []

    def factory(max_workers: int = 2, **codes: str) -> RunManager:
        project = make_project(root, **codes)
        manager = RunManager({"demo": project}, runs_dir, max_workers=max_workers)
        managers.append(manager)
        return manager

    yield factory
    for manager in managers:
        manager.shutdown()


def test_successful_run_is_recorded(make_manager):
    manager = make_manager(ok="print('hello')")

    run_id = manager.start("demo", "ok", 30)
    record = wait_finished(manager, run_id)

    assert record["status"] == runner.SUCCEEDED
    assert record["returncode"] == 0
    assert record["run_id"] == run_id
    assert record["started_at"] and record["finished_at"] and record["created_at"]
    assert manager.read_log(run_id, "stdout")["data"] == "hello" + _newline()


def _newline() -> str:
    # 자식 프로세스의 print는 Windows에서 \r\n을 기록한다.
    return "\r\n" if sys.platform == "win32" else "\n"


def test_failed_run_keeps_returncode(make_manager):
    manager = make_manager(bad="import sys; sys.exit(3)")

    record = wait_finished(manager, manager.start("demo", "bad", 30))

    assert record["status"] == runner.FAILED
    assert record["returncode"] == 3


def test_timed_out_run_is_recorded(make_manager):
    manager = make_manager(slow="import time; time.sleep(60)")

    record = wait_finished(manager, manager.start("demo", "slow", 1))

    assert record["status"] == runner.TIMED_OUT
    assert record["error"]


def test_stdout_and_stderr_logs_are_separate(make_manager):
    manager = make_manager(
        both="import sys; print('to-out'); print('to-err', file=sys.stderr)"
    )
    run_id = manager.start("demo", "both", 30)
    wait_finished(manager, run_id)

    assert manager.read_log(run_id, "stdout")["data"].strip() == "to-out"
    assert manager.read_log(run_id, "stderr")["data"].strip() == "to-err"


def test_read_log_continues_from_offset_and_reports_eof(make_manager):
    manager = make_manager(out="import sys; sys.stdout.write('abcdefghij')")
    run_id = manager.start("demo", "out", 30)
    wait_finished(manager, run_id)

    first = manager.read_log(run_id, "stdout", offset=0, max_bytes=4)
    second = manager.read_log(run_id, "stdout", offset=first["next_offset"], max_bytes=4)
    third = manager.read_log(run_id, "stdout", offset=second["next_offset"], max_bytes=4)

    assert (first["data"], first["eof"]) == ("abcd", False)
    assert (second["data"], second["eof"]) == ("efgh", False)
    assert (third["data"], third["next_offset"], third["eof"]) == ("ij", 10, True)


def test_read_log_with_split_multibyte_does_not_raise(make_manager):
    manager = make_manager(out="import sys; sys.stdout.buffer.write('한글'.encode())")
    run_id = manager.start("demo", "out", 30)
    wait_finished(manager, run_id)

    result = manager.read_log(run_id, "stdout", offset=0, max_bytes=2)

    assert result["next_offset"] == 2
    assert "�" in result["data"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stream": "other"},
        {"stream": "stdout", "offset": -1},
        {"stream": "stdout", "max_bytes": 0},
        {"stream": "stdout", "max_bytes": 1_048_577},
    ],
)
def test_read_log_rejects_invalid_arguments(make_manager, kwargs):
    manager = make_manager(ok="pass")
    run_id = manager.start("demo", "ok", 30)
    wait_finished(manager, run_id)

    with pytest.raises(ValueError):
        manager.read_log(run_id, **kwargs)


def test_unregistered_project_is_recorded_as_rejected(make_manager):
    manager = make_manager(ok="pass")

    record = wait_finished(manager, manager.start("missing", "ok", 30))

    assert record["status"] == runner.REJECTED
    assert record["error"]


def test_unlisted_command_is_recorded_as_rejected(make_manager):
    manager = make_manager(ok="pass")

    record = wait_finished(manager, manager.start("demo", "other", 30))

    assert record["status"] == runner.REJECTED


def test_cancel_running_job(make_manager, root):
    started = root / "started.txt"
    code = (
        "import pathlib, time; "
        f"pathlib.Path({str(started)!r}).write_text('x'); "
        "time.sleep(60)"
    )
    manager = make_manager(slow=code)
    run_id = manager.start("demo", "slow", 60)
    wait_until(started.exists)

    manager.cancel(run_id)
    record = wait_finished(manager, run_id)

    assert record["status"] == runner.CANCELLED


def test_cancel_queued_job_never_runs(make_manager, root):
    started = root / "blocker-started.txt"
    marker = root / "second-ran.txt"
    blocker = (
        "import pathlib, time; "
        f"pathlib.Path({str(started)!r}).write_text('x'); "
        "time.sleep(60)"
    )
    manager = make_manager(
        max_workers=1, blocker=blocker, second=f"open({str(marker)!r}, 'w')"
    )
    first = manager.start("demo", "blocker", 60)
    wait_until(started.exists)
    second = manager.start("demo", "second", 30)
    assert manager.get(second)["status"] == QUEUED

    manager.cancel(second)
    manager.cancel(first)

    assert wait_finished(manager, first)["status"] == runner.CANCELLED
    assert wait_finished(manager, second)["status"] == runner.CANCELLED
    assert not marker.exists()


def test_cancel_finished_job_leaves_record_unchanged(make_manager):
    manager = make_manager(ok="pass")
    run_id = manager.start("demo", "ok", 30)
    before = wait_finished(manager, run_id)

    returned = manager.cancel(run_id)

    assert returned == before
    assert manager.get(run_id) == before


@pytest.mark.parametrize("run_id", ["../x", "", "0" * 32, "G" * 32, "a" * 31])
def test_invalid_or_unknown_run_id_is_rejected(make_manager, run_id):
    manager = make_manager(ok="pass")

    with pytest.raises(RunNotFoundError):
        manager.get(run_id)
    with pytest.raises(RunNotFoundError):
        manager.cancel(run_id)
    with pytest.raises(RunNotFoundError):
        manager.read_log(run_id, "stdout")
    assert issubclass(RunNotFoundError, ValueError)


def test_list_is_newest_first_and_honors_limit(make_manager):
    manager = make_manager(ok="pass")
    ids = []
    for _ in range(3):
        run_id = manager.start("demo", "ok", 30)
        wait_finished(manager, run_id)
        ids.append(run_id)

    assert [r["run_id"] for r in manager.list()] == ids[::-1]
    assert [r["run_id"] for r in manager.list(limit=2)] == ids[:0:-1]


def test_running_records_are_failed_on_restart(make_manager, root, runs_dir):
    started = root / "started.txt"
    code = (
        "import pathlib, time; "
        f"pathlib.Path({str(started)!r}).write_text('x'); "
        "time.sleep(60)"
    )
    first = make_manager(slow=code)
    running_id = first.start("demo", "slow", 60)
    wait_until(started.exists)
    wait_status(first, running_id, RUNNING)

    # 새 관리자가 뜰 때 디스크에 남은 running/queued 기록을 정리하는지 본다.
    queued_id = "b" * 32
    (runs_dir / queued_id).mkdir()
    (runs_dir / queued_id / "status.json").write_text(
        json.dumps({"run_id": queued_id, "status": QUEUED, "created_at": "2000-01-01"})
    )
    second = make_manager(slow=code)

    for run_id in (running_id, queued_id):
        record = second.get(run_id)
        assert record["status"] == runner.FAILED
        assert record["error"] == "서버 재시작으로 중단되었습니다."


def test_broken_status_file_does_not_block_list(make_manager, runs_dir):
    manager = make_manager(ok="pass")
    run_id = manager.start("demo", "ok", 30)
    wait_finished(manager, run_id)
    broken = runs_dir / ("c" * 32)
    broken.mkdir()
    (broken / "status.json").write_text("{")

    assert [r["run_id"] for r in manager.list()] == [run_id]
    with pytest.raises(RunNotFoundError):
        manager.get("c" * 32)


def test_status_file_has_no_output_content(make_manager, runs_dir):
    manager = make_manager(out="print('SECRET-OUTPUT'); import sys; print('SECRET-ERR', file=sys.stderr)")
    run_id = manager.start("demo", "out", 30)
    wait_finished(manager, run_id)

    text = (runs_dir / run_id / "status.json").read_text(encoding="utf-8")
    record = json.loads(text)

    assert "SECRET" not in text
    assert set(record) == {
        "run_id", "project_id", "command_id", "status", "returncode", "error",
        "truncated", "created_at", "started_at", "finished_at", "extra_args", "cwd",
    }
