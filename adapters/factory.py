# -*- coding: utf-8 -*-
"""配置加载 + 适配器工厂。

读取 config.yaml，按 backend 选择实现：
  - local    → LocalAdapter（默认）
  - seatable → SeaTableAdapter（仅在填了 api_token/base_uuid 时启用，否则退回 local）
PartDB 是独立的物料后端，用 get_partdb() 单独取（enabled=false 时返回 None）。
"""
import os
import sys

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _coerce(v: str):
    v = v.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.lower() in ("null", '""', "''", ""):
        return ""
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _minimal_yaml(text: str):
    """只支持本技能 config 用到的「2 空格缩进嵌套字典 + 叶子值」，无列表。"""
    root: dict = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if ":" not in line:
            continue
        key, _, val = line.strip().partition(":")
        key = key.strip()
        val = val.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if val == "":
            node: dict = {}
            parent[key] = node
            stack.append((indent, node))
        else:
            parent[key] = _coerce(val)
    return root


def load_config(path: str = None) -> dict:
    if path is None:
        path = os.path.join(_SKILL_DIR, "config.yaml")
    if not os.path.exists(path):
        # 没有配置文件 → 退回最安全的 local 默认
        if not getattr(load_config, "_hinted", False):
            load_config._hinted = True
            print("[提示] 未发现 config.yaml，使用本地零配置；运行 `python setup.py` 可初始化或切换后端", file=sys.stderr)
        return {"backend": "local", "local": {"data_dir": "data", "format": "csv"}}
    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        with open(path, "r", encoding="utf-8") as f:
            return _minimal_yaml(f.read())


def get_adapter(config: dict = None):
    config = config or load_config()
    backend = (config.get("backend") or "local").lower()
    if backend == "seatable":
        sc = config.get("seatable") or {}
        token = sc.get("api_token") or ""
        uuid = sc.get("base_uuid") or ""
        server = sc.get("server") or "https://cloud.seatable.cn"
        if token and uuid:
            try:
                from .seatable import SeaTableAdapter
                return SeaTableAdapter(token, server, uuid)
            except Exception as e:
                print(f"[warn] SeaTable 初始化失败，退回 local：{e}", file=sys.stderr)
        else:
            print("[warn] 未配置 seatable.api_token/base_uuid，退回 local 模式", file=sys.stderr)
    if backend == "mysql":
        print("[warn] MySQL 适配器尚未实现，退回 local 模式（如需实现请告知）", file=sys.stderr)
    # 默认 / 兜底：本地
    lc = config.get("local") or {}
    data_dir = lc.get("data_dir") or "data"
    if not os.path.isabs(data_dir):
        data_dir = os.path.join(_SKILL_DIR, data_dir)
    from .local import LocalAdapter
    return LocalAdapter(data_dir)


def get_partdb(config: dict = None):
    config = config or load_config()
    pc = config.get("partdb") or {}
    if not pc.get("enabled"):
        return None
    url = pc.get("url") or ""
    token = pc.get("token") or ""
    if not (url and token):
        return None
    try:
        from .partdb import PartDBAdapter
        return PartDBAdapter(url, token)
    except Exception as e:
        print(f"[warn] PartDB 初始化失败：{e}", file=sys.stderr)
        return None
