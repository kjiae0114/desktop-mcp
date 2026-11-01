
from pathlib import Path
import subprocess
import sys
import pyperclip

import pyautogui
from mcp.server.fastmcp import FastMCP, Image

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE = (BASE_DIR / "workspace").resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

mcp = FastMCP(
    "windows-desktop",
    host="127.0.0.1",
    port=8765,
)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.2

# 임의의 실행 파일이나 셸 명령을 허용하지 않는다.
PROGRAMS = {
    "notepad": ["notepad.exe"],
    "calculator": ["calc.exe"],
    "explorer_workspace": [
        "explorer.exe", str(WORKSPACE)
    ],
}

def safe_path(relative_path: str) -> Path:
    """작업 폴더 바깥으로 나가지 못하게 경로를 검증한다."""
    candidate = (WORKSPACE / relative_path).resolve()

    if not candidate.is_relative_to(WORKSPACE):
        raise ValueError("작업 폴더 밖의 경로는 허용되지 않습니다.")

    return candidate


@mcp.tool()
def get_screen_size() -> dict:
    """모니터의 가로 및 세로 크기를 조회한다."""
    width, height = pyautogui.size()
    return {"width": width, "height": height}


@mcp.tool()
def screenshot() -> Image:
    """현재 화면을 캡처한다."""
    shot = pyautogui.screenshot()
    import io
    buffer = io.BytesIO()
    shot.save(buffer, format="PNG")
    return Image(data=buffer.getvalue(), format="png")


@mcp.tool()
def launch_program(name: str) -> str:
    """허용된 프로그램만 실행한다: notepad, calculator, explorer_workspace."""
    if name not in PROGRAMS:
        raise ValueError(
            f"허용되지 않은 프로그램입니다. "
            f"사용 가능: {', '.join(PROGRAMS)}"
        )

    subprocess.Popen(
        PROGRAMS[name],
        shell=False,
        cwd=str(WORKSPACE),
    )
    return f"{name} 실행 요청 완료"


@mcp.tool()
def list_files(relative_dir: str = ".") -> list[dict]:
    """작업 폴더 내 파일과 하위 폴더를 조회한다."""
    directory = safe_path(relative_dir)

    if not directory.is_dir():
        raise ValueError("폴더가 존재하지 않습니다.")

    items = []
    for item in sorted(directory.iterdir())[:200]:
        # 폴더 안의 심볼릭 링크를 따라가지 않는다.
        if item.is_symlink():
            continue
        items.append({
            "name": item.name,
            "is_dir": item.is_dir(),
            "size": item.stat().st_size if item.is_file() else None,
        })
    return items


@mcp.tool()
def read_text_file(relative_path: str) -> str:
    """작업 폴더 내 UTF-8 텍스트 파일을 읽는다."""
    path = safe_path(relative_path)

    if path.is_symlink() or not path.is_file():
        raise ValueError("일반 파일만 읽을 수 있습니다.")

    if path.stat().st_size > 200_000:
        raise ValueError("파일이 너무 큽니다. 최대 200KB입니다.")

    return path.read_text(encoding="utf-8")


@mcp.tool()
def write_text_file(relative_path: str, content: str) -> str:
    """작업 폴더에 텍스트 파일을 생성하거나 덮어쓴다."""
    path = safe_path(relative_path)

    if path.is_symlink():
        raise ValueError("심볼릭 링크에는 쓸 수 없습니다.")

    if len(content) > 100_000:
        raise ValueError("내용이 너무 깁니다. 최대 100KB입니다.")

    if path.exists() and not path.is_file():
        raise ValueError("일반 파일만 덮어쓸 수 있습니다.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"저장 완료: {path.relative_to(WORKSPACE)}"


@mcp.tool()
def click(x: int, y: int) -> str:
    """화면 좌표를 클릭한다."""
    width, height = pyautogui.size()

    if not (0 <= x < width and 0 <= y < height):
        raise ValueError("화면 범위를 벗어난 좌표입니다.")

    pyautogui.click(x, y)
    return f"클릭 완료: ({x}, {y})"


@mcp.tool()
def get_cursor_position() -> dict:
    """현재 마우스 커서의 화면 좌표를 반환한다."""
    x, y = pyautogui.position()
    return {
        "x": x,
        "y": y
    }


@mcp.tool()
def press_key(key: str) -> str:
    """제한된 키 하나를 입력한다."""
    allowed = {
        "enter", "tab", "esc", "space",
        "backspace", "delete", "up", "down",
        "left", "right", "home", "end"
    }

    if key.lower() not in allowed:
        raise ValueError("허용되지 않은 키입니다.")

    pyautogui.press(key.lower())
    return f"키 입력 완료: {key}"


@mcp.tool()
def type_text(text: str) -> str:
    """현재 포커스된 창에 ASCII 텍스트를 입력한다."""
    if len(text) > 2000:
        raise ValueError("최대 2000자까지 입력할 수 있습니다.")

    if not text.isascii():
        raise ValueError(
            "이 입력 도구는 ASCII만 지원합니다. "
            "한글 입력은 별도 기능으로 구현해야 합니다."
        )

    pyautogui.write(text, interval=0.01)
    return f"입력 완료: {len(text)}자"


@mcp.tool()
def click_and_paste(x: int, y: int, text: str) -> str:
    """지정한 화면 좌표를 클릭하고 클립보드로 텍스트를 붙여넣는다."""
    if len(text) > 2000:
        raise ValueError("최대 2000자까지 입력할 수 있습니다.")

    width, height = pyautogui.size()
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError("화면 범위를 벗어난 좌표입니다.")

    # 먼저 클립보드에 텍스트를 준비한다.
    pyperclip.copy(text)

    # 지정한 창에 포커스를 준 뒤 붙여넣는다.
    pyautogui.click(x, y)
    pyautogui.hotkey("ctrl", "v")

    return f"붙여넣기 요청 완료: {len(text)}자"


@mcp.tool()
def terminal(command: str) -> dict:
    """허용된 읽기 전용 명령만 작업 폴더에서 실행한다."""
    allowed = {
        "pwd": [sys.executable, "-c",
                "import os; print(os.getcwd())"],
        "git_status": ["git", "status", "--short"],
        "git_branch": ["git", "branch", "--show-current"],
        "python_version": [sys.executable, "--version"],
    }

    if command not in allowed:
        raise ValueError(
            f"허용되지 않은 명령입니다. 사용 가능: {', '.join(allowed)}"
        )

    result = subprocess.run(
        allowed[command],
        cwd=str(WORKSPACE),
        shell=False,
        capture_output=True,
        text=True,
        timeout=10,
        encoding="utf-8",
        errors="replace",
    )

    return {
        "returncode": result.returncode,
        "stdout": result.stdout[:10000],
        "stderr": result.stderr[:3000],
    }


if __name__ == "__main__":
    # 로컬 전용으로 바인딩한다.
    mcp.run(transport="streamable-http")
