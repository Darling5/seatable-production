# 可信执行层 v1（2026-09-26）

> 生产助手第一期改造：把「机器自己觉得对」和「人同意」彻底分开，
> 并把整条晚间链路做成可查账、可恢复、发布有闸的执行体系。

---

## 1. 为什么改（一句话）

旧链路里有一条**绕过所有人**的写库通道：`wx/wxmatch.py apply` 把「高置信」
直接当成写入许可 —— 匹配算法一命中就改真实业务表，而且那条路径连读回
验证都没有（旧注释以为「列名写错会抛异常」，但 SeaTable 实际是
**HTTP 200 静默丢列**，见 SKILL.md §12）。

**算法自评不能代替人的决定。** 这是本期要拆掉的东西。

---

## 2. 四项改造

### 2.1 统一写库授权：高置信 ≠ 授权

新增 `application/authorization.py`，把授权从置信度里摘出来：

| 概念 | 回答的问题 | 谁说了算 |
|---|---|---|
| 置信度 | 「这条消息像不像真的」 | 匹配算法（自评） |
| **授权** | 「人同意这次写入吗」 | **人**（授权文件 / 已通过的审批） |

- 任何 production / tasks 业务表写入，必须携带一份 `WriteGrant`；
- 授权只有两个来源：
  1. `SOURCE_APPROVAL` —— `ApprovalRequest.status == "approved"`；
  2. `SOURCE_MANUAL` —— 人亲手写的授权文件（`--grant-file`）；
- 授权可限定**表范围 / 动作 / 有效期 / 使用次数**；
- `authorization.py` **刻意不接收任何置信度字段** —— 高/中/低一视同仁。

**落地位置**：

- `application/dataservice.py`：production/tasks 路由在 `apply` 模式下
  无授权 → `blocked`（旧行为是返回 `candidate`，语义上仍暗示「等 approve 就能写」）；
- `wx/wxmatch.py cmd_apply`：不带 `--grant-file` 时**只列待授权清单，一行都不写**；
  带授权文件时逐条经 `DataService` 写入（幂等 + 读回验证 + 台账）。

### 2.2 晚间链路：采集 → OCR → 核对 → 授权写入及读回 → 快照 → 风险 → 生成发布

新增 `workflows/evening_full.py`（19:00），顺序严格照业主口径：

```
wechat_collect   采集（微信消息增量）
      ↓
wxmedia_ocr      OCR / 提取（图片解密 + 识别，环境不具备时 skipped）
      ↓
wxmatch_scan     核对（消息 ↔ 业务表，只读）
      ↓
evening_write    授权写入及读回（唯一写业务表的地方；无授权则 skipped）
      ↓
seatable_snap    快照（写入后的真实状态固化）
partdb_snap      库存快照（失败不 abort：502 不该连坐整晚）
      ↓
alerts / foresee / foresee_review   风险
      ↓
cockpit          生成驾驶舱
      ↓
publish          发布（带门禁；无 token 则 skipped）
```

**09:00 轻同步保持原样**（`workflows/daily_refresh.py` 未改动）：
只补增量、不 OCR、不写业务表、不发布。

### 2.3 运行账本 + 发布门禁

- **账本**：`data/runs/<run_id>/final.json`（每步 status / counts / artifacts）。
  OCR、核对、发布都在 DAG 内自动记账；AI 语义步骤（群聊总结、事件登记）
  跑在 DAG 之外，用 `workflows/workflow.py note` 补记 —— 「跑了没有」不能只留在 AI 的记忆里。
- **门禁**：`application/gates.py`。一条原则：**账本里没证明过的，不许对外发布。**

  发布前判定三件事：
  1. 整次运行不允许有 `failed` 步骤；
  2. 关键阶段（`wechat_collect` / `wxmedia_ocr` / `wxmatch_scan` / `evening_write`）
     不能 `failed` 或 `blocked`；
  3. 关键阶段若报 `verify_failed` → 视为关键失败
     （意味着「以为写进去了，其实没写」，看板与真实数据已脱节）。

  `skipped` **不算失败**：没微信消息、OCR 环境缺失、当晚没给授权，
  都是正常状态，不该连坐发布。

### 2.4 离线假数据验收

`tests/test_trusted_execution.py`（25 项），全程内存假数据，
**不连任何真实业务表 / 微信库 / SeaTable**：

- 授权：无授权 blocked、授权范围/过期/次数、未通过的审批不能转授权；
- **去重**：同一幂等键重复写 → `skipped_reuse`，不产生第二行；
- **恢复**：`--resume` 跳过已成功步骤、失败步骤重跑、abort 阻断下游；
- **读回失败**：假适配器静默丢列 → `verify_failed` → 账本 → 门禁拒绝发布（端到端）。

---

## 3. 授权文件怎么写

放在 `data/approvals/<grant_id>.json`（该目录已被 `.gitignore` 忽略）。
`workflows/evening_write.py` 只在**存在有效授权**时才执行写入。

```json
{
  "grant_id": "GRT-20260926-a1b2c3",
  "tables": ["IC采购记录", "组装料采购记录"],
  "actions": ["update"],
  "actor": "老板",
  "reason": "9/26 群消息核对无误，同意回填到货状态",
  "issued_at": "2026-09-26T19:05:00",
  "expires_at": "2026-09-27T00:00:00",
  "max_uses": 5
}
```

- `tables` / `actions` 留空 = 不限（**只应在人工确认时这么写**）；
- `max_uses: 0` = 不限次；建议一次性任务写有限次数，用完即失效；
- 撤销授权 = **删掉该文件**（一步到位，无需改代码）。

---

## 4. 常用命令

```bash
# 19:00 全量（演练 / 真实执行 / 断点续跑）
python workflows/workflow.py run evening --mode preview
python workflows/workflow.py run evening --mode apply
python workflows/workflow.py run evening --mode apply --resume <run_id>

# 09:00 轻同步（未改动）
python workflows/workflow.py run daily --mode apply

# 授权写入：先看清单，再给人批，再执行
python wx/wxmatch.py apply                        # 只列待授权清单，不写
python wx/wxmatch.py apply --grant-file data/approvals/GRT-xxx.json

# 发布门禁
python workflows/workflow.py gate latest                 # 判定是否允许发布
python cockpit/publish.py --gate latest                # 门禁通过才真正上传

# 给 AI 语义步骤补记账本（AI 步骤跑在 DAG 之外）
python workflows/workflow.py note <run_id> --step ai_summary --status success \
    --detail "12 个群 / 38 条待办" --artifact data/wechat_intake/ai_summary_24h.md
```

**退出码约定 —— ⚠️ 这里有两套命名空间，层级不同，不要互相参照**
（2026-10-03 批次 0b 补「层级」划分；起因见 `merge-plan.md` E10 / Q9）

**① 步骤脚本级** —— 被 `runner` 调起的脚本（`cockpit/publish.py` / `workflows/evening_write.py` 等）：

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 门禁未通过 / 有写入读回失败 —— **未上传 / 未写入任何内容** |
| 2 | 上传失败（网络/服务端），本地 HTML 仍可兜底 |
| 3 | 环境不具备（无 token / 无授权 / 非 WorkBuddy 环境）→ **skipped，不算失败** |

码 → 步骤状态的映射由 `application/runner.py::make_step` 承担：`0→ok` ｜ `3→skipped` ｜
其它非 0 → `failed`（`res.fail("exit=%s …")`）。

**② 工作流级** —— `workflows/workflow.py::cmd_run` 的返回值（唯一落地处是
`workflow.py` 末尾 `raise SystemExit(args.func(args))`）：

| 码 | 含义 | 对应 `RSTATUS` |
|---|---|---|
| 0 | 整次成功 | `success` |
| **2** | **跑完但存在非阻断失败**（降级）—— **仍可发布**，但播报须点名是哪一步 | `degraded` |
| 1 | 失败（含阻断型失败；未知状态保守按失败） | `failed` |

> ★ **字面冲突，务必知道**：`cockpit/publish.py` 在**步骤级**也用 `2`，表示「上传失败」，
> 那会落成步骤 `failed`；而工作流级的 `2` 表示「跑完但有非阻断失败、可以发布」。
> **同一个数字、两个层级、两种含义。**
>
> 另注：工作流级退出码目前**没有消费者** —— `automations/` 的两个 prompt 只发命令、
> 播报源是 `final.json`，不读退出码，所以 `1` 与 `2` 对它们都是「非 0」、行为不变。
> 这一层的语义是**给人、以及后续要接的自动化**读的。
> 相关不变量：`G12`（运行级状态单一实现）· `G9`（步骤级 `skipped` 不连坐）。

---

## 5. 行为变更（升级须知）

| 位置 | 旧行为 | 新行为 |
|---|---|---|
| `wx/wxmatch.py apply` | 高置信项**自动写库** | 无 `--grant-file` → **只列清单**；有授权才写 |
| `wx/wxmatch.py apply` 回填状态 | `已自动写入` | `已授权写入` |
| `DataService.write(production/tasks, apply)` | 返回 `candidate` | 无授权 → **`blocked`** |
| `cockpit/publish.py` 缺 token / 缺 lib | 退出码 2 | 退出码 **3（skipped）** |
| `StepSpec.allowed_in` | 不拦 `publish` | preview 模式拦 `publish` |
| `make_step(cmd)` | 不支持占位符 | 支持 `{run_id}` / `{skill_dir}` |

**历史数据兼容**：`data/核对结果.csv` 里既有的「已自动写入」行不受影响；
新链路只处理「待确认」行。`write_ledger.csv` 表结构未变，授权编号记在
「备注」列前缀（`[GRT-xxxxxx]`），审计时可 grep。

---

## 6. 验收

```bash
cd tests && python -m unittest discover -p "test_*.py"
```

- 可信执行层专项：`test_trusted_execution.py`（25 项）
- 全量：158 项，全绿（2026-09-26）

真实环境首次上线建议：先 `--mode preview` 演练一遍 evening，
确认每步 status 与预期一致，再跑 `--mode apply`。
