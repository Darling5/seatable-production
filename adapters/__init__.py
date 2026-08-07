"""生产交付协同助手 — 后端适配器包。

所有数据读写都通过 adapters 完成，SKILL.md 不直接接触任何具体存储。
这样同一套领域知识（流程 / 表规则 / 格式 / 分析）可以在三种后端上运行：
  - local  : 本地 CSV/Excel（默认，零配置，离线可用）
  - seatable: 你自己的 SeaTable Base（配置驱动，不再写死 token/uuid）
  - partdb : 可选的物料库存后端（仅缺料检查用）
"""
from .factory import get_adapter, load_config

__all__ = ["get_adapter", "load_config"]
