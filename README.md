# desktop-mcp

[![test](https://github.com/kjiae0114/desktop-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/kjiae0114/desktop-mcp/actions/workflows/test.yml)

> **English summary** — A Windows-only personal toolbox with two independent parts:
> **(A)** a local coding agent that lets a free Ollama model (`qwen3.5:4b`) edit code in an isolated
> Git worktree, runs the model's code only inside a network-less Docker container, and commits to a
> separate branch only when the full test suite passes (no push, no merge); and **(B)** a small MCP server
> (`desktop_mcp.py`, Python + FastMCP + PyAutoGUI) that gives Claude Desktop / Claude Code allow-listed
> screen capture, clicks, keys, and file access limited to `workspace/`.
> Quick start: `.\scripts\check.ps1 -Setup` (venv + deps + tests), then register the MCP server as in §4-2.
> HTTP mode (`--http`) requires a bearer token in `DESKTOP_MCP_TOKEN`; stdio is the default and recommended.
> All safety rules are allow-lists, and there are 264 pytest tests, run on Windows by GitHub Actions (Docker tests auto-skip without Docker).

이 문서만 보고 처음 받는 사람도 설치, 사용, 문제 해결, 유지보수를 할 수 있도록 썼습니다. 모든 명령은 PowerShell에서 이 폴더(`desktop-mcp`)를 현재 위치로 두고 실행합니다.

## 1. 이 프로젝트는 무엇인가

한 저장소에 서로 독립된 프로그램 두 개가 있습니다. 둘은 서로 호출하지 않습니다.

| 이름 | 하는 일 | 실행 방법 |
|---|---|---|
| **A. 로컬 코딩 에이전트** (주력) | 내 PC의 무료 모델(Ollama)이 코드를 고치게 하고, 테스트를 통과한 결과만 별도 Git 브랜치에 커밋 | `python -m dev_automation agent ...` |
| **B. 화면 제어 MCP 서버** | Claude 같은 AI 프로그램이 내 화면 캡처, 클릭, 정해진 키 입력, `workspace` 폴더 파일 읽기·쓰기를 하게 해 줌 | `python desktop_mcp.py` |

에이전트의 원칙은 "모델의 말은 믿지 않는다"입니다.

- 모델은 원본이 아닌 작업용 복사본(Git 워크트리)만 고칩니다.
- 모델이 쓴 코드는 네트워크가 끊긴 Docker 컨테이너 안에서만 테스트합니다.
- 성공 여부는 테스트 결과로 판정합니다.
- push와 병합은 하지 않습니다. 결과를 읽고 반영할지는 사람이 정합니다.

## 2. 처음 설치 (한 번만)

### 2-1. 필요한 프로그램

| 프로그램 | 확인 명령 | 정상일 때 |
|---|---|---|
| Python 3.12 | `py -3.12 --version` | `Python 3.12.x` |
| Git | `git --version` | 버전 출력 |
| [Ollama](https://ollama.com) | `ollama --version` | 버전 출력. 명령을 못 찾으면 터미널을 새로 열기 |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | `docker version` | `Server:` 항목까지 출력. 없으면 Docker Desktop 실행 |

### 2-2. 가상환경과 패키지

이 프로젝트는 **`.venv-new`** 를 씁니다(스크립트와 문서가 모두 이 이름을 가정합니다). 2-5의 `.\scripts\check.ps1 -Setup`이 아래 두 줄을 대신 해 줍니다.

```powershell
py -3.12 -m venv .venv-new        # .venv-new가 이미 있으면 건너뜀
.\.venv-new\Scripts\python.exe -m pip install -r requirements.txt pytest
```

### 2-3. 모델 내려받기 (약 3.3GB)

```powershell
ollama pull qwen3.5:4b-q4_K_M
ollama list                       # qwen3.5:4b-q4_K_M 가 보이면 완료
```

### 2-4. 테스트용 Docker 이미지 만들기

처음 한 번 만들고, `requirements.txt`가 바뀌면 다시 만듭니다.

```powershell
docker build -t desktop-mcp-sandbox:py312 -f sandbox/python.Dockerfile .
docker image ls desktop-mcp-sandbox   # py312 태그가 보이면 완료
```

### 2-5. 설치 확인

```powershell
.\scripts\check.ps1                # 테스트만
.\scripts\check.ps1 -Setup -Image  # 2-2, 2-4까지 한 번에 하고 테스트
```

- 직접 돌리려면 `.\.venv-new\Scripts\python.exe -m pytest` (옵션은 `pytest.ini`에 있음).
- 약 2~3분이 걸립니다. Docker 컨테이너를 실제로 띄우는 테스트가 포함되어 있기 때문입니다. Docker나 이미지가 없으면 그 테스트들은 건너뜁니다.
- 2026-11-06 기준 결과는 `263 passed, 1 skipped`입니다. 새로 clone한 폴더에서 `-Setup`부터 따라 해서 같은 결과가 나오는 것을 확인했습니다(Docker 이미지가 이미 있는 PC 기준).
- 건너뛴 1건은 심볼릭 링크를 만들 권한이 없어서입니다. Windows 개발자 모드를 켜면 실행됩니다.
- 실패가 있으면 아래 "7. 문제 해결"을 보세요.

## 3. 코딩 에이전트 사용법

### 3-1. 기본 흐름

1. 원하는 동작을 **테스트 파일로 먼저 씁니다.** 작은 모델은 스스로 쓴 테스트와 모순되기 쉬워서, 사람이 정답지를 주는 편이 훨씬 잘 됩니다.
2. 에이전트에 작업 문장과 테스트 파일을 줍니다. `--test`로 준 파일은 모델이 고칠 수 없습니다(잠금).
3. 결과를 확인하고, 좋으면 직접 병합합니다.

### 3-2. 예시: 함수 하나 만들기

어느 예시든 순서는 같습니다. **① 정답지(테스트 파일)를 만들고 → ② 명령을 실행합니다.** 정답지는 커밋하지 않아도 됩니다. 아래 예시는 2026-11-06에 실제로 실행해 모두 성공했고, 각각 2분 안팎 걸렸습니다.

#### 가장 쉬운 예시 ①: 짧게 시키기

`tests/test_mathutil.py`:

```python
from dev_automation.mathutil import add


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0
```

```powershell
.\.venv-new\Scripts\python.exe -m dev_automation agent desktop-mcp "dev_automation/mathutil.py에 add(a, b) 함수를 만들어줘" --test tests/test_mathutil.py
```

결과: 성공, 111초. 만든 코드는 `return a + b` 두 줄이었습니다.

#### 가장 쉬운 예시 ②: 평소 말하듯이 시키기

`tests/test_sizeutil.py`:

```python
from dev_automation.sizeutil import human_size


def test_bytes():
    assert human_size(500) == "500B"


def test_kilobytes():
    assert human_size(1536) == "1.5KB"


def test_megabytes():
    assert human_size(5 * 1024 * 1024) == "5.0MB"
```

```powershell
.\.venv-new\Scripts\python.exe -m dev_automation agent desktop-mcp "파일 크기를 사람이 읽기 좋게 바꿔주는 함수가 필요해. 1536 넣으면 1.5KB 나오게. dev_automation/sizeutil.py에 human_size로 만들어줘" --test tests/test_sizeutil.py
```

결과: 성공, 108초. B·KB·MB를 나누는 8줄짜리 함수를 만들었습니다.

말투는 편해도 됩니다. 다만 **파일 경로**와 **함수 이름**은 꼭 넣고, 예시 입력·출력("1536 넣으면 1.5KB")을 하나 넣으면 성공률이 올라갑니다. 정답지에는 문장에 없는 경우(500B, 5.0MB)도 넣어 둘 수 있습니다. 모델은 정답지를 읽고 맞춥니다.

#### 조금 더 자세한 예시 ③: 정답지를 보라고 시키기

`tests/test_textutil.py`에 정답지를 씁니다.

```python
import pytest
from dev_automation.textutil import format_duration

def test_hours_minutes_seconds():
    assert format_duration(3725) == "1시간 2분 5초"

def test_zero():
    assert format_duration(0) == "0초"

def test_negative_raises():
    with pytest.raises(ValueError):
        format_duration(-1)
```

에이전트를 실행합니다.

```powershell
.\.venv-new\Scripts\python.exe -m dev_automation agent desktop-mcp "dev_automation/textutil.py 파일을 새로 만들어 format_duration(seconds) 함수를 작성하세요. 자세한 동작은 tests/test_textutil.py를 보세요." --test tests/test_textutil.py
```

| 인자·옵션 | 뜻 | 기본값 |
|---|---|---|
| `desktop-mcp` | 대상 프로젝트 ID (`config/projects.json`에 등록된 이름) | 필수 |
| `"작업 문장"` | 모델에게 줄 지시. 고칠 파일 경로와 입력·출력 예시를 적기 | 필수 |
| `--test 경로` | 잠글 테스트 파일. 여러 번 지정 가능 | 없음 (지정 권장) |
| `--model 이름` | 사용할 Ollama 모델 | `qwen3.5:4b-q4_K_M` |
| `--max-steps N` | 모델 호출 최대 횟수 | `25` |
| `--unsafe-host-tests` | Docker 없이 PC에서 직접 테스트. **위험**. `sandbox` 설정이 없는 프로젝트에서만 의미 있음 | 끔 |

### 3-3. 실행 중 화면 읽는 법

```
테스트 격리: Docker(desktop-mcp-sandbox:py312)   ← 격리가 켜졌다는 뜻
경고: 커밋되지 않은 변경은 에이전트 작업에 포함되지 않습니다.   ← 정상(아래 참고)
작업 브랜치: agent/agent-20261106-010556-a54f2a  ← 결과가 들어갈 브랜치
기준 테스트 실행 중...                            ← 작업 전 전체 테스트(2~3분)
잠근 테스트: tests/test_textutil.py
도구: read_file dev_automation/...               ← 모델이 쓰는 도구와 대상 파일
변경 확인을 위해 테스트 실행 중...                 ← 모델이 "끝"이라고 한 뒤 최종 판정
```

- **커밋하지 않은 변경 경고:** 에이전트는 현재 커밋(HEAD)에서 출발합니다. 그래서 커밋하지 않은 수정은 모델이 보지 못합니다.
- **예외:** `--test`로 준 파일은 커밋하지 않았어도 복사해 들어갑니다.

### 3-4. 결과 확인과 반영

마지막에 `상태:`가 출력됩니다.

| 상태 | 뜻 | 할 일 |
|---|---|---|
| `succeeded` | 전체 테스트 통과, 검사 통과, `agent/...` 브랜치에 커밋됨 | 아래 명령으로 읽고 결정 |
| `failed` | 중간에 멈춤. 메시지에 이유가 나옴 | "7. 문제 해결"의 표 참고 |
| `no_changes` | 모델이 아무것도 바꾸지 않음 | 작업 문장을 더 구체적으로 |

성공한 경우의 처리 방법은 다음과 같습니다.

```powershell
git diff main..agent/agent-20261106-010556-a54f2a          # 바뀐 내용 읽기 (필수)
git merge agent/agent-20261106-010556-a54f2a               # 마음에 들면 병합
git branch -D agent/agent-20261106-010556-a54f2a           # 버릴 거면 브랜치 삭제
```

- `경고: 모델이 수정한 테스트가 있습니다`가 나오면, 모델이 테스트를 약하게 만들지 않았는지 diff에서 꼭 확인하세요.
- 병합하고 나면 그 코드는 격리 없이 내 PC에서 실행됩니다. 병합 전에 반드시 읽으세요.

### 3-5. 남는 기록

작업마다 `runs/agent/<작업 ID>/`가 생기고, 성공·실패와 관계없이 남습니다. 이 폴더는 Git에 올라가지 않으며, 필요 없으면 폴더째 지워도 됩니다.

| 파일 | 내용 |
|---|---|
| `final.diff` | 모델이 바꾼 전체 내용 |
| `conversation.json` | 모델과 주고받은 대화와 도구 호출 전체. 모델이 어디서 막혔는지 볼 때 사용 |

작업용 폴더(`worktree`)는 끝나면 자동으로 지워집니다. 실패한 작업의 브랜치도 지워집니다.

### 3-6. 작업을 잘 시키는 법

- 한 번에 **함수 하나, 파일 하나**만 맡깁니다. 여러 파일에 걸친 수정이나 설계는 자주 실패합니다.
- 작업 문장에 고칠 파일 경로, 함수 이름, 입력과 출력 예시를 씁니다.
- 실패하면 `conversation.json`을 보고 작업을 더 잘게 나눠 다시 시킵니다.
- 같은 과제도 실행할 때마다 결과가 다를 수 있습니다. 같은 `format_duration` 과제의 실제 기록은 4번 중 3번 성공이었습니다(2026-11-02에 3번, 2026-11-06에 1번).
- 대략적인 실력은 `workspace/bench_qwen3_5_4b_q4_k_m.json`에 있습니다. 쉬운 함수 작성은 3/3, 버그 수정은 2/3, 이 프로젝트 수준의 함수 작성은 1/3이었습니다.

### 3-7. GPU·CPU·메모리 사용량

RTX 4050 Laptop(6GB), i5-13500HX, RAM 14GB에서 2026-11-06에 잰 값입니다.

| 상태 | VRAM | 모델 프로세스(llama-server) RAM | CPU |
|---|---|---|---|
| 모델이 안 올라가 있을 때 | 10MiB | 0 | 0% |
| 에이전트 실행 중 | 약 3,970MiB (GPU 100%) | 약 1,100~1,250MB | 응답 생성 중 코어 1개(전체의 약 5%), 대기 중 0% |

- 응답 속도는 초당 약 54토큰입니다. 처음 모델을 불러올 때 5~11초 걸립니다.
- 에이전트는 끝날 때(실패·예외 포함) `OllamaClient.unload()`로 모델을 바로 내립니다. 이게 없으면 Ollama가 마지막 요청 뒤 5분 동안 VRAM 약 4GB를 붙잡고 있습니다.
- `num_ctx`를 8192에서 4096으로 줄여도 VRAM은 약 210MB만 줄어듭니다. 대화가 잘릴 위험이 있으므로 8192를 유지합니다(대화 기록 상한 24,000자).
- 플래시 어텐션은 이미 켜져 있습니다(`flash_attn = auto`). 모델이 다 올라가 있는지는 `ollama ps`의 PROCESSOR가 `100% GPU`인지로 확인합니다.

## 4. 화면 제어 MCP 서버 사용법

### 4-1. 실행 방식 두 가지

| 방식 | 명령 | 언제 |
|---|---|---|
| **stdio (기본)** | `.\.venv-new\Scripts\python.exe desktop_mcp.py` | Claude Desktop·Claude Code가 필요할 때 직접 띄움. 손으로 실행할 일 없음 |
| HTTP | `.\.venv-new\Scripts\python.exe desktop_mcp.py --http` | 서버를 따로 켜 두고 여러 클라이언트가 붙을 때. 주소 `http://127.0.0.1:8765/mcp`, **토큰 필수** |

- stdio 방식을 손으로 실행하면 입력을 기다리며 아무것도 출력하지 않는 것이 정상입니다(`Ctrl+C`로 종료).
- HTTP 방식은 이 PC 안(127.0.0.1)에서만 접속할 수 있고, 모든 요청에 `Authorization: Bearer <토큰>` 헤더가 있어야 합니다. 헤더가 없거나 토큰이 틀리면 `401`을 돌려줍니다.
- 토큰은 환경변수 `DESKTOP_MCP_TOKEN`으로 줍니다(16자 이상). 없으면 서버가 시작하지 않고 안내 메시지를 낸 뒤 종료합니다.

  ```powershell
  $env:DESKTOP_MCP_TOKEN = .\.venv-new\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
  $env:DESKTOP_MCP_TOKEN            # 클라이언트 설정에 넣을 값. 남에게 보여 주지 말 것
  .\.venv-new\Scripts\python.exe desktop_mcp.py --http
  ```

  `$env:`로 넣은 값은 그 PowerShell 창을 닫으면 사라집니다. 다음에는 새 토큰을 만들고 클라이언트 설정도 바꿉니다.
- 특별한 이유가 없으면 stdio 방식을 쓰세요. 네트워크 포트를 열지 않아 더 안전합니다.
- 파일을 다루는 작업 폴더는 `workspace/`입니다. 그 밖의 경로는 거부합니다.
- 2026-11-06 이전 버전은 인자 없이 실행하면 HTTP였습니다. 지금은 `--http`가 필요합니다.

### 4-2. AI 프로그램에 연결

**Claude Desktop:** 설정 → 개발자 → 구성 편집을 누르면 설정 파일(`claude_desktop_config.json`)이 열립니다. 보통 `%APPDATA%\Claude\`에 있습니다. Microsoft Store판처럼 그 폴더에 없을 때는 `%LOCALAPPDATA%\Claude-Data\claude_<무작위 문자열>\`에 있습니다. 이 파일에는 앱 환경설정(`preferences`)도 들어 있으니 다른 내용은 지우지 말고, 맨 바깥 `{ }` 안에 `"mcpServers"` 항목만 추가하세요. 아래 내용을 넣고 Claude Desktop을 완전히 종료(트레이 아이콘까지)한 뒤 다시 엽니다. 경로는 내 PC에 맞게 바꾸고 `\`는 `/`로 씁니다.

```json
{
  "mcpServers": {
    "windows-desktop": {
      "command": "C:/programming/02_tools/desktop-mcp/.venv-new/Scripts/python.exe",
      "args": ["C:/programming/02_tools/desktop-mcp/desktop_mcp.py"]
    }
  }
}
```

이미 `mcpServers`가 있으면 그 안에 `"windows-desktop": {...}` 항목만 추가합니다. 연결되면 입력창의 도구(🔨) 메뉴에 12개 도구가 보입니다. 안 보이면 `%APPDATA%\Claude\logs\mcp-server-windows-desktop.log`를 확인하세요.

**Claude Code (stdio):**

```powershell
claude mcp add windows-desktop -- C:/programming/02_tools/desktop-mcp/.venv-new/Scripts/python.exe C:/programming/02_tools/desktop-mcp/desktop_mcp.py
claude mcp list
```

**HTTP 방식으로 붙일 때** (4-1의 `--http`로 서버를 먼저 켬):

```powershell
claude mcp add --transport http windows-desktop http://127.0.0.1:8765/mcp --header "Authorization: Bearer $env:DESKTOP_MCP_TOKEN"
```

- 자동 테스트(`tests/test_desktop_mcp.py`)가 서버를 stdio로 실제로 띄워 도구 12개 목록과 경로 탈출 차단을 확인합니다.
- Claude Desktop·Claude Code 등록 자체는 각 앱의 화면에서 직접 확인해야 합니다(자동 테스트 없음).
- 다른 MCP 클라이언트도 stdio 또는 streamable HTTP를 지원하면 연결할 수 있습니다.

### 4-3. 제공하는 도구 (12개)

| 도구 | 하는 일 | 제한 |
|---|---|---|
| `get_screen_size` | 화면 크기 | — |
| `get_cursor_position` | 마우스 위치 | — |
| `screenshot` | 화면 캡처(PNG) | — |
| `click` | 좌표 클릭 | 화면 범위 안만 |
| `press_key` | 키 하나 입력 | enter, tab, esc, space, backspace, delete, 방향키, home, end만 |
| `type_text` | 글자 입력 | 영문 등 ASCII만, 2000자까지 |
| `click_and_paste` | 좌표 클릭 후 클립보드로 붙여넣기 | 한글 입력은 이것으로. 2000자까지 |
| `launch_program` | 프로그램 실행 | notepad, calculator, explorer_workspace만 |
| `list_files` | `workspace` 안 목록 | 200개까지, 링크는 숨김 |
| `read_text_file` | `workspace` 안 파일 읽기 | 200KB까지 |
| `write_text_file` | `workspace` 안 파일 쓰기 | 100KB까지 |
| `terminal` | 정해진 명령 실행 | pwd, git_status, git_branch, python_version만 |

비상 정지: 마우스를 화면 **왼쪽 위 모서리**로 밀면 pyautogui의 FAILSAFE가 동작해 진행 중인 마우스·키보드 조작이 멈춥니다.

### 4-4. 클릭할 좌표 찾기

```powershell
.\.venv-new\Scripts\python.exe coordinate_picker.py
```

1. 현재 화면이 축소된 창으로 뜹니다.
2. 원하는 지점을 클릭합니다.
3. `Enter`를 누르면 실제 화면 좌표(`x=..., y=...`)가 클립보드에 복사됩니다. `Esc`는 취소입니다.

## 5. 다른 프로젝트에 에이전트 쓰기

대상 프로젝트는 세 가지를 갖춰야 합니다.

1. Git 저장소여야 하고, 테스트가 pytest로 작성되어 있으며, 가상환경이 있어야 합니다.
2. 대상 프로젝트의 `.gitignore`에 `__pycache__/`가 있어야 합니다.
3. 작업 전에 대상 프로젝트의 테스트가 모두 통과해야 합니다.

**1단계.** 그 프로젝트의 의존성을 넣은 테스트 이미지를 만듭니다. 대상 프로젝트 폴더에서 이 저장소의 Dockerfile을 지정해 빌드하면, 대상 프로젝트의 `requirements.txt`가 설치됩니다.

```powershell
cd C:\경로\my-app
docker build -t my-app-sandbox:py312 -f C:\programming\02_tools\desktop-mcp\sandbox\python.Dockerfile .
```

**2단계.** `config/projects.json`의 `projects`에 항목을 추가합니다.

```json
"my-app": {
  "path": "C:/경로/my-app",
  "python": ".venv/Scripts/python.exe",
  "default_cwd": ".",
  "commands": ["pytest"],
  "allowed_env": [],
  "sandbox": "my-app-sandbox:py312"
}
```

| 항목 | 뜻 |
|---|---|
| `path` | 프로젝트 폴더. 상대 경로면 `config` 폴더 기준 |
| `python` | 프로젝트 가상환경의 python (프로젝트 폴더 기준) |
| `default_cwd` | 테스트를 실행할 폴더 (프로젝트 폴더 기준) |
| `commands` | 허용할 명령 프로파일 이름. 지금은 `pytest`만 있음 |
| `allowed_env` | 테스트에 넘길 환경 변수 이름. 비밀값이 든 변수는 넣지 말 것 |
| `sandbox` | 테스트 이미지. 없으면 `--unsafe-host-tests` 없이는 실행을 거부 |

설정 파일의 규칙은 다음과 같습니다.

- 항목이 하나라도 틀리면 설정 전체를 거부합니다. 오류 메시지에 어느 항목이 틀렸는지 나옵니다.
- 프로젝트 ID는 영문 소문자, 숫자, `-`, `_`만 쓸 수 있습니다.
- `path`에 내 PC 절대 경로가 들어가므로 이 파일을 공개 저장소에 올리지 마세요.

## 6. 폴더 구조

| 경로 | 내용 | Git |
|---|---|---|
| `dev_automation/` | 에이전트 코드 (아래 표) | 포함 |
| `desktop_mcp.py` | 화면 제어 MCP 서버 | 포함 |
| `coordinate_picker.py` | 좌표 선택 보조 창 | 포함 |
| `config/projects.json` | 에이전트 대상 프로젝트 등록 | 포함 |
| `sandbox/python.Dockerfile` | 테스트 이미지 정의 | 포함 |
| `tests/` | 자동 테스트 | 포함 |
| `scripts/check.ps1` | 설치·이미지 빌드·테스트 한 번에 | 포함 |
| `pytest.ini` | 테스트 위치와 기본 옵션 (`runs/`, `workspace/` 제외) | 포함 |
| `CLAUDE.md` | Claude Code용 작업 안내와 남은 할 일 | 포함 |
| `runs/` | 에이전트 실행 기록 | 제외 |
| `workspace/` | MCP 서버의 파일 작업 폴더, 실험 스크립트 | 제외 |
| `.venv-new/` | 사용하는 가상환경 | 제외 |

| `dev_automation/` 파일 | 역할 |
|---|---|
| `__main__.py` | 명령줄 입구, 옵션 해석, Docker 격리 여부 결정 |
| `agent.py` | 작업 흐름: 워크트리 → 기준 테스트 → 모델 반복 → 최종 테스트 → 검사 → 커밋 |
| `agent_tools.py` | 모델에게 주는 도구 5개(목록, 읽기, 쓰기, 부분 수정, 테스트)와 차단 경로 |
| `llm.py` | Ollama 호출. 127.0.0.1/localhost 외 주소는 거부 |
| `sandbox.py` | Docker 컨테이너에서 테스트 실행 |
| `runner.py` | 등록된 명령만 셸 없이 실행. 시간·로그 크기 제한, 자식 프로세스까지 종료 |
| `projects.py` | `config/projects.json` 검증, 명령 프로파일 정의 |
| `paths.py` | 프로젝트 밖 경로, 링크·정션 탈출 차단 |
| `runs.py` | 백그라운드 실행 이력·로그·취소. **아직 어디에도 연결되지 않음** |

## 7. 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| `Ollama에 연결하지 못했습니다` | 시작 메뉴에서 Ollama 실행 → `ollama list`로 확인 |
| `Docker를 실행하지 못했습니다` / `Docker에 연결하지 못했습니다` | Docker Desktop 실행 → `docker version`에 `Server:`가 나오는지 확인 |
| `테스트 이미지가 없습니다` | 2-4의 `docker build` 실행 |
| `이 프로젝트에는 sandbox 설정이 없어 실행하지 않습니다` | `config/projects.json`에 `sandbox` 추가. 위험을 알고도 PC에서 돌리려면 `--unsafe-host-tests` |
| `작업 전부터 테스트가 실패합니다` | 현재 커밋의 테스트부터 고칩니다. `--test`로 줄 새 테스트는 이 단계에서 아직 실행되지 않으니 원인이 아닙니다 |
| `잠글 테스트 경로가 올바르지 않습니다` | `--test` 경로는 프로젝트 폴더 기준 상대 경로로, 실제 있는 파일이어야 함 |
| `같은 수정을 반복해 중단했습니다` | 모델이 같은 수정을 3번 함. 테스트끼리 모순이 없는지 보고 작업을 잘게 나누기 |
| `최대 단계(25)를 넘었습니다` | `conversation.json`에서 막힌 곳 확인. 작업을 나누거나 `--max-steps`를 늘리기 |
| `수정 후에도 테스트가 계속 실패했습니다` | 최종 테스트 실패가 3번 넘음. 메시지 끝의 테스트 출력 확인 |
| `비밀정보로 보이는 내용이 있어 커밋하지 않습니다` | `final.diff`에 토큰·키 패턴이 있음. 내용 확인 |
| `차단된 경로의 파일이 변경되어` / `잠근 테스트가 변경되어` | 모델이 막힌 파일을 바꾸려 함. 커밋되지 않았으니 다시 시키기 |
| 컨테이너에서만 `ModuleNotFoundError` | 필요한 패키지를 `requirements.txt`에 넣고 이미지 다시 빌드 |
| 응답이 매우 느림 | 첫 실행은 모델을 불러오느라 1~2분 걸림. 계속 느리면 `ollama ps`에서 GPU 100%인지 확인 |
| MCP 서버가 `address already in use` | 8765 포트를 이미 쓰는 중. 켜 둔 서버가 있는지 확인 |
| 모델을 지우고 싶음 | `ollama rm 모델이름` |

## 8. 유지보수

- **변경 후 확인:** 코드를 고치면 `.\scripts\check.ps1`로 전체 테스트를 돌립니다.
- **CI:** GitHub에 push하거나 PR을 열면 GitHub Actions(`.github/workflows/test.yml`)가 `windows-latest`에서 같은 테스트를 돌립니다. CI에는 Docker 테스트 이미지가 없어서 Docker 통합 테스트 17개와 `.venv-new`가 필요한 테스트 1개는 건너뜁니다(2026-11-06 기준 `246 passed, 18 skipped`). 그래서 Docker 쪽을 고쳤다면 내 PC에서도 꼭 돌려 보세요. 결과는 README 맨 위 배지와 저장소의 Actions 탭에서 봅니다.
- **의존성 변경:** `requirements.txt`를 바꾸면 2-2의 pip 설치와 2-4의 이미지 빌드를 다시 합니다.
- **모델 교체:**
  1. `ollama pull 새모델`로 받습니다.
  2. `--model 새모델`로 시험해 봅니다.
  3. 기본값을 바꾸려면 `dev_automation/__main__.py`의 `--model` 기본값을 고칩니다.
  4. 모델은 도구 호출(tool calling)을 지원해야 합니다.
- **명령 추가:** 에이전트가 실행할 수 있는 명령은 `dev_automation/projects.py`의 `COMMAND_PROFILES`에만 정의합니다. 설정 파일로는 임의의 명령을 등록할 수 없습니다(의도된 제한).
- **MCP 도구 추가:** `desktop_mcp.py`에 `@mcp.tool()` 함수를 추가합니다. 이때 다음을 지킵니다.
  - 새 기능도 허용 목록 방식으로 제한합니다.
  - 화면을 건드리는 코드는 `pyautogui`를 직접 쓰지 말고 `_gui()`를 통해 씁니다(테스트에서 가짜로 바꿔 끼움).
  - `tests/test_desktop_mcp.py`에 테스트를 추가하고, 도구 이름 목록(`test_registered_tools`, 개수 확인)과 `tests/test_runner.py`의 `test_existing_desktop_mcp_is_unaffected` 목록도 함께 고칩니다.
- **정리:** 쌓인 `runs/agent/*` 폴더와 쓰지 않는 `agent/*` 브랜치(`git branch --list "agent/*"`)는 지워도 됩니다. main에 병합된 브랜치는 `git branch --merged main`으로 확인한 뒤 `git branch -d`로 지웁니다.

## 9. 안전 규칙과 알려진 한계

규칙은 다음과 같습니다.

- 에이전트는 push와 병합을 하지 않습니다.
- 모델이 쓴 코드는 Docker 밖에서 실행하지 않습니다. Docker에 문제가 있으면 PC 실행으로 넘어가지 않고 실패로 끝납니다.
- 모델은 `.git`, `.venv*`, `runs`, `.env*`, `*.pem`, `*.key`를 읽거나 쓸 수 없습니다.
- 자식 프로세스에는 허용된 환경 변수만 넘어갑니다.
- 모델 서버와 MCP 서버는 모두 127.0.0.1에서만 동작합니다.

한계는 다음과 같습니다.

| 한계 | 설명 |
|---|---|
| 모델 실력 | 작은 무료 모델이라 작은 단위 작업만 믿을 만함. 실제 시험은 같은 과제 4회뿐 |
| 격리의 한계 | 커널이나 Docker Desktop 취약점까지는 막지 못함. 이미지 빌드 때는 네트워크를 씀 |
| 병합 후 | 병합한 코드는 격리 없이 실행됨 |
| 테스트 약화 | `--test` 없이 실행하면 모델이 테스트를 고쳐 통과시킬 수 있음(경고는 나옴) |
| 비밀값 검사 | GitHub 토큰, AWS 키, 개인키, `sk-` 키 패턴만 검사. 그 밖의 비밀번호 문자열은 못 잡음 |
| MCP 서버 | HTTP 방식은 고정 토큰 하나만 검사(사용자 구분·만료 없음). 입력값 검사는 가짜 GUI로 테스트하지만 실제 클릭·입력이 화면에 반영되는지는 자동 테스트 없음 |
| 미완성 | `runs.py`(백그라운드 실행 이력)는 만들었지만 MCP 도구로 연결하지 않음 |

## 10. 인수인계 체크리스트

- [ ] 2-1의 확인 명령이 모두 정상 출력
- [ ] `ollama list`에 `qwen3.5:4b-q4_K_M`
- [ ] `docker image ls desktop-mcp-sandbox`에 `py312`
- [ ] `.\scripts\check.ps1` 결과 `263 passed, 1 skipped` (심볼릭 링크 권한이 있으면 skipped 없이 모두 passed)
- [ ] 3-2 예시를 직접 실행해 `succeeded` 확인 후 `git branch -D`로 정리
- [ ] 4-2대로 Claude Desktop에 등록하고 도구 12개가 보이는지 확인
