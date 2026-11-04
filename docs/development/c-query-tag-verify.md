# C：标签查询与引用查询验证报告

- 日期：2026-11-04
- 分支：`feature/c-query-tag-verify`，基线 `0c5af6644a53f027678dd1aea306800ef95e8c37`
- 定位：**查询/存储侧开发自检**，不是产品验收，也不代表人工验收通过

## 1. 结论摘要

| 检查点 | 结论 |
|---|---|
| 标签筛选 AND、只查保存版 | 通过。`search(keyword, tags)` 与新增的 `summaries(tags=...)` 都要求全部标签命中 |
| `summaries()` 的标签 | 原先不返回标签；本分支补上 `NoteSummary.tags`（已保存标签，按标签排序） |
| 标签大小写 | 区分：`AI` 与 `ai` 是不同标签，筛选不折叠 |
| 查询侧裁剪 | 改为**不做隐式裁剪**：`" AI "` 直接 `INVALID_TAG`，要求调用方先用统一规则规范化 |
| 组合与空结果 | 关键词 + 标签同时生效；无匹配返回空元组/空结果，不抛异常 |
| 集合隔离 | 通过：同标签/同关键词在另一集合不串结果 |
| 引用/反向引用 | 已保存版本上可查；跨集合标记、代码内标记不产生本地反向引用；删除目标仍是 `DELETED` 且保留历史名 |

**给 A 的建议**：可以恢复标签筛选（B 侧已有非 MVP 路径，可改用 `summaries(tags=...)`）；
引用/反向引用面板可以恢复，但需限定“只显示已保存、目标有效的本地引用”，并把失效原因按文字展示。

## 2. 本分支的代码改动（最小）

| 文件 | 改动 |
|---|---|
| `infrastructure/storage/reader.py` | `NoteSummary` 增加 `tags`；`list_summaries(..., tags=...)` 支持 AND 筛选；`search_notes` 结果带标签；`_filter_tags` 改为只校验不裁剪（`" AI "` → `INVALID_TAG`） |
| `infrastructure/storage/store.py` | `summaries(tags=...)` 透传筛选参数 |
| `application/query/service.py` | `SearchHit` 增加 `tags`，搜索命中带上已保存标签 |

未修改 `desktop/*`、`export_source.py`、`README.md`、`docs/requirements.md`；未合并 C PR #21。

## 3. 逐项检查点与实际行为

### 任务 1：标签筛选语义

1. **AND 语义（通过）**
   `search(keyword="知识管理", tags=("AI", "课程"))` 只返回同时具有两标签的笔记；
   `summaries(tags=("AI", "课程"))` 同样要求两者都在。
   测试：`TagFilterTests.test_search_combines_keyword_and_all_tags`、`test_summaries_tag_filter_is_and_and_case_sensitive`。
2. **`summaries()` 返回已保存标签（本分支新增并通过）**
   `NoteSummary.tags` 来自 `NoteTags` 的保存行；无标签笔记为 `()`；删除后不再出现。
   测试：`test_summaries_expose_saved_tags_only`、`test_tag_filter_uses_saved_state_after_update_and_delete`。
3. **规范化与查询侧边界（通过，含一处行为变更）**
   保存侧仍用统一入口 `normalize_tags`（首尾空白、重复、空标签、控制字符、30 码点、20 项）；
   查询侧原来会静默 `trim`，现在改为只校验：`" AI "` 不被接受，必须传已保存的 `"AI"`；
   标签大小写不做折叠（`AI` ≠ `ai`）。
   测试：`test_summaries_filter_has_no_implicit_trim_or_fold`、`test_tag_filter_is_case_sensitive_and_all_tags_must_match`。
4. **组合与空结果（通过）**
   关键词与标签同时生效；无匹配返回空 `hits`/空元组，不抛异常。
   测试：`test_search_combines_keyword_and_all_tags`、`test_no_match_is_a_distinct_empty_result`。
5. **集合隔离（通过）**
   两个集合同名标签、同关键词互不串结果。
   测试：`test_tag_filter_is_collection_scoped`、`test_search_is_scoped_to_one_collection`。

### 任务 2：引用/反向引用查询

1. **返回内容与分类（通过）**
   `saved_view(note_id).references` 给出跨越的引用分类 `VALID`/`DELETED`/`MISSING`/`FOREIGN_COLLECTION`
   以及目标名/历史名；`backlinks(note_id)` 只从 `RefOccurrence`（保存版派生行）读取。
   测试：`test_backlinks_and_valid_classification`、`test_deleted_target_keeps_historical_name_and_never_rebinds`。
2. **未保存引用不进入结果（按存储边界成立）**
   反向引用只读 `RefOccurrence`，该表只在 SAVE 事务内重建；B 的内存草稿不写库，因此未保存标记不会出现。
   本次未做“B 草稿对象 → 存储”的伪联调，这一点按 B 的编辑服务语义保证。
3. **跨集合与代码内标记（通过）**
   外来集合的完整标记不建立本地反向引用；围栏代码内的同形文字不产生引用。
   测试：`test_foreign_marker_creates_no_backlink_in_this_collection`、`test_backlinks_ignore_markers_inside_code`。
4. **删除目标（通过，供面板设计参考）**
   删除后仍可用 `backlinks(target)` 查到已保存来源，分类为 `DELETED` 并保留历史名；
   面板若只展示“目标有效”的反向引用，需要在 UI 侧按 `state` 过滤或明确显示失效原因。

## 4. 已知限制（供 A 决策）

- 反向引用查询目前没有“只返回目标有效来源”的过滤参数；`backlinks()` 返回所有已保存来源。
  B 如果恢复引用面板，建议先展示全部并标注状态，或由 C 增加 `valid_only` 参数（未在本分支实现，避免提前定契约）。
- `summaries(tags=...)` 目前是 Python 侧过滤（1000 篇规模可接受）；若后续列表需要分页/排序参数，另立契约。
- 标签筛选 UI 的输入规范化仍由 B 负责（`normalize_tags`）；C 的查询侧只接受规范化后的值。
- 本报告只覆盖 C 的查询/存储路径，不含 Qt 界面、导出、备份恢复。

## 5. 执行命令与结果

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_c_query -v
.\.venv\Scripts\python.exe scripts/check.py
```

- `tests.test_c_query`：**29 项通过**（基线 19 项 + 本分支 10 项）。
- 全仓 `scripts/check.py`：**606 项发现、604 通过、2 项因 Windows 符号链接权限（1314）跳过，
  48.5 秒，exit code 0**。跳过的 2 项属于 D 的符号链接用例，不计为本分支通过。
