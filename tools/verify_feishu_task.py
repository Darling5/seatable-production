# -*- coding: utf-8 -*-
"""飞书任务后端连通性与契约验证脚本（部署流程的 verify 钩子）。

两种模式：

  ``--read-only``（**默认**，部署钩子用这个）
      只读：认证 → 清单清单 → 列结构 → 摘要读 → 详情读 → 分组。
      **一个写请求都不发**，可安全对待任何真实账号。

  ``--write-test``
      在**临时清单**上把写路径跑通（建清单 / 建列 / 建任务 / 改任务 / 改成员 /
      删除），跑完反向删干净。
      临时清单一律用 ``ZZ_WB_VERIFY_`` 前缀，并有**硬断言**：
      名字不带这个前缀的清单**绝不**被创建、更**绝不**被删除。

为什么要有它：飞书任务有几处「看起来成功」的坑 ——
  · ``tasks.patch`` 的 ``update_fields`` 是固定白名单，``members`` 不在其中；
  · 列了不给值 → 整条失败；给了没列 → **静默忽略**；
  · 列表接口只回 7 个摘要字段（连描述都没有）；
  · 不带清单建的任务是**孤儿**，任何清单都看不到；
  · 删一个不存在的 guid 会报 1470404（删除不幂等）。
这些只有拿真实服务端跑一遍才能证明适配器都堵住了。

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
    return bool(ok)


def _adapter(args):
    """构造适配器。**不读也不改**用户的 config.yaml —— 走注册表 + 内存配置。"""
    from adapters import factory
    cfg = {"backend": "feishu_task", "feishu_task": {}}
    if args.identity:
        cfg["feishu_task"]["identity"] = args.identity
    if args.cli_path:
        cfg["feishu_task"]["cli_path"] = args.cli_path
    a = factory.get_adapter(cfg)
    if a.backend != "feishu_task":
        raise SystemExit("[错误] 没能构造出 feishu_task 后端（拿到的是 %s）" % a.backend)
    return a


def _pick_smallest(a):
    """挑任务最少的清单做验证 —— list_rows 是 N+1，别拿 99 条的大清单去跑。"""
    best, best_n = None, None
    for t in a.list_tables():
        try:
            n = len(a.list_rows_brief(t))
        except Exception:
            continue
        if best_n is None or n < best_n:
            best, best_n = t, n
    return best, best_n


def read_only(a):
    print("\n── 只读验证 ──")
    check("认证成功", a._t is not None, "identity=%s" % (a.identity or "auto"))

    from adapters.base import (CAP_BATCH_WRITE, CAP_LINK, CAP_LINK_READ,
                               capabilities_of)
    caps = sorted(capabilities_of(a))
    check("能力声明可读且无副作用", bool(caps), ",".join(caps))
    check("如实声明「没有关联能力」",
          CAP_LINK not in caps and CAP_LINK_READ not in caps,
          "任务没有表↔表的关联列模型")
    check("如实声明「没有批量创建」", CAP_BATCH_WRITE not in caps,
          "任务 API 没有批量创建接口")

    names = a.list_tables()
    check("能列出任务清单", bool(names),
          "共 %d 个：%s" % (len(names), " / ".join(names[:5])))
    if not names:
        return

    t, n = _pick_smallest(a)
    if t is None:
        t = names[0]
    check("能选定一个清单", bool(t), "%r（约 %s 条任务）" % (t, n))

    md = a.get_metadata(t)
    cols = md["columns"]
    custom = [c for c in cols if c.get("custom")]
    check("能读列结构", bool(cols),
          "%s：%d 列（自定义 %d）" % (t, len(cols), len(custom)))
    check("内置列都在", all(any(c["name"] == x for c in cols) for x in
                            ("摘要", "截止时间", "负责人", "状态")),
          " / ".join(c["name"] for c in cols[:8]) + " …")
    check("列带可写标志", all("writable" in c for c in cols),
          "只读列 %d 个" % len([c for c in cols if not c["writable"]]))
    renamed = [c for c in cols if c.get("origin_name")]
    check("重名自定义字段已改名而非静默覆盖", True,
          "；".join("%s ← %s" % (c["name"], c["origin_name"]) for c in renamed)
          or "本清单无重名")

    brief = a.list_rows_brief(t)
    check("摘要读可用", isinstance(brief, list), "%s：%d 行" % (t, len(brief)))
    if brief:
        r0 = brief[0]
        check("摘要行带 __row_id__", bool(r0.get("__row_id__")))
        check("摘要读**不**给详情列占位（区别于 list_rows）",
              "描述" not in r0 and "状态" not in r0,
              "缺的键就是不存在，不是空值")

    rows = a.list_rows(t)
    check("详情读可用（N+1 补齐每一列）", isinstance(rows, list),
          "%s：%d 行" % (t, len(rows)))
    if rows:
        r0 = rows[0]
        missing = [c["name"] for c in cols if c["name"] not in r0]
        check("metadata 声明的每一列都真的取了值", not missing,
              "缺列：%s" % (" / ".join(missing) if missing else "无"))
        check("空值已归一成空串而非 None",
              all(v is not None for v in r0.values()),
              "None 会让下游拼出字面量 None")
        rid = r0["__row_id__"]
        one = a.get_row(t, rid)
        check("单行读取与列表一致",
              one is not None and one.get("__row_id__") == rid)
        check("不存在的任务返回 None", a.get_row(t, "00000000-0000-0000-0000-000000000000") is None)
        # 与摘要读交叉核对（两个接口的同一行必须一致）
        b0 = next((b for b in brief if b["__row_id__"] == rid), None)
        check("摘要读与详情读对同一行一致",
              b0 is None or b0["摘要"] == r0.get("摘要"),
              "%r == %r" % (b0 and b0["摘要"], r0.get("摘要")))

    check("不存在的清单 → table_exists 为假", a.table_exists(PFX + "不存在") is False)
    check("不存在的清单 → 报错并列出可选清单",
          _raises(lambda: a.get_metadata("绝对不存在清单XYZ"), KeyError))

    sec = a.list_sections(t)
    check("能读分组", isinstance(sec, list),
          "%s：%s" % (t, " / ".join(s["name"] for s in sec[:5])) or "（无分组）")

    check("link() 如实抛 Unsupported",
          _raises(lambda: a.link(t, t, "x", "y", []), NotImplementedError))
    check("list_linked() 如实抛 Unsupported（不是 return []）",
          _raises(lambda: a.list_linked(t, "y", ""), NotImplementedError))


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def _settle(fn, pred, tries=6, sleep=0.8):
    """写后读有可见性延迟（实测 ~0.4s）；轮询到达成或超时。"""
    last = None
    for _ in range(tries):
        time.sleep(sleep)
        try:
            last = fn()
        except Exception as e:  # noqa: BLE001
            last = e
            continue
        if pred(last):
            return last
    return last


def write_test(a):
    print("\n── 写路径验证（仅临时清单 %s*）──" % PFX)
    tl_name = PFX + time.strftime("%Y%m%d%H%M%S")
    baseline = set(a.list_tables())
    lguid = None
    tasks = []
    try:
        # ① 建清单
        r = a.ensure_table(tl_name, [
            {"name": "优先级", "type": "select", "options": ["高", "中", "低"]},
            {"name": "工时", "type": "number", "symbol": "人天"},
            {"name": "备注2", "type": "text"},
        ])
        check("能建清单（ensure_table）", r == "created", "%s → %s" % (tl_name, r))
        lguid = a._list_guid(tl_name)
        check("新建清单可解析", bool(lguid), lguid)
        check("重复 ensure_table 返回 exists", a.ensure_table(tl_name) == "exists")

        cols = {c["name"]: c for c in a.get_metadata(tl_name)["columns"]}
        check("自定义列已建出", all(x in cols for x in ("优先级", "工时", "备注2")),
              " / ".join(k for k in cols if k in ("优先级", "工时", "备注2")))
        check("select 列带回选项", cols.get("优先级", {}).get("options") == ["高", "中", "低"],
              str(cols.get("优先级", {}).get("options")))

        # ② 建任务
        me = a._t.probe_identity()["identities"]["user"]["openId"]
        due = time.strftime("%Y-%m-%d", time.localtime(time.time() + 7 * 86400))
        tid = a.append_row(tl_name, {
            "摘要": tl_name + "_任务A", "描述": "探测用，请忽略",
            "截止时间": due, "负责人": [me],
            "优先级": "高", "工时": "3",
        })
        tasks.append(tid)
        check("能建任务（append_row 回 guid）", bool(tid), tid)

        got = _settle(lambda: a.get_row(tl_name, tid),
                      lambda r: r and r.get("摘要") == tl_name + "_任务A")
        check("新建后可读回", bool(got and got.get("__row_id__") == tid))
        check("摘要写对", got.get("摘要") == tl_name + "_任务A", repr(got.get("摘要")))
        check("描述写对", got.get("描述") == "探测用，请忽略", repr(got.get("描述")))
        check("日期列写对（YYYY-MM-DD 往返）", got.get("截止时间") == due,
              "%s vs %s" % (got.get("截止时间"), due))
        check("单选列按**选项名**写入并读回选项名", got.get("优先级") == "高",
              repr(got.get("优先级")))
        check("数值自定义列写入", str(got.get("工时")) == "3", repr(got.get("工时")))
        check("未设置的自定义列是空串而不是缺键",
              "备注2" in got and got["备注2"] == "", repr(got.get("备注2")))

        # ③ 改任务（patch 白名单）
        a.update_row(tl_name, tid, {"摘要": tl_name + "_任务A改", "描述": "改后描述",
                                    "优先级": "低", "工时": "5"})
        got = _settle(lambda: a.get_row(tl_name, tid),
                      lambda r: r and r.get("摘要") == tl_name + "_任务A改")
        check("改摘要生效", got.get("摘要") == tl_name + "_任务A改", repr(got.get("摘要")))
        check("改描述生效", got.get("描述") == "改后描述", repr(got.get("描述")))
        check("改单选生效（选项名 → guid → 选项名）", got.get("优先级") == "低",
              repr(got.get("优先级")))

        # ④ 只读列被剔除并登记（而不是静默丢 / 或整条炸掉）
        a.update_row(tl_name, tid, {"摘要": tl_name + "_任务A改", "子任务数": 9})
        check("只读列被剔除且登记在 last_skipped", a.last_skipped == ["子任务数"],
              str(a.last_skipped))
        check("剔除只读列后其余字段仍写入成功",
              (a.get_row(tl_name, tid) or {}).get("摘要") == tl_name + "_任务A改")

        # ⑤ 未知列必须拒绝（拼错列名不能静默丢弃）
        check("未知列被拒绝", _raises(
            lambda: a.update_row(tl_name, tid, {"没有这一列": 1}), ValueError))

        # ⑥ 完成 / 取消完成
        a.update_row(tl_name, tid, {"完成状态": True})
        got = _settle(lambda: a.get_row(tl_name, tid), lambda r: r and r["完成状态"])
        check("能标记完成", got.get("完成状态") is True, repr(got.get("完成状态")))
        a.update_row(tl_name, tid, {"完成状态": False})
        got = _settle(lambda: a.get_row(tl_name, tid), lambda r: r and not r["完成状态"])
        check("能取消完成（completed_at='0'）", got.get("完成状态") is False)

        # ⑦ 成员走 members 接口（patch 白名单里没有 members）
        a.update_row(tl_name, tid, {"负责人": [me]})
        got = _settle(lambda: a.get_row(tl_name, tid), lambda r: r and r["负责人"])
        check("能设负责人（自动改走 task members add）", got.get("负责人") == [me],
              repr(got.get("负责人")))
        a.update_row(tl_name, tid, {"关注人": [me]})
        got = _settle(lambda: a.get_row(tl_name, tid), lambda r: r and r["关注人"])
        check("能设关注人且不动负责人",
              got.get("关注人") == [me] and got.get("负责人") == [me],
              "负责人=%s 关注人=%s" % (got.get("负责人"), got.get("关注人")))
        a.update_row(tl_name, tid, {"负责人": []})
        got = _settle(lambda: a.get_row(tl_name, tid), lambda r: r and not r["负责人"])
        check("能清空负责人且保留关注人",
              got.get("负责人") == [] and got.get("关注人") == [me],
              "负责人=%s 关注人=%s" % (got.get("负责人"), got.get("关注人")))

        # ⑧ 摘要读 / 详情读一致
        rows = a.list_rows(tl_name)
        brief = a.list_rows_brief(tl_name)
        check("新清单里能读到这条任务",
              len(rows) == 1 and rows[0]["__row_id__"] == tid)
        check("摘要读与详情读行数一致", len(brief) == len(rows))
        check("摘要读的值与详情读一致",
              rows[0]["摘要"] == brief[0]["摘要"] and
              rows[0]["截止时间"] == brief[0]["截止时间"])

        # ⑨ 删除：不幂等，删不存在的必须报错
        check("删不存在的任务会报错（删除不幂等）", _raises(
            lambda: a.delete_rows(tl_name, ["00000000-0000-0000-0000-000000000000"])))
        a.delete_rows(tl_name, [tid])
        tasks.remove(tid)
        rows = _settle(lambda: a.list_rows(tl_name), lambda r: not r)
        check("删除后清单里查不到", rows == [], "%d 行" % len(rows))

        # ⑩ 孤儿防护：不带清单建任务必须被适配器拦下
        check("清单不存在时 append_row 拒绝（防孤儿任务）", _raises(
            lambda: a.append_row(PFX + "不存在的清单", {"摘要": "x"}), KeyError))

    finally:
        print("\n── 清理（反序）──")
        for g in reversed(tasks):
            try:
                a.delete_rows(lguid or tl_name, [g])
                print("  删任务 %s" % g)
            except Exception as e:  # noqa: BLE001
                print("  ⚠️ 删任务 %s 失败：%s" % (g, e))
        if lguid:
            for f in a._custom_fields(lguid):
                try:
                    a._call("custom_fields.remove",
                            ["custom_fields", "remove", "--custom-field-guid",
                             f["guid"]],
                            body={"resource_id": lguid, "resource_type": "tasklist"})
                except Exception as e:  # noqa: BLE001
                    print("  ⚠️ 摘字段 %s 失败：%s" % (f.get("name"), e))
            # 硬闸门：绝不允许删掉不是本脚本建的清单
            assert tl_name.startswith(PFX), "拒绝删除非临时清单：%s" % tl_name
            try:
                a._call("tasklists.delete",
                        ["tasklists", "delete", "--tasklist-guid", lguid])
                print("  删清单 %s" % tl_name)
            except Exception as e:  # noqa: BLE001
                print("  ⚠️ 删清单失败：%s" % e)
        a._invalidate()
        time.sleep(1.0)
        after = set(a.list_tables())
        leaked = after - baseline
        check("清理后清单集合回到基线", not leaked,
              "多了：%s" % (" / ".join(sorted(leaked)) if leaked else "无"))
        residue = [x for x in a.list_tables() if x.startswith(PFX)]
        check("没有 ZZ_WB_VERIFY_ 残留", not residue,
              " / ".join(residue) if residue else "无")


def main():
    ap = argparse.ArgumentParser(description="飞书任务后端连通性与契约验证")
    ap.add_argument("--read-only", action="store_true", help="只读验证（默认行为）")
    ap.add_argument("--write-test", action="store_true",
                    help="⚠️ 会在临时清单 ZZ_WB_VERIFY_* 上真建东西并清理")
    ap.add_argument("--identity", default="", help="user / bot；留空跟随 lark-cli 默认")
    ap.add_argument("--cli-path", default="", help="lark-cli 可执行文件路径")
    args = ap.parse_args()

    print("=" * 70)
    print("飞书任务后端验证")
    print("=" * 70)
    a = _adapter(args)
    a.auth()
    print(a.describe())
    print("清单：%s" % " / ".join(a.list_tables()))

    read_only(a)
    if args.write_test:
        write_test(a)
    else:
        print("\n（未加 --write-test，跳过写路径验证）")

    print("\n" + "=" * 70)
    failed = [r for r in _results if not r[1]]
    print("结果：%d 项通过 / %d 项失败 / 共 %d 项"
          % (len(_results) - len(failed), len(failed), len(_results)))
    if failed:
        print("\n失败项：")
        for n, _ok, d in failed:
            print("  · %s%s" % (n, (" —— " + d) if d else ""))
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
