---
name: partdb-price-import
description: 把采购合同/报价单 PDF（型号+含税单价）批量匹配并录入 PartDB 的价格/供应商记录。覆盖 PDF 文本提取、PartDB 全量零件拉取与结构化匹配、Hydra API 写价格（含派生字段与 PATCH 坑）。当用户"丢一个采购合同 PDF 并说 更新 / 录入物料 / 导入价格 / 把型号价格录到 PartDB"时触发——自动 analyze 生成审核报告，用户确认后 apply 写入。
---

# PartDB 采购价格导入（自动执行版）

## 适用场景
- 用户给出采购合同/报价单 PDF，要求把型号与单价录入 PartDB。
- 工作流：**丢 PDF → 说"更新/录入物料" → 自动分析生成审核报告 → 用户确认 → 写入**。

## 自然语言触发（agent 行为）
当用户在对话中**附带一个 PDF** 并说以下任一口吻时，按本流程走：
- "更新" / "录入物料" / "导入价格" / "把合同价格录进去"
- 不要等用户给详细指令，直接跑 analyze，再把报告呈现给用户审核。

## 标准两步闭环

### 第 1 步：analyze（只读，绝不写）
```bash
PY="C:/Users/11430/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
"$PY" "<skill>/scripts/import_prices.py" analyze --pdf <PDF路径或目录> [--out <输出目录>]
```
- 自动提取 PDF 表格（序号/型号/品牌/数量/含税单价/金额），**过滤非元件行**（同城费用、纯数字串、页脚等）。
- 全量拉取 PartDB 零件（分页直到空，API 固定每页 30 且 `limit` 被忽略、`totalItems` 不可信）。
- 本地结构化匹配：IC 精确名 / 无源器件按值+封装+介质+电压 / LED 按颜色 / 开关按 KEY 关键词。
- 输出 `partdb_import_report.json` + `partdb_import_report.md`。
- 每条处置分类：
  - `SKIP` 之安传感价已等于合同价
  - `UPDATE` 之安传感有 MOQ=1 档且不同 → 就地更新
  - `ADD_TIER` 之安传感有记录但无 MOQ=1 档 → 新增一档（保留历史）
  - `NEW_ORDER` 该料号从无之安传感记录 → 新建采购记录
  - `NEW_PART` PartDB 无此型号 → 新建料 + 之安传感价
  - `CONFLICT` 多候选/规格歧义 → **交用户定**（报告给出默认建议）
- 把报告路径与"共 N 条，其中 X 条需你确认"告诉用户，**请用户审核/确认**。

### 第 2 步：apply（写，必须用户确认）
- 若报告里有 `CONFLICT` 项，用户在 `report.json` 中为每个加 `"chosen_part_id": <id>`（或留空/`"skip"` 跳过）。
- 用户说"确认/全部按默认/录入"后，再执行：
```bash
"$PY" "<skill>/scripts/import_prices.py" apply --report <report.json> --yes
```
- 不加 `--yes` 只打印确认提示、**不写任何数据**（安全闸门）。
- 逐条执行并回显结果（成功/跳过/失败）。`NEW_PART` 会自动解析分类/封装/制造商，并把 `ipn` 设为 `P`+id 零填充 4 位（本项目约定）。

## 前置：凭据
读取 `~/.qclaw/seatable-cache/config.env` 的 `PARTDB_URL`（已含 `/api`）、`PARTDB_TOKEN`。
⚠️ 真实 token 禁止写进任何 skill 文件或公开仓库，只放用户本地 config.env。

## 关键技术坑（已在脚本内处理，勿在别处重写）
- **分页**：`GET /parts` 无视 `limit`，固定每页 30；必须按 page 翻到 member 为空，不能信 `totalItems`。
- **列表响应未展开 orderdetails**：`/parts` 列表里的 `orderdetails` 只是 IRI 引用，价格动作分类必须 `GET /parts/{id}` 拿详情。
- **派生字段**：`price_per_unit = price / price_related_quantity` 是只读派生。改单价必须同时 PATCH `price` 与 `price_related_quantity=1`。
- **PATCH 内容类型**：必须用 `application/merge-patch+json`（用 ld+json 会 415）。
- **MOQ 唯一约束**：同一 orderdetail 下 `min_discount_quantity=1` 只能有一条；已有则就地 PATCH，不能新增 MOQ=1 档（否则旧低价仍压过新档）。
- **C0G = NPO / COG**：视为同类介质。
- **子串误匹配**：`10K`⊂`510K`、`0R`⊂`180R`、`10KNTC` 热敏电阻会被值匹配误抓 → 已排除。
- **PartDB 料号 name 常不含公差**（如 `10K`/`100K`），数据上分不出 5%/1% → 这类一律判 `CONFLICT` 交用户定，评分仅排默认建议，绝不瞎猜。

## 参考实体（本项目 PartDB，运行时动态解析，勿硬编码）
脚本会按需 `GET /categories|/footprints|/manufacturers` 动态解析；品牌映射：国巨→YAGEO、村田→muRata、风华→Fenghua、顺络→SunLord、三星→Samsung 等。货币 ¥ = id 3。

## 依赖
managed Python venv 已装 `pypdf`：
```bash
PY="C:/Users/11430/.workbuddy/binaries/python/versions/3.13.12/python.exe"
"$PY" -m venv "C:/Users/11430/.workbuddy/binaries/python/envs/default"
"$PY" -m pip install --quiet pypdf
```
