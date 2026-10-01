# -*- coding: utf-8 -*-
"""飞书任务（Task / 飞书待办）适配器 —— 飞书侧的「项目管理」底座。

为什么单独做一个后端，而不是塞进 ``adapters/feishu.py``
────────────────────────────────────────────────────────
飞书在项目管理上其实是**两个不同的产品、两套完全不同的 API**：

  · **飞书多维表格（Base / Bitable）** —— 通用表格。已在 ``adapters/feishu.py``
    （``backend="feishu"``），路径 ``open.feishu.cn/open-apis/bitable/v1``。
  · **飞书任务（Task）** —— 任务清单 / 任务 / 子任务 / 分组 / 负责人。本文件
    （``backend="feishu_task"``），路径 ``open.feishu.cn/open-apis/task/v2``。

两者除了都叫「飞书」之外没有任何共同点：没有共同的表概念、没有共同的 id 空间、
连「写一个字段」的语义都不同（Base 是整条 record 覆盖，Task 是 ``update_fields``
白名单）。把它们硬塞进一个类，只会造出一个到处 ``if`` 的怪物 —— 所以拆开。

映射关系（本适配器的核心约定）
──────────────────────────────
===============  ==========================================
中立概念          飞书任务里对应什么
===============  ==========================================
表 ``table``      **任务清单（tasklist）**，可用清单名或 guid
行 ``row``        **任务（task）**
``__row_id__``    任务的 ``guid``
列 ``column``     任务的内置属性（摘要/截止时间/负责人…）+ 该清单的**自定义字段**
===============  ==========================================

⚠️ 但这层映射有**一处不完美**，必须知道：**任务可以先建、后进清单**。
实测 ``tasks.create`` 不带 ``tasklists`` 照样成功，任务会变成一个**孤儿** ——
不属于任何清单，于是 ``list_rows(任何清单)`` 永远看不到它。
所以本适配器的 ``append_row`` **强制要求目标清单**（见 ``append_row``）。

协议事实（**全部来自实测**，探测脚本见仓库外 ``probes/feishu_task_*.py``；
2026-10-01，四轮探测，全程只碰自建的一次性清单、反序清理、基线比对）
────────────────────────────────────────────────────────────────────
传输与信封
  1. 走 ``lark-cli task …`` 子进程，信封与 Base 一致：成功
     ``{"ok":true,"identity":…,"data":{…}}`` 走 stdout；失败
     ``{"ok":false,"error":{type,subtype,code,message,log_id}}`` 走 stderr。
  2. ⚠️ **``--yes`` 是另一条命令分界线**。``tasks.delete`` / ``tasklists.delete`` /
     ``sections.delete`` / ``custom_fields.remove`` 是 **high-risk-write**：
     不加 ``--yes`` 时服务端**什么都没做**，CLI 回
     ``{"ok":false,"error":{"type":"confirmation",…}}`` 且 **退出码 rc=10**。
     10 既不是 0 也不是常见的 1 —— 只看「rc != 0 就是错」会漏掉这条，
     只看「返回里有 error 就报错」则会把它当成普通失败。
  3. ``--dry-run`` 可预览真实 REST 路径（``/open-apis/task/v2/tasks/{guid}``），
     联调时比读文档可靠。
  4. 请求体走 ``--data @临时文件``（Windows argv 长度 + 中文转义都靠它绕开）。

读取形状
  5. ⚠️ **``tasklists.tasks`` 是摘要投影，不是详情**。实测该接口返回的列表项
     **只有 7 个键**：``completed_at / due / guid / members / start /
     subtask_count / summary`` —— 连 ``description``、``status``、
     ``custom_fields`` 都**没有**。而 ``tasks.get`` 返回 **28 个键**。
     所以「列表接口读回来的行」和「详情」根本不是一个东西；
     拿列表当行用，会让所有详情列看起来「本来就是空的」。
     → 本适配器的 ``list_rows()`` 逐条补详情（N+1 次调用，见该方法的说明），
       只想要摘要请显式用 ``list_rows_brief()``。
  6. 创建类接口回的**都不是** ``data.guid``，而是各自的包装键：
     ``data.tasklist.guid`` / ``data.task.guid`` / ``data.custom_field.guid`` /
     ``data.section.guid`` / ``data.subtask.guid``。
  7. ``tasklists.get`` 的清单字段：``archive_msec created_at creator guid
     members name owner updated_at url``。
  8. 时间字段两种形状：``due`` / ``start`` 是 ``{"timestamp":"<ms>","is_all_day":bool}``；
     ``completed_at`` / ``created_at`` / ``updated_at`` 是**毫秒字符串**。
     完成与否看 ``completed_at``：``"0"`` = 未完成，非零毫秒 = 已完成，
     且服务端会同时把 ``status`` 在 ``todo`` / ``done`` 之间翻。

写入语义
  9. ⚠️ ``tasks.patch`` 的 ``update_fields`` 是**必填**，而且是一个**固定白名单，
     只有 14 个字段**（服务端原文）：``agent_task_progress, agent_task_status,
     completed_at, custom_complete, custom_fields, description, due, extra,
     is_milestone, mode, repeat_rule, start, summary, text_deliveries``。
     → **``members`` 不在里面**。想改负责人/关注人走 ``patch`` 一定失败，
       必须用 ``task members add`` / ``members remove``（见第 12 条）。
 10. ⚠️ ``update_fields`` 里列了的字段，**必须同时在 ``task`` 体里给出非空值**，
     否则整条调用 1470400 ``Invalid Param 'summary', must not be empty.``
     —— 不是「忽略那一项」，是**全盘失败**。
 11. ⚠️ 反过来，出现在 ``task`` 体里、但**没**列进 ``update_fields`` 的字段会被
     **静默忽略**（实测：body 里塞了 description、``update_fields`` 只写 summary，
     调用成功、description 原文不动）。**只看返回值发现不了**。
     本适配器从结构上消灭这个坑：``update_fields`` 和 body 由**同一个 dict 派生**，
     两者不可能不一致。
 12. 成员：``task members add`` / ``members remove``，body
     ``{"members":[{"id":…,"role":"assignee|follower","type":"user"}]}``。
     ``add`` 是**追加**（先加 assignee 再加 follower，两个都在），
     ``remove`` 只摘掉**指定的 (id, role) 组合**、其余保留。
     （顺带说明为什么第 9 条的坑很隐蔽：``patch members`` 那种「整体替换」的直觉
     在这里是错的，而错的写法会被白名单直接挡下 —— 反而安全。）
 13. ``custom_fields`` 走 patch：``update_fields:["custom_fields"]``，
     元素形如 ``{"guid":"<字段 guid>", "<type>_value": …}``，**整列覆盖**。
     值键名就是 ``<type>_value``（实测 ``text_value`` / ``number_value`` /
     ``single_select_value``）。``number`` 传字符串也接受。
 14. ⚠️ ``single_select`` 写入**必须给选项 guid，不能给选项名**。传名字直接
     1470400 ``Value of single_select '红' isn't a visible option…``。
     而读回来也是 **guid**（``single_select_value`` 存的是 option guid）——
     写名/读 guid 不对称。本适配器对外统一用**选项名**（人话），内部做
     名→guid 映射，名字不存在时报错并**列出可用选项**，绝不静默丢弃。
 15. 只读字段（``guid`` / ``created_at`` / ``url`` / ``subtask_count`` …）与
     未知字段走 ``patch`` 会被服务端**明确拒绝**（1470400），不是静默忽略 ——
     这一点比多维表格老实，但仍不能依赖它当校验（列名写错在别处可能就悄悄过了），
     所以本适配器仍**先在客户端拦未知列**。
 16. ⚠️ **「找不到」是抛错，不是空结果**：``tasks.get`` 取不存在的 guid 与
     ``tasks.delete`` 删不存在的 guid 都回 **1470404 not_found**（删除**不幂等**）。
     契约规定 ``get_row`` 找不到要**返回 None 不抛异常**，所以适配器只在
     ``get_row`` 里把这个错误码翻译成 None（其余错误照常抛 —— 不能把所有失败
     都当成「没有」）；``delete_rows`` 则如实抛，因为「删不掉」被当成「已删掉」
     会留下幽灵数据。这一条是**真实验证时才发现**的：离线 mock 里
     ``tasks.get`` 回的是空 ``data``，掩盖了真实行为。
 17. 新建任务后约 **0.4s** 可读回（写后读延迟窗口比多维表格的 ~2.7s 小得多，
     但**存在**）。
 18. ``number_setting.format="custom"`` 时 ``custom_symbol`` **必填**（≥1 字符），
     否则 1470400 —— 官方规范没把这条写成条件必填。

映射里必须显式处理的一件事：**列名会撞车**
  · 本机「项目」清单里有一个用户自定义字段就叫「状态」，与内置 ``status``
    （``todo`` / ``done``）**同名但完全是两个东西**。
    按普通 dict 覆盖，内置列会被**静默顶掉**、值换了个来源而列名不变。
  · 规则：**内置列名永远属于内置列**；自定义字段重名时改名为 ``<原名>(自定义)``
    （列定义里用 ``origin_name`` 记原名）。两边都在、名字可预测。
    若改完还冲突（同名自定义字段有两个），**抛错**而不是继续编后缀 ——
    那种情况下「按名字取值」本身就是歧义的，编后缀只会把歧义藏起来。

仍未实测 / 本适配器刻意不做的事
────────────────────────────────
  · ``dependencies``（前置任务）在**本机 99 条任务里全部为空**，元素形状
    **未实测**。所以它只做**原样透传**（列「前置任务」），不解释、不做关联读写，
    更**不声明** ``CAP_LINK`` / ``CAP_LINK_READ``（见 ``link()``）。
  · ``multi_select`` / ``datetime`` 两种自定义字段类型**未实测**
    （本机单据里只有 text / number / single_select / member），
    按同构形状推断实现（值键 ``multi_select_value`` / ``datetime_value``），
    并在 ``_custom_type`` 里对**未知类型严格报错**而不是猜。
  · ``member`` 类型**已实测**：定义带 ``member_setting.multi``，
    值键 ``member_value``，形状 ``[{"id":"ou_…","type":"user"}]``
    （``multi=false`` 时**也是列表**）。读写都按这个形状实现。
  · ``member`` / ``multi_select`` 的**写入**路径未实测（只实测了读）。
    写进去是否被接受请以第一次真实写入的返回为准。
  · 提醒（``reminders`` / ``positive_reminders``）、附件（``attachments``）、
    ``repeat_rule``、``origin``、``mode`` 未实测，不映射为列。
  · ``tasks.list``（``type=my_tasks``，跨清单的「我的任务」）未接入 ——
    它不是一个「清单」，映射不进 ``table`` 概念，硬塞会破坏语义。
"""
import json
import os
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from . import schema
from .base import (CAP_DELETE, CAP_READ, CAP_SCHEMA_MANAGE, CAP_SERVER_ROW_ID,
                   CAP_UPDATE, CAP_WRITE, BaseAdapter, Unsupported)
from .feishu import FeishuError, find_lark_cli

#: 需要 ``--yes`` 才会真正执行的子命令（实测 high-risk-write）。
#: 不加 --yes 时 CLI 回 ``type=confirmation`` **且退出码 10**，服务端什么都没做。
_HIGH_RISK = frozenset({
    "tasks.delete", "tasklists.delete", "sections.delete", "custom_fields.remove",
})

#: ``tasks.patch`` 的 ``update_fields`` 白名单（服务端原文，实测）。
#: 注意 **不含 members** —— 成员必须走 members.add / members.remove。
_PATCHABLE = frozenset({
    "agent_task_progress", "agent_task_status", "completed_at", "custom_complete",
    "custom_fields", "description", "due", "extra", "is_milestone", "mode",
    "repeat_rule", "start", "summary", "text_deliveries",
})

#: 任务内置属性 → 中立列。``key`` 是飞书字段名；``"@"`` 开头的 key 表示
#: 「由本适配器合成的列」（不是原样透传的服务端字段），见 ``_to_row``。
#: writable=False 的列在写入时会被**剔除并登记**（见 ``last_skipped``）。
_BUILTIN_COLUMNS: Tuple[Tuple[str, str, str, bool], ...] = (
    ("摘要",    "text",     "summary",           True),
    ("描述",    "text",     "description",       True),
    ("完成状态", "checkbox", "@completed",        True),
    ("完成时间", "datetime", "completed_at",      True),
    ("开始时间", "datetime", "start",             True),
    ("截止时间", "datetime", "due",               True),
    ("负责人",   "user",     "@assignee",         True),
    ("关注人",   "user",     "@follower",         True),
    ("里程碑",   "checkbox", "is_milestone",      True),
    ("状态",    "text",     "status",            False),
    ("前置任务", "text",     "dependencies",      False),
    ("父任务",   "text",     "parent_task_guid",  False),
    ("子任务数", "number",   "subtask_count",     False),
    ("创建时间", "datetime", "created_at",        False),
    ("更新时间", "datetime", "updated_at",        False),
    ("任务链接", "text",     "url",               False),
)

#: 自定义字段与**内置列**重名时给自定义字段加的后缀。见 ``_column_map`` 的说明 ——
#: 本机「项目」清单里真的有一个自定义字段叫「状态」，与内置 ``status`` 撞名。
_CUSTOM_SUFFIX = "(自定义)"

#: 自定义字段类型 → 中立类型。**只列实测过的**；未列出的类型按未知处理（报错）。
#: 实测来源：``probes/feishu_task_cf_probe.py``（只读「项目」清单里的 6 个字段）。
_CUSTOM_TYPE = {
    "text": "text",
    "number": "number",
    "single_select": "select",
    "multi_select": "multiselect",
    "datetime": "datetime",
    "member": "user",          # member_setting.multi 决定单人/多人；值都是列表
}

#: 中立类型 → 自定义字段值键名（``<type>_value``）。
#: 前三者**实测**（probes/feishu_task_write_probe.py 第 4b 段）；
#: ``member_value`` 也**实测**（feishu_task_cf_probe.py，形状 ``[{"id","type"}]``）。
#: ``multi_select_value`` / ``datetime_value`` **未实测**（本机没有这两种字段），
#: 按同构推断 —— 一旦真遇到，读回是空值时**无法分辨**「没填」与「键名猜错」，
#: 所以 ``_custom_type`` 对未知类型直接报错，而不是静默给空。
_VALUE_KEY = {
    "text": "text_value",
    "number": "number_value",
    "select": "single_select_value",
    "multiselect": "multi_select_value",
    "datetime": "datetime_value",
    "user": "member_value",
}

#: 服务端的「找不到」错误码。实测两处都会回它：``tasks.get`` 取不存在的 guid、
#: ``tasks.delete`` 删不存在的 guid。前者必须**翻译成 None**（契约规定 get_row
#: 找不到返回 None、不抛异常），后者必须**照样抛**（删除不幂等，删不掉≠已删掉）。
_NOT_FOUND = 1470404

#: 写后读的轮询（实测 ~0.4s 可见；留足余量）。
_SETTLE_RETRIES = 4
_SETTLE_SLEEP = 0.8


class _TaskCli:
    """``lark-cli task`` 子进程传输层。

    为什么不复用 ``adapters/feishu.py`` 的 ``_CliTransport``：
    那个类的 ``_exec`` 把子命令域写死成 ``base``，而且有一张 ``_JSON_BODY_CMDS``
    白名单规定「哪些子命令用 ``--json`` 传体」。任务的域是 ``task``、体一律走
    ``--data``，两者的差异点全是**硬编码**的 —— 为了复用去改那 1000 行里被大量
    测试覆盖的传输层，风险远大于这里的几十行重复。宁可有意重复，注释说明白。

    **不使用 shell**：argv 数组直接交给 ``CreateProcessW``，中文摘要不会被转义折腾。
    """

    def __init__(self, cli_path: str, identity: str = "", timeout: int = 180):
        self.cli_path = cli_path
        self.identity = identity or ""
        self.timeout = timeout

    def _exec(self, args: List[str], timeout: int = None) -> Tuple[int, str, str]:
        cmd = [self.cli_path, "task"] + list(args)
        if self.identity:
            cmd += ["--as", self.identity]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout or self.timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()

    @staticmethod
    def _loads(text: str):
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

    def call(self, cmd: str, args: List[str], body: dict = None,
             timeout: int = None) -> dict:
        """执行一条 ``lark-cli task …``，返回信封的 ``data`` 段；失败抛 ``FeishuError``。

        ``cmd`` 是 ``domain.sub`` 形式的诊断名（如 ``tasks.delete``），**只用于**
        决定要不要补 ``--yes``，不是实际参数。
        """
        argv = list(args)
        if cmd in _HIGH_RISK:
            argv.append("--yes")
        tmp = None
        if body is not None:
            fd, tmp = tempfile.mkstemp(prefix="wb_task_", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False)
            argv += ["--data", "@" + tmp.replace("\\", "/")]
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
            raise FeishuError("lark-cli task 输出无法解析（rc=%s）：stdout=%r stderr=%r"
                              % (rc, out[:300], err[:300]))
        if not env.get("ok"):
            e = env.get("error") or {}
            raise FeishuError(
                "飞书任务调用失败：%s（code=%s%s%s）"
                % (e.get("message") or env, e.get("code"),
                   "，type=%s" % e.get("type") if e.get("type") else "",
                   "，log_id=%s" % e.get("log_id") if e.get("log_id") else ""),
                code=e.get("code"), log_id=e.get("log_id"), raw=e)
        return env.get("data") or {}

    def probe_identity(self) -> dict:
        """``lark-cli auth status``：确认目标身份可用（与 Base 侧同一份登录态）。"""
        p = subprocess.run([self.cli_path, "auth", "status"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        env = self._loads((p.stdout or "").strip())
        if not isinstance(env, dict):
            raise FeishuError("lark-cli auth status 解析失败：%r" % (p.stdout or "")[:300])
        return env


class FeishuTaskAdapter(BaseAdapter):
    """飞书任务适配器。表=任务清单，行=任务。"""

    backend = "feishu_task"

    #: 能力声明（声明式，不联网、不认证）：
    #:   · read / write / update / delete —— 8 个必修方法全部真实现
    #:   · schema_manage —— ``tasklists.create`` 建清单、``custom_fields.create``
    #:     建列，实测可用，故 ``ensure_table`` 真能建
    #:   · server_row_id —— ``guid`` 由服务端生成，客户端不可指定
    #:   · **不声明 batch_write**：任务没有批量创建接口，``append_rows`` 只能逐条，
    #:     声明了就是在骗调用方「这里能一次写很多」。
    #:   · **不声明 link / link_read**：见 ``link()`` 的说明 ——
    #:     飞书任务没有「同一 link_id 两张表各记一次」的关联列模型，
    #:     唯一候选 ``dependencies`` 实测全空且形状未知，读了也是猜。
    #:   · **不声明 query_pushdown**：``query()`` 走基类的内存过滤。服务端确实有
    #:     ``tasks.list`` 的 ``completed`` 过滤，但它只覆盖一个字段、且
    #:     ``type=my_tasks`` 的语义与本适配器的「清单=表」不同，无法对任意 filters
    #:     无条件成立 —— 宁可不声明，也不声明一个「有时成立」的能力。
    #:   · **不声明 idempotent**（无幂等键）、**不声明 optimistic_lock**
    #:     （任务没有行级版本号）。
    CAPS = frozenset({
        CAP_READ, CAP_WRITE, CAP_UPDATE, CAP_DELETE, CAP_SCHEMA_MANAGE,
        CAP_SERVER_ROW_ID,
    })

    def __init__(self, cli_path: str = "", identity: str = "", server: str = "",
                 base_name: str = None, timeout: int = 180):
        self.cli_path = cli_path or ""
        self.identity = identity or ""
        self.server = server or ""
        self.base_name = base_name or "default"
        self.timeout = int(timeout or 180)
        self._t: Optional[_TaskCli] = None
        self._lists: Optional[Dict[str, str]] = None     # 清单名 -> guid
        self._guid2name: Optional[Dict[str, str]] = None
        self._fields: Dict[str, List[dict]] = {}         # 清单 guid -> 自定义字段列表
        #: 最近一次写操作里**被剔除的列**（只读列）。写操作不该静默丢东西，
        #: 但也不能一遇到只读列就炸 —— 上游大量「读出来再写回去」的调用会因此全断。
        #: 折中：剔剔除得**有据可查**，调用方需要时可读这里做校验/告警。
        self.last_skipped: List[str] = []

    # ── 生命周期 ────────────────────────────────────────
    def auth(self) -> None:
        """定位 lark-cli 并确认身份可用。**不做任何写操作。**"""
        path = find_lark_cli(self.cli_path)
        if not path:
            raise FeishuError(
                "找不到 lark-cli。飞书任务后端需要①装好并登录 lark-cli"
                "（`lark-cli auth login`，需 ``task:*`` scope），"
                "或②在配置里用 feishu_task.cli_path 显式指定可执行文件路径。")
        self._t = _TaskCli(path, identity=self.identity, timeout=self.timeout)
        st = self._t.probe_identity()
        ident = self.identity or st.get("defaultAs") or "user"
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
        self._invalidate()

    def close(self) -> None:
        """丢掉元数据缓存。任务走子进程、无服务端会话，但缓存不该长期驻留。"""
        self._invalidate()

    def _invalidate(self) -> None:
        self._lists = None
        self._guid2name = None
        self._fields = {}

    def _call(self, cmd: str, args: List[str], body: dict = None,
              timeout: int = None) -> dict:
        if self._t is None:
            self.auth()
        return self._t.call(cmd, args, body=body, timeout=timeout)

    def describe(self) -> str:
        return "%s(%s, identity=%s) caps=%s" % (
            type(self).__name__, self.backend, self.identity or "auto",
            ",".join(sorted(self.capabilities())) or "-")

    # ── 清单（表）解析 ──────────────────────────────────
    def _load_lists(self, force: bool = False) -> None:
        if self._lists is not None and not force:
            return
        d = self._call("tasklists.list", ["tasklists", "list"])
        self._lists = {}
        for it in (d.get("items") or []):
            if it.get("name"):
                self._lists[str(it["name"])] = it.get("guid")
        self._guid2name = {v: k for k, v in self._lists.items() if v}

    def list_tables(self) -> List[str]:
        """本账号能看到的**任务清单名**（排序后）。诊断/部署前检查用。"""
        self._load_lists(force=True)
        return sorted(self._lists)

    def _list_guid(self, table: str) -> str:
        """把「清单名或 guid」解析成 guid。**解析不到就报错并列出可选清单**。

        为什么显式解析：CLI 接受名字或 guid，看起来省事 —— 但拼错名字时它回一个
        泛化的 not_found，让人分不清是清单错、权限错还是任务错。先取回清单表，
        报错时就能把现有清单一起列出来，一次定位。
        """
        self._load_lists()
        if not table:
            raise ValueError("表名不能为空（飞书任务里「表」= 任务清单，"
                             "需传清单名或 guid）")
        t = str(table)
        if t in self._lists:
            return self._lists[t]
        if self._guid2name and t in self._guid2name:
            return t
        raise KeyError("任务清单不存在：%r（本账号现有：%s）"
                       % (table, " / ".join(sorted(self._lists)) or "（空）"))

    def table_exists(self, table: str) -> bool:
        """按清单清单判断，不抛异常。"""
        try:
            self._load_lists()
        except Exception:
            return False
        return str(table) in self._lists or str(table) in (self._guid2name or {})

    # ── 列（元数据）─────────────────────────────────────
    def _custom_fields(self, lguid: str) -> List[dict]:
        """某清单的自定义字段定义（缓存）。"""
        if lguid not in self._fields:
            d = self._call("custom_fields.list",
                           ["custom_fields", "list",
                            "--resource-type", "tasklist", "--resource-id", lguid])
            self._fields[lguid] = list(d.get("items") or [])
        return self._fields[lguid]

    def _custom_type(self, f: dict) -> str:
        raw = str(f.get("type") or "").lower()
        if raw not in _CUSTOM_TYPE:
            raise Unsupported(
                "飞书任务自定义字段「%s」的类型 %r 本适配器**未实测**（已实测：%s），"
                "不敢按猜的形状读写。请先把该字段类型补进 _CUSTOM_TYPE，或改用原始接口。"
                % (f.get("name"), raw, " / ".join(sorted(_CUSTOM_TYPE))))
        return _CUSTOM_TYPE[raw]

    def _option_names(self, f: dict) -> List[str]:
        s = f.get("single_select_setting") or f.get("multi_select_setting") or {}
        return [str(o.get("name")) for o in (s.get("options") or []) if o.get("name")]

    def _column_map(self, lguid: str) -> Dict[str, dict]:
        """列名 -> 列定义（内置 16 列 + 该清单的自定义字段）。**单一真源**。

        ⚠️ **重名规则（实测踩过，必须显式处理）**：本机「项目」清单里就有一个
        用户自定义字段叫「状态」，与内置的 ``status`` 列**同名**。而任务对象上
        ``status``（``todo`` / ``done``）和这个自定义「状态」（进行中/已完成/暂停中…）
        是**两个完全不同的东西**。如果按普通 dict 覆盖，内置列会被**静默顶掉**，
        读出来的值换了一个来源，而列名还叫「状态」—— 没有任何迹象。

        因此本适配器的规则是：**内置列名永远属于内置列**；自定义字段若与内置列
        重名，改名为 ``<原名>(自定义)``（并在列定义里用 ``origin_name`` 记下原名）。
        两边都在、都可用、名字可预测，靠名字就能判断读到的是哪一个。

        两种「改名也救不了」的情况**直接抛错**，不叠加后缀：
          · ``<原名>(自定义)`` 也被占用（例如清单里真有一个字段就叫
            「状态(自定义)」）；
          · 两个自定义字段本身重名。
        这两种情况下「按名字取值」本身就是歧义的，继续编后缀只会把歧义藏起来。
        """
        out: Dict[str, dict] = {}
        for name, typ, key, w in _BUILTIN_COLUMNS:
            out[name] = {"name": name, "type": typ, "key": key, "writable": w}
        custom_keys = set()
        for f in self._custom_fields(lguid):
            nm = f.get("name")
            if not nm:
                continue
            nm = str(nm)
            col = {"type": self._custom_type(f), "key": f.get("guid"),
                   "writable": True, "custom": True, "_raw": f}
            if nm in custom_keys:
                # 两个自定义字段本身就重名 → 按名字取值是歧义的，加后缀也救不了
                raise KeyError(
                    "清单里有**两个**自定义字段都叫「%s」，按名字取值是歧义的。"
                    "请先在飞书里把其中一个改名。" % nm)
            if nm in out:
                # 撞上内置列 → 改名（内置列名永远属于内置列）
                renamed = nm + _CUSTOM_SUFFIX
                if renamed in out:
                    raise KeyError(
                        "自定义字段「%s」与内置列同名，改名成「%s」后**仍然冲突**"
                        "（已存在同名列）。按名字取值在这个清单上是歧义的，"
                        "请先在飞书里把其中一个字段改名。" % (nm, renamed))
                col["origin_name"] = nm
                nm = renamed
            custom_keys.add(nm)
            col["name"] = nm
            out[nm] = col
        return out

    def get_metadata(self, table: str) -> Dict[str, Any]:
        """列 = 内置属性（16 列）+ 该清单的自定义字段。

        每列给 ``name`` / ``type``（中立类型）/ ``key``（飞书字段名；自定义字段为
        其 guid）/ ``writable``；自定义字段另带 ``custom=True``，
        被重名规则改过名的还带 ``origin_name``（原字段名）。
        ``writable=False`` 的列在写入时会被剔除并登记到 ``last_skipped``。
        """
        lguid = self._list_guid(table)
        cols: List[Dict[str, Any]] = []
        for col in self._column_map(lguid).values():
            pub = {k: v for k, v in col.items() if not k.startswith("_")}
            if col.get("custom"):
                opts = self._option_names(col.get("_raw") or {})
                if opts:
                    pub["options"] = opts
            cols.append(pub)
        return {"table_name": table, "table_guid": lguid, "columns": cols}

    # ── 值翻译 ─────────────────────────────────────────
    @staticmethod
    def _ms_to_date(ms) -> str:
        """毫秒时间戳 → ``YYYY-MM-DD``（本地时区）。

        约定与 ``adapters/feishu.py`` 的 datetime 一致（下游同一套代码吃多个后端）。
        **时分秒会丢** —— 任务场景下「截止到某天」是主用法；需要精确时间请用
        ``get_row`` 的原始对象。
        """
        try:
            v = int(str(ms or "0"))
        except (TypeError, ValueError):
            return ""
        if v <= 0:
            return ""
        return time.strftime("%Y-%m-%d", time.localtime(v / 1000.0))

    @classmethod
    def _dts_to_date(cls, v) -> str:
        """``{"timestamp":"<ms>","is_all_day":bool}`` → ``YYYY-MM-DD``。"""
        if isinstance(v, dict):
            return cls._ms_to_date(v.get("timestamp"))
        return cls._ms_to_date(v)

    def _to_row(self, task: dict, cmap: Dict[str, dict]) -> Dict[str, Any]:
        """服务端 task 对象 → {中文列名: 值, '__row_id__': guid}。

        形状约定：
          · 完成状态 → ``bool``（由 ``completed_at`` 推导，``"0"`` 即未完成）
          · 各时间列 → ``YYYY-MM-DD``（空/0 → ``""``）
          · 负责人 / 关注人 → **用户 id 字符串列表**（按 role 拆开）
          · 自定义字段 → 按类型取值；单选**转成选项名**（对外说人话），
            选项 guid 查不到的（选项被删/隐藏）原样保留 guid 并加前缀 ``?``
            以便一眼看出是「没翻译出来」而不是「就叫这个名」
          · 空值一律 ``""`` / ``[]``，**不给 None**（下游到处 ``.get(col, "")``）
        """
        row: Dict[str, Any] = {"__row_id__": task.get("guid")}
        members = task.get("members") or []
        by_role: Dict[str, List[str]] = {"assignee": [], "follower": []}
        for m in members:
            role = str(m.get("role") or "")
            if role in by_role and m.get("id"):
                by_role[role].append(str(m["id"]))

        raw = {
            "摘要": task.get("summary") or "",
            "描述": task.get("description") or "",
            "完成状态": bool(str(task.get("completed_at") or "0") not in ("", "0")),
            "完成时间": self._ms_to_date(task.get("completed_at")),
            "开始时间": self._dts_to_date(task.get("start")),
            "截止时间": self._dts_to_date(task.get("due")),
            "负责人": by_role["assignee"],
            "关注人": by_role["follower"],
            "里程碑": bool(task.get("is_milestone")),
            "状态": task.get("status") or "",
            # dependencies 元素形状未实测 → 原样透传，不解释
            "前置任务": task.get("dependencies") or [],
            "父任务": task.get("parent_task_guid") or "",
            "子任务数": int(task.get("subtask_count") or 0),
            "创建时间": self._ms_to_date(task.get("created_at")),
            "更新时间": self._ms_to_date(task.get("updated_at")),
            "任务链接": task.get("url") or "",
        }
        for k, v in raw.items():
            if k in cmap:
                row[k] = v
        # 自定义字段先全部填空 —— 否则「任务没填这个字段」会表现为**键不存在**，
        # 而「键不存在」在别处又可能意味着「没取」，两者必须区分得开。
        for c in cmap.values():
            if c.get("custom"):
                row[c["name"]] = ""
        # 自定义字段：值按 <type>_value 取
        for cf in (task.get("custom_fields") or []):
            g = cf.get("guid")
            col = next((c for c in cmap.values() if c.get("custom") and c.get("key") == g),
                       None)
            if col is None:
                continue                       # 不在本清单的字段 → 忽略（如实：本表没这列）
            typ = col["type"]
            vk = _VALUE_KEY.get(typ)
            val = cf.get(vk) if vk else None
            if typ == "user":
                # 实测 member_value = [{"id": "ou_…", "type": "user"}]
                # （member_setting.multi=false 时**也是列表**）→ 统一摊平成 id 列表，
                # 与内置的「负责人」「关注人」两列形状一致
                val = self._id_list(val)
            elif typ in ("select", "multiselect") and val:
                # 读回来的是选项 guid → 翻成人话；翻不出来时加 "?" 前缀，
                # 让它一眼可辨是「没翻译出来」而不是「字段值就叫这个」
                raw_f = col.get("_raw") or {}
                s = (raw_f.get("single_select_setting")
                     or raw_f.get("multi_select_setting") or {})
                g2n = {str(o.get("guid")): str(o.get("name"))
                       for o in (s.get("options") or [])}
                if isinstance(val, list):
                    val = [g2n.get(str(x), "?" + str(x)) for x in val]
                else:
                    val = g2n.get(str(val), "?" + str(val))
            row[col["name"]] = "" if val is None else val
        return row

    def _to_body(self, data: Dict[str, Any], cmap: Dict[str, dict]):
        """{中文列名: 值} → (patch_body, update_fields, members, unknown)。

        拆分依据是**实测的写语义**（见模块 docstring 第 9~15 条）：
          · 成员（负责人/关注人）不在 ``update_fields`` 白名单里 → 单独走 members 接口
          · 自定义字段合成 ``custom_fields`` 数组，键为 ``<type>_value``
          · 只读列剔除并登记；未知列**直接报错**（拼错列名必须立刻可见）
        返回的 ``patch_body`` 与 ``update_fields`` 由**同一个 dict 派生** ——
        从结构上保证两者一致（第 10、11 条那两个坑就不可能发生）。
        """
        self.last_skipped = []
        patch: Dict[str, Any] = {}
        members: Dict[str, List[str]] = {}
        cf_payload: List[dict] = []
        unknown: List[str] = []

        for name, value in (data or {}).items():
            if name == "__row_id__":
                continue
            col = cmap.get(name)
            if col is None:
                unknown.append(str(name))
                continue
            if not col.get("writable"):
                self.last_skipped.append(str(name))
                continue
            if col.get("custom"):
                key = _VALUE_KEY.get(col["type"])
                if not key:
                    raise Unsupported("自定义字段「%s」类型 %s 的值键未实测"
                                      % (name, col["type"]))
                cf_payload.append({"guid": col["key"],
                                   key: self._cf_value(col, value)})
                continue
            k = col["key"]
            if k == "@assignee":
                members["assignee"] = self._id_list(value)
            elif k == "@follower":
                members["follower"] = self._id_list(value)
            elif k == "@completed":
                patch["completed_at"] = (str(int(time.time() * 1000))
                                         if self._truthy(value) else "0")
            elif k in ("start", "due"):
                patch[k] = self._date_cell(value)
            elif k == "completed_at":
                patch[k] = ("" if value in ("", None)
                            else str(int(value)) if not isinstance(value, str)
                            else value)
            elif k == "is_milestone":
                patch[k] = self._truthy(value)
            else:
                patch[k] = value
        if cf_payload:
            patch["custom_fields"] = cf_payload
        if unknown:
            raise ValueError(
                "列不存在（拒绝写入，以免拼错列名被静默丢弃）：%s。本清单可用列：%s"
                % (" / ".join(unknown),
                   " / ".join(sorted(c for c in cmap if not c.startswith("@")))))
        return patch, members

    @staticmethod
    def _truthy(v) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        return str(v).strip().lower() not in ("", "0", "false", "no", "否", "未完成")

    @staticmethod
    def _id_list(v) -> List[str]:
        """把负责人/关注人写法归一成 id 列表。

        接受：``"ou_x"`` / ``["ou_x"]`` / ``[{"id":"ou_x"}]`` ——
        读回来的是第 3 种（本适配器给的是第 2 种），写回去必须能用。
        """
        if v is None or v == "":
            return []
        items = v if isinstance(v, (list, tuple)) else [v]
        out = []
        for x in items:
            if isinstance(x, dict):
                x = x.get("id")
            if x:
                out.append(str(x))
        return out

    @staticmethod
    def _date_cell(v) -> dict:
        """``YYYY-MM-DD`` / 毫秒 / ``{"timestamp","is_all_day"}`` → 服务端日期单元。

        带时分（``YYYY-MM-DD HH:MM``）时 ``is_all_day=False``，否则 ``True``。
        """
        if isinstance(v, dict):
            ts = v.get("timestamp")
            return {"timestamp": str(int(ts)), "is_all_day": bool(v.get("is_all_day", True))}
        if isinstance(v, (int, float)):
            return {"timestamp": str(int(v)), "is_all_day": True}
        s = str(v or "").strip()
        if not s:
            raise ValueError("日期列为空：飞书任务的时间单元不接受空值，"
                             "要清空请显式传空串以外的语义（本适配器不猜）")
        all_day = len(s) <= 10
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                t = time.strptime(s, fmt)
                return {"timestamp": str(int(time.mktime(t) * 1000)),
                        "is_all_day": all_day}
            except ValueError:
                continue
        raise ValueError("无法解析的日期：%r（支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / "
                         "毫秒时间戳 / {'timestamp','is_all_day'}）" % v)

    def _cf_value(self, col: dict, value):
        """自定义字段值：对外写法 → 服务端写法。

        · ``select`` / ``multiselect``：**选项名 → 选项 guid**。实测必须如此，
          给名字直接 1470400；而读回来又是 guid（见 ``_option_guid``）。
        · ``user``（member 类型字段，实测 ``member_value``）：``"ou_x"`` /
          ``["ou_x"]`` / ``[{"id":"ou_x"}]`` → ``[{"id":"ou_x","type":"user"}]``。
        · 其余原样透传。
        """
        t = col["type"]
        if t in ("select", "multiselect"):
            return self._option_guid(col, value)
        if t == "user":
            return [{"id": i, "type": "user"} for i in self._id_list(value)]
        return value

    def _option_guid(self, col: dict, value):
        """单选/多选：选项名 → 选项 guid（对外用名字，对内用 guid）。

        ⚠️ 实测：直接给服务端选项名会被拒（1470400 "isn't a visible option"），
        读回来又是 guid —— 这个翻译只能由适配器做。**名字不存在时报错并列出可选值**，
        绝不静默丢弃（丢弃会让「设为高优先级」变成「什么都没设」）。
        """
        names = self._option_names(col.get("_raw") or {})
        raw_f = col.get("_raw") or {}
        s = raw_f.get("single_select_setting") or raw_f.get("multi_select_setting") or {}
        g2n = {str(o.get("guid")): str(o.get("name"))
               for o in (s.get("options") or [])}
        vals = value if isinstance(value, (list, tuple)) else [value]
        out = []
        for v in vals:
            if v is None or v == "":
                continue
            sv = str(v)
            if sv in g2n:                       # 已经是 guid（读回来的形状）→ 原样
                out.append(sv)
            elif sv in names:
                out.append(next(g for g, n in g2n.items() if n == sv))
            else:
                raise ValueError(
                    "自定义字段「%s」没有选项 %r（可选：%s）。飞书**不会**替你新增选项，"
                    "选项只能在建字段时一次给全。"
                    % (col["name"], sv, " / ".join(names) or "（该字段没有选项）"))
        return out if isinstance(value, (list, tuple)) else (out[0] if out else "")

    # ── 读 ─────────────────────────────────────────────
    def _iter_task_guids(self, lguid: str, page_size: int = 100):
        """分页取某清单下的所有任务 guid。终止条件看 ``has_more``（服务端字段）。"""
        token = None
        while True:
            args = ["tasklists", "tasks", "--tasklist-guid", lguid,
                    "--page-size", str(page_size)]
            if token:
                args += ["--page-token", token]
            d = self._call("tasklists.tasks", args)
            for it in (d.get("items") or []):
                if it.get("guid"):
                    yield str(it["guid"])
            if not d.get("has_more"):
                return
            token = d.get("page_token")
            if not token:                        # has_more 却没有下一页令牌 —— 不猜，停
                return

    def list_rows(self, table: str) -> List[Dict[str, Any]]:
        """列出清单里的全部任务，**每行都是完整详情**。

        ⚠️ **代价是 N+1 次调用**：列表接口只回 7 个摘要字段（连 description 都没有，
        实测见模块 docstring 第 5 条），要让 ``get_metadata`` 声明的每一列都**真的有值**，
        就只能逐条 ``tasks.get``。100 条任务 ≈ 100 次子进程调用。

        为什么不用「列表拿来当行、详情列留空」省事：那会让所有详情列看起来
        「本来就是空的」，调用方**无法区分「没填」和「没取」** —— 这正是本仓库
        反复吃过亏的那类静默失败。宁可慢，不要假。
        只想要摘要请显式用 :meth:`list_rows_brief`（一次调用，7 列）。
        """
        lguid = self._list_guid(table)
        cmap = self._column_map(lguid)
        rows = []
        for g in self._iter_task_guids(lguid):
            try:
                d = self._call("tasks.get", ["tasks", "get", "--task-guid", g])
            except FeishuError as e:
                if e.code == _NOT_FOUND:
                    # 列表说有、详情取不到 —— 不静默跳过（那会少数据且不报错）
                    raise FeishuError(
                        "任务 %s 在清单「%s」里，但 tasks.get 取不到详情"
                        "（可能在两次调用之间被删了）。不静默跳过："
                        "少返回一行而调用方不知道，比报错危险得多。" % (g, table))
                raise
            t = d.get("task")
            if not isinstance(t, dict):
                raise FeishuError("任务 %s 在清单「%s」里，但 tasks.get 没回 task 对象"
                                  % (g, table))
            rows.append(self._to_row(t, cmap))
        return rows

    def list_rows_brief(self, table: str) -> List[Dict[str, Any]]:
        """**只走列表接口**的廉价读法：一次调用，但**只有 7 列**。

        实测列：``__row_id__ / 摘要 / 完成状态 / 完成时间 / 开始时间 / 截止时间 /
        负责人``（列表项原样字段是 ``guid, summary, completed_at, start, due,
        members, subtask_count``；``子任务数`` 也有，故一并给出）。
        其余列（描述/状态/自定义字段/…) **本方法不取**，**也不会返回空占位** ——
        缺失的键就是**不存在**，与 ``list_rows`` 的「齐全但慢」形成明确区分。
        """
        lguid = self._list_guid(table)
        out = []
        token = None
        while True:
            args = ["tasklists", "tasks", "--tasklist-guid", lguid, "--page-size", "100"]
            if token:
                args += ["--page-token", token]
            d = self._call("tasklists.tasks", args)
            for it in (d.get("items") or []):
                if not it.get("guid"):
                    continue
                by_role = {"assignee": [], "follower": []}
                for m in (it.get("members") or []):
                    r = str(m.get("role") or "")
                    if r in by_role and m.get("id"):
                        by_role[r].append(str(m["id"]))
                out.append({
                    "__row_id__": str(it["guid"]),
                    "摘要": it.get("summary") or "",
                    "完成状态": bool(str(it.get("completed_at") or "0") not in ("", "0")),
                    "完成时间": self._ms_to_date(it.get("completed_at")),
                    "开始时间": self._dts_to_date(it.get("start")),
                    "截止时间": self._dts_to_date(it.get("due")),
                    "负责人": by_role["assignee"],
                    "子任务数": int(it.get("subtask_count") or 0),
                })
            if not d.get("has_more"):
                break
            token = d.get("page_token")
            if not token:
                break
        return out

    def get_row(self, table: str, row_id: str) -> Optional[Dict[str, Any]]:
        """单行读取（一次调用，列齐全）。任务不存在返回 ``None``。

        ⚠️ 实测：``tasks.get`` 对不存在的 guid **不是**回一个空的 ``data.task``，
        而是直接抛 **1470404 not_found**。契约（见 ``base.py``）规定
        「找不到返回 None，不抛异常」，所以这里把那个错误码**翻译成 None**；
        其余错误照常抛（不能把所有失败都当成「没有」）。
        """
        lguid = self._list_guid(table)
        cmap = self._column_map(lguid)
        try:
            d = self._call("tasks.get", ["tasks", "get", "--task-guid", str(row_id)])
        except FeishuError as e:
            if e.code == _NOT_FOUND:
                return None
            raise
        t = d.get("task")
        return None if not isinstance(t, dict) else self._to_row(t, cmap)

    # ── 写 ─────────────────────────────────────────────
    def append_row(self, table: str, data: Dict[str, Any]) -> str:
        """新建任务，返回新任务 guid。

        **强制要求清单**（``table`` 必须能解析成一个任务清单）：实测 ``tasks.create``
        **不带 ``tasklists`` 照样成功**，任务会变成不属于任何清单的**孤儿** ——
        ``list_rows(任何清单)`` 永远看不到它，而调用方以为建好了。这种「建成功了但
        谁也找不到」比直接报错危险得多，所以这里不给「不传清单」这条路。
        """
        lguid = self._list_guid(table)
        cmap = self._column_map(lguid)
        self.last_skipped = []
        body: Dict[str, Any] = {"tasklists": [{"tasklist_guid": lguid}]}

        if not str(data.get("摘要") or "").strip():
            raise ValueError("新建飞书任务必须有「摘要」（服务端 required 字段）")

        unknown = [str(k) for k in (data or {})
                   if k != "__row_id__" and k not in cmap]
        if unknown:
            raise ValueError("列不存在（拒绝写入）：%s。本清单可用列：%s"
                             % (" / ".join(unknown),
                                " / ".join(sorted(c for c in cmap if not c.startswith("@")))))

        cf: List[dict] = []
        members: List[dict] = []
        for name, value in (data or {}).items():
            if name == "__row_id__":
                continue
            col = cmap.get(name)
            if col is None or not col.get("writable"):
                if col is not None:
                    self.last_skipped.append(str(name))
                continue
            if col.get("custom"):
                key = _VALUE_KEY.get(col["type"])
                if not key:
                    raise Unsupported("自定义字段「%s」类型 %s 的值键未实测"
                                      % (name, col["type"]))
                cf.append({"guid": col["key"], key: self._cf_value(col, value)})
                continue
            k = col["key"]
            if k == "@assignee":
                members += [{"id": i, "role": "assignee", "type": "user"}
                            for i in self._id_list(value)]
            elif k == "@follower":
                members += [{"id": i, "role": "follower", "type": "user"}
                            for i in self._id_list(value)]
            elif k == "@completed":
                if self._truthy(value):
                    body["completed_at"] = str(int(time.time() * 1000))
            elif k in ("start", "due"):
                body[k] = self._date_cell(value)
            elif k == "completed_at":
                if value not in ("", None):
                    body[k] = str(value)
            elif k == "is_milestone":
                body[k] = self._truthy(value)
            else:
                body[k] = value
        if cf:
            body["custom_fields"] = cf
        if members:
            body["members"] = members

        d = self._call("tasks.create", ["tasks", "create"], body=body)
        t = d.get("task") or {}
        g = t.get("guid") or d.get("guid")     # 实测是 data.task.guid；d.guid 兜底
        if not g:
            raise FeishuError("tasks.create 未返回 guid，无法确认任务已建立：%r"
                              % (d,))
        return str(g)

    def update_row(self, table: str, row_id: str, data: Dict[str, Any]) -> None:
        """更新任务。自动把入参拆成三条路：``patch`` / ``members`` /（剔除只读列）。

        ⚠️ 为什么必须这么拆（都是实测，见模块 docstring 第 9~12 条）：
          · ``tasks.patch`` 的 ``update_fields`` 是**固定 14 项白名单**，
            **``members`` 不在其中** → 成员改动一律走 ``task members add/remove``。
          · 白名单里的字段**必须在 body 里同时给出非空值**，只列不给会整条失败。
            本方法让 ``update_fields`` 与 body 由同一个 dict 派生，结构上不可能不一致。
          · body 里有、白名单里没有的字段会被**静默忽略** —— 同一份来源也消灭了它。

        成员写入语义（实测）：``members add`` 是**追加**、``remove`` 只摘指定
        ``(id, role)``。所以「改负责人」= 先读当前成员 → 算出需要 add 的与需要
        remove 的 → 两次调用。⚠️ **这不是并发安全的**（读-改-写），与
        ``link_append`` 的已知局限同类：同一任务被并发改成员时可能丢失更新。
        需要严格并发请自行串行化。
        """
        lguid = self._list_guid(table)
        cmap = self._column_map(lguid)
        patch, members = self._to_body(data, cmap)
        row_id = str(row_id)

        if members:
            cur: Dict[str, List[str]] = {"assignee": [], "follower": []}
            d = self._call("tasks.get", ["tasks", "get", "--task-guid", row_id])
            for m in ((d.get("task") or {}).get("members") or []):
                r = str(m.get("role") or "")
                if r in cur and m.get("id"):
                    cur[r].append(str(m["id"]))
            to_add, to_del = [], []
            for role in ("assignee", "follower"):
                if role not in members:
                    continue                    # 没提到就不动这一角色（不是清空）
                want, have = members[role], cur[role]
                for i in want:
                    if i not in have:
                        to_add.append({"id": i, "role": role, "type": "user"})
                for i in have:
                    if i not in want:
                        to_del.append({"id": i, "role": role, "type": "user"})
            if to_add:
                self._call("members.add", ["members", "add", "--task-guid", row_id],
                           body={"members": to_add})
            if to_del:
                self._call("members.remove",
                           ["members", "remove", "--task-guid", row_id],
                           body={"members": to_del})

        if not patch:
            return                                  # 只有成员改动 / 全是只读列 → 完成
        bad = sorted(set(patch) - _PATCHABLE)
        if bad:
            # 本适配器不该构造出白名单外的键；真出现了就是内部 bug，必须炸出来
            raise FeishuError("内部错误：patch 体含 update_fields 白名单外的字段 %s"
                              % " / ".join(bad))
        d = self._call("tasks.patch", ["tasks", "patch", "--task-guid", row_id],
                       body={"task": patch, "update_fields": sorted(patch)})
        return d

    def delete_rows(self, table: str, row_ids: List[str]) -> None:
        """按 guid 删除（``--yes`` 由传输层按 ``cmd`` 自动补）。

        **删除不幂等**：删一个不存在的 guid 服务端回 1470404 not_found。
        本方法**如实抛错**而不是跳过 —— 「删不掉」被当成「已删掉」会留下幽灵数据。
        """
        ids = [str(x) for x in (row_ids or []) if x]
        for g in ids:
            self._call("tasks.delete", ["tasks", "delete", "--task-guid", g])

    # ── 关联：如实说做不到 ──────────────────────────────
    def link(self, table: str, other_table: str, link_id: str,
             row_id: str, other_row_ids: List[str]) -> None:
        """**不支持**：飞书任务没有「同一 link_id 两张表各记一次」的关联列模型。

        契约里的 ``link`` 指的是「两张**表**之间、同一条关系两边各记一次」，
        对应 Base 的关联字段。飞书任务里最接近的是任务的 ``dependencies``
        （前置任务）—— 但它 ①是**任务↔任务**，不是表↔表；②实测**只读**
        （不在 ``update_fields`` 白名单里）；③在本机 99 条任务里**全为空**，
        元素形状**未实测**。硬做等于猜。

        所以这里抛 ``Unsupported`` 而不是「悄悄只写一侧」——后者会让
        ``list_linked`` 读到空，「建了关联」和「没建」在数据上无法区分。
        需要「把任务放进另一个清单」请直接用 ``update_row`` 之外的上层流程调
        ``tasks.patch``（本适配器未提供该扩展方法，避免造出语义含糊的 API）。
        """
        raise Unsupported(
            "飞书任务后端不支持双向 link()：任务是「清单里的行」，没有表↔表的关联列。"
            "最接近的 dependencies（前置任务）实测只读且形状未实测，"
            "本适配器拒绝用猜的行为冒充支持。")

    def list_linked(self, table: str, row_id: str, link_id: str) -> List[str]:
        """**不支持**，理由同 :meth:`link`。

        ⚠️ 特别说明为什么**不**「顺手返回 dependencies」：本机 99 条任务里
        ``dependencies`` 全部为空，也就是说「读出来是空列表」这件事**无法**与
        「这个字段根本不生效」区分开。返回空列表就是在制造本仓库最忌讳的
        静默失败。要读前置任务请用 ``get_row()`` 的「前置任务」列（原样透传）。
        """
        raise Unsupported(
            "飞书任务后端不支持 list_linked()：没有关联列模型；"
            "dependencies 实测全空、形状未实测，返回空列表会与「真的没有」无法区分。"
            "要读前置任务请用 get_row() 的「前置任务」列。")

    # ── 表结构 ─────────────────────────────────────────
    def ensure_table(self, table: str, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        """确保清单存在；返回 ``"created"`` / ``"exists"``。

        ``columns`` 里的列若**不是**内置的 16 列之一，就按中立类型建一个自定义字段
        （``custom_fields.create``）。**只支持实测过的类型**（text / number /
        select / multiselect / datetime / user），未知类型抛 ``Unsupported`` ——
        建一个自己都不会读写的列，比报错更糟。

        ``select`` / ``multiselect`` 列**必须带上 ``options``**（名字字符串或
        ``{"name":…}``）。飞书不会替你新增选项，而选项只能建字段时一次给全。
        """
        self._load_lists()
        if str(table) in self._lists:
            return "exists"
        d = self._call("tasklists.create", ["tasklists", "create"],
                       body={"name": str(table)})
        lguid = ((d.get("tasklist") or {}).get("guid")) or d.get("guid")
        if not lguid:
            raise FeishuError("tasklists.create 未返回 guid：%r" % (d,))
        self._invalidate()

        builtin = {n for n, _t, _k, _w in _BUILTIN_COLUMNS}
        for col in (columns or []):
            nm = col.get("name")
            if not nm or nm in builtin:
                continue
            neutral = str(col.get("type") or "text").lower()
            # 写向的类型翻译交给 schema.BACKEND_TYPES["feishu_task"] —— 单一真源，
            # 免得这里再维护一份「中立类型 ↔ 飞书任务字段类型」的表（读向的
            # _CUSTOM_TYPE 是反方向，schema 没有对应能力，故留在本模块）。
            try:
                typ = schema.backend_type("feishu_task", neutral)
            except (Unsupported, ValueError) as e:
                raise Unsupported("无法建列「%s」：%s" % (nm, e))
            body: Dict[str, Any] = {"name": str(nm), "resource_id": lguid,
                                    "resource_type": "tasklist", "type": typ}
            if typ in ("single_select", "multi_select"):
                opts = col.get("options") or []
                body[typ + "_setting"] = {
                    "options": [{"name": o.get("name") if isinstance(o, dict) else str(o)}
                                for o in opts]}
            elif typ == "number":
                st = {"format": "custom", "decimal_count": 0}
                sym = (col.get("symbol") or "").strip()
                if sym:
                    st["custom_symbol"] = sym
                    st["custom_symbol_position"] = "right"
                else:
                    # 实测：format='custom' 时 custom_symbol 必填 → 不带符号就别用 custom
                    st["format"] = "plain"
                body["number_setting"] = st
            self._call("custom_fields.create", ["custom_fields", "create"], body=body)
        self._fields.pop(lguid, None)
        return "created"

    # ── 扩展方法（契约之外，但确有业务价值）──────────────
    def list_sections(self, table: str) -> List[Dict[str, Any]]:
        """清单里的**分组**（§）。飞书任务的清单内部还有一层分组，读任务时用不到，
        但做「按阶段看板」时必须。返回 ``{"name","guid","is_default"}`` 列表。"""
        lguid = self._list_guid(table)
        d = self._call("sections.list",
                       ["sections", "list", "--resource-type", "tasklist",
                        "--resource-id", lguid])
        return [{"guid": it.get("guid"), "name": it.get("name"),
                 "is_default": bool(it.get("is_default"))}
                for it in (d.get("items") or [])]

    def list_subtasks(self, task_guid: str) -> List[Dict[str, Any]]:
        """某任务的直接子任务（一次调用，字段与 ``tasks.get`` 同构）。"""
        d = self._call("subtasks.list", ["subtasks", "list", "--task-guid", str(task_guid)])
        return [self._to_row(it, {}) for it in (d.get("items") or [])]

    def create_table(self, name: str, member_ids: Optional[List[str]] = None) -> str:
        """新建任务清单，返回其 guid。``member_ids`` 里的人按 editor 加入。"""
        body: Dict[str, Any] = {"name": str(name)}
        if member_ids:
            body["members"] = [{"id": str(i), "role": "editor", "type": "user"}
                               for i in member_ids if i]
        d = self._call("tasklists.create", ["tasklists", "create"], body=body)
        g = ((d.get("tasklist") or {}).get("guid")) or d.get("guid")
        if not g:
            raise FeishuError("tasklists.create 未返回 guid：%r" % (d,))
        self._invalidate()
        return str(g)

    def rename_table(self, table: str, new_name: str) -> None:
        """给清单改名。"""
        lguid = self._list_guid(table)
        self._call("tasklists.patch", ["tasklists", "patch", "--tasklist-guid", lguid],
                   body={"tasklist": {"name": str(new_name)}, "update_fields": ["name"]})
        self._invalidate()

    def list_columns(self, table: str) -> List[Dict[str, Any]]:
        """``get_metadata(...)["columns"]`` 的直读写法（诊断/脚本里顺手）。"""
        return self.get_metadata(table)["columns"]
