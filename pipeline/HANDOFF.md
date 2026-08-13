# 采购流水线交接说明

## 目标

公共仓库提供可移植的采购流水线：合同解析、BOM 扩量、库存审核、供应商分组与正式采购订单 PDF。客户数据永不进入受跟踪文件。

## 公共与本地边界

受跟踪：`pipeline/rules.yaml`、代码、示例配置和文档。

本地且 Git 忽略：`config.yaml`、`pipeline/rules.local.yaml`、`pipeline/customer/`、`pipeline/out/`。其中可放公司抬头、联系人、付款条款、产品别名、BOM、库存导出、流程文件和凭证。

## 首次部署

```bash
python pipeline/run.py init
# 填写 pipeline/rules.local.yaml
# 配置 config.yaml 中的 inventory.source
python pipeline/run.py preflight
```

库存源支持 `partdb`、`api`、`mcp` 与 `file`。已有金蝶、简道云、禅道或自建 ERP 时优先走 API/MCP：通用连接器负责鉴权、分页、响应路径和字段映射；文件源用于 Excel/CSV 离线兜底。所有通用库存默认未确认，不会自动抵扣生产缺口。

## 执行链路

```bash
python pipeline/run.py prepare <run_id> --contract 合同.pdf
python pipeline/run.py audit <run_id>
# 人工审核 pipeline/out/<run_id>/库存审核表.csv
python pipeline/run.py plan <run_id>
python pipeline/run.py pdf <run_id>
```

`submit` 是可选 SeaTable 同步，必须显式 `--yes`。PDF 生成会强制检查采购方完整信息和已确认单价。
