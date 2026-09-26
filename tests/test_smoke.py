#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_smoke.py — 零依赖冒烟测试（不需要 pytest，直接 python test_smoke.py）。

覆盖：
  1. 所有模块可导入、HTML 模板占位符可完整替换
  2. 资源域计算：负载率 / 超载 / 闲置 / 排程冲突 / 人工成本
  3. 演示数据能稳定产出「超载 + 闲置 + 冲突」三种信号（否则驾驶舱首次打开是死页面）
  4. 口令从 config 读取，且明文不出现在产物 HTML 里
  5. 每条 next_action 的 cat 都能在驾驶舱里找到落点（否则「去处理」按钮消失）

强制在临时目录跑，绝不读写真实 data/。
"""
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# 测试已归入 tests/，模块与 data/ 在上一级仓库根目录
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

_FAIL = []
_PASS = [0]


def check(cond, msg):
    if cond:
        _PASS[0] += 1
    else:
        _FAIL.append(msg)
        print("  FAIL: " + msg)


# 文本类扩展名才做去敏扫描；二进制/图片直接跳过
_BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".xlsx", ".xls",
               ".zip", ".bundle", ".woff", ".woff2", ".ttf")


def _tracked_text_files():
    """返回「会被提交的文本文件」绝对路径列表。

    优先 `git ls-files`（精确等于入库面，天然排除 config.yaml / data/ 等
    gitignore 项）；无 git 时退化为目录遍历 + 跳过本地文件。
    """
    try:
        out = subprocess.run(["git", "-C", _HERE, "ls-files"],
                             capture_output=True, timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            res = []
            for p in out.stdout.decode("utf-8", "replace").splitlines():
                p = p.strip()
                if not p or p.endswith(_BINARY_EXT):
                    continue
                fp = os.path.join(_HERE, p)
                if os.path.isfile(fp):
                    res.append(fp)
            if res:
                return res
    except Exception:
        pass
    _skip = {".git", "data", "__pycache__", ".venv", "node_modules", ".workbuddy"}
    res = []
    for root, dirs, files in os.walk(_HERE):
        dirs[:] = [d for d in dirs if d not in _skip]
        for fn in files:
            if fn.endswith(_BINARY_EXT):
                continue
            if fn.startswith("config") and fn.endswith((".yaml", ".yml")):
                continue
            res.append(os.path.join(root, fn))
    return res


def main():
    tmp = tempfile.mkdtemp(prefix="cockpit_test_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        import adapters.factory as F
        from adapters.local import LocalAdapter
        from adapters import schema

        real = F.get_adapter
        F.get_adapter = lambda config=None: LocalAdapter(data)
        try:
            from tools import seed_demo
            seed_demo.get_adapter = F.get_adapter
            seed_demo.main()

            ad = LocalAdapter(data)
            ad.auth()
            today = datetime.date.today()
            plans = ad.list_rows("生产计划")

            print("\n[1] 表结构")
            check(len(schema.TABLES) == 22, "TABLES 应为 22 张，实际 %d" % len(schema.TABLES))
            check("工作日志" in schema.TABLES and "阶段轨迹" in schema.TABLES, "第二大脑表未注册")
            check("资源" in schema.TABLES and "资源分配" in schema.TABLES, "资源域表未注册")
            check(schema.link_id_for("资源", "资源分配") == "RsAl", "资源↔资源分配 link_id 解析失败")
            check(schema.columns_of("资源") is not None, "columns_of('资源') 返回 None")
            check(len(schema.validate_enum("资源", {"类型": "外星人"})) == 1, "枚举软校验未生效")
            check(len(schema.validate_enum("资源", {"类型": "人员"})) == 0, "合法枚举被误报")

            print("[2] 资源域计算")
            sys.path.insert(0, os.path.join(_HERE, "cockpit"))
            import cockpit
            res = cockpit.compute_resources(ad, today, plans)
            check(res is not None, "compute_resources 返回 None（演示资源数据未被读到）")
            if res:
                check(res["week"]["workdays"] == 5, "本周工作日应为 5，实际 %s" % res["week"]["workdays"])
                check(len(res["over"]) >= 1, "演示数据未产生「超载」信号")
                check(len(res["idle"]) >= 1, "演示数据未产生「闲置」信号")
                check(len(res["conflicts"]) >= 1, "演示数据未产生「排程冲突」信号")
                check(res["labor_cost"] > 0, "人工成本为 0")
                check(len(res["plan_cost"]) > 0, "未按生产计划归集人工成本")
                over = res["over"][0]
                check(over["load"] > 100, "超载资源负载率应 >100%%，实际 %s" % over["load"])
                # 已完成的分配计成本但不占本周负载
                idle = res["idle"][0]
                check(idle["week_alloc"] == 0, "闲置资源本周投入应为 0")

            print("[3] 空资源时模块应隐藏")
            empty_dir = os.path.join(tmp, "empty")
            os.makedirs(empty_dir)
            ad2 = LocalAdapter(empty_dir)
            ad2.auth()
            check(cockpit.compute_resources(ad2, today, []) is None,
                  "无资源数据时应返回 None（隐藏模块），而不是空表")

            print("[4] 模型与行动建议")
            model = cockpit.compute(ad, today)
            check(model.get("resource") is not None, "model 缺少 resource")
            for k in ("res_load", "res_over", "res_conflict", "labor_cost"):
                check(k in model["kpi"], "KPI 缺少 %s" % k)
            acts = model["next_actions"]
            check(any(a.get("cat") == "resource" for a in acts), "未生成资源类行动建议")
            # 每条建议都要有 pri / cat / text，且 cat 在驾驶舱有映射
            # （KNOWN 必须与 cockpit.py 的 CAT_SEC 一致：v1.3 加了 wechat，v1.5 加了 market，
            #  v1.6 又把 market 类建议翻倍（物料+原料），v1.7 加了 risk（风险雷达），漏登记会导致测试 FAIL 而非静默漏报）
            KNOWN = {"purchase", "warehouse", "delivery", "production", "sales",
                     "boss", "resource", "wechat", "market", "risk"}
            for a in acts:
                check(a.get("pri") in ("高", "中", "提示"), "行动建议优先级非法: %r" % a.get("pri"))
                check(a.get("cat") in KNOWN, "行动建议 cat 未在 CAT_SEC 中登记: %r" % a.get("cat"))
                check(bool(a.get("text")), "行动建议缺少文案")

            print("[5] HTML 产物")
            html = cockpit.HTML.replace("__MODEL__", json.dumps(model, ensure_ascii=False))
            for k, v in cockpit.ICONS.items():
                html = html.replace("__" + k + "__", v)
            html = html.replace("__PW_BLOB__", json.dumps(cockpit.PW_BLOB))
            import re
            left = sorted(set(re.findall(r"__[A-Z][A-Z0-9_]{2,}__", html)))
            check(not left, "HTML 中存在未替换占位符: %s" % left)
            check("sec-Rs" in html, "HTML 中缺少资源区")
            check(".rs-track" in html, "缺少资源进度条样式（数字会被填充条盖住）")
            # 口令明文不得出现
            check(cockpit.ADMIN_PASSWORD not in html,
                  "管理员口令明文出现在 HTML 中（应为 base64 混淆）")
            for pw in cockpit.ROLE_PASSWORDS.values():
                check(pw not in html, "角色口令明文出现在 HTML 中")
            # 每个 CAT_SEC 已登记的类别都能在 JS 里找到
            check('resource:["Rs"' in html.replace(" ", "").replace("\n", "") or
                  'resource:["Rs","G","T"]' in html, "CAT_SEC 未登记 resource 类别")

            print("[6] 口令生成幂等")
            a1, r1 = cockpit._load_pw()
            a2, r2 = cockpit._load_pw()
            check(a1 == a2 and r1 == r2, "两次读取口令不一致（每次生成会导致分享出去的口令失效）")

            print("[7] 录入分级（第二大脑的安全底线）")
            from domain import intake

            def mk(op, table, data, row_id=None):
                return intake.assess(intake.Intent(op, table, data, row_id), ad)

            # 低风险：补备注 → 自动
            check(mk("update", "项目", {"备注": "客户口头反馈"}).risk == intake.AUTO,
                  "补备注被误判为高风险，日常录入会被卡死")
            # 高风险：改交期/金额 → 必须确认
            check(mk("update", "项目", {"合同交期": "2026-09-01"}).risk == intake.CONFIRM,
                  "改合同交期未被拦截 —— 关键数据可能被静默改写")
            check(mk("update", "项目", {"合同总价": 1}).risk == intake.CONFIRM,
                  "改合同总价未被拦截")
            # 高风险：在采购表新建 → 等于花钱
            check(mk("append", "PCB下单记录", {"生产计划": "X"}).risk == intake.CONFIRM,
                  "新建采购记录未被拦截")
            # 记忆表纯追加 → 自动
            check(mk("append", "工作日志", {"原话": "随便说两句"}).risk == intake.AUTO,
                  "写工作日志不该需要确认，否则没人愿意记")
            # 改动前的值必须被展示出来，否则「审核」是盲审
            p = ad.list_rows("项目")[0]
            it = mk("update", "项目", {"合同交期": "2026-12-31"}, p["__row_id__"])
            check(any("→" in w for w in it.warnings), "未展示 改动前→改动后，无法审核")

            # 阶段：合法推进自动、跳步/回退需确认
            check(mk("stage", "项目", {"原阶段": "立项", "新阶段": "研发"}).risk == intake.AUTO,
                  "合法阶段推进被误拦")
            jump = mk("stage", "项目", {"原阶段": "立项", "新阶段": "量产"})
            check(jump.risk == intake.CONFIRM, "阶段跳步未被拦截")
            check(any("跳步" in w for w in jump.warnings), "跳步未给出说明")
            back = mk("stage", "项目", {"原阶段": "试产", "新阶段": "打样"})
            check(back.risk == intake.AUTO, "试产→打样 是合法返工路径，不该拦")
            # 空的原阶段绝不能让跳步检查失效（曾经的真实缺陷）
            check(schema.stage_jump_warning("立项", "量产") is not None,
                  "stage_jump_warning 对跳步失灵")

            # 工序 / 生命周期两套枚举必须互不误伤
            check(len(schema.validate_enum("生产计划", {"阶段": "贴片"})) == 0,
                  "工序值『贴片』被生命周期枚举误报")
            check(len(schema.validate_enum("项目", {"阶段": "打样"})) == 0,
                  "生命周期值『打样』被误报")

            print("[8] 混合批次不可半写")
            batch = [mk("update", "项目", {"备注": "低风险"}),
                     mk("update", "项目", {"合同交期": "2026-10-01"})]
            auto, confirm = intake.split(batch)
            check(len(auto) == 1 and len(confirm) == 1, "分组结果不符")
            plan = intake.render_plan(batch)
            check("需要你确认" in plan and "合同交期" in plan, "确认清单未列出关键改动")
            print("[9] 可移植性：不得写死任何一家公司的供应商")
            from tools import doctor as _doc
            for _t, _d in schema.TABLE_DEFAULTS.items():
                for _c in ("供应商", "组装厂", "贴片厂"):
                    check(not _d.get(_c),
                          "「%s.%s」写死了默认供应商『%s』——别家公司装上会用错"
                          % (_t, _c, _d.get(_c)))
            # 用户配置能覆盖默认值
            md = schema.merged_defaults("外壳采购记录",
                                        {"defaults": {"外壳采购记录": {"供应商": "张三厂"}}})
            check(md.get("供应商") == "张三厂", "config 的 defaults 未生效")
            check(md.get("采购时间") == "__TODAY__", "覆盖 defaults 时把内置默认值弄丢了")
            # ── 去敏守卫：抓形态 + 扫全量 + 用真值表反向覆盖 ────────────
            # 历史教训（2026-09-26 复盘）：旧守卫是「15 个硬编码真名的黑名单
            # + 只扫 SKILL.md」，仓库里 28 个被跟踪文件、287 行真名全部漏过，
            # 而 CI 一路绿灯 —— 名单外的东西压根不在射程内，守卫自己还把
            # 真名提交进了仓库。现在：① 抓形态不抓名单 ② 扫入库面全量
            # ③ 真值表反向覆盖（新增真名只改 config，守卫自动生效）。
            print("[09b] 去敏守卫（全仓库）")
            _files = _tracked_text_files()
            check(len(_files) > 50, "守卫只扫到 %d 个文件，明显偏少（目录解析错？）" % len(_files))
            _corpus = {}
            for _f in _files:
                try:
                    _rel = os.path.relpath(_f, _HERE).replace("\\", "/")
                    _corpus[_rel] = io.open(_f, encoding="utf-8").read()
                except Exception:
                    pass  # 非 UTF-8 文本直接跳过

            # (1) 手机号形态（测试占位号放行）
            _TEL_OK = ("13800000000", "13711111111", "13911111111")
            _bad = []
            for _rel, _t in _corpus.items():
                for _m in sorted(set(re.findall(r"(?<!\d)1[3-9]\d{9}(?!\d)", _t))):
                    if _m not in _TEL_OK:
                        _bad.append("%s→%s" % (_rel, _m[:3] + "****" + _m[-2:]))
            check(not _bad, "出现真实手机号：%s" % "；".join(_bad[:5]))

            # (2) 未泛化的完整企业名
            #     放行：含「示例/某/测试」的占位串、以及纯后缀词表项
            #     （`股份有限公司`/`科技有限公司` 等是匹配词表，不是主体名）
            _CORP_OK = re.compile(r"示例|某|测试")
            _SUFFIX_ONLY = re.compile(
                r"^(?:股份|有限责任|科技|电子|实业|贸易|网络|信息|智能|材料)?有限公司$")
            _bad = []
            for _rel, _t in _corpus.items():
                for _m in sorted(set(re.findall(r"[\u4e00-\u9fa5]{2,14}(?:有限公司|股份有限公司)", _t))):
                    if _CORP_OK.search(_m) or _SUFFIX_ONLY.match(_m):
                        continue
                    _bad.append("%s→%s" % (_rel, _m))
            check(not _bad, "出现未泛化企业名：%s" % "；".join(_bad[:5]))

            # (3) 真值表反向覆盖：config.yaml（已 gitignore）里的真实主体名
            #     一个都不该出现在被跟踪文件里。CI 上无此文件 → 自动跳过。
            _cfg = os.path.join(_HERE, "config.yaml")
            if not os.path.exists(_cfg):
                print("       （无本地 config.yaml，跳过真值表检查）")
            else:
                _ent = {}
                try:
                    import yaml
                    _ent = (yaml.safe_load(io.open(_cfg, encoding="utf-8").read())
                            or {}).get("entities") or {}
                except Exception as _e:
                    print("       config.yaml 解析失败，跳过：%s" % _e)
                _ok = set(_ent.get("generic_ok") or [])
                _needles = set()
                _stack = [_v for _k, _v in _ent.items() if _k != "generic_ok"]
                while _stack:
                    _x = _stack.pop()
                    if isinstance(_x, dict):
                        _stack.extend(_x.keys())
                        _stack.extend(_x.values())
                    elif isinstance(_x, list):
                        _stack.extend(_x)
                    else:
                        _s = str(_x).strip()
                        # generic_ok 是「通用描述词」白名单：允许出现在文档里
                        if len(_s) >= 2 and _s not in _ok and _s[0] not in "①②③":
                            _needles.add(_s)
                if _ent:
                    check(len(_needles) >= 20,
                          "真值表只解析出 %d 条 needle，entities 结构可能已变" % len(_needles))
                    _hit = sorted(_rel for _rel, _t in _corpus.items()
                                  if any(_n in _t for _n in _needles))
                    check(not _hit, "被跟踪文件里出现真实主体名：%s" % "；".join(_hit[:5]))
                    print("       真值表 %d 条 × %d 文件 → %d 命中"
                          % (len(_needles), len(_corpus), len(_hit)))

            print("[10] 开局体检 doctor")
            f_empty = _doc.check_inventory({})
            check(len(f_empty) == 1 and f_empty[0].level == _doc.BLOCK,
                  "未配置库存源应报 BLOCK（缺料推算是核心能力）")
            check(f_empty[0].fix and "inventory.source" in f_empty[0].fix,
                  "库存源的修复建议应给出明确配置项")
            # 每条体检结论都必须给出「怎么办」，否则等于没说
            allf = _doc.run(ad, {})
            check(allf, "体检对演示库应至少给出若干结论")
            for _f in allf:
                check(bool(_f.fix), "体检项「%s」没有给出下一步操作" % _f.title)
            check(_doc.has_blocker(allf), "库存源未配时应判定为存在致命问题")
            check("体检" in _doc.render(allf), "体检报告渲染异常")
            # 有数据 + 有效库存源时，不该再报「数据库是空的」
            ok_f = _doc.run(ad, {"inventory": {"source": "file", "file": {"path": "stock.xlsx"}}})
            check(not any("空的" in x.title for x in ok_f),
                  "演示库有数据，却仍报「数据库是空的」")

        finally:
            F.get_adapter = real
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if _FAIL:
        print("FAILED %d / %d" % (len(_FAIL), len(_FAIL) + _PASS[0]))
        for f in _FAIL:
            print("  · " + f)
        return 1
    print("ALL %d CHECKS PASSED" % _PASS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
