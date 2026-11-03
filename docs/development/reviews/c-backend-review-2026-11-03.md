# C 后端交付审查与修改清单

审查日期：2026-11-03。关联 [Issue #3](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/issues/3)。

审查对象为 `feature/c-3-storage-query` 上 herbelat 于 2026-11-02 21:59:21（Asia/Hong_Kong）提交的首批后端，提交说明为 `feat(storage): add saved-version transactions, shared parser and saved queries (refs #3)`。下文行号以本次审查版本为准，返修后以函数名定位。

**结论：请求修改，暂不合并 C 后端。** 三项阻断问题已通过定向探针复现并经独立 AI 复核。本文发布到主线仅用于共享审查意见，不代表 C 后端已合并、已批准或已完成产品验收；也不表示任何成员完成了人工核查。

## 1. 返修方式与范围

请继续在原分支 **`feature/c-3-storage-query` 追加修复提交并推送**，保留现有实现，不需要重做、另起分支或覆盖已共享历史。已有 PR 时更新原 PR；尚无 PR 时可提交 PR，也可先提供原分支返修记录供复审。

本批已有真实事务、回执、身份账本、保存版查询、共享解析和 B 存储适配，方向可继续沿用。优先处理 C-REV-01—03，再处理关联跨度问题 C-REV-04，并核对 C-REV-05 的核实语义。每项写明修改位置、实际回归结果和仍未执行的项目。

不要求本轮接管 B 的 Qt 接线、预览渲染或 D 的恢复端口适配；这些继续列为待集成。若修复涉及共同契约变更，说明调用方影响并共同核对，不单方面将内部窄端口声明为稳定公共契约。

## 2. 本次实际验证及边界

- 对本次 C 交付源码快照重新运行 `scripts/check.py`：编译、公共契约导入、依赖检查和 unittest 完成，退出码 0；**561 项发现，559 通过，2 项因 Windows 符号链接权限 1314 跳过**。跳过不计为通过。
- 查询时 C 分支 [Actions 41458788829](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/actions/runs/41458788829) 为成功。CI 成功没有覆盖下述额外探针揭示的问题。
- 定向探针使用合成数据和真实临时 SQLite 数据库；通过受控交错稳定触发竞争窗口，不依赖多线程随机碰撞。本文已写明复现步骤和结果，阅读不依赖私人目录或日志。
- 上述数字仅属于本次已审查交付。返修后的验证必须重新执行，不能继承本次或作者旧记录的通过结论。
- 本次未复跑性能基准，未执行最终 Windows 界面、离线、隐私及 V01—V16 整组产品验收。

## 3. C-REV-01：P1，恢复初始化缺少可靠目标所有权

位置：[writing.py](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/blob/feature/c-3-storage-query/src/pmkb/infrastructure/storage/writing.py#L569)，`initialize_restored_collection()`；重点为原 569—573 行和 636—646 行。

**触发与实际结果：**

1. 传入尚不存在的恢复目标，执行 `path.exists()`，得到不存在。
2. 在 `sqlite3.connect()` 执行前，让另一创建者在同一路径写入已知合成字节。
3. 继续初始化。SQLite 打开该文件，后续建表失败进入异常清理。
4. 本次探针实际返回 `StorageFailure`，但目标文件已被 `unlink`，另一创建者的原字节丢失。

这是数据保护问题：存在检查不等于独占创建，也不能证明该路径属于本次操作。即使正常初始化都成功，异常清理仍可能删除不属于本次操作的文件。

**修改建议：** 使用可证明归属的受控私有候选或可靠独占创建机制；发布和清理都要保留所有权边界。不要只在原 `exists()` 之前或之后再加一次存在检查。失败时只能清理仍能确认属于本次操作的产物，已被替换或由其他创建者取得的目标必须保留。

**回归要求：**

- 目标原已存在时拒绝初始化，原字节不变。
- 检查后另一创建者抢先创建目标时安全拒绝，另一创建者的字节不变。
- 本次候选创建后路径被替换时，不清理替换者的文件。
- 本次自有候选初始化失败时按设计收尾；正常初始化及现有回执检查继续通过。

## 4. C-REV-02：P2，保存视图跨多个事务混合快照

位置：[query/service.py](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/blob/feature/c-3-storage-query/src/pmkb/application/query/service.py#L160)，`SavedQueryService.saved_view()`，原 167—173 行。

**触发与实际结果：**

1. 在真实集合保存目标笔记和来源笔记，来源正文包含一个有效引用；此时集合版本为 3。
2. `saved_view()` 通过 `load_note()` 取得来源笔记修订 1。
3. 在后续读取前插入一次真实 SAVE：来源正文删掉该引用，笔记推进到修订 2、集合推进到版本 4。
4. 继续视图构造。实际响应仍含笔记修订 1 和一个旧引用，却携带集合版本 4；数据库实际修订 2、引用数为 0。

正文、引用分类、反向引用和集合版本分多次独立只读事务读取，因此返回值不对应一个一致的保存时刻。仅在界面丢弃旧回调不能修复响应自身的不一致。

**修改建议：** 在同一个只读事务内读取并构造完整视图，包括正文、标签、引用分类、反向引用和集合版本；也可使用等价的一致快照方案。保持连接在正确线程内使用，不为解决此问题把 SQLite 连接交给界面。

**回归要求：** 在各读取阶段之间插入真实保存、改名或删除。响应须完整对应某一个有效保存快照；不能拼接旧正文和新关系，也不能用最新集合版本标记旧数据。包括无引用、空反向引用和正常读取场景。

## 5. C-REV-03：P2，撇号破坏外链和图片的引用保护

位置：[markdown.py](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/blob/feature/c-3-storage-query/src/pmkb/application/parsing/markdown.py#L289)，`_balanced()`、`_link_end()`，重点为原 298—306 行。

**复现输入生成：**

```python
from pmkb.application.parsing import parse_markdown
from pmkb.domain.identity import format_reference_marker

collection_id = "a" * 32
marker = format_reference_marker(collection_id, "b" * 32)
examples = (
    f"[don't {marker}](https://example.com)",
    f"![don't {marker}](image.png)",
    f"[label](https://example.com/don't/{marker})",
)
for body in examples:
    document = parse_markdown(body, collection_id)
    print(len(document.references))  # 预期均为 0；本次实际均为 1
```

在真实集合中先保存目标，再保存含上述撇号链接的来源正文，目标实际出现了不应存在的反向引用。普通无撇号外链对照样例没有该引用。

**原因与影响：** `_balanced()` 将标签或 URL 中的普通撇号当作引号状态，链接/图片识别失败，内部标记被错误当成关系。除污染已保存反向引用外，后续按解析跨度执行的导出/恢复也可能误处理本应保护的文本。

**修改建议：** 区分链接标签、目的地址及可选标题的语法，不在所有括号内容中统一套用引号规则。保持单一共享解析器，不在 B/D 另写一套规避。

**回归要求：** 覆盖标签内撇号、双引号、行内代码，含撇号的 URL，图片，以及转义和既有嵌套括号样例；受保护内容不产生引用/反向引用。另保留普通正文有效标记的正向用例，并验证 UTF-16 跨度在非 BMP 字符前缀下仍准确。

## 6. C-REV-04：P3，行内链接保护跨度漏掉闭合括号

位置：`markdown.py` 的 `_link_end()`，原 282 行。

`_balanced()` 返回闭合字符的索引，但行内链接分支直接将其作为排他结束索引。实际对 `[x](https://example.com)` 切取 LINK 保护跨度，得到 `[x](https://example.com`，漏掉最后一个 `)`。

请与 C-REV-03 一起修复结束索引，并为普通链接、图片、带标题链接和 UTF-16 前缀补充“切片等于完整原构造”的断言。

## 7. C-REV-05：补充核对，错误集合不能证明原保存未提交

位置：[editing_storage.py](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/blob/feature/c-3-storage-query/src/pmkb/infrastructure/storage/editing_storage.py#L96)，`SqliteEditingStorage.check_save()`，原 96—103 行。

当前核实路径遇到集合不匹配会直接返回 `NOT_COMMITTED`。它只能证明适配器不匹配，不能证明原事务未提交；如果 B 接受这种证据，就可能解除待核实保护。

此项为独立审查提出的风险提示，**未计入上述三个已复现阻断项**。请结合 B 核实流程检查，补充“原命令可能已提交，但被交给另一集合适配器核实”的回归；应拒绝核实或保留未知，不得误报回滚。不要连带修改正常的保存前取消/参数拒绝语义。

## 8. 返修交付与复审清单

| 项目 | 本次状态 | 返修后需要提供 |
|---|---|---|
| C-REV-01 目标所有权 | 已复现，阻断 | 修改说明、竞态及原字节保护回归 |
| C-REV-02 视图快照 | 已复现，阻断 | 一致读取方案、真实交错回归 |
| C-REV-03 链接引用保护 | 已复现，阻断 | 解析与真实保存/反向引用回归 |
| C-REV-04 排他跨度 | 已复现，关联小修 | 完整原文跨度切片断言 |
| C-REV-05 核实语义 | 待核对风险 | 结论、调用方影响、针对性回归 |
| 全仓检查 | 本次已执行；返修结果未知 | 重新运行 `scripts/check.py`，记录通过/失败/跳过及原因 |
| Qt、D 端口及产品验收 | 待集成或未执行 | 如实保留边界，不写成已完成 |

请逐项回复编号、修改位置、测试命令和实际结果，继续更新本人的交付说明。复审通过后再按既有授权合并；Issue #3 不因本批组件返修完成就自动关闭。
