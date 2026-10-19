# D 独立组件续批：ZIP 写出、备份清单、恢复引用转换

日期：2026-10-18。关联工作包：[D / Issue #4](../tasks/D-export-backup-restore.md)。

本轮约定的三块独立组件均已实现并实际测试。另提供一层组合适配，检查清单与实际 ZIP
内容的摘要及大小是否一致。本轮不等于 API08/09 或整个 D 工作包已完成。
后续统一按 [D 工作总清单与实际进度](d-progress.md)更新；本文件仅记录这一批组件。

本地分支为 `feature/d-4-backup-components`，包含上一批 D 提交 `f4be9c4`，并合入
截至本轮开始时的 A 主线 `76aa583`；本地合并点为 `11cad30`。上一批对应
[PR #7](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/pull/7)。
本轮已获本人授权按功能分批提交、推送及建立续批 PR；交付实际链接统一记在
[D 总进度表](d-progress.md#4-最近检查与-git-交付状态)。不更改远端 main，不代填任何人的批准。

## 1. 交付入口

| 组件 | 源码及主要入口 | 已完成的独立能力 |
|---|---|---|
| ZIP 写出 | [zip_writer.py](../../src/pmkb/infrastructure/file_io/zip_writer.py)：`ArchiveSource`、`publish_zip` | 从不可变字节或已冻结文件流式构建 ZIP；关闭后回读检查；非覆盖发布 |
| 备份清单 | [manifest.py](../../src/pmkb/application/backup_restore/manifest.py)：`encode_manifest`、`decode_manifest`、`check_manifest_against_facts` | 严格 JSON、字段/版本检查，以及对调用方提供事实的比较 |
| 恢复引用转换 | [references.py](../../src/pmkb/application/backup_restore/references.py)：`restore_reference_ownership` | 只转换已授权跨度中源集合引用的归属，保留目标身份与失效分类 |
| 组合适配 | [package_components.py](../../src/pmkb/application/backup_restore/package_components.py)：`publish_package`、`inspect_package` | 将清单与已冻结内容组合；发布前检查实际摘要/大小；读取时捕获小清单并流式检查快照条目 |

这些类型是 D 内部辅助类型，不是 A/C 已确认的共同契约。本轮没有改写共同 contracts、
需求、C 的数据库与解析器、B 的界面或 A 的集合协调代码；没有新增第三方依赖。

## 2. ZIP：先完整验证，再尝试一次发布

调用方必须提供已冻结的保存版数据。`Path` 不是快照证明，不能把正在使用的 SQLite
文件直接交给本适配器，就声称取得了一致备份。大文件按块读取，不整库读入内存。

ZIP 条目允许列表、必需项及资源预算由调用方显式提供。预算属于技术资源保护，
不新增产品笔记容量限制。实现拒绝路径穿越、重复或大小写/Unicode 歧义名称、
链接/特殊文件、源目标混用、超预算和可观察的源文件变化。

构建文件在目标同目录独占创建。条目写完后，适配器关闭 ZIP、flush/fsync、关闭文件，
再调用既有只读检查器，核对 CRC、实际 SHA-256 及调用方指定的大小。只有这些检查通过，
适配器才调用上一批的非覆盖发布器。目标若已存在或被并发创建，原文件保持不变。

| 结果 | 处理规则 |
|---|---|
| `OK` | 已确认发布完成；迟到取消不删除正式结果。该结果不证明数据库业务有效 |
| `ERROR` | 构建、检查或已知未发布的操作失败；不覆盖原目标 |
| `CANCELLED` | 发布前观察到取消；只尝试清理本次仍可确认归属的构建文件 |
| `UNCONFIRMED` | 发布效果或整个发布回执无法确认；保留必要候选，不自动重试或删除可能正式的文件 |

构建暂存与发布候选是两个不同文件。清理操作只针对可正面核对身份的自有文件，
不递归删除、不清扫其他临时文件。无法安全清理时，结果带有保留警告。
只读 ZIP 回读尚不支持中途取消；取消请求在该阶段结束后检查，随后不会继续发布。

父目录及源文件应来自可信本地环境。本实现不承诺抵御恶意祖先目录替换、
伪造旧文件元数据的并发篡改或所有文件系统的掉电行为；摘要也不是来源认证。

## 3. 清单：明确事实来源，不自证数据库正确

现有 [Python 设计](../design.md) 的备份要求与
[历史设计的清单字段说明](../archive/design-csharp-v2.md)提供字段依据；不沿用旧 C# 接口。
本轮内部格式使用 `manifest.json` 与固定 `snapshot.sqlite` 条目。

| 字段组 | 本轮含义 |
|---|---|
| `formatVersion`、`schemaVersion` | 只接受调用方显式允许的正整数版本；拒绝未知版本，不自动迁移 |
| `snapshotEntry` | 严格为 `snapshot.sqlite` |
| `sourceCollectionId`、`sourceCollectionRevision` | 源身份与捕获版本；须和独立验证的快照事实一致 |
| `sourceLocation` | 历史 Windows 路径，仅作展示/识别；恢复过程不得据此打开源目录 |
| `backupCapturedAtUtc` | 由快照提供方给出的实际捕获时刻，不用打包时刻冒充 |
| `savedNoteCount`、`referenceCounts` | 保存笔记数与四类引用出现次数；引用次数不是唯一目标数，也不必小于笔记数 |
| `snapshotSha256`、`snapshotSizeBytes` | 本轮内部技术扩展，用于绑定清单与实际条目；仍需 A/C 确认正式字段与版本 |

JSON 解码拒绝非法 UTF-8、BOM、重复键、缺字段、未知字段、非有限数、bool/float
冒充整数，以及超预算内容。清单时刻使用 UTC `Z`，支持零至六位小数；更高精度拒绝，
不静默截断，也不引入未同步的 v3 固定毫秒规则。调用方预算不是严格内存分配上限。

`SnapshotFacts` 必须来自 C 独立检查的模式、身份、保存数据及实际字节测量。
把清单声明复制成 facts，只能测试类型比较，不能证明快照正确。
`inspect_package` 不打开 SQLite；其 `supplied_facts_checked` 只表示执行了所提供事实的比较。
检查报告不授权稍后不经复核地重新打开同一路径进行恢复。

## 4. 恢复转换：只改授权归属，不修复失效身份

输入必须来自 C 的共享解析器和同一保存快照，带原文、UTF16 跨度及四类分类。
转换函数不另写 Markdown 解析器，不读写数据库，也不生成恢复初始化回执。

| 输入分类 | 文本转换 | 必须保留的状态 |
|---|---|---|
| 本集合有效引用 | 将源集合 ID 换为新集合 ID，笔记 ID 不变 | 有效目标身份 |
| 本集合已删除引用 | 同上 | 删除分类与历史名称，不复活 |
| 本集合缺失引用 | 同上 | 缺失分类与历史名称，不按名字重新绑定 |
| 外来集合引用 | 原样保留 | 跨集合失效，不降级按局部 ID 查找 |

函数拒绝畸形 Unicode、非法/重叠跨度、与原文不符的标记、分类与归属冲突、
新旧集合 ID 相同，以及新 ID 撞上输入中已知的外来引用集合身份。
转换后的标记长度不变。未在授权跨度中的代码示例、图片、普通链接及其他文字保持原样。
这不证明调用方的跨度完整、分类正确或新 ID 在全局未被使用；仍须由 C/A 验证。

## 5. 实际检查与可复核入口

本机 Windows 11 `10.0.26200`，标准 CPython `3.12.5`，项目虚拟环境的
PySide6-Essentials/shiboken6 均为 `6.12.0`。本轮没有升级依赖或更改系统权限。

| 测试组 | 发现用例 | 通过 | 跳过 |
|---|---:|---:|---:|
| 原 A/D 合并基线（含上一批 D 与 A 后台运行） | 138 | 137 | 1 |
| [ZIP 写出](../../tests/test_d_zip_writer.py) | 36 | 36 | 0 |
| [清单编解码与事实比较](../../tests/test_d_manifest.py) | 27 | 27 | 0 |
| [恢复引用纯转换](../../tests/test_d_restore_references.py) | 27 | 27 | 0 |
| [本轮组件组合](../../tests/test_d_backup_components.py) | 10 | 10 | 0 |
| 合计 | 238 | 237 | 1 |

`scripts/check.py` 实际成功退出：编译、共同契约导入、依赖检查及全部测试均通过。
唯一跳过项沿用上一批 ZIP 检查测试：当前 Windows 无创建文件符号链接的特权，
返回 `WinError 1314`。该项不能记作通过；ZIP 元数据链接条目的拒绝测试已执行。

三个组件均先运行缺少实现时的失败测试，再完成实现。AI 交叉审查发现并修复了版本集合
先去重可能隐藏 bool/float 的问题；ZIP 故障测试还复现并修复了发布整体回执丢失时
可能误报 `ERROR` 的问题。修复后重新执行检查，未发现剩余阻断问题。
这些是实际 AI 技术审查，不是 A/B/C 的人工批准。

全部用例仅用合成文本、虚构路径与临时文件。组合用例中的快照载荷明确不是 SQLite 库。
[清单样例](../../tests/test-data/d-backup/manifest-example.json)只演示格式；其中摘要与大小
不是某个真实数据库的测量结果。未上传真实笔记、个人数据库、凭据或课程提交包。

仓库根目录复核：

```powershell
.\.venv\Scripts\python.exe scripts/check.py
```

## 6. 已完成、独立待办与联调的分界

本组件批的三块独立实现、组合验证与对应测试已完成，但组件批结束时仍缺 D 自身的
应用服务、恢复文件流水线和交接包。它们已在后续连续开发中补齐可独立部分，
最新状态和检查统一见 [D 总清单](d-progress.md)，本文保留组件批的历史统计。后续交付为：

- D01：API07 导出、API08 备份、API09 恢复的 D 侧编排、接口草案及测试替身验证。
- D02：恢复归档的安全流式提取、自有候选准备及取消/失败保护。
- D03：公共故障索引、需求—测试/验收对照及使用交接说明。

上述独立交付不替代真实业务链路，仍需：

- C 提供一致保存快照、共享引用解析分类、库内事实检查及受控恢复初始化。
- B 接入未保存选择、目标选择、取消和失败/未知结果反馈。
- A/B/C/D 确认正式清单格式及相关接口；A 对接新身份登记、显式打开及激活。
- 各方共同验证完整 V07/V08/V09/V16。本轮没有执行这些产品验收，不关闭整个 Issue #4。

## 技术依据

- [Python 3.12 zipfile](https://docs.python.org/3.12/library/zipfile.html)：流式条目写入与关闭行为。
- [Python 3.12 json](https://docs.python.org/3.12/library/json.html)：JSON 编解码接口。
- [Python 3.12 datetime](https://docs.python.org/3.12/library/datetime.html)：UTC 时间对象及精度边界。

产品规则以仓库需求与共同确认的设计为准，官方资料与测试不替代跨模块确认。
