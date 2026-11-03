# B 单篇 Markdown 导出接线审查

审查日期：2026-11-03。关联 [Issue #2](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/issues/2)、[PR #14](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/pull/14)。

审查对象为 robin6111210 于 2026-11-03 15:46:57（Asia/Hong_Kong）在 `feature/b-single-note-export` 发布的 `feat(editor): wire saved-only single-note Markdown export`，涉及 `desktop/app.py`、`desktop/editor.py` 和对应 Qt 测试。PR 由获授权 AI 根据已发布原分支代建以保留审查记录，原作者提交保留。

**技术结论：需修改默认导出名，暂不合并。** 请在原 `feature/b-single-note-export` 分支追加修复提交并推送，不需要重做。该 PR 由当前审查账号代建，使用 COMMENT 记录实际技术结论，不伪造自我批准或成员人工核查。

**最新复审：** 第 4 节记录默认名修复已通过本轮技术复审。第 1—3 节保留为首次审查历史，不再代表该问题尚未修复；实际合并状态以 PR #14 为准。真实 C 数据源默认接入和完整产品验收仍未完成。

## 1. 本次实际检查

- 当前 B 分支重新运行 `scripts/check.py`：**476 项发现、474 通过、2 项因 Windows 符号链接权限跳过，退出码 0**。这是 B 当前分支的结果，不是 C+B 合并结果；两边尚未合并，不将数字相加作为集成验收。
- 本次查询分支 [CI 41598609142](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/actions/runs/41598609142) 为成功。
- 独立离屏 Qt 探针实际核验：新草稿禁用导出；返回保存不打开选址/不读取源；其他脏草稿不阻塞当前干净笔记的单篇输出；读取期间编辑当前草稿阻止发布；切换集合不显示旧结果；退出等待读取结束并取消发布。
- 已有分支测试实际核验仅导出保存正文、选址期间编辑失效、已有目标不覆盖及草稿保留。
- 默认名探针复现下述偏差。重复在途点击会产生重复读取，但同目标安全发布保留原字节，本轮没有足够证据将其另列为缺陷。
- 尚无真实 C 导出源默认注入，默认入口不可用属于已声明的集成边界，本轮不将其报为已实现功能或代码缺陷。完整产品验收及真实阅读器验证未执行。

## 2. B-REV-01：P2，保存对话框默认名固定为 note.md

位置：[editor.py](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/blob/feature/b-single-note-export/src/pmkb/desktop/editor.py#L385)，`export_saved_note()` 原审查版本 385—387 行和 397 行。

保存对话框使用字面值 `note.md` 作为默认名，随后始终将选定文件名传给 D 的 `ExportRequest`。因此 D 已有的 `default_export_name(saved_name)` 分支不会生效，不能在服务层自动补回正确默认名。

独立离屏 Qt 探针加载有保存版本的笔记、确认导出并捕获 `getSaveFileName()` 的默认名称参数：

| 已保存名称 | FR09 / D2 要求的默认名 | 本次实际默认名 |
|---|---|---|
| 课程/笔记 | `note-课程_笔记.md` | `note.md` |
| CON | `note-CON.md` | `note.md` |
| 复习.md | `note-复习.md.md` | `note.md` |

上述规则见需求 FR09、D2 及当前设计 §6.3；不是新加的命名要求。

**修改建议：** 从可信保存版读取名称，复用既有 `default_export_name()`，再作为保存对话框的默认名。不能直接取当前草稿 `snapshot.name`：用户可能改名但尚未保存，而导出及默认名必须依保存版。获取保存名的异步过程要继续保留上下文、版本确认、取消和退出保护，不直接在 Qt 线程执行 SQLite，不隐式保存。

**回归要求：**

- 三个需求示例的默认名符合上表。
- 已保存名称为 A、草稿未保存改为 B 时，仍依 A 生成默认名，草稿保持未保存。
- 用户选择合法自定义名仍可使用；缺扩展名/无效名按既有规则处理，目标存在仍拒绝覆盖。
- 获取保存名或弹窗期间发生编辑、切换、取消、核实未知及退出时，不使用过期确认、不误发布，也不把草稿写入输出。
- 重新运行分支检查及针对性 Qt 回归，提供本次实际结果，不能沿用本文数字。

## 3. 返修后交付

请按 B-REV-01 回复修改位置、测试命令、实际结果及未执行项，在原分支推送后复审。Issue #2 不因这批导出组件接线完成而关闭；C 保存/查询/解析、预览及 D 其他输出界面仍分别跟踪。

## 4. 默认名修复复审通过

返修由 vortex-227 于 2026-11-03 16:46:34（Asia/Hong_Kong）在 `fix/b-export-default-name` 追加，提交说明为 `fix(editor): default export filename to saved title (refs #14)`；该分支继承原 B 单篇导出提交。集成时保留 B 原实现和本次修复作者，不将工作重新归为审查者开发。

实现先异步取得保存投影，再用该投影的 `saved_name` 和既有 `default_export_name()` 提供对话框默认名；后续导出复用同一不可变捕获，避免命名与正文来自两次不同读取。等待和弹窗前后均核对选择与上下文，不隐式保存当前草稿。

**本轮实际验证：** 当前 main 与该修复的精确合并源码运行 `scripts/check.py`，477 项发现、475 通过、2 项因 Windows 符号链接权限跳过，退出码 0。此结果不包含尚未合并的 C 后端，不与 C 测试数相加作为产品验收。

独立离屏 Qt 探针 10 组通过：三个规定默认名正确；未保存改名不影响保存版命名；一次输出只读取一次保存投影；取消、无保存版、源错误和保存修订变化均不发布；读取/选址期间编辑、读取期间切换集合及退出取消均保持保护。草稿名称、正文及未保存状态未被隐式写入输出。现有非覆盖回归继续通过。

本轮独立 AI 技术复审未发现阻断项，B-REV-01 已解决。修复纳入原 PR #14 后，须等待其当前 CI 成功再按已有授权合并；不伪造自我 Approve。默认真实 C 数据源、预览及备份/恢复界面仍待接入，完整产品验收未执行，Issue #2 继续开放。
