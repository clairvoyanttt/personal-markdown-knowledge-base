# C：第二波引用/导出接口确认

- 日期：2026-11-04
- 分支：`feature/c-ref-export-verify`，基线 `862c356`（第一波已合并的 main）
- 范围：FR06/FR07 引用与反向引用的**查询契约确认**、FR09 单篇导出的**已保存投影确认**
- 定位：组件级真实 SQLite 自检，不是产品验收；不改 B/D 的 UI 与 `export_source.py`

## 1. 结论

| 功能 | 结论 |
|---|---|
| FR06 引用查询 | 通过。`saved_view()` 提供 B 面板所需全部字段：`state`、`target_name`、`marker_text`、`reason`、`navigable`、`target_note_id` |
| FR07 反向引用 | 通过。`backlinks()` 只从保存版派生行读取，目标/来源/跨集合边界符合预期 |
| 引用插入 | 通过。B 用 `format_reference_marker(collection_id, note_id)` 插入后保存，读回为 `VALID` 且目标出现反向引用 |
| FR09 导出投影 | 通过。`SavedExportNote` 由 C 的一致视图映射，D 的 `_projection` 接受，四类引用渲染为 FR09 要求的文本 |
| 取消/错误映射 | **仍需 D 修**：`export_source.py` 归 D，当前直接抛 `asyncio.CancelledError` / `StorageFailure`，需要映射为 D 的 `OutputFailure` 语义 |

## 2. 引用/反向引用契约（给 B）

B 的 `saved_panel` 现在按下列字段渲染，C 侧全部提供：

| 字段 | 来源 | 说明 |
|---|---|---|
| `references[i].state` | `SavedNoteView.references` | `VALID` / `DELETED` / `MISSING` / `FOREIGN_COLLECTION` |
| `references[i].target_name` | 同上 | 有效=当前保存名；删除=历史名；缺失/外来=`None` |
| `references[i].marker_text` | 同上 | 完整 `[[note:<32hex>:<32hex>]]`，可直接展示或复制 |
| `references[i].reason` | 同上 | 已含中文原因文本（“目标已删除，无法跳转。”等） |
| `references[i].navigable` | 同上 | 仅 `VALID` 为 `True` |
| `references[i].target_note_id` | 同上 | 仅有效目标可跳转 |
| `backlinks[i].note_id/name/revision` | 同上 | 已保存来源笔记，按名称排序 |

行为确认：

- 插入：`format_reference_marker(...)` → 保存 → 读回 `VALID`、`navigable=True`，目标 `backlinks` 含来源。
- 跨集合：外来标记 `FOREIGN_COLLECTION`、不可跳转、无 `target_name`、不产生本地反向引用。
- 删除后：`DELETED`，保留历史名，不可跳转；同名重建不会复活旧引用（新笔记反向引用为空）。
- 代码内：围栏/行内代码中的标记不建关系，也不出现在导出引用里。
- 未保存：编辑器中插入的标记只在内存，`saved_view`/`backlinks` 看不到；保存后才出现。

**给 B 的两个建议（不改 C 接口）**：

1. 面板当前展示 `ref.state` 原值（`VALID:` 等英文），建议映射成中文“有效/目标已删除/目标不存在/来自其他集合”，NFR04 的可读性更好。
2. 如果要隐藏失效引用，只需按 `navigable`/`state` 过滤；C 暂不提供 `backlinks(valid_only=)`，避免为 UI 细节提前定契约。

## 3. FR09 导出投影（给 D）

C 提供的数据（`saved_view` 映射为 `SavedExportNote`）：

- `saved_name`、`saved_body`、`saved_revision`、`collection_revision`、`note_id`
- `references[i]`：`start_utf16`、`length_utf16`、`marker_text`、`kind`（与 `ReferenceState` 取值一致）、`target_name`

确认结果：

- D 的 `_projection()` 接受该对象；`render_export_markdown` 对四类引用输出 FR09 规定文本，
  目标名中的 Markdown 特殊字符处理仍由 D 的渲染器负责。
- 代码内标记不进入引用；无保存版返回 `None`（CB01）；过期上下文拒绝。
- 跨度是 UTF-16 半开区间，切片等于完整标记。

**D 仍需处理**（本 PR 不改 `export_source.py`）：

- `read_saved` 取消时当前直接抛 `asyncio.CancelledError`，错误时抛 C 的 `StorageFailure`；
  需要按 D 的端口约定映射为 `OutputFailure`/取消语义，避免被当成未知失败。
- 若需要“导出取消后不覆盖已有文件”，仍由 D 的发布器保证；C 只读。

## 4. 已知限制

- `backlinks()` 没有 `valid_only` 参数；如 UI 要只显示有效来源，由 B 侧按状态过滤。
- `saved_view()` 会同时读取反向引用（同一只读事务，成本可忽略）；导出若只需要引用，仍用同一入口，避免多开读取。
- 未保存引用不进入任何保存版查询，这是设计边界，不是缺失。
- 本报告不含 Qt 人工验收、导出文件写入/拒绝覆盖验证（D 负责）。

## 5. 执行命令与结果

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_c_reference_ui_contract tests.test_c_export_projection -v
.\.venv\Scripts\python.exe scripts/check.py
```

- 新增契约测试：**10 项通过**（引用面板 6 项、导出投影 4 项）。
- 全仓 `scripts/check.py`：**624 项发现、622 通过、2 项 Windows 符号链接权限（1314）跳过，
  65.2 秒，exit code 0**。跳过的 2 项属于 D 的符号链接用例，不计为本分支通过。
