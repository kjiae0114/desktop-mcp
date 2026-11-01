import asyncio
from pathlib import Path
import sys
import time

import pytest

from dev_automation import runner
from dev_automation.runner import CommandSpec, run_command


def python_command(code: str, **kwargs) -> CommandSpec:
    return CommandSpec(sys.executable, ("-c", code), **kwargs)


def run_code(code: str, cwd: Path, timeout: float = 30, **kwargs):
    return run_command("test", {"test": python_command(code)}, cwd, timeout, **kwargs)


def test_successful_command(tmp_path):
    result = run_code("print('hello')", tmp_path)

    assert result.ok
    assert result.status == runner.SUCCEEDED
    assert result.returncode == 0
    assert result.stdout == "hello\n"
    assert result.error is None


def test_collects_stdout_and_stderr_separately(tmp_path):
    result = run_code(
        "import sys; print('to-out'); print('to-err', file=sys.stderr)",
        tmp_path,
    )

    assert result.stdout == "to-out\n"
    assert result.stderr == "to-err\n"


def test_failing_command_returns_exit_code(tmp_path):
    result = run_code("import sys; sys.exit(7)", tmp_path)

    assert not result.ok
    assert result.status == runner.FAILED
    assert result.returncode == 7


def test_runs_in_given_working_directory(tmp_path):
    result = run_code("import os; print(os.getcwd())", tmp_path)

    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_shell_syntax_is_not_interpreted(tmp_path):
    marker = tmp_path / "injected.txt"
    spec = CommandSpec(
        sys.executable,
        ("-c", "import sys; print(sys.argv[1])", f"x & echo hi > {marker}"),
    )

    result = run_command("test", {"test": spec}, tmp_path, 30)

    assert result.ok
    assert "&" in result.stdout
    assert not marker.exists()


def test_invalid_utf8_output_does_not_crash(tmp_path):
    result = run_code(r"import sys; sys.stdout.buffer.write(b'\xff')", tmp_path)

    assert result.ok
    assert result.stdout == "�"


def test_long_output_is_truncated(tmp_path):
    result = run_code(f"print('x' * {runner.MAX_OUTPUT_CHARS + 10})", tmp_path)

    assert result.truncated
    assert len(result.stdout) == runner.MAX_OUTPUT_CHARS


def test_missing_executable_is_reported(tmp_path):
    spec = CommandSpec(str(tmp_path / "does-not-exist.exe"))

    result = run_command("test", {"test": spec}, tmp_path, 30)

    assert result.status == runner.START_FAILED
    assert result.returncode is None
    assert result.error


def test_timeout_kills_process(tmp_path):
    started = time.monotonic()

    result = run_code("import time; time.sleep(60)", tmp_path, timeout=1)

    assert result.status == runner.TIMED_OUT
    assert result.returncode is not None
    assert time.monotonic() - started < 30


def test_timeout_keeps_output_written_before_kill(tmp_path):
    result = run_code(
        "import time; print('partial', flush=True); time.sleep(60)",
        tmp_path,
        timeout=2,
    )

    assert result.status == runner.TIMED_OUT
    assert "partial" in result.stdout


def test_timeout_kills_child_processes(tmp_path):
    started_file = tmp_path / "child-started.txt"
    survived_file = tmp_path / "child-survived.txt"
    child_code = (
        "import pathlib, time; "
        f"pathlib.Path({str(started_file)!r}).write_text('x'); "
        "time.sleep(4); "
        f"pathlib.Path({str(survived_file)!r}).write_text('x')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(60)"
    )

    result = run_code(parent_code, tmp_path, timeout=2)
    time.sleep(4)

    assert result.status == runner.TIMED_OUT
    assert started_file.exists()
    assert not survived_file.exists()


def test_unregistered_command_is_rejected_before_execution(tmp_path):
    marker = tmp_path / "ran.txt"
    commands = {"test": python_command(f"open({str(marker)!r}, 'w')")}

    result = run_command("other", commands, tmp_path, 30)

    assert result.status == runner.REJECTED
    assert result.returncode is None
    assert not marker.exists()


def test_unlisted_extra_arg_is_rejected(tmp_path):
    commands = {
        "test": python_command(
            "import sys; print(sys.argv[1])",
            allowed_extra_args=frozenset({"--allowed"}),
        )
    }

    allowed = run_command("test", commands, tmp_path, 30, extra_args=["--allowed"])
    blocked = run_command("test", commands, tmp_path, 30, extra_args=["--blocked"])

    assert allowed.stdout == "--allowed\n"
    assert blocked.status == runner.REJECTED


@pytest.mark.parametrize("program", ["build.bat", "BUILD.CMD"])
def test_batch_files_are_rejected(tmp_path, program):
    result = run_command("test", {"test": CommandSpec(program)}, tmp_path, 30)

    assert result.status == runner.REJECTED


def test_missing_working_directory_is_rejected(tmp_path):
    result = run_code("print('x')", tmp_path / "missing")

    assert result.status == runner.REJECTED


@pytest.mark.parametrize("timeout", [0, -1, runner.MAX_TIMEOUT_SECONDS + 1])
def test_invalid_timeout_is_rejected(tmp_path, timeout):
    result = run_code("print('x')", tmp_path, timeout=timeout)

    assert result.status == runner.REJECTED


def test_existing_desktop_mcp_is_unaffected():
    import desktop_mcp

    assert desktop_mcp.safe_path(".") == desktop_mcp.WORKSPACE
    with pytest.raises(ValueError):
        desktop_mcp.safe_path("..")

    tool_names = {tool.name for tool in asyncio.run(desktop_mcp.mcp.list_tools())}
    assert tool_names == {
        "get_screen_size",
        "screenshot",
        "launch_program",
        "list_files",
        "read_text_file",
        "write_text_file",
        "click",
        "get_cursor_position",
        "press_key",
        "type_text",
        "click_and_paste",
        "terminal",
    }
