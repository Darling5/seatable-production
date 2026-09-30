# -*- coding: utf-8 -*-
"""简道云开放平台（API v5）适配器。

⚠️ **本文件未在真实环境验证过。**
与 ``adapters/feishu.py``（每条协议事实都来自只读/自清理实测）相反，本机没有
简道云账号，因此这里每一条协议细节都**只来自官方文档**，没有一条经过实测。
**不要把本文的任何结论当实测结论引用。** 首次对接真实环境时请先跑
``python tools/verify_jiandaoyun.py --read-only``（只读、不发写请求），
拿实际输出逐条核对下面这些推断。

文档来源
────────
  · ``hc.jiandaoyun.com/open/10993``  表单和数据接口（接口清单、写入包装、时间格式限制、系统字段）
  · ``hc.jiandaoyun.com/open/12320``  字段类型 ↔ 数据类型对照表
  · ``hc.jiandaoyun.com/open/12063``  时区转换。官方原文：「简道云里面的日期时间采用 UTC 标准时间，
    由于中国是东八区，所以看到的 ``2020-04-14T07:32:22.000Z`` 会比 ``2020-04-14 15:32:22`` 慢 8 小时」

来自文档、且**直接影响正确性**的几条（有依据，但都未实测）
──────────────────────────────────────────────────────────
1. 统一地址 ``POST https://api.jiandaoyun.com/api/v5/<path>``，鉴权
   ``Authorization: Bearer <APIKey>``；错误是 **HTTP 400 + ``{code, msg}``**。
2. 写入包装：每个字段形如 ``{"_widget_xxx": {"value": <值>}}``。字段名建好后固定为
   ``_widget_xxx``，**但设了别名之后全 API 都用别名** —— 所以本适配器一律以
   ``widget/list`` 返回的 ``name`` 作为读写键，**绝不硬编码 ``_widget_`` 前缀**。
3. ⚠️ **时间字段是 UTC**（见上）。因此日期扁平化**必须做 UTC→东八区转换**，
   不能像 SeaTable / 飞书那样直接 ``v[:10]`` 截断 —— 截断在跨日时会给**早一天**
   的日期，整批记录静默算错（见 ``_from_utc`` 的 docstring）。
4. ⚠️ **写时间时，不允许的格式会被服务端静默转成空值**（官方原文：「注:不允许的输入
   会转为空值传入」）。这是本后端最像「看起来成功」的一处：日期格式写错不报错，
   只是悄悄变成空。适配器因此在**发送前就校验并拒绝**不支持的格式（``_to_utc``），
   并在 ``update_row`` 里做**回显校验**兜底。
5. ⚠️ **成员/部门控件只接受 id**，且官方明说「如果 id 不存在，API 不会因此报错，
   即使该字段为必填也会留空」—— 又一处静默失败。适配器**不猜**这类控件的写入形状，
   直接抛 ``Unsupported``（见 ``_UNVERIFIABLE_WIDGETS``）。
6. 批量接口一律 **≤100/批**：``data/batch_create`` / ``data/batch_update`` / ``data/batch_delete``。
7. ``data/list`` 用 ``data_id`` 游标翻页（= 上一页最后一条的 ``_id``），
   **始终按 data_id 正序**，``limit`` 1~100（默认 10）。
8. ``data/batch_update`` 是「把**多条**改成**同一个固定值**」，不是「每条各自的值」。
   这与本仓库 ``update_row(table, row_id, data)`` 的契约不同，所以 ``update_row``
   走**单条** ``data/update``，不用批量那个。
9. 系统字段（``_id`` / ``createTime`` / ``updateTime`` / ``creator`` / ``updater`` /
   ``deleter`` / ``flowState`` / ``appId`` / ``entryId`` / ``ext``）**不支持写入**。

刻意不声明 / 刻意不实现的（不是漏了）
──────────────────────────────────────
· **不声明 ``link`` / ``link_read``**：官方开放接口**没有**关联数据的写入接口；
  ``lookup``（关联查询）只是把对方字段的**显示文本**读出来，拿不到对方记录的
  ``data_id``。所以既写不了、也读不回「对方 row_id」。按契约铁律，做不到就抛
  ``Unsupported``，**绝不返回空列表冒充「没有关联」**。
· **不声明 ``schema_manage``**：开放接口只提供字段**只读**清单（``widget/list``），
  没有建表/加列接口 —— 表结构必须在简道云里预建。这正是 ``base.py`` 里点名的
  「只读字段接口」那一类，``ensure_table`` 因此抛 ``Unsupported``。
· **不声明 ``query_pushdown``**：``data/list`` 的 ``filter`` 确实能在服务端过滤，
  但 ``cond[].type`` 的取值域文档没写清楚，无法对任意 ``filters`` 无条件成立。
  宁可让 ``query()`` 走基类的内存过滤，也不声明一个「有时成立」的能力。
· **不声明 ``idempotent``**：``batch_create`` 收 ``transaction_id``（1 小时内幂等），
  但那是**调用方给的**幂等键；适配器不自己造一个，声明了就等于承诺没兑现的东西。
· **不声明 ``optimistic_lock``**：没有行级版本号。

首次对接真实简道云时的第一步
────────────────────────────
``python tools/verify_jiandaoyun.py --read-only``。它会逐条打印实际行为，
请拿输出**逐条核对**上面这些文档推断；对不上的地方，本文件就要按实测改写
（这正是 ``feishu.py`` 的成文方式：先实测，再把结论写进 docstring）。
"""
import datetime
import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

from . import schema
from .base import (CAP_BATCH_WRITE, CAP_DELETE, CAP_READ, CAP_SERVER_ROW_ID,
                   CAP_UPDATE, CAP_WRITE, BaseAdapter, Unsupported)

#: 官方硬限制：批量接口一律 ≤100/批。
_BATCH = 100

#: ``data/list`` 单页上限（``limit`` 允许 1~100，默认只有 10，所以要显式给）。
_PAGE = 100

#: 默认服务地址（私有部署时由 config 覆盖；若服务端带路径前缀，也能原样拼）。
_DEFAULT_SERVER = "https://api.jiandaoyun.com"

#: 时间一律以东八区呈现／解读（下游全部按北京时间墙上时间写数据）。
_TZ_CN = datetime.timezone(datetime.timedelta(hours=8))
_TZ_UTC = datetime.timezone.utc

#: 系统字段 name → 中文名。文档明确「系统字段不支持写入」，故写入时一律跳过。
#: ⚠️ 未验证：``widget/list`` 的 ``sysWidgets`` 只给 ``name``，中文名是这里补的。
_SYS_FIELDS = {
    "appId": "应用id", "entryId": "表单id", "_id": "数据id", "ext": "扩展字段",
    "createTime": "创建时间", "updateTime": "更新时间", "creator": "创建人",
    "updater": "修改人", "deleter": "删除人", "flowState": "流程状态",
}

#: 系统字段的中文名集合。写入时按**两种写法都得认**：调用方既可能给 API 键
#: （``createTime``），也可能给上游读出来的中文名（``创建时间``）。
#: 只认一种的话，另一种会被当成「列不存在」直接报错 —— 那是误报，
#: 因为「读回来的整行再写回去」是最常见的使用方式。
_SYS_LABELS = frozenset(_SYS_FIELDS.values())

#: 只在服务端算出来的控件：**写入无意义**，客户端先剔掉（成文转换，不是静默丢弃）。
#:   · sn            流水号（服务端自增）
#:   · lookup        关联查询（把对方字段的显示文本算出来）
#:   · aggregation / aggregationtext  聚合
_READONLY_WIDGETS = frozenset({"sn", "lookup", "aggregation", "aggregationtext"})

#: 标量文本类控件：读 → String，写 → 字符串。
_SCALAR_WIDGETS = frozenset({"text", "textarea", "radiogroup", "combo", "lookup"})

#: 数组文本类控件：读 → Array<String>，写 → Array<String>。
_ARRAY_WIDGETS = frozenset({"checkboxgroup", "combocheck"})

#: 数值类控件。
_NUMBER_WIDGETS = frozenset({"number", "aggregation"})

#: 时间类控件（UTC 语义，读写都要转换）。
_TIME_WIDGETS = frozenset({"datetime"})

#: ⚠️ **写入形状无法从文档确认**的控件。读的时候原样带出（不丢信息），
#: 写的时候**直接拒绝**。
#:
#: 为什么拒绝而不是「试着写写看」：官方已经明确成员/部门控件「id 不存在不报错、
#: 必填也会留空」，而 ``subform`` / ``upload`` / ``location`` 等复合形状文档只给了
#: 数据类型、没给可照抄的写入样例。在没有实例可验的情况下硬写，最可能的结果是
#: **数据被悄悄写空且不报错** —— 那比一个说得清楚的 Unsupported 危险得多。
#: 真要写这类控件，请在真实环境验证形状后，把该类型从这里移出去并补测试。
_UNVERIFIABLE_WIDGETS = frozenset({
    "user", "usergroup", "dept", "deptgroup", "subform", "image", "upload",
    "signature", "address", "location", "richtext", "phone", "linkdata",
})

#: 控件类型 → 中立类型（``adapters/schema.py`` 的词表），供 ``get_metadata`` 附带。
_WIDGET_TO_NEUTRAL = {
    "text": "text", "textarea": "longtext", "number": "number",
    "datetime": "datetime", "radiogroup": "select", "combo": "select",
    "checkboxgroup": "multiselect", "combocheck": "multiselect",
    "upload": "attachment", "image": "attachment",
}

#: 带时区标注的时间串（``Z`` / ``±HH:MM``）由 ``_parse_zoned`` 处理；
#: 下表是**无时区标注**的形式，一律按东八区墙上时间解读。
_RE_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_RE_DATETIME = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$")
#: 小数秒位数归一化（官方 rfc3339 样例里有 9 位的 ``.767135400``，
#: 而 ``fromisoformat`` 在部分 Python 版本上只吃 3/6 位）。
_RE_FRACTION = re.compile(r"\.(\d{1,9})")


class JiandaoyunError(RuntimeError):
    """简道云调用失败。带服务端 ``code`` / ``msg``，便于按错误码定位。"""

    def __init__(self, message: str, code=None, msg=None, raw=None):
        super().__init__(message)
        self.code = code
        self.msg = msg or message
        self.raw = raw or {}


# ══════════════════════════════════════════════════════════════════
# 时间转换（本后端最容易「悄悄出错」的地方）
# ══════════════════════════════════════════════════════════════════

def _parse_local(s: str):
    """解析**无时区标注**的时间串，按东八区墙上时间理解；解析不了返回 None。"""
    m = _RE_DATE.match(s)
    if m:
        try:
            return datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                     tzinfo=_TZ_CN)
        except ValueError:
            return None
    m = _RE_DATETIME.match(s)
    if m:
        g = m.groups()
        try:
            return datetime.datetime(
                int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]), int(g[5] or 0),
                int((g[6] or "0").ljust(6, "0")), tzinfo=_TZ_CN)
        except ValueError:
            return None
    return None


def _parse_zoned(s: str):
    """解析**带时区标注**（``Z`` / ``±HH:MM``）的时间串；没有标注返回 None。

    先把小数秒归一到 6 位：官方给的可写格式样例 ``'2020-06-04 14:41:54.767135400+08:00'``
    带 9 位小数秒，而 ``datetime.fromisoformat`` 在部分 Python 版本上只接受 3/6 位 ——
    直接喂进去会把一个**官方声明支持**的格式误判成不支持。
    """
    t = _RE_FRACTION.sub(lambda m: "." + m.group(1).ljust(6, "0")[:6], s, count=1)
    if t[-1:] in ("Z", "z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(t)
    except Exception:
        return None
    return dt if dt.tzinfo else None


def _to_utc(value) -> str:
    """下游值 → 简道云要求的 RFC3339 **UTC** 字符串。

    接受的形式（官方列出的可写格式 + 本仓库下游的习惯写法）：
      · ``datetime`` / ``date`` 对象（naive 一律按东八区理解）
      · 毫秒时间戳（int / float）
      · ``YYYY-MM-DD``                        → 东八区 00:00
      · ``YYYY-MM-DD HH:MM(:SS[.ffffff])``    → 东八区
      · ISO / RFC3339：``…Z`` 或 ``…+08:00`` 按标注的时区；**无标注**的
        ``YYYY-MM-DDTHH:MM:SS`` 按东八区理解

    ⚠️ 解析不了就**抛错**，绝不原样放行：官方明说「不允许的输入会转为空值传入」，
    放行等于把「日期格式写错了」变成「日期悄悄没了」，而且不报错。
    像 ``2021/10/10 10:10:10``（斜杠）这种最常见的误写，就在这条上被拦下来。

    ⚠️ 未验证：官方把无时区的 ``'2018-11-09T10:00:00'`` 也列为可写格式，但**没说**
    它算哪个时区。本适配器一律按东八区解读 —— 因为下游契约（``YYYY-MM-DD``）就是
    北京时间墙上时间，只有这样才自洽。
    """
    if isinstance(value, datetime.datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=_TZ_CN)
    elif isinstance(value, datetime.date):
        dt = datetime.datetime(value.year, value.month, value.day, tzinfo=_TZ_CN)
    elif isinstance(value, bool):
        raise JiandaoyunError("布尔值不是合法的时间输入：%r" % (value,))
    elif isinstance(value, (int, float)):
        dt = datetime.datetime.fromtimestamp(float(value) / 1000.0, tz=_TZ_UTC)
    else:
        s = _RE_FRACTION.sub(lambda m: "." + m.group(1).ljust(6, "0")[:6],
                             str(value).strip(), count=1)
        dt = _parse_zoned(s) or _parse_local(s)
        if dt is None:
            raise JiandaoyunError(
                "不支持的时间格式：%r。简道云**不会**为此报错，而是把该值静默写成空值，"
                "所以适配器在这里拦下来。可用格式：YYYY-MM-DD / YYYY-MM-DD HH:MM:SS / "
                "RFC3339（带 Z 或 ±HH:MM）/ 毫秒时间戳 / datetime 对象。" % (value,))
    return dt.astimezone(_TZ_UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _from_utc(raw):
    """服务端时间值 → 下游习惯的 ``YYYY-MM-DD``（**东八区**）。

    ⚠️ 为什么不能像 SeaTable / 飞书那样直接 ``v[:10]`` 截断：简道云回的是 **UTC**。
       例：``2020-04-14T17:00:00.000Z`` 的北京时间是 ``2020-04-15 01:00``，
       截断会得到 ``2020-04-14`` —— **早一天**。跨日的记录会整批静默算错。
       所以这里必须真做时区转换。

    认不出来的值**原样返回**（不丢信息，也不假装解析成功）——
    下游的日期解析会因此失败得可见，而不是拿到一个错的日期。
    """
    dt = None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        try:
            dt = datetime.datetime.fromtimestamp(float(raw) / 1000.0, tz=_TZ_UTC)
        except (OverflowError, OSError, ValueError):
            return raw
    elif isinstance(raw, str) and raw.strip():
        s = _RE_FRACTION.sub(lambda m: "." + m.group(1).ljust(6, "0")[:6],
                             raw.strip(), count=1)
        dt = _parse_zoned(s)
        if dt is None:
            naive = _parse_local(s)
            # 无标注 → 按 UTC 理解（官方称该字段是 UTC 标准时间）
            if naive is not None:
                dt = naive.replace(tzinfo=_TZ_UTC)
    if dt is None:
        return raw
    return dt.astimezone(_TZ_CN).strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════════
# 单元格翻译（读 / 写两个方向）
# ══════════════════════════════════════════════════════════════════

def _unwrap_cell(cell):
    """剥掉 ``{"value": …}`` 外壳。

    ⚠️ 未验证：文档明确给了**写入**包装 ``{"_widget_x": {"value": …}}``，
       但没写**读取**返回的是不是同一层包装。这里两种都收：
       只要是「唯一键为 value 的 dict」就剥。

    已知副作用（成文，不掩盖）：``address`` 这类本身就是 JSON 的控件，
    若服务端**未加**外层包装而恰好只带 ``value`` 一个键（如 ``{"value": "广东省深圳市"}``），
    会连它自己的 ``value`` 一起剥掉 —— 结果是拿到显示文本、丢掉省市区结构。
    对下游真正使用的文本/数值/日期/选项类控件没有影响。
    """
    if isinstance(cell, dict) and set(cell) == {"value"}:
        return cell["value"]
    return cell


def _flat_cell(v, wtype: str):
    """服务端单元格值 → 下游习惯的扁平值（规则与 SeaTable / 飞书适配器对齐）。"""
    if v is None:
        return ""                       # 不是 None：下游到处 ``.get(col, "")``，给 None 会拼出 "None"
    t = str(wtype or "").lower()
    if t in _TIME_WIDGETS:
        return _from_utc(v)
    if t in _ARRAY_WIDGETS:
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v]
        return [str(v)] if v != "" else []
    if t in _NUMBER_WIDGETS:
        return v
    return v


def _to_value(value, wtype: str, field: str):
    """下游值 → ``{"value": …}`` 里的那个值。

    空值统一送 ``""``（清空该字段）—— **数组类控件送 ``[]``**：对复选框组来说
    「清空」就是一个空数组，送空字符串是另一种类型，服务端怎么处理文档没写。
    传进来的如果已经是 ``{"value": …}`` 形状，视为**调用方自己包好了**，
    原样放行 —— 这是给「形状未验证控件」留的显式逃生通道
    （见 ``_UNVERIFIABLE_WIDGETS``），属于调用方主动选择，不是适配器偷偷绕过校验。
    """
    if isinstance(value, dict) and set(value) == {"value"}:
        return value["value"]
    t = str(wtype or "").lower()
    if value is None or value == "" or value == [] or value == ():
        return [] if t in _ARRAY_WIDGETS else ""
    if t in _TIME_WIDGETS:
        return _to_utc(value)
    if t in _ARRAY_WIDGETS:
        vals = value if isinstance(value, (list, tuple, set)) else [value]
        return [str(x) for x in vals if x not in (None, "")]
    if t in _NUMBER_WIDGETS:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        try:
            return float(str(value).replace(",", "").strip())
        except ValueError:
            raise JiandaoyunError(
                "「%s」是数值控件，但收到的值转不成数字：%r"
                "（不原样放行 —— 那多半会被服务端当成 0 或空，静默丢数据）"
                % (field, value))
    if t in _SCALAR_WIDGETS:
        return str(value)
    return value


def _neutral_of(wtype: str) -> str:
    """控件类型 → 中立类型；没有对应关系时返回原类型名（不硬凑）。"""
    return _WIDGET_TO_NEUTRAL.get(str(wtype or "").lower(), str(wtype or "").lower())


# ══════════════════════════════════════════════════════════════════
# 传输层
# ══════════════════════════════════════════════════════════════════

class _HttpTransport:
    """直连 ``api.jiandaoyun.com``（API v5）。**全部 POST**。

    只用标准库 ``urllib``：这个适配器不该为了发几个 POST 引入依赖。

    ``proxy`` / ``no_proxy`` 的用处：本机 WorkBuddy 沙箱会给子进程注入一个
    ``HTTPS_PROXY``（随机端口的本地代理），从沙箱里直连会被它拦成 502。
    ``no_proxy: true`` 可显式绕开环境代理；``proxy: <url>`` 可指定代理。
    两者都不写 = 走系统默认（正常用户环境下的正确行为）。
    """

    def __init__(self, api_key: str, server: str = "", timeout: int = 30,
                 proxy: str = "", no_proxy: bool = False):
        self.api_key = api_key
        self.server = (server or _DEFAULT_SERVER).rstrip("/")
        self.timeout = timeout
        self.proxy = proxy or ""
        self.no_proxy = bool(no_proxy)
        if self.proxy:
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        elif self.no_proxy:
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    def close(self) -> None:
        """无持久连接可关；保留接口形状，与其它传输层一致。"""
        return None

    def call(self, path: str, body: dict = None) -> dict:
        """调用 ``/api/v5/<path>``，返回**剥掉外层信封后**的 payload。

        错误判定（两道，缺一不可）：
          ① HTTP ≥ 400 → 取响应体里的 ``{code, msg}`` 报错；
          ② HTTP 200 但体里有 ``code`` 且 ``code != 0``（或 ``msg`` 非空）→ 也报错。
             官方错误信封是 ``{code, msg}``，而**没有任何成功响应同时带这两个键**；
             何况网关配置不同有可能把 400 转成 200。这条是防「错误被当成成功」。
        """
        url = "%s/api/v5/%s" % (self.server, path)
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", "Bearer %s" % self.api_key)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Accept", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise self._error(path, e.read().decode("utf-8", "replace"), e.code)
        except urllib.error.URLError as e:
            raise JiandaoyunError(
                "无法连接简道云（%s）：%s。若本机有代理，可试 jiandaoyun.no_proxy: true"
                % (url, getattr(e, "reason", e)))
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise JiandaoyunError("简道云 %s 返回的不是 JSON（前 200 字符）：%s"
                                  % (path, raw[:200]))
        if not isinstance(payload, dict):
            raise JiandaoyunError("简道云 %s 返回的不是对象：%r" % (path, payload))
        code = payload.get("code")
        if code not in (None, 0, "0") or payload.get("msg"):
            if code is not None or "msg" in payload:
                raise JiandaoyunError(
                    "简道云 %s 调用失败：code=%s msg=%s"
                    % (path, code, payload.get("msg")), code=code,
                    msg=payload.get("msg"), raw=payload)
        return _unwrap(payload)

    @staticmethod
    def _error(path: str, raw: str, status: int) -> "JiandaoyunError":
        code = msg = None
        try:
            body = json.loads(raw)
            if isinstance(body, dict):
                code, msg = body.get("code"), body.get("msg")
        except ValueError:
            pass
        return JiandaoyunError(
            "简道云 %s 调用失败（HTTP %s）：%s"
            % (path, status, ("code=%s msg=%s" % (code, msg)) if msg is not None
               else (raw[:200] or "（空响应体）")),
            code=code, msg=msg)


def _unwrap(payload: dict) -> dict:
    """剥掉 ``{"code":0,"data":{…}}`` 式外壳（存在才剥）。

    ⚠️ 未验证：官方文档里成功响应是**裸的**（``{forms:[…]}`` / ``{status,success_count}``），
    但部分接口（``data/create`` / ``data/update``）的示例又写成 ``{"data": {…}}``，
    网关也可能统一加壳。这里用一个保守判据兼容两种：**所有键都属于
    {code, msg, data} 且 data 是对象**才算外壳。

    已知副作用（成文）：若某表单的字段**别名**恰好叫 ``data`` 且是唯一字段，
    其记录 ``{"data": {…}}`` 会被误剥一层。字段别名按惯例是中文，实际不会撞上。
    """
    guard = 0
    while (isinstance(payload, dict) and set(payload) <= {"code", "msg", "data"}
           and isinstance(payload.get("data"), dict) and guard < 5):
        payload = payload["data"]
        guard += 1
    return payload


# ══════════════════════════════════════════════════════════════════
# 适配器
# ══════════════════════════════════════════════════════════════════

class JiandaoyunAdapter(BaseAdapter):
    backend = "jiandaoyun"

    #: 能力声明（声明式：不联网、不认证、无副作用）：
    #:   · read / write / update / delete —— 8 个必修方法都真实现
    #:   · batch_write —— ``data/batch_create`` 是服务端原生批量（≤100/批）
    #:   · server_row_id —— ``_id`` 由服务端生成，客户端不可指定
    #:
    #: **刻意不声明**（都与模块 docstring 的理由一一对应）：
    #:   link / link_read  —— 开放接口没有关联写入；``lookup`` 只有显示文本
    #:   schema_manage     —— 只有字段只读清单，没有建表接口
    #:   query_pushdown    —— ``filter`` 的 ``cond[].type`` 取值域不明，不能对任意 filters 成立
    #:   idempotent        —— ``transaction_id`` 是调用方给的，适配器不自己造
    #:   optimistic_lock   —— 没有行级版本号
    #:
    #: ``link_append`` / ``link_one_way`` 沿用基类的 ``Unsupported``。
    CAPS = frozenset({CAP_READ, CAP_WRITE, CAP_UPDATE, CAP_DELETE,
                      CAP_BATCH_WRITE, CAP_SERVER_ROW_ID})

    def __init__(self, app_id: str, api_key: str, server: str = "",
                 app_name: str = None, timeout: int = 30,
                 proxy: str = "", no_proxy: bool = False, transport=None):
        self.app_id = app_id
        self.api_key = api_key
        self.server = server or ""
        self.app_name = app_name or "default"
        self.timeout = timeout
        self.proxy = proxy or ""
        self.no_proxy = bool(no_proxy)
        self._t = transport
        self._entries = None      # {表单名: entry_id}
        self._meta = {}           # 表名 -> 已解析的控件元数据
        self._probed = False      # 是否已探活成功（诊断用）

    # ── 生命周期 ────────────────────────────────────────
    def auth(self) -> None:
        """建连 + 探活。

        简道云没有独立的「登录」接口（每个请求都带 ``Bearer <APIKey>``），
        所以这里用一次**最小只读请求**（``app/entry/list``，limit=1）当探活：
        凭据错 / 应用 id 错 / 应用没开 API，都会在**建连时**就报出来，
        不会拖到第一次业务写入才炸 —— 那时错误会混在业务日志里，很难归因。
        """
        if self._t is None:
            if not self.api_key:
                raise JiandaoyunError("jiandaoyun 后端缺 api_key（简道云「APIKey」）")
            self._t = _HttpTransport(self.api_key, self.server, self.timeout,
                                     proxy=self.proxy, no_proxy=self.no_proxy)
        d = self._call("app/entry/list", {"app_id": self.app_id, "limit": 1, "skip": 0})
        if not isinstance(d.get("forms"), list):
            raise JiandaoyunError(
                "探活时 app/entry/list 未返回 forms 列表（拿到键：%s）。"
                "常见原因：app_id 不是「应用」的 id、或该应用未开启 API。"
                % ("/".join(sorted(d)) or "（无）"))
        self._probed = True
        self._invalidate()

    def close(self) -> None:
        """清掉本地元数据缓存（表单/控件结构会变，长期驻留进程不该拿旧快照）。"""
        if self._t is not None and hasattr(self._t, "close"):
            try:
                self._t.close()
            except Exception:
                pass
        self._invalidate()

    def _invalidate(self) -> None:
        self._entries = None
        self._meta = {}

    def _call(self, path: str, body: dict = None) -> dict:
        if self._t is None:
            self.auth()
        return self._t.call(path, body)

    def describe(self) -> str:
        return "%s(%s, app=%s) caps=%s" % (
            type(self).__name__, self.backend, self.app_name,
            ",".join(sorted(self.capabilities())) or "-")

    # ── 表名 ↔ entry_id ─────────────────────────────────
    def _load_entries(self) -> None:
        """拉取本应用的表单清单：``POST app/entry/list`` → ``{forms:[{name, app_id, entry_id}]}``。

        ⚠️ 未验证：文档只给 ``limit`` 1~100 / ``skip``，没说 ``skip`` 的上限与
        ``forms`` 是否含子表单。这里按「limit=100 一直翻到不满页」处理，
        并且**翻页无进展就报错**（宁可报错也不死循环）。
        """
        if self._entries is not None:
            return
        self._entries = {}
        skip = 0
        while True:
            d = self._call("app/entry/list",
                           {"app_id": self.app_id, "limit": _PAGE, "skip": skip})
            forms = d.get("forms")
            if not isinstance(forms, list):
                raise JiandaoyunError(
                    "app/entry/list 未返回 forms 列表（拿到键：%s）"
                    % ("/".join(sorted(d)) or "（无）"))
            for f in forms:
                if isinstance(f, dict) and f.get("name") and f.get("entry_id"):
                    self._entries[str(f["name"])] = str(f["entry_id"])
            if len(forms) < _PAGE or not forms:
                break
            skip += len(forms)

    def _entry_id(self, table: str) -> str:
        """按**表单名**解析 entry_id；解析不到就报错，绝不把名字当 id 发出去。

        为什么要先解析：直接拿中文表名当 ``entry_id`` 传，服务端只会回一个
        语焉不详的「表单不存在」，分不清是表名错了还是权限错了。先取回清单，
        报错时就能把「本应用现有表单」一并列出，一次定位。
        """
        self._load_entries()
        if table in self._entries:
            return self._entries[table]
        raise KeyError("表单不存在：%r（本应用现有：%s）"
                       % (table, " / ".join(sorted(self._entries)) or "（空）"))

    def list_tables(self) -> List[str]:
        """本应用的表单名清单（排序后）。

        为什么要有：没有它，调用方想知道「这个应用里有哪些表单」就只能读
        ``self._entries`` 私有属性 —— 而私有属性换个后端必然 ``AttributeError``，
        正是多底座改造要消灭的耦合。
        """
        self._load_entries()
        return sorted(self._entries)

    # ── 控件元数据 ──────────────────────────────────────
    def _fields(self, table: str) -> dict:
        """解析并缓存某表单的控件结构。

        产出结构（内部用）::

            {"entry_id", "widgets": [...],
             "by_name": {API 键: widget}, "by_label": {中文列名: widget},
             "dup_labels": {重名标签}, "sys": {系统字段名: 中文名}}

        为什么要 ``by_name`` 和 ``by_label`` 两套：``widget/list`` 的 ``name`` 在
        设了别名之后**就是别名**（全 API 都用它），而 ``label`` 才是界面上给人看的
        中文列名。本仓库的对外契约是「中文列名」，所以两个方向都要能查。
        """
        if table in self._meta:
            return self._meta[table]
        eid = self._entry_id(table)
        d = self._call("app/entry/widget/list", {"app_id": self.app_id, "entry_id": eid})
        widgets = d.get("widgets")
        if widgets is None:
            widgets = []
        if not isinstance(widgets, list):
            raise JiandaoyunError(
                "app/entry/widget/list 的 widgets 不是列表（拿到 %r）。"
                "这通常说明 entry_id 不是「表单」的 id。" % (widgets,))
        by_name, by_label, dup = {}, {}, set()
        norm = []
        for w in widgets:
            if not isinstance(w, dict):
                continue
            nm = str(w.get("name") or "").strip()
            if not nm:
                continue                       # 没有 API 键的控件无法读写，跳过（不算业务列）
            label = str(w.get("label") or nm).strip() or nm
            item = {"name": nm, "label": label,
                    "type": str(w.get("type") or "").lower(),
                    "raw": w}
            norm.append(item)
            by_name[nm] = item
            if label in by_label and by_label[label]["name"] != nm:
                dup.add(label)                 # 同名标签 → 二义，查到时必须报错
            else:
                by_label[label] = item
        self._meta[table] = {"entry_id": eid, "widgets": norm,
                             "by_name": by_name, "by_label": by_label, "dup_labels": dup,
                             "sys": dict(_SYS_FIELDS)}
        return self._meta[table]

    def _widget_of(self, meta: dict, key: str) -> dict:
        """按「中文列名」或「API 键」取控件；查不到/有二义就报错。

        二义必须报错而不是取第一个：两个同名标签的控件，静默取第一个会把数据
        写到**另一个字段**上，且不报错 —— 这正是最难排查的那类错误。
        """
        if key in meta["dup_labels"]:
            names = [w["name"] for w in meta["widgets"] if w["label"] == key]
            raise JiandaoyunError(
                "列名「%s」在表单里有 %d 个同名字段（%s），无法判定写哪个。"
                "请改用 API 键指定。" % (key, len(names), " / ".join(names)))
        return meta["by_label"].get(key) or meta["by_name"].get(key)

    # ── 读 ─────────────────────────────────────────────
    def get_metadata(self, table: str) -> Dict[str, Any]:
        """返回 ``{"table_name", "columns":[{name,type,key,neutral[,items][,readonly]}]}``。

        ``name`` 是中文列名（``label``）—— 与 SeaTable / 飞书适配器同义。
        ``type`` 是**后端原生**控件类型（保持一致），另外附 ``neutral`` 给出
        中立类型（``schema.NEUTRAL_TYPES`` 词表），供需要跨后端统一的调用方使用。

        ⚠️ 未验证：``widget/list`` 的字段顺序、以及 ``sysWidgets`` 是否真的只给 ``name``。
        """
        meta = self._fields(table)
        cols = []
        for w in meta["widgets"]:
            c = {"name": w["label"], "type": w["type"], "key": w["name"],
                 "neutral": _neutral_of(w["type"])}
            if w["type"] in _READONLY_WIDGETS:
                c["readonly"] = True
            items = (w["raw"].get("items") if isinstance(w["raw"], dict) else None)
            if isinstance(items, list) and items:      # 子表单：附上子控件清单
                c["items"] = [{"name": str(i.get("label") or i.get("name") or ""),
                               "type": str(i.get("type") or "").lower(),
                               "key": str(i.get("name") or "")}
                              for i in items if isinstance(i, dict)]
            cols.append(c)
        for sname, label in meta["sys"].items():
            cols.append({"name": label, "type": "text", "key": sname, "readonly": True})
        return {"table_name": table, "columns": cols}

    def table_exists(self, table: str) -> bool:
        """按表单清单判断；表不存在返回 False，不抛异常。"""
        try:
            self._load_entries()
        except Exception:
            return False
        return table in self._entries

    def _translate(self, raw: dict, meta: dict) -> dict:
        """一行「API 键 → ``{"value": …}``」→ 「中文列名 → 扁平值」+ ``__row_id__``。"""
        out: Dict[str, Any] = {}
        for k, cell in raw.items():
            if k == "_id":
                continue
            if k in meta["sys"]:
                out[meta["sys"][k]] = _unwrap_cell(cell)
                continue
            w = meta["by_name"].get(k)
            if w is None:
                # 结构里没有的键（例如后台新加的列、或我们拉清单时的可见性延迟）：
                # 原样带出，绝不丢 —— 丢了下游会以为「这列是空的」。
                out[k] = _unwrap_cell(cell)
                continue
            out[w["label"]] = _flat_cell(_unwrap_cell(cell), w["type"])
        out["__row_id__"] = raw.get("_id")
        return out

    def _row_ids_of_page(self, payload: dict, table: str) -> List[dict]:
        """从 ``data/list`` 的响应里取出记录列表（兼容两种信封）。

        ⚠️ 未验证：文档写 ``data_list``；但若网关统一加壳，剥壳后可能是 ``data`` 是列表。
        两者都认；**都认不出就报错**，不返回空冒充「这个表没有数据」。
        """
        rows = payload.get("data_list")
        if rows is None and isinstance(payload.get("data"), list):
            rows = payload["data"]
        if not isinstance(rows, list):
            raise JiandaoyunError(
                "data/list 未返回记录列表（表单「%s」，拿到键：%s）"
                % (table, "/".join(sorted(payload)) or "（无）"))
        return [r for r in rows if isinstance(r, dict)]

    def _iter_rows(self, table: str) -> Iterator[dict]:
        """按 ``data_id`` 游标逐页产出原始行（**始终正序**）。

        游标 = 上一页最后一条的 ``_id``。翻页保护有两道：
          · 本页不满一页 → 结束；
          · 游标没前进（服务端返回了同一批）→ 报错。宁可报错也不死循环。
        """
        eid = self._entry_id(table)
        cursor = None
        while True:
            body = {"app_id": self.app_id, "entry_id": eid, "limit": _PAGE}
            if cursor:
                body["data_id"] = cursor
            payload = self._call("app/entry/data/list", body)
            batch = self._row_ids_of_page(payload, table)
            for r in batch:
                yield r
            if len(batch) < _PAGE or not batch:
                return
            nxt = batch[-1].get("_id")
            if not nxt or str(nxt) == str(cursor):
                raise JiandaoyunError(
                    "data/list 翻页未前进（表单「%s」，游标 %r，本页 %d 条）。"
                    "为避免死循环，此处直接报错。" % (table, cursor, len(batch)))
            cursor = nxt

    def list_rows(self, table: str) -> List[Dict[str, Any]]:
        meta = self._fields(table)
        return [self._translate(r, meta) for r in self._iter_rows(table)]

    def get_row(self, table: str, row_id: str) -> Optional[Dict[str, Any]]:
        """按 ``data_id`` 取单行；找不到返回 ``None``。

        覆盖基类版本的唯一理由是**提前退出**：基类会先把整表拉完再线性查找。
        简道云没有「按 id 取单条」的接口，只能按 data_id 正序翻页，
        所以这里翻到就停，大表能省掉后面所有页。
        """
        target = str(row_id)
        meta = self._fields(table)
        for raw in self._iter_rows(table):
            if str(raw.get("_id", "")) == target:
                return self._translate(raw, meta)
        return None

    # ── 写 ─────────────────────────────────────────────
    def _to_payload(self, table: str, data: dict, meta: dict) -> dict:
        """业务 dict → 简道云 ``data``：``{API 键: {"value": …}}``。

        三类字段的处理都是**成文转换**，不是静默丢弃：
          · 系统字段（文档明说不支持写入）→ 剔除；
          · 服务端算出来的控件（``sn`` / ``lookup`` / ``aggregation*``）→ 剔除；
          · 形状未验证的控件（成员/部门/子表单/附件/定位…）→ **抛 Unsupported**，
            因为官方已明确「id 不对不报错、必填也会留空」，硬写等于把数据写没。
        列名写错 → 抛错并列出现有列名（不丢进服务端换一句看不懂的话）。
        """
        out: Dict[str, Any] = {}
        unknown: List[str] = []
        for k, v in (data or {}).items():
            if k == "__row_id__" or k in meta["sys"] or k in _SYS_LABELS:
                continue                 # 系统字段（API 键或中文名都认，见 _SYS_LABELS）
            w = self._widget_of(meta, k)
            if w is None:
                unknown.append(str(k))
                continue
            t = w["type"]
            if t in _READONLY_WIDGETS:
                continue
            if t in _UNVERIFIABLE_WIDGETS:
                raise Unsupported(
                    "控件「%s.%s」的类型是 %s，其**写入形状未经确认**，适配器拒绝写。"
                    "官方已明确这类控件（成员/部门等）「id 不存在不会报错，即使必填也会留空」——"
                    "硬写最大的可能是把数据悄悄写空。请在真实环境验证形状后再放开，"
                    "或把值包成 {\"value\": …} 原样传入以显式承担该风险。"
                    % (table, w["label"], t))
            out[w["name"]] = {"value": _to_value(v, t, w["label"])}
        if unknown:
            raise JiandaoyunError(
                "以下列在表单「%s」里不存在（可能表单选错了，或列名有多余空格）：%s；"
                "该表单可用列：%s" % (table, " / ".join(unknown[:8]),
                                      " / ".join(w["label"] for w in meta["widgets"][:20])))
        return out

    def append_row(self, table: str, data: Dict[str, Any]) -> str:
        ids = self.append_rows(table, [data])
        return ids[0] if ids else None

    def append_rows(self, table: str, rows: List[Dict[str, Any]]) -> List[str]:
        """服务端批量新增，100 行/批（``data/batch_create``）。

        单条新增也走这条路径（``append_row`` 只是它的一元调用）——
        两条路径分开写迟早会长出两套行为。

        ⚠️ **部分成功必须报错**：响应里的 ``success_count`` 若与本次提交条数不符，
        说明有几条没写进去，而服务端**没有告知是哪几条**。这时返回一个带洞的 id
        列表等于让调用方以为「都写好了」—— 所以直接报错，并在文案里说清
        「无法判定是哪几条」，让调用方知道要去核对。

        ``success_ids`` 与入参同序；服务端少给 id 时位置补 ``None``
        （宁可为 None 也不错位 —— 错位会把 A 的 id 记到 B 头上）。
        """
        eid = self._entry_id(table)
        meta = self._fields(table)
        rows = list(rows or [])
        if not rows:
            return []
        ids: List[str] = []
        for i in range(0, len(rows), _BATCH):
            chunk = rows[i:i + _BATCH]
            payload = [self._to_payload(table, r, meta) for r in chunk]
            d = self._call("app/entry/data/batch_create",
                           {"app_id": self.app_id, "entry_id": eid,
                            "data_list": payload})
            cnt = d.get("success_count")
            if cnt is not None and int(cnt) != len(chunk):
                raise JiandaoyunError(
                    "批量新增部分失败：本批 %d 条，服务端只成功 %s 条，且未告知是哪几条。"
                    "请按业务键核对后再重试（不要盲目重试 —— 成功的那些会重复）。"
                    % (len(chunk), cnt))
            got = list(d.get("success_ids") or [])
            ids.extend([str(x) for x in got] if len(got) == len(chunk)
                       else [None] * len(chunk))
        return ids

    def update_row(self, table: str, row_id: str, data: Dict[str, Any]) -> None:
        """更新单行（``data/update``）。

        为什么不用 ``data/batch_update``：那个接口的语义是「把**多条**改成**同一个
        固定值**」，与本方法「只改这一行的这些字段」的契约根本不是一回事。

        **回显校验**：``data/update`` 的响应带修改后的整条记录，正好可以用来堵住
        本后端最阴的一处静默失败 —— 时间格式不被接受时，服务端**不报错**，只是把
        该字段写成空值。这里把我们发出去的值与回显值按控件类型做语义比较
        （日期比到「天」，数值比字符串化后的结果），对不上就报错。
        回显里没有该字段时不做判断（信息不足，不猜）。
        """
        eid = self._entry_id(table)
        meta = self._fields(table)
        payload = self._to_payload(table, data, meta)
        if not payload:
            return                     # 全是只读/系统列 → 没有要写的东西，不是错误
        row_id = str(row_id)
        d = self._call("app/entry/data/update",
                       {"app_id": self.app_id, "entry_id": eid,
                        "data_id": row_id, "data": payload})
        self._verify_echo(table, payload, d, meta)

    def _verify_echo(self, table: str, sent: dict, returned: dict, meta: dict) -> None:
        """把「发出去的值」与「服务端回显的值」做语义比较，不一致就报错。

        比较前先各自过一遍 ``_flat_cell``：这样 ``2026-05-06T16:00:00.000Z`` 与
        ``2026-05-07`` 会被判成同一个东西（都是北京 5 月 7 日），不会因为表示法
        不同而误报。
        """
        if not isinstance(returned, dict):
            return
        for name, cell in sent.items():
            w = meta["by_name"].get(name)
            if w is None:
                continue
            got = returned.get(name)
            if name not in returned and w["label"] in returned:
                got = returned[w["label"]]
            elif name not in returned:
                continue               # 回显里没有这一列 → 信息不足，不判
            want_f = _flat_cell(sent[name].get("value"), w["type"])
            got_f = _flat_cell(_unwrap_cell(got), w["type"])
            if _same(want_f, got_f):
                continue
            raise JiandaoyunError(
                "更新「%s.%s」后服务端回显与发出的值不一致：发出 %r，回显 %r。"
                "简道云对不被接受的时间格式**不报错**、只写成空值，这类静默丢失"
                "只有靠回显校验才能发现。" % (table, w["label"], want_f, got_f))

    def delete_rows(self, table: str, row_ids: List[str]) -> None:
        """按 ``data_id`` 批量删除（``data/batch_delete``，≤100/批）。

        ⚠️ 未验证：文档没说「删一条不存在的 id」是报错还是静默跳过。所以这里
        以 ``success_count`` 为准：与提交条数不符就报错。
        删不掉就是删不掉，当成成功会产生幽灵数据（上层以为清了、实际还在）。
        """
        eid = self._entry_id(table)
        ids = [str(x) for x in (row_ids or []) if x]
        if not ids:
            return
        for i in range(0, len(ids), _BATCH):
            chunk = ids[i:i + _BATCH]
            d = self._call("app/entry/data/batch_delete",
                           {"app_id": self.app_id, "entry_id": eid, "data_ids": chunk})
            cnt = d.get("success_count")
            if cnt is not None and int(cnt) != len(chunk):
                raise JiandaoyunError(
                    "删除部分失败：本批 %d 条，服务端只成功 %s 条。"
                    "可能有 id 不存在或已被删除；请不要当成已清理。" % (len(chunk), cnt))

    # ── 表结构 ─────────────────────────────────────────
    def ensure_table(self, table: str,
                     columns: Optional[List[Dict[str, Any]]] = None) -> str:
        """**不支持**：简道云开放接口只提供字段只读清单，没有建表/加列接口。

        这是如实回答「做不到」，由上层决定是报错还是「人工预建 + 只做数据映射」。
        不要退化成「假装建好了」——
        详见 ``base.py`` 的 ``ensure_table`` docstring 与 ``schema.backend_type``。
        """
        raise Unsupported(
            "简道云开放接口不提供建表/改列能力（只有 app/entry/widget/list 这份"
            "**只读**字段清单）。表单与字段必须在简道云界面里预建，"
            "本适配器只做数据映射。需要自动建表的后端请用 local / seatable / feishu。")

    # ── 关联 ───────────────────────────────────────────
    def link_columns(self, table: str) -> List[str]:
        """该表单上**关联类控件**的名字清单（诊断用，可能为空）。

        空列表是一个**真实答案**（这表确实没有关联类控件），不是「读不了」——
        与 ``list_linked`` 的抛错语义不同，别混用。
        """
        meta = self._fields(table)
        return [w["label"] for w in meta["widgets"]
                if w["type"] in ("lookup", "linkdata")]

    def link(self, table: str, other_table: str, link_id: str,
             row_id: str, other_row_ids: List[str]) -> None:
        """**不支持**：简道云开放接口没有关联数据的写入接口。

        契约要求「同一 link_id，两张表各记一次」。简道云这边：``lookup``
        （关联查询）是**只读**的计算控件，``linkdata``（关联数据）的写入形状文档未给。
        写不了就是写不了 —— 抛 ``Unsupported``，让调用方去决定人工维护还是改设计，
        **绝不**悄悄只写一侧然后让上层以为关联建好了。
        """
        raise Unsupported(
            "简道云开放接口不支持建立关联（没有关联数据的写入接口）："
            "表「%s」→「%s」的关联只能在简道云界面上维护。" % (table, other_table))

    def list_linked(self, table: str, row_id: str, link_id: str) -> List[str]:
        """**不支持**：读不回对方记录的 ``data_id``。

        ``lookup``（关联查询）返回的是对方字段的**显示文本**，不是可用的行标识；
        拿它当 row_id 用会得到一堆谁也认不出的字符串。按契约铁律，
        给不出正确答案就抛 ``Unsupported``，**不返回空列表冒充「没有关联」**
        （本仓库吃过这个亏：SeaTable 的 ``list_linked`` 曾是 ``return []`` 空桩，
        于是「查关联」永远成功、永远返回空、永远不报错）。
        """
        raise Unsupported(
            "简道云开放接口读不回关联的对方记录 id（lookup 只给显示文本）："
            "表「%s」的行 %s。需要关联关系请改用可双向关联的后端（seatable / feishu），"
            "或把对方表的主键作为普通文本列冗余存储、由调用方自行解析。"
            % (table, row_id))


def _same(a, b) -> bool:
    """两值语义相等（列表按集合比，标量按去空白字符串比）。

    保守取「宽松」：宁可漏报也不误报 —— 误报会让人去查一个不存在的 bug，
    而漏报只是少了一层额外保护（真正的写入失败仍会被 ``success_count`` 之类拦住）。
    """
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        la = [str(x).strip() for x in (a if isinstance(a, (list, tuple)) else [a])]
        lb = [str(x).strip() for x in (b if isinstance(b, (list, tuple)) else [b])]
        return sorted(la) == sorted(lb)
    if a is None and b is None:
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) < 1e-9
    return str(a).strip() == str(b).strip()


def neutral_type(widget_type: str) -> str:
    """控件类型 → 中立类型（``adapters/schema.py`` 词表）。查不到时原样返回。"""
    return _neutral_of(widget_type)
