# 检查边界
使用标准库unittest，基础用例验证活动上下文门禁及Python/UTF16跨度转换。
后续还会检查Qt窗口可构造/关闭。它们不能替代FR全功能或V01—V16产品验收。

## D 输出模块检查

D 的命名/引用转换、发布/ZIP/清单/候选提取，以及三个输出服务的 D 侧流程均有
`test_d_*.py` 检查。服务用例显式注入 C/A/B 替身，不能作为真实 SQLite、解析或 Qt 联调结果。

- [总进度与未完成项](../docs/development/d-progress.md)
- [接口草案、调用与合成示例](../docs/development/d-output-interfaces.md)
- [公共故障索引](../docs/development/d-fault-index.md)
- [需求/产品验收矩阵](../docs/development/d-validation-matrix.md)

实际执行结果、跳过原因和完整产品未执行项在总进度记录，不根据测试数推算业务完成率。
