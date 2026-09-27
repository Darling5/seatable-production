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
            print("[提示] 未发现 config.yaml，使用本地零配置；运行 `python tools/setup.py` 可初始化或切换后端", file=sys.stderr)
        return {"backend": "local", "local": {"data_dir": "data", "format": "csv"}}
    try:
        import yaml  # type: ignore
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        with open(path, "r", encoding="utf-8") as f:
            return _minimal_yaml(f.read())


def _seatable_bases(config: dict) -> dict:
    """Return normalized named SeaTable Base definitions.

    New configurations may use ``seatable.bases`` (the preferred form), or a
    top-level ``bases`` mapping.  A flat ``seatable.api_token/base_uuid`` block
    remains valid and is exposed as the implicit/default Base.  Values are not
    copied to logs or persisted here; credentials stay in the caller's config.
    """
    sc = config.get("seatable")
    sc = sc if isinstance(sc, dict) else {}
    named = sc.get("bases")
    if not isinstance(named, dict):
        named = config.get("bases")
    if not isinstance(named, dict):
        named = config.get("seatable_bases")
    if not isinstance(named, dict):
        # Also accept ``seatable: {production: {...}, tasks: {...}}`` for
        # concise deployments; reserved legacy keys are not treated as names.
        named = {k: v for k, v in sc.items()
                 if k not in {"api_token", "token", "base_uuid", "uuid", "server", "default_base"}
                 and isinstance(v, dict)}
    result = {}
    for name, value in named.items():
        if not isinstance(value, dict):
            continue
        item = dict(value)
        # Accept the common token/uuid spellings while exposing one stable
        # shape to the rest of the factory.
        if not item.get("api_token") and item.get("token"):
            item["api_token"] = item["token"]
        if not item.get("base_uuid") and item.get("uuid"):
            item["base_uuid"] = item["uuid"]
        result[str(name)] = item
    # Legacy flat configuration is deliberately retained, but named entries
    # win when the same name is present.
    if (sc.get("api_token") or sc.get("base_uuid")) and "default" not in result:
        result["default"] = {k: sc.get(k) for k in ("api_token", "base_uuid", "server")}
    return result


def get_base_config(config: dict = None, base_name: str = None):
    """Resolve one named Base's config without authenticating or doing I/O.

    ``base_name`` defaults to ``seatable.default_base`` then ``production``
    when present, preserving the historical flat config when no named Bases
    are configured.  ``None`` is returned when the requested Base is absent.
    """
    config = load_config() if config is None else config
    if not isinstance(config, dict):
        raise SystemExit("[错误] 配置文件格式不对（顶层应为 key: value）。"
                         "请检查 config.yaml，或运行 python tools/setup.py 重新生成。")
    sc = config.get("seatable") if isinstance(config.get("seatable"), dict) else {}
    bases = _seatable_bases(config)
    if not bases:
        return None
    if base_name is None:
        base_name = sc.get("default_base") or ("production" if "production" in bases else None)
        if base_name is None:
            base_name = "default" if "default" in bases else next(iter(bases))
    selected = bases.get(str(base_name))
    if selected is None:
        return None
    out = dict(selected)
    out["name"] = str(base_name)
    out.setdefault("server", sc.get("server") or "https://cloud.seatable.cn")
    return out


def get_adapters(config: dict = None):
    """Build a mapping of configured named adapters (not authenticated).

    This is useful to callers that need to inspect multiple Bases.  Each
    value is a separate adapter instance, so auth tokens and metadata caches
    can never leak between Bases.
    """
    config = load_config() if config is None else config
    backend = str(config.get("backend", "local") or "local").lower()
    if backend != "seatable":
        return {}
    return {name: get_adapter(config, base_name=name)
            for name in _seatable_bases(config)
            if get_base_config(config, name)}


def _fallback(strict: bool, why: str) -> None:
    """SeaTable 不可用时：默认只告警退回 local；写入路径必须硬失败。"""
    if strict:
        raise RuntimeError("SeaTable 不可用，已拒绝退回 local 以免误写本地库：%s" % why)
    print("[warn] %s，退回 local 模式" % why, file=sys.stderr)


def get_adapter(config: dict = None, base_name: str = None, strict: bool = False):
    """Build an adapter for one named Base.

    ``strict`` is for write paths: when the configuration asks for SeaTable but
    the Base/token is missing or initialisation fails, silently returning a
    LocalAdapter would turn a requested online write into a local write and
    still read back as "verified".  With ``strict=True`` we raise instead, so
    the caller fails closed and reports a blocked/unverified outcome.
    """
    config = load_config() if config is None else config
    if not isinstance(config, dict):
        raise SystemExit("[错误] 配置文件格式不对（顶层应为 key: value）。"
                         "请检查 config.yaml，或运行 python tools/setup.py 重新生成。")
    backend = (config.get("backend") or "local").lower()
    if backend == "seatable":
        sc = config.get("seatable") if isinstance(config.get("seatable"), dict) else {}
        selected = get_base_config(config, base_name)
        if selected is None and base_name is not None and _seatable_bases(config):
            _fallback(strict, "未配置 SeaTable Base「%s」" % base_name)
        if selected:
            token = selected.get("api_token") or ""
            uuid = selected.get("base_uuid") or ""
            server = selected.get("server") or "https://cloud.seatable.cn"
            if token and uuid:
                try:
                    from .seatable import SeaTableAdapter
                    return SeaTableAdapter(token, server, uuid, base_name=selected.get("name"))
                except Exception as e:
                    _fallback(strict, "SeaTable 初始化失败：%s" % e)
            else:
                _fallback(strict, "Base「%s」缺 api_token/base_uuid" %
                          (selected.get("name") or base_name or "default"))
        elif not _seatable_bases(config):
            # Keep the old diagnostic for a flat, empty seatable block.
            _fallback(strict, "未配置 seatable.api_token/base_uuid")
    # 默认 / 兜底：本地
    lc = config.get("local")
    if not isinstance(lc, dict):
        # 配置写坏了（例如 local: 后面跟了字符串而非缩进子项）时，
        # 与其抛 AttributeError，不如说清楚哪写错了。
        if lc is not None:
            print("[warn] config 的 local 段格式不对（应为缩进的 data_dir: ...），"
                  "已退回默认 data/ 目录", file=sys.stderr)
        lc = {}
    data_dir = lc.get("data_dir") or "data"
    if not os.path.isabs(data_dir):
        data_dir = os.path.join(_SKILL_DIR, data_dir)
    from .local import LocalAdapter
    return LocalAdapter(data_dir, config)


def get_partdb(config: dict = None):
    config = load_config() if config is None else config
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
