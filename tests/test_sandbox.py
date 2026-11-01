from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from dev_automation import agent, sandbox as sandbox_module
from dev_automation.agent import TEST_ARGS, run_task
from dev_automation.projects import Project
from dev_automation.runner import FAILED, START_FAILED, SUCCEEDED, TIMED_OUT, CommandSpec, RunResult
from dev_automation.sandbox import BUILD_COMMAND, DockerSandbox
from test_agent import FakeLLM, call, done, git

IMAGE = "desktop-mcp-sandbox:py312"
REQUIRED_FLAGS = [
    ["--rm"],
    ["--network", "none"],
    ["--cap-drop", "ALL"],
    ["--security-opt", "no-new-privileges"],
    ["--pids-limit", "256"],
    ["--memory", "2g"],
    ["--memory-swap", "2g"],
    ["--cpus", "4"],
    ["--read-only"],
    ["--tmpfs", "/tmp"],
    ["--user", "1000:1000"],
    ["-e", "HOME=/tmp"],
    ["-e", "PYTHONDONTWRITEBYTECODE=1"],
]


def has_sequence(args: list[str], sequence: list[str]) -> bool:
    size = len(sequence)
    return any(args[i : i + size] == sequence for i in range(len(args) - size + 1))


def docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        done_ = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done_.returncode == 0


# ---- 단위 테스트 ----


@pytest.mark.parametrize(
    "image",
    ["-v", "--privileged", "", "UPPER:tag", "img name", "img:", "img:a b", "img;ls", "/img", "img\n"],
)
def test_invalid_image_is_rejected(image):
    with pytest.raises(ValueError):
        DockerSandbox(image)


@pytest.mark.parametrize("image", ["desktop-mcp-sandbox:py312", "python", "reg.io/team/img:1.0_a"])
def test_valid_image_is_accepted(image):
    assert DockerSandbox(image).image == image


def test_run_args_have_all_isolation_flags_and_no_forbidden_ones(tmp_path):
    args = DockerSandbox(IMAGE).run_args(
        "desktop-mcp-sandbox-x", tmp_path, "/work/sub", ["-m", "pytest"], ["-q", "--tb=short"]
    )

    for flags in REQUIRED_FLAGS:
        assert has_sequence(args, flags), flags
    assert has_sequence(args, ["--name", "desktop-mcp-sandbox-x"])
    assert has_sequence(args, ["--mount", f"type=bind,source={tmp_path},target=/work"])
    assert has_sequence(args, ["-w", "/work/sub"])
    assert args[0] == "run"
    assert args[-6:] == [IMAGE, "python", "-m", "pytest", "-q", "--tb=short"]
    forbidden = {"-v", "--volume", "--privileged", "--pid", "--env-file", "--cap-add"}
    for index, arg in enumerate(args):
        if arg == "-e":
            # 이름만 쓰면 호스트 환경 변수가 전달되므로 항상 값을 함께 준다.
            assert "=" in args[index + 1]
        assert arg not in forbidden
        assert not arg.startswith(("--device", "--network=host", "--net=host"))
        assert "docker.sock" not in arg


@pytest.mark.parametrize("bad", ["..", "a/../..", "C:/x", "/etc", "-v", "\\\\srv\\x"])
def test_bad_relative_cwd_is_rejected(tmp_path, bad):
    result = DockerSandbox(IMAGE).run_tests(tmp_path, bad, ["-m", "pytest"], [], 10)

    assert result.status == "rejected"


@pytest.mark.parametrize(
    "relative, expected",
    [(".", "/work"), ("pkg", "/work/pkg"), ("pkg\\sub", "/work/pkg/sub")],
)
def test_container_dir(relative, expected):
    assert sandbox_module._container_dir(relative) == expected


def test_check_reports_docker_not_running_and_missing_image(monkeypatch):
    def fake(result):
        monkeypatch.setattr(sandbox_module, "run_command", lambda *a, **k: result)

    fake(RunResult("docker", START_FAILED, error="not found"))
    assert "Docker Desktop" in DockerSandbox(IMAGE).check()

    fake(RunResult("docker", FAILED, 1, stderr="Error response from daemon: No such image: x"))
    assert BUILD_COMMAND in DockerSandbox(IMAGE).check()

    fake(RunResult("docker", FAILED, 1, stderr="error during connect: pipe not found"))
    assert "Docker Desktop" in DockerSandbox(IMAGE).check()

    fake(RunResult("docker", SUCCEEDED, 0))
    assert DockerSandbox(IMAGE).check() is None


@pytest.fixture
def host_marker_project(tmp_path):
    """PC에서 실행되면 표식 파일을 만드는 프로젝트."""
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "ran_on_host.txt"
    (repo / "test_mod.py").write_text(
        "from pathlib import Path\n\n\ndef test_marker():\n"
        f"    Path({str(marker)!r}).write_text('x')\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "tester")
    git(repo, "config", "user.email", "tester@example.com")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    spec = CommandSpec(sys.executable, ("-m", "pytest"), frozenset({"-q", "--tb=short"}))
    return Project("demo", repo, ".", {"pytest": spec}), marker


class FakeSandbox:
    image = "fake:1"

    def __init__(self, problem=None):
        self.problem = problem
        self.calls = []

    def check(self):
        return self.problem

    def run_tests(self, source, relative_cwd, args, pytest_args, timeout):
        self.calls.append((Path(source), relative_cwd, tuple(args), tuple(pytest_args), timeout))
        return RunResult("pytest", SUCCEEDED, 0, stdout="1 passed")


def test_failed_check_ends_without_worktree_or_host_execution(host_marker_project, tmp_path):
    project, marker = host_marker_project
    fake = FakeSandbox("Docker Desktop을 실행하세요.")
    runs_dir = tmp_path / "runs"

    result = run_task(project, "작업", object(), runs_dir, sandbox=fake, on_progress=lambda m: None)

    assert result.status == agent.FAILED
    assert result.message == "Docker Desktop을 실행하세요."
    assert not marker.exists()
    assert fake.calls == []
    assert list(runs_dir.glob("*/worktree")) == []
    assert git(project.root, "branch", "--list", "agent/*") == ""
    assert len(git(project.root, "worktree", "list").strip().splitlines()) == 1


def test_all_test_runs_go_to_sandbox_not_host(host_marker_project, tmp_path):
    project, marker = host_marker_project
    fake = FakeSandbox()
    llm = FakeLLM(call("write_file", path="new.py", content="X = 1\n"), call("run_tests"), done())

    result = run_task(
        project, "작업", llm, tmp_path / "runs", sandbox=fake, on_progress=lambda m: None
    )

    assert result.status == agent.SUCCEEDED
    assert not marker.exists()
    # 기준 테스트, 모델 도구, 최종 검증이 모두 컨테이너로 간다.
    assert len(fake.calls) == 3
    for source, cwd, args, pytest_args, timeout in fake.calls:
        assert source.name == "worktree"
        assert cwd == "."
        assert args == ("-m", "pytest")
        assert pytest_args == TEST_ARGS
        assert timeout == 600


def test_locked_tests_with_subdirectory_go_to_sandbox(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "test_base.py").write_text("def test_b():\n    pass\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    (repo / "pkg" / "test_sub.py").write_text("def test_s():\n    pass\n", encoding="utf-8")
    spec = CommandSpec(sys.executable, ("-m", "pytest"), frozenset({"-q", "--tb=short"}))
    project = Project("demo", repo, "pkg", {"pytest": spec})
    fake = FakeSandbox()
    llm = FakeLLM(call("write_file", path="pkg/x.py", content="X = 1\n"), call("run_tests"), done())

    run_task(
        project, "작업", llm, tmp_path / "runs",
        protected_tests=["pkg/test_sub.py"], sandbox=fake, on_progress=lambda m: None,
    )

    assert fake.calls[0][1] == "pkg" and fake.calls[0][3] == TEST_ARGS
    assert fake.calls[1][1] == "pkg" and fake.calls[1][3] == TEST_ARGS + ("test_sub.py",)


def _cli(monkeypatch, project):
    from dev_automation import __main__ as cli

    monkeypatch.setattr(cli, "load_projects", lambda path: {"demo": project})
    monkeypatch.setattr(cli, "OllamaClient", lambda model: object())
    captured = {"called": False}

    def fake_run_task(project, task, llm, runs_dir, **kwargs):
        captured["called"] = True
        captured.update(kwargs)
        return agent.AgentResult(agent.FAILED, "agent/x", "실패")

    monkeypatch.setattr(cli, "run_task", fake_run_task)
    return cli, captured


def test_cli_refuses_host_tests_without_sandbox_or_flag(monkeypatch, capsys):
    cli, captured = _cli(monkeypatch, SimpleNamespace(sandbox=None))

    code = cli.main(["agent", "demo", "작업"])

    out = capsys.readouterr().out
    assert code == 1
    assert not captured["called"]
    assert "--unsafe-host-tests" in out and "sandbox" in out


def test_cli_unsafe_flag_runs_on_host_with_warning(monkeypatch, capsys):
    cli, captured = _cli(monkeypatch, SimpleNamespace(sandbox=None))

    cli.main(["agent", "demo", "작업", "--unsafe-host-tests"])

    assert captured["called"] and captured["sandbox"] is None
    assert "테스트 격리 없음" in capsys.readouterr().out


def test_cli_uses_docker_when_project_has_sandbox(monkeypatch, capsys):
    cli, captured = _cli(monkeypatch, SimpleNamespace(sandbox=IMAGE))

    cli.main(["agent", "demo", "작업"])

    assert isinstance(captured["sandbox"], DockerSandbox)
    assert captured["sandbox"].image == IMAGE
    assert f"테스트 격리: Docker({IMAGE})" in capsys.readouterr().out


# ---- 통합 테스트(실제 컨테이너) ----

docker_only = pytest.mark.skipif(
    not docker_ready(), reason="Docker 또는 테스트 이미지(desktop-mcp-sandbox:py312)가 없습니다"
)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "src"
    path.mkdir()
    (path / "a.txt").write_text("original", encoding="utf-8")
    (path / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    return path


def run_py(source, code, timeout=60, sandbox=None, cwd="."):
    box = sandbox or DockerSandbox(IMAGE)
    return box.run_tests(source, cwd, ["-c", code], [], timeout)


def leftover_containers() -> str:
    return subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=desktop-mcp-sandbox-", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@docker_only
def test_check_passes_with_built_image():
    assert DockerSandbox(IMAGE).check() is None


@docker_only
def test_missing_image_is_reported():
    assert BUILD_COMMAND in DockerSandbox("desktop-mcp-sandbox:no-such-tag").check()


@docker_only
def test_pytest_exit_codes(source):
    box = DockerSandbox(IMAGE)

    passed = box.run_tests(source, ".", ["-m", "pytest"], ["-q", "--tb=short"], 120)
    (source / "test_bad.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    failed = box.run_tests(source, ".", ["-m", "pytest"], ["-q", "--tb=short"], 120)

    assert passed.ok and "1 passed" in passed.stdout
    assert failed.status == FAILED and failed.returncode == 1 and failed.error is None
    assert "1 failed" in failed.stdout


@docker_only
def test_subdirectory_cwd(source):
    (source / "pkg").mkdir()
    (source / "pkg" / "m.txt").write_text("x", encoding="utf-8")

    result = run_py(source, "import os; print(os.getcwd(), os.listdir('.'))", cwd="pkg")

    assert result.ok and "/work/pkg" in result.stdout and "m.txt" in result.stdout


@docker_only
def test_only_the_copy_changes(source):
    code = (
        "open('a.txt', 'w').write('changed')\n"
        "open('new.txt', 'w').write('new')\n"
        "import os; os.remove('test_ok.py')\n"
    )

    result = run_py(source, code)

    assert result.ok, result.stderr
    assert (source / "a.txt").read_text(encoding="utf-8") == "original"
    assert (source / "test_ok.py").exists()
    assert not (source / "new.txt").exists()


@docker_only
def test_git_and_excluded_dirs_are_not_copied(source):
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    for name in (".venv", ".venv-new", "runs", "__pycache__", ".pytest_cache"):
        (source / name).mkdir()
        (source / name / "f").write_text("x", encoding="utf-8")
    (source / "keep").mkdir()

    result = run_py(source, "import os; print(sorted(os.listdir('/work')))")

    assert result.ok, result.stderr
    assert "keep" in result.stdout and "a.txt" in result.stdout
    for name in (".git", ".venv", ".venv-new", "runs", "__pycache__", ".pytest_cache"):
        assert f"'{name}'" not in result.stdout


@docker_only
def test_git_pointer_file_is_not_copied(source):
    (source / ".git").write_text("gitdir: C:/somewhere/.git/worktrees/x", encoding="utf-8")

    result = run_py(source, "import os; print(os.path.lexists('/work/.git'))")

    assert result.stdout.strip() == "False"


@docker_only
def test_network_is_blocked(source):
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError:\n"
        "    print('BLOCKED')\n"
    )

    result = run_py(source, code)

    assert result.stdout.strip() == "BLOCKED"


@docker_only
def test_host_files_are_not_visible(source):
    home_name = Path.home().name
    code = (
        "import os\n"
        f"names = ('/c', '/mnt', '/host', '/Users', '/home/{home_name}')\n"
        # 이미지에 기본으로 있는 빈 /mnt는 호스트 파일이 아니므로 내용이 있을 때만 센다.
        "print([p for p in names if os.path.exists(p) and (not os.path.isdir(p) or os.listdir(p))])\n"
        "print(sorted(os.listdir('/')))\n"
        "print(os.path.exists('C:\\\\'), os.path.expanduser('~'))\n"
    )

    result = run_py(source, code)

    lines = result.stdout.splitlines()
    assert result.ok, result.stderr
    assert lines[0] == "[]"
    assert "'host'" not in lines[1] and "'Users'" not in lines[1] and "'c'" not in lines[1]
    assert lines[2] == "False /tmp"


@docker_only
def test_host_environment_is_not_visible(source, monkeypatch):
    monkeypatch.setenv("SANDBOX_TEST_SECRET", "s3cret-value-123")

    result = run_py(source, "import os; print(dict(os.environ))")

    assert result.ok, result.stderr
    assert "SANDBOX_TEST_SECRET" not in result.stdout
    assert "s3cret-value-123" not in result.stdout + result.stderr


@docker_only
def test_runs_as_non_root(source):
    result = run_py(source, "import os; print(os.getuid(), os.getgid())")

    assert result.stdout.split() == ["1000", "1000"]


@docker_only
def test_root_filesystem_is_read_only_but_tmp_and_work_are_writable(source):
    code = (
        "for path in ('/etc/x', '/usr/x', '/root/x', '/work/ok.txt', '/tmp/ok.txt'):\n"
        "    try:\n"
        "        open(path, 'w').write('x'); print(path, 'WRITE_OK')\n"
        "    except OSError:\n"
        "        print(path, 'DENIED')\n"
    )

    result = run_py(source, code)

    lines = dict(line.split() for line in result.stdout.splitlines())
    assert lines["/etc/x"] == lines["/usr/x"] == lines["/root/x"] == "DENIED"
    assert lines["/work/ok.txt"] == lines["/tmp/ok.txt"] == "WRITE_OK"


@docker_only
def test_capabilities_are_dropped(source):
    result = run_py(source, "print(open('/proc/self/status').read())")

    caps = {
        line.split(":")[0]: line.split(":")[1].strip()
        for line in result.stdout.splitlines()
        if line.startswith("Cap")
    }
    assert caps["CapEff"] == "0000000000000000" and caps["CapBnd"] == "0000000000000000"
    assert "NoNewPrivs:\t1" in result.stdout


@docker_only
def test_timeout_removes_container(source):
    assert leftover_containers() == ""

    result = run_py(source, "import time; time.sleep(120)", timeout=5)

    assert result.status == TIMED_OUT
    assert leftover_containers() == ""


@docker_only
def test_memory_limit_applies(source):
    box = DockerSandbox(IMAGE, memory="128m")
    code = (
        "import os\n"
        "p = '/sys/fs/cgroup/memory.max'\n"
        "print(open(p).read().strip() if os.path.exists(p) else 'NOFILE', flush=True)\n"
        "chunks = []\n"
        "for _ in range(40):\n"
        "    chunk = bytearray(10 * 1024 * 1024)\n"
        "    for i in range(0, len(chunk), 4096):\n"
        "        chunk[i] = 1\n"
        "    chunks.append(chunk)\n"
        "print('ALLOCATED')\n"
    )

    result = run_py(source, code, sandbox=box)

    assert result.status == FAILED
    assert "ALLOCATED" not in result.stdout
    assert result.stdout.splitlines()[0] in ("NOFILE", str(128 * 1024 * 1024))
    assert leftover_containers() == ""


@docker_only
def test_docker_error_is_not_disguised_as_test_failure(source):
    result = run_py(source, "print(1)", sandbox=DockerSandbox(IMAGE, memory="not-a-size"))

    assert result.status == FAILED and result.returncode == 125
    assert "Docker 실행 실패" in result.error
    assert leftover_containers() == ""


@docker_only
def test_no_leftover_containers_after_runs(source):
    run_py(source, "print(1)")
    run_py(source, "import sys; sys.exit(3)")

    assert leftover_containers() == ""
