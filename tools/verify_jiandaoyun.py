# -*- coding: utf-8 -*-
"""简道云后端连通性与契约验证脚本（部署流程的 verify 钩子）。

⚠️ **本脚本未在真实简道云上跑过。** 本机没有简道云账号，所以它存在的意义是
「第一次接真实环境时，先把实际行为打印出来，好逐条核对
``adapters/jiandaoyun.py`` 里那些来自文档的推断」。它不是「已验证」的证明。

两种模式：

  ``--read-only``（**默认**，部署钩子用这个）
      只读：认证探活 → 表单清单 → 控件结构 → 拉一页数据 → 单行读取 → 不存在项。
      **一个写请求都不发**，可安全对待任何真实应用。

  ``--write-test``
      ⚠️ **简道云开放接口没有建表接口**，所以写路径无法在临时表里做。
      本模式会写进**指定表单**（``--table``，必填）并尽力清理自己写入的行。
      没有 ``--table`` 时**直接拒绝运行**，绝不猜一个表来写。

没配置凭据时：明确打印「未配置，跳过」并返回 0 —— 因为「这个后端还没接」是一个
正常状态，不是失败。**不会**假装验证过。

退出码：0 全通过 /未配置跳过；1 有失败项；2 参数或配置错误。
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("通过" if ok else "失败", name,
                           (" —— " + detail) if detail else ""))
    return ok


def _note(text):
    """打印一条「文档推断」的核对提示（不是断言，只提示人去比对）。"""
    print("      · %s" % text)


def _build(args):
    """按注册表链路构造适配器。

    刻意走 ``factory`` 而不是直接 new：顺带证明「注册完就不用改工厂」。
    用 ``--app-id`` / ``--api-key`` 时构造的是一份**内存里的** config，
    不读也不写用户的 config.yaml。
    """
    from adapters import factory
    if args.app_id or args.api_key:
        if not (args.app_id and args.api_key):
            raise SystemExit("[错误] --app-id 与 --api-key 必须同时给。")
        cfg = {"backend": "jiandaoyun",
               "jiandaoyun": {"api_key": args.api_key,
                              "bases": {args.base_name: {"app_id": args.app_id}}}}
        return cfg, args.base_name

    cfg = factory.load_config()
    inst = factory.get_base_config(cfg, base_name=args.base_name, backend="jiandaoyun")
    if not inst or not (inst.get("app_id")
                        and ((cfg.get("jiandaoyun") or {}).get("api_key")
                             or inst.get("api_key"))):
        return None, None
    return cfg, args.base_name


def read_only(a):
    print("\n── 只读验证 ──")

    from adapters.base import capabilities_of
    caps = sorted(capabilities_of(a))
    check("能力声明可读且无副作用", bool(caps), ",".join(caps))
    # 这几条是**反向断言**：能力声明里不该有的东西，出现了就说明有人把
    # 「文档没写清」当成了「支持」。用真实适配器跑，等价于把单测的判据
    # 也搬到真实环境上再确认一次。
    for cap, why in (("link", "没有关联写入接口"),
                     ("link_read", "lookup 只有显示文本，读不回对方 id"),
                     ("schema_manage", "只有字段只读清单，没有建表接口"),
                     ("query_pushdown", "cond[].type 取值域不明"),
                     ("idempotent", "transaction_id 要调用方自己给"),
                     ("optimistic_lock", "没有行级版本号")):
        check("不声明 %s" % cap, cap not in caps, why)

    tables = a.list_tables()
    check("能列出表单", bool(tables), "共 %d 张：%s" % (len(tables), " / ".join(tables[:6])))
    if not tables:
        return

    t = tables[0]
    meta = a.get_metadata(t)
    cols = meta["columns"]
    check("能读控件结构", bool(cols),
          "%s：%d 列（%s）" % (t, len(cols), " / ".join(c["name"] for c in cols[:6])))
    _note("请核对：这里列出的中文列名与简道云界面上的字段名是否一致"
          "（文档称设了别名后 API 用别名，label 才是界面名）")

    rows = a.list_rows(t)
    check("能拉数据", isinstance(rows, list), "%s：%d 行" % (t, len(rows)))
    if rows:
        r0 = rows[0]
        check("每行都带 __row_id__", bool(r0.get("__row_id__")), str(r0.get("__row_id__")))
        check("空单元格被归一成空值而非 None",
              all(v is not None for v in r0.values()),
              "（None 会让下游拼出字面量 None）")
        # 日期是本后端最容易悄悄错的地方：UTC 必须转成东八区再取日期。
        time_cols = [c["name"] for c in cols if c["type"] == "datetime"]
        if time_cols:
            vals = [r.get(time_cols[0]) for r in rows[:5]]
            ok = all(isinstance(v, str) and len(v) == 10 and v[4:5] == "-"
                     for v in vals if v not in ("", None))
            check("日期已转成 YYYY-MM-DD（UTC→东八区）", ok,
                  "%s 前几行：%s" % (time_cols[0], " / ".join(str(v) for v in vals)))
            _note("请拿上面这几个日期与简道云界面上的值**逐个人工比对**："
                  "若整体差一天，说明时区语义与文档不符（这正是本脚本要暴露的事）")
        rid = r0["__row_id__"]
        one = a.get_row(t, rid)
        check("单行读取与列表一致", one is not None and one.get("__row_id__") == rid)
        check("不存在的行返回 None", a.get_row(t, "不存在的data_id") is None)

    check("不存在的表单 → table_exists 为假", a.table_exists("ZZ_WB_不存在的表单") is False)
    check("不存在的表单 → 取结构报错（不返回空冒充）",
          _raises(lambda: a.get_metadata("ZZ_WB_不存在的表单"), KeyError))

    if a.link_columns(t) == []:
        _note("表「%s」没有关联类控件；若有 lookup/linkdata 控件而这里仍为空，"
              "说明控件类型名与文档不符" % t)


def write_test(a, table):
    print("\n── 写路径验证（会真实写入表单「%s」！）──" % table)
    meta = a.get_metadata(table)
    names = [c["name"] for c in meta["columns"] if not c.get("readonly")]
    txt = next((c["name"] for c in meta["columns"]
                if c.get("type") in ("text", "textarea") and not c.get("readonly")), None)
    timec = next((c["name"] for c in meta["columns"]
                  if c.get("type") == "datetime" and not c.get("readonly")), None)
    if not txt:
        print("  [跳过] 该表单没有可写的文本列，无法做安全的写测试。")
        return
    print("      将在表单「%s」上新增 1 行，用到的列：%s" % (table, " / ".join(names[:8])))

    created = None
    try:
        mark = "ZZ_WB_VERIFY_请忽略"
        data = {txt: mark}
        if timec:
            # 日期用「今天」：既验证写入，也验证回显校验能过。
            import datetime
            today = datetime.date.today().strftime("%Y-%m-%d")
            data[timec] = today
        rid = a.append_row(table, data)
        created = rid
        check("新增 1 行并能拿到 data_id", bool(rid), str(rid))

        # 不支持的日期格式必须在**客户端**就被拦下（服务端只会静默写成空）。
        if timec:
            check("误写的日期格式被拦下（不让服务端静默写空）",
                  _raises(lambda: a.update_row(table, rid, {timec: "2021/10/10 10:10:10"}),
                          Exception))

        got = a.get_row(table, rid)
        check("刚写入的行能读回", got is not None and got.get(txt) == mark,
              repr(got.get(txt) if got else None))
        if timec and got:
            _note("写入的日期 %s，读回 %r —— 请与界面对照确认时区转换正确"
                  % (data[timec], got.get(timec)))

        check("写不存在的列会报错（不静默丢弃）",
              _raises(lambda: a.update_row(table, rid, {"不存在的列": 1}), Exception))
    finally:
        if created:
            print("\n── 清理本次写入的行 ──")
            try:
                a.delete_rows(table, [created])
                check("已删除本次写入的行", a.get_row(table, created) is None,
                      "data_id=%s" % created)
            except Exception as e:
                check("已删除本次写入的行", False, "删除失败，请手工处理：%s" % e)


def _raises(fn, exc=Exception):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="简道云后端连通性与契约验证")
    ap.add_argument("--app-id", default="", help="应用 id（不给则读 config.yaml）")
    ap.add_argument("--api-key", default="", help="企业 APIKey（不给则读 config.yaml）")
    ap.add_argument("--base-name", default="production", help="命名实例名（默认 production）")
    ap.add_argument("--read-only", action="store_true", help="只读验证（默认行为）")
    ap.add_argument("--write-test", action="store_true",
                    help="额外做写路径验证；**必须**同时给 --table，且会真实写入该表单")
    ap.add_argument("--table", default="",
                    help="写路径验证用的表单名（没有临时表可用，故必须显式指定）")
    args = ap.parse_args()

    if args.write_test and not args.table:
        print("[错误] --write-test 必须同时给 --table。\n"
              "       简道云开放接口**没有建表接口**，没有安全的临时表可用，"
              "所以绝不替调用方猜一个表单来写。")
        return 2

    cfg, base_name = _build(args)
    if cfg is None:
        print("[未配置，跳过] config.yaml 里没有可用的 jiandaoyun 实例"
              "（需要 jiandaoyun.api_key + 某个实例的 app_id）。\n"
              "  这不是失败：该后端只是还没接入。\n"
              "  接入后请先跑 `python tools/verify_jiandaoyun.py --read-only`，"
              "拿实际输出逐条核对 adapters/jiandaoyun.py 里那些来自文档的推断。")
        return 0

    from adapters import factory
    a = factory.get_adapter(cfg, base_name=base_name, strict=True)
    try:
        a.auth()
    except Exception as e:
        print("[错误] 认证探活失败：%s" % e)
        return 1
    print("后端：%s" % a.describe())
    print("⚠️ 本脚本未在真实简道云上验证过：下面的每一项都请当作**待核对的实测**，"
          "而不是已知结论。")

    read_only(a)
    if args.write_test and not args.read_only:
        write_test(a, args.table)

    bad = [x for x in _results if not x[1]]
    print("\n══ 结果：%d 项通过 / %d 项失败 ══" % (len(_results) - len(bad), len(bad)))
    for n, _, d in bad:
        print("   ✗ %s %s" % (n, d))
    if not bad:
        print("（通过只说明「行为与文档推断一致」，不等于适配器已在生产验证过。）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
