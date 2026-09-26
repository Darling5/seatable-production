# -*- coding: utf-8 -*-
"""application/project_brain/store.py — 本地「第二大脑」记忆库（append-only）。

━━ 为什么用事件流 + 折叠，而不是直接改 JSON ━━
业主的硬要求是「不能覆盖历史」。用「读出来改一改写回去」的 JSON，
历史每一次都被抹掉一次，事后根本没法回答「当时为什么记成 3 号」。
所以这里做成 append-only 事件流：每次写入只是**追加一行**，
当前状态由折叠（fold）算出来。代价是文件会长，收益是历史永不丢失。

━━ 为什么暴露成 adapter 接口 ━━
只要 ``list_rows / append_row / update_row`` 三个方法与 SeaTable 适配器
同名同义，第一期的 ``DataService``（授权 + 幂等 + 读回验证 + 台账）就能
**原样复用**，不需要第二套写入闸门。这是本期「不自建另一套授权机制」
的落地方式。

并发：进程内用可重入锁，跨进程用文件锁（Windows msvcrt / POSIX flock），
再加上行级 ``__expected_version__`` 乐观锁 —— 旧版本写入直接抛
``StaleVersionError``，绝不静默覆盖。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Iterable, Optional

from . import schema as S

LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT = 10.0


class StaleVersionError(RuntimeError):
    """乐观锁失败：调用方持有的版本已过期，拒绝覆盖。"""

    def __init__(self, table: str, row_id: str, expected: Any, actual: Any):
        super().__init__("版本冲突：表「%s」行 %s 期望版本 %s，实际 %s"
                         % (table, row_id, expected, actual))
        self.table = table
        self.row_id = row_id
        self.expected = expected
        self.actual = actual


class _FileLock:
    """跨进程文件锁；获取不到时轮询等待，超时抛 TimeoutError。"""

    def __init__(self, path: str, timeout: float = LOCK_TIMEOUT):
        self.path = path
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fh = open(self.path, "a+b")
        deadline = time.time() + self.timeout
        while True:
            try:
                self._lock()
                return self
            except OSError:
                if time.time() > deadline:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError("获取文件锁超时：%s" % self.path)
                time.sleep(0.02)

    def _lock(self) -> None:
        if os.name == "nt":
            import msvcrt
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def __exit__(self, *exc):
        if self._fh is not None:
            self._unlock()
            self._fh.close()
            self._fh = None
        return False


class LocalMemoryAdapter:
    """本地 JSONL 记忆库，接口与 SeaTable 适配器一致。

    行标识：业务代码通过 ``__row_id__`` 自带实体 ID（如 ACT-20260926-ab12），
    这样 ID 与业务编号一一对应，不会出现「表里第 7 行」这种易失标识。
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._rlock = threading.RLock()
        self._seq_cache: dict[str, int] = {}

    # ── 生命周期 ───────────────────────────────────────
    def auth(self) -> None:      # 本地库无需鉴权，保持接口一致
        return None

    # ── 路径 ───────────────────────────────────────────
    def path_of(self, table: str) -> str:
        fn = S.TABLE_FILES.get(table)
        if not fn:
            raise KeyError("未知本地记忆表：%r（合法：%s）"
                           % (table, "|".join(S.TABLE_NAMES)))
        return os.path.join(self.root, fn)

    def _lock(self, table: str) -> _FileLock:
        return _FileLock(self.path_of(table) + LOCK_SUFFIX)

    # ── 读原始事件（含历史，绝不被折叠吃掉）────────────
    def read_events(self, table: str) -> list[dict]:
        path = self.path_of(table)
        if not os.path.exists(path):
            return []
        out: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue          # 坏行跳过，不影响其余历史
                if isinstance(rec, dict):
                    out.append(rec)
        return out

    def _next_seq(self, table: str) -> int:
        """单调递增序号；用于同毫秒内的稳定排序，不承担并发控制职责。"""
        n = self._seq_cache.get(table)
        if n is None:
            n = len(self.read_events(table))
        n += 1
        self._seq_cache[table] = n
        return n

    # ── 折叠：事件流 → 当前状态 ─────────────────────────
    @staticmethod
    def fold(events: Iterable[dict]) -> dict[str, dict]:
        """按 row_id 聚合事件；append 覆盖、update 合并、delete 打墓碑。

        ``__version__`` 取该行已发生的事件数（从 1 开始），
        因此「版本」天然等于「这一行被写过几次」，无需额外计数文件。
        """
        rows: dict[str, dict] = {}
        versions: dict[str, int] = {}
        for rec in sorted(events, key=lambda r: r.get(S.F_SEQ, 0)):
            rid = rec.get(S.F_ROW_ID)
            if not rid:
                continue
            rid = str(rid)
            op = rec.get(S.F_OP, "append")
            versions[rid] = versions.get(rid, 0) + 1
            if op == "delete":
                rows.pop(rid, None)
                continue
            data = {k: v for k, v in rec.items()
                    if not S.is_internal(k)}
            if op == "append" or rid not in rows:
                rows[rid] = dict(data)
            else:
                rows[rid].update(data)
        out: dict[str, dict] = {}
        for rid, row in rows.items():
            rec = dict(row)
            rec[S.F_ROW_ID] = rid
            rec[S.F_VERSION] = versions.get(rid, 1)
            out[rid] = rec
        return out

    # ── adapter 接口 ───────────────────────────────────
    def list_rows(self, table: str) -> list[dict]:
        return [dict(r) for r in self.fold(self.read_events(table)).values()]

    def get_row(self, table: str, row_id: str) -> Optional[dict]:
        return self.fold(self.read_events(table)).get(str(row_id))

    def version_of(self, table: str, row_id: str) -> Optional[int]:
        row = self.get_row(table, row_id)
        return int(row[S.F_VERSION]) if row else None

    def append_row(self, table: str, data: dict) -> str:
        data = dict(data)
        rid = str(data.pop(S.F_ROW_ID, "") or "").strip()
        if not rid:
            raise ValueError("append_row 需要显式 __row_id__（本地记忆库以业务 ID 为主键）")
        with self._rlock, self._lock(table):
            if self.get_row(table, rid) is not None:
                # 同一 ID 二次 append 视为同一实体的新版本，而不是新建一行
                raise StaleVersionError(table, rid, "不存在", "已存在")
            self._append_event(table, rid, data, op="append")
        return rid

    def update_row(self, table: str, row_id: str, data: dict) -> None:
        data = dict(data)
        expected = data.pop(S.F_EXPECTED_VERSION, None)
        rid = str(row_id)
        with self._rlock, self._lock(table):
            row = self.get_row(table, rid)
            if row is None:
                raise KeyError("行不存在：表「%s」%s" % (table, rid))
            actual = int(row[S.F_VERSION])
            if expected is not None and int(expected) != actual:
                raise StaleVersionError(table, rid, expected, actual)
            self._append_event(table, rid, data, op="update")
        return None

    def delete_rows(self, table: str, row_ids: list) -> None:
        """逻辑删除（打墓碑），物理历史保留 —— 「不能覆盖历史」同样适用于删除。"""
        with self._rlock, self._lock(table):
            for rid in row_ids:
                if self.get_row(table, str(rid)) is None:
                    continue
                self._append_event(table, str(rid), {}, op="delete")

    def get_metadata(self, table: str) -> dict:
        rows = self.list_rows(table)
        cols: list[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        return {"table_name": table,
                "columns": [{"name": c, "type": "text"} for c in cols
                            if not S.is_internal(c)]}

    # ── 关联（本地库用不到，保持接口形状）──────────────
    def link(self, table: str, other_table: str, link_id: str,
             row_id: str, other_row_ids: list) -> None:
        return None

    def list_linked(self, table: str, row_id: str, link_id: str) -> list:
        return []

    def query(self, table: str, filters: dict | None = None) -> list[dict]:
        rows = self.list_rows(table)
        if not filters:
            return rows
        out = []
        for r in rows:
            if all(str(r.get(k, "")) == str(v) for k, v in filters.items()):
                out.append(r)
        return out

    # ── 内部 ───────────────────────────────────────────
    def _append_event(self, table: str, rid: str, data: dict, op: str) -> None:
        import datetime as _dt
        payload = {k: v for k, v in data.items() if not S.is_internal(k)}
        payload[S.F_ROW_ID] = rid
        payload[S.F_OP] = op
        payload[S.F_SEQ] = self._next_seq(table)
        payload[S.F_AT] = _dt.datetime.now().isoformat(timespec="seconds")
        path = self.path_of(table)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


__all__ = ["LocalMemoryAdapter", "StaleVersionError"]
