# -*- coding: utf-8 -*-
"""飞书多维表格（Base / Bitable）适配器。

本文件里的**每一条协议事实都来自只读/自清理实测**（2026-10-01，探测脚本：
``probes/feishu_write_probe.py`` / ``feishu_link_probe.py`` / ``feishu_link_probe2.py``），
不照文档猜。实测得到的关键结论：

1. **响应信封**：成功 ``{"ok":true,"identity":..,"data":{...}}`` 走 stdout；
   失败 ``{"ok":false,"error":{code,message,hint,log_id}}`` 走 **stderr** 且退出码 1。
2. **字段类型是字符串**（``text`` / ``number`` / ``select`` / ``datetime`` /
   ``checkbox`` / ``user`` / ``attachment`` / ``link`` / ``formula`` / ``lookup`` …），
   不是数字码；``select`` 带 ``multiple`` 与 ``options[{name,hue,lightness}]``，
   **选项没有 id**（与 SeaTable 不同，所以读写直接用选项名，不需要 id→名映射）。
3. ``link`` 字段带 ``link_table``（对方表 id）与 ``bidirectional``。
4. **单元格读取形状**：select → ``["进行中"]``；datetime →
   ``"2026-05-07T00:00:00.000+08:00"``；checkbox → bool；number → 数字；
   attachment → ``[{file_token,name,size}]``；user → ``[{id,name}]``；
   link → ``[{id}]``；空 → ``null``。
5. **link 单元格写入形状 = ``[{"id": "rec_xxx"}]``**（服务端也宽容接受
   ``["rec_xxx"]`` 与裸字符串 ``"rec_xxx"``，但我们只使用文档形状）。
6. ``+record-batch-create`` → ``data.record_id_list``（与入参同序）；
   单批 **≤200**（实测 500 直接报错）；**同表写入必须串行**（并行会撞并发冲突）。
7. ⚠️ **``+record-batch-update`` 对不存在的 record_id 返回 rc=0**，只把 id 放进
   ``data.record_not_found`` —— 这是本仓库最忌讳的「看起来成功」。
   ``update_row`` 因此**必须**检查 ``record_not_found``，不能只看 rc。
8. ⚠️ **新建记录后有可见性延迟**（实测 ~0.6s 时 update 报 record_not_found，
   ~2.9s 后成功）。所以「建完立刻改」会被误判成行不存在 → 需要短重试。
9. ⚠️ ``bidirectional=false`` 的关联字段在对方表**不会生成反向列**（实测对方表
   字段列表里没有它）。因此飞书上**做不到**「两张表各记一次」的双向关联 →
   ``link()`` 必须显式拒绝，而不是悄悄只写一侧（详见 ``link()`` 的 docstring）。
10. 写只读字段（formula / lookup / auto_number / created_* / modified_*）会被服务端
    **静默过滤**并回 ``ignored_fields``。适配器改为**客户端先剔除**这些列，
    于是 ``ignored_fields`` 一旦出现就说明有真正的意外 → 直接报错。
11. ⚠️ **``+table-create`` 的 ``--json`` 是 ``--format json`` 的布尔简写，不是 body**。
    把 JSON 体塞给它只会得到 ``positional arguments are not supported`` ——
    一个指不到病根的报错。表结构走 ``--fields``、表名走 ``--name``（见 ``_JSON_BODY_CMDS``）。
12. ⚠️ **读不是强一致的**：实测更新/删除**成功之后**立刻读回仍可能拿到旧值，
    约 2.7s 后才可见（写后读同样如此）。适配器不去掩盖这件事 ——
    「读回是空的/是旧的」既可能是「真没有」也可能是「还没可见」，
    适配器无权替调用方选一个。**上层的写后读回验证必须容忍这个窗口**
    （``tools/verify_feishu.py`` 里有一份带轮询的范例）。
13. ⚠️ ``+field-list`` 的返回顺序**不保证**与建列顺序一致（实测同一张表两次
    调用顺序不同）。所以 ``get_metadata`` 的列顺序只可用于展示，不可当契约；
    一切按列名取值。
14. 单选/多选列的选项**必须在建列时一次给全**：写一个不存在的选项直接报
    ``800030005 not_found``（"Provide an existing option value"），
    **飞书不会替你新增选项**。``ensure_table`` 因此支持列定义里的 ``options``。
15. ``+table-delete`` 收表名或 id；删完被引用的表后立刻删另一张可能瞬时失败
    （最终一致），需要重试。

传输层
──────
  · **cli（默认）** —— 通过 ``lark-cli`` 子进程调用。复用 lark-cli 已登录的
    身份与 scope（``base:record:*`` / ``base:table:*``），**不需要 app_secret**。
    本机实测路径见 ``_CLI_HINTS``。要求目标机装过 lark-cli 并 ``auth login`` 过。
  · **api** —— 直连 ``open.feishu.cn`` Bitable v1，需要 ``app_id`` + ``app_secret``，
    适合分发给没有 lark-cli 的机器。
    ⚠️ **这条分支未经实测**：本机 app_secret 存在系统钥匙串里（``cmdkey /list``
    为空，lark-cli 也没有导出命令），拿不到就无法验证。故 ``auth()`` 里对它做一次
    真实探活（``GET /tables``），配置错了会在**建连时**报错，不会拖到业务写入才暴露。
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional

from . import schema
from .base import (CAP_BATCH_WRITE, CAP_DELETE, CAP_LINK, CAP_LINK_READ, CAP_READ,
                   CAP_SCHEMA_MANAGE, CAP_SERVER_ROW_ID, CAP_UPDATE, CAP_WRITE,
                   BaseAdapter, Unsupported)

#: 单批写入上限（实测：500 直接报错，200 是官方硬限制）。
_RECORD_BATCH = 200

#: ``--format json`` 下 ``--limit`` 的上限（实测 500 即报错），分页步长。
_PAGE = 200

#: 新建记录后的可见性延迟重试（实测 ~3s 内恢复）。
_SETTLE_RETRIES = 4
_SETTLE_SLEEP = 1.5

#: 这些字段类型平台只读，**写入会被静默丢弃**。
#: 适配器在发送前先把它们从 payload 里剔除（有意的、成文的转换），
#: 于是服务端若仍回 ``ignored_fields``，就一定是意外 → 报错而不是放过。
_READONLY_TYPES = frozenset({
    "formula", "lookup", "auto_number", "created_time", "modified_time",
    "created_user", "modified_user", "button",
})

#: 关联类字段类型（读关联 / 写关联的目标）。
_LINK_TYPES = frozenset({"link", "duplex_link"})

#: Bitable 数字类型码 → 字符串类型名（**仅 api 传输层需要**；cli 直接给字符串）。
_TYPE_BY_CODE = {
    1: "text", 2: "number", 3: "select", 4: "multiselect", 5: "datetime",
    7: "checkbox", 11: "user", 13: "text", 15: "text", 17: "attachment",
    18: "link", 19: "lookup", 20: "formula", 21: "duplex_link", 22: "location",
    23: "group_chat", 1001: "created_time", 1002: "modified_time",
    1003: "created_user", 1004: "modified_user", 1005: "auto_number",
}

#: 各子命令「怎么把 JSON 体传进去」**不是统一的**（实测踩过）：
#:   · ``+table-create`` 的 ``--json`` 是 ``--format json`` 的**布尔简写**，不是 body！
#:     它的表结构走 ``--fields``（JSON 数组）、表名走 ``--name``。
#:     把 body 塞给 ``+table-create --json`` 会得到
#:     ``positional arguments are not supported`` —— 一个完全指不到病根的报错。
#:   · 其余写入类命令用 ``--json``，并且支持 ``@文件``（大 payload 用它避开 argv 长度限制，
#:     实测相对/绝对路径都行）。
_JSON_BODY_CMDS = frozenset({
    "+field-create", "+field-update", "+record-batch-create",
    "+record-batch-update", "+record-delete", "+record-get",
})

#: 可执行文件的候选位置（**本机实测路径在前**）。
_CLI_HINTS = (
    r"%USERPROFILE%\.workbuddy\binaries\node\cli-connector-packages"
    r"\node_modules\@larksuite\cli\bin\lark-cli.exe",
    r"%APPDATA%\npm\node_modules\@larksuite\cli\bin\lark-cli.exe",
    r"%LOCALAPPDATA%\Programs\lark-cli\lark-cli.exe",
)


class FeishuError(RuntimeError):
    """飞书调用失败。带服务端 ``code`` / ``log_id``，便于按日志定位。"""

    def __init__(self, message: str, code=None, log_id=None, raw: dict = None):
        super().__init__(message)
        self.code = code
        self.log_id = log_id
        self.raw = raw or {}


# ══════════════════════════════════════════════════════════════════
# 传输层
# ══════════════════════════════════════════════════════════════════

def find_lark_cli(explicit: str = "") -> str:
    """定位 ``lark-cli`` 可执行文件。找不到返回空串（**不猜、不下载**）。"""
    for cand in ([explicit] if explicit else []) + [os.environ.get("LARK_CLI", "")] + \
            [os.path.expandvars(p) for p in _CLI_HINTS]:
        if cand and os.path.exists(cand):
            return os.path.abspath(cand)
    found = shutil.which("lark-cli") or shutil.which("lark-cli.exe")
    return os.path.abspath(found) if found else ""


class _CliTransport:
    """通过 ``lark-cli`` 子进程访问飞书 Base。

    **不使用 shell**：参数以 argv 数组直接交给 ``CreateProcessW``，中文列名不会被
    引号/编码折腾。JSON 体一律走 ``--json @文件``（避免 Windows argv 长度与转义）。
    """

    def __init__(self, cli_path: str, identity: str = "", timeout: int = 180):
        self.cli_path = cli_path
        self.identity = identity or ""
        self.timeout = timeout
        self._seq = 0

    # ── 底层调用 ────────────────────────────────────────
    def _exec(self, args: List[str], timeout: int = None) -> tuple:
        cmd = [self.cli_path, "base"] + list(args)
        if self.identity:
            cmd += ["--as", self.identity]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout or self.timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()

    @staticmethod
    def _loads(text: str):
        """宽松解析：成功路径 stdout 是纯 JSON；兼容前面混进提示行的情况。"""
        try:
            return json.loads(text)
        except Exception:
            i = text.find("{")
            if i < 0:
                return None
            try:
                return json.loads(text[i:])
            except Exception:
                return None

    def call(self, args: List[str], json_body: dict = None, timeout: int = None) -> dict:
        """执行一条 ``lark-cli base …``，返回信封里的 ``data`` 段。失败抛 FeishuError。"""
        argv = list(args)
        cmd = argv[0] if argv else ""
        tmp = None
        if json_body is not None:
            if cmd == "+table-create":
                # 见 _JSON_BODY_CMDS 的说明：这个子命令不吃 --json 体
                argv += ["--fields", json.dumps(json_body.get("fields") or [],
                                                ensure_ascii=False)]
            else:
                if cmd not in _JSON_BODY_CMDS:
                    # 宁可在这里报错，也不要让 CLI 回一个「positional arguments are
                    # not supported」这种指不到病根的话（实测就是这个报错）。
                    raise FeishuError("子命令 %s 未声明如何使用 JSON 体（_JSON_BODY_CMDS）"
                                      % cmd)
                fd, tmp = tempfile.mkstemp(prefix="wb_lark_", suffix=".json")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(json_body, f, ensure_ascii=False)
                argv += ["--json", "@" + tmp.replace("\\", "/")]
        try:
            rc, out, err = self._exec(argv, timeout=timeout)
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        env = self._loads(out) or (self._loads(err) if rc != 0 else None)
        if env is None:
            raise FeishuError("lark-cli 输出无法解析（rc=%s）：stdout=%r stderr=%r"
                              % (rc, out[:300], err[:300]))
        if not env.get("ok"):
            e = env.get("error") or {}
            raise FeishuError(
                "飞书调用失败：%s（code=%s%s）"
                % (e.get("message") or env, e.get("code"),
                   "，log_id=%s" % e.get("log_id") if e.get("log_id") else ""),
                code=e.get("code"), log_id=e.get("log_id"), raw=e)
        return env.get("data") or {}

    # ── 身份探活 ────────────────────────────────────────
    def probe_identity(self) -> dict:
        """``lark-cli auth status``：确认目标身份可用。"""
        p = subprocess.run([self.cli_path, "auth", "status"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        env = self._loads((p.stdout or "").strip())
        if not isinstance(env, dict):
            raise FeishuError("lark-cli auth status 解析失败：%r" % (p.stdout or "")[:300])
        return env


class _ApiTransport:
    """直连飞书 Open API（Bitable v1）。

    ⚠️ **未经实测**（本机拿不到 app_secret）。按官方文档实现，``auth()`` 会真实探活。
    """

    def __init__(self, app_id: str, app_secret: str, server: str = ""):
        self.app_id = app_id
        self.app_secret = app_secret
        self.server = (server or "https://open.feishu.cn").rstrip("/")
        self._token = None

    def _req(self, method: str, path: str, params=None, body=None):
        import requests  # 延迟导入：cli 传输层不需要它
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        r = requests.request(method, self.server + path, headers=headers, params=params,
                             json=body, timeout=60)
        try:
            env = r.json()
        except Exception:
            raise FeishuError("飞书 Open API 返回非 JSON（HTTP %s）：%s"
                              % (r.status_code, r.text[:300]))
        if r.status_code >= 400 or env.get("code") not in (0, None):
            raise FeishuError("飞书 Open API 失败：%s（code=%s）"
                              % (env.get("msg") or r.text[:200], env.get("code")),
                              code=env.get("code"), raw=env)
        return env.get("data") or {}

    def auth(self) -> None:
        d = self._req("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                      body={"app_id": self.app_id, "app_secret": self.app_secret})
        tok = d.get("tenant_access_token")
        if not tok:
            raise FeishuError("未能取得 tenant_access_token（检查 app_id / app_secret）")
        self._token = tok

    def close(self) -> None:
        self._token = None

    def call(self, args: List[str], json_body: dict = None, timeout: int = None) -> dict:
        """把 cli 风格的子命令翻译成 Bitable v1 调用（形状与 cli 的 data 段对齐）。"""
        a = list(args)
        cmd = a[0]
        opt = {}
        i = 1
        while i < len(a):
            if a[i].startswith("--"):
                key = a[i][2:]
                val = a[i + 1] if i + 1 < len(a) and not a[i + 1].startswith("--") else True
                opt.setdefault(key, val)
                i += 2
            else:
                i += 1
        base = opt.get("base-token")
        tbl = opt.get("table-id")
        root = "/open-apis/bitable/v1/apps/%s" % base

        if cmd == "+table-list":
            d = self._req("GET", root + "/tables")
            return {"tables": d.get("items") or [], "total": d.get("total")}
        if cmd == "+table-create":
            d = self._req("POST", root + "/tables", body=json_body or {})
            return {"table": d}
        if cmd == "+table-delete":
            self._req("DELETE", "%s/tables/%s" % (root, tbl))
            return {"deleted": True, "table_id": tbl}
        if cmd == "+field-list":
            d = self._req("GET", "%s/tables/%s/fields" % (root, tbl),
                          params={"page_size": 200})
            return {"fields": [self._norm_field(f) for f in (d.get("items") or [])]}
        if cmd == "+field-create":
            d = self._req("POST", "%s/tables/%s/fields" % (root, tbl), body=json_body or {})
            return {"fields": [self._norm_field(d.get("field") or {})]}
        if cmd == "+record-list":
            d = self._req("POST", "%s/tables/%s/records/search" % (root, tbl),
                          body={"page_size": int(opt.get("limit") or _PAGE),
                                "page_token": opt.get("page-token") or None})
            items = d.get("items") or []
            names = None
            rows, ids = [], []
            for it in items:
                f = it.get("fields") or {}
                if names is None:
                    names = list(f)
                rows.append([f.get(k) for k in names])
                ids.append(it.get("record_id"))
            return {"data": rows, "fields": names or [],
                    "record_id_list": ids, "has_more": bool(d.get("has_more"))}
        if cmd == "+record-batch-create":
            recs = [{"fields": r} for r in (json_body or {}).get("create_records", [])]
            d = self._req("POST", "%s/tables/%s/records/batch_create" % (root, tbl),
                          body={"records": recs})
            return {"record_id_list": [x.get("record_id") for x in (d.get("records") or [])]}
        if cmd == "+record-batch-update":
            ups = (json_body or {}).get("update_records", {})
            recs = [{"record_id": k, "fields": v} for k, v in ups.items()]
            d = self._req("POST", "%s/tables/%s/records/batch_update" % (root, tbl),
                          body={"records": recs})
            return {"record_id_list": [x.get("record_id") for x in (d.get("records") or [])],
                    "record_not_found": d.get("not_found") or []}
        if cmd == "+record-delete":
            ids = (json_body or {}).get("record_id_list") or []
            d = self._req("POST", "%s/tables/%s/records/batch_delete" % (root, tbl),
                          body={"records": ids})
            return {"deleted": True, "record_not_found": d.get("not_found") or []}
        if cmd == "+record-get":
            out = self._req("POST", "%s/tables/%s/records/search" % (root, tbl),
                            body={"filter": {"conjunction": "and", "conditions": [
                                {"field_name": "record_id", "operator": "is",
                                 "value": [opt.get("record-id")]}]}})
            return {"record_id_list": [x.get("record_id") for x in (out.get("items") or [])],
                    "record_not_found": []}
        raise FeishuError("api 传输层尚未实现子命令 %s" % cmd)

    @staticmethod
    def _norm_field(f: dict) -> dict:
        """把 Bitable v1 的字段形状（数字 type + property）折成 cli 的字符串形状。"""
        out = dict(f)
        t = f.get("type")
        if isinstance(t, int):
            out["type"] = _TYPE_BY_CODE.get(t, str(t))
        prop = f.get("property") or {}
        if out.get("type") in ("select", "multiselect"):
            out["multiple"] = out.get("type") == "multiselect"
            out["options"] = prop.get("options") or []
        if out.get("type") in _LINK_TYPES:
            out.setdefault("link_table", prop.get("table_id"))
            out.setdefault("bidirectional", bool(prop.get("multiple")))
        return out


# ══════════════════════════════════════════════════════════════════
# 适配器
# ══════════════════════════════════════════════════════════════════

class FeishuAdapter(BaseAdapter):
    backend = "feishu"

    #: 能力声明（声明式，不联网、不认证）：
    #:   · batch_write    —— ``records/batch_create`` 原生批量（≤200/批）
    #:   · schema_manage  —— ``+table-create`` / ``+field-create`` 实测可用
    #:   · link / link_read —— 实测：单向关联可读可写；**双向**由 ``link()`` 显式拒绝
    #:     （见 ``link()``：单向字段在对方表连反向列都没有，做不到「两表各记一次」）
    #:   · server_row_id  —— ``record_id`` 由服务端生成，客户端不可指定
    #:   · **不声明 query_pushdown**：``query()`` 走基类的内存过滤。
    #:     飞书确实能在服务端过滤（``--filter-json``），但那条路要求逐列判类型
    #:     （select 要传数组、日期要 ExactDate 包装），无法对任意 filters 无条件成立 ——
    #:     宁可不声明，也不能声明一个「有时成立」的能力。需要下推请用 ``search()``。
    #:   · **不声明 idempotent**：没有幂等键，重试即重复写。
    #:   · **不声明 optimistic_lock**：只有 Base 级 ``rev``，没有行级版本号。
    #:
    #: 另外：``link_append()`` **刻意不实现**（沿用基类的 ``Unsupported``）。
    #: 它不是「懒得写」——实测确认飞书写后有可见性延迟（~3s），而 link_append 的
    #: 读-改-写必须**先读准再写**；读到陈旧数据就会把既有历史关联整体覆盖掉，
    #: 且不报错。这正是基类默认抛 Unsupported 要防的那件事（SeaTable 版实现了，
    #: 但同样带着「非并发安全」的已知局限；飞书还多一层读延迟，风险更高）。
    #: 需要追加语义的调用方请自行串行化并显式用 ``list_linked`` + ``link``。
    CAPS = frozenset({
        CAP_READ, CAP_WRITE, CAP_UPDATE, CAP_DELETE,
        CAP_LINK, CAP_LINK_READ, CAP_BATCH_WRITE, CAP_SCHEMA_MANAGE, CAP_SERVER_ROW_ID,
    })

    def __init__(self, base_token: str, cli_path: str = "", identity: str = "",
                 app_id: str = "", app_secret: str = "", transport: str = "",
                 server: str = "", base_name: str = None):
        self.base_token = base_token
        self.base_name = base_name or "default"
        self.app_id = app_id or ""
        self.app_secret = app_secret or ""
        self.server = server or ""
        self.identity = identity or ""
        self.cli_path = cli_path or ""
        self._transport_kind = (transport or "").strip().lower()
        self._t = None
        self._tables = None          # {表名: table_id}
        self._tid2name = None
        self._fields = {}            # tid -> [field, ...]
        self._kind_used = ""         # 实际选中的传输层（诊断用）

    # ── 生命周期 ────────────────────────────────────────
    def auth(self) -> None:
        """建连 + 探活。选传输层的顺序：显式指定 > 有 app_id/secret 走 api > cli。"""
        kind = self._transport_kind
        if not kind:
            kind = "api" if (self.app_id and self.app_secret) else "cli"
        if kind == "api":
            if not (self.app_id and self.app_secret):
                raise FeishuError("feishu 后端选了 api 传输层，但缺 app_id / app_secret")
            self._t = _ApiTransport(self.app_id, self.app_secret, self.server)
            self._t.auth()
        else:
            path = find_lark_cli(self.cli_path)
            if not path:
                raise FeishuError(
                    "找不到 lark-cli。feishu 后端需要①装好并登录 lark-cli"
                    "（`lark-cli auth login`），或②配置 feishu.app_id + feishu.app_secret。"
                    "也可用 feishu.cli_path 显式指定可执行文件路径。")
            self._t = _CliTransport(path, identity=self.identity)
            st = self._t.probe_identity()
            ident = self.identity or st.get("defaultAs") or "user"
            # defaultAs=auto 时账号级身份优先，其次 bot；两者都要「available」才算可用
            if ident == "auto":
                cands = [k for k, v in (st.get("identities") or {}).items()
                         if isinstance(v, dict) and v.get("available")]
                if not cands:
                    raise FeishuError("lark-cli 没有任何可用身份，请先 `lark-cli auth login`")
            else:
                info = (st.get("identities") or {}).get(ident) or {}
                if not info.get("available"):
                    raise FeishuError(
                        "lark-cli 的身份「%s」不可用（%s），请先 `lark-cli auth login`"
                        % (ident, info.get("message") or info.get("status") or "未知原因"))
        self._kind_used = kind
        self._invalidate()

    def close(self) -> None:
        """清掉本地缓存并发起传输层清理。

        为什么需要：api 传输层的 ``tenant_access_token`` 有有效期；cli 传输层虽然
        无服务端会话，但缓存了表/字段元数据，长期驻留的进程不该拿着过期快照。
        """
        if self._t is not None and hasattr(self._t, "close"):
            try:
                self._t.close()
            except Exception:
                pass
        self._invalidate()

    def _invalidate(self) -> None:
        self._tables = None
        self._tid2name = None
        self._fields = {}

    def _call(self, args: List[str], json_body: dict = None, timeout: int = None) -> dict:
        if self._t is None:
            self.auth()
        return self._t.call(args, json_body=json_body, timeout=timeout)

    def describe(self) -> str:
        return "%s(%s, transport=%s, base=%s) caps=%s" % (
            type(self).__name__, self.backend, self._kind_used or self._transport_kind or "auto",
            self.base_name, ",".join(sorted(self.capabilities())) or "-")

    # ── 表名 ↔ id ───────────────────────────────────────
    def _load_tables(self) -> None:
        if self._tables is not None:
            return
        d = self._call(["+table-list", "--base-token", self.base_token])
        self._tables = {}
        for t in (d.get("tables") or []):
            if t.get("name"):
                self._tables[str(t["name"])] = t.get("id")
        self._tid2name = {v: k for k, v in self._tables.items() if v}

    def _table_id(self, table: str) -> str:
        """按**表名**解析 table_id。解析不到就报错，绝不把名字直接当 id 发出去。

        为什么显式解析：``lark-cli`` 接受「表名或 id」两种写法，看起来省事 ——
        但表名不存在时它会回一个泛化的 ``not_found``，让人分不清是表错了还是权限错了。
        先把可用表名取回来，报错时就能把「现有表」一并列出，一次定位。
        """
        self._load_tables()
        if table in self._tables:
            return self._tables[table]
        # 也允许直接传 table_id（tbl... 开头）
        if str(table).startswith("tbl") and self._tid2name and table in self._tid2name:
            return table
        raise KeyError("表不存在：%r（本 Base 现有：%s）"
                       % (table, " / ".join(sorted(self._tables)) or "（空）"))

    def _fields_of(self, tid: str) -> List[dict]:
        if tid not in self._fields:
            d = self._call(["+field-list", "--base-token", self.base_token, "--table-id", tid])
            self._fields[tid] = list(d.get("fields") or [])
        return self._fields[tid]

    def _field_map(self, tid: str) -> Dict[str, dict]:
        return {f["name"]: f for f in self._fields_of(tid) if f.get("name")}

    def list_tables(self) -> List[str]:
        """本 Base 的表名列表（排序后）。

        为什么要有：没有它，调用方想知道「这个 Base 里有哪些表」就只能去读
        ``_tables`` 私有属性 —— 而私有属性换个后端必然 ``AttributeError``，
        正是这一期要消灭的耦合。诊断、校验、部署前检查都该走这个方法。
        """
        self._load_tables()
        return sorted(self._tables)

    # ── 读 ─────────────────────────────────────────────
    def get_metadata(self, table: str) -> Dict[str, Any]:
        tid = self._table_id(table)
        cols = []
        for f in self._fields_of(tid):
            nm = f.get("name")
            if not nm:
                continue
            t = str(f.get("type") or "").lower()
            # 把「select + multiple=true」折成 multiselect，让两种传输层对外形状一致
            if t == "select" and f.get("multiple"):
                t = "multiselect"
            cols.append({"name": nm, "type": t, "key": f.get("id")})
        return {"table_name": table, "columns": cols}

    def table_exists(self, table: str) -> bool:
        """按表清单判断，不抛异常（表不存在返回 False）。"""
        try:
            self._load_tables()
        except Exception:
            return False
        return table in self._tables

    # ── 单元格翻译（读写两个方向）────────────────────────

    @staticmethod
    def _flat_cell(v, ftype, multiple=False):
        """服务端单元格值 → 下游习惯的扁平值。

        规则与 SeaTable 适配器对齐（下游同一套代码要吃两个后端）：
          · 单选 → 标量字符串（飞书回的是 ``["进行中"]``，掏第一个）
          · 多选 → 字符串列表
          · 日期/时间 → ``YYYY-MM-DD``（飞书回 ISO 带时区，截断；**时分秒会丢**，
            与 SeaTable 路径行为一致 —— 需要精确时间请直接用原始值，见 get_metadata 的 type）
          · 空 → ``""``（不是 None：下游到处 ``.get(col, "")``，给 None 会拼出 "None"）
          · 其余（number/bool/attachment/user/link）→ 原样，不丢信息
        """
        t = str(ftype or "").lower()
        if v is None:
            return ""
        if t == "datetime" and isinstance(v, str) and "T" in v:
            return v[:10]
        if t == "select" and isinstance(v, list):
            vals = [str(x) for x in v]
            return vals if multiple else (vals[0] if vals else "")
        if t == "multiselect" and isinstance(v, list):
            return [str(x) for x in v]
        return v

    @staticmethod
    def _to_cell(value, ftype, multiple=False):
        """下游值 → 服务端要求的 CellValue 形状。

        这层翻译不是美化，是**必需**：飞书对单元格形状有硬校验，写错形状会直接
        报 ``800010701 Cell value does not match any supported shape``。
        实测确认过的形状：
          · select       ``["选项名"]``（单选最多一个）—— 必须已存在于该字段的选项
          · checkbox     bool（注意 ``bool("false") is True``，字符串要显式翻译）
          · number       纯数字
          · datetime     ``"YYYY-MM-DD"`` / ISO / Unix 毫秒
          · link/user/group  ``[{"id": "rec_xxx" / "ou_xxx" / "oc_xxx"}]``
          · 清空          ``null``（空数组等价）
        """
        t = str(ftype or "").lower()
        if value is None or value == "" or value == [] or value == ():
            return None
        if t in ("select", "multiselect"):
            vals = value if isinstance(value, (list, tuple, set)) else [value]
            vals = [str(v) for v in vals if v not in (None, "")]
            if not vals:
                return None
            return vals if (multiple or t == "multiselect") else vals[:1]
        if t == "checkbox":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "y", "yes", "是", "✓", "已完成")
            return bool(value)
        if t == "number":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float)):
                return value
            try:
                return float(str(value).replace(",", "").strip())
            except ValueError:
                return value          # 交给服务端报错，别在这里悄悄变成 0
        if t in _LINK_TYPES or t in ("user", "group_chat"):
            vals = value if isinstance(value, (list, tuple, set)) else [value]
            out = []
            for x in vals:
                if not x:
                    continue
                out.append({"id": str(x.get("id"))} if isinstance(x, dict) else {"id": str(x)})
            return out or None
        return value                   # text / attachment / location / 未知：原样交给服务端校验

    def _to_record(self, data: dict, fmap: Dict[str, dict]) -> dict:
        """业务 dict → 飞书 ``fields`` 对象。

        **有意剔除只读列**（formula / lookup / auto_number / created_* / modified_*）：
        上游常把「读回来的整行」再写回去，里面天然含这些列；服务端对它们的处理是
        **静默过滤**并回一个 ``ignored_fields``。与其让「我让它写了它却没写」悄悄发生，
        不如在这里按已知的只读类型剔掉（成文转换），然后在写完后校验
        ``ignored_fields`` 是否为空 —— 那时它一旦出现就一定是意外。
        """
        out = {}
        unknown = []
        for k, v in (data or {}).items():
            if k == "__row_id__":
                continue
            f = fmap.get(k)
            if f is None:
                unknown.append(k)
                continue
            t = str(f.get("type") or "").lower()
            if t in _READONLY_TYPES:
                continue
            out[k] = self._to_cell(v, t, bool(f.get("multiple")))
        if unknown:
            # 不静默丢：列名写错是最常见的错，而飞书侧报错文案里只给前几个字段名
            raise FeishuError(
                "以下列在表里不存在（可能表选错了，或列名有多余空格）：%s；该表可用列：%s"
                % (" / ".join(unknown[:8]), " / ".join(list(fmap)[:20])))
        return out

    def _check_ignored(self, data: dict, what: str) -> None:
        """服务端回 ``ignored_fields`` = 有字段被静默丢弃。必须报错，不许放过。"""
        ig = data.get("ignored_fields")
        if not ig:
            return
        if isinstance(ig, dict):
            names = []
            for v in ig.values():
                names.extend(v if isinstance(v, list) else [v])
        elif isinstance(ig, (list, tuple)):
            names = list(ig)
        else:
            names = [ig]
        raise FeishuError(
            "%s：服务端忽略了字段 %s（只读列应已在客户端剔除，出现即说明有未知的只读字段）"
            % (what, " / ".join(str(x) for x in names)))

    # ── 读 ─────────────────────────────────────────────
    def list_rows(self, table: str) -> List[Dict[str, Any]]:
        """按 200 行/页拉全表（``--format json`` 的 limit 上限实测就是 200）。"""
        tid = self._table_id(table)
        fmap = self._field_map(tid)
        ftypes = {n: str(f.get("type") or "").lower() for n, f in fmap.items()}
        multi = {n: bool(f.get("multiple")) for n, f in fmap.items()}
        rows, offset = [], 0
        while True:
            d = self._call(["+record-list", "--base-token", self.base_token,
                            "--table-id", tid, "--limit", str(_PAGE),
                            "--offset", str(offset), "--format", "json"])
            names = list(d.get("fields") or [])
            matrix = d.get("data") or []
            ids = list(d.get("record_id_list") or [])
            for i, row in enumerate(matrix):
                rec = {}
                for j, nm in enumerate(names):
                    raw = row[j] if j < len(row) else None
                    rec[nm] = self._flat_cell(raw, ftypes.get(nm), multi.get(nm, False))
                rec["__row_id__"] = ids[i] if i < len(ids) else None
                rows.append(rec)
            # 终止条件：服务端说没有更多 / 本页不满 / 本页为空
            # （最后一条是防死循环的兜底：has_more 为真却没给新行时必须停）
            if not d.get("has_more") or len(matrix) < _PAGE or not matrix:
                break
            offset += len(matrix)
        return rows

    def get_row(self, table: str, row_id: str) -> Optional[Dict[str, Any]]:
        """单行读取；找不到返回 None。

        ⚠️ 为什么覆盖成「先试快路径、失败退全表扫描」而不是直接用 ``+record-get``：
        实测 ``+record-get`` 对**真实存在**的记录也可能回 ``record_not_found``
        （新建后 ~1s 内），而且它会把不存在的 id 回成一行全 null ——
        「行不存在」和「行存在但全空」在它眼里长得一模一样。
        所以这里只用它当快路径，并且**要求 record_not_found 为空**才采信；
        任何异常都退回 ``list_rows`` 扫描（慢但一定对）。
        """
        tid = self._table_id(table)
        fmap = self._field_map(tid)
        ftypes = {n: str(f.get("type") or "").lower() for n, f in fmap.items()}
        multi = {n: bool(f.get("multiple")) for n, f in fmap.items()}
        try:
            d = self._call(["+record-get", "--base-token", self.base_token,
                            "--table-id", tid, "--record-id", str(row_id),
                            "--format", "json"])
            ids = [str(x) for x in (d.get("record_id_list") or [])]
            nf = [str(x) for x in (d.get("record_not_found") or [])]
            matrix = d.get("data") or []
            names = list(d.get("fields") or [])
            if str(row_id) in ids and str(row_id) not in nf and matrix:
                rec = {}
                row = matrix[0]
                for j, nm in enumerate(names):
                    raw = row[j] if j < len(row) else None
                    rec[nm] = self._flat_cell(raw, ftypes.get(nm), multi.get(nm, False))
                rec["__row_id__"] = str(row_id)
                return rec
        except Exception:
            pass  # 快路径不可用 → 退全表（正确性优先，绝不因快路径失败而报「没有」）
        target = str(row_id)
        for r in self.list_rows(table):
            if str(r.get("__row_id__")) == target:
                return r
        return None

    def search(self, table: str, filter_json: dict = None, sort_json: list = None,
               limit: int = None) -> List[Dict[str, Any]]:
        """**服务端过滤**读取（``--filter-json``）。不属于基类契约，是飞书特有增强。

        为什么单独开一个方法而不是塞进 ``query()``：``query()`` 的 filters 是
        ``{列名: 值}`` 的松散约定，要下推就必须逐列猜语义（select 得包成数组、
        日期得包 ``ExactDate()``、数字得和字符串区分），做不到无条件正确。
        能力位 ``query_pushdown`` 一声明就是「总是」，所以宁可不声明；
        真正需要下推的调用方请显式写 ``filter_json`` —— 语法完全交给飞书。
        条件语法（实测自 CLI 官方文档）：``{"logic":"and","conditions":[["列","==","值"]]}``，
        操作符支持 ``== != > >= < <= intersects disjoint empty non_empty``。
        """
        tid = self._table_id(table)
        fmap = self._field_map(tid)
        args = ["+record-list", "--base-token", self.base_token, "--table-id", tid,
                "--limit", str(min(int(limit or _PAGE), _PAGE)), "--format", "json"]
        tmp = None
        if filter_json:
            fd, tmp = tempfile.mkstemp(prefix="wb_lark_f_", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(filter_json, f, ensure_ascii=False)
            args += ["--filter-json", "@" + tmp.replace("\\", "/")]
        if sort_json:
            args += ["--sort-json", json.dumps(sort_json, ensure_ascii=False)]
        try:
            d = self._call(args)
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        out = []
        names = list(d.get("fields") or [])
        ids = list(d.get("record_id_list") or [])
        for i, row in enumerate(d.get("data") or []):
            rec = {}
            for j, nm in enumerate(names):
                raw = row[j] if j < len(row) else None
                rec[nm] = self._flat_cell(raw, str(fmap.get(nm, {}).get("type") or "").lower(),
                                          bool(fmap.get(nm, {}).get("multiple")))
            rec["__row_id__"] = ids[i] if i < len(ids) else None
            out.append(rec)
        return out

    # ── 写 ─────────────────────────────────────────────
    def append_row(self, table: str, data: Dict[str, Any]) -> str:
        ids = self.append_rows(table, [data])
        return ids[0] if ids else None

    def append_rows(self, table: str, rows: List[Dict[str, Any]]) -> List[str]:
        """服务端批量新增，200 行/批，**同表串行**（并行会撞飞书并发冲突）。

        ``record_id_list`` 与入参同序；服务端若少回 id，位置补 ``None``
        （宁可为 None 也不错位 —— 错位会把 A 的 id 记到 B 头上）。
        """
        tid = self._table_id(table)
        fmap = self._field_map(tid)
        rows = list(rows or [])
        if not rows:
            return []
        ids: List[str] = []
        for i in range(0, len(rows), _RECORD_BATCH):
            chunk = rows[i:i + _RECORD_BATCH]
            recs = [self._to_record(r, fmap) for r in chunk]
            d = self._call(["+record-batch-create", "--base-token", self.base_token,
                            "--table-id", tid], json_body={"create_records": recs})
            self._check_ignored(d, "批量新增（表 %s）" % table)
            got = [x for x in (d.get("record_id_list") or [])]
            ids.extend(got if len(got) == len(chunk) else [None] * len(chunk))
        return ids

    def update_row(self, table: str, row_id: str, data: Dict[str, Any]) -> None:
        """更新单行。

        ⚠️ 三处与「想当然」不同，都是实测撞出来的：

        1. ``+record-batch-update`` 对不存在的 record_id **返回 rc=0**，只把 id 放进
           ``record_not_found``。只看退出码就会把「没写进去」当成「写好了」。
        2. 新建记录后有**最终一致性窗口**：实测创建后 ~0.6s 更新报 record_not_found，
           ~2.9s 后同一操作成功。所以「建完立刻改」不能当成行不存在 —— 这里做短重试。
        3. 只读列已在 ``_to_record`` 里剔除；若服务端仍回 ``ignored_fields``，
           说明出现了未知的只读字段 → 报错（见 ``_check_ignored``）。
        """
        tid = self._table_id(table)
        rec = self._to_record(data, self._field_map(tid))
        if not rec:
            return                      # 全是只读列/空值 → 没有要写的东西，不是错误
        row_id = str(row_id)
        last_nf = []
        for attempt in range(_SETTLE_RETRIES + 1):
            d = self._call(["+record-batch-update", "--base-token", self.base_token,
                            "--table-id", tid],
                           json_body={"update_records": {row_id: rec}})
            self._check_ignored(d, "更新（表 %s，行 %s）" % (table, row_id))
            nf = [str(x) for x in (d.get("record_not_found") or [])]
            if row_id not in nf:
                return
            last_nf = nf
            if attempt < _SETTLE_RETRIES:
                time.sleep(_SETTLE_SLEEP)     # 等最终一致性窗口过去再试
        raise FeishuError(
            "更新失败：服务端报告 record_not_found（已重试 %d 次共 %.1fs）：%s；"
            "该行可能已被删除，或 row_id 属于别的表"
            % (_SETTLE_RETRIES, _SETTLE_RETRIES * _SETTLE_SLEEP, " / ".join(last_nf)))

    def delete_rows(self, table: str, row_ids: List[str]) -> None:
        """按 id 删除。**必须显式确认**（``--yes``）：飞书把它标为 high-risk-write。"""
        tid = self._table_id(table)
        ids = [str(x) for x in (row_ids or []) if x]
        for i in range(0, len(ids), _RECORD_BATCH):
            chunk = ids[i:i + _RECORD_BATCH]
            d = self._call(["+record-delete", "--base-token", self.base_token,
                            "--table-id", tid, "--yes"],
                           json_body={"record_id_list": chunk})
            nf = list(d.get("record_not_found") or [])
            if nf:
                # 删不掉就是删不掉，不许当成功 —— 上层若据此认为已清理会产生幽灵数据
                raise FeishuError("删除失败：服务端报告以下行不存在或未删除：%s"
                                  % " / ".join(str(x) for x in nf))

    # ── 表结构 ─────────────────────────────────────────
    def ensure_table(self, table: str, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        """建表；返回 ``"created"`` / ``"exists"``。

        ⚠️ 飞书建表的**第一个字段会成为主字段**，而主字段只接受有限的几种类型
        （文本类最稳）。这里如实按传入顺序提交，不偷偷插入占位列 ——
        被服务端拒绝是**立刻可见的硬失败**，比默默多出一列好查。

        ⚠️ ``multiselect`` 需要 ``backend_type`` 之外再带 ``multiple: true``：
        Bitable 单选/多选是**同一个 type** 加一个布尔位，不是两个类型。

        ⚠️ 单选/多选列**必须在这里给全选项**（列定义里的 ``options``，
        元素可以是名字字符串或 ``{"name": ...}``）。实测：建列时不带选项，
        之后写任何值都会得到 ``800030005 not_found``（"Provide an existing option
        value"）——**飞书不会替你新增选项**。所以选项只能建列时一次给全，
        或者事后用 ``tools/verify_feishu.py``/原生 field-update 补。
        """
        if self.table_exists(table):
            return "exists"
        fields = []
        for c in (columns or []):
            nm = c.get("name") if isinstance(c, dict) else c
            nt = (c.get("type") if isinstance(c, dict) else None) or "text"
            if not nm:
                continue
            ftype = schema.backend_type("feishu", nt)
            f = {"name": nm, "type": ftype}
            if ftype == "select":
                f["multiple"] = (str(nt).strip().lower() == "multiselect")
                opts = c.get("options") if isinstance(c, dict) else None
                if opts:
                    f["options"] = [{"name": o} if isinstance(o, str) else dict(o)
                                    for o in opts]
            fields.append(f)
        if not fields:
            raise Unsupported(
                "飞书建表至少要有一个字段（它同时是主字段）。"
                "请传入 columns=[{'name': ..., 'type': 'text'}, ...]")
        self._call(["+table-create", "--base-token", self.base_token, "--name", table],
                   json_body={"name": table, "fields": fields})
        self._invalidate()
        return "created"

    # ── 关联 ───────────────────────────────────────────
    def _link_fields(self, tid: str) -> List[dict]:
        return [f for f in self._fields_of(tid)
                if str(f.get("type") or "").lower() in _LINK_TYPES]

    def link_columns(self, table: str) -> List[str]:
        """该表上所有关联列的名字（诊断用；与 SeaTable 适配器同名同义）。"""
        return [f["name"] for f in self._link_fields(self._table_id(table)) if f.get("name")]

    def _resolve_link_field(self, table: str, other_table: str, link_id: str) -> dict:
        """把 ``link_id`` 解析成一条关联**列**。

        本后端没有 SeaTable 那种 link_id —— ``link_id`` 参数在这里就是**关联列名**
        （可传列 id ``fld...``）。传空串时按对方表名反查；同一对表有多条关联列时
        **必须显式指定**，不许静默取第一条（取错列的后果是「数据挂到了另一条业务线上」，
        不报错、看起来也在，最难排查）。
        """
        tid = self._table_id(table)
        cands = self._link_fields(tid)
        if not cands:
            raise KeyError("表「%s」上没有任何关联列" % table)
        if link_id:
            for f in cands:
                if f.get("name") == link_id or f.get("id") == link_id:
                    return f
            raise KeyError("表「%s」上不存在关联列 %r（现有：%s）"
                           % (table, link_id, " / ".join(f.get("name", "?") for f in cands)))
        other_tid = self._table_id(other_table) if other_table else None
        matched = [f for f in cands if f.get("link_table") == other_tid]
        if len(matched) == 1:
            return matched[0]
        if not matched:
            raise KeyError("表「%s」没有指向「%s」的关联列（现有：%s）"
                           % (table, other_table,
                              " / ".join("%s→%s" % (f.get("name"), f.get("link_table"))
                                         for f in cands)))
        raise KeyError("表「%s」→「%s」之间有 %d 条关联列（%s），必须显式指定 link_id=列名"
                       % (table, other_table, len(matched),
                          " / ".join(f.get("name", "?") for f in matched)))

    def link(self, table: str, other_table: str, link_id: str,
             row_id: str, other_row_ids: List[str]) -> None:
        """双向关联。**飞书做不到时直接拒绝，而不是只写一侧。**

        契约要求「同一 link_id，两张表各记一次」。实测（只读 + 自清理探测）：
        ``bidirectional=false`` 的关联字段在**对方表连反向列都不存在**，
        写一侧就是真的只有一侧 —— 上层拿 ``list_linked(对方表)`` 会读到空，
        于是「建了关联」和「没建」在数据上无法区分。这正是不该悄悄降级的场景，
        所以：单向字段 → 抛 ``Unsupported``，让调用方显式改调 ``link_one_way()``
        或在飞书里把该字段设成双向关联。

        ``bidirectional=true`` 的字段由服务端维护反向列，写一侧即完成两表记账。
        """
        f = self._resolve_link_field(table, other_table, link_id)
        if not f.get("bidirectional"):
            raise Unsupported(
                "飞书关联列「%s.%s」是**单向**的（bidirectional=false），对方表没有反向列，"
                "无法实现双向 link()。请改用 link_one_way()（只写一侧），"
                "或在飞书里把该字段改成双向关联。"
                % (table, f.get("name")))
        self.link_one_way(table, other_table, f["name"], row_id, other_row_ids)

    def link_one_way(self, table: str, other_table: str, link_id: str,
                     row_id: str, other_row_ids: List[str]) -> None:
        """只写 ``table`` 一侧的关联列（设置语义，非追加）。飞书原生行为即如此。"""
        f = self._resolve_link_field(table, other_table, link_id)
        tid = self._table_id(table)
        cell = [{"id": str(x)} for x in (other_row_ids or []) if x]
        # 走 update_row 以复用「只读列剔除 + record_not_found 检查 + 一致性重试」
        self.update_row(table, row_id, {f["name"]: cell})

    def list_linked(self, table: str, row_id: str, link_id: str) -> List[str]:
        """读回某行的关联。

        ``link_id`` 传空串 = 该行**所有**关联列的并集（与 SeaTable 适配器同义）。
        读不到关联列时抛 ``Unsupported``/``KeyError``，**绝不返回空列表冒充「没有」** ——
        本仓库吃过这个亏（SeaTable 的 ``list_linked`` 曾是 ``return []`` 的空桩，
        于是「查关联」永远成功、永远返回空、永远不报错）。行不存在同样抛 ``KeyError``：
        空列表**只**代表「这行确实没有关联」。
        """
        tid = self._table_id(table)
        links = self._link_fields(tid)
        if not links:
            raise KeyError("表「%s」上没有任何关联列" % table)
        if link_id:
            cols = [self._resolve_link_field(table, None, link_id)["name"]]
        else:
            cols = [f["name"] for f in links if f.get("name")]

        def _pull(rec: dict) -> List[str]:
            out: List[str] = []
            for c in cols:
                v = rec.get(c)
                items = v if isinstance(v, list) else ([v] if v else [])
                for x in items:
                    rid = x.get("id") if isinstance(x, dict) else x
                    if rid and str(rid) not in out:
                        out.append(str(rid))
            return out

        rec = self.get_row(table, row_id)
        if rec is None:
            raise KeyError("行不存在：表「%s」row_id=%s" % (table, row_id))
        return _pull(rec)
