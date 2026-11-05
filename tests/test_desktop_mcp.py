"""desktop_mcp 도구 테스트. 실제 마우스·키보드 대신 가짜 GUI 객체를 쓴다."""

import asyncio

import pytest

import desktop_mcp


class FakeGui:
    def __init__(self, width=1920, height=1080):
        self._size = (width, height)
        self.calls = []

    def size(self):
        return self._size

    def position(self):
        return (10, 20)

    def click(self, x, y):
        self.calls.append(("click", x, y))

    def press(self, key):
        self.calls.append(("press", key))

    def write(self, text, interval=0):
        self.calls.append(("write", text))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", *keys))


@pytest.fixture
def gui(monkeypatch):
    fake = FakeGui()
    monkeypatch.setattr(desktop_mcp, "_gui", lambda: fake)
    return fake


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_mcp, "WORKSPACE", tmp_path)
    return tmp_path


def test_registered_tools():
    names = {tool.name for tool in asyncio.run(desktop_mcp.mcp.list_tools())}
    assert names == {
        "get_screen_size", "screenshot", "launch_program", "list_files",
        "read_text_file", "write_text_file", "click", "get_cursor_position",
        "press_key", "type_text", "click_and_paste", "terminal",
    }


# --- 경로 정책 -------------------------------------------------------------

@pytest.mark.parametrize("bad", ["..", "../secret.txt", "a/../../x", "C:/Windows/win.ini"])
def test_safe_path_blocks_escape(workspace, bad):
    with pytest.raises(ValueError):
        desktop_mcp.safe_path(bad)


def test_safe_path_allows_inside(workspace):
    assert desktop_mcp.safe_path("sub/a.txt") == (workspace / "sub" / "a.txt").resolve()


# --- 파일 도구 -------------------------------------------------------------

def test_write_then_read_round_trip(workspace):
    msg = desktop_mcp.write_text_file("notes/hello.txt", "안녕하세요")
    assert msg == "저장 완료: notes/hello.txt"
    assert desktop_mcp.read_text_file("notes/hello.txt") == "안녕하세요"


def test_write_rejects_too_long(workspace):
    with pytest.raises(ValueError):
        desktop_mcp.write_text_file("big.txt", "x" * (desktop_mcp.MAX_WRITE_CHARS + 1))


def test_write_rejects_directory_target(workspace):
    (workspace / "folder").mkdir()
    with pytest.raises(ValueError):
        desktop_mcp.write_text_file("folder", "x")


def test_read_rejects_missing_and_large(workspace):
    with pytest.raises(ValueError):
        desktop_mcp.read_text_file("nope.txt")
    (workspace / "big.txt").write_bytes(b"x" * (desktop_mcp.MAX_READ_BYTES + 1))
    with pytest.raises(ValueError):
        desktop_mcp.read_text_file("big.txt")


def test_list_files(workspace):
    (workspace / "b.txt").write_text("12345", encoding="utf-8")
    (workspace / "a_dir").mkdir()
    assert desktop_mcp.list_files() == [
        {"name": "a_dir", "is_dir": True, "size": None},
        {"name": "b.txt", "is_dir": False, "size": 5},
    ]


def test_list_files_rejects_non_dir(workspace):
    with pytest.raises(ValueError):
        desktop_mcp.list_files("missing")


# --- 화면 도구 -------------------------------------------------------------

def test_screen_size_and_cursor(gui):
    assert desktop_mcp.get_screen_size() == {"width": 1920, "height": 1080}
    assert desktop_mcp.get_cursor_position() == {"x": 10, "y": 20}


def test_click_inside_screen(gui):
    desktop_mcp.click(100, 200)
    assert gui.calls == [("click", 100, 200)]


@pytest.mark.parametrize("x,y", [(-1, 0), (0, -1), (1920, 0), (0, 1080)])
def test_click_outside_screen_is_rejected(gui, x, y):
    with pytest.raises(ValueError):
        desktop_mcp.click(x, y)
    assert gui.calls == []


def test_press_key_allowlist(gui):
    desktop_mcp.press_key("Enter")
    assert gui.calls == [("press", "enter")]
    with pytest.raises(ValueError):
        desktop_mcp.press_key("win")


def test_type_text_ascii_only(gui):
    desktop_mcp.type_text("hello")
    assert gui.calls == [("write", "hello")]
    with pytest.raises(ValueError):
        desktop_mcp.type_text("한글")
    with pytest.raises(ValueError):
        desktop_mcp.type_text("a" * (desktop_mcp.MAX_TEXT_CHARS + 1))


def test_click_and_paste(gui, monkeypatch):
    copied = []
    monkeypatch.setattr(desktop_mcp, "_clipboard_copy", copied.append)
    desktop_mcp.click_and_paste(5, 6, "한글 붙여넣기")
    assert copied == ["한글 붙여넣기"]
    assert gui.calls == [("click", 5, 6), ("hotkey", "ctrl", "v")]


def test_click_and_paste_checks_before_copy(gui, monkeypatch):
    copied = []
    monkeypatch.setattr(desktop_mcp, "_clipboard_copy", copied.append)
    with pytest.raises(ValueError):
        desktop_mcp.click_and_paste(99999, 0, "x")
    assert copied == [] and gui.calls == []


# --- 프로그램·명령 실행 ------------------------------------------------------

def test_launch_program_allowlist(workspace, monkeypatch):
    launched = []
    monkeypatch.setattr(
        desktop_mcp.subprocess, "Popen",
        lambda args, **kw: launched.append((args, kw)),
    )
    desktop_mcp.launch_program("notepad")
    assert launched[0][0] == ["notepad.exe"]
    assert launched[0][1]["shell"] is False
    with pytest.raises(ValueError):
        desktop_mcp.launch_program("cmd")
    assert len(launched) == 1


def test_terminal_allowlist(workspace):
    result = desktop_mcp.terminal("python_version")
    assert result["returncode"] == 0
    assert "Python" in result["stdout"] + result["stderr"]
    with pytest.raises(ValueError):
        desktop_mcp.terminal("rm -rf /")


# --- 실제 서버 실행(stdio) ---------------------------------------------------

def test_server_starts_over_stdio_and_lists_tools():
    """Claude Desktop이 하는 것처럼 서버를 하위 프로세스로 띄우고 도구 목록을 받는다."""
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=[desktop_mcp.__file__]
    )

    async def scenario():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                blocked = await session.call_tool("read_text_file", {"relative_path": "../README.md"})
                return {t.name for t in tools.tools}, blocked

    names, blocked = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert "screenshot" in names and len(names) == 12
    assert blocked.isError
