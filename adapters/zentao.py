# -*- coding: utf-8 -*-
"""禅道（ZenTao）REST API v2 适配器。

这是四个底座里**最特殊**的一个：SeaTable / 飞书 / 简道云 都是「你定义表，它们存」，
而禅道是 **PM 系统**——实体（项目/执行/任务/产品/需求/Bug…）与字段都是**固定的**，
没有「建一张 IC采购记录」这种能力。所以本适配器除了实现契约，还必须带一层
**实体映射层**（见 `ENTITY_SPECS` 与 `ALIASES`），并且对映射不到的东西**如实拒绝**，
绝不去硬塞一个语义不符的实体（那会把采购数据写进 Bug 表，且没人会发现）。

实测依据（2026-10-01，本机 127.0.0.1 禅道；探测脚本
``probes/zentao_read_probe.py`` / ``zentao_read_probe2.py``，**只发 GET**）
──────────────────────────────────────────────────────────────────────────
1. 基址 ``http://<host>/zentao/api.php/v2``；鉴权是**请求头** ``token: <令牌>``。
2. ⚠️ **`/users/login`（复数）不存在**。OpenAPI 规范里写的就是这个路径，
   但实测它回 ``{"status":"fail","message":"User does not exist."}``；
   真实路径是 **`/user/login`（单数）**。
   「能跑的 setup-token.py」和「官方规范」在这里**互相矛盾**，实测站在脚本这边。
3. ⚠️ **失败不一定改 HTTP 状态码**：资源不存在时是
   **HTTP 200** + ``{"status":"fail","message":"Project does not exist."}``。
   所以判错**必须看 body 里的 ``status``**，只看 HTTP 状态码会把失败当成功。
   认证失败才是 401 + ``{"status":"fail","message":"Not allowed"}``。
4. ⚠️ 错误原因在 **``message``** 字段——而 OpenAPI 规范里**根本没有这个字段**
   （规范只声明了 ``status``）。只看规范写不出正确的错误处理。
   **而且 ``message`` 有两种形态**：字符串（``"Project does not exist."``）和
   **字典**（字段级校验错误，见第 12 条）。
5. 成功信封：``{"status":"success","<资源复数>":[…],"pager":{…}}``。
   **``pager`` 规范里也没写**，实测它有 9 个键：
   ``offset / recTotal / recPerPage / pageTotal / pageID / moduleName / methodName / params / pageCookie``。
   既然服务端自己给了 ``recTotal``/``pageTotal``，翻页就以它们为准。
6. ⚠️ **分页参数疑似不生效**：实测 ``limit=1&page=2`` 与 ``recPerPage=1&pageID=2``
   返回的 ``pager.pageID`` **恒为 1**、条数不变。（本实例只有 1 个用户，
   没法完全区分「参数无效」与「数据不足」。）因此 ``_iter_rows`` 带了硬保护：
   若「本轮 id 与上一轮完全相同」就直接报错，**绝不返回部分数据冒充全量**。
7. ⚠️ **HTTP 200 + 空响应体**：``/tasks``、``/stories``、``/requirements``、
   ``/epics``、``/feedbacks``、``/tickets``、``/builds`` 的列表 GET 返回 **0 字节**。
   适配器把它当**错误**，不返回 ``[]`` 冒充「这个表是空的」。
8. ⚠️ **HTTP 200 + HTML**：``/productplans`` 的列表 GET、``/executions/1/tasks``
   回落到了 web 路由，返回一整页 HTML。适配器检测到非 JSON 直接报错。
9. ``DELETE /<资源>/:id`` **不带请求体**（规范里没有 requestBody）。
10. 实测「列表 GET 可用」的资源：``/projects`` ``/products`` ``/programs``
    ``/executions`` ``/users`` ``/bugs`` ``/testcases`` ``/testtasks``。
    ``/tasks`` 与 ``/stories`` 列表**不可用**（见 7），但创建/改/删与按 id 取详情存在。
11. ⚠️ ``/bugs``/``/testcases``/``/testtasks`` 的列表响应里除了业务数组还塞了
    一大堆**表单初始化数据**（products / modules / projectPairs…），而且业务数组的
    键名各不相同：bugs→``bugs``、testcases→``cases``、testtasks→``tasks``。
    按「取第一个数组」解析必然拿错，必须按精确键名取。

写路径实测（2026-10-01，``probes/zentao_write_probe.py``；只在带 ``ZZ_WB_VERIFY``
标记的记录上写，且已逐条清理，基线条数复原）
──────────────────────────────────────────────────────────────────────────
12. ⚠️ **存在第三种信封：字段级校验错误用**：:

        {"result": "fail", "message": {"end": ["『计划完成』不能为空。"]}}

    **没有 ``status`` 键**（所以 ``data.get("status")`` 是 ``None``），
    而 ``message`` 是**字典**。三种信封（``status=success`` / ``status=fail`` /
    ``result=fail``）必须都当失败处理 —— 好在「``status`` 不是 ``success`` 就报错」
    这条判据天然覆盖了第三种，只是错误文案需要自己格式化
    （见 :func:`_format_failure`），否则只能打印一句 ``status=None``。
13. ⚠️ **未声明的字段会被服务端静默忽略**：``PUT /projects/1 {"zz_not_a_field":"x"}``
    回 ``status: success``（"保存成功"），那个字段**没写进去也没报错**。
    这正是适配器**必须在客户端拦未知列**（而不是丢给服务端）的实测依据。
14. ⚠️ **空值会被整体拒绝，而不是写坏**：``PUT /projects/1 {"begin": ""}`` 回
    第 12 条那种 ``result: fail``（『计划开始』不能为空），且读回**原值未变**。
    所以「空值不发送」是**实测支持的**决定，不是保守猜测。
    代价：适配器**无法清空字段** —— 要清空请到禅道界面做。
15. ``DELETE /<资源>/:id`` 删**不存在的 id** → HTTP 200 + ``status: fail`` +
    ``"Project does not exist."``（不是静默成功）。所以逐条删、非 success 即报错是对的。
16. ``POST`` 成功的信封是 ``{"message":"保存成功","id":1,"status":"success"}``，
    ``id`` 是**数字**；而列表 GET 回来的 ``id`` 是**字符串** ``"1"``。
    适配器统一 ``str()`` 化，两处都能对得上。
17. 创建 **scrum 项目会自动建一个同名产品**（列表里 ``hasProduct: "1"``）。
    也就是说写「项目」的副作用不止一行 —— 删掉项目**不会**删掉那个产品。
    ``tools/verify_zentao.py --write-test`` 因此会把「本次新增的产品」一并清理。
18. 创建项目时**没给 ``begin``，服务端自动填了当天**（实测 ``begin: "2026-10-01"``）；
    ``model`` 缺省是 ``scrum``。⇒ 规范 ``required`` 里列的
    ``model/begin/workflowGroup`` **并不是服务端真的要求**（``workflowGroup``
    规范自己还写着「付费版功能，开源版可以不填」）。所以 ``ENTITY_SPECS`` 的
    ``required`` 对「项目」取**实测最小集**，其余实体取规范值，并在
    ``required_source`` 里标出来源 —— 它是**客户端礼貌性校验**，不是接口契约。
19. ``PUT`` 成功的信封带一个 ``load`` 字段（web 跳转地址），与 API 语义无关。
20. ⚠️ **各字段的读回形状**：实测 ``id`` / ``days`` / ``progress`` / ``pri`` /
    ``path`` 等**一律回成字符串**（``"0.00"``、``"0"``），只有 ``POST`` 的 ``id`` 是数字。
21. ``GET /<资源>/:id`` **存在并可用**（实测 ``GET /products/6`` 回
    ``{"status":"success","product":{…}}``）；删除之后再取 → ``status: fail`` +
    ``"Product does not exist."``。这验证了 ``get_row`` 走详情接口那条路是对的。
22. **删除不是幂等的**：对同一个 id ``DELETE`` 第二次 → ``status: fail`` +
    ``"does not exist."``。所以 :meth:`ZentaoAdapter.delete_rows` 逐条删、非 success
    即抛，是**被实测支持**的。
23. ⚠️⚠️ **一个会让人查半天的坑**：给「项目」起一个**曾经用过（哪怕已删）**的名字，
    服务端会拒绝，理由却是「**『产品名称』已经有『X』这条记录了**」——
    因为创建项目会顺手建同名产品，而**项目创建路径的重名检查把已删除的产品也算在内**。
    实测对比：同一个名字，``POST /products`` 能成功（产品那条路径的重名检查
    **排除**已删记录），``POST /projects`` 却失败 —— **两条代码路径的查重口径不一致**。
    结论：**项目名必须唯一且不可回收**；写「项目」时务必用一次性名字。
    :meth:`ZentaoAdapter._format_failure` 会在这种错误上追加一句大白话提示。
24. ``PUT`` 成功后立刻读回是**强一致**的（实测改完 name 立刻 GET 到新值）——
    与飞书那套「写完 ~2.7s 才可见」完全不同，不需要 ``_eventually`` 轮询。

**实测依据·第二轮（2026-10-01，全量覆盖 19 资源 × 88 路径）**
──────────────────────────────────────────────────────────
探测脚本：``probes/zentao_full_read_probe.py``（只发 GET）、
``probes/zentao_full_write_probe.py``、``probes/zentao_param_probe.py``、
``probes/zentao_round2_probe.py``、``probes/zentao_round3_probe.py``（都带标记 + 级联清理）。

这一轮**推翻了第一轮的一条结论**，并补出 17 条新事实：

25. ⚠️ **推翻第 6 条：分页 ``recPerPage`` + ``pageID`` 是生效的。**
    第一轮说「不生效」是**我自己参数名写错了**（用了 ``limit``/``page``，而规范写的是
    ``recPerPage``/``pageID``）。实测 ``recPerPage=1`` 时第 1 页 id=10、第 2 页 id=11。
    **教训：得出「服务端不支持 X」之前，先确认自己把 X 的名字写对了。**

26. ⚠️ **超范围的页码会被「钳制到最后一页」，不是返回空。**
    只有 2 页时，``pageID=3`` 与 ``pageID=99`` 都返回第 2 页的数据、且 ``pager.pageID`` 回 2。
    所以**判断「读完了」只能靠 ``pager.pageTotal`` / ``recTotal``**；
    靠「本页为空」会死循环，靠「本页与上页重复」会把正常收尾误判成故障。

27. ⚠️⚠️ **``productID`` 必须作为 query string 传，放进 JSON body 会被当成「没传」。**
    实测：``POST /bugs?productID=20`` + body ``{title, openedBuild}`` → **成功**；
    同一个 body 里带 ``productID``（字符串或整数都一样）→
    ``Missing required parameter: productID.``
    而 **官方规范把它声明为 requestBody 里的 ``integer`` 属性 —— 规范是错的。**
    逐实体实测（见 ``parent_query``）：``/productplans`` 两种位置**都认**，
    ``/stories`` ``/bugs`` ``/testcases`` **只认 query**。
    所以适配器**统一走 query**（对两者都成立）。

28. ``/releases``、``/systems``、``/productplans`` 的**列表 GET 也需要 ``productID`` query**。
    规范里 ``/releases`` 与 ``/systems`` **根本没声明 GET**，但实现里有；
    不带参数时报的正是 ``Missing required parameter: productID.``
    （所以「不带参数读不到」≠「没有这个路由」）。

29. ⚠️ ``/epics`` 与 ``/requirements`` 的 **DELETE 需要 ``storyID`` query 参数**，
    而规范写的是路径参数 ``:epicID`` / ``:requirementID`` —— **规范又错了**。
    实测 ``DELETE /epics/2`` → ``Missing required parameter: storyID.``；
    ``DELETE /epics/2?storyID=2`` → 成功。

30. ⚠️ ``/stories`` ``/epics`` ``/requirements`` 的 ``reviewer`` **必须是数组**。
    传字符串 ``"admin"`` → ``reviewer：『评审人』不能为空。``；
    传 ``["admin"]`` → 成功。**规范把它声明为 string —— 规范错。**

31. ``/testtasks`` 还需要 ``status`` 字段；``/productplans`` 实测需要 ``begin`` + ``end``。
    两者规范里的 ``required`` 都没写全。

32. ``/executions`` 的 ``begin`` 必须 **≥ 所属项目的 ``begin``**
    （项目只给 ``name``+``end`` 时 ``begin`` 自动填当天，于是 ``2026-01-01`` 会被拒）。

33. ⚠️⚠️ **``POST /epics`` ``/requirements`` ``/systems`` 成功时不返回 ``id``。**
    只回 ``{"status":"success","message":"保存成功","load":"..."}``。
    要拿到刚建那行的 id，**只能反查父资源子列表**
    （``/products/:id/epics`` 等）。``append_row`` 因此必须做「创建后反查」，
    否则就违反了「返回服务端行 id」的契约。详见 ``create_id_via``。

34. ⚠️⚠️ **``status:"success"`` 也可能是「什么都没有」。**
    ``GET /<资源>/:id`` 对**不存在的 id** 返回
    ``{"status":"success","load":{"alert":"抱歉，您访问的对象不存在！","locate":"..."}}``。
    判据不能只看 ``status``，还必须看**有没有业务数据对象**（``load`` 键就是「没有」）。
    这是本适配器见过最阴的一种失败形状 —— 只看 ``status`` 的 ``get_row`` 会把
    「不存在」当成「存在但字段全空」。

35. 详情字段数（用于判断"响应被截断"）：产品 65 / 项目集 80 / 项目 80 / 执行 86 /
    构建 26 / 测试用例 56 / Bug 72 / 任务 76 / 发布 29 / 产品计划 17。
    **列表与详情的 ``id`` 一律是字符串**（POST 回的才是数字）。

36. ``/systems`` **没有 DELETE**（规范里也只有 POST/PUT）→ 应用记录建了就删不掉。
    → ``delete_rows`` 对「应用」如实抛 ``Unsupported``，而不是发一个注定失败的请求。

37. **DELETE 是软删除**：删完 ``deleted`` 变成 ``"1"``，记录仍能 ``GET`` 到、名字仍被占用。
    「删成功」不等于「真的没了」。

38. **哪些路由在本版确实不存在**（HTTP 200 + **0 字节**）：
    · 顶层列表：``/builds`` ``/epics`` ``/feedbacks`` ``/files`` ``/requirements``
      ``/stories`` ``/tasks`` ``/tickets``
    · 顶层 ``/productplans`` 回**整页 HTML**（回落到 web 路由）
    · POST：``/tickets`` ``/feedbacks``
    · 子列表：``/products/:id/{feedbacks,tickets,builds}``
    这些一律 ``listable=False`` / 如实抛 ``Unsupported``，**不返回 [] 冒充「没有数据」**。

39. **父资源子列表（实测全部可用）** —— 这是读「顶层列表不可用」那些实体的**唯一正路**：
    · ``/products/:id/{bugs,epics,productplans,releases,requirements,stories,systems,testcases,testtasks}``
    · ``/projects/:id/{bugs,builds,executions,stories,testcases,testtasks}``
    · ``/executions/:id/{bugs,builds,stories,tasks,testcases,testtasks}``
    · ``/programs/:id/{products,projects}``
    → 见 ``list_children()``。

40. ⚠️ **用 bogus id 探路由会产生假阴性。**
    第一轮用 ``/executions/999999/...`` 探，6 条子列表全回 HTML，于是记成「路由不存在」；
    第二轮用**真实执行 id** 探，**6 条全部可用**。
    教训：路由存在性必须用**真实存在的父 id** 复验，假 id 的失败形状不可作数。

41. **状态流转动作**（``PUT /<资源>/:id/<动作>``）—— 规范里的子路径，这是专业 PM 软件的
    核心能力，实测各动作的**真实必填 body**（都在规范里找不到）：
    · ``tasks/start``    要 ``consumed`` + ``left``（「"总计消耗"和"预计剩余"不能同时为0」）
    · ``tasks/finish``   要 ``realStarted`` + ``currentConsumed`` + ``finishedDate``
    · ``tasks/activate`` 要 ``left``
    · ``tasks/close``    **空 body 即可**；副作用 ``status=closed`` + ``closedDate``
    · ``bugs/resolve``   要 ``resolution`` + ``resolvedBuild``；
      副作用 ``status=resolved`` + ``resolvedBy`` + ``resolvedDate``
    · ``bugs/close``     **空 body 即可**
    · ``bugs/activate``  要 ``openedBuild``
    动作成功后**不返回 id**，且**读取是强一致的**（做完立刻 GET 就能看到新状态，
    这点与飞书那 ~2.7s 的可见性延迟不同）。
    → 见 ``run_action()`` / ``actions_of()``。

**仍未实测**
────────────
· 分页在**超过 1 页真实数据**时的表现（本机数据量始终很小，只验到 2 条 / 2 页）。
· 各实体「列表不可用」是否与服务端**权限或版本**有关（本机是单用户 admin 的全新实例）。
· 剩余动作：``stories`` ``epics`` ``requirements`` 的 ``activate/change/close``，
  ``feedbacks`` / ``tickets`` 的 ``activate/close``（后两个实体在本版根本建不出来）。
· ``/files``（附件）全程不可用，没有实测。
· 富文本字段（``spec`` / ``steps``）的真实存储形状。
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .base import (CAP_DELETE, CAP_READ, CAP_SERVER_ROW_ID, CAP_UPDATE, CAP_WRITE,
                   BaseAdapter, Unsupported)

#: 默认单页条数。**只影响请求参数**：翻页是否真的生效见模块 docstring 第 6 条，
#: 适配器不依赖它来判断「读完了没有」，而是依赖 ``pager.recTotal``/``pageTotal``。
_PAGE = 100

#: 翻页保护：这么多页还没读完就报错（防止服务端忽略分页参数时死循环）。
_MAX_PAGES = 200

#: 默认基址（私有部署时由 config 覆盖）。
_DEFAULT_BASE = "http://127.0.0.1/zentao/api.php/v2"


# ══════════════════════════════════════════════════════════════════
# 枚举：只在**规范自己把取值域写清楚了**的字段上做代码↔中文翻译
#
# 取值域来源：OpenAPI 里字段描述的括号，例如
#   ``type(类型(normal 正常 | branch 多分支 | platform 多平台))``
# 每个字面量都是规范给的，没有一个是我编的。
# 规范**没给**取值域的字段（最典型的是项目/执行的 ``status``）一律**原样透传**，
# 不猜、不造标签 —— 猜出来的「已关闭」比裸的 ``closed`` 更容易让人误信。
# ══════════════════════════════════════════════════════════════════
_ENUMS: Dict[str, Dict[str, Dict[str, str]]] = {
    "项目": {
        "model": {"scrum": "敏捷", "waterfall": "瀑布", "kanban": "看板",
                  "agileplus": "融合敏捷", "waterfallplus": "融合瀑布"},
    },
    "执行": {
        "lifetime": {"short": "短期", "long": "长期", "ops": "运维"},
        "acl": {"open": "公开", "private": "私有"},
    },
    "产品": {
        "type": {"normal": "正常", "branch": "多分支", "platform": "多平台"},
        "acl": {"open": "公开", "private": "私有"},
    },
    "需求": {
        "category": {"feature": "功能", "interface": "接口", "performance": "性能",
                     "safe": "安全", "experience": "体验", "improve": "改进",
                     "other": "其他"},
        "source": {"customer": "客户", "user": "用户", "po": "产品经理", "market": "市场",
                   "service": "客服", "operation": "运营", "support": "技术支持",
                   "competitor": "竞争对手", "partner": "合作伙伴", "dev": "开发人员",
                   "tester": "测试人员", "bug": "Bug", "forum": "论坛", "other": "其他"},
    },
    "Bug": {
        "type": {"codeerror": "代码错误", "config": "配置相关", "install": "安装部署",
                 "security": "安全相关", "performance": "性能问题",
                 "standard": "标准规范", "automation": "测试脚本",
                 "designdefect": "设计缺陷", "others": "其他"},
    },
    "测试用例": {
        "type": {"unit": "单元测试", "interface": "接口测试", "feature": "功能测试",
                 "install": "安装部署", "config": "配置相关", "performance": "性能测试",
                 "security": "安全相关", "other": "其他"},
    },
    "测试单": {
        "type": {"integrate": "集成测试", "system": "系统测试", "acceptance": "验收测试",
                 "performance": "性能测试", "safety": "安全测试"},
        "status": {"wait": "未开始", "doing": "进行中", "done": "已关闭",
                   "blocked": "被阻塞"},
    },
    "用户": {
        "visions": {"rnd": "研发综合界面", "lite": "运营管理界面"},
    },
    "发布": {
        "status": {"wait": "未开始", "normal": "已发布", "fail": "发布失败",
                   "terminate": "停止维护"},
    },
    "应用": {
        # 规范把它声明为 integer(0 否 | 1 是)，但**禅道把一切都回成字符串**，
        # 所以这里的键必须是字符串 —— 用 int 键的话 _enum_label 永远命中不了
        # （它的第一句就是 `if not isinstance(code, str): return code`）。
        "integrated": {"0": "否", "1": "是"},
    },
}


def _enum_map(table: str, field: str) -> Dict[str, str]:
    return (_ENUMS.get(table) or {}).get(field) or {}


def _enum_label(table: str, field: str, code) -> Any:
    """代码 → 中文标签；不在表里就**原样返回**（不编一个看起来很像的标签）。"""
    if not isinstance(code, str):
        return code
    m = _enum_map(table, field)
    return m.get(code, code)


def _enum_code(table: str, field: str, label) -> Any:
    """中文标签 → 代码；认不出就**原样返回**（让服务端去报它自己的错）。"""
    if not isinstance(label, str):
        return label
    m = _enum_map(table, field)
    for code, name in m.items():
        if name == label:
            return code
    return label


# ══════════════════════════════════════════════════════════════════
# 状态流转动作的字段词表
#
# 为什么单独一张表：动作要的字段（``consumed`` / ``left`` / ``realStarted`` …）
# **不是实体的通用列** —— 它们只在某个动作的请求体里有意义，
# 平时既读不出来也不该当普通列写。混进 ``ENTITY_SPECS["任务"]["columns"]``
# 会让「任务有哪些列」这个问题变糊。
#
# 每个字面值都来自实测（第 41 条），不是从规范抄的 —— 规范里一个都没写。
# ══════════════════════════════════════════════════════════════════
ACTION_FIELDS: Dict[str, str] = {
    "消耗工时": "consumed",          # tasks/start
    "预计剩余": "left",              # tasks/start、tasks/activate
    "实际开始": "realStarted",       # tasks/finish
    "本次消耗": "currentConsumed",   # tasks/finish
    "实际完成": "finishedDate",      # tasks/finish
    "解决方案": "resolution",        # bugs/resolve
    "解决版本": "resolvedBuild",     # bugs/resolve
    "影响版本": "openedBuild",       # bugs/activate（数组）
    "备注": "comment",               # 多数动作都接受，可选
}

#: 值的类型提示（只用于把「数组」型动作字段转对 —— ``openedBuild`` 实测要数组）。
ACTION_ARRAY_FIELDS = frozenset({"openedBuild"})


# ══════════════════════════════════════════════════════════════════
# 实体映射层
#
# 每个实体声明：怎么读（resource / list_key / listable）、有哪些列
# （中文列名 ↔ 禅道字段 ↔ 类型 ↔ 可写性）、创建时哪些列必填、哪些列是关联。
#
# 可写性（``W`` / ``R``）的判据**只有一条**：该字段有没有出现在 OpenAPI 规范里
# 这个实体的 **POST 或 PUT 请求体**里。出现了才标 W。规范没写、只在列表响应里
# 见过的字段（如 ``progress`` / ``openedBy``）一律标 R —— 宁可为只读（少一个功能），
# 也不要发一个服务端可能静默忽略的字段（那会变成「改了但没改」——
# 实测已确认服务端**确实**会静默忽略未知字段，见模块 docstring 第 13 条）。
#
# ``required`` 是**客户端的礼貌性校验**，不是接口契约。来源标在 ``required_source``：
#   · ``"spec"``     —— 取自 OpenAPI 规范的 ``required`` 列表。
#   · ``"measured"`` —— 实测出来的**最小可成功集合**（规范在这些地方与实际不符）。
# 规范可能比服务端更严（第 18 条），也可能**更松**（第 31 条：测试单漏了 status），
# 所以真正的兜底是「把服务端的字段级校验错误 format 成人话」（见 ``_format_failure``）。
#
# 第二轮新增的映射键（全部对应一条实测事实）：
#   · ``parent_query``   —— 该父字段必须作为 **query 参数**发送（第 27 条）。
#                          值形如 ``("所属产品", "productID")``。
#   · ``children_of``    —— ``(父实体, 父字段名, 子列表数组键)``：经父资源读子列表
#                          的路径（第 39 条）。这是读「顶层列表不可用」实体的唯一正路。
#   · ``create_id_via``  —— POST 成功但**不回 id**，靠反查这个子列表拿 id（第 33 条）。
#   · ``delete_query``   —— 删除时必须带的 query 参数名（第 29 条，规范写错了参数名）。
#   · ``no_delete_why``  —— 该实体**没有删除接口**，如实拒绝（第 36 条）。
#   · ``actions``        —— ``(中文动作名, 禅道路径段, 必填字段, 说明)``（第 41 条）。
#
# 列的类型是**给下游看的**（text/number/date/select/array/secret/longtext），
# 禅道自己把一切都回成字符串。
# ══════════════════════════════════════════════════════════════════
W, R = "W", "R"

ENTITY_SPECS: Dict[str, Dict[str, Any]] = {
    "项目集": {
        "resource": "programs",
        "list_key": "programs",
        "listable": True,
        "columns": (
            ("名称", "name", "text", W),
            ("计划开始", "begin", "date", W),
            ("计划完成", "end", "date", W),
            ("负责人", "PM", "text", W),
            ("描述", "desc", "longtext", W),
            ("状态", "status", "text", R),
            ("进度", "progress", "text", R),
            ("创建人", "openedBy", "text", R),
            ("创建日期", "openedDate", "date", R),
        ),
        "required": ("名称", "计划开始", "计划完成"),
        "required_source": "spec",      # 规范 required: name / begin / end
    },
    "项目": {        "resource": "projects",
        "list_key": "projects",
        "listable": True,
        "columns": (
            ("名称", "name", "text", W),
            ("管理方式", "model", "select", W),
            ("计划开始", "begin", "date", W),
            ("计划完成", "end", "date", W),
            ("所属项目集", "parent", "number", W),
            ("关联产品", "products", "array", W),
            ("负责人", "PM", "text", W),
            ("代号", "code", "text", R),
            ("状态", "status", "text", R),
            ("实际开始", "realBegan", "date", R),
            ("实际完成", "realEnd", "date", R),
            ("进度", "progress", "text", R),
        ),
        "required": ("名称", "计划完成"),
        # 实测最小集：只给 name 时服务端报「『计划完成』不能为空」；
        # 给 name + end 就成功（model 默认 scrum、begin 自动填当天）。
        # 规范另外还要 model / begin / workflowGroup —— 比服务端**更严**。
        "required_source": "measured",
        # 规范列的 required（**禅道字段名**）。workflowGroup 是付费版功能，
        # 规范自己的描述就写着「开源版可以不填」，所以它连列都没进 ENTITY_SPECS。
        "spec_required": ("name", "model", "begin", "end", "workflowGroup"),
        "link_cols": (("所属项目集", "parent", "项目集"), ("关联产品", "products", "产品")),
    },
    "执行": {
        "resource": "executions",
        "list_key": "executions",
        "listable": True,
        "columns": (
            ("名称", "name", "text", W),
            ("所属项目", "project", "number", W),
            ("执行类型", "lifetime", "select", W),
            ("计划开始", "begin", "date", W),
            ("计划完成", "end", "date", W),
            ("可用工作日", "days", "number", W),
            ("关联产品", "products", "array", W),
            ("负责人", "PM", "text", W),
            ("产品负责人", "PO", "text", W),
            ("访问控制", "acl", "select", W),
            ("状态", "status", "text", R),
            ("进度", "progress", "text", R),
            ("实际开始", "realBegan", "date", R),
            ("实际完成", "realEnd", "date", R),
            ("所属项目名", "projectName", "text", R),
        ),
        "required": ("名称", "所属项目", "计划开始", "计划完成"),
        "required_source": "spec",      # 规范 required: project / name / begin / end
        "link_cols": (("所属项目", "project", "项目"), ("关联产品", "products", "产品")),
    },
    "任务": {
        "resource": "tasks",
        # ⚠️ 实测 /tasks 的列表 GET 返回 HTTP 200 + 空响应体（模块 docstring 第 7 条），
        #    所以这里 listable=False：读不了就是读不了，不返回 [] 冒充「没有任务」。
        #    但**执行子列表可以读**（第 39 条）—— 见 children_of，那才是读任务的正路。
        "list_key": "tasks",
        "listable": False,
        "not_listable_why": "实测 GET /tasks 返回 HTTP 200 + 空响应体（开源版未开放任务列表接口）",
        # ⚠️ 「所属执行」走 **body**（实测 POST /tasks {name, executionID} 成功），
        #    与产品系的 productID 走 query 不同 —— 所以这里**没有** parent_query。
        "children_of": ("执行", "executionID", "tasks"),
        "columns": (
            ("名称", "name", "text", W),
            ("所属执行", "executionID", "number", W),
            ("任务类型", "type", "text", W),
            ("指派给", "assignedTo", "text", W),
            ("预计开始", "estStarted", "date", W),
            ("截止日期", "deadline", "date", W),
            ("优先级", "pri", "number", W),
            ("预计工时", "estimate", "number", W),
            ("相关需求", "story", "number", W),
            ("描述", "desc", "longtext", W),
            ("状态", "status", "text", R),
        ),
        "required": ("名称", "所属执行"),
        "required_source": "spec",      # 规范 required: name / executionID
        "link_cols": (("所属执行", "executionID", "执行"), ("相关需求", "story", "需求")),
        # 第 41 条：每个动作的必填 field **全部实测**，规范里一个都没写。
        # 「预计剩余」「实际开始」「本次消耗」「实际完成」都是**工时/日期**语义，
        # 不是可以随手给个默认值的 —— 所以它们标成必填，逼调用方明确表达。
        "actions": (
            ("启动", "start", ("消耗工时", "预计剩余"),
             "开始任务（需给本次消耗与预计剩余，两者不能同时为 0）"),
            ("完成", "finish", ("实际开始", "本次消耗", "实际完成"),
             "完成任务（需给实际开始/本次消耗/实际完成三个日期与工时）"),
            ("激活", "activate", ("预计剩余",), "重新激活任务"),
            ("关闭", "close", (), "关闭任务（空 body 即可）"),
        ),
    },
    "产品": {
        "resource": "products",
        "list_key": "products",
        "listable": True,
        "columns": (
            ("名称", "name", "text", W),
            ("所属项目集", "program", "number", W),
            ("产品类型", "type", "select", W),
            ("产品负责人", "PO", "text", W),
            ("测试负责人", "QD", "text", W),
            ("发布负责人", "RD", "text", W),
            ("访问控制", "acl", "select", W),
            ("评审人", "reviewer", "array", W),
            ("描述", "desc", "longtext", W),
            ("代号", "code", "text", R),
            ("状态", "status", "text", R),
            ("创建人", "createdBy", "text", R),
            ("创建日期", "createdDate", "date", R),
        ),
        "required": ("名称",),
        "required_source": "spec",      # 规范 required: name
        "link_cols": (("所属项目集", "program", "项目集"),),
    },
    "需求": {
        "resource": "stories",
        "list_key": "stories",
        # 同「任务」：实测列表 GET 空响应体
        "listable": False,
        "not_listable_why": "实测 GET /stories 返回 HTTP 200 + 空响应体",
        # ⚠️ 第 27/39 条：顶层读不了，但**父资源子列表可以** —— 这是读需求的唯一正路。
        #    `parent_query` 只管「POST 时这个父字段放哪」；读子列表看 `children_of`。
        "parent_query": ("所属产品", "productID"),
        # (父实体, 父字段名, 子列表响应里的数组键)
        "children_of": ("产品", "productID", "stories"),
        "columns": (
            ("标题", "title", "text", W),
            ("所属产品", "productID", "number", W),
            ("所属项目", "project", "number", W),
            ("所属执行", "execution", "number", W),
            ("优先级", "pri", "number", W),
            ("类别", "category", "select", W),
            ("来源", "source", "select", W),
            ("预计工时", "estimate", "number", W),
            ("指派给", "assignedTo", "text", W),
            ("评审人", "reviewer", "array", W),
            ("需求描述", "spec", "longtext", W),
            ("验收标准", "verify", "longtext", W),
        ),
        "required": ("标题", "所属产品", "评审人"),
        # 实测最小集：标题 + productID(query) + **reviewer 数组**。
        # 第 30 条：reviewer 传字符串会被判「不能为空」，必须传 ["admin"]。
        "required_source": "measured",
        "link_cols": (("所属产品", "productID", "产品"), ("所属执行", "execution", "执行")),
        # 第 41 条：需求的状态流转。**必填字段全部来自实测**，规范里一个都没写。
        "actions": (
            ("激活", "activate", (), "把已关闭的需求重新激活"),
            ("关闭", "close", (), "关闭需求（空 body 即可）"),
            ("变更", "change", (), "走需求变更流程"),
        ),
    },
    "Bug": {
        "resource": "bugs",
        "list_key": "bugs",
        "listable": True,
        # 第 27 条：productID 只认 query。虽然 /bugs 顶层列表可用（「全产品」视角），
        # 但按产品筛仍然要靠子列表 —— 顶层列表**没有**可用的 productID 过滤语义。
        "parent_query": ("所属产品", "productID"),
        "children_of": ("产品", "productID", "bugs"),
        "columns": (
            ("标题", "title", "text", W),
            ("所属产品", "productID", "number", W),
            ("所属项目", "project", "number", W),
            ("所属执行", "execution", "number", W),
            ("严重程度", "severity", "number", W),
            ("优先级", "pri", "number", W),
            ("Bug类型", "type", "select", W),
            # 规范声明 openedBuild 是 **array of string**（主干用 "trunk"）——
            # 标 array 后 _to_value 会把裸值包成 [值]，与实测成功的传法一致。
            ("影响版本", "openedBuild", "array", W),
            ("重现步骤", "steps", "longtext", W),
            ("相关需求", "story", "number", W),
            ("状态", "status", "text", R),
        ),
        "required": ("标题", "所属产品", "影响版本"),
        # 字段名与规范一致，但**「所属产品」必须走 query**（第 27 条）→ 标 measured。
        "required_source": "measured",
        "link_cols": (("所属产品", "productID", "产品"), ("所属执行", "execution", "执行")),
        # 第 41 条：字段级必填全部实测，规范里一个都没写。
        "actions": (
            ("解决", "resolve", ("解决方案", "解决版本"), "标记为已解决（需给方案 + 解决版本）"),
            ("关闭", "close", (), "关闭 Bug（空 body 即可）"),
            ("激活", "activate", ("影响版本",), "把已关闭的 Bug 重新激活"),
        ),
    },
    "测试用例": {
        "resource": "testcases",
        # ⚠️ 实测键名是 cases，不是 testcases（见模块 docstring 第 11 条）
        "list_key": "cases",
        "listable": True,
        "parent_query": ("所属产品", "productID"),
        "children_of": ("产品", "productID", "testcases"),
        "columns": (
            ("标题", "title", "text", W),
            ("所属产品", "productID", "number", W),
            ("优先级", "pri", "number", W),
            ("用例类型", "type", "select", W),
            ("前置条件", "precondition", "longtext", W),
            ("用例步骤", "steps", "longtext", W),
            ("期望结果", "expects", "longtext", W),
            ("相关需求", "story", "number", W),
        ),
        "required": ("标题", "所属产品"),
        "required_source": "measured",  # 规范字段名对，但「所属产品」必须走 query（第 27 条）
        "link_cols": (("所属产品", "productID", "产品"),),
    },
    "测试单": {
        "resource": "testtasks",
        # ⚠️ 实测键名是 tasks（不是 testtasks）
        "list_key": "tasks",
        "listable": True,
        "parent_query": ("所属产品", "productID"),
        "children_of": ("产品", "productID", "testtasks"),
        "columns": (
            ("名称", "name", "text", W),
            ("所属产品", "productID", "number", W),
            ("所属执行", "execution", "number", W),
            ("提测构建", "build", "number", W),
            ("类型", "type", "select", W),
            ("负责人", "owner", "text", W),
            ("状态", "status", "select", W),
            ("开始日期", "begin", "date", W),
            ("结束日期", "end", "date", W),
            ("描述", "desc", "longtext", W),
        ),
        "required": ("名称", "所属产品", "提测构建", "状态", "开始日期", "结束日期"),
        # 第 31 条：规范 required 漏了 ``status``（实测不给报「『当前状态』不能为空」）。
        "required_source": "measured",
        "link_cols": (("所属执行", "execution", "执行"),),
    },
    "用户": {
        "resource": "users",
        "list_key": "users",
        "listable": True,
        "columns": (
            ("用户名", "account", "text", W),
            ("姓名", "realname", "text", W),
            ("密码", "password", "secret", W),
            ("部门", "dept", "number", W),
            ("邮箱", "email", "text", W),
            ("手机", "mobile", "text", W),
            ("微信", "weixin", "text", W),
            ("入职日期", "join", "date", W),
            ("界面类型", "visions", "select", W),
            ("职位", "role", "text", R),
            ("最后登录", "last", "date", R),
            ("已删除", "deleted", "text", R),
        ),
        "required": ("用户名", "姓名", "密码"),
        "required_source": "spec",      # 规范 required: account / realname / password
    },

    # ── 以下 6 个实体是第二轮探测补出来的（第一版只覆盖了 10 个 PM 实体）──────
    "产品计划": {
        "resource": "productplans",
        "list_key": "productplans",
        # ⚠️ 第 38 条：顶层 GET 回**整页 HTML**（回落到 web 路由），不是 0 字节。
        "listable": False,
        "not_listable_why": "实测 GET /productplans 返回 HTTP 200 + 整页 HTML（回落到 web 路由）",
        # 第 28 条：GET 需要 productID query —— 这就是读计划的正路（子列表实测有数据）。
        "parent_query": ("所属产品", "productID"),
        "children_of": ("产品", "productID", "productplans"),
        "columns": (
            ("名称", "title", "text", W),
            ("所属产品", "productID", "number", W),
            ("父计划", "parent", "number", W),
            ("计划开始", "begin", "date", W),
            ("计划完成", "end", "date", W),
            ("分支", "branchID", "number", W),
            ("描述", "desc", "longtext", W),
            ("状态", "status", "text", R),
            ("创建人", "createdBy", "text", R),
            ("创建日期", "createdDate", "date", R),
        ),
        "required": ("名称", "所属产品", "计划开始", "计划完成"),
        # 第 31 条：规范 required 只有 productID/title，实测不给 begin+end 会报
        # 「『结束日期』不能为空」。
        "required_source": "measured",
        "link_cols": (("所属产品", "productID", "产品"),),
    },
    "构建": {
        "resource": "builds",
        "list_key": "builds",
        # 顶层 GET 0 字节；POST **可用**（第一轮以为不行，是因为 executionID=1 不存在）。
        "listable": False,
        "not_listable_why": "实测 GET /builds 返回 HTTP 200 + 空响应体（顶层列表不可用）",
        # 经 /executions/:id/builds 或 /projects/:id/builds 读（第 39 条，实测可用）。
        # ⚠️ executionID 走 **body**（实测），没有 parent_query。
        "children_of": ("执行", "executionID", "builds"),
        "columns": (
            ("名称", "name", "text", W),
            ("所属执行", "executionID", "number", W),
            ("所属产品", "product", "number", W),
            ("所属应用", "system", "number", W),
            ("构建者", "builder", "text", W),
            ("打包日期", "date", "date", W),
            ("源代码地址", "scmPath", "text", W),
            ("下载地址", "filePath", "text", W),
            ("描述", "desc", "longtext", W),
        ),
        "required": ("名称", "所属执行", "所属产品", "所属应用", "构建者", "打包日期"),
        "required_source": "spec",
        "link_cols": (("所属执行", "executionID", "执行"), ("所属产品", "product", "产品")),
    },
    "发布": {
        "resource": "releases",
        "list_key": "releases",
        "listable": False,
        "not_listable_why": "实测 GET /releases 不带 productID 报 Missing required parameter（第 28 条）",
        "parent_query": ("所属产品", "productID"),
        "children_of": ("产品", "productID", "releases"),
        "columns": (
            ("名称", "name", "text", W),
            ("所属产品", "productID", "number", W),
            ("所属应用", "system", "number", W),
            ("包含构建", "build", "array", W),
            ("状态", "status", "select", W),
            ("计划发布日期", "date", "date", W),
            ("描述", "desc", "longtext", W),
        ),
        "required": ("名称", "所属产品", "所属应用", "包含构建", "计划发布日期"),
        # 字段名与规范一致，但「所属产品」必须走 query（第 28 条）→ 判据算实测。
        "required_source": "measured",
        "link_cols": (("所属产品", "productID", "产品"),),
    },
    "业务需求": {
        # 禅道的三层需求模型：业务需求(epic) → 用户需求(requirement) → 研发需求(story)。
        # 三者是**三个不同的实体**，不是同一张表的三种视图。
        "resource": "epics",
        "list_key": "epics",
        "listable": False,
        "not_listable_why": "实测 GET /epics 返回 HTTP 200 + 空响应体",
        "parent_query": ("所属产品", "productID"),
        "children_of": ("产品", "productID", "epics"),
        # 第 33 条：POST 成功但**不返回 id** → 只能创建后反查子列表拿 id。
        "create_id_via": ("所属产品", "epics"),
        # 第 29 条：DELETE 要 storyID query（规范写的是 :epicID —— 规范错）。
        "delete_query": ("storyID",),
        "columns": (
            ("标题", "title", "text", W),
            ("所属产品", "productID", "number", W),
            ("父需求", "parent", "number", W),
            ("优先级", "pri", "number", W),
            ("类别", "category", "select", W),
            ("来源", "source", "select", W),
            ("预计工时", "estimate", "number", W),
            ("指派给", "assignedTo", "text", W),
            ("评审人", "reviewer", "array", W),
            ("需求描述", "spec", "longtext", W),
            ("验收标准", "verify", "longtext", W),
        ),
        "required": ("标题", "所属产品", "评审人"),
        # 第 30 条：reviewer 必须数组（规范说是 string）。
        "required_source": "measured",
        "link_cols": (("所属产品", "productID", "产品"),),
        "actions": (
            ("激活", "activate", (), "重新激活业务需求"),
            ("关闭", "close", (), "关闭业务需求"),
            ("变更", "change", (), "走业务需求变更流程"),
        ),
    },
    "用户需求": {
        "resource": "requirements",
        "list_key": "requirements",
        "listable": False,
        "not_listable_why": "实测 GET /requirements 返回 HTTP 200 + 空响应体",
        "parent_query": ("所属产品", "productID"),
        "children_of": ("产品", "productID", "requirements"),
        "create_id_via": ("所属产品", "requirements"),      # 第 33 条
        "delete_query": ("storyID",),                        # 第 29 条
        "columns": (
            ("标题", "title", "text", W),
            ("所属产品", "productID", "number", W),
            ("父需求", "parent", "number", W),
            ("优先级", "pri", "number", W),
            ("类别", "category", "select", W),
            ("来源", "source", "select", W),
            ("预计工时", "estimate", "number", W),
            ("指派给", "assignedTo", "text", W),
            ("评审人", "reviewer", "array", W),
            ("需求描述", "spec", "longtext", W),
            ("验收标准", "verify", "longtext", W),
        ),
        "required": ("标题", "所属产品", "评审人"),
        "required_source": "measured",
        "link_cols": (("所属产品", "productID", "产品"),),
        "actions": (
            ("激活", "activate", (), "重新激活用户需求"),
            ("关闭", "close", (), "关闭用户需求"),
            ("变更", "change", (), "走用户需求变更流程"),
        ),
    },
    "应用": {
        "resource": "systems",
        "list_key": "systems",
        "listable": False,
        "not_listable_why": "实测 GET /systems 不带 productID 报 Missing required parameter（第 28 条）",
        "parent_query": ("所属产品", "productID"),
        "children_of": ("产品", "productID", "systems"),
        "create_id_via": ("所属产品", "systems"),           # 第 33 条
        # 第 36 条：**没有 DELETE 接口**（规范里也只有 POST/PUT）→ 建了就删不掉。
        "no_delete_why": "禅道没有 DELETE /systems 接口（规范里也只有 POST/PUT），建了就删不掉",
        "columns": (
            ("名称", "name", "text", W),
            ("所属产品", "productID", "number", W),
            ("是否集成应用", "integrated", "number", W),
            ("集成子应用", "children", "array", W),
            ("描述", "desc", "longtext", W),
        ),
        "required": ("名称", "所属产品", "是否集成应用", "集成子应用"),
        # 「所属产品」必须走 query（第 28 条）→ 判据算实测。
        "required_source": "measured",
        "link_cols": (("所属产品", "productID", "产品"),),
    },
}

#: 逻辑表名 → 禅道实体名。**这就是「实体映射层」的全部**：
#: 只在语义真的对得上时才建立别名，绝不为了「表看起来更全」去硬塞。
#: 对不上的（IC采购记录 / 外壳采购记录 / 发货清单 / 库存核对记录 …）不在这里 ——
#: 它们本来就不是 PM 概念，禅道承载不了，请继续放在 SeaTable/飞书/简道云上。
ALIASES: Dict[str, str] = {
    "生产计划": "执行",     # 一轮「执行」= 一版生产计划
    "生产工序": "任务",     # 一道工序 = 一个任务
    "迭代": "执行",
    "产品线": "项目集",
    "资源": "用户",         # 只对得上「人」那一部分（设备/外协无对应实体）
    "人员": "用户",
    "缺陷": "Bug",
    "用例": "测试用例",
    "测试任务": "测试单",
    "计划": "产品计划",     # 禅道的「计划」= 产品计划（排期），不是项目计划
    "版本": "发布",         # 「发布」= 对外版本；对内打包叫「构建」，别混
    "打包": "构建",
    "史诗": "业务需求",     # 敏捷术语 epic 的常见中文译法
    "RR": "用户需求",       # 禅道术语 requirement = 用户需求
    "US": "需求",           # user story
}


#: ⚠️ 实测到的一处服务端「答非所问」，值得在报错里直接点破 ——
#: 给「项目」起一个曾经用过（哪怕已删）的名字，服务端会说
#: 「『产品名称』已经有『X』这条记录了」：因为建项目会顺手建同名产品，
#: 而**项目创建路径的重名检查把已删除的产品也算在内**（直接 POST /products 却不算）。
#: 只看这句错误，人是猜不到「换个项目名就好了」的。
_QUIRK_MARKERS = ("产品名称", "已经有")
_QUIRK_HINT = (
    "　【提示·实测】创建「项目」时服务端的重名检查查的是**产品名**（它会随手建一个"
    "同名产品），而且**把已删除的产品也算进去** —— 所以一个用过又删掉的项目名就再也"
    "建不出来了（同一个名字直接 POST /products 却能成功，两条路径口径不一致）。"
    "换个名字即可。详见 adapters/zentao.py 模块 docstring 第 23 条。")


def _quirk_hint(text: str) -> str:
    return _QUIRK_HINT if all(m in text for m in _QUIRK_MARKERS) else ""


class ZentaoError(RuntimeError):
    """禅道调用失败。带 HTTP 状态、body 里的 ``status``、以及**真正的原因** ``message``。

    为什么三个都留：``message`` 才是人话（实测「Project does not exist.」），
    而 HTTP 状态码**可能**是 200（资源不存在就是），光看它判不出来。
    """

    def __init__(self, message: str, http_status=None, zentao_status=None, body=None):
        super().__init__(message)
        self.http_status = http_status
        self.zentao_status = zentao_status
        self.body = body or {}


# ══════════════════════════════════════════════════════════════════
# 传输层
# ══════════════════════════════════════════════════════════════════

class _HttpTransport:
    """直连禅道 REST API v2。只用标准库。

    ``proxy`` / ``no_proxy``：WorkBuddy 沙箱会给子进程注入 ``HTTPS_PROXY``
    （随机端口的本地代理），本机 127.0.0.1 的请求会被它拦成 502。
    本机现成的 ``setup-token.py`` 也是显式 ``ProxyHandler({})`` 才通的。
    默认 ``no_proxy=True``：禅道几乎总是部署在内网/本机，走系统代理没有意义，
    而踩上沙箱代理的概率很高。
    """

    def __init__(self, base: str, token: str = "", timeout: int = 30,
                 proxy: str = "", no_proxy: bool = True):
        self.base = (base or _DEFAULT_BASE).rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.proxy = proxy or ""
        self.no_proxy = bool(no_proxy)
        if self.proxy:
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        elif self.no_proxy:
            self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self._opener = urllib.request.build_opener()

    def request(self, method: str, path: str, query: dict = None,
                body: dict = None) -> Tuple[int, str]:
        """发一个请求，返回 ``(http_status, 响应文本)``。**不做任何判断**。

        判断一律留给 :meth:`ZentaoAdapter._call` —— 这样「拿凭据探活」那种
        不该因业务错误而中止的场景也能复用同一个传输层。
        """
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None})
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        if self.token:
            # 实测可用的头名是 token（HTTP 头不区分大小写；
            # setup-token.py 用 "token"，OpenAPI 描述里写的是 "Token"）。
            req.add_header("token", self.token)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            raise ZentaoError("无法连接禅道（%s）：%s。若本机有代理，试 zentao.no_proxy: true"
                              % (url, getattr(e, "reason", e)))

    def close(self) -> None:
        return None


# ══════════════════════════════════════════════════════════════════
# 适配器
# ══════════════════════════════════════════════════════════════════

class ZentaoAdapter(BaseAdapter):
    backend = "zentao"

    #: 能力声明（声明式：不联网、不认证、无副作用）：
    #:   · read / write / update / delete —— 8 个必修方法都真实现
    #:   · server_row_id —— ``id`` 由服务端分配，客户端不可指定
    #:
    #: **刻意不声明**：
    #:   · batch_write  —— 禅道没有批量创建接口。``append_rows`` 走基类的逐条循环，
    #:     **不是原子的**（中途失败前面的已经落库），声明 batch_write 会让人误以为能当批处理。
    #:   · schema_manage —— 实体与字段都是固定的，v2 没有建表/加字段接口。
    #:   · link / link_read —— 禅道的关联是**实体固定外键字段**（``task.executionID``、
    #:     ``story.productID``），不是可读写的通用关联列，也没有 link_id 概念。
    #:     建立关联请直接写那个普通列（见 ``link_columns()`` 的说明）。
    #:   · query_pushdown —— 实测 ``/executions?project=1`` 确实能在服务端过滤，
    #:     但参名是**实体特定的**（project / projectID / productID / executionID…），
    #:     无法对任意 ``filters`` 无条件成立。宁可走基类内存过滤。
    #:   · idempotent / optimistic_lock —— 都没有。
    CAPS = frozenset({CAP_READ, CAP_WRITE, CAP_UPDATE, CAP_DELETE, CAP_SERVER_ROW_ID})

    def __init__(self, base: str = "", token: str = "", account: str = "",
                 password: str = "", site: str = None, timeout: int = 30,
                 proxy: str = "", no_proxy: bool = True, transport=None):
        self.base = base or ""
        self.token = token or ""
        self.account = account or ""
        self.password = password or ""
        self.site = site or "default"
        self.timeout = timeout
        self.proxy = proxy or ""
        self.no_proxy = bool(no_proxy)
        self._t = transport
        self._probed = False
        self._probe_note = ""

    # ── 生命周期 ────────────────────────────────────────
    def auth(self) -> None:
        """建连 + 探活。有 token 就用 token，否则用 账号+密码 换 token。

        ⚠️ 登录路径是 **``/user/login``（单数）**。OpenAPI 规范里写的
        ``/users/login``（复数）实测**不存在**（回 ``User does not exist.``）——
        这是本适配器最反直觉的一处，别照着规范改回去。

        探活的判据刻意宽松：**只有认证类失败才算失败**。
        实测「不带/错令牌」是 HTTP 401 + ``{"status":"fail","message":"Not allowed"}``；
        而业务类失败（如某资源没权限）也是 ``status: fail``，那说明**凭据是好的**，
        不该在建连阶段就报错。业务失败的原因记在 ``probe_note`` 里供诊断。
        """
        if self._t is None:
            self._t = _HttpTransport(self.base or _DEFAULT_BASE, self.token,
                                     self.timeout, proxy=self.proxy,
                                     no_proxy=self.no_proxy)
        if not self.token:
            if not (self.account and self.password):
                raise ZentaoError(
                    "zentao 后端需要 token，或 account + password 用来换 token。"
                    "（取 token：POST %s/user/login，body {account, password}）" % self.base)
            self._login()
        status, text = self._t.request("GET", "/projects", {"recPerPage": 1, "pageID": 1})
        if status == 401 or self._looks_like_auth_failure(text):
            raise ZentaoError(
                "禅道认证失败（HTTP %s）：%s。请检查 token 是否过期 —— "
                "禅道令牌会失效，重新登录换一个即可。" % (status, self._message_of(text)))
        self._probed = True
        self._probe_note = "" if self._is_success(text) else self._message_of(text)

    def _login(self) -> None:
        status, text = self._t.request(
            "POST", "/user/login",
            body={"account": self.account, "password": self.password})
        data = self._parse_or_raise(text, status, "/user/login")
        if data.get("status") != "success":
            raise ZentaoError("禅道登录失败（HTTP %s）：%s"
                              % (status, self._format_failure(data)))
        token = data.get("token")
        if not token:
            raise ZentaoError("禅道登录成功但响应里没有 token（拿到键：%s）"
                              % "/".join(sorted(data)))
        self.token = token
        self._t.token = token

    def close(self) -> None:
        if self._t is not None and hasattr(self._t, "close"):
            try:
                self._t.close()
            except Exception:
                pass

    def describe(self) -> str:
        """一行诊断串。**不含令牌**（凭据永远不进日志）。"""
        return "%s(%s, site=%s, auth=%s) caps=%s" % (
            type(self).__name__, self.backend, self.site,
            "token" if self.token else "account", ",".join(sorted(self.capabilities())) or "-")

    # ── 底层调用与四道守卫 ───────────────────────────────
    @staticmethod
    def _parse_or_raise(text: str, http_status: int, path: str) -> dict:
        """把响应文本解析成 dict；空响应体 / HTML / 非 JSON 一律报错。"""
        raw = (text or "").strip()
        if not raw:
            # ⚠️ 实测：/tasks /stories /requirements /epics /feedbacks /tickets /builds
            #    的列表 GET 就是 HTTP 200 + 0 字节。返回 {} 会让上层以为「没数据」。
            raise ZentaoError(
                "禅道 %s 返回了空响应体（HTTP %s）。实测这是某些端点在本版禅道上"
                "**不可用**的表现，而不是「没有数据」—— 所以适配器报错，不返回空列表。"
                % (path, http_status), http_status=http_status)
        if raw[:1] == "<":
            # ⚠️ 实测：/productplans 的 GET、/executions/1/tasks 会回落 web 路由返回 HTML。
            raise ZentaoError(
                "禅道 %s 返回了 HTML 而不是 JSON（HTTP %s，%d 字节）。"
                "说明这条路由在当前版本上不存在，请求回落到了 web 页面。"
                % (path, http_status, len(raw)), http_status=http_status)
        try:
            data = json.loads(raw)
        except ValueError:
            raise ZentaoError("禅道 %s 返回的不是 JSON（前 200 字符）：%s"
                              % (path, raw[:200]), http_status=http_status)
        if not isinstance(data, dict):
            raise ZentaoError("禅道 %s 返回的不是对象：%r" % (path, data),
                              http_status=http_status)
        return data

    def _call(self, method: str, path: str, query: dict = None,
              body: dict = None) -> dict:
        """发请求并把「HTTP 200 但其实是失败」也判成失败。

        ⚠️ 这一条是本适配器**最重要的守卫**：禅道资源不存在时返回的是
        **HTTP 200** + ``{"status":"fail","message":"… does not exist."}``。
        只看 HTTP 状态码的实现会把「项目不存在」当成「项目读到了」，
        然后拿到一个没有业务数组的 dict 继续往下跑 —— 典型的静默错。

        判据只有一条「``status`` 不是 ``success`` 就报错」，这一条同时覆盖了
        **第三种信封**（字段级校验错误，``{"result":"fail","message":{…}}``，
        **连 ``status`` 键都没有**）。第三种信封的文案由
        :meth:`_format_failure` 负责，否则只能打出一句 ``status=None``。
        """
        if self._t is None:
            self.auth()
        status, text = self._t.request(method, path, query, body)
        data = self._parse_or_raise(text, status, path)
        if status == 401:
            raise ZentaoError("禅道认证失败（HTTP 401）：%s"
                              % (data.get("message") if isinstance(data.get("message"), str)
                                 else "Not allowed"),
                              http_status=status, zentao_status=data.get("status"), body=data)
        if data.get("status") != "success":
            raise ZentaoError(
                "禅道 %s 调用失败（HTTP %s，%s）：%s"
                % (path, status, self._envelope_of(data), self._format_failure(data)),
                http_status=status,
                zentao_status=data.get("status") or data.get("result"),
                body=data)
        return data

    @staticmethod
    def _envelope_of(data: dict) -> str:
        """一句话说清「这是**哪一种**信封」。禅道有三种，混着看会很费解：

          · ``{"status":"success",…}``           正常
          · ``{"status":"fail","message":"…"}``  业务失败（HTTP 可能是 200）
          · ``{"result":"fail","message":{…}}``  字段级校验失败（**连 status 键都没有**）

        不区分的话只能打出一句 ``status=None``，读的人会以为是适配器自己出的错。
        """
        if "status" in data:
            return "status=%r" % (data.get("status"),)
        if "result" in data:
            return "result=%r（无 status 键：字段级校验失败）" % (data.get("result"),)
        return "既无 status 也无 result 键"

    @staticmethod
    def _format_failure(data: dict) -> str:
        """把失败响应整理成人话。

        ⚠️ ``message`` 有**两种形态**，都是实测：
          · 字符串 —— 如 ``"Project does not exist."``；
          · **字典** —— 字段级校验错误，形如
            ``{"end": ["『计划完成』不能为空。"]}``。此时整个响应里**连 ``status``
            键都没有**，只有 ``result: "fail"``。

        不处理第二种的话，错误文案只能打出一句 ``status=None`` 加一个裸 dict ——
        而那个 dict 里其实写着「**哪个**字段错了、错在哪」，正是最该让人看见的东西。
        """
        msg = data.get("message")
        if isinstance(msg, dict):
            parts = []
            for field, errs in msg.items():
                if isinstance(errs, (list, tuple)):
                    parts.append("%s：%s" % (field, "；".join(str(e) for e in errs)))
                else:
                    parts.append("%s：%s" % (field, errs))
            text = "字段校验失败 —— " + " / ".join(parts)
        elif isinstance(msg, str) and msg.strip():
            text = msg
        elif msg:
            text = str(msg)
        else:
            text = "服务端未给 message，原始键：" + "/".join(sorted(data))
        return text + _quirk_hint(text)

    @staticmethod
    def _is_success(text: str) -> bool:
        try:
            return json.loads(text or "{}").get("status") == "success"
        except Exception:
            return False

    @classmethod
    def _message_of(cls, text: str) -> str:
        try:
            d = json.loads(text or "{}")
        except Exception:
            return (text or "")[:120]
        if not isinstance(d, dict):
            return (text or "")[:120]
        return cls._format_failure(d)

    @classmethod
    def _looks_like_auth_failure(cls, text: str) -> bool:
        return "not allowed" in cls._message_of(text).lower()

    # ── 实体解析（映射层入口）────────────────────────────
    def _resolve(self, table: str) -> Tuple[str, dict]:
        """把「逻辑表名或实体名（或它的别名）」解析成 (实体名, 规格)。

        解析不到就报错并**列出全部可用项** —— 尤其是要把「禅道承载不了这张表」
        这件事说清楚，而不是含糊地说一句「表不存在」。
        """
        name = str(table or "").strip()
        name = ALIASES.get(name, name)
        spec = ENTITY_SPECS.get(name)
        if spec is None:
            raise KeyError(
                "禅道后端没有「%s」对应的实体。禅道的实体是固定的，"
                "能用的只有：%s；别名：%s。"
                "采购/发货/库存这类表不是 PM 概念，禅道承载不了，请继续用 "
                "seatable / feishu / jiandaoyun。"
                % (table, " / ".join(sorted(ENTITY_SPECS)),
                   " / ".join("%s→%s" % (k, v) for k, v in sorted(ALIASES.items()))))
        return name, spec

    def _by_label(self, spec: dict) -> Dict[str, tuple]:
        return {c[0]: c for c in spec["columns"]}

    def _by_field(self, spec: dict) -> Dict[str, tuple]:
        return {c[1]: c for c in spec["columns"]}

    def list_tables(self) -> List[str]:
        """可用表名 = 禅道实体名 + 逻辑别名（排序后）。

        为什么把别名也列出来：上层（驾驶舱/工作流）用的是**逻辑表名**
        （如「生产计划」），而禅道那边叫「执行」。两个名字都能用，
        但只有都列出来，调用方才能一眼看出「哪张逻辑表在禅道上是有的」。
        """
        return sorted(list(ENTITY_SPECS) + list(ALIASES))

    # ── 读 ─────────────────────────────────────────────
    def get_metadata(self, table: str) -> Dict[str, Any]:
        """返回**适配器声明的**表结构，而不是从服务端读来的。

        为什么：禅道没有「读某实体全部字段定义」的接口（OpenAPI 里也没有），
        能读到的只是若干实体各自的响应体。所以列定义就是 ``ENTITY_SPECS``
        这份**成文的映射契约** —— 它是适配器对外承诺的列集合，
        写不进去的字段会被明确拒绝（见 ``_to_payload``），而不是被服务端静默忽略。

        ``type`` = number/date/select/array/longtext/text/secret；
        ``readonly`` = 该字段没出现在规范的 POST/PUT 请求体里（只在列表响应里见过），
        因此适配器**只读不写**。
        """
        ename, spec = self._resolve(table)
        cols = []
        for label, field, ctype, rw in spec["columns"]:
            c = {"name": label, "key": field, "type": ctype}
            if rw == R:
                c["readonly"] = True
            em = _enum_map(ename, field)
            if em:
                c["options"] = em
            cols.append(c)
        return {"table_name": ename, "columns": cols,
                "requested_name": table, "listable": bool(spec.get("listable"))}

    def table_exists(self, table: str) -> bool:
        """「这张逻辑表在禅道上有映射吗」。**只读本地映射表，不发网络请求。**"""
        try:
            self._resolve(table)
            return True
        except KeyError:
            return False

    @staticmethod
    def _flat_value(table: str, field: str, value):
        """服务端值 → 下游习惯的扁平值（枚举代码翻成中文，None 归一成空串）。"""
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return [_enum_label(table, field, v) for v in value]
        return _enum_label(table, field, value)

    def _flat_row(self, ename: str, raw: dict, spec: dict) -> Dict[str, Any]:
        row = {}
        for label, field, _ctype, _rw in spec["columns"]:
            row[label] = self._flat_value(ename, field, raw.get(field))
        row["__row_id__"] = raw.get("id")
        return row

    def _iter_rows(self, ename: str, spec: dict, page_size: int = None):
        """按 ``pager`` 翻页产出行。**三重保护，绝不返回部分数据冒充全量。**"""
        if not spec.get("listable"):
            co = self._children_of(spec)
            hint = ("　→ 这个实体**可以经父资源读**：用 "
                    "list_children('%s', <父 id>)，父实体是「%s」。"
                    % (ename, co[0])) if co else ""
            raise Unsupported(
                "禅道实体「%s」的**顶层列表**接口在本版不可用（%s）。"
                "读不了就是读不了 —— 适配器不返回空列表冒充「没有数据」。%s"
                % (ename, spec.get("not_listable_why") or "原因未知", hint))
        size = page_size or _PAGE
        page = 1
        prev_sig = None
        total = None
        while True:
            d = self._call("GET", "/" + spec["resource"],
                           {"recPerPage": size, "pageID": page})
            arr = d.get(spec["list_key"])
            if not isinstance(arr, list):
                # 实测 /bugs /testcases /testtasks 的响应里混着一堆表单初始化数组，
                # 键名还各不相同；这里要求**精确键名**，拿不准就报错。
                raise ZentaoError(
                    "禅道 %s 的列表响应里没有 %r 数组（实际键：%s）。"
                    "该端点的响应结构与适配器约定不符，请核对禅道版本。"
                    % (spec["resource"], spec["list_key"], "/".join(sorted(d))),
                    body=d)
            if not arr:
                return
            sig = tuple(str(x.get("id")) for x in arr)
            if sig == prev_sig:
                # ⚠️ 实测本实例上 recPerPage/pageID 疑似不生效（pager.pageID 恒为 1）。
                #    如果不在这里拦住，就会把第一页反复读，然后「成功」返回一份
                #    只有第一页的数据 —— 上层完全看不出来少了东西。
                #
                # 判重刻意放在 **yield 之前**：放在后面的话，同一页会被 yield 两次，
                # 而调用方若是「捕获异常后继续用已拿到的行」，拿到的就是**重复**数据 ——
                # 那比少数据更难发现（条数看着对，内容却是重播的第一页）。
                raise ZentaoError(
                    "禅道 %s 的翻页参数没有生效：第 %d 页与上一页返回完全相同的 %d 条数据，"
                    "但服务端报告的 recTotal=%s，说明还有数据没读到。"
                    "为避免静默返回不完整数据，此处直接报错（这是本适配器实测到的"
                    "禅道行为差异，需要针对你的禅道版本确认分页参数名）。"
                    % (spec["resource"], page, len(arr), total), body=d)
            for raw in arr:
                if isinstance(raw, dict):
                    yield raw
            pager = d.get("pager") if isinstance(d.get("pager"), dict) else {}
            try:
                total = int(pager.get("recTotal")) if pager.get("recTotal") is not None else None
                page_total = int(pager.get("pageTotal")) if pager.get("pageTotal") is not None else None
            except (TypeError, ValueError):
                total = page_total = None
            if total is not None and total <= page * size:
                return
            if page_total is not None and page >= page_total:
                return
            if total is None and page_total is None and len(arr) < size:
                return
            prev_sig = sig
            page += 1
            if page > _MAX_PAGES:
                raise ZentaoError("禅道 %s 翻页超过 %d 页仍未读完（recTotal=%s），已中止。"
                                  % (spec["resource"], _MAX_PAGES, total))

    def list_rows(self, table: str) -> List[Dict[str, Any]]:
        ename, spec = self._resolve(table)
        return [self._flat_row(ename, raw, spec) for raw in self._iter_rows(ename, spec)]

    def raw_rows(self, table: str) -> List[Dict[str, Any]]:
        """**扩展方法**（不在契约里）：返回服务端原样的行，保留全部字段。

        为什么需要：禅道实体有 80~90 个字段（``progress``/``left``/``consumed``/
        ``teamCount``/``totalEstimate``…），适配器只声明了其中一小撮。
        要一两个未声明的字段时，不该逼调用方去改 ``ENTITY_SPECS`` 再发版 ——
        用这个方法拿原样数据。写入仍然只认声明的列（``_to_payload`` 会拒绝未知列）。
        """
        ename, spec = self._resolve(table)
        return [dict(r) for r in self._iter_rows(ename, spec)]

    # ── 父资源子列表（第 39 条：读「顶层列表不可用」那些实体的**唯一正路**）──
    def _children_of(self, spec: dict) -> Optional[tuple]:
        co = spec.get("children_of")
        return tuple(co) if co else None

    def _iter_children(self, ename: str, spec: dict, parent_id, page_size: int = None):
        """按 ``/<父资源>/:id/<子路径>`` 翻页产出行。

        实测这些子列表**都是可用的**（第 39 条），哪怕该实体自己的顶层列表
        回的是 0 字节。所以「读不了」的实体里，只有真正没有父路径的那些
        （如**构建**只能经执行/项目读）才需要如实拒绝。
        """
        co = self._children_of(spec)
        if not co:
            raise Unsupported(
                "禅道实体「%s」既没有可用顶层列表，也没有声明的父资源路径，"
                "读不了就是读不了（不返回 [] 冒充「没有数据」）。" % ename)
        parent_ename, _field, arr_key = co
        p_ename, p_spec = self._resolve(parent_ename)
        base = "/%s/%s/%s" % (p_spec["resource"], parent_id, spec["resource"])
        size = page_size or _PAGE
        page = 1
        while True:
            d = self._call("GET", base, {"recPerPage": size, "pageID": page})
            arr = d.get(arr_key)
            if not isinstance(arr, list):
                raise ZentaoError(
                    "禅道 %s 的子列表响应里没有 %r 数组（实际键：%s）。"
                    % (base, arr_key, "/".join(sorted(d))), body=d)
            if not arr:
                return
            for raw in arr:
                if isinstance(raw, dict):
                    yield raw
            pager = d.get("pager") if isinstance(d.get("pager"), dict) else {}
            try:
                total = int(pager["recTotal"]) if pager.get("recTotal") is not None else None
                page_total = int(pager["pageTotal"]) if pager.get("pageTotal") is not None else None
            except (TypeError, ValueError, KeyError):
                total = page_total = None
            # 第 26 条：终止**只能**靠 pageTotal/recTotal ——
            # 超范围页码会被钳制到最后一页，本页永远不为空。
            if page_total is not None and page >= page_total:
                return
            if total is not None and total <= page * size:
                return
            if total is None and page_total is None and len(arr) < size:
                return
            page += 1
            if page > _MAX_PAGES:
                raise ZentaoError("禅道 %s 子列表翻页超过 %d 页仍未读完，已中止。"
                                  % (base, _MAX_PAGES))

    def list_children(self, table: str, parent_id) -> List[Dict[str, Any]]:
        """**扩展方法**（不在契约里）：按父 id 读子列表。

        用途：``任务``/``需求``/``业务需求``/``用户需求``/``产品计划``/``发布``/``应用``
        的顶层列表在禅道本版回 0 字节或 HTML，**只有经父资源才读得到**。
        另一个用途是性能：读「某个执行下的任务」比全量扫再过滤快得多，
        而且**结果就是全体**（不需要再筛）。

        ``parent_id`` 传的是**父实体在禅道里的 id**（如 productID / executionID）。
        父 id 对不上时服务端报 ``... does not exist``，这里如实抛出。
        """
        ename, spec = self._resolve(table)
        pid = str(parent_id or "").strip()
        if not pid:
            raise ZentaoError("读禅道实体「%s」的子列表必须给 parent_id。" % ename)
        return [self._flat_row(ename, raw, spec)
                for raw in self._iter_children(ename, spec, pid)]

    def get_row(self, table: str, row_id: str) -> Optional[Dict[str, Any]]:
        """按 id 取单行；不存在返回 ``None``。

        禅道有按 id 取详情的接口（``GET /<资源>/:id``，实测不存在的 id 会回
        ``status: fail`` + "… does not exist."）——但**规范里它只覆盖一部分实体**
        （``/projects/:projectID`` 只有 PUT/DELETE，没有 GET）。所以这里：
        列表可用的实体走列表扫描（一定对），列表不可用的才去试详情接口，
        并把「实体不存在」与「接口不存在」分开报。

        注意本方法**要对得起「找不到返回 None」的契约**：因此它**不能**复用
        ``_call``（``_call`` 会把 ``status: fail`` 抛成异常）。它用
        ``_parse_or_raise`` 自己判。
        """
        ename, spec = self._resolve(table)
        target = str(row_id)
        if spec.get("listable"):
            for raw in self._iter_rows(ename, spec):
                if str(raw.get("id")) == target:
                    return self._flat_row(ename, raw, spec)
            return None
        if self._t is None:
            self.auth()
        status, text = self._t.request("GET", "/%s/%s" % (spec["resource"], target))
        data = self._parse_or_raise(text, status, "/%s/%s" % (spec["resource"], target))
        if data.get("status") != "success":
            msg = str(data.get("message") or "")
            if "does not exist" in msg.lower():
                return None                    # 行不存在 —— 正是契约要的 None
            raise ZentaoError("禅道取 %s#%s 详情失败（HTTP %s，status=%r）：%s"
                              % (ename, target, status, data.get("status"),
                                 self._format_failure(data)),
                              http_status=status, zentao_status=data.get("status"),
                              body=data)
        detail = data.get(spec.get("detail_key") or spec["resource"])
        if not isinstance(detail, dict):
            # ⚠️ 第 34 条（本文件里最阴的一条）：不存在的 id 回的是
            #    ``{"status":"success","load":{"alert":"抱歉，您访问的对象不存在！"}}``
            #    —— **status 是 success**。所以不能靠 status 判存在性；
            #    判据是「除了 status/load/message 之外，还有没有业务数据对象」。
            #    ``load`` 是禅道的「跳转指令」字段，不是数据。
            keys = [k for k in data
                    if k not in ("status", "load", "message") and isinstance(data[k], dict)]
            if len(keys) == 1:
                detail = data[keys[0]]
            elif not keys and "load" in data:
                return None                # 「success 但什么都没有」= 不存在
            else:
                raise ZentaoError("禅道 %s#%s 详情响应里找不到实体对象（键：%s）"
                                  % (ename, target, "/".join(sorted(data))), body=data)
        return self._flat_row(ename, detail, spec)

    # ── 写 ─────────────────────────────────────────────
    def _to_payload(self, ename: str, spec: dict, data: dict,
                    for_create: bool) -> dict:
        """业务 dict → 禅道请求体：``{禅道字段: 值}``。

        三类处理都是**成文转换**（与 ``adapters/feishu.py`` 的 ``_to_record`` 同一套规矩）：

          · **只读列**（规范里没进 POST/PUT 请求体的字段，如 ``状态``/``进度``）→ 剔除。
            剔除而不是报错，是因为最常见的使用方式就是「读回来的整行改一个字段再写回去」，
            整行里天然带着这些列；为它们报错会让这条路彻底走不通。
            它们是**枚举出来的**（``ENTITY_SPECS`` 里的 R 标记），不是「拿不准就丢」——
            ``get_metadata`` 也会把 ``readonly: True`` 标出来，调用方可以先看一眼。
          · **未知列** → 抛错并列出现有列名（列名写错是最常见的错，不丢给服务端）。
          · **空值** → 不发送（不覆盖服务端既有值）。这**不是**保守猜测：
            实测 ``PUT /projects/1 {"begin": ""}`` 会整条被拒
            （``result: fail`` + 「『计划开始』不能为空」）且原值不变 ——
            所以「发送空值来清空」在禅道这里根本不成立。
            代价是适配器**无法清空字段**，要清空请到禅道界面做。

        ``for_create`` 时校验必填列：按**实际会发出去的字段**判，而不是按调用方
        传进来的键 —— 传了 ``{"名称": ""}`` 等于没传。

        ⚠️ 这个必填校验是**客户端的礼貌性检查**，不是接口契约 ——
        ``required`` 的来源见 ``ENTITY_SPECS`` 的 ``required_source``
        （「项目」用实测最小集，其余用规范的 ``required``，规范可能比服务端更严）。
        它存在的意义是**早一点、用中文说清楚**；真正的兜底是服务端的字段级校验错误
        （实测会明确说「『计划完成』不能为空」），由 :meth:`_format_failure` 呈现。
        """
        by_label = self._by_label(spec)
        by_field = self._by_field(spec)
        out: Dict[str, Any] = {}
        unknown: List[str] = []
        for k, v in (data or {}).items():
            if k == "__row_id__":
                continue
            col = by_label.get(k) or by_field.get(k)
            if col is None:
                unknown.append(str(k))
                continue
            label, field, ctype, rw = col
            if rw == R:
                continue                        # 成文剔除，见 docstring
            val = self._to_value(ename, label, field, ctype, v)
            if val is None:
                continue                        # 空值不发送
            out[field] = val
        if unknown:
            raise ZentaoError(
                "以下列在禅道实体「%s」的映射里不存在：%s；可用列：%s"
                % (ename, " / ".join(unknown[:8]),
                   " / ".join(c[0] for c in spec["columns"])))
        if for_create:
            missing = [lab for lab in spec["required"]
                       if by_label[lab][1] not in out]
            if missing:
                raise ZentaoError(
                    "创建禅道实体「%s」缺必填列：%s（必填来自 %s：%s）。"
                    "在客户端就拦下来是为了早一点、用中文说清楚；"
                    "服务端也会报字段级错误，但那是『发出去被拒』，"
                    "错误里不会告诉你「适配器的列名该怎么写」。"
                    % (ename, " / ".join(missing),
                       spec.get("required_source") or "spec",
                       " / ".join(spec["required"])))
        return out

    def _to_value(self, ename: str, label: str, field: str, ctype: str, value):
        """单值翻译；返回 ``None`` 表示「不发这个字段」。"""
        if value is None or value == "" or value == [] or value == ():
            return None
        if ctype == "array":
            vals = value if isinstance(value, (list, tuple, set)) else [value]
            return [_enum_code(ename, field, v) for v in vals if v not in (None, "")]
        if ctype == "number":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float)):
                return value
            try:
                return float(str(value).replace(",", "").strip())
            except ValueError:
                raise ZentaoError(
                    "「%s」是数值列（禅道字段 %s），收到的值转不成数字：%r"
                    "（不原样放行 —— 禅道把一切都当字符串收下，会静默存一个怪值）"
                    % (label, field, value))
        if ctype == "select":
            return _enum_code(ename, field, value)
        if ctype == "date":
            # 禅道收 'YYYY-MM-DD' 这种字符串（规范里 begin/end/join 都是 string）。
            # 不在这里做日期格式转换或校验：禅道对非法日期的行为未验证，
            # 而下游本来就用 YYYY-MM-DD。
            return value if isinstance(value, str) else str(value)
        if ctype == "secret":
            return str(value)
        return value if isinstance(value, str) else str(value)

    # ── 写 ─────────────────────────────────────────────
    def _parent_param(self, ename: str, spec: dict, data: dict) -> dict:
        """挑出**必须走 query string** 的父参数（第 27 条）。

        为什么不能像别的字段一样待在 body 里：实测 ``productID`` 放进 body 会被
        服务端当成「没传」（报 ``Missing required parameter: productID.``），
        而**官方规范恰恰把它声明为 requestBody 里的 integer 属性** —— 规范错了。
        调用方只管在 ``data`` 里照列名给值，放 query 还是放 body 由映射层决定。
        """
        pq = spec.get("parent_query")
        if not pq:
            return {}
        _label, field = pq
        by_label = self._by_label(spec)
        by_field = self._by_field(spec)
        for k, v in (data or {}).items():
            col = by_label.get(k) or by_field.get(k)
            if col and col[1] == field and v is not None and str(v).strip() != "":
                return {field: v}
        return {}

    def _child_id_set(self, ename: str, spec: dict, parent_id) -> set:
        return {str(r.get("id")) for r in self._iter_children(ename, spec, parent_id)}

    def _recover_created_id(self, ename: str, spec: dict, query: dict, data: dict,
                            pre_ids: set) -> Any:
        """第 33 条：``POST /epics`` ``/requirements`` ``/systems`` **成功但不回 id**。

        只能反查父资源子列表、取「新增出来的那一条」。按 id 差集找（不是按标题匹配）——
        标题可能重复，id 不会。
        """
        co = self._children_of(spec)
        parent_id = next(iter(query.values()), None)
        if not co or parent_id is None:
            raise ZentaoError(
                "禅道创建 %s 成功但响应里没有 id，而该实体**没有声明 create_id_via**、"
                "也没有可用的父资源路径，无法反查新建行的 id。"
                "（上层要靠 id 才能更新/删除，静默返回 None 会让记录「建好了但找不回来」。）"
                % ename)
        after = list(self._iter_children(ename, spec, parent_id))
        fresh = [r for r in after if str(r.get("id")) not in pre_ids]
        if len(fresh) == 1:
            return fresh[0].get("id")
        if len(fresh) > 1:
            # 并发或残留导致一次多出几条 —— 用标题再筛一次（仍拿不准就报错，不猜）
            want = None
            for k, v in (data or {}).items():
                col = self._by_label(spec).get(k) or self._by_field(spec).get(k)
                if col and col[1] in ("title", "name"):
                    want = str(v)
                    break
            hit = [r for r in fresh
                   if want is not None
                   and str(r.get("title") or r.get("name") or "") == want]
            if len(hit) == 1:
                return hit[0].get("id")
        raise ZentaoError(
            "禅道创建 %s 成功但响应里没有 id，回查父资源子列表时找到 %d 条新增记录"
            "（无法确定是哪一条，不猜）。请到禅道界面确认后按 id 手工处理。"
            % (ename, len(fresh)))

    def append_row(self, table: str, data: Dict[str, Any]) -> str:
        """新增一行，返回**字符串**形式的 id。

        ⚠️ 实测 ``POST`` 的成功信封是 ``{"message":"保存成功","id":1,"status":"success"}``
        —— ``id`` 是**数字**；而列表 GET 回来的 ``id`` 是**字符串** ``"1"``。
        这里统一 ``str()`` 化，两边的 id 才能互相对得上（``get_row``/``update_row``
        都按字符串比）。

        ⚠️ **第 33 条**：``业务需求`` / ``用户需求`` / ``应用`` 三个实体**成功也不回 id**，
        适配器会**反查父资源子列表**把 id 找回来（见 ``create_id_via``）。
        这条不做的话，调用方拿到的是「建好了但不知道是哪一条」。

        ⚠️ **写「项目」有副作用**：实测创建 scrum 项目会顺手建一个**同名产品**
        （列表里 ``hasProduct: "1"``）。删掉项目**不会**删掉那个产品。
        批量建项目前请先想清楚产品线要怎么收。
        """
        ename, spec = self._resolve(table)
        query = self._parent_param(ename, spec, data)
        if spec.get("parent_query") and not query:
            raise ZentaoError(
                "创建禅道实体「%s」必须给「%s」（它是必填的父资源，"
                "且在禅道里必须作为 query 参数发送，见模块 docstring 第 27 条）。"
                % (ename, spec["parent_query"][0]))
        payload = self._to_payload(ename, spec, data, for_create=True)
        for f in query:
            payload.pop(f, None)        # 已经在 query 里了，别再往 body 发一份
        pre_ids = (self._child_id_set(ename, spec, next(iter(query.values())))
                   if spec.get("create_id_via") else None)
        d = self._call("POST", "/" + spec["resource"], query, body=payload)
        rid = d.get("id")
        if rid is None and spec.get("create_id_via"):
            rid = self._recover_created_id(ename, spec, query, data, pre_ids or set())
        if rid is None:
            raise ZentaoError(
                "禅道创建 %s 返回成功但没有 id（键：%s）。上层要靠 id 才能后续更新/删除，"
                "返回 None 会让「建好了但找不回来」。"
                % (ename, "/".join(sorted(d))), body=d)
        return str(rid)

    def append_rows(self, table: str, rows: List[Dict[str, Any]]) -> List[str]:
        """批量新增。**禅道没有批量接口** → 逐条 POST，**不是原子的**。

        这里不覆盖基类行为，只把「非原子」这件事说清楚：中途失败时，
        前面几条**已经落库了**，而异常会把 id 列表丢掉。调用方若需要
        「全成功或全失败」，请自己记录已成功的 id 再决定是否回滚。
        """
        return [self.append_row(table, r) for r in (rows or [])]

    def update_row(self, table: str, row_id: str, data: Dict[str, Any]) -> None:
        """更新单行（``PUT /<资源>/:id``）。

        ``row_id`` 必须是禅道自己的 id。**不允许凭业务键猜行号** ——
        禅道没有「按名称找 id」的接口，猜一个数字去 PUT 会改到别人身上。
        """
        ename, spec = self._resolve(table)
        rid = str(row_id or "").strip()
        if not rid:
            raise ZentaoError("更新禅道实体「%s」必须给 row_id（禅道 id），"
                              "不允许凭业务键猜行号。" % ename)
        payload = self._to_payload(ename, spec, data, for_create=False)
        if not payload:
            return                     # 全是只读/空值 → 没有要写的东西，不是错误
        self._call("PUT", "/%s/%s" % (spec["resource"], rid), body=payload)

    def delete_rows(self, table: str, row_ids: List[str]) -> None:
        """按 id 删除（``DELETE /<资源>/:id``，实测规范里不带请求体）。

        实测：删**不存在的 id** 会回 HTTP 200 + ``status: fail`` +
        ``"Project does not exist."``（不是静默成功）。所以逐条删、
        把任何非 success 都当失败抛出是**被实测支持**的 ——
        当成成功会产生「上层以为清了、实际还在」的幽灵数据。

        ⚠️ 两处与规范不符、都已实测确认：

        · **第 29 条**：``业务需求`` / ``用户需求`` 的删除要 ``?storyID=<id>``
          作为 **query 参数**，而规范写的是路径参数 ``:epicID`` / ``:requirementID``。
          不带它服务端报 ``Missing required parameter: storyID.``

        · **第 36 条**：``应用`` **根本没有 DELETE 接口**（规范里也只有 POST/PUT）→
          直接抛 ``Unsupported``，而不是发一个注定失败的请求、
          再让上层从报错里去猜「是不是没权限」。

        ⚠️ 另注**第 37 条**：删除是**软删除** —— 删完 ``deleted`` 变 ``"1"``，
        记录仍能 ``GET`` 到、名字仍被占用。所以「删成功」≠「真的没了」。
        """
        ename, spec = self._resolve(table)
        why = spec.get("no_delete_why")
        if why:
            raise Unsupported("禅道实体「%s」不能删除：%s" % (ename, why))
        ids = [str(x).strip() for x in (row_ids or []) if str(x or "").strip()]
        for rid in ids:
            q = {p: rid for p in (spec.get("delete_query") or ())}
            self._call("DELETE", "/%s/%s" % (spec["resource"], rid), q)

    # ── 状态流转动作（第 41 条：专业 PM 软件最核心的能力）──────
    def actions_of(self, table: str) -> List[Dict[str, Any]]:
        """**扩展方法**（不在契约里）：列出该实体支持的状态流转动作。

        返回 ``[{"动作","说明","必填"}, …]``；不支持动作的实体返回 ``[]``
        —— 这里是**真的没有**（禅道只为部分实体提供流转子路径），不是读不到。

        为什么要把它做成可查询的：动作的**必填字段全靠实测**（规范里一个都没写），
        而且每个动作要的字段不一样（``start`` 要工时、``finish`` 还要三个日期）。
        调用方能先问一句「这个动作要什么」再决定怎么调，比试错省事。
        """
        ename, spec = self._resolve(table)
        return [{"动作": lab, "说明": desc, "必填": list(req), "路径": act}
                for lab, act, req, desc in (spec.get("actions") or ())]

    def run_action(self, table: str, row_id, action: str,
                   data: Dict[str, Any] = None) -> None:
        """**扩展方法**（不在契约里）：执行状态流转动作。

        ``PUT /<资源>/:id/<动作>`` —— 这类接口是「专业 PM 软件」和「一张表」
        的分水岭：``start`` 不是把 ``status`` 改成 ``doing`` 那么简单，
        它要记工时、写实际开始时间、按服务端规则算剩余；
        ``resolve`` 要记解决方案与解决版本。**用 ``update_row`` 改 ``status``
        是做不出这些副作用的。**

        实测要点（第 41 条）：

        · 每个动作的**必填字段都不一样**，且规范里一个都没写 ——
          必填清单来自实测，见 ``actions_of()``。
        · ``启动``/``完成``/``激活`` 少了工时字段会被拒
          （``"总计消耗"和"预计剩余"不能同时为0``）。
        · ``关闭`` 是**空 body 即可**的，但它**有副作用**：会写 ``closedDate``。
        · 动作**成功时不返回 id**（与 ``append_row`` 不同），所以本方法返回 ``None``。
        · **读是强一致的**：动作做完立刻 ``get_row`` 就能看到新状态
          （这点与飞书那 ~2.7s 的可见性延迟不一样）。
        """
        ename, spec = self._resolve(table)
        rid = str(row_id or "").strip()
        if not rid:
            raise ZentaoError("执行禅道实体「%s」的动作必须给 row_id（禅道 id）。" % ename)
        acts = spec.get("actions") or ()
        hit = [a for a in acts if action in (a[0], a[1])]
        if not hit:
            avail = " / ".join("%s(%s)" % (a[0], a[1]) for a in acts) or "（该实体没有动作）"
            raise Unsupported(
                "禅道实体「%s」没有动作 %r。可用的是：%s。"
                "　（动作是**服务端固定的子路径**，不像字段那样可以任意新增；"
                "要改普通属性请用 update_row。）" % (ename, action, avail))
        _lab, act, required, _desc = hit[0]
        by_label = self._by_label(spec)
        by_field = self._by_field(spec)

        payload: Dict[str, Any] = {}
        unknown: List[str] = []
        for k, v in (data or {}).items():
            f = ACTION_FIELDS.get(str(k))
            if f is None:
                col = by_label.get(k) or by_field.get(k)
                f = col[1] if col else None
            if f is None:
                unknown.append(str(k))
                continue
            if v is None or (isinstance(v, str) and not v.strip()):
                continue                          # 空值不发送（同 _to_payload 的规矩）
            if f in ACTION_ARRAY_FIELDS and not isinstance(v, (list, tuple)):
                v = [v]                           # 实测 openedBuild 要数组
            payload[f] = v
        if unknown:
            raise ZentaoError(
                "动作「%s」不认识这些字段：%s。可用的是：%s"
                % (_lab, " / ".join(unknown),
                   " / ".join(sorted(ACTION_FIELDS))))
        missing = [lab for lab in required
                   if ACTION_FIELDS.get(lab, lab) not in payload]
        if missing:
            raise ZentaoError(
                "禅道动作「%s（%s/%s/%s）」缺少必填字段：%s。"
                "　（这些必填是**实测**出来的，规范里没有写；可先调 actions_of('%s') 查。）"
                % (_lab, spec["resource"], rid, act, " / ".join(missing), ename))
        self._call("PUT", "/%s/%s/%s" % (spec["resource"], rid, act), body=payload)

    # ── 表结构 ─────────────────────────────────────────
    def ensure_table(self, table: str,
                     columns: Optional[List[Dict[str, Any]]] = None) -> str:
        """**不支持**：禅道的实体与字段都是固定的，REST v2 没有建表/加字段接口。

        这正是 ``base.py`` 里点名的「252 张固定表」那一类。需要自动建表的后端
        请用 local / seatable / feishu。这里的替代方案是在禅道界面里预建好实体，
        然后用 ``ENTITY_SPECS`` 把它的字段映射进来。
        """
        raise Unsupported(
            "禅道不支持通过 API 管理表结构：实体（项目/执行/任务/产品/需求/Bug…）"
            "与它们的字段都是系统固定的，REST v2 没有建表、加列、改列接口。"
            "需要新增字段请在禅道界面（或付费版工作流）里配置，"
            "再把映射补进 adapters/zentao.py 的 ENTITY_SPECS。")

    # ── 关联 ───────────────────────────────────────────
    def link_columns(self, table: str) -> List[str]:
        """该实体上**外键型关联列**的中文名（诊断用，可能为空）。

        禅道的关联不是「关联列」而是**实体自带的字段**：任务的 ``executionID``
        指向执行、需求的 ``productID`` 指向产品。这些列用 ``update_row``
        直接写 id 就能建立关联 —— 所以 ``link()`` 没必要存在（见下）。

        空列表是真实答案（该实体没有外键列），不是「读不了」。
        """
        ename, spec = self._resolve(table)
        return [c[0] for c in spec.get("link_cols") or ()]

    def link(self, table: str, other_table: str, link_id: str,
             row_id: str, other_row_ids: List[str]) -> None:
        """**不支持**：禅道没有「通用的可读写关联列」，也没有 link_id 概念。

        契约要求「同一 link_id，两张表各记一次」。禅道这边关联是**实体固定外键**
        （``task.executionID``、``story.productID``），一对表之间只有一条固定的
        方向确定的关系，既没有第二个方向可以「各记一次」，也没有 link_id 可供解析。

        替代做法（**已经能用的**）：把它当普通列写 ——
        ``update_row("任务", task_id, {"所属执行": execution_id})``。
        ``link_columns()`` 会告诉你该实体有哪些这种列。
        这里抛 ``Unsupported`` 而不是退化成「写那个外键」，是因为本方法签名的
        ``link_id`` 在禅道里**无意义**：拿它去猜字段，猜错就是把数据挂到别的业务线上。
        """
        raise Unsupported(
            "禅道没有通用关联接口（关联是实体固定外键字段，且没有 link_id 概念）："
            "「%s」→「%s」。请改用 update_row 直接写外键列，列名见 link_columns('%s')。"
            % (table, other_table, table))

    def list_linked(self, table: str, row_id: str, link_id: str) -> List[str]:
        """**不支持**：读不回「对方 row_id 列表」这种形状。

        禅道的外键只给**一个** id（``executionID``），给不出「本行关联到的对方
        记录数组」；而 ``row_id`` 参数在契约里是「本行的行标识」，与 link_id
        组合在本后端没有对应语义。

        按铁律抛 ``Unsupported``，**不返回空列表冒充「没有关联」**（本仓库吃过
        这个亏：SeaTable 的 ``list_linked`` 曾是 ``return []`` 空桩，
        于是「查关联」永远成功、永远返回空、永远不报错）。
        要读外键：``get_row(table, row_id)`` 里就有那个列的值。
        """
        raise Unsupported(
            "禅道读不回「行 → 关联到的对方行 id 列表」：外键字段只给单个 id，"
            "没有多对多的关联数组，也没有 link_id 概念。"
            "要看外键请用 get_row(%r, %r) 读 link_columns(%r) 里的列。"
            % (table, row_id, table))


def neutral_type(zentao_type: str) -> str:
    """禅道列类型 → 中立类型（``adapters/schema.py`` 词表）；查不到原样返回。"""
    return {
        "text": "text", "longtext": "longtext", "number": "number",
        "date": "date", "select": "select", "array": "multiselect",
        "secret": "text",
    }.get(str(zentao_type or "").lower(), str(zentao_type or "").lower())


def entity_tables() -> List[str]:
    """本后端支持的禅道实体名（不含别名）。供文档与测试使用。"""
    return sorted(ENTITY_SPECS)
