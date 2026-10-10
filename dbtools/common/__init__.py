"""Shared helpers for tool handlers, split by what they operate on.

- ``params``        입력 검증 (바인드·상한·문자열)
- ``results``       드라이버 출력 해석 (행·컬럼·스칼라·ORA 오류 필드)
- ``catalog_access`` 카탈로그 조회 (오브젝트·컬럼·소유자·목록)
- ``execution``     SQL 실행

이름은 모두 공개다 — 이 모듈들은 ``dbtools.tools`` 가 쓰는 패키지 API이고,
밑줄 이름은 각 모듈 내부 전용에만 남긴다.
"""
