"""에이전트의 '기준 테스트'를 그대로 재현한다.

에이전트는 작업을 시작하기 전에 저장소 전체 테스트를 Docker(Linux) 샌드박스에서 돌리고,
실패하면 '작업 전부터 테스트가 실패합니다'로 멈춘다. Windows CI만으로는 이 실패를 잡지 못하므로
CI(ubuntu)와 로컬에서 같은 경로로 확인한다.

사용: python scripts/sandbox_baseline.py   (먼저 desktop-mcp-sandbox:py312 이미지를 빌드)
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dev_automation.agent import TEST_ARGS  # noqa: E402
from dev_automation.projects import COMMAND_PROFILES  # noqa: E402
from dev_automation.sandbox import SUCCEEDED, DockerSandbox  # noqa: E402

IMAGE = "desktop-mcp-sandbox:py312"


def main() -> int:
    sandbox = DockerSandbox(IMAGE)
    problem = sandbox.check()
    if problem:
        print(problem)
        return 2
    result = sandbox.run_tests(ROOT, ".", COMMAND_PROFILES["pytest"].args, TEST_ARGS, 900)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.error:
        print(result.error, file=sys.stderr)
    return 0 if result.status == SUCCEEDED else 1


if __name__ == "__main__":
    sys.exit(main())
