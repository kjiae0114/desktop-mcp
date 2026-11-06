# CLAUDE.md

Claude Code가 이 저장소에서 작업할 때 보는 안내입니다. 사람용 설명은 `README.md`에 있습니다. 응답과 커밋 메시지는 한국어로 씁니다.

## 개요

Windows 전용 개인 도구입니다. 서로 호출하지 않는 프로그램 두 개로 되어 있습니다.

- `dev_automation/`: 로컬 Ollama 모델용 코딩 에이전트입니다. 격리된 워크트리와 Docker 테스트를 거쳐 브랜치에 커밋합니다.
- `desktop_mcp.py`: FastMCP 서버입니다. 화면 캡처, 클릭, 키 입력, `workspace/` 파일 접근을 허용 목록 안에서만 제공합니다.

## 실행과 테스트

```powershell
.\scripts\check.ps1                 # 전체 테스트 (2~3분, Docker 없으면 Docker 테스트 skip)
.\scripts\check.ps1 -Setup -Image   # venv 생성 + pip 설치 + Docker 이미지 빌드 + 테스트
.\.venv-new\Scripts\python.exe -m pytest tests/test_desktop_mcp.py   # 일부만
.\.venv-new\Scripts\python.exe desktop_mcp.py          # MCP 서버 (stdio)
.\.venv-new\Scripts\python.exe desktop_mcp.py --http   # MCP 서버 (http://127.0.0.1:8765/mcp)
.\.venv-new\Scripts\python.exe -m dev_automation agent desktop-mcp "<작업>" --test <잠글 테스트>
```

- 가상환경은 `.venv-new`(Python 3.12)를 씁니다. `.venv`는 쓰지 않는 Python 3.8 환경입니다.
- `pytest.ini`가 `tests/`만 수집합니다. `runs/`에는 에이전트 워크트리(저장소 전체 사본)가 생기므로 수집하면 중복 오류가 납니다.

## 구조

| 경로 | 역할 |
|---|---|
| `desktop_mcp.py` | MCP 도구 12개. GUI는 `_gui()`, 클립보드는 `_clipboard_copy()`로만 접근 (테스트에서 교체) |
| `coordinate_picker.py` | 클릭 좌표를 고르는 tkinter 창 (테스트 없음) |
| `dev_automation/__main__.py` | CLI (`agent` 하위 명령) |
| `dev_automation/agent.py` | 워크트리 → 기준 테스트 → 모델 루프 → 최종 테스트 → 비밀값·잠금 검사 → 커밋 |
| `dev_automation/agent_tools.py` | 모델용 파일 도구와 차단 경로 |
| `dev_automation/llm.py` | Ollama 클라이언트 (localhost만) |
| `dev_automation/sandbox.py` | Docker 격리 테스트 실행 |
| `dev_automation/runner.py` | 허용된 명령만 실행, 시간·로그 제한 |
| `dev_automation/projects.py` | `config/projects.json` 검증, `COMMAND_PROFILES` |
| `dev_automation/paths.py` | 경로 탈출 차단 |
| `dev_automation/runs.py` | 백그라운드 실행 이력 (미연결) |
| `tests/` | pytest. `test_desktop_mcp.py`는 가짜 GUI와 실제 stdio 서버 기동 |
| `sandbox/python.Dockerfile` | 테스트 이미지 `desktop-mcp-sandbox:py312` |

## 지켜야 할 규칙

- 새 기능도 허용 목록 방식으로 만듭니다. 임의 명령, 임의 경로, 셸 실행(`shell=True`)은 추가하지 않습니다.
- MCP 도구를 추가하거나 이름을 바꾸면 `tests/test_desktop_mcp.py::test_registered_tools`, 개수 확인(`len(names) == 12`), `tests/test_runner.py::test_existing_desktop_mcp_is_unaffected`를 함께 고칩니다.
- `desktop_mcp.py`를 import할 때 화면이 필요하면 안 됩니다. Docker 테스트 이미지에는 화면이 없습니다. 그래서 `pyautogui`는 모듈 맨 위에서 import하지 않습니다.
- stdio 모드에서는 stdout이 MCP 통신에 쓰입니다. 서버 코드에서 `print`를 쓰지 않습니다.
- 에이전트는 push와 병합을 하지 않습니다. 이 규칙을 바꾸지 않습니다.
- `config/projects.json`에 PC 절대 경로가 들어가면 공개 저장소에 올리지 않습니다.

## 남은 할 일 (TODO)

- [ ] `dev_automation/runs.py`(백그라운드 실행 이력·로그·취소)를 MCP 도구나 CLI에 연결합니다. 지금은 테스트만 있고 호출하는 곳이 없습니다.
- [ ] HTTP 모드에 인증이 없습니다. 토큰 헤더를 검사하거나 stdio만 쓰도록 정리합니다.
- [ ] `coordinate_picker.py`에는 테스트가 없습니다. 좌표 변환 부분을 함수로 분리하면 테스트할 수 있습니다.
- [ ] 실제 클릭·입력이 화면에 반영되는지는 사람이 확인합니다(자동 E2E 없음).
- [ ] Claude Desktop 등록(README 4-2)을 실제 앱에서 확인한 뒤 README에 결과를 적습니다.
- [ ] 정리: 쓰지 않는 `.venv`, 오래된 `agent/*` 브랜치와 `runs/agent/*` 폴더, `desktop_mcp.py.*.bak`, 루트의 빈 파일 `update.finished`를 지웁니다.
- [ ] 비밀값 검사 패턴이 GitHub 토큰, AWS 키, 개인키, `sk-` 키 네 가지뿐입니다.
