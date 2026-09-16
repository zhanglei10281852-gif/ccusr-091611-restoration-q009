# 修复资助受限资金核销

本项目描述修复项目中的资助限制、预算承诺、发票分摊和材料实际消耗。账务变化采用可追溯凭证，跨币种金额引用业务日汇率，结账后的调整通过反向凭证表达。

`domain/contract.json` 定义账务对象，`examples/allocations.json` 给出一张发票的分摊。运行 `python tools/validate_contract.py` 可核对金额、币种和分摊总额。

