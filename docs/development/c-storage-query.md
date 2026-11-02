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
| `tests/test_c_parsing.py` | 17 | 代码/外链/图片/转义/引用定义保护、完整标记、外来归属、UTF-16（😀/CRLF）、六类块、坏输入 |
| `tests/test_c_storage.py` | 19 | 新保存/二次保存/base 冲突、判重、重复命令、外键、触发器、指纹注入回滚、引用替换、删除级联、回执复读、`UNRESOLVED` |
| `tests/test_c_query.py` | 16 | 连续字面匹配、ASCII 大小写、空/空白查询、无结果、标签 AND/区分大小写、集合隔离、改名保持关系、反向引用、删除不复活、外来/缺失 |
| `tests/test_c_snapshot.py` | 6 | 快照一致性与身份账本、恢复初始化（READY/修订 1/初始化回执/引用重建）、保留已删除身份、拒绝已存在目标、坏输入不留文件 |
| `tests/test_c_editing_adapter.py` | 11 | 与 B 真实服务联调、草稿不入检索、判重不写入、上下文过期、取消不写入、并发保存串行、无回执不判回滚 |

合计 **86 项 C 用例**（一次性运行 `Ran 86 tests ... OK`）。首次运行时发现并修正的问题：
测试用 `with sqlite3.Connection` 不关闭句柄导致 Windows 临时目录占用（改为 `closing`）；
B 的草稿在首存前 `note_id` 为 `None`，证据断言改用 `SaveReply.note_id`。
这些是测试/接线错误，不是业务规则放宽。

全仓回归：`.venv\Scripts\python.exe scripts/check.py` 实际 **561 项发现、559 通过、2 项因
Windows 符号链接权限跳过（exit code 0）**，其中 C 新增 86 项；语法、公共契约导入与 `pip check` 均通过。
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
