# 第二波技术审查与返修清单（2026-11-04）

本记录来自维护侧按用户授权执行的 AI 技术检查，不是用户人工交叉复核或产品验收。第一波已合并；第二波按原分支追加修复，不要求重做。以下结果是本轮实际运行，不能将作者各自基线上的自检当作三支集成结果。

## 当前结论

| 交付 | 审查结论 | 入口 |
|---|---|---|
| D：保存版导出源取消与错误映射 | 当前 main+D 完整检查通过；待当前 PR CI 完成成功后合并 | [PR #27](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/pull/27) / `feature/d-export-fix` |
| C：引用面板、导出投影契约测试 | C-W2-REV-01 待修；暂不合并 | [PR #26](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/pull/26) / `feature/c-ref-export-verify` |
| B：保存版导出、引用展示与插入 | B-W2-REV-01 待修；暂不合并 | [PR #28](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/pull/28) / `feature/b-wave2-export-references` |

## C-W2-REV-01（P2）：跨集合测试仍断言旧异常类型

位置：`tests/test_c_export_projection.py::ExportProjectionTests.test_stale_context_is_rejected`。

实际将当前 main、C、B（包含 D 依赖）合并到私人源码快照后，`scripts/check.py` 在本例报错：D 的 `CExportSource.read_saved` 已按输出端口边界抛出 `OutputFailure`，测试仍用 `assertRaises(StorageFailure)`，并直接取 `.code`。C 文档本来就要求 D 完成这种映射，不能再以旧类型作为集成契约。

请 C 在原分支做最小修正：

1. 从 `pmkb.application.output_support` 导入 `OutputFailure`，本例断言该类型。
2. 断言 `error.exception.error.code == 'STALE_CONTEXT'`，继续验证跨集合拒绝，不能删除本例或改成宽泛异常捕获。
3. 更新 `docs/development/c-ref-export-verify.md` 对 D 取消/错误映射“仍待实现”的现状描述，区分此前自检与本轮组合结果。
4. 基于包含 D 修复的当前主线重新跑 10 项契约测试及全量检查，回复实际结果。

这是测试契约未跟进 D 新实现，不是要求 C 修改 D 负责的 `export_source.py`。

## B-W2-REV-01（P2）：刷新列表会让当前引用面板永久变空

位置：`src/pmkb/desktop/saved_panel.py::refresh_saved` 及新增 `refresh_relations` 的调用协调。

真实 Qt + 临时 SQLite 的独立复现：

1. 创建并保存目标 `target`；创建来源 `source`，插入目标的内部标记并保存。
2. 当前仍为 `source`，引用面板显示 `有效引用：target`。
3. 点击“刷新保存版 / 应用标签”，等后台任务和 Qt 排队结果全部结束。
4. 当前笔记身份未变，数据库 `saved_view(source).references` 仍为 1 条，但引用面板变为 0 条，后续不恢复。

根因：`refresh_saved` 清空面板并将 `_reference_query` 置空，只刷新笔记列表；没有重新取得当前笔记的保存版引用。普通搜索/标签筛选也走同一路径。用户会把空面板理解为当前笔记没有引用。

请 B 在原分支修复刷新协调：列表刷新、标签筛选、搜索不应使当前保存版关系永久丢失；保留或重新查询当前笔记，继续检查请求标识、上下文和集合版本。不能简单展示上一个笔记的旧关系。

需要真实 Qt 回归：

- 来源有有效/跨集合/缺失引用时，刷新列表和应用标签后当前关系仍正确。
- 切换到无引用笔记、全新草稿、另一上下文时正确清空。
- 人为延迟旧关系请求后再刷新或切换，旧结果不能覆盖当前面板。
- 引用插入只改草稿，保存前反向引用不变；继续保持本轮仅展示和插入、不开放导航。

## 本轮实际验证

| 检查 | 结果 |
|---|---|
| 当前 main+C+B（含 D）完整检查 | 638 项，635 通过、1 错误、2 项 Windows 符号链接权限跳过；62.484 秒，exit 1。唯一错误为 C-W2-REV-01 |
| 当前 main+D 完整检查 | 625 项，623 通过、2 项同类权限跳过；48.883 秒，exit 0 |
| Windows 原生 Qt 第二波脚本 | 引用插入/保存后展示、保存前无反向关系、只输出保存版、已有目标拒绝覆盖、脏输入保留、删除入口隐藏、运行时和存储关闭均通过 |
| 独立 Qt 刷新探针 | 实际复现 B-W2-REV-01；数据库关系未丢失，仅面板显示丢失 |

上述探针未修改 B/C/D 业务源码，也没有把测试失败改为跳过。私人原始证据目录：`D:\软件工程\.wave2-review-2026-11-04`。本公开文档已给出完整复现和修改要求，队友无需访问私人目录。

## 范围与验收边界

第二波仍以显式 `--export`、`--references` 开启；预览独立使用 `--preview`，只显示保存版。引用只展示/插入，不承诺跳转、改名删除联动；备份恢复继续延期。7 条人工验收和 V01–V16 完整产品验收未执行，未据此更新为通过；最终冻结后再统一重建交付包和验收材料。
