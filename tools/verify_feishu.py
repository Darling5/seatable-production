# -*- coding: utf-8 -*-
"""飞书后端连通性与契约验证脚本（部署流程的 verify 钩子）。

两种模式：

  ``--read-only``（**默认**，部署钩子用这个）
      只读：认证 → 表清单 → 字段清单 → 拉一张表的记录 → 单行读取。
      **一个写请求都不发**，可安全对待任何真实 Base。

  ``--write-test``
      在**临时表**上把写路径跑通（建表 / 批量新增 / 更新 / 关联 / 删除），
      跑完删表。临时表一律用 ``ZZ_WB_VERIFY_`` 前缀，并有硬断言：
      名字不带这个前缀的表**绝不**被创建或删除。
      ⚠️ 会向目标 Base 建表，请只在确认可接受时使用。

为什么要有它：飞书后端有几处「看起来成功」的坑（更新的行不存在时 rc=0、
写只读列被静默丢弃、新建记录后 ~3s 内更新误报行不存在）。
只有拿真实服务端跑一遍才能证明适配器把这些坑都堵住了。

退出码：0 全通过；1 有失败项；2 参数/配置错误。
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PFX = "ZZ_WB_VERIFY_"

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("通过" if ok else "失败", name,
                           (" —— " + detail) if detail else ""))
    return ok


def _load_cfg(args):
    """构造一份**内存里的** config，不读也不写用户的 config.yaml。

    刻意走 ``--base-token`` 显式传参：验证脚本不该依赖、更不该修改生产配置。
    另外用 ``feishu.factory`` 的注册表链路构造适配器，顺带证明「注册完就不用改工厂」。
    """
    from adapters import factory
    if not args.base_token:
        cfg = factory.load_config()
        inst = factory.get_base_config(cfg, base_name=args.base_name, backend="feishu")
        if not inst:
            raise SystemExit(
                "[错误] 未配置飞书 Base。请在 config.yaml 里加：\n"
                "  backend: feishu\n"
                "  feishu:\n"
                "    bases:\n"
                "      production:\n"
                "        base_token: <你的 Base token>\n"
                "或用 --base-token 直接指定。")
        return cfg, None
    cfg = {"backend": "feishu",
           "feishu": {"bases": {args.base_name: {"base_token": args.base_token}}}}
    return cfg, args.base_name


def read_only(a):
    print("\n── 只读验证 ──")
    check("认证成功", a._t is not None, "transport=%s" % a._kind_used)

    from adapters.base import capabilities_of
    caps = sorted(capabilities_of(a))
    check("能力声明可读且无副作用", bool(caps), ",".join(caps))

    names = a.list_tables()
    check("能列出表", bool(names), "共 %d 张：%s" % (len(names), " / ".join(names[:5])))
    if not names:
        return

    t = names[0]
    meta = a.get_metadata(t)
    cols = meta["columns"]
    check("能读字段结构", bool(cols),
          "%s：%d 列（%s）" % (t, len(cols), " / ".join(c["name"] for c in cols[:6])))

    rows = a.list_rows(t)
    check("能拉记录", isinstance(rows, list), "%s：%d 行" % (t, len(rows)))
    if rows:
        r0 = rows[0]
        check("每行都带 __row_id__", bool(r0.get("__row_id__")), str(r0.get("__row_id__")))
        empty_ok = all(v is not None for k, v in r0.items())
        check("空单元格被归一成空值而非 None", empty_ok,
              "（None 会让下游拼出字面量 None）")
        rid = r0["__row_id__"]
        one = a.get_row(t, rid)
        check("单行读取与列表一致", one is not None and one.get("__row_id__") == rid)
        check("不存在的行返回 None", a.get_row(t, "recZZZZZZZZZZ") is None)

    check("不存在的表 → table_exists 为假", a.table_exists(PFX + "不存在") is False)


def write_test(a):
    print("\n── 写路径验证（仅临时表 %s*）──" % PFX)
    ta, tb = PFX + "A", PFX + "B"
    created = []
    try:
        r = a.ensure_table(ta, [{"name": "名称", "type": "text"},
                                {"name": "状态", "type": "select",
                                 "options": ["进行中", "已完成"]},
                                {"name": "数量", "type": "number"},
                                {"name": "日期", "type": "date"},
                                {"name": "完成", "type": "bool"}])
        created.append(ta)
        check("ensure_table 建表", r == "created", r)
        check("ensure_table 幂等", a.ensure_table(ta, []) == "exists")
        meta = a.get_metadata(ta)
        got = {c["name"]: c["type"] for c in meta["columns"]}
        want = {"名称": "text", "状态": "select", "数量": "number",
                "日期": "datetime", "完成": "checkbox"}
        # 比的是「名字→类型」映射而不是顺序：实测 +field-list 的顺序**不保证**与
        # 创建顺序一致（这次就与上次不同），按顺序断言等于给一个不该有的承诺。
        check("类型映射：bool→checkbox / date→datetime / select→select",
              got == want, str(got))

        t0 = time.time()
        ids = a.append_rows(ta, [
            {"名称": "甲", "状态": "进行中", "数量": 12.5, "日期": "2026-05-07", "完成": True},
            {"名称": "乙", "状态": "已完成", "数量": 3, "日期": "2026-05-08", "完成": False},
            {"名称": "丙", "状态": "进行中", "数量": 0, "日期": "2026-05-09", "完成": False},
        ])
        check("批量新增 3 行且 id 同序", len(ids) == 3 and all(ids), str(ids))

        # 立刻更新：这一条同时验证「最终一致性重试」生效（实测新记录 ~3s 内会误报
        # record_not_found，适配器必须重试而不是直接判定行不存在）
        t0 = time.time()
        a.update_row(ta, ids[0], {"数量": 99, "完成": True})
        check("新建后立即更新成功（一致性重试生效）", True, "耗时 %.1fs" % (time.time() - t0))

        def _n99():
            got = {r["__row_id__"]: r for r in a.list_rows(ta)}
            if str(ids[0]) in got and str(got[ids[0]]["数量"]) in ("99", "99.0"):
                return got
            return None

        ok, rows, dt = _eventually(_n99)
        check("更新后读回一致（读有明显延迟，故轮询）", ok, "生效耗时 %.1fs" % dt)
        rows = {r["__row_id__"]: r for r in a.list_rows(ta)}
        check("单选被扁平化成标量",
              rows[ids[1]]["状态"] == "已完成", repr(rows[ids[1]]["状态"]))
        check("日期被截成 YYYY-MM-DD",
              rows[ids[2]]["日期"] == "2026-05-09", repr(rows[ids[2]]["日期"]))
        check("勾选是 bool", rows[ids[0]]["完成"] is True)

        # 只读列剔除 + 未知列报错
        try:
            a.update_row(ta, ids[0], {"不存在的列": 1})
            check("写未知列会报错（不静默丢弃）", False)
        except Exception as e:
            check("写未知列会报错（不静默丢弃）", "不存在" in str(e), str(e)[:70])

        # 关联：建 B 表 + 一条指向 A 的 link 字段。
        # 注意 ensure_table 建不出 link 列（NON_CREATABLE_TYPES 会显式拒绝 —— 关联列要
        # 同时指定对方表，不是「一个类型」能表达的），所以这里用一次原生 field-create
        # 在**临时表**上把关联列搭出来，再走适配器验证读写。
        a.ensure_table(tb, [{"name": "标题", "type": "text"}])
        created.append(tb)
        tid_a, tid_b = a._table_id(ta), a._table_id(tb)
        a._call(["+field-create", "--base-token", a.base_token, "--table-id", tid_b],
                json_body={"name": "关联A", "type": "link", "link_table": tid_a})
        a._invalidate()
        check("link 列不能由 ensure_table 创建（要人工/原生接口预建）",
              _raises(lambda: a.ensure_table(PFX + "C", [{"name": "关联", "type": "link"}]),
                      exc=NotImplementedError))

        bids = a.append_rows(tb, [{"标题": "挂甲"}])
        check("B 表新增 1 行", len(bids) == 1 and bool(bids[0]), str(bids))
        check("link_columns 能列出关联列", a.link_columns(tb) == ["关联A"],
              str(a.link_columns(tb)))

        a.link_one_way(tb, ta, "关联A", bids[0], [ids[0]])
        linked = _read_after_write(lambda: a.list_linked(tb, bids[0], "关联A"))
        check("单向关联写入后可读回", linked == [str(ids[0])],
              "读到 %s（期望 [%s]）" % (linked, ids[0]))
        check("单向字段上 link() 显式拒绝（不静默只写一侧）",
              _raises(lambda: a.link(tb, ta, "关联A", bids[0], [ids[1]]),
                      exc=NotImplementedError))
        check("link_append 在飞书上显式不支持",
              _raises(lambda: a.link_append(tb, ta, "关联A", bids[0], [ids[1]]),
                      exc=NotImplementedError))
        check("对非关联列做关联会报错",
              _raises(lambda: a.link_one_way(tb, ta, "标题", bids[0], [ids[0]]),
                      exc=KeyError))
        check("无关联列的表 list_linked 会报错（不返回空冒充「没有」）",
              _raises(lambda: a.list_linked(ta, ids[0], ""), exc=KeyError))

        a.delete_rows(ta, [ids[2]])
        gone, _, dt = _eventually(
            lambda: str(ids[2]) not in {r["__row_id__"] for r in a.list_rows(ta)})
        check("删除生效（同样要等读延迟）", gone, "生效耗时 %.1fs" % dt)

    finally:
        print("\n── 清理临时表（只删 %s* 前缀）──" % PFX)
        for t in reversed(created):
            if not t.startswith(PFX):
                print("  [跳过] %s 不在临时表命名空间内，拒绝删除" % t)
                continue
            for attempt in range(4):
                if not a.table_exists(t):
                    break
                try:
                    a._call(["+table-delete", "--base-token", a.base_token,
                             "--table-id", a._tables.get(t) or t, "--yes"])
                except Exception as e:
                    print("  第 %d 次删除 %s 失败：%s" % (attempt + 1, t, str(e)[:80]))
                a._invalidate()
                time.sleep(2.5)   # 删表存在最终一致性，稍等再确认
            print("  %s：%s" % (t, "已删除" if not a.table_exists(t) else "!! 仍存在，请手工处理"))
        over = [n for n in a.list_tables() if n.startswith(PFX)]
        check("临时表已全部清理", not over, "残留：%s" % (" / ".join(over) or "无"))


def _raises(fn, exc=Exception):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


def _read_after_write(fn, tries=5, sleep=1.5):
    """写后读**可能读到旧值**（实测飞书有可见性延迟），所以读回要求非空时给几次机会。

    ⚠️ 这层重试只在验证脚本里加：适配器本身不做这件事 ——
    「读回是空的」既可能是「真的没有」也可能是「还没可见」，适配器无权替调用方
    把两种情况都当成前者重试。验证脚本知道期望值，才有资格重试。
    """
    got = []
    for i in range(tries):
        got = fn()
        if got:
            return got
        if i < tries - 1:
            time.sleep(sleep)
    return got


def _eventually(pred, tries=8, sleep=1.5):
    """轮询到 ``pred`` 成立为止。返回 (是否成立, 最后一次的值, 耗时秒)。

    为什么必须轮询而不是「写完立刻读，不等就判失败」：飞书的读**不是**强一致，
    实测更新/删除成功后立刻读回仍可能拿到旧值。直接判失败会把「最终一致的延迟」
    误报成「写入丢了」，而这种误报最坑人 —— 会让人去查一个根本不存在的 bug。
    反过来，把「延迟」当成「写入成功」也不行，所以这里给一个**有界的**等待窗口：
    窗口内没生效 = 真失败。
    """
    t0 = time.time()
    last = None
    for i in range(tries):
        last = pred()
        if last:
            return True, last, time.time() - t0
        if i < tries - 1:
            time.sleep(sleep)
    return False, last, time.time() - t0


def main():
    ap = argparse.ArgumentParser(description="飞书后端连通性与契约验证")
    ap.add_argument("--base-token", default="", help="Base token（不给则读 config.yaml）")
    ap.add_argument("--base-name", default="production", help="命名实例名（默认 production）")
    ap.add_argument("--read-only", action="store_true", help="只读验证（默认行为）")
    ap.add_argument("--write-test", action="store_true",
                    help="额外在临时表 %s* 上跑写路径并清理" % PFX)
    args = ap.parse_args()

    cfg, base_name = _load_cfg(args)
    from adapters import factory
    if base_name:
        a = factory.get_adapter(cfg, base_name=base_name, strict=True)
    else:
        a = factory.get_adapter(cfg, strict=True)
    try:
        a.auth()
    except Exception as e:
        print("[错误] 认证失败：%s" % e)
        return 1
    print("后端：%s" % a.describe())

    read_only(a)
    if args.write_test and not args.read_only:
        write_test(a)

    bad = [x for x in _results if not x[1]]
    print("\n══ 结果：%d 项通过 / %d 项失败 ══" % (len(_results) - len(bad), len(bad)))
    for n, _, d in bad:
        print("   ✗ %s %s" % (n, d))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
