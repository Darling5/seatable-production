#!/usr/bin/env python3
"""幂等新增项目阶段表，并扩展生产计划的样机/试产/量产字段。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.factory import load_config  # noqa: E402
from adapters.schema import TABLE_COLUMNS  # noqa: E402


COLORS = [
    "#F4667C", "#EAA775", "#FBD44A", "#ADDF84", "#89D2EA",
    "#4ECCCB", "#B7CEF9", "#46A1FD", "#9860E5",
]


def _options(names: list[str]) -> dict:
    return {
        "options": [
            {
                "name": name,
                "id": f"{700000 + i:06d}",
                "color": COLORS[i % len(COLORS)],
                "textColor": "#FFFFFF",
            }
            for i, name in enumerate(names)
        ]
    }


class LifecycleMigration:
    def __init__(self, config: dict, apply: bool, cleanup_duplicate_links: bool = False):
        seatable = config.get("seatable") or {}
        self.server = (
            os.environ.get("SEATABLE_SERVER")
            or seatable.get("server")
            or "https://cloud.seatable.cn"
        ).rstrip("/")
        self.api_token = (
            os.environ.get("SEATABLE_API_TOKEN")
            or seatable.get("api_token")
            or ""
        )
        self.uuid = (
            os.environ.get("SEATABLE_BASE_UUID")
            or seatable.get("base_uuid")
            or ""
        )
        if not self.api_token or not self.uuid:
            raise SystemExit("缺少 SeaTable token/base_uuid；请通过 config.yaml 或环境变量提供。")
        self.apply = apply
        self.cleanup_duplicate_links = cleanup_duplicate_links
        self.base_url = ""
        self.headers: dict[str, str] = {}
        self.metadata: dict = {}
        self.actions: list[str] = []

    def connect(self) -> None:
        response = requests.get(
            f"{self.server}/api/v2.1/dtable/app-access-token/",
            headers={"Authorization": f"Bearer {self.api_token}"},
            timeout=20,
        )
        response.raise_for_status()
        token = response.json()
        self.base_url = (
            f"{token['dtable_server'].rstrip('/')}/api/v2/dtables/{self.uuid}"
        )
        self.headers = {"Authorization": f"Bearer {token['access_token']}"}
        self.refresh()

    def refresh(self) -> None:
        response = requests.get(
            f"{self.base_url}/metadata/", headers=self.headers, timeout=30
        )
        response.raise_for_status()
        self.metadata = response.json()["metadata"]

    def table(self, name: str) -> dict | None:
        return next(
            (table for table in self.metadata["tables"] if table["name"] == name),
            None,
        )

    def create_table(self, name: str) -> None:
        if self.table(name):
            return
        first = TABLE_COLUMNS[name][0]
        payload = {
            "table_name": name,
            "columns": [{
                "column_name": first["name"],
                "column_type": first["type"],
            }],
        }
        self.actions.append(f"创建表：{name}")
        if self.apply:
            response = requests.post(
                f"{self.base_url}/tables/",
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            self.refresh()

    def add_column(self, table_name: str, column: dict) -> None:
        if column.get("managed_by_reciprocal"):
            return
        table = self.table(table_name)
        if table and any(c["name"] == column["name"] for c in table["columns"]):
            return
        payload = {
            "table_name": table_name,
            "column_name": column["name"],
            "column_type": column["type"],
        }
        if column.get("options"):
            payload["column_data"] = _options(column["options"])
        if column["type"] == "link":
            payload["column_data"] = {
                "other_table": column["other_table"],
            }
        self.actions.append(
            f"新增列：{table_name}.{column['name']} ({column['type']})"
        )
        if self.apply:
            response = requests.post(
                f"{self.base_url}/columns/",
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            if not response.ok:
                raise RuntimeError(
                    f"新增列失败 {table_name}.{column['name']}: "
                    f"HTTP {response.status_code} {response.text[:500]}"
                )
            self.refresh()

    def rename_column(self, table_name: str, old_name: str, new_name: str) -> None:
        table = self.table(table_name)
        names = {column["name"] for column in table["columns"]}
        if new_name in names or old_name not in names:
            return
        self.actions.append(f"重命名列：{table_name}.{old_name} → {new_name}")
        if self.apply:
            response = requests.put(
                f"{self.base_url}/columns/",
                headers={**self.headers, "Content-Type": "application/json"},
                json={
                    "op_type": "rename_column",
                    "table_name": table_name,
                    "column": old_name,
                    "new_column_name": new_name,
                },
                timeout=30,
            )
            response.raise_for_status()
            self.refresh()

    def cleanup_stage_plan_duplicate(self) -> None:
        if not self.cleanup_duplicate_links:
            return
        table = self.table("项目阶段")
        links = {
            column["name"]: (column.get("data") or {}).get("link_id")
            for column in table["columns"]
            if column.get("type") == "link"
        }
        if not (
            links.get("关联生产计划")
            and links.get("生产计划")
            and links["关联生产计划"] != links["生产计划"]
        ):
            return
        rows = requests.get(
            f"{self.base_url}/rows/",
            headers=self.headers,
            params={"table_name": "项目阶段", "limit": 1},
            timeout=30,
        )
        rows.raise_for_status()
        if rows.json().get("rows"):
            raise RuntimeError("项目阶段表已有数据，拒绝自动删除重复关联列。")
        self.actions.append("删除重复空关联：项目阶段.关联生产计划")
        if self.apply:
            response = requests.delete(
                f"{self.base_url}/columns/",
                headers={**self.headers, "Content-Type": "application/json"},
                json={"table_name": "项目阶段", "column": "关联生产计划"},
                timeout=30,
            )
            response.raise_for_status()
            self.refresh()

    def run(self) -> list[str]:
        self.connect()
        self.cleanup_stage_plan_duplicate()
        self.create_table("项目阶段")
        for column in TABLE_COLUMNS["项目阶段"][1:]:
            self.add_column("项目阶段", column)
        for column in TABLE_COLUMNS["生产计划"]:
            self.add_column("生产计划", column)
        self.rename_column("项目阶段", "生产计划", "关联生产计划")
        return self.actions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际修改 SeaTable；不带此参数时只输出迁移计划。",
    )
    parser.add_argument(
        "--cleanup-duplicate-links",
        action="store_true",
        help="仅用于清理由旧迁移产生、且空表中的重复项目阶段↔生产计划关联。",
    )
    args = parser.parse_args()
    migration = LifecycleMigration(
        load_config(args.config),
        apply=args.apply,
        cleanup_duplicate_links=args.cleanup_duplicate_links,
    )
    actions = migration.run()
    mode = "已执行" if args.apply else "计划"
    print(json.dumps({"mode": mode, "actions": actions}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
