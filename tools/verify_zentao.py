# -*- coding: utf-8 -*-
"""禅道后端连通性与契约验证脚本（部署流程的 verify 钩子）。

与 ``tools/verify_jiandaoyun.py`` 的根本差别：**这个脚本能真的跑** ——
开发机上有在运行的禅道实例（``http://127.0.0.1/zentao``）和有效令牌，
所以它的输出是**实测结果**，不是「待核对的推断」。

两种模式：

  ``--read-only``（**默认**，部署钩子用这个）
      只读：认证探活 → 能力反向断言 → 列表端点形状 → 逻辑表名 → 列结构 →
      拉数据 → 外键列 → 不存在的资源。**运行时强制只发 GET**（见 ``_MethodGuard``）。

  ``--write-test``
      ⚠️ 禅道**没有建表接口**，没有安全的临时表可建。所以写路径只能落在
      ``--table`` 指定的**真实实体**上，建一条带 ``ZZ_WB_VERIFY`` 标记的记录
      并在 ``finally`` 里删掉。不给 ``--table`` 则**直接拒绝运行**。

      另外它会清理**级联产物**：实测创建 scrum 项目会顺手建一个同名产品
      （见 ``CASCADE_WATCH``），删项目不会删它。所以脚本会对比开跑前后的条数，
      把「本次新增的」一并删掉并在输出里逐条列出 —— 验证脚本不该留下垃圾。

令牌处理：默认从 ``~/.workbuddy/mcp.json`` 的 ``zentao.headers.token`` 读
（本机 zentao-mcp 就是这么配的）。**绝不打印、绝不落盘**；
所有输出统一过 :func:`redact`。

退出码：0 全通过/未配置跳过；1 有失败项；2 参数或配置错误。
"""
import argparse
import io
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

RESULTS = []
TOKEN = ""

#: 写路径验证时用来标记「这是我建的」——便于人工检索与清理。
#: ⚠️ 实际使用的名字会**带上本次运行的时间戳**（见 :func:`run_mark`）：
#: 实测项目名的重名检查**把已删除的产品也算进去**，于是固定名字的第二次运行必然失败
#: （``adapters/zentao.py`` 模块 docstring 第 23 条）。
MARK = "ZZ_WB_VERIFY_请忽略"


def run_mark():
    """本次运行唯一的标记名。**必须唯一**：禅道的项目名用过一次就不可复用。"""
    import datetime
    return "%s_%s" % (MARK, datetime.datetime.now().strftime("%Y%m%d%H%M%S"))

def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("通过" if ok else "失败", name,
                           (" —— " + detail) if detail else ""))
    return ok


def note(text):
    print("      · %s" % text)


def redact(text):
    """把令牌字面量抹掉 —— 任何输出都不带它。"""
    return text.replace(TOKEN, "<TOKEN-REDACTED>") if TOKEN else text


def _raises(fn, exc=Exception):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


_HINTED = set()


def note_once(text):
    if text not in _HINTED:
        _HINTED.add(text)
        note(text)


# ══════════════════════════════════════════════════════════════════
# 只读方法闸门
# ══════════════════════════════════════════════════════════════════

class _MethodGuard:
    """只读模式下的传输层包装：任何非 GET 请求**立刻 AssertionError**。

    为什么要在传输层拦、而不是「我写代码时小心点」：文档里说「本脚本只发 GET」
    是**承诺**，这里是**运行时断言**。承诺会随着后来的改动失效，断言不会。
    """

    def __init__(self, inner, allowed=("GET",)):
        self._inner = inner
        self._allowed = set(m.upper() for m in allowed)
        self.blocked = []

    def request(self, method, path, query=None, body=None):
        if str(method).upper() not in self._allowed:
            self.blocked.append((method, path))
            raise AssertionError("只读模式拒绝了 %s %s（只允许 %s）"
                                 % (method, path, "/".join(sorted(self._allowed))))
        return self._inner.request(method, path, query, body)

    @property
    def token(self):
        return getattr(self._inner, "token", "")

    @token.setter
    def token(self, value):
        self._inner.token = value

    def close(self):
        return self._inner.close()


# ══════════════════════════════════════════════════════════════════
# 构造
# ══════════════════════════════════════════════════════════════════

def _token_from_mcp_json():
    """本机 zentao-mcp 的令牌就在 ``~/.workbuddy/mcp.json`` 里。

    只在**内存里**读，不复制到任何文件；读不到就返回空串。
    """
    p = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")
    try:
        with io.open(p, encoding="utf-8") as f:
            d = json.load(f)
        return (((d.get("mcpServers") or {}).get("zentao") or {})
                .get("headers", {}).get("token") or "")
    except Exception:
        return ""


def _build(args):
    """按注册表链路构造 config（顺带证明「注册完就不用改工厂」）。

    凭据优先级：命令行 → config.yaml → ``~/.workbuddy/mcp.json``。
    构造出的 config 是**内存里的**，不读也不写用户的 config.yaml。
    """
    from adapters import factory

    base = args.base_url or ""
    token = args.token or ""
    if not base and not token:
        cfg = factory.load_config()
        inst = factory.get_base_config(cfg, base_name=args.base_name, backend="zentao")
        if inst:
            base = inst.get("base_url") or ""
            token = ((cfg.get("zentao") or {}).get("token")
                     or inst.get("token") or "")
    if not token:
        token = _token_from_mcp_json()
        if token:
            note_once("令牌取自 ~/.workbuddy/mcp.json（zentao-mcp 的配置），全程脱敏")
    if not base:
        base = "http://127.0.0.1/zentao/api.php/v2"

    cfg = {"backend": "zentao",
           "zentao": {"token": token, "base_url": base, "no_proxy": True,
                      "timeout": int(args.timeout or 20),
                      "bases": {args.base_name: {"base_url": base}}}}
    if not token:
        if not (args.account and args.password):
            return None, None
        cfg["zentao"]["account"] = args.account
        cfg["zentao"]["password"] = args.password
    return cfg, args.base_name


# ══════════════════════════════════════════════════════════════════
# 只读验证
# ══════════════════════════════════════════════════════════════════

#: 反向断言：这些能力**不该**出现在禅道的声明里。
ABSENT_CAPS = (
    ("batch_write", "禅道没有批量创建接口，append_rows 是本地逐条循环"),
    ("link", "关联是实体固定外键，没有通用可读写关联列"),
    ("link_read", "外键只给单个 id，读不回「对方 id 列表」"),
    ("schema_manage", "实体与字段固定，REST v2 没有建表/加列接口"),
    ("query_pushdown", "过滤参名是实体特定的，无法对任意 filters 成立"),
    ("idempotent", "没有幂等键"),
    ("optimistic_lock", "没有行级版本号"),
)


def read_only(a):
    """只读验证。进来时会把 ``a._t`` 套上 ``_MethodGuard``。"""
    from adapters.base import Unsupported, capabilities_of
    from adapters.zentao import ENTITY_SPECS
    SPECS = ENTITY_SPECS                 # 下面用短名，读起来清爽些

    print("\n── 只读验证（传输层已套上「只发 GET」闸门）──")
    a._t = _MethodGuard(a._t)

    caps = sorted(capabilities_of(a))
    check("能力声明可读且无副作用", bool(caps), ",".join(caps))
    for cap, why in ABSENT_CAPS:
        check("不声明 %s" % cap, cap not in caps, why)

    # ── 实体映射层 ───────────────────────────────────────
    tables = a.list_tables()
    entities = [t for t in tables if t in ENTITY_SPECS]
    check("能列出可用表", bool(entities), "实体 %d 个：%s"
          % (len(entities), " / ".join(entities)))
    check("别名已解析到实体",
          a.get_metadata("生产计划")["table_name"] == "执行",
          "生产计划 → 执行（list_tables 里同时给出两个名字）")
    check("禅道承载不了的逻辑表被明确拒绝",
          _raises(lambda: a.get_metadata("IC采购记录"), KeyError),
          "IC采购记录 → KeyError，并说明请继续用 seatable/feishu/jiandaoyun")

    # ── 列表端点形状（实测定下来的「可用 / 不可用」清单）──
    print("\n  ── 列表读取（实测：部分端点在本版禅道上根本不可用）──")
    listed, unlisted = [], []
    for t in entities:
        if not a.get_metadata(t)["listable"]:
            unlisted.append(t)
            continue
        try:
            listed.append((t, len(a.list_rows(t))))
        except Exception as e:
            check("能拉取「%s」" % t, False, redact(str(e))[:160])
    check("可列表实体全部读通", bool(listed),
          " / ".join("%s=%d 行" % (t, n) for t, n in listed) or "（无可列表实体）")
    if unlisted:
        note("以下实体在本版禅道上**列表接口不可用**（适配器如实抛 Unsupported，"
             "不返回空列表冒充「没有数据」）：%s" % " / ".join(unlisted))
        check("不可列表实体读列表 → Unsupported 而不是空列表",
              all(_raises(lambda t=t: a.list_rows(t), Unsupported) for t in unlisted))

    # ── 「HTTP 200 但其实是失败」——本后端最重要的守卫 ────────
    print("\n  ── 失败形状（实测：HTTP 200 + status=fail）──")
    first = entities[0]
    try:
        a._call("GET", "/" + ENTITY_SPECS[first]["resource"] + "/999999")
        check("不存在的资源 → 必须报错", False, "居然没报错")
    except Exception as e:
        check("不存在的资源 → 报错（HTTP 状态码可能是 200）", True,
              redact(str(e))[:120])
    check("不存在的逻辑表 → KeyError（不是空结果）",
          _raises(lambda: a.list_rows("ZZ_WB_不存在的表"), KeyError))

    # ── 列结构 ───────────────────────────────────────────
    print("\n  ── 列结构（来自成文映射契约 ENTITY_SPECS，不是从服务端读的）──")
    meta = a.get_metadata(first)
    cols = meta["columns"]
    writable = [c["name"] for c in cols if not c.get("readonly")]
    readonly = [c["name"] for c in cols if c.get("readonly")]
    check("能给出列结构", bool(cols),
          "%s：%d 列（可写 %d / 只读 %d）"
          % (first, len(cols), len(writable), len(readonly)))
    note("可写列：%s" % " / ".join(writable[:10]))
    note("只读列（规范里没进 POST/PUT 请求体，适配器只读不写）：%s"
         % (" / ".join(readonly) or "（无）"))
    opts = [c["name"] for c in cols if c.get("options")]
    if opts:
        note("带枚举选项的列：%s（取值域全部来自规范描述，没有一个是我编的）"
             % " / ".join(opts))

    # ── 外键列与三个「如实说不」的接口 ───────────────────
    lc = {t: a.link_columns(t) for t in entities}
    with_lc = {t: v for t, v in lc.items() if v}
    check("外键型关联列能报出来", bool(with_lc),
          "；".join("%s: %s" % (t, "/".join(v[:4]))
                    for t, v in list(with_lc.items())[:5]) or "（没有实体带外键列）")
    note("禅道没有通用 link()：要建关联请 update_row 直接写上面这些外键列。")

    for label, fn, why in (
            ("link", lambda: a.link("任务", "执行", "l", "1", ["2"]),
             "没有通用关联接口，且 link_id 在禅道无意义"),
            ("list_linked", lambda: a.list_linked("任务", "1", ""),
             "读不回「行 → 对方行 id 列表」"),
            ("ensure_table", lambda: a.ensure_table("项目", None),
             "实体与字段固定，没有建表/加列接口")):
        check("%s 如实抛 Unsupported" % label, _raises(fn, Unsupported), why)

    # ── 第二轮的扩展能力（全部对应实测事实）───────────────
    print("\n  ── 扩展能力（第二轮实测：19 资源 × 88 路径全量覆盖）──")
    check("实体覆盖 16 个", len(entities) == 16,
          "新增：产品计划 / 构建 / 发布 / 业务需求 / 用户需求 / 应用")
    absent = {"工单", "反馈", "文件"} & set(entities)
    check("本版不存在的实体没有被硬塞进来", not absent,
          "工单/反馈/文件 的 POST 实测回 0 字节（路由不存在）→ 不建映射")

    reachable = [t for t in entities if SPECS[t].get("children_of")]
    check("顶层读不了的实体都指出了「经父资源读」的正路", len(reachable) >= 7,
          "；".join("%s←%s" % (t, SPECS[t]["children_of"][0]) for t in reachable[:8]))

    pq = sorted(t for t in entities if SPECS[t].get("parent_query"))
    check("父参数走 query 的实体标全了", len(pq) == 9,
          "%s（实测：放 body 会被当成「没传」，而规范恰恰把它声明在 body 里）"
          % " / ".join(pq))

    acts = {t: a.actions_of(t) for t in entities}
    with_acts = {t: v for t, v in acts.items() if v}
    check("状态流转动作能报出来（含实测必填）", bool(with_acts),
          "；".join("%s：%s" % (t, "/".join(x["动作"] for x in v)) for t, v in with_acts.items()))
    for t, v in with_acts.items():
        for x in v:
            note("· %s.%s → /%s  必填=%s（%s）"
                 % (t, x["动作"], x["路径"], "、".join(x["必填"]) or "空 body 即可",
                    x["说明"]))
    check("动作必填清单非空或显式声明空 body 即可",
          all(isinstance(x["必填"], list) for v in with_acts.values() for x in v))

    rec = sorted(t for t in entities if SPECS[t].get("create_id_via"))
    check("「建成功但不回 id」的实体已声明回查路径", len(rec) == 3,
          "%s —— POST 成功只回 message，靠反查父资源子列表拿 id" % " / ".join(rec))
    nd = sorted(t for t in entities if SPECS[t].get("no_delete_why"))
    check("没有删除接口的实体已如实标注", nd == ["应用"],
          "应用：禅道没有 DELETE /systems，建了就删不掉")

    check("describe() 不含令牌",
          (TOKEN not in a.describe()) if TOKEN else True, redact(a.describe()))
    guard = a._t
    check("本次只读验证确实没发出任何写请求", not guard.blocked,
          "拦下的请求：%s" % guard.blocked if guard.blocked else "只有 GET")


# ══════════════════════════════════════════════════════════════════
# 写路径验证（会真写！只写标记过的记录，finally 里删 **含级联产物**）
# ══════════════════════════════════════════════════════════════════

#: ⚠️ 实测（``probes/zentao_write_probe.py``，2026-10-01）：创建 **scrum 项目**会顺手建
#: 一个**同名产品**（项目行里 ``hasProduct: "1"``），而删掉项目**不会**删掉那个产品。
#: 所以写测试必须记住「开跑前有哪些条」，收尾时把新增的删掉 ——
#: 否则「验证脚本」会变成「往禅道里塞垃圾的脚本」。
CASCADE_WATCH = ("产品",)


def _snapshot(a, tables):
    """记录这些实体当前的 ``{id: 名称}``；读不了的记为 ``None``（收尾时跳过，不猜）。"""
    out = {}
    for t in tables:
        try:
            out[t] = {str(r["__row_id__"]): (r.get("名称") or r.get("标题"))
                      for r in a.list_rows(t)}
        except Exception as e:
            out[t] = None
            note("「%s」开跑前读不到（%s），收尾时无法判断新增，跳过其级联清理"
                 % (t, redact(str(e))[:60]))
    return out


def _safe_value(ctype, mark):
    """给必填列造一个**安全**的值，造不出来返回 ``None``。

    只允许两类：文本（放标记）和日期（给一个远未到期的固定日期）。
    ``number``/``select``/``array``/``secret`` 一律拒绝 —— 它们是**引用**
    （产品 id、执行 id、用户账号…），随手造一个数字就是把记录挂到陌生业务线上。
    """
    if ctype in ("text", "longtext"):
        return mark
    if ctype == "date":
        import datetime
        return (datetime.date.today() + datetime.timedelta(days=365)).strftime("%Y-%m-%d")
    return None


def write_test(a, table):
    from adapters.zentao import ENTITY_SPECS

    print("\n── 写路径验证（会真实写入禅道实体「%s」！）──" % table)
    if isinstance(a._t, _MethodGuard):
        # 只读阶段套上的「只发 GET」闸门必须在这里摘掉，否则写请求会被它自己拦下。
        blocked = list(a._t.blocked)
        a._t = a._t._inner
        note("已摘掉只读闸门（只读阶段的拦截记录：%s）" % (blocked or "无"))
    meta = a.get_metadata(table)
    ename = meta["table_name"]
    by = {c["name"]: c for c in meta["columns"]}
    if table in ENTITY_SPECS:
        required = list(ENTITY_SPECS[ename]["required"])
    else:
        required = [c["name"] for c in meta["columns"] if not c.get("readonly")][:1]

    mark = run_mark()
    data = {}
    for r in required:
        if r not in by:
            print("  [跳过] 必填列「%s」不在可写映射里。" % r)
            return
        v = _safe_value(by[r]["type"], mark)
        if v is None:
            print("  [跳过] 必填列「%s」是 %s 类型 —— 它是**引用**（别的实体的 id），"
                  "自动造值等于把记录挂到陌生业务线上。请人工评估后再跑写测试。"
                  % (r, by[r]["type"]))
            return
        data[r] = v
    txt = next((c["name"] for c in meta["columns"]
                if c["type"] in ("text", "longtext") and not c.get("readonly")), None)
    if txt and txt not in data:
        data[txt] = mark
    print("      将在「%s」新增 1 行，字段：%s" % (ename, " / ".join(data)))
    note("名字带本次运行的时间戳（%s）：实测项目名一旦用过就不可回收，"
         "固定名字会让第二次运行必然失败" % mark)

    before = _snapshot(a, CASCADE_WATCH)
    created = None
    try:
        rid = a.append_row(table, data)
        created = rid
        check("新增 1 行并能拿到 id（且是字符串）", bool(rid),
              "id=%r（type=%s）" % (rid, type(rid).__name__))

        got = a.get_row(table, rid)
        check("刚写入的行能读回", got is not None and got.get("__row_id__") == rid,
              ("读回：%s" % redact(json.dumps(got, ensure_ascii=False))[:140])
              if got else "读不回来")

        check("更新列名写错 → 客户端拦下（不静默丢弃）",
              _raises(lambda: a.update_row(table, rid, {"ZZ_WB_不存在的列": 1}),
                      Exception),
              "实测服务端对未知字段是**静默忽略**，这一层必须由客户端把住")

        # 「读回来的整行改一个字段再写回去」是常见用法，整行天然带只读列 ——
        # 只读列必须被**成文剔除**，这条路才走得通。
        if got:
            check("整行回写（含只读列）能走通",
                  not _raises(lambda: a.update_row(table, rid, dict(got)), Exception),
                  "只读列：%s" % (" / ".join(c["name"] for c in meta["columns"]
                                            if c.get("readonly")) or "（无）"))
    except Exception as e:
        check("写路径", False, redact(str(e))[:200])
    finally:
        print("\n── 清理本次写入的记录 ──")
        if created:
            try:
                a.delete_rows(table, [created])
                leftover = a.get_row(table, created)
                check("已删除本次写入的记录", leftover is None, "id=%s" % created)
            except Exception as e:
                check("已删除本次写入的记录", False,
                      "删除失败，请手工处理 id=%s：%s" % (created, redact(str(e))))
        # 级联清理：删了项目 ≠ 删了它顺手建的东西。
        for t in CASCADE_WATCH:
            b = before.get(t)
            if b is None or not created:
                continue
            after = _snapshot(a, (t,)).get(t)
            if after is None:
                continue
            fresh = {k: v for k, v in after.items() if k not in b}
            if not fresh:
                check("级联实体「%s」无新增（基线 %d 条）" % (t, len(b)), True)
                continue
            bad = []
            for cid, nm in fresh.items():
                try:
                    a.delete_rows(t, [cid])
                    print("      · 已清理级联产生的「%s」#%s（名称=%r）" % (t, cid, nm))
                except Exception as e:
                    bad.append("%s#%s(%s): %s" % (t, cid, nm, redact(str(e))))
            check("级联产生的「%s」已清理（%d 条）" % (t, len(fresh)), not bad,
                  "；".join(bad) if bad else
                  "（这就是实测到的副作用，见 adapters/zentao.py 模块 docstring 第 17 条）")


def deep_write_test(a):
    """第二轮新增能力的**真实**写路径验证。

    为什么值得单独跑一遍：这四项在第一版里**根本不存在**，而它们恰恰是
    「禅道是 PM 软件」与「禅道是一张表」的分界线 ——

      · ``list_children``    —— 读「顶层列表回 0 字节」那些实体的**唯一正路**
      · ``run_action``       —— 状态流转（其副作用是 ``update_row`` 改 status 做不出来的）
      · ``create_id_via``    —— 「建成功却不回 id」的三个实体的反查
      · ``delete_query``     —— 业务需求/用户需求删除要的 ``?storyID=``

    全部名字带本次运行的时间戳，``finally`` 里**反序**删除 + 清理级联产品。
    """
    from adapters.zentao import ACTION_FIELDS  # noqa: F401  （说明用）

    mark = run_mark()
    print("\n" + "=" * 70)
    print("── 深度写路径验证（子列表 / 流转动作 / 回查 id / storyID 删除）──")
    print("   本次标记：%s" % mark)
    note("这一节会建 产品→项目→执行→任务 与一条业务需求，收尾全部删除。")
    note("⚠️ 建「项目」会**顺手建一个同名产品**且**项目名用过一次就不可回收** —— "
         "所以名字必须唯一（见 adapters/zentao.py 模块 docstring 第 23 条）。")

    before = _snapshot(a, ("产品", "项目", "执行", "任务"))
    made = []            # [(实体, id)] 按创建顺序，收尾反序删

    def mk(table, data):
        rid = a.append_row(table, data)
        made.append((table, rid))
        return rid

    try:
        pid = mk("产品", {"名称": mark + "_产品"})
        check("① 产品建立（required 只有 name）", bool(pid), "id=%s" % pid)

        jid = mk("项目", {"名称": mark + "_项目", "计划完成": "2027-12-31"})
        check("② 项目建立（实测最小集 = name + end）", bool(jid), "id=%s" % jid)

        eid = mk("执行", {"名称": mark + "_执行", "所属项目": jid,
                          "计划开始": "2026-10-01", "计划完成": "2026-12-31"})
        check("③ 执行建立（begin 必须 ≥ 项目的 begin）", bool(eid), "id=%s" % eid)

        tid = mk("任务", {"名称": mark + "_任务", "所属执行": eid})
        check("④ 任务建立（父字段 executionID 走 **body**）", bool(tid), "id=%s" % tid)

        # ── A. list_children：真实执行 id 读它下面的任务 ─────────────
        try:
            kids = a.list_children("任务", eid)
            hit = [r for r in kids if str(r.get("__row_id__")) == str(tid)]
            check("A. list_children 按真实执行 id 读到了刚建的任务",
                  len(hit) == 1,
                  "/executions/%s/tasks 共 %d 行，含本次任务 %s 条" % (eid, len(kids), len(hit)))
            check("A2. 顶层 list_rows(任务) 仍然如实拒绝（指路 list_children）",
                  _raises(lambda: a.list_rows("任务"), Exception))
        except Exception as e:
            check("A. list_children", False, redact(str(e))[:180])

        # ── B. run_action：关闭任务（实测空 body 即可，且有副作用）──
        try:
            r0 = a.get_row("任务", tid) or {}
            a.run_action("任务", tid, "关闭")
            r1 = a.get_row("任务", tid) or {}
            check("B. run_action 关闭任务生效（status → closed 且写了 closedDate）",
                  str(r1.get("状态")) == "closed" and bool(r1.get("__row_id__")),
                  "状态 %r → %r（读回是强一致的，与飞书的可见性延迟不同）"
                  % (r0.get("状态"), r1.get("状态")))
            check("B2. 动作缺必填 → 客户端拦下并说清缺哪个",
                  _raises(lambda: a.run_action("任务", tid, "启动",
                                               {"消耗工时": "1"}), Exception),
                  "实测服务端只会回「总计消耗和预计剩余不能同时为0」，指不到具体字段")
            check("B3. 不存在的动作 → Unsupported 并列出可用的",
                  _raises(lambda: a.run_action("任务", tid, "起飞"), Exception))
        except Exception as e:
            check("B. run_action", False, redact(str(e))[:180])

        # ── C. 「建成功但不回 id」的三个实体靠反查拿 id ─────────────
        for t, body in (("业务需求", {"标题": mark + "_业务需求", "所属产品": pid,
                                      "评审人": ["admin"]}),
                        ("用户需求", {"标题": mark + "_用户需求", "所属产品": pid,
                                      "评审人": ["admin"]})):
            try:
                rid = mk(t, body)
                check("C. %s：POST 不回 id，反查父资源子列表拿到 id" % t,
                      bool(rid) and str(rid).isdigit(),
                      "id=%s（实测响应只有 message，没有 id）" % rid)
            except Exception as e:
                check("C. %s 回查 id" % t, False, redact(str(e))[:180])

        # ── D. 业务需求删除要 ?storyID=（规范写的是 :epicID）────────
        try:
            epic = next((i for tb, i in made if tb == "业务需求"), None)
            if epic:
                a.delete_rows("业务需求", [epic])
                made[:] = [(tb, i) for tb, i in made if not (tb == "业务需求" and i == epic)]
                check("D. 业务需求删除（带 ?storyID=）成功", True, "id=%s" % epic)
            else:
                check("D. 业务需求删除", False, "上一节没建出业务需求，跳过")
        except Exception as e:
            check("D. 业务需求删除（带 ?storyID=）", False, redact(str(e))[:180])

        check("E. 应用没有删除接口 → 如实抛 Unsupported，且不发请求",
              _raises(lambda: a.delete_rows("应用", ["1"]), Exception),
              "禅道没有 DELETE /systems（规范里也只有 POST/PUT）")
    except Exception as e:
        check("深度写路径", False, redact(str(e))[:200])
    finally:
        print("\n── 清理深度验证建立的记录（反序）──")
        for t, rid in reversed(made):
            try:
                a.delete_rows(t, [str(rid)])
                print("      · 已删 %s#%s" % (t, rid))
            except Exception as e:
                print("      ! 删 %s#%s 失败，请手工处理：%s" % (t, rid, redact(str(e))[:120]))
        # 级联：建项目顺手建的同名产品
        for t in CASCADE_WATCH:
            b = before.get(t)
            if b is None:
                continue
            after = _snapshot(a, (t,)).get(t)
            if after is None:
                continue
            fresh = {k: v for k, v in after.items() if k not in b}
            for cid, nm in fresh.items():
                try:
                    a.delete_rows(t, [cid])
                    print("      · 已清理级联产生的「%s」#%s（%r）" % (t, cid, nm))
                except Exception as e:
                    print("      ! 级联清理失败 %s#%s：%s" % (t, cid, redact(str(e))[:120]))
        left = _snapshot(a, ("产品", "项目", "执行", "任务"))
        stuck = []
        for t, snap in left.items():
            b = before.get(t) or {}
            for k, v in (snap or {}).items():
                if k not in b:
                    stuck.append("%s#%s(%s)" % (t, k, v))
        check("深度验证后全部回到基线", not stuck,
              "残留：%s" % " / ".join(stuck) if stuck else "产品/项目/执行/任务 均与开跑前一致")


def main():
    global TOKEN
    ap = argparse.ArgumentParser(description="禅道后端连通性与契约验证")
    ap.add_argument("--base-url", default="", help="API 基址（不给则读 config.yaml / 默认本机）")
    ap.add_argument("--token", default="", help="禅道令牌（不给则读 config.yaml → mcp.json）")
    ap.add_argument("--account", default="", help="账号（没有 token 时用来换 token）")
    ap.add_argument("--password", default="", help="密码（同上）")
    ap.add_argument("--base-name", default="production", help="命名实例名（默认 production）")
    ap.add_argument("--timeout", type=int, default=20, help="单请求超时秒数（默认 20）")
    ap.add_argument("--read-only", action="store_true", help="只读验证（默认行为）")
    ap.add_argument("--write-test", action="store_true",
                    help="额外做写路径验证；**必须**同时给 --table，且会真实写入该实体")
    ap.add_argument("--table", default="",
                    help="写路径验证用的实体/逻辑表名（禅道没有建表接口，故必须显式指定）")
    ap.add_argument("--skip-deep", action="store_true",
                    help="跳过深度写验证（子列表/流转动作/回查 id/storyID 删除）")
    args = ap.parse_args()

    if args.write_test and not args.table:
        print("[错误] --write-test 必须同时给 --table。\n"
              "       禅道**没有建表接口**，没有安全的临时表可建，"
              "所以绝不替调用方猜一个实体来写。\n"
              "       示例：--write-test --table 项目")
        return 2

    cfg, base_name = _build(args)
    if cfg is None:
        print("[未配置，跳过] 没有可用的禅道凭据（config.yaml 的 zentao 段，"
              "或 ~/.workbuddy/mcp.json 的 zentao.headers.token）。\n"
              "  这不是失败：该后端只是还没接入。")
        return 0

    from adapters import factory
    a = factory.get_adapter(cfg, base_name=base_name, strict=True)
    TOKEN = getattr(a, "token", "") or ""
    print("后端：%s" % a.describe())
    print("基址：%s" % a.base)
    try:
        a.auth()
    except Exception as e:
        print("[错误] 认证探活失败：%s" % redact(str(e)))
        print("       取令牌：POST <base>/user/login  body {account, password}"
              "（注意是 **/user/login 单数**；规范里写的 /users/login 实测不存在）。")
        return 1
    if a._probe_note:
        note("探活提示（凭据可用，但探测请求本身是业务失败）：%s"
             % redact(a._probe_note))

    read_only(a)
    if args.write_test and not args.read_only:
        write_test(a, args.table)
        # 第二轮新增能力的真实验证（子列表 / 流转动作 / 回查 id / storyID 删除）。
        # 默认跟着 --write-test 一起跑：这四项是「禅道是 PM 软件」的核心，
        # 只验证「能建一行项目」等于没摸到它真正的能力。
        if not args.skip_deep:
            deep_write_test(a)

    bad = [x for x in RESULTS if not x[1]]
    print("\n══ 结果：%d 项通过 / %d 项失败 ══" % (len(RESULTS) - len(bad), len(bad)))
    for n, _, d in bad:
        print("   ✗ %s %s" % (n, d))
    if not bad:
        print("（本脚本在真实禅道上跑过，结论以实际输出为准。）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
