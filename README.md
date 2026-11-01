# desktop-mcp

유료 AI 없이 내 PC의 무료 모델로 코드를 고치는 로컬 개발 자동화 도구입니다. 모든 명령은 프로젝트 폴더에서 실행합니다.

## 준비

- Windows, Python 3.12, Git, [Ollama](https://ollama.com)
- 모델 내려받기: `ollama pull qwen3.5:4b-q4_K_M`
- 패키지 설치: `.\.venv-new\Scripts\python.exe -m pip install -r requirements.txt pytest`

Ollama가 실행 중인지는 `ollama list`로 확인합니다. `ollama` 명령을 찾지 못하면 터미널을 새로 열어 보세요.

## 사용법

약한 모델은 스스로 쓴 테스트와 모순되기 쉽습니다. **테스트를 먼저 직접 쓰고 `--test`로 잠그는 방식을 권장합니다.**

1. 원하는 동작을 테스트 파일로 씁니다. 예: `tests/test_textutil.py`
2. 에이전트를 실행합니다.

   ```powershell
   .\.venv-new\Scripts\python.exe -m dev_automation agent desktop-mcp "dev_automation/textutil.py에 format_duration 함수를 작성하세요." --test tests/test_textutil.py
   ```

3. 성공하면 `agent/...` 브랜치에 커밋됩니다. 결과를 읽어 보고 직접 병합합니다.

   ```powershell
   git diff main..<브랜치>
   git merge <브랜치>
   ```

옵션: `--model 모델이름`, `--max-steps 25`, `--test 경로`(여러 번 지정 가능)

## 작업을 잘 시키는 법

- 한 번에 함수 하나, 파일 하나 수준으로 나눕니다.
- 수정할 파일 경로와 입력·출력 예시를 적습니다.
- 실패하면 작업을 더 잘게 나눠 다시 시킵니다. 큰 설계나 여러 파일에 걸친 수정은 자주 실패합니다.

## 동작 방식

1. `runs/agent/<작업 ID>/worktree`에 별도 Git 워크트리를 만듭니다. 현재 작업 폴더는 건드리지 않으며, 커밋하지 않은 변경은 포함되지 않습니다.
2. 전체 테스트가 통과하는지 먼저 확인합니다.
3. 모델이 파일을 읽고 고칩니다. 모델이 쓸 수 있는 도구는 파일 목록, 읽기, 쓰기, 부분 수정, 테스트 실행뿐입니다.
4. 모델의 말이 아니라 전체 테스트 결과로 성공을 판정합니다.
5. 성공하면 브랜치에 커밋하고, 실패하면 브랜치를 지우고 `final.diff`와 `conversation.json`만 남깁니다. push와 병합은 하지 않습니다.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| Ollama에 연결하지 못함 | 시작 메뉴에서 Ollama를 실행하고 `ollama list` 확인 |
| 작업 전부터 테스트가 실패함 | 현재 커밋의 테스트를 먼저 고칩니다. 잠글 테스트는 커밋하지 않아도 됩니다. |
| 같은 수정을 반복해 중단됨 | 테스트에 모순이 없는지 보고, 작업을 더 잘게 나눕니다. |
| 최대 단계를 넘음 | `runs/agent/<작업 ID>/conversation.json`에서 모델이 막힌 곳을 봅니다. |
| 비밀정보로 보여 커밋 거부 | `final.diff`에서 키나 토큰이 들어갔는지 확인합니다. |
| 응답이 매우 느림 | `ollama ps`에서 GPU 100%인지 확인합니다. 낮으면 더 작은 모델을 씁니다. |
| 모델을 지우고 싶음 | `ollama rm 모델이름` |

## 다른 프로젝트 등록

`config/projects.json`의 `projects`에 항목을 추가합니다. 대상 프로젝트는 Git 저장소이고 pytest 테스트와 가상환경이 있어야 합니다. `path`에 내 PC의 절대 경로를 적었다면 이 파일을 공개 저장소에 올리지 않도록 주의하세요.

```json
"my-app": {
  "path": "C:/경로/my-app",
  "python": ".venv/Scripts/python.exe",
  "default_cwd": ".",
  "commands": ["pytest"],
  "allowed_env": []
}
```

대상 프로젝트의 `.gitignore`에 `__pycache__/`가 있어야 합니다. 실행할 수 있는 명령은 `dev_automation/projects.py`의 `COMMAND_PROFILES`에 정의되어 있습니다.

## 알아 둘 위험

- **격리 없음:** 모델이 쓴 코드는 테스트할 때 내 PC에서 실제로 실행됩니다. 파일 도구의 제한은 이 코드에 적용되지 않으므로, 중요한 자료가 있는 PC에서는 결과를 자주 백업하세요.
- **테스트 약화:** `--test` 없이 실행하면 모델이 테스트를 고쳐 통과시킬 수 있습니다. 경고가 나오면 diff를 꼭 읽으세요.
- **병합 전 검토:** 테스트가 통과해도 요구하지 않은 동작이 섞일 수 있습니다.

## 개발

```powershell
.\.venv-new\Scripts\python.exe -m pytest -q --tb=short
```

| 파일 | 역할 |
|---|---|
| `dev_automation/runner.py` | 허용된 명령 실행, 시간 제한, 로그 |
| `dev_automation/paths.py` | 프로젝트 밖 경로 차단 |
| `dev_automation/projects.py` | 프로젝트 등록과 명령 프로파일 |
| `dev_automation/runs.py` | 백그라운드 실행 이력 (아직 MCP 미연결) |
| `dev_automation/llm.py` | Ollama 호출 |
| `dev_automation/agent_tools.py` | 모델에게 주는 파일 도구 |
| `dev_automation/agent.py` | 에이전트 흐름 |
| `desktop_mcp.py` | 화면·파일 제어 MCP 서버 (에이전트와 별개) |
