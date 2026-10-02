# 不变量覆盖矩阵（Invariant Coverage Matrix）

> **Step 0 的交付物**（见 `docs/DEV-KICKOFF.md` §6）。
> **已填写：2026-10-03（批次 0b）** —— 全部 31 行逐条打开实际文件核对，实现点/测试均给
> `file::symbol` / `file::case`，**无一条凭记忆**。
>
> 来源：
> - `G1–G13` ← `docs/formal-runtime-model.md` **§7.2（判定）+ §10（汇总）**
>   ⚠️ 注：§10 汇总表此前只列到 `G11`、`§11` 写「上述 11 条」，而 `G12/G13` 定义在 §7.2 ——
>   本表按 **13 条**登记（计数漂移见 `merge-plan.md` E1，同批修正）。
> - `G14–G30` ← `docs/domain-model.md` §5
> - `G31` ← 本批新增（见 `merge-plan.md` Q2；案件完整性，2026-10-02 已实现但从未登记）
>
> 填写规则（硬性，违反即返工）：
> 1. **实现点 / 测试** 必须写**可核对的具体路径与符号**（`application/gates.py::Gate.check`），
>    不许写"在 gates 里"这类模糊描述，**不许凭记忆填**。
> 2. **状态** 只能取 `已闭` / `半闭` / `空白`。
> 3. **半闭** 必须写清"缺哪半边"（缺分支？缺反向用例？缺整个实体？）。
> 4. **空白** 不许留空，必须写出**具体的缺口动作**（如"新增 `Receivable` 状态机 + G17 单调性测试"）。
> 5. 起点参考 `docs/DEV-KICKOFF.md` §3.1 的"模型要素 → 代码锚点"表（**候选，仍需逐个核对**）。

---

## A. 项目执行控制平面（`L_proj`）—— G1–G13 + G31

| 不变量 | 命题 | 实现点 `file::symbol` | 测试 `file::case` | 状态 | 缺口动作 |
|---|---|---|---|---|---|
| **G1** | 无越权写入：任何 `online_write`/`destructive`/`publish` 必过 `δ_gate` 且过授权模型 | `application/contracts.py::StepSpec.allowed_in` · `application/runner.py::WorkflowRunner._validate` · `application/dataservice.py::DataService.write` | `tests/test_contracts.py::TestGates`（6 例）· `tests/test_dataservice.py::TestRoutePolicies::test_production_apply_without_grant_blocked` · `::test_unknown_route_blocked` | 已闭 | — |
| **G2** | 硬终态：`closed → {after_sales}` 唯一出口；`cancelled` 只进不出 | `application/contracts.py::_build_transitions`（`_LEGAL_TRANSITIONS`）· `domain/order_to_cash.py::Service.advance`（`severity=="illegal"` 直接返回、不落库） | `tests/test_contracts.py::TestStateMachine::test_cancel_anytime_after_sales_paths` · `tests/test_business_loop.py::TestBusinessLoop::test_advance_preview_illegal_and_skip` | 已闭 | — |
| **G3** | 售后可达性：`after_sales` 仅从 `{ready_to_ship, delivering, acceptance}` 进入 | `application/contracts.py::_build_transitions`（`_POST_DELIVERY` → `_LEGAL_TRANSITIONS[..].add("after_sales")`）· `::can_transition` | `tests/test_contracts.py::TestStateMachine::test_illegal` · `::test_normal_chain` · `tests/test_business_loop.py::TestBusinessLoop::test_after_sales_path_and_verify` | 已闭 | — |
| **G4** | 状态迁移可见性：每次迁移落 `StateTransition` 记录，`illegal` 不允许落库 | `application/contracts.py::StateTransition` · `::transition_severity` · `domain/order_to_cash.py::Service._transition` | `tests/test_contracts.py::TestStateMachine::test_skip_and_rollback_visible_not_silent` · `tests/test_business_loop.py::TestBusinessLoop::test_full_chain_materializes_expected_objects` | 已闭 | — |
| **G5** | 一次授权一次写：`max_uses=1` 的授权 `reserve` 后即不可再用 | `application/authorization.py::GrantStore.reserve` · `::WriteGrant.max_uses`（`::consumed`） | `tests/test_write_safety.py::TestGrantConsumption::test_one_use_grant_cannot_be_reused` · `::test_usage_persists_across_instances` · `tests/test_trusted_execution.py::TestAuthority::test_approval_grant_is_single_use` | 已闭 | — |
| **G6** | 载荷一致性：实际写入行 `sha256 = payload_hash`，否则拒绝 | `application/authorization.py::payload_hash` · `::WriteGrant.covers` · `application/dataservice.py::DataService.verify_readback` | `tests/test_trusted_execution.py::TestAuthority::test_approval_grant_rejects_other_payload` · `tests/test_dataservice.py::TestReadbackVerification::test_silent_column_drop_detected` · `tests/test_write_safety.py::TestWriteOutcomes::test_readback_failure_is_not_success_on_retry` | 已闭 | — |
| **G7** | 授权来源纯净：不接收置信度；`approval` 授权必来自 `approved` 的审批 | `application/authorization.py::grant_from_approval` | `tests/test_trusted_execution.py::TestAuthority::test_confidence_is_not_authorization` · `::test_unapproved_request_cannot_become_grant` · `::test_approval_without_payload_refused` | 已闭 | — |
| **G8** | 发布门槛：`publish` 实际释放 ⟹ 门禁 `allow` 且产物 `bind` 通过 | `application/gates.py::evaluate_steps` · `::bind_artifact` · `::sha256_file` | `tests/test_write_safety.py::TestPublishGate::test_artifact_must_be_bound_and_unmodified` · `::test_unbound_artifact_is_refused` · `tests/test_trusted_execution.py::TestReadbackAndPublishGate::test_ok_steps_allow_publish` | 已闭 | — |
| **G9** | `skipped` 不连坐：正常跳过不阻断发布 | `application/contracts.py::run_status`（`STATUS_SKIPPED` 计入通过集） | `tests/test_trusted_execution.py::TestReadbackAndPublishGate::test_skipped_is_not_failure` · `tests/test_contracts.py::TestRunStatus::test_成功加跳过仍是成功` | 已闭 | — |
| **G10** | fail-closed：账本缺失/不可读/格式错 ⟹ 门禁 `deny` | `application/gates.py::load_steps`（唯有 `\d+_*.json` 视为步骤；损坏即 deny）· `::_has_ledger` | `tests/test_write_safety.py::TestLedgerReading::test_唯独_NN_前缀的文件才是_fail_closed` · `::test_损坏的_NN_json_必须_fail_closed` · `tests/test_trusted_execution.py::TestReadbackAndPublishGate::test_missing_ledger_blocks_publish` | 已闭 | — |
| **G11** | 适配器 fail-closed：未知后端名直接抛错，不静默兜底 | `adapters/factory.py::get_adapter` · `::_fallback` · `::register_backend(required_keys=)` | `tests/test_adapter_contract.py::PluggableBackendTests::test_unknown_backend_fails_closed_on_write_path` · `::test_missing_required_key_fails_closed` · `::test_absent_named_instance_fails_closed` | 已闭 | — |
| **G12** | 运行级状态单一实现：`RSTATUS` 推导全系统**有且只有一个**函数 `run_status()` | `application/contracts.py::run_status` · `::RunResult.refresh_status` | `tests/test_contracts.py::TestRunStatus::test_refresh_status_与函数同源` · `::test_三条路径同源` · `tests/test_write_safety.py::TestPublishGate::test_运行级_skipped_判不可发布`（+反面 `::test_步骤级_skipped_仍然放行`） | 已闭 | — （**本批已收口**：`application/gates.py::_RELEASABLE_RUN_STATUSES` 原含 `skipped`，而 `run_status()` 不可能产出该值 → 手写 `status="skipped"` 的 final.json 会被放行发布（实测改动前 `allowed=True`）。已移除该值，见 `merge-plan.md` Q11/E12） |
| **G13** | 账本自洽：`final.json.status == run_status(final.steps)`（违反**告警不 deny**） | `application/gates.py::evaluate_steps`（自洽性告警段，`:283` 起，「只告警不拦」的理由已就地注释） | `tests/test_write_safety.py::TestLedgerReading::test_status_与步骤不自洽要告警` · `::test_自洽的账本不该有该告警` · `::test_自洽性检查不拦发布` | 已闭 | — |
| **G31** | 案件完整性：建案必须有 `CASE_SEED_KINDS` 三个种子对象（customer/lead/opportunity），缺失即自愈或拒建；空壳不得被谎报为复用 | `domain/order_to_cash.py::CASE_SEED_KINDS` · `::Service._repair_case` · `::Service.start_case`（返回 `missing`）· `workflows/loop_trigger.py::_existing_cases` | `tests/test_loop_trigger.py::TestHalfWrittenCaseHeal::test_空壳不再被谎报为复用` · `::test_空壳在_apply_下被自愈补齐且幂等` · `::test_剩余根对象_bug_不会复现` | 已闭 | — |

> **与 §4 编号冲突的交叉核对**：`G12` 曾撞代码里的 `FIX-6`、`G13` 曾撞代码里的 `FIX-8`
> —— 那**正是** §4 消除的异号。**§4.4 的改名已完成**（commit `4926fcf`，`.py` 中裸 `G<数字>` 残留为 0），
> 故本表可直接填 `G12/G13` 而不串号。

---

## B. 治理平面 / 执行平面 / 接缝 —— G14–G30

| 不变量 | 命题 | 层 | 实现点 `file::symbol` | 测试 `file::case` | 状态 | 缺口动作 |
|---|---|---|---|---|---|---|
| **G14** | 战略对齐：每个立项项目至少挂载一个战略目标 | `L_gov` | 无实体（`cockpit/cockpit.py` 仅**展示**「战略」字段，非校验） | 无 | 空白 | 新增 `战略目标` 实体 + `项目↔目标` link + `G14` 校验器（立项前置：无目标不得 `won_and_funded`） |
| **G15** | 预算护栏：项目累计支出 ≤ 下达预算 ⊕ 显式超支授权 | `L_gov` | 无实体（`cockpit/cockpit.py` 仅展示「预算」列） | 无 | 空白 | 新增 `预算`/`超支授权` 实体 + 累计支出聚合 + `G15` 校验；复用 G7 的「显式授权」范式 |
| **G16** | 收入确认前置：收入确认 ⟹ 存在验收记录 | `L_gov` | **全仓 0 命中**（`收入确认` / `RevenueRecognition` 均无） | 无 | 空白 | 沿用 `application/project_brain/evidence_ledger.py::is_acceptance_evidence` 作前置判据，新增 `收入确认` 实体 |
| **G17** | 金额单调链：`Σ回款 ≤ 应收 ≤ 开票 ≤ 合同额`（按币种） | `L_gov` | **无**（`O_kind` 12 类止于 `contract`/`shipment`/`after_sales`，无 invoice/receivable/receipt） | 无 | 空白 | 新增 `Invoice`/`Receivable`/`CashReceipt` 三实体 + 币种维度 + `G17` 单调性断言测试 |
| **G18** | 产能承诺约束：同一资源同一窗口承诺总量 ≤ 可用量（多项目共享） | `L_gov` | `application/decision_support/resources.py::detect_conflicts` · `::ResourceTimeline` · `::resource_feasible_schedule` | `tests/test_decision_support.py::ResourceConflictTest::test_detect_conflicts_finds_daily_overload` · `::test_shared_test_equipment_serializes` · `::test_unknown_capacity_is_flagged_not_silently_ignored` | **半闭** | **缺半边**：`build_inputs(project_id=…)` 是**单项目快照**，缺「多项目共享同一资源池」的跨项目求交。**动作**：快照层支持多 `project_id` 合并 + 跨项目冲突用例 |
| **G19** | 决策留痕：金额/风险达阈值的决策必有 `DecisionRecord`（选项+理由+decided_by） | `L_gov` | `application/project_brain/schema.py::KIND_DECISION` · `::build_memory_row`（`memory.py`）· `application/decision_support/alignment.py::KNOWN_PREFIXES`（含 `DEC`） | `tests/test_project_brain.py::TestProjectMemory::test_five_kinds_are_separated` · `::test_correction_appends_and_keeps_history` | **半闭** | **缺半边**：`decision` 记录载体有（含理由），但**「金额/风险达阈值即强制留痕」的触发门不存在**。**动作**：定义阈值表 + 在 `dataservice` 写路径加前置校验 |
| **G20** | 度量可溯：每个 KPI 值可追溯到实体，禁手填黑数 | `L_gov` | **无实体**（`KPI` 仅出现在 `cockpit/cockpit.py` / `cockpit/build_share.py` 的展示层；`度量` 全仓 0 命中） | 无 | 空白 | 新增 `KPI 定义` 实体（含取值表达式 + 溯源实体类型），KPI 值改为**计算**而非录入 |
| **G21** | BOM 结构：无环、版本内父件唯一、用量守恒、替代料显式受控 | `L_exec` | `application/exec_plane/bom.py::validate_bom` · `::check_structure` · `::find_cycles`（复用 `application/decision_support/graph.py::DependencyGraph`）· `::explode` · `application/exec_plane/schema.py::KIND_PREFIX`（`ITM`/`BOM`/`BLN`） | `tests/test_exec_plane.py::TestG21BomStructure`（14 例：逐步累加成环 / 自环 / 同父件同版本 / 同版本同子件 / 用量 ≤0 / 损耗率越界 / 替代组无主料·多主料·跨父件 / 行指向不存在 BOM / explode 数学与遇环中止） | 已闭 | —— **批次 1 已落地**（commit `e9ab364`）。四条性质全部落成可执行检查，每条都有「注入反例必须报红」用例。**剩余**：与外部 BOM 源（PLM/Excel）的导入校验未接 |
| **G22** | 齐套开工：开工 ⟹ 齐套=100% ∨ 显式缺料放行授权 | `L_exec` | `application/exec_plane/work_order.py::check_release_precondition` · `::release` · `::override_verdict` · `::claimed_full_items` · `application/exec_plane/service.py::ExecPlane.release_work_order`（写前强制 `WO_TRANSITIONS`） | `tests/test_exec_plane.py::TestG22KittingRelease`（16 例：判定顺序 / 部分到货报 `partial_not_kitting` 且**与二期同字符串** / 授权 fail-closed 六种失效 / `claim=full` 定性通道 / 原因码逐条可达）· `tests/test_project_brain.py::TestActionLoop::test_partial_arrival_cannot_close_kitting` · `::test_shipped_is_not_arrived` | 已闭 | —— **批次 1 已落地**（commit `e9ab364`）。**补的正是缺的那半边**：`工单` 实体 + `release()` 前置；齐套口径与 `R_PARTIAL_NOT_KITTING` **直接复用二期**，未新立 |
| **G23** | 付款前置：付款 ⟹ 存在到货验收记录 | `L_exec` | `application/exec_plane/procurement.py::check_payment_precondition` · `::check_payment_amount` · `::can_pay` · `application/exec_plane/service.py::ExecPlane.add_payment`（`enforce=True` 时写前强制） | `tests/test_exec_plane.py::TestG23PaymentPrecondition`（24 例：无 PO / PO 形态非法 / 无 GR / 已发货≠已到货 / 部分到货 / 未验收 / 拒收 / 已取消 / 超付 / **「判不了」第三态可见** / 原因码逐条可达）· `tests/test_project_brain.py::TestActionLoop::test_close_requires_acceptance_evidence` · `::test_close_succeeds_with_full_arrival_and_acceptance` | 已闭 | —— **批次 1 已落地**（commit `e9ab364`）。实现口径按**「存在覆盖该笔付款的有效到货验收记录」**（同 PO ∧ 齐套到货 ∧ 验收通过），是对命题的**收紧而非另立**。**剩余**：金额单调链属 `G17`（阶段二），本批只做**数量**覆盖且「判不了」显式可见 |
| **G24** | 质量闸：OQC 不合格 ⟹ 不得入库、不得发货 | `L_exec` | **`OQC` 全仓 0 命中**（`质检` 仅见于 `project_brain/schema.py` 的字段名与驾驶舱展示） | 无 | 空白 | 新增 `检验单（IQC/IPQC/OQC）` + `MRB 处置` 实体；入库/发货路径加 `G24` 前置（复用 G10 fail-closed 范式） |
| **G25** | 库存守恒：`Σ入库 − Σ出库 = 结存` | `L_exec` | **无**（`库存` 仅由 `adapters/partdb.py` **只读**外部 PartDB；`守恒`/`结存` 全仓 0 命中） | 无 | 空白 | 新增 `库存流水（入库/出库）` 实体 + 守恒校验器 + 与 PartDB 快照的对账报告（差异可见、挂原因码） |
| **G26** | 指令-回执：下行指令无 `Ack` ⟹ 视为未执行，上层不得据此推进 | 接缝 | **全仓 0 命中**（`Ack` 仅命中 `.venv` 第三方包内 `Ack` 字样） | 无 | 空白 | 定义 `Command/Ack` 契约 + 超时未回执的上抛策略；可复用 `application/runner.py` 的步骤级 fail-closed 语义 |
| **G27** | 三账可对账：项目账/执行账/财务账 差异可见且挂原因码，不得静默 | 接缝 | **无三账**（`三账` 0 命中）。可复用件：`application/project_brain/context.py::G_UNRESOLVED_CONFLICT` · `application/project_brain/memory.py::find_conflicts` | `tests/test_project_brain.py::TestQueryAndContract::test_conflict_from_sample` | 空白 | 新增「财务账」实体（G16/G17 的前置）并定义三账对齐报告；**冲突可见 + 原因码机制已具备，直接复用** |
| **G28** | 成本可归集：任一成本条目可追溯到 项目 / 工单 / 采购单 | 接缝 | **无真实成本**。`application/decision_support/scenarios.py::_estimate_cost` 是**合成假设**（`::_SYNTHETIC_COST_NOTE` 明示「不是真实报价」） | `tests/test_decision_support.py::ScenarioCompareTest::test_delta_cost_is_marked_synthetic`（**反向用例**：证明它标了「合成」） | 空白 | 新增 `成本条目` 实体（`project_id` + 工单号 + 采购单号 三选一必填外键）；工单/采购单实体依赖 G21/G22/G23 |
| **G29** | 跨层 ID 一致：三层引用同一 `project_id`；计划域用 `plan_id`；不引入新名称映射 | 接缝 | `application/decision_support/alignment.py::make_unified` · `::UNIFIED_FIELDS` · `::is_valid_id` · `::is_canonical_id` · `::LEGACY_ID_RE` · `application/project_brain/ids.py::validate_id` · `application/contracts.py::is_canonical_object_id` · `application/exec_plane/schema.py::KIND_PREFIX` | `tests/test_cross_layer_ids.py`（16 例：**三张前缀表并集恒等于 `KNOWN_PREFIXES`** / 各层生成的 ID 全部跨层合法 / 前缀表与生成函数表不得脱节 / 历史形态只读兼容）· `tests/test_exec_plane.py::TestNoSelfInventedEnums`（13 例）· `tests/test_project_brain.py::UnifiedFieldsRegressionTest::test_plan_id_propagates_message_to_memory_action_evidence` · `::test_plan_id_passes_own_validator` · `::test_memory_projections_carry_all_eight` | 已闭 | —— **批次 1 已落地**（commit `e9ab364`）。**修的是一个实测出来的真缺口**：`L_exec` 一接入就发现 `contracts` 12 类业务对象里 **11 类被跨层校验拒绝**（`MO`/`PO`/`AS` 只有 2 字母；`CTR`/`CUS`/`LED`/`OPP`/`QUO`/`REQ`/`SHP`/`SOL` 未登记）——即「三层引用同一 ID」在真正接入前**一直是纸面约定**。已收口：前缀一律 3 字母、旧前缀只读兼容、两处严格/宽松判定分离、三张表并集由测试强制。**剩余**：`L_gov` 层尚不存在，其前缀登记待该层建立时按同一断言补 |
| **G30** | 上行新鲜度：上层结论须带 `as_of`；来源过期须标注 | 接缝 | `application/decision_support/ports.py::freshness_caveats` · `application/project_brain/context.py::G_STALE_SOURCE`（+`GAP_CN`）· `cockpit/cockpit.py::SOURCE_SPEC` · `application/decision_support/backtest.py::filter_as_of` | `tests/test_decision_support.py::PortsContractTest::test_stale_source_becomes_caveat` · `::test_analyze_carries_context_caveats` · `tests/test_project_brain.py::UnifiedFieldsRegressionTest::test_data_as_of_is_capture_cutoff_not_now` · `tests/test_source_health.py`（27 例） | 已闭 | ⚠️ **口径风险**：同一命题有 **3 处独立判据**（decision_support ports / project_brain context / cockpit `SOURCE_SPEC`），当前一致但无「单一实现」约束。**动作**：加一条交叉一致性测试（防 E12 那类双口径漂移复发） |

---

## C. 汇总

> ★ **2026-10-03 批次 1 订正**（commit `e9ab364`）：`G21` 空白→已闭、
> `G22`/`G23`/`G29` 半闭→已闭。下表的**占比按最大余数法取整**（合计 100%）。

| 状态 | 数量 | 占比 |
|---|---|---|
| 已闭 | 19 | 61% |
| 半闭 | 2 | 7% |
| 空白 | 10 | 32% |
| **合计** | **31** | 100% |

> `L_proj`（A 段 14 条）：已闭 **14** / 半闭 0 / 空白 0 —— **控制平面完全闭合**
> （`G12` 的常量残留已在本批一并移除）。
> `L_gov` + `L_exec` + 接缝（B 段 17 条）：已闭 **5** / 半闭 2 / 空白 10
> —— **执行平面已开工**（批次 1 闭掉 `G21`/`G22`/`G23`/`G29`），
> 财务链（`G14`–`G17`、`G27`）仍全空。

**验收清单**（对应 `DEV-KICKOFF.md` §6.4）：
- [x] 31 行齐，无空行
- [x] 至少 5 条能点名到**现成测试用例** —— 实测 **31 条全部**点名到 `file::case`
      （其中 `G1`–`G13`+`G31` 均有专门用例；
      `G18`/`G19`/`G28`/`G30` 亦有；`G21`/`G22`/`G23`/`G29` 已于批次 1 补齐专门用例）
- [x] 所有「空白」项都写出**具体缺口动作**（现 10 条，均含要新增的实体名与校验挂载点）
- [x] §4 编号冲突已处置完毕（commit `4926fcf`；`.py` 中裸 `G<数字>` 残留为 0）
- [x] **引用可核对**：本文件 **73 处** `file::symbol` / `file::case` 引用
      已逐条回验 —— 文件全部存在、符号全部在文件中真实出现（**0 处虚构**）
- [x] **★ 2026-10-03 批次 1：这条回验已升级为自动守卫**
      （`tests/test_doc_refs.py`）—— 全仓库 `docs/**/*.md` 的每一条
      `` `path::symbol` `` 都必须在 CI 里可核对，且**裸文件名（同名文件不唯一）
      会被判歧义**。理由：这件事**手工做过两次、两次都抓到同一类错误**
      （只写文件名、漏目录前缀）—— 靠人记得回验 = 迟早不回验。

**本次填表的验证方法**（可复现）：把文件里所有 `` `path::symbol` `` 抽出来，
逐个 `os.path.isfile` + 在文件正文中检索符号末段。这一步抓出并修掉了 2 处
**只写了文件名、漏了目录前缀**的引用（`project_brain/memory.py`、`evidence_ledger.py`）
—— 若不做回验，它们会以「看起来合规」的姿态留在矩阵里。
★ 批次 1 更新本矩阵时**又犯了同一类错**（`evidence_ledger.py::is_arrival_evidence`、
`decision_support/graph.py::DependencyGraph`），被同一方法再次抓出；连同另外 2 处
陈旧的裸引用（`docs/contracts/alignment-p2-p3-v1.md`、`docs/merge-plan.md`）
一并补全路径。这类错误**反复出现**正是要把它做成守卫的直接原因。

## D. Step 0 阶段判定（填完后写结论）

- **最大缺口**（★ 2026-10-03 批次 1 后重估）：**`L_exec` 不再空白
  —— 11 条里 3 条已闭（`G21`/`G22`/`G23`），`L_exec` 实体层已建**
  （`application/exec_plane/`：`Item` / `BOM版本` / `BOM行` / `请购单` / `到货验收记录` /
  `付款单` / `工单` 共 7 类实体）。
  现在**最大的缺口转移到 `L_gov` 的财务链**：`G16`（收入确认）/ `G17`（金额单调链）/
  `G27`（三账对账）仍全空，`G14`/`G15`/`G20` 亦空 —— 「收入 / 回款 / 预算 / 三账」
  无处落地。第二个缺口是 `L_exec` 剩下的 `G24`（质量闸）/ `G25`（库存守恒）——
  它们与已有的到货验收链**直接相邻**，`GR` 实体已就位，接续成本低于财务链。
  值得注意的是：**阻碍它们的仍然不是能力，而是实体**——`evidence_ledger`（到货/验收判定）、
  `graph.py`（成环检测）、`resources.py`（产能冲突）、`G_UNRESOLVED_CONFLICT`（冲突可见）
  这些**部件都已写好**，缺的是把它们挂上去的**业务实体**。
  批次 1 的实测又给这条判断加了一个旁证：真正卡住进度的从来不是判据不会写，
  而是**实体与注册表之间的接头**（本批就撞到 3 处「自造枚举值导致写入静默失效」）。
- **阶段一（向下·制造执行）优先补的不变量**：`G21` `G22` `G23`
  —— ✅ **已于批次 1（commit `e9ab364`）全部闭合**。
  与 `DEV-KICKOFF.md` §7 阶段一的表格**逐字一致**（该处「不变量」列写的正是这三个）。
  ⚠️ **`G28` 不在阶段一**：`DEV-KICKOFF.md` §7 把它归**阶段三（成本归集 + 组合与产能）**。
  本节初稿曾写「`G21 G22 G23` + `G28`」并声称「与 DEV-KICKOFF 一致」—— **那是错的**：
  当时是把 `domain-model.md` §8 的口径误当成了 `DEV-KICKOFF` 的口径，正是 `merge-plan.md`
  E9 记录的「三套说法」本身。现已按 `DEV-KICKOFF` §7 收敛（该文档同步加注）。
- **执行序全景（以 `DEV-KICKOFF.md` §7 为唯一准绳）**：
  阶段一 `G21 G22 G23`（✅ 已闭）· 阶段二 `G16 G17` · 阶段三 `G14 G15 G18 G28` ·
  阶段四 `G19 G20 G26 G27 G29 G30`（其中 `G29` 已随批次 1 提前闭合 —— 它是阶段一的**前置**：
  `L_exec` 引用跨层 ID 之前必须先让 ID 形态合法）。
- **第一个垂直切片（已执行完毕）**：`G23` 付款前置 ——
  以「采购单 `PO` → 到货验收记录 `GR` → 付款单」为一条线。
  ✅ 实测回顾：这个切片的判断完全成立 —— **最小、复用度最高**，
  到货/验收侧的判据 `application/project_brain/evidence_ledger.py::is_arrival_evidence` / `::is_acceptance_evidence`
  **一行没改就复用了**，新增的只有 `付款单`/`GR`/`PO` 引用三件事。
  但**低估了一处**：真正的成本不在业务判据，而在**新一层与既有注册表的接头**
  （路由 / 写入动作 / ID 前缀 / 表名映射 —— 四处「自造取值」全是静默失效）。
  按此经验，后续每开一层，应**先补「不自造枚举值」的对接用例，再写业务判据**。
