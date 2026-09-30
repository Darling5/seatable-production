# -*- coding: utf-8 -*-
"""生产交付协同助手 — 后端适配器包。

所有数据读写都通过 adapters 完成，SKILL.md 与上层模块不直接接触任何具体存储。
这样同一套领域知识（流程 / 表规则 / 格式 / 分析）可以在多个底座上运行：

  - local    : 本地 CSV（默认，零配置，离线可用）
  - seatable : 你自己的 SeaTable Base（配置驱动，不再写死 token/uuid）
  - 后续      : 飞书多维表格 / 简道云 / 禅道 / 金蝶（在 factory 的注册表里登记即可）

分层（详见 :mod:`adapters.base`）
────────────────────────────────
:mod:`adapters.base`
    三层契约：必修方法（ABC 强制）/ 能力声明（``capabilities()``，声明式、无副作用）/
    可选方法（有通用兜底，拿不到正确答案时抛 ``Unsupported`` 而非静默返回空）。
:mod:`adapters.factory`
    **后端注册表** + 配置加载。新增后端只登记一行，不再改 6 处 ``if backend ==``。
:mod:`adapters.schema`
    领域结构声明：表清单、语义关联、默认值、中立列类型词表。
:mod:`adapters.partdb`
    可选的物料库存后端。⚠️ 它**至今没有**继承 BaseAdapter，是个侧挂集成 ——
    已知的下一阶段工作，不在当前契约内。
"""
from .base import (  # noqa: F401
    BASE_CAPABILITIES, BaseAdapter, Unsupported,
    backend_of, capabilities_of, supports,
)
from .factory import (  # noqa: F401
    BACKENDS, backend_deploy_hooks, backend_info, get_adapter, get_adapters,
    get_backend, get_base_config, list_backends, load_config, register_backend,
)

__all__ = [
    # 契约
    "BaseAdapter", "Unsupported", "BASE_CAPABILITIES",
    "capabilities_of", "supports", "backend_of",
    # 工厂 / 注册表
    "get_adapter", "get_adapters", "get_base_config", "load_config",
    "register_backend", "list_backends", "backend_info", "backend_deploy_hooks",
    "get_backend", "BACKENDS",
]
