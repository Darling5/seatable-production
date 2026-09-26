# -*- coding: utf-8 -*-
"""application/decision_support/graph.py — 前置关系图：校验 + 拓扑排序。

前置关系是最容易出错、也最容易掩盖错误的地方：
  · 漏了一条边 → 排出来的计划看着很顺，但那是假的；
  · 多了一条反向边 → 循环依赖，算法要么死循环、要么静默取一个任意顺序；
  · 引用了不存在的工序 → 那条约束被无声丢弃。

所以这里的原则是：**宁可不给结论，也不给一个建立在坏图上的结论**。
任何一条硬错误（自环/悬空/循环）都会让上游的排程函数返回「不可确定」，
而不是吞掉继续算。
"""
from __future__ import annotations

from collections import defaultdict, deque

from . import schema as S


class GraphIssue:
    """一条图上的问题。severity 决定它是否阻断排程。"""

    __slots__ = ("code", "message", "ref", "severity", "detail")

    def __init__(self, code: str, message: str, ref: str = "",
                 severity: str = "error", detail=None):
        self.code = code
        self.message = message
        self.ref = ref
        self.severity = severity          # error = 阻断；warning = 提示
        self.detail = detail or {}

    @property
    def blocking(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message, "ref": self.ref,
             "severity": self.severity,
             "hint": S.GAP_CN.get(self.code, "")}
        if self.detail:
            d["detail"] = self.detail
        return d


class DependencyGraph:
    """有向图：predecessor -> successor（前置 → 后置）。

    边一律用 (pred_id, succ_id) 表示，且**只允许由前置指向后置**。
    同时接受 kind（finish_to_start / start_to_start / finish_to_finish）以便
    将来扩展；本期只实现 finish_to_start（前置做完才能开始）。
    """

    FS = "finish_to_start"

    def __init__(self, node_ids=()):
        self.nodes: list[str] = list(dict.fromkeys(node_ids))
        self._node_set = set(self.nodes)
        self.preds: dict[str, list[str]] = defaultdict(list)
        self.succs: dict[str, list[str]] = defaultdict(list)
        self.edges: list[dict] = []
        self.issues: list[GraphIssue] = []
        self._seen_edges: set[tuple] = set()

    # ── 构造 ──────────────────────────────────────
    def add_node(self, node_id: str) -> None:
        node_id = str(node_id or "").strip()
        if not node_id:
            return
        if node_id not in self._node_set:
            self._node_set.add(node_id)
            self.nodes.append(node_id)

    def add_edge(self, pred: str, succ: str, kind: str = FS,
                 source: str = "") -> None:
        pred, succ = str(pred or "").strip(), str(succ or "").strip()
        if not pred or not succ:
            return
        self.add_node(pred)
        self.add_node(succ)
        if pred == succ:
            self.issues.append(GraphIssue(
                S.GAP_SELF_LOOP,
                "工序 %s 把自己声明为前置（自环）" % pred, ref=pred,
                detail={"step_id": pred, "source": source}))
            return
        key = (pred, succ)
        if key in self._seen_edges:
            self.issues.append(GraphIssue(
                S.GAP_DUP_EDGE,
                "重复声明前置关系 %s → %s" % (pred, succ), ref=pred,
                severity="warning",
                detail={"pred": pred, "succ": succ, "source": source}))
            return
        self._seen_edges.add(key)
        self.preds[succ].append(pred)
        self.succs[pred].append(succ)
        self.edges.append({"pred": pred, "succ": succ, "kind": kind,
                           "source": source})

    def add_missing(self, step_id: str, pred_id: str, source: str = "") -> None:
        """记录一条指向不存在工序的约束。不静默丢弃。"""
        self.issues.append(GraphIssue(
            S.GAP_MISSING_PREDECESSOR,
            "工序 %s 的前置 %s 不存在（该约束将被忽略）"
            % (step_id, pred_id), ref=step_id,
            detail={"step_id": step_id, "missing_pred": pred_id, "source": source}))

    def has_edge(self, pred: str, succ: str) -> bool:
        """该有向边是否真的存在（重复声明/自环不算）。"""
        return (str(pred or "").strip(), str(succ or "").strip()) in self._seen_edges

    # ── 校验 ──────────────────────────────────────
    def validate(self) -> list[GraphIssue]:
        """返回全部问题（含循环）。调用后 self.issues 即完整结论。"""
        self._detect_cycles()
        return list(self.issues)

    def _detect_cycles(self) -> None:
        """Kahn 拓扑；剩余节点即环上节点，再走一条 DFS 取出可视的环路径。"""
        try:
            order = self.topo_order()
        except ValueError:
            order = None
        if order is not None:
            return
        indeg = {n: len([p for p in self.preds.get(n, []) if self.has_edge(p, n)])
                 for n in self.nodes}
        left = [n for n in self.nodes if indeg.get(n, 0) > 0]
        cycle = self._find_cycle(left) or left
        self.issues.append(GraphIssue(
            S.GAP_CYCLE,
            "前置关系存在循环依赖：%s" % " → ".join(cycle + cycle[:1]),
            ref=cycle[0] if cycle else "",
            detail={"cycle": cycle}))

    def _find_cycle(self, candidates):
        """在候选节点里找一条实际环路（DFS 回溯，带访问栈）。"""
        cand = set(candidates)
        state: dict[str, int] = {}     # 0 未访问 / 1 在栈 / 2 完成
        stack: list[str] = []
        found: list[str] = []

        def dfs(n):
            state[n] = 1
            stack.append(n)
            for m in self.succs.get(n, []):
                if m not in cand:
                    continue
                if state.get(m, 0) == 1:
                    i = stack.index(m)
                    found.extend(stack[i:])
                    return True
                if state.get(m, 0) == 0 and dfs(m):
                    return True
            stack.pop()
            state[n] = 2
            return False

        for n in candidates:
            if state.get(n, 0) == 0 and dfs(n):
                return found
        return []

    # ── 拓扑 ──────────────────────────────────────
    def topo_order(self) -> list[str]:
        """Kahn 拓扑序。有环时抛 ValueError（**不返回一个看似正常的顺序**）。

        平手时按节点 id 排序，保证同一份输入的结果可复现。
        """
        indeg = {n: 0 for n in self.nodes}
        for (pred, succ) in self._seen_edges:
            indeg[succ] = indeg.get(succ, 0) + 1
        ready = deque(sorted(n for n in self.nodes if indeg.get(n, 0) == 0))
        out: list[str] = []
        while ready:
            n = ready.popleft()
            out.append(n)
            for m in sorted(self.succs.get(n, [])):
                if not self.has_edge(n, m):
                    continue
                indeg[m] -= 1
                if indeg[m] == 0:
                    ready.append(m)
        if len(out) != len(self.nodes):
            raise ValueError("存在循环依赖，无法拓扑排序")
        return out

    def has_blocking_issue(self) -> bool:
        return any(i.blocking for i in self.issues)

    def issues_dict(self) -> list[dict]:
        return [i.to_dict() for i in self.issues]

    def to_dict(self) -> dict:
        return {
            "nodes": list(self.nodes),
            "edges": [dict(e) for e in self.edges],
            "issues": self.issues_dict(),
        }


__all__ = ["DependencyGraph", "GraphIssue"]
