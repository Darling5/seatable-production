---
name: partdb-part-create
description: 在 PartDB 新建物料（元件/PCB/半成品/成品）并同时建立库存批次（PartLot）。当用户说"partdb新建XX""录入一个新料/半成品/成品""录入组装料/XX料""XX数量N存在X位置，盘点日期X"时使用。覆盖搜重、分类/库位解析、建料、ipn 约定、批次入库与确认摘要。
agent_created: true
---

# PartDB 新建物料 + 批次入库

## 铁律
1. **先搜重再新建**：全量扫描 parts 比对名称/描述，展示候选，用户确认"不是已有料"后才建。
2. **先展示完整数据摘要，等用户明确确认后再写**（生产协同项目规则）。
3. 任何写操作失败要如实回显，不要假装成功。

## 凭据
读 `~/.qclaw/seatable-cache/config.env`：`PARTDB_URL`（已含 `/api`）、`PARTDB_TOKEN`。
认证头：`Authorization: Token <token>`。真实 token 禁止写进任何技能文件。

## 关键 API 事实（已验证，勿重写成别的形式）
| 事项 | 结论 |
|---|---|
| 列表响应 | `/parts?page=N` 直接返回 **list**（不是 hydra:member），固定每页 30，需翻到空为止 |
| 精确搜索 | 用 `?name=<关键词>`（与 Web 端一致）；`?search=` 全局模糊不准 |
| 库存准确值 | 必须逐个 `GET /parts/{id}`，列表里的 `total_instock` 不可信 |
| 建料 | `POST /parts`，body 用 IRI 字符串引用关联实体：`{"category": "/api/categories/38"}` |
| **PATCH 内容类型** | 必须 `application/merge-patch+json`，用 `application/json` 会 **415** |
| 建批次 | `POST /part_lots`，body `{part, amount, storage_location, description}` |
| 分类/库位列表 | `GET /categories?page=N`、`GET /storage_locations?page=N` |

## 本项目约定
- **ipn** = `P` + 4 位零填充 part id（如 id=408 → `P0408`）。建料返回 id 后立即 PATCH 写入（用 merge-patch）。
- **PartLot.description = 盘点日期**，格式 `MDD`（9月2日 → `902`）或 `MMDD`（11月1日 → `1101`）。
  **有数字 = 已盘点可用；空 = 未确认，不可用于生产计划。**
- 常用分类 id：成品=33，**半成品=38**（会变，运行时用 `/categories` 按名解析，勿硬编码）。
- 常用库位 id：小卡纸箱=39、T1号纸箱=9、12号箱=38（同样运行时解析）。
- `supplierpartnr` 不能留空（建采购记录时会 422）；**若用户给了价格/供应商，则必须走「采购信息」模块建 orderdetail + pricedetail，不能只在 description 里写价格**。

## ⚠️ 与 SeaTable「组装料采购记录」的消歧（2026-09-03 踩过）
用户说「**录入组装料：XXX，价格/链接**」且**没给数量、库位、盘点日期**时，指的是**在 PartDB 建档**，
不是往 SeaTable「组装料采购记录」表写一行。判别依据：
- 给了**淘宝/采购链接 + 单价** → 建档（PartDB），且必须同时**建采购信息记录（orderdetail + pricedetail）**，价格/供应商/供应商型号写入采购信息模块；description 里保留型号、采购链接等不可结构化字段，避免「单价88元」这种可结构化数字只躺在描述里。
- 给了**数量 + 花销 + 要关联生产计划** → 采购记录（SeaTable）。
拿不准就一句话问清，别默认 SeaTable（本技能与 seatable-production 的表名高度重叠）。
淘宝/1688 商品页**需登录，WebFetch 抓不到标题**，物料名称/型号必须让用户补。

## 标准流程
1. 抽取字段：name / description / category / tags / amount / storage_location / 盘点日期；若用户给了价格/供应商/供应商型号/采购链接，还要抽取：**supplier、supplierpartnr、price_per_unit、min_discount_quantity（默认1）**。缺失项在摘要里标「待补」，不瞎猜。
2. 搜重：分页拉全量 parts（约 370 条 / 13 页），按关键词过滤 name+description，展示近似的已有料（含 ipn、分类、库存、库位）。
3. 解析 category id 与 storage_location id（按名称精确匹配）。
4. 输出确认摘要表格 + 执行步骤，**等用户说"确认"**。
5. 执行：
   - `POST /parts` → 拿 id
   - `PATCH /parts/{id}` 写 ipn（**merge-patch**）
   - 若用户给了价格/供应商：
     - `POST /orderdetails {"part": "/api/parts/{id}", "supplier": "/api/suppliers/{sid}", "supplierpartnr": "..."}`
     - `POST /pricedetails {"orderdetail": "/api/orderdetails/{odid}", "price": ..., "price_per_unit": ..., "min_discount_quantity": 1, "price_related_quantity": 1}`
     - 关联实体 IRI 必须带 `/api/` 前缀，否则报 `Invalid IRI`。
   - `POST /part_lots` 建批次
6. 回读 `GET /parts/{id}` 校验，回显 ipn/分类/库存/批次明细。

## 脚本
`scripts/create_part.py` —— 支持 `--dry`（默认，只打印）/`--apply`（真写）。
```bash
PY="C:/Users/11430/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
"$PY" scripts/create_part.py --name "4G小卡BL-PCBA" --desc "DEMO_4G_V4.0蓝牙基础版本（示例）" \
  --category 半成品 --tags "4G,小卡,蓝牙" --amount 192 --location 小卡纸箱 --date 902 --apply
```
无依赖，标准库 urllib 即可。

## 改库存数量（用户说"Pxxxx 数量改为 N"）
1. `GET /parts/{id}` 回读所有 PartLot，展示 lot_id / amount / 库位 / 盘点日期，**让用户确认改哪一条**。
2. `PATCH /part_lots/{lot_id} {amount: N}`，**必须 merge-patch+json**。
3. 回读 `GET /parts/{id}` 校验 `total_instock`。
- 数量跳变超过 3 倍时，执行前用一句话提示用户复核，别默默改。
- 名称相近的物料（如 P0374 4G小卡PCB主板裸板 vs P0408 4G小卡BL-PCBA 半成品）必须先回读 lot 明细确认对象，改错代价高。
- 不要动其它 lot（如 0 @ 贴片厂的占位批次）。

## 新建项目 + 批量导入 BOM（用户丢 BOM Excel 说"建项目"）
1. 读 Excel，按「ID」列（内部料号）、「数量」列（单套用量）、「位号」列（写 mountnames）解析；核对所有 IPN 在 PartDB 存在。
2. 双 ID 行（如 `P0143/P0166`）必须列候选（ipn/名称/规格/库存）让用户选，默认可建议高库存者。
3. 搜重项目（`GET /projects` 按 full_path），确认无同名后建：`POST /projects {name, parent, description, status}`。
4. **BOM 写入端点是 `POST /project_bom_entries`**（不是 `/projects/{id}/bom`，那个是 GET 专用，POST 返 405）；
   body：`{"project":"/api/projects/{pid}", "part":"/api/parts/{id}", "quantity": N, "mountnames": "C4,C5"}`。
5. 回读 `/projects/{pid}/bom?page=N` 校验条数、数量、无重复。
- 常用父项目 id：小卡=2、小卡→4G=4、大卡→4G=17（运行时解析）。

## 易错点
- 半成品/成品**不要**填 footprint、manufacturer、supplier——P0404 蓝牙信标PCBA主板（半成品样板）这些字段全空。
- 同一物料若已有 PartLot，新增批次直接 POST /part_lots 即可，不要去 PATCH 覆盖旧批次。
- **orderdetail / pricedetail 里的 part、supplier、orderdetail 引用必须是完整 IRI 如 `/api/parts/409`，不能写 `/parts/409`（会报 Invalid IRI）。**
- 电子元件新建才需要追问封装/厂商；半成品/成品不需要。
