"""프로젝트 루트 밖으로 나가지 못하게 경로를 검증한다."""

from pathlib import Path, PureWindowsPath


class PathPolicyError(ValueError):
    """경로가 정책에 맞지 않을 때 발생한다."""


def resolve_within(root: Path, relative_path: str) -> Path:
    """루트 기준 상대 경로를 실제 경로로 바꾸고 루트 안에 있는지 확인한다.

    심볼릭 링크와 정션은 실제 위치 기준으로 판단한다. 검증과 사용 사이에
    경로가 교체되는 경쟁 조건까지 막지는 못한다.
    """
    # 현재 OS와 관계없이 드라이브와 역슬래시 표기를 같은 규칙으로 해석한다.
    pure = PureWindowsPath(relative_path)

    if not relative_path or pure.drive or pure.root:
        raise PathPolicyError(f"프로젝트 기준 상대 경로만 허용됩니다: {relative_path!r}")

    # ':'는 드라이브 표기 외에 NTFS 대체 데이터 스트림에도 쓰인다.
    if ":" in relative_path or ".." in pure.parts:
        raise PathPolicyError(f"허용되지 않은 경로입니다: {relative_path!r}")

    resolved_root = root.resolve(strict=True)

    try:
        candidate = (resolved_root / relative_path).resolve(strict=True)
    except OSError as exc:
        raise PathPolicyError(f"경로가 존재하지 않습니다: {relative_path!r}") from exc

    if not candidate.is_relative_to(resolved_root):
        raise PathPolicyError(f"프로젝트 밖의 경로입니다: {relative_path!r}")

    return candidate
