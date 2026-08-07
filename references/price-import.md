# 采购合同 PDF → PartDB 价格导入

> 适用：用户给出采购合同 / 报价单 PDF，要求把型号与含税单价录入 PartDB 的价格 / 供应商记录。
> 本文件是 `seatable-production` 的补充参考，与 `op.py` 后端无关，直接走 PartDB Hydra API。
> 完整业务流程、BOM 成本算法见本仓库 `SKILL.md` 与 `references/workflow.md`。

## 0. 前置：凭据（不入库）

读取本地配置（不要写进任何仓库文件或公开位置）：

- `PARTDB_URL`（已含 `/api`，如 `http://host:port/api`）
- `PARTDB_TOKEN`

⚠️ **真实 token 禁止进入仓库 / skill 文件 / 公开位置**，只放用户本地 `config.yaml` 或 `~/.qclaw/seatable-cache/config.env`。

## 1. 提取 PDF 文本

用项目所在环境的 Python（如 WorkBuddy managed venv）建隔离环境并装 `pypdf`，再用独立 `.py` 脚本逐页提取，避免命令行引号嵌套：

```python
import pypdf
r = pypdf.PdfReader("合同.pdf")
for i, p in enumerate(r.pages):
    print(p.extract_text() or "")
```

解析出每行的：**型号、品牌、封装、含税单价、来源合同 / 日期**。无源器件注意介质（X7R/X5R/C0G=NPO）、电压、公差、容值/阻值字段。

## 2. 全量拉取 PartDB 零件（关键坑）

```bash
GET {PARTDB_URL}/parts?limit=100&page=N
```

- ⚠️ **不能信 `hydra:totalItems`**：批量查询会提前截断，该字段给出的总数不可靠。
- 按页累加，**一直翻页到某页 `member` 为空**为止，收集全部 Part（典型 300+）。
- 每个 Part 关键字段：`id`、`ipn`、`name`、`footprint.name`、`manufacturer`、`orderdetails[].supplier.id`、`orderdetails[].pricedetails[].price_per_unit`。

## 3. 本地结构化匹配

- **IC 类**用 `name` 精确匹配最准。
- **无源器件**（电阻/电容/电感/晶振/LED）：解析合同型号里的容值 / 阻值 / 介质 / 电压 / 公差 / 封装，与 Part 逐项比对。
  - `C0G` 与 `NPO` 视为同类温度系数，可合并。
  - 排除子串误匹配：`10K`⊂`510K`、`0R`⊂`180R`、`100R`⊂`0R` 等。
- 输出四类：①精确一致 ②多候选冲突 ③规格出入 ④需新建（**先用 `name=` 精确查询确认 `total=0` 才算真不存在**）。

## 4. 写价格（Hydra API 坑很多）

价格挂在 `Part → orderdetails[] → pricedetails[].price_per_unit` 下。

- 供应商（如「华之安」）先查 `GET /suppliers?name=...` 拿 `id`。
- **该 Part 无此供应商记录** → `POST /orderdetails`：`{part, supplier, supplierpartnr:"待补"}`（supplierpartnr 不可空，否则 422）→ 再 `POST /pricedetails`：`{orderdetail, price, price_per_unit, min_discount_quantity:1, price_related_quantity:1}`。
- **该 Part 已有此供应商记录** → 直接 `POST /pricedetails` 到现有 orderdetail（新增价格档）。
- 内容类型：POST 用 `application/ld+json`；**PATCH 必须用 `application/merge-patch+json`**（用 ld+json 会 415）。
- ⚠️ **`price_per_unit` 是派生只读字段 = `price / price_related_quantity`**。要改单价必须**同时 PATCH `price` 和 `price_related_quantity=1`**，否则只改 price 单价不变。
- ⚠️ **同一 orderdetail 下 `min_discount_quantity` 唯一约束**：已有 MOQ=1 档时无法再新增 MOQ=1 档（422 "already used"）。此时只能**就地 PATCH 现有 MOQ=1 档**的价格，或换不同 MOQ（但旧低价会压过新档，合同价不生效于成本计算）。

## 5. IPN 命名约定（新建料号必读）★

本项目 PartDB 新建料号的 `ipn` 字段统一规则：

- **格式：`P` + PartDB `id` 零填充到 4 位。**
- 例：PartDB `id=400` → `ipn=P0400`；`id=4` → `P0004`；`id=239` → `P0239`。
- 与库内现有 `P0004` / `P0058` / `P0239` 等写法保持一致。
- ⚠️ 不要写成裸数字（如 `400`）或 `P0`+短 id，必须用 `P` + 4 位零填充。
- PATCH 设置 `ipn` 同样用 `application/merge-patch+json`。

## 6. 冲突与确认（铁律）

- 所有增删改**必须先展示完整清单等用户确认**（项目规则）。
- 「用日期最新价格」：同型号多份合同价不同，取**最新合同日**的价格。
- 品牌 / 封装 / 精度优先从合同字段取；重复料（完全相同的两条）提示用户选其一或都更新。
- 缺料 / 价格异常 / 多供应商比价可在展示时一并提示。

## 7. 校验

写入后逐条 `GET /parts/{id}`，确认目标 `supplier.id` 下有期望 `price_per_unit`，全部一致再汇报。

## 参考实体（本项目 PartDB，ID 需按实际查询）

> 以下 ID 为本项目实测值，不同 PartDB 实例需重新查询，**不要硬编码**。

- 分类：电阻=3 / 电感=4 / 电容=2
- 封装：0201=2 / 0402=1 / 0603=4
- 厂商：YAGEO(国巨)=2 / muRata(村田)=25 / Fenghua(风华)=32
- 货币 ¥ = id 3
