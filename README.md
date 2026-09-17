# 修复资助受限资金核销

面向修复项目的受限资金管理服务：把资助条款、预算版本、采购承诺、到货验收、材料实际用量和费用分摊连接起来，使财务能在付款前判断每一笔支出的合规来源。账务变化采用可追溯凭证，跨币种金额引用业务日汇率，结账后的调整通过反向凭证表达。

## 目录

- `domain/contract.json` — 账务对象与业务不变量契约
- `service/` — 受限资金管理服务
  - `models.py` 领域对象（资助/条款/预算版本/承诺/验收/用量/发票/分摊/退料/付款）
  - `restrictions.py` 资助限制表达求值，输出条款依据
  - `fx.py` 汇率表，按业务日取价并锁定
  - `ledger.py` 不可变凭证账，已结账期间拒绝直接入账
  - `core.py` 服务入口 `RestrictedFundService`
  - `reporting.py` 月底对齐报告
- `examples/` — 资助限制表达（`funds.json`）、汇率表（`fx_rates.json`）、器物（`artifacts.json`）、发票（`invoices.json`）、用量（`usage.json`）、单张发票分摊（`allocations.json`）
- `tools/validate_contract.py` — 核对契约与分摊样例的金额、币种和分摊总额
- `tools/run_scenario.py` — 端到端演示：抢救改用进口加固剂后的合规核销
- `tests/test_service.py` — 业务规则测试（标准库 unittest，无第三方依赖）

## 业务规则

- **付款前合规判断**：`request_payment` 在付款前核对资助条款（材料来源、器物类别、支出类别、有效期），拒付逐笔留痕并附条款依据。
- **预算调整只影响未承诺额度**：`adjust_budget` 要求新总额不低于已承诺金额（含紧急采购待审与逾期未批部分），每次调整形成新的预算版本。
- **退料沿原分摊比例冲回**：`return_material` 按原分摊金额比例拆分，每行挂回原分摊与资助来源，累计退料不得超过已分摊总额，不生成无来源余额。
- **跨币种按业务日锁定汇率**：承诺、分摊、付款在各自业务日取价并固化，汇率表后续变化不影响已锁定金额。
- **紧急采购待审额度**：紧急承诺创建即占用额度；`sweep_emergency_deadlines`（付款时自动触发）将逾期未批承诺转为过期并冻结该资助后续付款，补办审批或驳回后解冻。
- **发票并发核销不超含税总额**：每张发票一把锁，剩余额度检查与写入在同一临界区完成；分摊 `allocation_id` 幂等去重。
- **结账后更正形成反向凭证**：`close_period` 后该期间禁止直接入账，`correct_voucher` 生成借贷对调的反向凭证，原凭证保留不动。
- **月底对齐查询**：`build_month_end_report` 输出各资助的预算、已承诺、已消耗、账面余额、可用额度与器物实际消耗（金额+实物量），校验账簿费用科目与分摊净额一致，并列出本期被拒付款及条款依据。

## 运行

```bash
python3 tools/validate_contract.py     # 契约与分摊样例校验
python3 tools/run_scenario.py          # 端到端演示（含月底对齐报告）
python3 -m unittest discover -s tests  # 业务规则测试
```
