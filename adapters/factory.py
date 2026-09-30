# -*- coding: utf-8 -*-
"""配置加载 + 适配器工厂（**后端注册表**驱动）。

按 config.yaml 的 ``backend`` 选择实现：
  - local    → LocalAdapter（默认，零配置）
  - seatable → SeaTableAdapter（填了 api_token + base_uuid 才启用，否则退回 local）

新增一个后端（飞书 / 简道云 / 禅道 / 金蝶…）**只需在下面的 BACKENDS 注册表里加一条**：
宣告它叫什么、怎么造、命名实例需要哪些必填键、能不能通过 API 管表结构。

为什么要有注册表：以前 `backend == "seatable"` 这个判断被**硬编码在 6 个地方**
（本模块 2 处 + `tools/deploy.py` 3 处 + `wx/` 两个模块的类名嗅探）。每加一个底座
都要去全仓库搜 `if backend ==`，那不是「可插拔」，那只是「可修改」。注册表把
「系统支持哪些后端」收敛成一处数据，上层只问「是什么后端」，不再问「是不是 SeaTable」。

PartDB 是独立的**物料后端**，不在这条存储分派链上，用 ``get_partdb()`` 单独取
（``enabled: false`` 时返回 None）。它至今没继承 BaseAdapter、也没有多底座抽象 ——
这是已知的下一阶段工作，不在本次改动范围内。
"""
import os
import sys

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

__all__ = [
    "load_config", "get_adapter", "get_adapters", "get_base_config",
    "get_backend", "backend_info", "list_backends", "register_backend", "BACKENDS",
    "backend_deploy_hooks",
]


# ══════════════════════════════════════════════════════════════════
# 后端注册表
# ══════════════════════════════════════════════════════════════════

#: name -> {
#:   label         展示名（错误信息里的人话）
#:   make          make(selected, config) -> adapter；selected 为 None 表示该后端
#:                 不吃「命名实例」（如 local 只有一份）
#:   required_keys 一个可用命名实例的必填键（缺一即视为配置不完整）
#:   key_aliases   {规范键: (可接受的别名…)}，如 api_token ← token
#:   named_bases   是否支持「一个后端下挂多个命名实例」
#:   default_server 该后端的默认服务地址
#: }
BACKENDS: dict = {}


def register_backend(name: str, *, label: str, make, required_keys=(),
                     key_aliases=None, named_bases: bool = False,
                     default_server: str = "", deploy: dict = None) -> None:
    """登记一个后端。重复登记同名会覆盖（便于测试与本地实验）。

    ``deploy`` 声明该后端在**部署流程**里的钩子（``tools/deploy.py`` 消费）：
      · ``verify``    ``(script, argv, timeout)`` 连通性验证（如 dry-run）
      · ``init_sync`` ``(script, argv, timeout)`` 初次「云端 → 本地」同步
    以前这两件事由 ``tools/deploy.py`` 里的 ``if backend == "seatable"`` 写死，
    于是新后端在部署流程里会被**静默跳过验证** —— 声明式之后，漏配是看得见的
    （部署报告会明确写「该后端未声明验证步骤」）。
    """
    BACKENDS[str(name).lower()] = {
        "label": label,
        "make": make,
        "required_keys": tuple(required_keys),
        "key_aliases": dict(key_aliases or {}),
        "named_bases": bool(named_bases),
        "default_server": default_server,
        "deploy": dict(deploy or {}),
    }


def backend_deploy_hooks(backend: str) -> dict:
    """某后端声明的部署钩子；未登记或未声明时返回空 dict（**不猜**）。"""
    info = BACKENDS.get(str(backend or "").lower())
    return dict(info.get("deploy") or {}) if info else {}


def list_backends():
    """已登记的后端名（排序后，便于错误信息里稳定展示）。"""
    return sorted(BACKENDS)


def backend_info(config: dict = None):
    """返回 (后端名, 注册表条目)；未登记的后端返回 (名字, None)。

    只读 config 的 ``backend`` 字段，不做任何 I/O。上层想知道「当前是什么后端」
    就该用这个（或适配器上的 ``backend`` 属性），而不是 ``isinstance`` / 类名嗅探。
    """
    config = load_config() if config is None else config
    name = str((config or {}).get("backend") or "local").strip().lower()
    return name, BACKENDS.get(name)


def get_backend(config: dict = None) -> str:
    """当前配置声明的后端名（小写）。只读 config，不做 I/O、不建连。"""
    return backend_info(config)[0]


# ── 各后端的构造实现 ────────────────────────────────────────────

def _make_local(_selected, config: dict):
    from .local import LocalAdapter
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
    return LocalAdapter(data_dir, config)


def _make_seatable(selected, _config: dict):
    from .seatable import SeaTableAdapter
    return SeaTableAdapter(
        selected["api_token"],
        selected.get("server") or BACKENDS["seatable"]["default_server"],
        selected["base_uuid"],
        base_name=selected.get("name"),
    )


def _make_feishu(selected, config: dict):
    """构造飞书适配器。

    **两种传输层**（详见 adapters/feishu.py 的模块 docstring）：
      · cli（默认）—— 走 lark-cli 子进程，复用其登录态，不需要 app_secret
      · api        —— 直连 open.feishu.cn，需要 app_id + app_secret

    ``app_id`` / ``app_secret`` 是**应用级**凭据（一个应用可访问它名下的所有 Base），
    所以写在 ``feishu.app_id`` / ``feishu.app_secret``，而不是每个命名实例里重复填。
    ``cli_path`` / ``identity`` 同理写在 ``feishu`` 段。
    """
    from .feishu import FeishuAdapter
    fc = config.get("feishu") if isinstance(config.get("feishu"), dict) else {}
    return FeishuAdapter(
        selected["base_token"],
        cli_path=fc.get("cli_path") or selected.get("cli_path") or "",
        identity=fc.get("identity") or "",
        app_id=fc.get("app_id") or "",
        app_secret=fc.get("app_secret") or "",
        transport=fc.get("transport") or "",
        server=selected.get("server") or "",
        base_name=selected.get("name"),
    )


register_backend("local", label="本地 CSV", make=_make_local)

register_backend(
    "seatable",
    label="SeaTable",
    make=_make_seatable,
    required_keys=("api_token", "base_uuid"),
    key_aliases={"api_token": ("token",), "base_uuid": ("uuid",)},
    named_bases=True,
    default_server="https://cloud.seatable.cn",
    deploy={
        "verify": ("seatable_sync.py", ["--dry-run"], 120),
        "init_sync": ("seatable_sync.py", [], 900),
    },
)

register_backend(
    "feishu",
    label="飞书多维表格",
    make=_make_feishu,
    required_keys=("base_token",),
    # Base 在飞书里的正式叫法是 app_token；早期文档/URL 里叫 base_token。
    # token 与 base 也都是「同一个东西」的常见口头叫法 —— 三种都收，对外只暴露 base_token。
    key_aliases={"base_token": ("app_token", "token", "base")},
    named_bases=True,
    default_server="https://open.feishu.cn",
    # verify 钩子只跑**只读**验证（认证 / 表清单 / 字段 / 拉记录 / 单行读取），
    # 部署流程不会因此往生产 Base 里建任何东西。
    # init_sync（云端 → 本地初次同步）尚未落地，故不声明 ——
    # 部署报告会明确写「该后端未声明初始化步骤」，比让它静默跳过要好。
    deploy={"verify": ("verify_feishu.py", ["--read-only"], 180)},
)


# ══════════════════════════════════════════════════════════════════
# 配置读取
# ══════════════════════════════════════════════════════════════════

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
    """只支持本技能 config 用到的「2 空格缩进嵌套字典 + 叶子值」，**无列表**。

    ⚠️ 已知短板（未修，需要时再上真正的 YAML 解析器）：本函数遇到 YAML 列表会
    **静默丢弃**。例如 config 里写了

        watch_groups:
          - 群A
          - 群B

    这里会解析成 ``{}``，130+ 个群名凭空消失且不报错。PyYAML 缺失时才会走到
    本函数（`load_config` 优先用 yaml），本机装了 PyYAML，所以现状不触发。
    真要修：遇到 ``- `` 开头行时抛错而不是 continue —— 「解析不了」必须比
    「解析成空」安全。
    """
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


def _named_bases(config: dict, backend: str) -> dict:
    """返回某后端下**归一化后**的命名实例映射 {名字: 配置字典}。

    兼容四种书写形态（按优先级）：
      ① ``<backend>.bases: {名: {...}}``   —— 推荐
      ② 顶层 ``bases: {名: {...}}``
      ③ 顶层 ``<backend>_bases: {...}``
      ④ ``<backend>: {名: {...}}``         —— 简写；与保留键同名的键不算实例名
    另外，只填了扁平必备键（如 ``seatable.api_token/base_uuid``）而没有命名实例时，
    会暴露成一个名为 ``default`` 的隐式实例（**命名实例优先，不被覆盖**）。

    实现是通用的：以前这套逻辑写死在 ``_seatable_bases()`` 里，换个后端就得抄一遍。
    凭证只在此处复制与归一，**不**写日志、不落盘。
    """
    info = BACKENDS.get(backend)
    if info is None or not info["named_bases"]:
        return {}
    section = config.get(backend)
    section = section if isinstance(section, dict) else {}
    named = section.get("bases")
    if not isinstance(named, dict):
        named = config.get("bases")
    if not isinstance(named, dict):
        named = config.get("%s_bases" % backend)
    if not isinstance(named, dict):
        reserved = {"server", "default_base", "enabled"} | set(info["required_keys"])
        for aliases in info["key_aliases"].values():
            reserved |= set(aliases)
        named = {k: v for k, v in section.items()
                 if k not in reserved and isinstance(v, dict)}
    result = {}
    for name, value in named.items():
        if not isinstance(value, dict):
            continue
        item = dict(value)
        # 接受同一件的多种写法，但对外只暴露一种稳定形状。
        for canon, aliases in info["key_aliases"].items():
            if item.get(canon):
                continue
            for alias in aliases:
                if item.get(alias):
                    item[canon] = item[alias]
                    break
        result[str(name)] = item
    if "default" not in result and any(section.get(k) for k in info["required_keys"]):
        implicit = {k: section.get(k) for k in info["required_keys"]}
        implicit["server"] = section.get("server")
        result["default"] = implicit
    return result


def get_base_config(config: dict = None, base_name: str = None, backend: str = None):
    """解析某后端下一个命名实例的配置。**不做 I/O、不认证。**

    ``base_name`` 缺省时按 ``<backend>.default_base`` → ``production`` → ``default``
    → 第一个实例 的顺序回落；请求的实例不存在时返回 ``None``（不抛错，交由调用方决定）。
    """
    config = load_config() if config is None else config
    if not isinstance(config, dict):
        raise SystemExit("[错误] 配置文件格式不对（顶层应为 key: value）。"
                         "请检查 config.yaml，或运行 python tools/setup.py 重新生成。")
    if backend is None:
        backend = str(config.get("backend") or "local").strip().lower()
    info = BACKENDS.get(backend)
    if info is None or not info["named_bases"]:
        return None
    section = config.get(backend)
    section = section if isinstance(section, dict) else {}
    bases = _named_bases(config, backend)
    if not bases:
        return None
    if base_name is None:
        base_name = section.get("default_base") or ("production" if "production" in bases else None)
        if base_name is None:
            base_name = "default" if "default" in bases else next(iter(bases))
    selected = bases.get(str(base_name))
    if selected is None:
        return None
    out = dict(selected)
    out["name"] = str(base_name)
    if not out.get("server"):
        out["server"] = section.get("server") or info["default_server"]
    return out


def get_adapters(config: dict = None):
    """构造当前后端下**全部**命名实例的映射（未认证）。

    调用方靠它同时看多个实例。每个值都是独立实例 —— 认证 token 与 metadata 缓存
    绝不跨实例复用（那条隔离由 BaseAdapter 实现保证，有测试钉住）。

    当前后端不支持命名实例（如 local）时返回空 dict —— 这是**如实回答**
    「这个后端没有多实例概念」，而不是错误。
    """
    config = load_config() if config is None else config
    backend = str(config.get("backend", "local") or "local").lower()
    info = BACKENDS.get(backend)
    if info is None or not info["named_bases"]:
        return {}
    return {name: get_adapter(config, base_name=name)
            for name in _named_bases(config, backend)
            if get_base_config(config, name, backend)}


def _fallback(strict: bool, why: str, label: str = "远程后端") -> None:
    """目标后端不可用时：默认只告警退回 local；**写入路径必须硬失败**。

    `strict=True` 由 DataService 等写入路径传入：静默退回 local 会把「写线上」
    变成「写本地 CSV」，而且读回校验还会照样「通过」。
    """
    if strict:
        raise RuntimeError("%s 不可用，已拒绝退回 local 以免误写本地库：%s" % (label, why))
    print("[warn] %s，退回 local 模式" % why, file=sys.stderr)


def get_adapter(config: dict = None, base_name: str = None, strict: bool = False):
    """构造当前后端下一个实例。``strict`` 供写入路径使用（见 `_fallback`）。"""
    config = load_config() if config is None else config
    if not isinstance(config, dict):
        raise SystemExit("[错误] 配置文件格式不对（顶层应为 key: value）。"
                         "请检查 config.yaml，或运行 python tools/setup.py 重新生成。")
    backend = str(config.get("backend") or "local").strip().lower()
    info = BACKENDS.get(backend)
    if info is None:
        # ⚠️ 以前这里会**静默**返回 LocalAdapter。加了新后端之后那就成了隐患：
        #    config 里写了 backend: feishu（还没实现完）会被当成 local，把线上写入
        #    变成写本地 CSV，且毫无提示。所以未知后端一律走 fail-closed 的 _fallback。
        _fallback(strict, "未知后端 backend=%r（可用：%s）"
                  % (backend, "/".join(list_backends())), label="后端「%s」" % backend)
        return _make_local(None, config)
    if not info["named_bases"]:
        return info["make"](None, config)

    selected = get_base_config(config, base_name, backend)
    if selected is None and base_name is not None and _named_bases(config, backend):
        _fallback(strict, "未配置 %s 实例「%s」" % (info["label"], base_name), label=info["label"])
    if selected:
        missing = [k for k in info["required_keys"] if not selected.get(k)]
        if not missing:
            try:
                return info["make"](selected, config)
            except Exception as e:
                _fallback(strict, "%s 初始化失败：%s" % (info["label"], e), label=info["label"])
        else:
            _fallback(strict, "%s 实例「%s」缺 %s"
                      % (info["label"], selected.get("name") or base_name or "default",
                         "/".join(missing)), label=info["label"])
    elif not _named_bases(config, backend):
        _fallback(strict, "未配置 %s 实例（需 %s）"
                  % (info["label"], "/".join(info["required_keys"])), label=info["label"])
    # 默认 / 兜底：本地
    return _make_local(None, config)


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


def _seatable_bases(config: dict) -> dict:
    """已废弃：SeaTable 专用的命名实例解析。

    保留为**兼容别名**，只为不让仓库外可能存在的 `import _seatable_bases` 断掉。
    新代码请用通用的 :func:`_named_bases`（或公开的 :func:`get_base_config`）——
    后端专属的解析路径正是本次要消灭的东西。
    """
    return _named_bases(config, "seatable")
