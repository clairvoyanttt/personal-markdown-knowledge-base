# C 后端交付审查与修改清单

审查日期：2026-11-03。关联 [Issue #3](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/issues/3)。

审查对象为 `feature/c-3-storage-query` 上 herbelat 于 2026-11-02 21:59:21（Asia/Hong_Kong）提交的首批后端，提交说明为 `feat(storage): add saved-version transactions, shared parser and saved queries (refs #3)`。下文行号以本次审查版本为准，返修后以函数名定位。

**结论：请求修改，暂不合并 C 后端。** 三项阻断问题已通过定向探针复现并经独立 AI 复核。本文发布到主线仅用于共享审查意见，不代表 C 后端已合并、已批准或已完成产品验收；也不表示任何成员完成了人工核查。

**最新复审：** C 已在原分支连续返修。第 12 节为第五轮最新结论：第四轮列出的回执与 `displayHint` 残余已由 PR #18 修复，经真实文件和独立探针复核通过并合并（`ef602aa`）；第 11 节为第四轮结论（PR #13 已合并）。第 1—10 节为此前审查记录，不能将其全部条目继续认定为未修复。

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

## 9. 第二轮复审：部分修复通过，仍请求修改

复审对象：[PR #13](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/pull/13)，原分支 `feature/c-3-storage-query`，2026-11-03 15:46:30（Asia/Hong_Kong）的 `fix(storage): address C-REV-01..05 review findings (refs #3)`。

本轮重新运行当前返修源码的 `scripts/check.py`：**568 项发现、566 通过、2 项因 Windows 符号链接权限跳过，退出码 0**。当前分支 CI 成功，但额外探针仍发现问题；未执行整组产品验收，也未将旧测试数字作为本次结果。

| 编号 | 本轮结论 |
|---|---|
| C-REV-01 | 目标竞争创建和异常清理已有改进，但所有权未贯穿写入/发布，仍有下述 P1 阻断 |
| C-REV-02 | 完整视图改为同一只读事务；本轮结构及真实交错回归通过，原混合读取路径已移除 |
| C-REV-03 | 原撇号链接/图片/URL 样例保护恢复；新增空白结束回归另记 C-REV-06 |
| C-REV-04 | 普通已覆盖链接构造的排他结束索引已修正 |
| C-REV-05 | 错误集合核实改为 `UNRESOLVED`，相关回归通过，保存前拒绝语义未连带改变 |

### C-REV-01 续：P1，候选身份只用于清理，未绑定写入与发布

位置：`writing.py` 原返修版本 584—589 行（初始化/发布调用）、601 行（SQLite 打开候选路径）和 759—762 行（按候选路径硬链接发布）。

本轮两个确定性交错探针使用真实文件和真实初始化/发布函数，均返回成功：

1. **独占创建后、初始化前替换候选：** 在 `_create_candidate()` 返回后，用另一创建者的空文件替换候选，再继续真实初始化。替换文件被写入完整数据库并发布，返回成功；并非只保留对方文件而不触碰。
2. **初始化后、发布前替换候选：** 在进入 `_publish_candidate()` 前，将已初始化候选替换为普通合成文本。实际正式目标含该非 SQLite 文本，仍返回 `RestoreInitResult` 成功。

记录的 `(st_dev, st_ino)` 只在 `finally` 清理时比较，无法保证先前写入的是本次文件，也无法保证发布的是已验证的数据库。现有“替换后主动抛异常，不删除替换者”测试不能覆盖以上路径。

请使所有权与内容有效性保护贯穿创建、打开、初始化、验证、发布和清理；检测替换后安全失败，既不能写入对方文件，也不能发布任意替换内容为成功集合。不要仅增加一次仍可再次竞态的路径检查。回归必须包含替换后继续执行真实写入/发布、目标不被误报成功及对方文件内容不变。

### C-REV-06：P2，目的地址后有空白且无标题时保护失效

位置：`markdown.py::_destination_end()` 原返修版本 319—329 行。

例如以下代码生成的正文均为有效外链/图片，MARKER 所在部分应受保护：

```python
marker = format_reference_marker("a" * 32, "b" * 32)
bodies = (
    f"[label {marker}](https://example.com )",
    f"[label {marker}](https://example.com\n)",
    f"![label {marker}](image.png )",
)
```

本次实际均产生 **1 个内部引用，保护跨度为空**。原因是裸目的地址结束后先检查 `)`，遇到空白则跳过空白，但跳过后没有再次接受直接闭合的 `)`，错误进入“必须有标题”的分支。

请支持目的地址后空格/换行、没有标题而直接结束的构造，覆盖普通链接、图片、空白后有标题的正向场景，补解析跨度和真实保存/反向引用回归。不能只修复最初三个撇号样例。

本轮正式审查已在 PR #13 请求修改。请继续在原分支追加返修，后端仍未合并。Qt、D 端口和完整产品验收继续保留原有边界。

## 10. 第三轮复审：链接空白已修复，恢复保护仍需修改

审查对象仍为 [PR #13](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/pull/13) 原分支，2026-11-03 16:37:19（Asia/Hong_Kong）的 `fix(storage): enforce restore ownership through publish and accept blank before paren (refs #3)`。

本轮重新运行当前返修源码 `scripts/check.py`：**573 项发现、571 通过、2 项因 Windows 符号链接权限跳过，退出码 0**。CI 查询成功；额外真实文件探针及独立 AI 复核仍确认以下问题，不能据现有测试全过判定可合并。

已确认改善：候选打开前后新增身份检查，原来粗粒度的替换后直接写入/发布普通文本路径受到拦截。C-REV-06 的空格、换行、图片三个样例及原撇号链接均实际为零内部引用，保护跨度等于完整原构造，相关真实保存回归通过。前轮已通过的快照、排他跨度和核实语义保持原记录。

### C-REV-01 续：P1，目标身份失配时反而删除了替换者文件

位置：`writing.py::_publish_candidate()` 本轮版本 877—881 行。

**确定性复现：** 让真实 `os.link()` 成功发布本次候选，随后在目标身份核对之前，仅执行一次竞争替换：移除刚才的目标链接名，并在同一路径写入另一创建者的已知合成文件。继续真实发布逻辑。

本次实际结果为 `StorageFailure` 且 `unknown=True`，但 `target_exists_after=False`、`replacement_preserved=False`：竞争者文件被 `target.unlink()` 删除。

这个分支已发现目标身份不等于记录身份，恰好说明不能再证明当前路径属于本次操作。“刚才创建过该名字”不能授权删除现在占用该名字的文件。

**修改要求：** 目标身份不符时不得按路径直接删除；保留现场并报告未知，交由受控的上层处理或人工检查。删除必须以仍持有的可靠所有权为依据，不靠再次存在判断掩盖竞态。补充“真实发布完成后目标被竞争替换”的回归，断言对方字节完整保留且不报告恢复成功；已有“发布前替换候选”的测试不能代替此场景。

### C-REV-07：P2，结构与数量校验不等于预期恢复内容一致

位置：`writing.py::_verify_candidate()` 本轮版本 815—840 行。

当前核对元数据、笔记/身份行数、初始化回执数量、`quick_check` 和 `foreign_key_check`，但没有比对传入 `notes` 的实际名称、正文、标签等内容。

**确定性复现：** 初始化一个正文为 `expected body` 的真实候选数据库。进入真实发布前，将候选文件里该正文等长原位改写为 `replaced body`，不替换 inode，不改集合身份、版本、行数或回执。继续真实 `_publish_candidate()`。

本次验证及发布都成功，返回 `RestoreInitResult`；从正式数据库查询到的正文却为 `replaced body`。SQLite 结构仍完整，但结果不等于请求恢复的已保存内容。

**修改要求：** 在一致只读视图内核对规范化后的预期恢复投影，包括实际笔记身份、名称/名称键、正文、标签，以及保留身份和引用等对应内容；不能只比较计数。可复用现有规则和共享解析，不新增独立规则副本或擅改共同契约。只有结构、身份及预期内容均符合才能报告初始化成功。

**回归要求：** 同一 inode、相同行数下的正文/标签/身份记录或引用内容不一致应被拒绝；同时保留正常恢复、空集合、合法失效引用和来源不被改写的正向用例。测试必须实际读取最终数据确认，不仅断言函数未抛异常。

第三轮正式审查仍在 PR #13 请求修改，C 后端未合并。请在原分支追加修复，按 C-REV-01 续、C-REV-07 回复位置与本次真实结果；C-REV-06 不必重复重做。整组产品验收仍未执行，本文数字不可作为下一次返修的验证结果。

## 11. 第四轮复审：C-REV-01 续与 C-REV-07 已修复，通过

审查对象为 [PR #13](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/pull/13) 原分支 2026-11-03 17:09:20（Asia/Hong_Kong）的 `fix(storage): keep replaced restore targets and verify restored content (refs #3)`（`d82fd71`）。本轮使用该提交的仓库外源码快照与独立探针复核，不继承作者或此前轮次的测试数字。

本轮实际运行快照 `scripts/check.py`：**576 项发现、574 通过、2 项因 Windows 符号链接权限（1314）跳过，24.316 秒，退出码 0**。两项跳过分属 D 的符号链接用例，不计为通过。CI 运行 41608759773、41608767578（head `d82fd71`）均为 success；CI 成功不覆盖下列独立探针。

### C-REV-01 续：通过

真实 `os.link()` 成功后，竞争者把目标名改指另一文件。本轮实际结果：`StorageFailure`、`unknown=True`、`code=IO_FAILURE`；竞争者字节完整保留，原先发布的链接也未被删除（`replacement_preserved=True`）。`_publish_candidate()` 不再在身份不符时 `unlink` 目标，符合“身份不符不得删除、保留现场、报未知”的要求。发布前候选替换、创建后替换等既有回归继续通过。

### C-REV-07：通过

同一 inode、同长度的正文原位替换（`expected body` → `replaced body`），不改集合身份、版本、行数和回执数量，在真实发布前执行。本轮实际结果：`_verify_candidate()` 在 `_publish_candidate()` 内拒绝，返回 `StorageFailure`（`INVALID_ARGUMENT`），目标未创建、未返回成功。实现新增 `_build_expected()` 冻结每篇笔记的规范化投影（身份、名称/名称键、正文、标签、引用 ordinal/目标/UTF-16 跨度）及保留身份，写入与两次验证共用同一份预期；验证逐篇比对正文、名称、名称键、修订、标签集合、引用投影和 `NoteKeys` 台账，并核对保留身份、回执数量与完整性。测试另覆盖名称、标签、引用跨度替换，均被拒绝且不产生目标。

正向回归：两篇笔记（名称空白规范化、标签排序）、三条引用投影与 `parse_markdown` 完全一致、`DELETED`/`MISSING_RESERVED` 保留身份、空集合（0 笔记/0 身份/1 条 `RESTORE_INIT`）均成功，读回实际数据库与请求一致。C-REV-06 的空格、换行、图片和撇号样例仍为零内部引用且保护跨度完整。C-REV-02/03/04/05 的既有用例在全套回归中通过。

### 非阻断观察（建议后续跟进）

在发布前对同一候选做原位改写时，以下字段未进入比对，探针仍返回成功：`CommitReceipt.noteId`/`collectionId` 的对应关系、`RefOccurrence.displayHint`。这些属于回执台账和展示提示，不改变笔记正文、名称、标签或引用跨度投影；复现仍要求能对候选文件原位写入的竞争条件，且本轮两条阻断问题均已消除。因此不阻断合并，但建议后续在 `_verify_candidate()` 中一并比较（写入时 `displayHint` 固定为 NULL，回执字段可直接构造预期），或在 Issue #3 记录为已知残余。

### 结论

C-REV-01 续与 C-REV-07 均经真实文件、真实发布路径复核通过，未发现新增阻断问题，按既有授权合并 PR #13。本节数字为本轮实际结果；完整 V01—V16 产品验收、正式性能验收和真实界面/离线/隐私验证仍未执行，合并组件不等于产品验收，Issue #3 不因合并关闭。

## 12. 第五轮复审：回执与 displayHint 残余已修复，通过并合并 PR #18

审查对象为 [PR #18](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/pull/18) head `a235ca8`（`fix(storage): verify restore receipt ownership and display hints (refs #3)`，作者 herbelat），base `main` `d940919`。该提交只改动 3 个文件：`src/pmkb/infrastructure/storage/writing.py`、`tests/test_c_snapshot.py`、`docs/development/c-storage-query.md`，未发现越界改动。本轮使用该提交的仓库外源码快照与独立探针复核，不继承作者或此前轮次的测试数字。

本轮实际运行：

- 快照 `scripts/check.py`：**579 项发现、577 通过、2 项因 Windows 符号链接权限（1314）跳过、12.974 秒、退出码 0**；C 的 6 个测试模块单独运行 **102/102 通过**。
- PR CI 41628148497、41628185027（head `a235ca8`）均为 success。
- 合并后 `main` `ef602aa` 仓库外快照：**579 项发现、577 通过、2 项跳过、23.491 秒、退出码 0**；main CI 41629613518（head `ef602aa`）success。两项跳过分属 D 的符号链接用例，不计为通过。

### 修复内容

`_verify_candidate()` 补齐第 11 节列出的两项非阻断残余：

- `RESTORE_INIT` 恰好 1 条：`collectionId` 为新集合，`noteId`/`beforeRevision`/`afterRevision` 均为 NULL，`collectionRevision`=1。
- `RESTORE_NOTE_INIT` 数量与 notes 一致，每条 `collectionId` 为新集合、`noteId` 属于本批、`beforeRevision` NULL、`afterRevision`=1、`collectionRevision`=1。
- 每篇 `Notes.lastCommitId` 必须指向**该篇自己的** `RESTORE_NOTE_INIT` 回执（按 `commandId` 关联后逐篇核对）。
- 每篇笔记的 `RefOccurrence.displayHint` 必须全部为 NULL（写入时固定 NULL）。

### 独立探针

均在真实候选文件上、真实 `_publish_candidate()` 发布前原位改写：

- `RESTORE_NOTE_INIT.noteId` 互换、`Notes.lastCommitId` 互换、`RESTORE_NOTE_INIT.afterRevision=2`、`RESTORE_INIT.collectionRevision=2`、`RESTORE_NOTE_INIT.collectionId` 改写、`RefOccurrence.displayHint='legacy hint'`：**6/6 均返回 `StorageFailure`（`INVALID_ARGUMENT`），目标未创建、未报告成功**。
- `RefOccurrence.sourceRevision=2`：虽未进入显式投影，但被最终 `PRAGMA foreign_key_check` 拒绝，目标未创建。
- 正向与既有修复回归：C-REV-01 续（发布后目标被替换仍报 unknown 且保留竞争者字节）、C-REV-07（同 inode 同长度正文替换被拒）、C-REV-06 解析样例、正常恢复（两篇笔记、名称规范化、标签、三条引用、保留身份、空集合）均重新通过。

### 非阻断观察（建议后续跟进）

对同一候选原位改写 `CommitReceipt.operationId` 仍可通过验证并发布。该值是写入时生成的随机操作标识，不属于请求负载的预期投影，也不改变笔记、引用或回执语义；如后续要求回执台账逐字段一致，再纳入校验即可。此项不阻断本轮合并。

### 结论

第 11 节所列残余已补齐并经独立探针确认，未发现新增阻断问题。PR #18 已按既有授权合并为 **`ef602aa`**；正式审查记录为 AI 技术复审 5751562671（state=APPROVED，正文明确标注 AI、非人工审查）。以上均为组件级存储复审与回归，不等于 V01—V16 完整产品验收、正式性能验收或真实界面/离线/隐私验证；Issue #3 不因合并关闭。
