# SeaTable 云端「生产」库接入要点（cloud.seatable.cn）

`seatable_sync.py` 把云端真实业务表拉到本地 `data/*.csv`，供 `cockpit.py` 直接渲染。
下面是踩坑后确认的 API 调用方式（官方 `seatable-api-python` 源码核对）。

## 凭证（来自用户 config-example.env，存于本技能 config.yaml 的 [seatable] 段）
- `api_token`：`Bearer` 鉴权用
- `base_uuid`：库 UUID
- `server`：`https://cloud.seatable.cn`

## 调用顺序与路径
1. **取网关令牌**
   `GET {server}/api/v2.1/dtable/app-access-token/`，头 `Authorization: Bearer <api_token>`
   返回 `access_token`、`dtable_uuid`、`dtable_server`（形如 `https://cloud.seatable.cn/api-gateway/`）、`use_api_gateway=True`。
2. **后续所有 dtable 操作走网关**，鉴权 `Authorization: Bearer <access_token>`：
   - 元数据：`GET {dtable_server}/api/v2/dtables/{uuid}/metadata/` → `md["metadata"]["tables"]`
   - 行：`GET {dtable_server}/api/v2/dtables/{uuid}/rows/?table_name=<名称>&limit=1000&offset=0`（按 1000 分页）
   - 列/选项：`GET {dtable_server}/api/v2/dtables/{uuid}/columns/?table_name=<名称>`

> ⚠ 常见错误：云端库**不是** `/api/v1/dtables/...`（404 page not found），也**不是** `{server}/api/v2.1/dtable/rows/`（404 前端页）。必须用网关的 `/api/v2/dtables/{uuid}/...`。

## 行数据结构坑（极易写错）
- 行数据**按列内部 key 索引**（如 `0000`、`VO4i`），不是中文显示名。
  用 metadata 每张表的 `columns: [{key, name, type}]` 做 `key → name` 映射后再落库。
- **单选/多选列**：行值存的是选项 **id**（如 `58668`），不是显示名。
  选项的 `id → name` 映射在「列定义」接口里：`columns[].data.options = [{id, name, color}]`。
  `/metadata/` 与 `/columns/` 顶层 `options` 字段都是 `None`，必须读 `data.options`。
- **链接列**：值是 `[{row_id, display_value}, ...]`，`display_value` 即被链接行的展示字段（如项目名），可直接用。
- **日期列**：值是 ISO 字符串 `2026-03-31T00:00:00+08:00`，落库时取 `[:10]`。
- `交期（天）` 在云端叫这名，cockpit 读 `交期` → 同步脚本里做了别名映射 `交期（天）→交期`。

## 状态词差异（真实库 vs demo）
- 真实库项目/计划状态用：`计划中 / 已交付 / 已超期 / 可能延迟 / 待客户下单`
- demo 用：`计划中 / 进行中 / 已完成`
- `cockpit.py` 用 `STATUS_DONE={已完成,已交付}`、`STATUS_ACTIVE={进行中,可能延迟,已超期,待客户下单}` 归一化。
- `完货日期` 在真实库普遍为空 → 交期达成率算不出，显示 `—`（N/A）而非 0%。

## 重跑
```
python seatable_sync.py        # 全量同步（写 data/*.csv + data/_sync_meta.json）
python cockpit.py              # 重生成 项目管理驾驶舱.html
```
cockpit 检测到 `data/_sync_meta.json` 即把标识切到「真实数据 · SeaTable云」。
