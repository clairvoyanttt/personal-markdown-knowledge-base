# C 交付：输入规则、SQLite 受控事务、共享解析、保存版查询

2026-11-02，关联 Issue #3。基于 main `4e8934d`。本批交付 C 范围内的源码、测试与说明，
**不关闭 Issue #3**，也不代表 API04/05/10 已获 B/D 共同确认或 V01—V16 产品验收通过。

## 1. 本批做了什么

| 模块 | 文件 | 职责 |
|---|---|---|
| 纯规则 | `src/pmkb/domain/rules.py` | 版本/文本/代理项的运行时校验与统一 `RuleViolation` 错误码 |
| 纯规则 | `src/pmkb/domain/naming.py` | 名称 1—100 码点、标签 1—30/每篇 20、明确空白裁剪、ASCII 名称键 |
| 纯规则 | `src/pmkb/domain/identity.py` | 32 位小写十六进制身份、74 单元引用标记的生成/解析、历史显示名 |
| 解析 M04 | `src/pmkb/application/parsing/document.py` | `ParsedDocument`、保护跨度、引用出现项、六类结构块、UTF-16 半开区间 |
| 解析 M04 | `src/pmkb/application/parsing/markdown.py` | 唯一的 Markdown 扫描器（代码/外链/图片/转义保护，标记识别，跨度换算） |
| 存储 M09 | `src/pmkb/infrastructure/storage/connection.py` | 连接策略：每连接外键 ON 并核实、显式 BEGIN/COMMIT/ROLLBACK、只读连接 |
| 存储 M09 | `src/pmkb/infrastructure/storage/writing.py` | SAVE/DELETE 受控事务、回执、版本推进、提交后独立复读、恢复初始化事务 |
| 存储 M09 | `src/pmkb/infrastructure/storage/reader.py` | 保存版读取、搜索、标签筛选、反向引用、引用分类、一致快照 |
| 存储 M09 | `src/pmkb/infrastructure/storage/store.py` | `CollectionStore`：唯一写锁 + 只读连接，API10 的服务入口，不暴露连接 |
| 存储 M09 | `src/pmkb/infrastructure/storage/editing_storage.py` | B 的 `EditingStorage` 端口实现：单线程执行器、SAVE 证据映射 |
| 查询 M05 | `src/pmkb/application/query/service.py` | API04 搜索/筛选、API05 保存视图（引用+反向引用），返回集合版本 |

未修改 `contracts/`、`desktop/`、`application/editing/`、`application/export/`、
`application/backup_restore/`、`application/collection/`、`schema.sql` 或任何依赖版本。
`collection_catalog.py` 与 `schema.sql` 按 A 已批准的 DDL 复用，未改结构。

## 2. 规则如何落实（可核对到代码）

- **先拒绝再裁剪**：`_reject_forbidden_characters` 先拒绝 U+0000—001F、U+007F、U+2028/2029，
  再由 `trim_edges` 只裁 `U+0020 U+0085 U+00A0 U+1680 U+2000—200A U+202F U+205F U+3000`。
  首尾制表符因此被**拒绝**而不是被裁掉（FR04 补充验收）。长度按 Unicode 码点，不按 UTF-8 字节或 UTF-16 单元。
- **名称判重不扩大语义**：`name_key` 只折叠 ASCII `A—Z`，不用 `casefold`/Unicode 正规化；
  `Notes.nameKey UNIQUE` 是兜底，事务内先按 `nameKey` 排除自身查重，返回 `DUPLICATE_NAME`。
- **标签**：区分大小写；完全相同的字符串不增加名额；`AI`/`ai` 各占 1 项；超过 20 个不同标签返回 `TAG_LIMIT`，
  不截断、不丢弃输入。
- **身份**：`[[note:<32hexCollection>:<32hexNote>]]` 固定 74 个 UTF-16 单元；
  `[[note:]]`、旧短示例、大写十六进制都不是标记；`Notes`/`NoteKeys` 的触发器和延迟外键禁止改身份、
  禁止复用已删除身份；删除时 `NoteKeys` 置 `DELETED` 并保留 `lastSavedName`。
- **保护跨度优先**：先算围栏代码、行内代码、外链（含目的地址/标题）、图片、自动链接、引用定义行、反斜杠转义，
  再在剩余文本识别完整标记；代码/外链/图片里的同形文字不建关系。跨度统一转成 UTF-16 半开区间，
  调用方只能用 `ParsedDocument.slice_span` 切片，`😀` 等非 BMP 字符不会错位。
- **事务**：`autocommit=True` + 显式 `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK`；先核 `foreign_keys=1`；
  顺序为 身份/版本校验 → 事务内复查名称/标签/判重/base_revision → 写身份/正文/标签/出站出现项 →
  写 SAVE 回执 → 推进 `collectionRevision` → COMMIT → 用独立只读连接复读回执+正文/标签/`lastCommitId`。
- **失败语义**：回滚成功才返回 `NOT_COMMITTED`；COMMIT 或复读无法确认返回 `UNRESOLVED`；
  缺回执不证明未提交（`check_save` 对缺失回执返回 `UNRESOLVED`）。
- **删除**：确认预期版本 → 写 DELETE 回执（before=当前修订）→ 身份置 `DELETED` 保留历史名 →
  删除 `Notes`（标签与出站级联，入站保留）→ 推进集合版本；删除后同名重建不会复活旧引用。
- **查询只读保存版**：搜索用 `instr(lower(name/body), lower(:kw))`——SQLite 的 `lower()` 只折叠 ASCII，
  与名称键语义一致；空/全空白关键词返回 `INVALID_ARGUMENT`（不是空结果）；标签筛选区分大小写且要求全部命中；
  结果携带 `collectionRevision`，迟到回调可按集合/代次丢弃。
- **引用分类**：先判集合归属（外来 → `FOREIGN_COLLECTION`，不按名称回退），再按身份账本判
  `VALID`/`DELETED`/`MISSING`，删除目标用 `lastSavedName` 显示、绝不换绑到同名新笔记。

## 3. 与 B / D / A 的对接

| 对方 | 本批提供 | 状态 |
|---|---|---|
| B | `SqliteEditingStorage`（`EditingStorage` 端口：`load_note`/`save`/`check_save`） | 已用 B 的真实 `EditingService` 联调：保存、二次保存、判重、草稿不入检索、并发串行 |
| D | `capture_snapshot()`（一致只读快照）与 `initialize_restored_collection()`（新集合初始化事务） | 存储侧可用并有测试；**尚未**实现 D 的 `validate_snapshot`/`initialize_restore` 端口适配，见未执行项 |
| A | 复用 `schema.sql`、连接/外键/完整性检查；`CollectionStore` 不暴露连接 | A 的集合发布/登记流程未改动 |

B 的端口类型仍是 B 提出的窄端口，不是已批准 API10；C 的 `CommitResult` 映射为
`SaveEvidence`，`COMMITTED` 只确认被捕获的快照（版本 = base+1、集合版本真实、`lastCommitId` 匹配）。
API04/05 的 `SearchRequest`/`SearchReply`/`SavedViewRequest`/`SavedNoteView` 是 C 侧精确对象，
**待 B 确认字段与状态**后再视为稳定契约；`ReferenceState` 与 D 内部 `ReferenceKind` 取值一致，
但 C 不 import D 的模块，映射由适配方负责。

## 4. 实际执行的检查

环境：Windows（见第 6 节设备记录）、CPython 3.12.10、SQLite 3.49.1（Python 自带）、
`PYTHONPATH=src`、`python -m unittest`。全部使用真实临时集合数据库（经 A 的 `LocalCollectionBackend`
创建并发布），没有用内存替身冒充持久化。

| 用例文件 | 数量 | 覆盖 |
|---|---:|---|
| `tests/test_c_naming.py` | 17 | 100/101 码点、30/31 标签、20/21 项、控制字符先拒绝、明确空白、ASCII 键、代理项、标记格式 |
| `tests/test_c_parsing.py` | 20 | 代码/外链/图片/转义/引用定义保护、撇号与引号、`)` 前空白、完整跨度切片、完整标记、外来归属、UTF-16（😀/CRLF）、六类块、坏输入 |
| `tests/test_c_storage.py` | 19 | 新保存/二次保存/base 冲突、判重、重复命令、外键、触发器、指纹注入回滚、引用替换、删除级联、回执复读、`UNRESOLVED` |
| `tests/test_c_query.py` | 19 | 连续字面匹配、ASCII 大小写、空/空白查询、无结果、标签 AND/区分大小写、集合隔离、改名保持关系、反向引用、删除不复活、外来/缺失、视图单事务与交错一致性、`)` 前空白真实保存 |
| `tests/test_c_snapshot.py` | 15 | 快照一致性与身份账本、恢复初始化（READY/修订 1/初始化回执/引用重建）、保留已删除身份、目标所有权、竞争创建、候选在创建后/初始化后/发布前被替换、发布后目标被替换、内容与引用投影替换、回执与 displayHint 篡改 |
| `tests/test_c_editing_adapter.py` | 12 | 与 B 真实服务联调、草稿不入检索、判重不写入、上下文过期、取消不写入、并发保存串行、无回执不判回滚、跨集合核实保持未知 |
| `tests/test_c_reopen.py` | 8 | 最小闭环：空集合、列表顺序、重开后列表/搜索/读回逐字符一致、删除后移出、关系重开后一致、`close` 等待在途保存并拒绝新工作、`aclose` 可等待 |

合计 **110 项 C 用例**（一次性运行 `Ran 110 tests ... OK`）。首次运行时发现并修正的问题：
测试用 `with sqlite3.Connection` 不关闭句柄导致 Windows 临时目录占用（改为 `closing`）；
B 的草稿在首存前 `note_id` 为 `None`，证据断言改用 `SaveReply.note_id`。
这些是测试/接线错误，不是业务规则放宽。

全仓回归：`.venv\Scripts\python.exe scripts/check.py` 实际 **579 项发现、577 通过、2 项因
Windows 符号链接权限跳过（exit code 0）**，其中 C 共 102 项；语法、公共契约导入与 `pip check` 均通过。
（2 项跳过属于 D 的符号链接用例，不计为本批通过。）

### 4.1 NFR03 规模的 C 侧单机记录

命令：`python scripts/benchmark_c.py --count 1000 --body-bytes 10240`
（`scripts/benchmark_c.py` 为本批新增，用 A 的真实集合创建 + C 的真实事务生成合成数据，
对 30 条固定查询逐条核对结果集并计时；退出码 0 表示 30/30 正确且均在 2 秒内。）

| 项目 | 实际值 |
|---|---|
| 环境 | Windows 11 `10.0.26200`，Intel64 Family 6 Model 183，CPython 3.12.10 |
| 数据 | 1000 篇，每篇正文目标 10240 UTF-8 字节（含唯一 TOKEN、中文/英文混排），另含 50 条已保存引用 |
| 数据库 | `knowledge.sqlite` 12,918,784 字节 |
| 保存 | 1000 笔独立事务合计 14.837 秒，平均 14.4 ms，p95 19.1 ms |
| 查询 | 30 条固定查询合计 0.605 秒；p50 20.93 ms，p95 31.16 ms，最大 41.87 ms |
| 正确性 | 30/30 结果集与构造预期一致（含大小写、全空白负例、标签 AND、标签大小写区分） |
| 2 秒门限 | 30/30 ≤ 2 秒 |

这是 **C 侧单机、单次、合成数据** 的实际记录，不等于 NFR03 整组验收：设计要求的固定数据集版本、
预热/后台负载/顺序记录和 A 侧复核仍需按验收流程另行执行；本记录只说明 C 的存储与查询路径在该规模可用。

## 5. 未执行与限制（不能写成通过）

- **未接入 Qt 界面**：B 的 `desktop/editor.py` 仍以 `storage=None` 启动；本批只交付服务与适配，
  真实界面里的保存版列表、搜索框、引用/反向引用面板、删除确认尚未接线。
- **六类预览未渲染**：解析器提供结构块与跨度，但 B 的 `PreviewRenderer` 渲染适配未实现；
  FR03 预览验收未执行。
- **D 的恢复端口未适配**：`initialize_restored_collection` 已有事务与测试，但 D 的
  `RestoreStoragePorts.validate_snapshot`/`initialize_restore`/`release` 适配、清单交叉核对、
  发布与 `source_snapshot_sha256` 计算尚未做，FR11 未联调。
- **未执行**：V01/V04/V05/V06/V10/V14 整组验收；NFR03 的正式验收流程（固定数据集版本、预热/后台负载/
  顺序记录、A 侧复核）；Windows 11 人工界面/离线/隐私检查；跨模块故障注入联调。4.1 的单机数据只是
  C 侧实际记录，不能直接写成 NFR03 通过。
- **解析器范围**：不解释强调、表格、公式、原始 HTML、嵌套块重排；这些字符按普通文本保留。
  这是有意的范围限制，不是完整 CommonMark 实现。
- **删除/恢复的历史回执**：库存回执不清理（RESTORE 会新建集合，旧库不迁移），符合设计；
  未实现任何回执压缩/清理任务。
- **性能**：保存每笔都是独立事务 + fsync + 提交后复读，单笔成本高于批处理；未做大集合压测。

## 6. 设备与运行方式

本批实际运行环境：Windows 11 `10.0.26200`、Intel64 Family 6 Model 183、CPython 3.12.10、
SQLite 3.49.1（Python 3.12 自带）。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts/check.py
.\.venv\Scripts\python.exe -m unittest tests.test_c_storage tests.test_c_query -v
.\.venv\Scripts\python.exe scripts/benchmark_c.py --count 1000 --body-bytes 10240
```

新增依赖：**无**。解析器为自研标准库扫描器，未引入第三方 Markdown 库，因此没有新的版本/许可证审核项；
选择理由与限制见 `application/parsing/markdown.py` 模块说明。

## 7. 下一步（按依赖）

1. B 确认 `SearchRequest`/`SavedViewRequest` 字段与错误状态，接线保存版列表/搜索/引用面板（API04/05/06）。
2. D 与 C 核对 `CollectionSnapshot` 与恢复端口，补 `validate_snapshot`/`initialize_restore` 适配与清单交叉核对（API09）。
3. A/B 在真实界面下跑保存→重开→搜索→引用→删除闭环，再补 V01/V04/V05/V06 的实际记录。
4. 把 4.1 的单机规模记录并入 NFR03 正式验收（固定数据集版本、预热/负载/顺序、A 侧复核）。

## 8. 2026-11-03 复审返修记录（C-REV-01—05）

复审依据：[C 后端交付审查与修改清单](reviews/c-backend-review-2026-11-03.md)（已发布到 main `9ddac57`）。
返修仍提交在原分支 `feature/c-3-storage-query`，未重做、未另起分支、未改写已共享历史。

| 编号 | 修改位置 | 修改内容 | 实际回归结果 |
|---|---|---|---|
| C-REV-01（P1） | `writing.py::initialize_restored_collection`，新增 `_create_candidate`/`_candidate_identity`/`_discard_candidate`/`_publish_candidate` | 不再用 `exists()` 当所有权证明：先在同一目录用 `O_CREAT\|O_EXCL` 独占创建私有候选并记录 `(st_dev, st_ino)`，初始化全部在候选内完成，再用同目录硬链接原子发布且不覆盖；异常清理由 `finally` 执行，但只在文件身份仍与本操作记录一致时删除 | 目标已存在 → `TARGET_EXISTS`，原字节逐字节不变、无候选残留；竞争者在发布前创建目标 → `TARGET_EXISTS`，对方字节不变、无候选残留；候选被替换 → 不删除替换者（保留 `intruder bytes`）；正常初始化、保留身份与初始化回执用例继续通过 |
| C-REV-02（P2） | `reader.py::saved_view`（新增 `SavedViewData`、`_classify_with`、`_backlink_rows`）、`store.py::saved_view`、`query/service.py::saved_view` | 正文、标签、引用分类、反向引用和集合版本改为在**同一个只读事务**内读取；服务层不再分四次调用 `load_note`/`classify`/`backlinks`/`metadata` | 结构回归：一次视图只开 1 个连接、恰好 1 次 `BEGIN` 与 1 次 `COMMIT`；交错回归：真实 SAVE 线程持续改写时，120 次视图的正文、引用数、`collectionRevision` 均对应同一保存时刻（`collectionRevision == revision + 2`），无拼接 |
| C-REV-03（P2） | `markdown.py::_link_end`、`_balanced`、`_destination_end`、`_bare_destination_end`、`_quoted_end`、`_parenthesised_end` | 链接标签只做括号配平，撇号/双引号按普通字符；目的地址区分裸地址、`<>` 地址和可选标题，引号只在标题位置生效，因此 URL 中的撇号/双引号不会吞掉闭合括号 | 复审三个复现样例（含 `don't` 的链接标签、图片、URL）引用数均为 0 且产生对应保护跨度；额外覆盖双引号 URL、行内代码、嵌套括号、转义；普通正文有效标记与反向引用正向用例仍通过 |
| C-REV-04（P3） | `markdown.py::_link_end` | 返回闭合括号后一位，使排他跨度包含完整构造（原先漏掉最后的 `)`） | 普通链接、图片、带标题链接、`<>` 地址、嵌套括号与 `😀` 前缀样例的 `slice_span` 均等于完整原构造 |
| C-REV-05（补充核对） | `editing_storage.py::check_save` | 适配器集合不匹配时返回 `UNRESOLVED`（`COMMIT_UNCONFIRMED`），不再返回 `NOT_COMMITTED`；`save()` 的保存前取消与上下文拒绝语义未改 | 原命令已在集合 A 提交：A 适配器核实为 `COMMITTED`（修订 1）；交给集合 B 的适配器核实为 `UNRESOLVED`、`saved_revision`/`collection_revision` 均为空，不解除 B 的待核实保护 |

返修后实际检查：

- C 用例 **93 项全部通过**（原 86 项 + 7 项针对性回归）。
- 全仓 `scripts/check.py`：**568 项发现、566 通过、2 项因 Windows 符号链接权限（1314）跳过，exit code 0**；跳过项不计为通过。
- 未因返修改动 `contracts/`、B/D 模块、依赖版本或已验证的保存/删除回执语义；未新增第三方依赖。
- 仍未执行（与本轮修复无关，如实保留）：Qt 界面接线、六类预览渲染、D 恢复端口适配、V01—V16 整组验收、
  NFR03 正式流程。返修通过只说明上述五项已处理，不代表 C 后端已合并或产品验收完成。

### 8.1 第二轮复审返修（C-REV-01 续、C-REV-06）

二轮复审结论：C-REV-02/03/04/05 通过；C-REV-01 的所有权未贯穿写入与发布；新增 C-REV-06（`)` 前空白）。
本轮针对这两项追加修复，仍提交在原分支。

**C-REV-01 续（P1）：所有权与内容有效性贯穿创建 → 打开 → 初始化 → 验证 → 发布 → 清理。**

- `_open_owned_candidate()`：打开候选前后各校验一次 `(st_dev, st_ino)`。若候选在打开前被替换，直接失败且不写；
  若在打开过程中被替换，在写入任何语句前关闭并失败。
- `_verify_candidate()`：不只比身份（就地覆盖会保留 inode），还以只读连接核对
  `CollectionMeta`（新集合身份/版本 1/修订 1/READY）、`Notes`/`NoteKeys` 行数、`RESTORE_INIT`/`RESTORE_NOTE_INIT`
  数量、每篇 `revision=1` 且 `lastCommitId` 指向初始化回执、`quick_check` 与 `foreign_key_check`。
  初始化提交后立即执行一次，发布前再执行一次。
- `_publish_candidate()`：验证 → 硬链接 → 校验目标 inode 等于已记录身份（不等则删除自己创建的链接名并返回未知失败）
  → 对目标再做一次内容验证；失败只删除自己的链接名，不报告 `RestoreInitResult`。
- `_discard_candidate()`：仍只在文件身份匹配时清理；被替换或就地覆盖后不再属于本次记录的候选不会被误删。

回归（真实文件、真实初始化/发布函数，通过包装调用边界在三个时点替换候选）：

| 时点 | 实际结果 |
|---|---|
| 独占创建后、初始化前替换候选 | 抛 `StorageFailure`（`unknown=True`），替换者字节逐字节不变，目标不出现 |
| 初始化后、发布前就地覆盖候选为普通文本 | 发布前内容验证失败，不发布、不返回成功，目标不出现，候选按身份清理 |
| 预检查与 `os.link` 之间替换候选 | 目标 inode 校验失败，删除刚创建的链接名并返回未知失败，不误报成功 |

**C-REV-06（P2）：目的地址后空白且无标题时保护失效。**

- `_destination_end()`：跳过空白后若直接遇到 `)`，按“无标题的合法链接/图片”接受并返回闭括号后一位；
  只有空白后跟着其他 token 时才要求标题。
- 回归：`[label MARKER](https://example.com )`、`[label MARKER](https://example.com\n)`、`![label MARKER](image.png )`
  均产生零引用且保护跨度切片等于完整构造；空白后有标题的 `(url  "title" )` 仍被正确接受；
  另加真实 SAVE 回归：三类正文保存后 `RefOccurrence` 为空、目标无反向引用。

本轮返修后实际检查：C 用例 **98 项全部通过**；全仓 `scripts/check.py` **573 项发现、571 通过、
2 项符号链接权限跳过，exit code 0**。PR #13 自动包含本轮提交；仍未合并，未执行产品验收。

### 8.2 第三轮复审返修（C-REV-01 再续、C-REV-07）

三轮复审结论：C-REV-02/03/04/05 及 8.1 的 C-REV-06 通过；C-REV-01 的删除边界仍不成立；新增
C-REV-07（验证未比对期望内容）。两项均在原分支追加修复。

**C-REV-01 再续（P1）：身份不符时不得删除目标。**

- `_publish_candidate()`：`os.link` 之后若目标身份与记录不符，**不再 `unlink`**，而是返回 `unknown=True`
  的 `StorageFailure` 并保留现场；发布后内容验证失败同样只报告未知、保留目标。身份不符证明本调用无法
  证明对当前名字的删除授权，"刚才创建过这个名字"不能作为删除依据。
- 回归：预检查与 `os.link` 之间替换候选 → 不误报成功、目标保留替换内容；link 与校验之间目标被竞争者
  替换 → 竞争者字节逐字节不变；发布后发现目标不是本文件 → 保留并报未知。

**C-REV-07（P2）：验证必须比对期望恢复内容，而不只是结构。**

- 新增 `_ExpectedNote` / `_ExpectedCollection` 与 `_build_expected()`：在创建候选**之前**一次性校验并
  冻结每篇的 `noteId`、规范化名称与名称键、正文、标签集合、引用投影（ordinal/目标集合/目标笔记/
  `startUtf16`/`lengthUtf16`）以及保留身份；写入与验证共用这一份投影，不再各自重新解析。
- `_verify_candidate()` 逐篇比对数据库中的正文、名称、名称键、修订 1、标签集合、引用投影和
  `NoteKeys` 台账（LIVE / liveNoteId / lastSavedName），再核对保留身份、回执数量与完整性。
- 回归：真实文件、真实发布函数，在发布前分别把**正文、名称、标签、引用跨度**替换为同结构的其他内容，
  四种替换全部被拒绝、目标不出现、不返回成功；正常恢复用例继续通过。

本轮返修后实际检查：C 用例 **101 项全部通过**；全仓 `scripts/check.py` **576 项发现、574 通过、
2 项符号链接权限跳过，exit code 0**。仍未合并，未执行产品验收。

### 8.3 第四轮复审非阻断项跟进（2026-11-03）

第四轮复审结论：C-REV-01 续与 C-REV-07 经真实文件/发布路径复核**通过**，PR #13 按既有授权合并
（main `d940919`）。复审仅留一条非阻断建议：发布前的原位改写探针仍可漏过 `CommitReceipt.noteId` /
`collectionId` 的对应关系与 `RefOccurrence.displayHint`。

本批在从 main 新建的 `fix/c-r4-receipt-projection` 上补齐（不改动已合并历史）：

- `_verify_candidate()` 增加回执台账校验：恰好 1 条 `RESTORE_INIT`（`collectionId`=新集合，
  `noteId`/`beforeRevision`/`afterRevision` 均为 NULL，`collectionRevision`=1）；`RESTORE_NOTE_INIT`
  数量与 notes 一致，每条 `collectionId`=新集合、`noteId` 属于本批、`beforeRevision` NULL、
  `afterRevision`=1、`collectionRevision`=1；每篇 `Notes.lastCommitId` 必须指向**该篇自己的**
  `RESTORE_NOTE_INIT` 回执。
- `_verify_candidate()` 增加 `RefOccurrence.displayHint` 必须为 NULL 的校验（写入时固定 NULL）。
- 回归 `test_receipt_and_display_hint_tampering_is_rejected`：在真实发布前对候选原位改写
  `RESTORE_NOTE_INIT.collectionId`、`RESTORE_NOTE_INIT.noteId`、`RESTORE_INIT.collectionId`、
  `RefOccurrence.displayHint` 四种字段，全部被拒绝、目标不出现、不返回成功。

本轮实际检查：C 用例 **102 项全部通过**；全仓 `scripts/check.py` **579 项发现、577 通过、
2 项符号链接权限跳过，exit code 0**。仍未执行产品验收。

## 10. 最小交付范围（2026-11-04 范围收紧）

依据《范围收紧执行版（2026-11-04）》：本次只保证
**新建 → 写笔记 → 保存 → 退出重启 → 保存版列表重开 → 搜索找到**。

C 本轮的范围：

- 确认 `CollectionStore.summaries()` / `search()` / `load_note()` / `saved_view()` 在真实集合上的
  行为、排序、空结果和关闭后重开一致；为保存版列表与搜索补集成测试（`tests/test_c_reopen.py`）。
- **PR #21（D 导出/恢复端口）整体移出本次交付**，不合并。`CExportSource` 与恢复适配保留在分支
  `feature/c-d-port-adapters`；只有决定保留导出时再单独交付 `CExportSource`，并先修取消语义与
  `StorageFailure` 映射；恢复部分留在开发分支。
- 新增 `SqliteEditingStorage.aclose()`：B 的串行关闭/重绑定（B-03）可以 await 存储线程结束，
  不阻塞 Qt 事件循环；阻塞式 `close()` 保留给非 UI 线程。`close()` 开始即拒绝新提交，返回时在途工作已结束。
- 标签、引用与反向引用、备份恢复、预览、导出的界面入口按范围文档延期或隐藏；C 的底层实现与测试
  保留，不为了裁剪删除代码。
- 排序语义：`summaries()` 按 `lower(name)` 再按 `noteId`；SQLite 的 `lower()` 只折叠 ASCII，与名称键
  一致；折叠后相同的名称已被名称键唯一约束拒绝，因此 `noteId` 只是确定性的兜底。
- 验证边界：本节的列表/搜索/读回是**组件级真实 SQLite 回归**，不等于 Qt 人工验收；最小验收仍需
  真实界面、真实执行人和设备记录（范围文档第 4 节）。

本轮实际检查：C 用例 **110 项全部通过**；全仓 `scripts/check.py` **587 项发现、585 通过、
2 项符号链接权限跳过，exit code 0**。
