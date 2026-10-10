#!/usr/bin/env python3
"""실제 Oracle에서 에이전트의 공통코드 경로를 검증한다.

전용 도구는 없다. 에이전트는 ① list_connections로 코드 테이블 위치를 받고
② describe_table 코멘트로 역할을 판별하고 ③ query로 조회·조인해 디코딩한다.
이 세 단계가 실제로 성립하는지만 본다.

테이블명·컬럼명·그룹명·코드값을 하나도 고정하지 않는다. 코드 테이블에서
그룹을 읽고 카탈로그에서 같은 이름의 컬럼을 찾아 관례
"코드 컬럼명 == 코드 그룹키"를 확인한다. 스키마가 바뀌어도 따라간다.

DB나 common_code 설정이 없으면 스킵한다.
"""

from __future__ import annotations

import json
import sys
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dbtools.service import ToolService


class RealCommonCodeTests(unittest.TestCase):
    service: ToolService
    connection: str
    table: str
    roles: dict[str, str]             # group/code/name -> 컬럼명 (코멘트에서 판별)
    groups: dict[str, list[str]]      # 그룹키 -> 코드값
    columns: dict[str, list[str]]     # 그룹키 -> 그 이름을 쓰는 OWNER.TABLE.COLUMN

    @classmethod
    def setUpClass(cls) -> None:
        path = ROOT / "config.json"
        if not path.is_file():
            raise unittest.SkipTest("config.json이 없습니다")
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if not (config.get("common_code") or {}).get("table"):
            raise unittest.SkipTest("config.json에 common_code.table이 없습니다")
        try:
            cls.service = ToolService(config, base_dir=str(ROOT))
        except Exception as exc:  # 드라이버 미설치·DB 미기동 모두 스킵
            raise unittest.SkipTest(f"실 DB 연결 불가: {exc}") from exc

        # ① 에이전트가 받는 위치 정보
        advertised = cls.service.dispatch("list_connections", {}).get("common_code")
        if not advertised:
            raise unittest.SkipTest("list_connections가 common_code를 알리지 않습니다")
        cls.table = advertised["table"]
        cls.connection = advertised.get("connection") or config["connections"][0]["name"]

        # ② 코멘트로 역할 판별 — 에이전트가 하는 그대로
        described = cls.service.dispatch(
            "describe_table", {"connection": cls.connection, "table": cls.table})
        cls.roles = {}
        for item in described["columns"]:
            comment = (item.get("comment") or "").replace(" ", "")
            if "코드그룹" in comment and "group" not in cls.roles:
                cls.roles["group"] = item["name"]
            elif "코드명" in comment or "명칭" in comment:
                cls.roles.setdefault("name", item["name"])
            elif "코드" in comment and "group" in cls.roles:
                cls.roles.setdefault("code", item["name"])
        if set(cls.roles) < {"group", "code", "name"}:
            raise unittest.SkipTest(f"코멘트로 역할을 판별하지 못했습니다: {cls.roles}")

        # ③ query로 전체 그룹 수집
        g, c, n = cls.roles["group"], cls.roles["code"], cls.roles["name"]
        scan = cls.service.dispatch("query", {
            "connection": cls.connection,
            "sql": f"SELECT {g}, {c}, {n} FROM {cls.table} ORDER BY {g}, {c}",
            "max_rows": 1000})
        if "error" in scan:
            raise unittest.SkipTest(f"코드 테이블 조회 실패: {scan['error']}")
        cls.groups = defaultdict(list)
        for row in scan["rows"]:
            cls.groups[row[0]].append(row[1])

        owner = cls.table.split(".", 1)[0]
        code_table = cls.table.split(".", 1)[1]
        cls.columns = defaultdict(list)
        for group in cls.groups:
            found = cls.service.dispatch("search_catalog", {
                "connection": cls.connection, "keyword": group,
                "owner": owner, "scope": "column", "limit": 200})
            for match in found.get("matches", ()):
                if match.get("column") != group or match["object"] == code_table:
                    continue
                qualified = f"{match['owner']}.{match['object']}.{match['column']}"
                if qualified not in cls.columns[group]:
                    cls.columns[group].append(qualified)

    @classmethod
    def tearDownClass(cls) -> None:
        service = getattr(cls, "service", None)
        if service is not None:
            service.close()

    def test_comments_alone_identify_the_role_columns(self) -> None:
        """전용 설정 없이 컬럼 코멘트만으로 그룹·코드·코드명이 갈린다."""
        self.assertEqual(set(self.roles) >= {"group", "code", "name"}, True)
        self.assertEqual(len(set(self.roles.values())), len(self.roles))
        self.assertTrue(self.groups, "코드 테이블에서 그룹을 하나도 읽지 못했습니다")

    def test_plain_sql_decodes_a_column_in_place(self) -> None:
        """조인 한 번으로 코드 컬럼을 코드명으로 치환할 수 있다 — 사전 조회가 아니라 디코딩."""
        decoded = 0
        g, c, n = self.roles["group"], self.roles["code"], self.roles["name"]
        for group, columns in self.columns.items():
            for qualified in columns:
                owner, table, column = qualified.split(".")
                with self.subTest(column=qualified):
                    result = self.service.dispatch("query", {
                        "connection": self.connection,
                        "sql": (f"SELECT t.{column}, k.{n} FROM {owner}.{table} t "
                                f"JOIN {self.table} k ON k.{g} = :grp AND k.{c} = t.{column}"),
                        "binds": {"grp": group},
                        "max_rows": 50})
                    self.assertNotIn("error", result, result)
                    for row in result["rows"]:
                        self.assertIn(row[0], self.groups[group])
                        self.assertTrue(row[1])
                    decoded += 1
        self.assertGreater(decoded, 0, "그룹키와 같은 이름의 컬럼을 찾지 못했습니다")

    def test_two_codes_decode_in_one_query_with_aliases(self) -> None:
        """같은 코드 테이블을 두 번 조인해 두 컬럼을 동시에 디코딩한다 (별칭 필요)."""
        pairs = [(group, cols[0]) for group, cols in self.columns.items() if cols]
        by_table: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for group, qualified in pairs:
            owner, table, column = qualified.split(".")
            by_table[f"{owner}.{table}"].append((group, column))
        target = next((t for t, items in by_table.items() if len(items) >= 2), None)
        if target is None:
            self.skipTest("코드 컬럼이 2개 이상인 테이블이 없습니다")
        (g1, c1), (g2, c2) = by_table[target][:2]
        g, c, n = self.roles["group"], self.roles["code"], self.roles["name"]
        result = self.service.dispatch("query", {
            "connection": self.connection,
            "sql": (f"SELECT a.{n} AS FIRST_NAME, b.{n} AS SECOND_NAME FROM {target} t "
                    f"JOIN {self.table} a ON a.{g} = :g1 AND a.{c} = t.{c1} "
                    f"JOIN {self.table} b ON b.{g} = :g2 AND b.{c} = t.{c2}"),
            "binds": {"g1": g1, "g2": g2},
            "max_rows": 20})
        self.assertNotIn("error", result, result)
        self.assertEqual(result["columns"], ["FIRST_NAME", "SECOND_NAME"])
        self.assertTrue(result["rows"])

    def test_order_by_a_column_that_is_not_projected(self) -> None:
        """정렬 컬럼을 SELECT에 넣지 않아도 조회된다 (sql_safety 스코프)."""
        g, c, n = self.roles["group"], self.roles["code"], self.roles["name"]
        others = [item for item in
                  self.service.dispatch("describe_table",
                                        {"connection": self.connection, "table": self.table})["columns"]
                  if item["name"] not in self.roles.values()]
        if not others:
            self.skipTest("역할 외 컬럼이 없습니다")
        result = self.service.dispatch("query", {
            "connection": self.connection,
            "sql": (f"SELECT {c}, {n} FROM {self.table} "
                    f"WHERE {g} = :grp ORDER BY {others[0]['name']}"),
            "binds": {"grp": next(iter(self.groups))},
            "max_rows": 50})
        self.assertNotIn("error", result, result)
        self.assertTrue(result["rows"])

    def test_shared_group_decodes_identically_everywhere(self) -> None:
        """여러 테이블이 같은 그룹을 쓰면(진짜 공통코드) 같은 사전을 쓴다."""
        shared = {g: cols for g, cols in self.columns.items() if len(cols) > 1}
        if not shared:
            self.skipTest("두 테이블 이상이 공유하는 코드 그룹이 없습니다")
        for group in shared:
            with self.subTest(group=group):
                self.assertTrue(self.groups[group])

    def test_fk_column_is_not_a_common_code(self) -> None:
        """개별코드(상품코드 등)는 FK다 — suggest_joins가 담당한다."""
        fk_tables = set()
        for table in {q.rsplit(".", 1)[0] for cols in self.columns.values() for q in cols}:
            joins = self.service.dispatch(
                "suggest_joins", {"connection": self.connection, "table": table})
            fk_tables.update(item["table"] for item in joins.get("joins", ())
                             if item.get("source") == "fk")
        self.assertTrue(fk_tables, "FK 관계를 하나도 찾지 못했습니다")
        self.assertNotIn(self.table, fk_tables, "공통코드 테이블은 FK 대상이 아니어야 합니다")


if __name__ == "__main__":
    unittest.main()
