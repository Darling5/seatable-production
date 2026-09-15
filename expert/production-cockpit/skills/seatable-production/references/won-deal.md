# 赢单转化（won_deal.py）—— CRM 线索一键转生产立项

> 从 SKILL.md §11.6 下沉。CRM 线索赢单后写 production Base 三表时阅读。

### 11.6 赢单转化速查（`won_deal.py`，v2.0）

CRM 线索赢单后「一键转生产立项」：plan 只读出方案 → 人核对 → apply --yes 写入
production Base 的「客户档案表 + 合同信息表 + 项目」三表（自动查重复用、
编号顺延、写入读回验证、全程台账 `data/won_deal_ledger.csv`）。

```bash
python won_deal.py plan --customer "云南亚雄科技" --product "天然气人员定位" \
    --amount 464250 --delivery-days 90 --payment "50%,30%,20%" \
    --contact 柴义 --phone 17862926609          # 只读方案（--offline 不连云端）
python won_deal.py apply --yes ...              # 同参数执行（必须显式 --yes）
python won_deal.py ledger                       # 写入台账核对
```

付款支持百分比（合计必须等于总价）或绝对值三段；`--profile '{...}'` 补充开票/
银行等档案字段。至此「来单→跟进→赢单→立项」前半链闭环，立项后走 pipeline 采购。

