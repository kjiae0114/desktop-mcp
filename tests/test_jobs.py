"""백그라운드 작업(dev_automation.jobs)과 CLI 연결 테스트."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from dev_automation import __main__ as cli
from dev_automation import agent, jobs


def _make_job(jobs_dir: Path, **fields) -> Path:
    job_id = fields.pop("job_id", "job-20261107-120000-abcd")
    path = jobs_dir / job_id
    path.mkdir(parents=True)
    record = {
        "job_id": job_id, "task": "작업", "argv": [], "status": jobs.RUNNING, "pid": os.getpid(),
        "created_at": "2026-11-07T12:00:00", "finished_at": None, "exit_code": None,
        "branch": None, "message": None, **fields,
    }
    (path / jobs.JOB_FILE).write_text(json.dumps(record), encoding="utf-8")
    (path / jobs.LOG_FILE).write_text("첫 줄\n", encoding="utf-8")
    return path


def test_pid_alive():
    assert jobs.pid_alive(os.getpid())
    assert not jobs.pid_alive(0)
    assert not jobs.pid_alive(-5)


@pytest.mark.parametrize("bad", ["../x", "job-1", "", "job-20261107-120000-ABCD", None])
def test_job_dir_rejects_bad_ids(tmp_path, bad):
    with pytest.raises(jobs.JobNotFoundError):
        jobs.job_dir(tmp_path, bad)


def test_refresh_marks_dead_process_interrupted(tmp_path):
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
    )
    path = _make_job(tmp_path, pid=int(done.stdout))

    record = jobs.refresh(path)

    assert record["status"] == jobs.INTERRUPTED
    assert jobs._read(path)["status"] == jobs.INTERRUPTED


def test_refresh_keeps_live_process_running(tmp_path):
    path = _make_job(tmp_path)
    assert jobs.refresh(path)["status"] == jobs.RUNNING


def test_cancel_checker_raises_only_once(tmp_path):
    check = jobs.cancel_checker(tmp_path)
    check()  # 취소 파일이 없으면 아무 일도 없다
    (tmp_path / jobs.CANCEL_FILE).write_text("x")
    with pytest.raises(jobs.JobCancelled):
        check()
    check()  # 정리 단계에서는 다시 던지지 않는다


@pytest.mark.parametrize("code, cancel, expected", [
    (0, False, jobs.SUCCEEDED),
    (1, False, jobs.FAILED),
    (1, True, jobs.CANCELLED),
    (0, True, jobs.SUCCEEDED),  # 취소 요청이 늦어 이미 성공했으면 성공으로 남긴다
])
def test_finish_status(tmp_path, code, cancel, expected):
    path = _make_job(tmp_path)
    if cancel:
        (path / jobs.CANCEL_FILE).write_text("x")

    jobs.finish(path, code, "agent/x" if code == 0 else None, "메시지")

    record = jobs._read(path)
    assert record["status"] == expected
    assert record["exit_code"] == code and record["finished_at"]


def test_request_cancel_only_for_running(tmp_path):
    running = _make_job(tmp_path)
    done = _make_job(tmp_path, job_id="job-20261107-120001-abcd", status=jobs.SUCCEEDED)

    jobs.request_cancel(tmp_path, running.name)
    jobs.request_cancel(tmp_path, done.name)

    assert (running / jobs.CANCEL_FILE).exists()
    assert not (done / jobs.CANCEL_FILE).exists()


def test_list_jobs_newest_first_and_limit(tmp_path):
    for second in range(3):
        _make_job(tmp_path, job_id=f"job-20261107-12000{second}-abcd", status=jobs.FAILED)
    (tmp_path / "not-a-job").mkdir()

    records = jobs.list_jobs(tmp_path, limit=2)

    assert [r["job_id"] for r in records] == [
        "job-20261107-120002-abcd", "job-20261107-120001-abcd",
    ]
    assert jobs.list_jobs(tmp_path / "없음") == []


def test_start_runs_detached_child_and_records_result(tmp_path):
    """실제 자식 프로세스를 띄운다. 등록되지 않은 프로젝트라 바로 실패로 끝나야 한다."""
    config = tmp_path / "projects.json"
    config.write_text('{"version": 1, "projects": {}}', encoding="utf-8")
    argv = ["agent", "없는프로젝트", "작업", "--config", str(config)]

    record = jobs.start(tmp_path / "jobs", argv, "작업", Path(cli.REPO_ROOT))

    path = tmp_path / "jobs" / record["job_id"]
    assert record["pid"]
    deadline = time.time() + 60
    while jobs.refresh(path)["status"] == jobs.RUNNING and time.time() < deadline:
        time.sleep(0.5)
    final = jobs.refresh(path)
    log = (path / jobs.LOG_FILE).read_text(encoding="utf-8")
    assert final["status"] == jobs.FAILED, log
    assert final["exit_code"] == 1
    assert "등록되지 않은 프로젝트" in log


# --- CLI ---------------------------------------------------------------------

def _patch_cli(monkeypatch, tmp_path, run_task):
    monkeypatch.setattr(cli, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(
        cli, "load_projects", lambda path: {"demo": SimpleNamespace(sandbox=None)}
    )
    unloaded = []
    monkeypatch.setattr(
        cli, "OllamaClient", lambda model: SimpleNamespace(unload=lambda: unloaded.append(1))
    )
    monkeypatch.setattr(cli, "run_task", run_task)
    return unloaded


def test_cli_background_starts_job_without_running_agent(monkeypatch, tmp_path, capsys):
    started = {}

    def fake_start(jobs_dir, argv, task, cwd):
        started.update(argv=argv, task=task)
        return {"job_id": "job-20261107-120000-abcd", "pid": 123}

    _patch_cli(monkeypatch, tmp_path, lambda *a, **k: pytest.fail("포그라운드로 실행됨"))
    monkeypatch.setattr(cli.jobs, "start", fake_start)

    code = cli.main(["agent", "demo", "작업", "--unsafe-host-tests", "--background"])

    assert code == 0
    assert started["argv"] == ["agent", "demo", "작업", "--unsafe-host-tests"]
    assert "job-20261107-120000-abcd" in capsys.readouterr().out


def test_cli_job_child_records_success(monkeypatch, tmp_path):
    path = _make_job(tmp_path / "jobs")
    unloaded = _patch_cli(monkeypatch, tmp_path, lambda *a, **k: agent.AgentResult(
        agent.SUCCEEDED, "agent/x", "완료", "abc", ["a.py"]))

    code = cli.main(["agent", "demo", "작업", "--unsafe-host-tests", "--job-dir", str(path)])

    record = jobs._read(path)
    assert code == 0 and unloaded == [1]
    assert record["status"] == jobs.SUCCEEDED and record["branch"] == "agent/x"


def test_cli_job_child_cancel_unloads_and_records(monkeypatch, tmp_path):
    path = _make_job(tmp_path / "jobs")

    def fake_run_task(project, task, llm, runs_dir, on_progress, **kwargs):
        on_progress("도구: read_file a.py")
        (path / jobs.CANCEL_FILE).write_text("x")
        on_progress("도구: write_file a.py")  # 여기서 취소
        pytest.fail("취소 뒤에도 계속 실행됨")

    unloaded = _patch_cli(monkeypatch, tmp_path, fake_run_task)

    code = cli.main(["agent", "demo", "작업", "--unsafe-host-tests", "--job-dir", str(path)])

    record = jobs._read(path)
    assert code == 1 and unloaded == [1]
    assert record["status"] == jobs.CANCELLED
    assert record["message"] == "사용자가 취소했습니다."


def test_cli_jobs_log_cancel(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "JOBS_DIR", tmp_path)
    _make_job(tmp_path, status=jobs.SUCCEEDED, branch="agent/x",
              finished_at="2026-11-07T12:01:30")

    assert cli.main(["jobs"]) == 0
    out = capsys.readouterr().out
    assert "job-20261107-120000-abcd" in out and "succeeded" in out and "90초" in out

    assert cli.main(["log", "job-20261107-120000-abcd"]) == 0
    assert "첫 줄" in capsys.readouterr().out

    assert cli.main(["cancel", "job-20261107-120000-abcd"]) == 0
    assert "이미 끝난 작업" in capsys.readouterr().out

    assert cli.main(["log", "../etc"]) == 1
