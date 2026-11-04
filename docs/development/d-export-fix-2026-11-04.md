# D 第二波单篇导出模块交接

开发基线为 `0c5af6644a53f027678dd1aea306800ef95e8c37`，分支为 `feature/d-export-fix`。本批对应 2026-11-04 的 D 导出修复交接，模块结果供 A/B 接线与集成检查使用。

## 接口行为

入口继续使用 `CExportSource(store).read_saved(context, note_id, cancel)` 和 `ExportService.export_note(request, cancel)`。

| 情况 | 导出源与服务的行为 | 文件结果 |
|---|---|---|
| 读取前取消 | 导出源返回 `None`，取消事件保持已设置，服务返回 `CANCELLED` | 保留已有目标 |
| 读取过程中取消 | 等待在途只读工作结束，丢弃读取结果，服务返回 `CANCELLED` | 保留已有目标 |
| 外层任务请求取消 | 使用既有 `run_io` 等待工作线程收尾，同时设置取消事件 | 保留已有目标 |
| 无保存版且未取消 | 导出源返回 `None`，服务返回 `ERROR / NO_SAVED_VERSION` | 保留已有目标 |
| 集合上下文不匹配 | 读取前返回 `STALE_CONTEXT` | 保留已有目标 |
| 存储读取失败 | 转换为 `OutputFailure`，保留原始 `code`、`reason`、`action` 和未知标记，服务通过 `AppError` 返回明细 | 保留已有目标 |
| 目标已存在 | 文件发布返回 `ERROR / TARGET_EXISTS` | 原字节保留 |
| 导出成功 | 从同一次 `saved_view()` 读取保存名称、正文、引用分类和版本，按既有发布机制输出 | 返回实际路径与保存版本 |

B 直接调用导出源时，返回后应先检查取消事件，再用 `None` 判断缺少保存版。现有桌面导出预读流程已经按此顺序处理。取消发生在保存版读取阶段时，没有文件发布效果。文件发布已经开始后的成功或未知结果继续依据发布回执处理。

未保存草稿通过输出会话的当前版本确认处理，源数据来自保存版。引用沿用 C 在同一只读事务中给出的 UTF-16 授权跨度及分类，D 使用既有渲染器转换。

## 实际检查

检查于 2026-11-04 在 Windows、本机 Python 3.12.5 和指定冻结基线上实际执行。

| 检查 | 发现 | 通过 | 跳过 | 测试耗时 | 退出码 |
|---|---:|---:|---:|---|---:|
| 新增 `tests.test_d_export_source` | 9 | 9 | 0 | 1.470 秒 | 0 |
| 既有 `tests.test_d_export_service` | 41 | 41 | 0 | 18.307 秒 | 0 |
| `scripts/check.py` 全量检查 | 605 | 603 | 2 | 测试 129.387 秒，整个命令 133.006 秒 | 0 |

全量检查同时包含语法编译、公共契约导入及依赖检查。两项跳过分别是实际 ZIP 源链接和恢复候选源链接检查，原因均为本机创建符号链接所需的 Windows 权限不足，错误码为 1314。

基线首先复现取消和错误码问题，最先编写的两项测试出现三处失败。修复后新增九项全部通过，包括导出源及服务外层任务取消等待、精确错误与未知标记保留、跨集合拒绝、缺少保存版，以及真实 SQLite、真实编辑草稿和真实文件发布。保存名称、正文、Unicode 与授权引用输出、未保存草稿、库中保存投影及已有导出文件字节均有对应断言。

既有服务回归覆盖文件发布开始后的已知成功和未知回执行为。代码复核完成，未发现本批阻断问题。原始全量检查输出位于本地 `artifacts/d-export-fix/full-check.log`，服务回归输出位于 `artifacts/d-export-fix/service-regression.log`。

复跑命令如下，使用安装了项目依赖的 Python，并令 `PYTHONPATH=src`。

```powershell
python -m unittest tests.test_d_export_source -v
python -m unittest tests.test_d_export_service -v
python scripts/check.py
```

## 集成交接

D 维护 `infrastructure/storage/export_source.py` 和导出模块。B 负责恢复导出按钮及未保存提示，A 决定合入范围并执行集成、界面检查和重新打包。

建议 A/B 接入导出界面并执行集成检查。本批结论为“开发自检可用，未完成正式验收”。FR11 备份恢复继续延期，七项核心人工验收由真实执行人留证。
