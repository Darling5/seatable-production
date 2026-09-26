# -*- coding: utf-8 -*-
"""application/decision_support/ports.py — 第二期契约适配层（本期唯一的「跨期假设」聚集地）。

分工写清楚，免得两边各写一套：

  第二期 `project-brain-v1` 负责 **公共契约**：八个统一字段、五类记忆、行动闭环、
  `get_project_context(project_id, snapshot_id)`。那是「人说了什么、谁答应了什么」。
  第三期负责 **计划域算法**：工序、前置关系、工期、日历、资源 ——
  那是「活该怎么排」。两者通过 `project_id` / `plan_id` / `snapshot_id` 对接。

所以本模块只做两件事：
  1. 把第二期的 `ProjectContext` **读**进来（真实模块优先，读不到就用合成的 mock）；
  2. 把它翻译成算法能用的输入，并把翻译过程中的**每一个假设**列出来。

明确不做（避免两边重复建设）：
  · 不再建一套项目 ID 映射 —— 直接用契约里的 `project_id` / `plan_id`；
  · 不再建事实库 —— 事实只在二期；本期需要事实时是**引用**它，不是复制它；
  · 不建审批器 —— 授权只有一个出口：第一期 `application.authorization`。

计划的工序/依赖/资源数据（`PlanningSnapshot`）不在二期契约范围内，
本期先用合成快照驱动算法；字段名与 `adapters/schema.py` 的
「生产工序 / 资源 / 资源分配」保持一致，将来接真实表时只换数据源、不改算法。
"""
from __future__ import annotations

import datetime as _dt
import json
import os

from . import schema as S

# ────────────────────────── 假设清单（全部集中在此）──────────────────────────
ASSUMPTIONS = [
    {
        "id": "A1",
        "about": "公共字段",
        "assumption": "project_id / plan_id / action_id / evidence_id / decision_id / "
                      "run_id / snapshot_id / version 由二期 project-brain-v1 定义，本期只读对齐。",
        "risk": "若二期改字段名，本期需同步；本期不做自动兼容别名。",
    },
    {
        "id": "A2",
        "about": "计划域数据不在二期契约内",
        "assumption": "工序、前置关系、工期、日历、资源属于计划域，二期契约未覆盖，"
                      "本期以 PlanningSnapshot（合成 JSON）提供；字段名与 "
                      "adapters/schema.py 的「生产工序 / 资源 / 资源分配」对齐。",
        "risk": "接真实表时可能需要字段映射，映射只应改在本适配层。",
    },
    {
        "id": "A3",
        "about": "未核实观察不得当输入",
        "assumption": "ProjectContext.observations（未核实观察）**不**作为排程的确定输入；"
                      "只有 facts（已核实）与 commitments 中带明确时间的，才可当条件。",
        "risk": "若把 observation 当事实用，会产出伪确定交期。",
    },
    {
        "id": "A4",
        "about": "来源时效",
        "assumption": "ProjectContext.source_freshness 中标 stale=true 的来源，"
                      "在本期转成「结论待复核」提示，并降低结论的确定性表述。",
        "risk": "忽略时效会让基于旧数据的排程看起来像新结论。",
    },
    {
        "id": "A5",
        "about": "关联键",
        "assumption": "计划快照的 steps[*].plan_id 与二期行动/记忆的 plan_id 同源可比；"
                      "名称（产品名、供应商名）一律不作关联键。",
        "risk": "用名称关联会在改名时静默断链。",
    },
    {
        "id": "A6",
        "about": "写入",
        "assumption": "本期不写入任何业务表，不新增审批器；执行一律走第一期闸门。",
        "risk": "无。",
    },
]


# ────────────────────────── 端口定义 ──────────────────────────
class ProjectContextPort:
    """读第二期 ProjectContext 的端口。实现可换，算法不依赖具体来源。"""

    source = "abstract"

    def get_project_context(self, project_id: str, snapshot_id: str = "") -> dict:
        raise NotImplementedError

    def available(self) -> bool:
        return True


class MockProjectContextPort(ProjectContextPort):
    """合成输入：从本地 JSON 读一份契约结构的上下文。

    存在的意义是让算法**现在就能跑**，而不必等二期交接完成。
    结构严格照 `project-brain-v1` 的返回结构，这样换成真实模块时读法不变。
    """

    source = "mock"

    def __init__(self, data: dict = None, path: str = ""):
        self._data = dict(data or {})
        self.path = path

    @classmethod
    def from_file(cls, path: str) -> "MockProjectContextPort":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f), path=path)

    def get_project_context(self, project_id: str, snapshot_id: str = "") -> dict:
        d = dict(self._data)
        if d.get("project_id") and project_id and d["project_id"] != project_id:
            return {"error": "project_mismatch",
                    "message": "mock 上下文属于 %s，请求的是 %s"
                               % (d["project_id"], project_id)}
        if snapshot_id:
            d["snapshot_id"] = snapshot_id
        return d

    def available(self) -> bool:
        return bool(self._data)


class BrainProjectContextPort(ProjectContextPort):
    """真实端口：优先调用第二期的 `application.project_brain.get_project_context`。

    二期模块尚未合并到本分支时，`available()` 返回 False，
    调用方应回退到 mock —— 而不是让整个流程挂掉。
    """

    source = "project_brain"

    def __init__(self, root: str = ""):
        self.root = root
        self._fn = None
        self._loaded = False
        self._err = ""

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            import sys
            if self.root and self.root not in sys.path:
                sys.path.insert(0, self.root)
            from application.project_brain import get_project_context   # noqa
            self._fn = get_project_context
        except Exception as e:                                          # pragma: no cover
            self._err = "%s: %s" % (type(e).__name__, e)

    def available(self) -> bool:
        self._load()
        return self._fn is not None

    def get_project_context(self, project_id: str, snapshot_id: str = "") -> dict:
        self._load()
        if self._fn is None:
            return {"error": "brain_unavailable", "message": self._err}
        try:
            return self._fn(project_id, snapshot_id) or {}
        except Exception as e:                                          # pragma: no cover
            return {"error": "brain_call_failed", "message": str(e)}


class PlanningSnapshotPort:
    """计划域输入（工序/依赖/日历/资源）端口。本期由合成 JSON 提供。"""

    source = "abstract"

    def get_plan_snapshot(self, project_id: str, snapshot_id: str = "") -> dict:
        raise NotImplementedError


class MockPlanningSnapshotPort(PlanningSnapshotPort):
    source = "mock"

    def __init__(self, data: dict = None, path: str = ""):
        self._data = dict(data or {})
        self.path = path

    @classmethod
    def from_file(cls, path: str) -> "MockPlanningSnapshotPort":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f), path=path)

    def get_plan_snapshot(self, project_id: str, snapshot_id: str = "") -> dict:
        d = dict(self._data)
        if snapshot_id:
            d["snapshot_id"] = snapshot_id
        return d


# ────────────────────────── 上下文 → 算法输入 ──────────────────────────
def pick_default_port(brain_root: str = "", mock_path: str = "") -> ProjectContextPort:
    """按「真实优先、mock 兜底」挑一个端口，并说明挑了谁、为什么。"""
    brain = BrainProjectContextPort(brain_root)
    if brain.available():
        return brain
    if mock_path and os.path.exists(mock_path):
        return MockProjectContextPort.from_file(mock_path)
    return MockProjectContextPort({})


def context_notes(ctx: dict) -> dict:
    """从 ProjectContext 里抽出本期要用的东西：时效、缺口、冲突、证据索引。

    注意：这里**不复制事实内容** —— 只在需要引用时按 evidence_id / memory_id 指回去，
    事实的家永远在二期。
    """
    ctx = dict(ctx or {})
    stale = [s for s in (ctx.get("source_freshness") or []) if s.get("stale")]
    gaps = list(ctx.get("missing_info") or [])
    conflicts = [c for c in (ctx.get("conflicts") or []) if not c.get("resolved")]
    return {
        "contract_version": ctx.get("contract_version") or "",
        "data_as_of": ctx.get("data_as_of") or "",
        "snapshot_id": ctx.get("snapshot_id") or "",
        "stale_sources": stale,
        "missing_info": gaps,
        "unresolved_conflicts": conflicts,
        "evidence_ids": [e.get("evidence_id") for e in (ctx.get("evidence_refs") or [])
                         if e.get("evidence_id")],
        "decision_ids": [d.get("decision_id") for d in (ctx.get("decisions") or [])
                         if d.get("decision_id")],
        "n_observations_unverified": len(ctx.get("observations") or []),
    }


def conditions_from_context(ctx: dict, step_id: str = "") -> list[dict]:
    """把上下文里的「未确认事项」转成算法层面的前置条件。

    两条映射规则（宁可保守）：
      · 未解决的冲突（同一件事有两个说法）→ 按 **unknown** 处理，不放行；
      · 来源过旧（stale）→ 转成 announced 级提示，但不产生硬性时间。
    绝不把 observation（未核实观察）当成 confirmed 事实。
    """
    out: list[dict] = []
    notes = context_notes(ctx)
    for c in notes["unresolved_conflicts"]:
        out.append({
            "kind": S.COND_OTHER,
            "status": S.COND_UNKNOWN,
            "available_at": "",
            "ref": c.get("conflict_id") or "",
            "note": "来源冲突未解决：%s 有多个取值 %s"
                    % (c.get("aspect_key"), c.get("values")),
        })
    for s in notes["stale_sources"]:
        out.append({
            "kind": S.COND_OTHER,
            "status": S.COND_ANNOUNCED,
            "available_at": "",
            "ref": "%s/%s" % (s.get("source_system"), s.get("source_table")),
            "note": "来源 %s.%s 已 %s 小时未更新（阈值 %s），结论需复核"
                    % (s.get("source_system"), s.get("source_table"),
                       s.get("age_hours"), s.get("stale_after_hours")),
        })
    return out


def freshness_caveats(ctx: dict) -> list[dict]:
    """时效提示：直接交给结论层展示，说明「这条结论建立在多旧的数据上」。"""
    notes = context_notes(ctx)
    out = []
    for s in notes["stale_sources"]:
        out.append({
            "code": S.GAP_STALE_SNAPSHOT,
            "message": "来源 %s/%s 数据已 %s 小时未更新（阈值 %s 小时），"
                       "本结论可能基于过期数据"
                       % (s.get("source_system"), s.get("source_table"),
                          s.get("age_hours"), s.get("stale_after_hours")),
            "ref": s.get("source_system") or "",
        })
    if not notes["contract_version"]:
        out.append({
            "code": "context_absent",
            "message": "没有取到 %s 的 ProjectContext，本次结论未绑定二期的证据与缺口信息"
                       % S.BRAIN_CONTRACT_VERSION,
            "ref": "",
        })
    elif notes["contract_version"] != S.BRAIN_CONTRACT_VERSION:
        out.append({
            "code": "context_version_mismatch",
            "message": "上下文契约版本为 %s，本期对齐的是 %s"
                       % (notes["contract_version"], S.BRAIN_CONTRACT_VERSION),
            "ref": "",
        })
    return out


def build_inputs(project_id: str, *, context_port: ProjectContextPort = None,
                 planning_port: PlanningSnapshotPort = None,
                 snapshot_id: str = "", brain_root: str = "",
                 mock_context_path: str = "", mock_planning_path: str = "") -> dict:
    """组装本期算法的完整输入：计划域快照 + 二期上下文 + 假设与注意事项。"""
    cport = context_port or pick_default_port(brain_root, mock_context_path)
    if planning_port is not None:
        pport = planning_port
    elif mock_planning_path:
        pport = MockPlanningSnapshotPort.from_file(mock_planning_path)
    else:
        pport = MockPlanningSnapshotPort({})

    ctx = cport.get_project_context(project_id, snapshot_id)
    planning = pport.get_plan_snapshot(project_id, snapshot_id)
    notes = context_notes(ctx)
    caveats = freshness_caveats(ctx)

    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id or planning.get("snapshot_id") or notes["snapshot_id"],
        "context_source": cport.source,
        "planning_source": pport.source,
        "data_as_of": notes["data_as_of"] or planning.get("as_of") or "",
        "planning": planning,
        "context": ctx,
        "context_notes": notes,
        "caveats": caveats,
        "assumptions": ASSUMPTIONS,
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


__all__ = ["ASSUMPTIONS", "ProjectContextPort", "MockProjectContextPort",
           "BrainProjectContextPort", "PlanningSnapshotPort",
           "MockPlanningSnapshotPort", "build_inputs", "context_notes",
           "conditions_from_context", "freshness_caveats", "pick_default_port"]
