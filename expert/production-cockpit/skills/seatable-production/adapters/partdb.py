# -*- coding: utf-8 -*-
"""PartDB 适配器（可选，仅用于缺料检查 / BOM 成本）。

配置驱动，enabled=false 或留空时工厂不会创建它。
API 为 Hydra(JSON-LD)，Token 认证。批量库存不可靠，故缺料计算逐个查零件。
"""
import requests


class PartDBAdapter:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token
        self._h = {"Authorization": f"Token {token}"}

    def search_parts(self, keyword: str, limit: int = 20):
        r = requests.get(f"{self.url}/parts", params={"limit": 500}, headers=self._h, timeout=30)
        r.raise_for_status()
        kw = keyword.lower()
        out = []
        for p in r.json().get("hydra:member", []):
            blob = " ".join(str(p.get(f, "")) for f in ("name", "ipn", "tags", "description")).lower()
            if kw in blob:
                out.append(p)
                if len(out) >= limit:
                    break
        return out

    def get_part(self, part_id: int):
        r = requests.get(f"{self.url}/parts/{part_id}", headers=self._h, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_bom(self, project_id: int):
        items, page = [], 1
        while True:
            r = requests.get(f"{self.url}/projects/{project_id}/bom",
                             params={"limit": 100, "page": page}, headers=self._h, timeout=30)
            r.raise_for_status()
            batch = r.json().get("hydra:member", [])
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    def shortage(self, project_id: int, qty: int):
        """复现 PartDB 前端「生产 N 套 → 缺料」算法。返回缺料清单。"""
        bom = self.get_bom(project_id)
        shortages = []
        for item in bom:
            part = item.get("part", {})
            pid = part.get("id")
            need = int(item.get("quantity", 0)) * qty
            stock = self.get_part(pid).get("total_instock", 0) if pid else 0
            if stock < need:
                shortages.append({
                    "name": part.get("name"), "ipn": part.get("ipn"),
                    "need": need, "stock": stock, "gap": need - stock,
                })
        return shortages
