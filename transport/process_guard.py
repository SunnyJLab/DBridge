"""require_process 게이트: 지정된 프로그램이 떠 있을 때만 실행을 허용한다. 표준 라이브러리만.

기본값은 빈 목록이라 게이트가 꺼져 있다. config의 require_process 에 이름을 넣은
배포본에서만 검사한다."""

from __future__ import annotations

import csv
import subprocess
import sys
from io import StringIO
from pathlib import PurePath


# require_process 키가 없는 config 에 적용되는 기본값. 비어 있으면 게이트를 켜지 않는다.
DEFAULT_REQUIRE_PROCESS: tuple[str, ...] = ()


class RequiredProcessMissing(Exception):
    """require_process에 지정된 프로그램이 실행 중이 아니다."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        super().__init__(f"필수 프로그램이 실행 중이 아닙니다: {', '.join(self.missing)}")


class RequiredProcessCheckFailed(Exception):
    """실행 중인 프로세스 목록을 열거하지 못했다 (tasklist/ps 실패)."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"필수 프로그램 실행 여부를 확인할 수 없습니다: {cause}")


def _running_image_names() -> set[str]:
    """실행 중인 프로세스 이미지명을 소문자 집합으로 한 번 열거한다."""
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            stdin=subprocess.DEVNULL,          # windowed exe는 콘솔이 없어 stdin 상속 시 WinError 6
            creationflags=subprocess.CREATE_NO_WINDOW,  # 콘솔 창 깜빡임 방지
            encoding="oem",                    # tasklist 출력은 OEM 코드페이지
            errors="replace",
            timeout=15,
            check=True,
        ).stdout
        return {row[0].lower() for row in csv.reader(StringIO(out)) if row}
    # POSIX는 개발 편의용: comm은 커널이 15자로 절삭할 수 있다.
    out = subprocess.run(
        ["ps", "-axo", "comm="],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=15,
        check=True,
    ).stdout
    return {
        PurePath(line.strip()).name.lower()
        for line in out.splitlines()
        if line.strip()
    }


def missing_processes(required: str | list[str] | None) -> list[str]:
    """required 중 실행 중이지 않은 이미지명을 원래 표기로 돌려준다 (대소문자 무시).

    None이면 DEFAULT_REQUIRE_PROCESS를 검사한다(기본값이 비어 있어 게이트가 꺼진다).
    빈 목록이면 검사하지 않는다.
    """
    if required is None:
        required = DEFAULT_REQUIRE_PROCESS
    names = [required] if isinstance(required, str) else list(required)
    # config_store.load는 정규화되지 않은 원본 dict를 반환하므로 여기서도 strip한다.
    names = [name.strip() for name in names]
    names = [name for name in names if name]
    if not names:
        return []
    running = _running_image_names()
    return [name for name in names if name.lower() not in running]


__all__ = [
    "DEFAULT_REQUIRE_PROCESS",
    "RequiredProcessCheckFailed",
    "RequiredProcessMissing",
    "missing_processes",
]

