# -*- coding: utf-8 -*-
"""后端适配器抽象接口。

SKILL.md 只依赖这个接口，不关心底层是本地文件、SeaTable、飞书多维表格、
简道云、禅道还是金蝶。所有方法返回/接受的都是「中文列名 → 值」的字典；
行标识统一叫 ``__row_id__``（方法形参历史上叫 ``row_id``，两者指同一个东西）。

三层契约
────────
本模块把「后端适配器」拆成三层，目的只有一个：**让上层不必猜**。

1. **必修方法**（``@abstractmethod``）
   任何后端都必须实现，ABC 会在实例化时强制。缺一个就 new 不出来，
   不会等到线上跑到一半才炸。

2. **能力声明**（``capabilities()`` / ``supports()``）
   后端**能做什么**。上层据此决定走哪条路径。以前是靠 ``hasattr(adapter, "auth")``
   或 ``adapter.__class__.__name__ == "SeaTableAdapter"`` 来分叉的 —— 但
   ``hasattr`` 对任何实现了抽象接口的适配器**恒为真**（连 LocalAdapter 都有
   ``auth``），分支永不触发，等于没探测；类名嗅探则把「换个后端」变成了改代码。
   能力声明是**声明式**的：不联网、不认证、无副作用，纯读类属性。

3. **可选方法**（带默认实现）
   不实现也能跑（走通用兜底），实现了就更高效/更精确。兜底路径若**无法给出
   正确答案**，必须抛 :class:`Unsupported`，**绝不允许静默返回空值** ——
   静默返回空会让上层把「查不到」当成「没有」，本仓库已经踩过这个坑
   （SeaTable 适配器的 ``list_linked`` 曾是一个 ``return []`` 的空桩，
   于是「查关联」命令永远成功、永远返回空、永远不报错）。

新增一个后端要做什么
────────────────────
给基类加上后，写一个新底座只剩三步：① 实现 8 个必修方法；② 覆盖
``backend`` / ``CAPS``；③ 在 ``adapters/factory.py`` 的注册表里登记一行。
**不需要**改任何上层调用方 —— 这正是「多底座可插拔」的定义。
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, FrozenSet, List, Optional

# ── 能力词表 ────────────────────────────────────────────────
# 只列「上层真的会据此分叉」的能力。不为了对称而凑数。
CAP_READ = "read"                        # list_rows / get_metadata
CAP_WRITE = "write"                      # append_row
CAP_UPDATE = "update"                    # update_row
CAP_DELETE = "delete"                    # delete_rows
CAP_LINK = "link"                        # link() 能真正建立关联（不是空实现）
CAP_LINK_READ = "link_read"              # list_linked() 能真正读回关联（不是空桩）
CAP_BATCH_WRITE = "batch_write"          # append_rows 是服务端批量，不是本地循环
CAP_SCHEMA_MANAGE = "schema_manage"      # 能通过 API 建表/改列（ensure_table）
CAP_IDEMPOTENT = "idempotent"            # 原生幂等键，重试 == 不重复
CAP_OPTIMISTIC_LOCK = "optimistic_lock"  # 行级版本号，支持乐观锁
CAP_QUERY_PUSHDOWN = "query_pushdown"    # query() 在服务端过滤，不是拉全表内存过滤
CAP_SERVER_ROW_ID = "server_row_id"      # __row_id__ 由服务端生成，客户端不可指定

#: 任何实现了必修方法的适配器都至少具备这些能力。
BASE_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_READ, CAP_WRITE, CAP_UPDATE, CAP_DELETE, CAP_LINK,
})


class Unsupported(NotImplementedError):
    """后端不支持该**可选**操作。

    继承 ``NotImplementedError``，让老代码里 ``except NotImplementedError``
    也能兜住，不至于因为换后端而穿透成未捕获异常。

    用法铁律：可选方法**只有在能给出正确答案时**才返回结果；拿不到答案就抛它。
    ``return []`` / ``return None`` 冒充「没有」是禁止的。
    """


class BaseAdapter(ABC):
    #: 后端标识。子类必须覆盖（'local' / 'seatable' / 'feishu' / ...）。
    #: 上层用它做诊断与分叉，替代 ``__class__.__name__`` 嗅探。
    backend: str = "base"

    #: 本后端声明支持的原子能力。子类覆盖它。
    #: ⚠️ 这里必须是纯常量：不要在里面做 I/O 或认证 —— 上层会在**建连之前**
    #:    读它来决定「要不要建连 / 建哪种连」。
    CAPS: FrozenSet[str] = BASE_CAPABILITIES

    # ── 能力声明（声明式，无副作用）────────────────────────
    def capabilities(self) -> FrozenSet[str]:
        """返回本适配器支持的能力集合。廉价、无副作用、可重复调用。"""
        return frozenset(getattr(self, "CAPS", BASE_CAPABILITIES))

    def supports(self, cap: str) -> bool:
        """是否支持某一项能力。上层分叉请用这个，不要用 hasattr / 类名。"""
        return cap in self.capabilities()

    def describe(self) -> str:
        """一行诊断串（日志/报错用，不含任何凭证）。"""
        return "%s(%s) caps=%s" % (
            type(self).__name__, self.backend, ",".join(sorted(self.capabilities())) or "-")

    # ── 生命周期 ──────────────────────────────────────────
    @abstractmethod
    def auth(self) -> None:
        """建立/刷新连接（local 模式为空操作）。"""

    def close(self) -> None:
        """释放连接/会话。默认什么都不做。

        为什么要有：禅道那类「token 其实是 PHP session id」的后端，不显式关闭
        会一直占着服务端会话；并发登录还会互相顶号。有了这个钩子，上层就能在
        批量任务结束时主动收尾，而不用把会话管理知识塞进调用方。
        """
        return None

    # ── 读 ────────────────────────────────────────────────
    @abstractmethod
    def list_rows(self, table: str) -> List[Dict[str, Any]]:
        """返回某表全部行，每行是 {中文列名: 值, '__row_id__': row_id}。"""

    @abstractmethod
    def get_metadata(self, table: str) -> Dict[str, Any]:
        """返回表结构：{"table_name":..., "columns":[{"name":..., "type":...}]}。"""

    def get_row(self, table: str, row_id: str) -> Optional[Dict[str, Any]]:
        """按 row_id 取单行；不存在返回 None。

        默认实现是「拉全表再线性查找」—— 对任何后端都成立，但对大表很贵。
        后端若支持单行/带过滤读取，请覆盖它（并声明相应能力）。
        返回值语义与 ``application/project_brain/store.LocalMemoryAdapter.get_row``
        保持一致：**找不到返回 None，不抛异常**。
        """
        target = str(row_id)
        for r in self.list_rows(table):
            if str(r.get("__row_id__", "")) == target:
                return r
        return None

    def table_exists(self, table: str) -> bool:
        """表是否存在。默认用 get_metadata 探测（读元数据，通用但可能昂贵）。"""
        try:
            self.get_metadata(table)
            return True
        except Exception:
            return False

    # ── 写 ────────────────────────────────────────────────
    @abstractmethod
    def append_row(self, table: str, data: Dict[str, Any]) -> str:
        """新增一行，返回新行 row_id（已自动套默认值/跳过自动列）。"""

    def append_rows(self, table: str, rows: List[Dict[str, Any]]) -> List[str]:
        """批量新增，返回与入参同序的 row_id 列表。

        默认实现退化为逐条 ``append_row``。**注意**：这个默认实现只保证「语义
        等价」，不保证「原子性」—— 中途失败时前面的行已经落库了。声明了
        ``CAP_BATCH_WRITE`` 的后端（如飞书 Bitable 一次 500 条、简道云一次 100 条）
        必须覆盖它，并且要如实暴露「批量是原子的还是部分的」，不要拿本方法当
        原子事务用。
        """
        return [self.append_row(table, r) for r in (rows or [])]

    @abstractmethod
    def update_row(self, table: str, row_id: str, data: Dict[str, Any]) -> None:
        """更新指定行（只传要改的字段）。"""

    @abstractmethod
    def delete_rows(self, table: str, row_ids: List[str]) -> None:
        """删除多行。"""

    # ── 关联 ──────────────────────────────────────────────
    @abstractmethod
    def link(self, table: str, other_table: str, link_id: str,
             row_id: str, other_row_ids: List[str]) -> None:
        """建立双向关联（同一 link_id，两张表各记一次）。

        ⚠️ 语义是**整体替换**：``other_row_ids`` 就是该行最终应关联到的全集。
        想「追加一条关联而不冲掉历史」请用 :meth:`link_append`，
        直接拿本方法做增量是错的（会把既有历史覆盖掉）。
        """

    @abstractmethod
    def list_linked(self, table: str, row_id: str, link_id: str) -> List[str]:
        """返回某行在某 link_id 上关联到的对方 row_id 列表。

        ``link_id`` 传空字符串表示「该行所有关联列的并集」。
        后端若无法真正读回关联，必须抛 :class:`Unsupported`，
        **不要** ``return []`` —— 那会让「没有关联」和「读不了」变得无法区分。
        """

    def link_append(self, table: str, other_table: str, link_id: str,
                    row_id: str, other_row_ids: List[str]) -> List[str]:
        """**追加式**关联：在既有关系之上新增，保留历史。返回追加后的完整列表。

        为什么必须有：``link()`` 是整体替换语义（SeaTable 的 PUT /links/ 会拿
        ``other_rows_ids_map`` 覆盖该行原有的全部关联）。而业务里更常见的是
        「再挂一个」——例如给某个客户线索再叠一条跟进记录。若用 ``link()`` 做
        增量，每挂一条新记录都会冲掉之前挂的，且**不报错**。

        默认实现抛 ``Unsupported`` 而不是「先读后写」地硬凑：读-改-写在有并发写
        的后端上会产生丢失更新（飞书同表禁止并发写、SeaTable 的 PUT 是整体覆盖），
        悄悄做这个加法比不做更危险。支持的后端请覆盖并声明 ``CAP_LINK``。
        """
        raise Unsupported(
            "%s 未实现 link_append（追加式关联）；若只需整体替换请改用 link()"
            % type(self).__name__)

    def link_one_way(self, table: str, other_table: str, link_id: str,
                     row_id: str, other_row_ids: List[str]) -> None:
        """**只写 ``table`` 一侧**的关联列，完全不碰对方表。

        为什么必须有：``link()`` 是**双向**的。但业务里有大量「子记录归属父记录」
        的单向关系——一条跟进记录属于某个客户线索。此时若用双向 ``link()``，
        它会顺手把**父记录**那一侧的关联列表整体替换掉，于是父记录上历史挂过的
        所有子记录**全部消失，且不报错**。``domain/crm_dispatch.py`` 正是踩了这个
        坑，才不得不绕过适配器、拿 ``adapter._base()`` / ``adapter._h`` 去裸发
        PUT —— 四个私有属性，换任何后端都必然 ``AttributeError``。

        语义是**该行在此 link_id 上最终只关联到 other_row_ids**（设置，非追加）。
        想「保留历史再加一条」用 :meth:`link_append`。

        默认抛 ``Unsupported``，不提供「退化成双向 link()」的兜底：
        那正是本方法要消灭的错误行为，悄悄降级等于把坑换个地方埋。
        """
        raise Unsupported(
            "%s 未实现 link_one_way（单向关联）；双向整体替换请用 link()，"
            "追加请用 link_append()" % type(self).__name__)

    # ── 表结构 ────────────────────────────────────────────
    def ensure_table(self, table: str, columns: Optional[List[Dict[str, Any]]] = None) -> str:
        """确保表存在；返回 ``"created"`` 或 ``"exists"``。

        ``columns`` 是列定义列表，元素形如
        ``{"name": "编号", "type": "text"}``（中立类型词表见 ``adapters/schema.py``）。

        为什么必须在协议层：以前这个能力是上层**绕过适配器**实现的 ——
        ``workflows/loop_sync.py`` 直接读 ``adapter._meta``、调 ``adapter._base()``、
        拼私有头 ``adapter._h`` 去裸 ``POST /tables/``。四个私有属性，换任何后端
        都必然 ``AttributeError``。建表是「后端能力」，就该由后端自己实现。

        默认抛 ``Unsupported``：不能通过 API 管表结构的后端（禅道 252 张固定表、
        金蝶元数据设计器、简道云只读字段接口）就该老实说做不到，由上层决定
        「报错」还是「改用人工预建 + 只做数据映射」。
        """
        raise Unsupported(
            "%s 不支持通过 API 管理表结构（ensure_table）" % type(self).__name__)

    # ── 版本 / 并发 ───────────────────────────────────────
    def version_of(self, table: str, row_id: str) -> Optional[int]:
        """返回行版本号（乐观锁）；后端不提供则返回 None。

        默认 None = 「没有版本概念」，上层必须把 None 当作「无法做乐观锁」处理，
        而不是当作版本 0。
        """
        return None

    # ── 便捷查询 ──────────────────────────────────────────
    def query(self, table: str, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """简单等值/包含过滤，filters={列名: 值}。

        默认实现在**客户端**过滤（拉全表）。支持服务端过滤的后端请覆盖并声明
        ``CAP_QUERY_PUSHDOWN`` —— 对十万行的表，这两者的差别是「秒级」和「超时」。
        """
        rows = self.list_rows(table)
        if not filters:
            return rows
        out = []
        for r in rows:
            ok = True
            for k, v in filters.items():
                cur = r.get(k, "")
                if v is None:
                    if cur not in ("", None, []):
                        ok = False
                elif isinstance(v, str) and v.startswith("*"):
                    if v[1:] not in str(cur):
                        ok = False
                elif str(cur) != str(v):
                    ok = False
            if ok:
                out.append(r)
        return out


# ══════════════════════════════════════════════════════════════
# 面向调用方的能力读取入口
#
# 为什么要模块级函数、而不是只留 adapter.capabilities()：
#   仓库里存在**不继承 BaseAdapter 的鸭子类型适配器**
#   （如 application/project_brain/store.LocalMemoryAdapter —— 它只为 project_brain
#    实现接口形状，刻意不依赖 adapters 包）。上层若直接写
#   `adapter.capabilities()` 会在它身上 AttributeError。
#   这两个函数给出一条统一、且**保守**的读法。
# ══════════════════════════════════════════════════════════════

def capabilities_of(adapter) -> FrozenSet[str]:
    """读取任意适配器的能力集合。没有声明时按「最小可用」保守推断。

    读取优先级（先声明的赢）：
      ① ``capabilities()`` 方法 —— BaseAdapter 子类走这条；
      ② ``CAPS`` 类属性 —— **鸭子类型适配器**（不继承 BaseAdapter、只为接口形状
         而存在，如 `application/project_brain/store.LocalMemoryAdapter`、测试替身）
         走这条。少了这一步，一个明确声明了 ``CAPS = {...CAP_SCHEMA_MANAGE}`` 的
         适配器会被判成「不支持建表」，能力声明形同虚设。
      ③ 保守推断 —— 只认**能由方法存在性证伪**的能力（读/写/改/删）。
         **不**推断关联类能力：``link`` / ``list_linked`` 完全可能存在却是个空实现
         （历史上 SeaTable 的 list_linked 就是 ``return []``），光看方法在不在
         分辨不出来。宁可判它「不支持」走上层兜底，也不要判它「支持」然后拿到假结果。
    """
    caps = getattr(adapter, "capabilities", None)
    if callable(caps):
        try:
            return frozenset(caps())
        except Exception:
            pass  # 声明本身出错 → 继续往下试，绝不因它炸掉调用方
    declared = getattr(adapter, "CAPS", None)
    if isinstance(declared, (set, frozenset, list, tuple)):
        return frozenset(declared)
    inferred = set()
    if callable(getattr(adapter, "list_rows", None)) and \
            callable(getattr(adapter, "get_metadata", None)):
        inferred.add(CAP_READ)
    if callable(getattr(adapter, "append_row", None)):
        inferred.add(CAP_WRITE)
    if callable(getattr(adapter, "update_row", None)):
        inferred.add(CAP_UPDATE)
    if callable(getattr(adapter, "delete_rows", None)):
        inferred.add(CAP_DELETE)
    return frozenset(inferred)


def supports(adapter, cap: str) -> bool:
    """``cap in capabilities_of(adapter)`` 的可读写法。"""
    return cap in capabilities_of(adapter)


def backend_of(adapter) -> str:
    """适配器的后端标识；未声明时退回类名小写，便于日志与错误定位。"""
    b = getattr(adapter, "backend", None)
    if isinstance(b, str) and b and b != "base":
        return b
    return type(adapter).__name__.lower()
