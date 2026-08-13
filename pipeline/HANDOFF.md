# 无缝衔接口令（Resume Brief）— seatable-production 采购流水线

> 新会话里直接把本文件内容（或下面"粘贴口令"段）丢给 AI，即可无缝续上。

## 这是什么
把"采购合同 PDF + 标准 BOM"自动转成"按供应商分组、可直接发给供应商的采购订单 PDF"的流水线。
已写入 `seatable-production` 技能的 `pipeline/` 模块，目标：**合同进 → 人工库存审核 → 采购订单出**。

## 当前进度（2026-08-13）
- 已 commit 到 `main`，commit `f4ab99a`，工作区干净。
- 已 push 到 GitHub：`https://github.com/Darling5/seatable-production.git`
- 本地路径：`C:\Users\11430\.workbuddy\skills\seatable-production`
- 流水线代码：`pipeline/`（`core / prepare / inventory / plan / po_pdf / preflight / run.py` + `rules.yaml`）
- venv：`.venv-pipeline/`，依赖 requests/yaml/pypdf/reportlab/openpyxl

## 五步链路（已离线 + 真实只读验证跑通）
1. **prepare** — 解析合同数量 + 多版本 BOM 按套数扩量合并 → 合并备料表
2. **audit** — 拉 PartDB 真实库存，区分可用/未确认，算缺口 → 人工库存审核表 CSV
3. **（你审）** — 编辑 `pipeline/out/<run>/库存审核表.csv` 的"审核决定 / 采购数量 / 单价 / 供应商"
4. **plan** — 读审核 CSV，按供应商分组 → 采购预览（不落地）
5. **pdf** — 出按供应商的中文采购订单 PDF（含抬头/单号/明细/金额大写/签章）

## 在新会话里继续（先 `Skill: seatable-production`）
```bash
cd C:/Users/11430/.workbuddy/skills/seatable-production
PY=.venv-pipeline/Scripts/python.exe
$PY pipeline/run.py preflight                                   # 体检：缺什么配置/数据
$PY pipeline/run.py prepare <run> --contract 合同.pdf --bom 无GPS版=a.csv --bom 无UWB版=b.csv
$PY pipeline/run.py audit <run>                                 # 真实 PartDB 库存核对
# 人工审 pipeline/out/<run>/库存审核表.csv
$PY pipeline/run.py plan <run>                                  # 按供应商分组预览
$PY pipeline/run.py pdf <run>                                   # 出采购订单 PDF
```

## 还剩 2 个"全自动"堵点（需补配置，填完即真·一键）
- `pipeline/rules.yaml` 里**标准 BOM 路径**（无GPS版 / 无UWB版）还是空的 → 合同无法自动选 BOM。
- `rules.yaml` 里**采购方公司抬头 / 地址 / 付款条款**是占位值 → 出的单不能真发。
- 解法：`run.py preflight` 会明确告诉你缺哪项；把这两项填上即可"只丢合同就出正式单"。

## 关键约定（别重踩坑）
- SeaTable 写入用**中文列名**（不用 GET 返回的 `AK7C` 这类 key）；更新用 `updates` 数组；关联走 `PUT /links/` 双向各一次；状态默认"未下单"。
- PartDB 批次"已确认" = description 是纯数字日期（如 `603` / `512`），**不是** MDD/MMDD。
- 真实配置 `config*.yaml` 已被 gitignore，只提交 `.example`。
- 合同数量解析为"能确定才自动，不能确定就停在可编辑工件"，避免误识别放大到采购。

## 粘贴口令（复制到新窗口）
> 继续 seatable-production 采购流水线。本地代码在 `C:\Users\11430\.workbuddy\skills\seatable-production\pipeline\`，已 commit 到 main（f4ab99a）并 push 到 GitHub。先读 `pipeline/HANDOFF.md` 和 `pipeline/run.py` 了解现状。当前剩两个堵点：① `pipeline/rules.yaml` 未配置标准 BOM 路径（无GPS版/无UWB版）；② 公司抬头是占位值。请先跑 `python pipeline/run.py preflight` 看缺什么，再帮我补齐这两项，目标是做到"只丢合同就出可发供应商的正式采购单"。
