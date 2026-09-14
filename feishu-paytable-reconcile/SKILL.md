---
name: feishu-paytable-reconcile
description: 把 SeaTable 生产库的采购记录与飞书「付款申请单」多维表格做对账，并把结论回写飞书（备注/状态修正/待付单）。当用户要「采购付款对账」「检查哪些合同没付款/付了多少」「同供应商金额不符的备注一下」「付款单状态改成已付」「把已付50%预付的尾款列出来」时使用。覆盖：记录导出、供应商名归一化、精确/模糊匹配、A/B/C/D 四类处置、lark-cli 批量读写删、以及「导出快照滞后」这一必踩坑。
version: 1.0.0
agent_created: true
---

# 飞书付款表 × SeaTable 采购记录 对账回写

## 适用场景
采购侧数据在 **SeaTable 生产库**（7 张采购表），付款侧在 **飞书多维表格「💰付款申请单」**（智环 / 振道 两张表）。用户要对齐二者，并把结论回写飞书。

## 关键 ID（本项目已知，换项目需重查）
| 对象 | 值 |
|---|---|
| 飞书 base_token | `LDopb0RNsanQBfsLgXkcvT3mnjg` |
| 智环-付款申请单 | `tbl5yIsBhoLFRqQa` |
| 振道-付款申请单 | `tblZY68Q3FQScd5U` |
| SeaTable 生产库 | UUID `973530ad-ab16-42f5-ae33-b6ce477d7c87` |
| SeaTable 适配器 | `~/.workbuddy/skills/seatable-production-1.8.0/adapters/seatable.py` |
| lark-cli | `C:\Users\11430\.workbuddy\binaries\node\cli-connector-packages\lark-cli.ps1` |

飞书字段：付款事由 `fldZW0HZeQ` / 状态 `fldoAda77r` / 1付款金额 `fldx4A8OJC` / 收款方全称 `fld0fE2pCz` / 发票 `fld5z0kHaY`。
状态 select 取值：`已付、已收发票` / `已付、待收发票` / `未付、已收发票` / `未付、未收发票`。

## 环境约束（必读）
- **本机 Bash 不可用**（`dirname`/`head`/`grep` 全缺）。一律 `python -c` 内联脚本或 PowerShell；输出写临时文件再用 Read。
- **lark-cli 必须用 PowerShell 调 `.ps1`**，且先 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`，否则中文乱码。`.cmd` / 裸名会失败。
- Windows 上 Python 用托管版：`C:\Users\11430\.workbuddy\binaries\python\versions\3.13.12\python.exe`。

## 执行流程

### 1. 导出两侧数据
```powershell
$cli = "C:\Users\11430\.workbuddy\binaries\node\cli-connector-packages\lark-cli.ps1"
& $cli base +record-list --base-token LDopb0RNsanQBfsLgXkcvT3mnjg --table-id tbl5yIsBhoLFRqQa --format ndjson --output fx_zh.ndjson --as user
& $cli base +record-list --base-token LDopb0RNsanQBfsLgXkcvT3mnjg --table-id tblZY68Q3FQScd5U --format ndjson --output fx_zd.ndjson --as user
```
ndjson 每行一个记录，含 `record_id` 与中文字段名。

### 2. 供应商名归一化
去前缀/后缀：`深圳市/东莞市/广东省/有限公司/科技/电子/物联…`，再套 ALIAS 表（PCB板厂→华秋智联/华秋电子/牧泰莱；禾电讯→禾电迅；齐犇物联→齐犇；淘宝→[]）。
**坑**：供应商名可能带括号（如 `(PCB板厂)`），匹配前必须 `strip('()（）')`，否则别名表失效。

### 3. 匹配（分层，逐级降级）
1. **精确**：供应商 + 金额相等 → ✅ 已付款
2. **事由关键词**：必须**叠加金额近似约束** `abs(pay-amt) <= max(amt*0.05, 5)`，否则小额付款会命中大额合同
3. **50% 预付**：`amount*2 == contract` 且事由含 `预付|一半|50%`
4. **同供应商可认领**：只报**组合计**，**绝不按比例摊派**（摊派会产出 11.8 万这种假数字）
5. 其余 → A（同供应商金额不符）/ B（金额相同收款方不同）/ 🔴 无流水

### 4. 模糊匹配（B 类）
`difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()`，阈值 0.55。
**实测经验**：真实公司名对相似度普遍 0.00，48 组金额碰撞通过率 0/48 → 等额多为巧合，不是同公司两写法。

### 5. 四类处置（需用户确认后才写）
- **A**：往「付款事由」**末尾追加** `〔未匹配到采购合同｜<供应商> 名下合同 N 份/¥X，本笔金额均不符〕`
- **B**：追加 `〔金额与<供应商>合同<号> ¥X相同，但收款方不同，模糊相似度0.00，未确认对应〕`
- **C**：付款单已提交但状态未付 → 改状态。有发票→`已付、已收发票`，无发票→`已付、待收发票`
- **D**：已付 50% 预付 → 新建待付单（**建前必须做去重核查，见下**）

### 6. 写回（lark-cli）
```
改：base +record-batch-update --json @upd.json --as user
    payload {"update_records":{"<rid>":{"付款事由":"..."}}}   # 或 {"状态":["已付、待收发票"]}
增：base +record-batch-create --json @new.json --as user
    payload {"create_records":[{...}]}
删：base +record-delete --base-token B --table-id T --record-id X --record-id Y --as user --yes
    （high-risk-write，必须 --yes，且仅在用户明确确认后）
```

## ⚠️ 三大必踩坑

### 坑 1：A/B 备注互相覆盖
同一 `record_id` 既属 A 又属 B 时，若两者都写 `{'付款事由': txt}`，**后写的会覆盖先写的**（replace 语义，不 append）。
**正确做法**：先按 rid 归并所有 note 到列表 `want[rid]['notes']`，再拼成一份完整文本一次写入。

### 坑 2：导出快照滞后，导致"写入没生效"的假象
写完立刻用**旧**导出校验，会显示缺失。**验证必须重新跑 `+record-list` 导出**。
判别法：写一条 `★TEST` 探针，重新导出能读到 → 说明写入正常，之前的"缺失"是快照问题。收尾记得清掉探针。

### 坑 3：D 待付单重复建档（最危险，可能引发重复付款）
预付款识别只认事由含「预付/一半/50%」的流水。**尾款事由若写作「货款」，会被漏配**，从而误判"仅付 50%、尾款待付"。
**建 D 前必须**：拿到该合同已计入的预付款 rid（来自 `match_results.json` 的 `match.rid`）→ 排除它 → 再查同供应商是否有**第二笔等额「已付」**流水。有 → 说明尾款已付，**不要建**，或建了也要删。

## 脚本参考（本次产出，可复用）
`fix_ab.py`（A/B 落库校验 + 只补不覆盖）、`verify_all.py`（C/D 验收）、`check_dup.py` / `dup_final2.py`（D 去重终审）、`build_payload.py`（payload 生成）。
