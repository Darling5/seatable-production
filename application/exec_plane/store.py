# -*- coding: utf-8 -*-
"""application/exec_plane/store.py — 执行平面的本地表适配器。

## 为什么需要这个薄薄一层

`project_brain/store.py::LocalMemoryAdapter` 已经是「append-only JSONL + 折叠出当前行
+ 乐观锁版本」的完整实现，本层**不想也不可能再写一套**。但直接拿它来装执行平面的表
是**跑不通**的，实测症状与根因：

    LocalMemoryAdapter.path_of(table) 里查的是 `project_brain.schema.TABLE_FILES`，
    那张表只有 7 张**记忆**表（projects / messages / evidence / memory / actions /
    action_events / reminders）。执行平面的表名（`物料` / `BOM版本` / `付款单` …）
    不在其中，于是**每一次 list_rows / append_row 都抛 KeyError「未知本地记忆表」**。

也就是说：「复用适配器」这句话在**表名映射**这一处就断了 ——
`LocalMemoryAdapter` 的表名表是**写死在 import 上的**，不是构造参数。

## 修法：只替换「表名 → 文件名」这一张映射

子类只覆盖 `path_of()`，其余（读写、折叠、文件锁、序号、版本）**一个字都不重写**：

    · 表名映射 → 本层 `schema.TABLE_FILES`
    · 未知表仍**抛 KeyError**（fail-loud，不静默落到同名文件上）
    · 其余行为与二期完全一致 → 第一期 `DataService` 能原样复用

将来 L_exec 换成真 ERP / MES 或 SeaTable 表时，**只换这一个类**，
`service.py` 与上层业务代码不用改（`list_rows / get_row / version_of /
append_row / update_row` 与 SeaTable 适配器同名同义）。
"""
from __future__ import annotations

import os

from ..project_brain.store import LocalMemoryAdapter
from . import schema as S


class ExecTableStore(LocalMemoryAdapter):
    """与二期同款的本地 JSONL 适配器，**只把表名映射换成执行平面的那张**。"""

    def path_of(self, table: str) -> str:
        fn = S.TABLE_FILES.get(table)
        if not fn:
            # fail-loud：绝不猜文件名，否则会把行写进一个谁也不知道的表
            raise KeyError("未知执行平面表：%r（合法：%s）"
                           % (table, "|".join(S.TABLE_NAMES)))
        return os.path.join(self.root, fn)


__all__ = ["ExecTableStore"]
