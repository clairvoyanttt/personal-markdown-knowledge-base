# D 输出服务：接口草案、使用与交接

更新日期：2026-10-19。总进度以 [D00—D05 清单](d-progress.md)为准。
本文覆盖 D01 的可执行 D 侧流程和 D03 交接，不宣称真实 Qt/SQLite 集成或公共契约已经共同批准。
没有更改 `src/pmkb/contracts`；API08 沿用原请求/回复，API07/09 及依赖端口仍为待确认草案。

## 1. 源码与调用方向

| 入口 | 调用方向 | 当前可执行能力 | 真实实现尚需提供 |
|---|---|---|---|
| [ExportService.export_note](../../src/pmkb/application/export/service.py) | B → D → C 保存投影 / D 文件层 | 保存版、无保存版、选中笔记未保存选择、四类引用转换、命名、非覆盖及结果处理 | B 的提示/会话，C 的 `ExportSourcePort` |
| [BackupServiceImpl.backup_collection](../../src/pmkb/application/backup_restore/backup_service.py) | B → D → C 一致快照 / D 文件层 | 全草稿选择复核、同次来源事实、清单、实际归档验证和发布 | B 的会话，C 的 `SnapshotSourcePort` |
| [RestoreService.restore_backup](../../src/pmkb/application/backup_restore/restore_service.py) | B → D → C 校验/初始化 → A 发布 | 空目标检查、受控候选、初始化报告绑定、取消/未知保护；不激活 | B 的会话，C 的 `RestoreStoragePort`，A 的 `RestoreTargetPort` |
| [prepare_restore_candidate](../../src/pmkb/application/backup_restore/candidate.py) | D → 单个受控 ZIP 源句柄 | 验证并流式提取私有候选，实际摘要/大小检查，自有身份清理 | C 必须在自己的受控读取边界复核候选，不作 unchecked reopen |

三个服务必须显式注入端口，没有默认的假数据库、假解析器或假成功 UI。
文件任务通过 `run_io` 在工作线程执行；真实 C 端口须使用自身连接所属的串行队列，
不能把 `sqlite3.Connection` 跨线程传入 D，也不能直接在 Qt 线程阻塞等待。

## 2. 会话与选择：B → D

[output_support.py](../../src/pmkb/application/output_support.py)给出 D 内部 `OutputSession.inspect()` 草案。
该异步端口应通过 B 的 Qt 调度桥捕获一次不可变状态，不在后台直接读控件。

| 类型/字段 | 要求 |
|---|---|
| `OutputSessionState.context` | 当前集合 ID/代次；导出和备份要求已有活动集合，恢复可为 `CallContext(None, 0)` |
| `buffers` | `tuple[OutputBufferState, ...]`；每个 buffer 的身份、编辑版本、脏状态及可空保存笔记 ID；包含全新草稿 |
| `has_unconfirmed_save` | 当前是否存在待核实 SAVE 的布尔状态标志，不是命令 ID；为真时阻止本轮输出，不调用 API11 冒充核实 |
| `acknowledged_dirty_versions` | 本次用户决定已明确覆盖的 `(buffer_id, edit_version)` 元组；须绑定本次操作和 Context，不是永久忽略草稿的开关 |

三种选择沿用 `UnsavedBackupChoice`：

- `RETURN_TO_SAVE`：本次直接 `CANCELLED`，不调用保存、读取快照或输出文件。
- `NO_UNSAVED`：服务检查实际脏状态，不信任 UI 的空值声明。
- `CONTINUE_SAVED_ONLY`：当前脏版本必须逐项匹配用户确认；捕获期间新增/修改草稿必须重新决定。

单篇导出只针对选中笔记检查脏状态；其他笔记的普通草稿不阻止导出，但待核实 SAVE 全局阻止。
备份检查全部草稿。服务在捕获及发布前复核状态；B/C 必须在自己的捕获边界保证对应 Context 和选择证据有效，
不能把几次 Python 状态检查宣传为横跨 UI/数据库的全局锁。

B 在导出前还须说明内部引用不保留应用内跳转能力、图片/附件不打包。
这个真实界面行为尚未接入，不能用 D 返回成功代替提示验收。

## 3. API07 草案

`ExportRequest(context, note_id, destination_directory, file_name, unsaved_choice)`：
`note_id=None` 表示没有保存身份的新草稿，返回 `NO_SAVED_VERSION`；目标目录须绝对且已经存在。
`file_name=None` 使用已保存名称的默认名；自选名无扩展名时才补 `.md`，
已有 `.md`/`.MD` 保留，已有非 `.md` 扩展名拒绝；不静默清洗用户输入。

`SavedExportNote(context, note_id, saved_revision, collection_revision, saved_name, saved_body, references)`
由 C 的 `read_saved(context, note_id, cancel)` 提供。所有字段来自同次一致保存视图；
引用分类、名称及授权 UTF16 跨度来自共享解析器，不由 D 扫描正文重新发现。

`ExportReply` 返回路径、源身份、笔记身份、实际保存修订及集合修订、文件发布状态和可空保留候选路径。
候选路径是检查线索，不是删除权限。无保存版、请求错误或明确未发布时不填成功字段。

## 4. API08 与快照租约

请求/回复直接使用 [公共 backup.py](../../src/pmkb/contracts/backup.py)，未新增或修改字段。
`SnapshotSourcePort.capture_saved(context, cancel)` 返回 D 内部的：

```text
SnapshotLease(context, frozen_snapshot: bytes | Path, manifest, facts)
```

C 保证这是冻结的保存数据，不是活跃 SQLite 文件或草稿。`manifest`、独立检查的 `SnapshotFacts`、
实际捕获时刻和来源位置必须描述同一保存快照。捕获失败没有返回租约时，C 自行管理可能分配的资源。
D 在打包前检查元数据一致性，文件层再次检查实际打进 ZIP 的摘要/大小。

`source.release(snapshot)` 仅收尾 C 自有的冻结资源。D 从不因为租约是 `Path` 就自行删除它。
清理失败附警告，不撤销已确认的备份；发布未知也不重发、不覆盖、不交 SAVE 核实。
捕获结果丢失但尚未发布时，返回阻止状态，不伪造 UTC 时间、数量或成功回复。

版本允许集合、JSON/ZIP 字节预算必须显式传入，属于技术资源保护，不是产品笔记容量上限。
当前两条目格式与 `snapshotSha256`/`snapshotSizeBytes` 是待 A/C 确认的内部 profile；
没有引入 v3 固定毫秒、恢复时刻或倒序规则。

## 5. API09、C/A 端口与阶段

`RestoreRequest(context, source_file, destination_directory)` 使用绝对路径。
工作区父目录通过构造函数显式指定，须可信、已存在且不在恢复目标内部。
原来源位置仅供识别，服务不会访问 `manifest.sourceLocation`。

| 端口/报告 | 提供方 | 约束 |
|---|---|---|
| `validate_snapshot(candidate, cancel) -> SnapshotFacts` | C | 用 C 受控句柄检查实际模式、身份、笔记及引用派生事实；摘要一致不等于业务有效 |
| `prepare(destination, source_id, cancel) -> RestoreTarget` | A | 空位置保护，分配新且无已知冲突的集合身份；返回绑定位置和身份的所有权 token |
| `initialize_restore(candidate, target, cancel) -> InitializedRestore` | C | 在自有产物中初始化；保留对应笔记 ID/失效身份与历史名称，仅转换授权归属；不改输入候选 |
| `InitializedRestore` | C | 绑定目标 token、新 ID、源摘要及实际数量；集合修订为 1，每篇初始化计数一致 |
| `publish(target, initialized, cancel) -> RestorePublication` | A | 在最终边界重核空目标、归属与 READY 有效产物，再非覆盖发布一次；复用现有集合适配，不自动打开 |
| `release(...)` | C/A | 只清理本端口能确认归属的未发布额外资源；正式产物不得误删 |

`InitializedRestore` 是 C 的受控报告，不是自证数据库正确：C 仍须实测旧回执已移除、
集合级 `RESTORE_INIT`、逐篇 `RESTORE_NOTE_INIT` 及各篇 `lastCommitId`，不能把初始化回执当新 SAVE 证据。
D 的替身测试只检查调用次序、报告绑定和数量形状，没有验证真实 SQLite 行。

恢复结果 `RestoreReply` 区分 `PUBLISHED`、`PREPARATION_UNKNOWN`、`INITIALIZATION_UNKNOWN`、
`PUBLICATION_UNKNOWN`。只有最后一次发布确认成功才报告恢复产物已发布；准备/初始化未知不能说成已发布。
未知时保留相关候选与端口资源，不自动重试或清理；所报告候选路径不授权盲目删除。

## 6. 共同返回与取消规则

| 情况 | 对外结果 |
|---|---|
| 恢复入口请求类型、Context 或取消参数非法 | `ERROR / INVALID_ARGUMENT`，成功 value 为空；在服务锁、会话读取及文件/端口调用前拒绝，不向调用方抛这类参数异常 |
| 已知成功发布 | `OK`，真实来源/版本/数量或目标，错误为空；迟到取消不撤回 |
| 明确失败且未正式发布 | `ERROR`，错误及下一步，成功 value 为空 |
| 返回保存或发布前已确认取消 | `CANCELLED`，成功 value 为空，不自动保存 |
| 发布调用效果或回执无法确认 | `UNCONFIRMED`，已知位置/阶段及可确认事实，保留资源，不重发 |
| 清理失败但已知成功 | 保留成功，加警告，不能误报整次发布失败 |
| 调用任务被外层取消 | 设置取消意图并等待子任务实际结果，不让 future 丢掉在途效果 |
| 当前集合之后变化 | `delivery_context` 仍绑定原 Context，B 丢弃过期显示，不删除实际文件 |

端口的 `OutputFailure` 应使用文档化、已脱敏的公共错误，不暴露正文、SQL 或密钥。
格式坏、目标不符、裸字符串状态或自相矛盾的发布回执按未知处理，不能拿它作为清理凭据。

恢复请求的 Context 合法时，参数错误结果仍绑定该请求 Context，包括无活动集合的合法 Context；
请求或 Context 无法使用时，使用 `CallContext(None, 0)` 作为错误回包的安全占位，
不读取当前会话猜测来源。这不改变已经派发的恢复操作、未知结果保全或取消语义。

## 7. 复现与模块使用

仓库根目录，使用已安装项目依赖的虚拟环境：

```powershell
.\.venv\Scripts\python.exe scripts/check.py
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_d_*.py" -v
.\.venv\Scripts\python.exe scripts/verify_d_components.py
```

最后一条是 [可运行的合成组件示例](../../scripts/verify_d_components.py)：
它只在新建临时目录中执行真实导出、ZIP、候选和授权转换，验证后清理。
输出明确标注 `sqlite_validated=false` 和 `product_restore_executed=false`；
它不接受用户目录，也不构造假业务服务或宣称已恢复集合。

三个服务的可运行调用样例见相应测试中的受控端口替身：
[导出](../../tests/test_d_export_service.py)、[备份](../../tests/test_d_backup_service.py)、
[恢复](../../tests/test_d_restore_service.py)。生产组装时必须换成真实 B/C/A 端口，
不能把这些测试替身接入应用界面。

## 8. 交接前待共同确认

- B/C 确认 API07、会话选择证据和保存投影；B/C/A 确认 API09 与初始化/发布所有权。
- A/C 确认归档版本、清单扩展字段、资源策略和初始化报告/发布 token 的真实适配。
- A/D 对齐共用发布边界；当前新服务未替换 A 的现有适配，也未在 Desktop 默认组装中接入。
- 按 [故障索引](d-fault-index.md)与 [验收矩阵](d-validation-matrix.md)执行真实联调并填写观察记录。

这些未确认项属于 D04/D05，不把它们改写为“D 侧内部流程尚未实现”，也不单方宣称队友已经批准。
