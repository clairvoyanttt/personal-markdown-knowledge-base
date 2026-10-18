# Personal Markdown Knowledge Base

Windows 11 个人本地 Markdown 笔记与知识管理工具，面向学生与知识工作者，支持记录、组织、查找、关联和输出。

## 当前开发基线：Python

2026-10-18用户确认改用 **Python 3.12+ / PySide6 Qt Widgets / SQLite**，保持第一关第二版需求和业务范围；第一关v3暂不混入。C#方案已转为历史参考，未发布的C#骨架不保留在当前源码。

目前可安装运行的是**开发启动壳**：公共类型契约、活动上下文保护、UTF16跨度转换和基础检查已具备；编辑保存、数据库、搜索引用、导出备份恢复尚未实现。基础检查不等于产品验收，V01—V16完整产品验证仍未执行。

## 队友先看这些

- **[第一次接手：操作清单与注意事项](docs/development/notes.md)**
- [四人计划与分工](docs/team-plan.md)：按依赖尽快推进，不等待原拟定五周。
- **[B：界面与编辑保存](docs/tasks/B-ui-editing.md)**
- **[C：存储、检索与引用](docs/tasks/C-storage-search-references.md)**
- **[D：导出、备份与恢复](docs/tasks/D-export-backup-restore.md)**
- [A：当前进度和后续集合任务](docs/tasks/A-baseline-integration.md)
- [开发注意事项](docs/development/notes.md)、[Git协作](CONTRIBUTING.md)
- [接口责任](docs/development/contracts.md)、[v3待同步](docs/development/pending-requirement-sync.md)

## 安装、检查、启动

Windows11安装标准CPython 3.12（含pip），建议使用Python官方安装或Python Launcher；某些MSYS Python不含pip或不兼容Windows Qt轮子。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts/check.py
.\.venv\Scripts\python.exe -m pmkb
```

若editable安装提示缺setuptools，先执行`python -m pip install "setuptools>=69"`；或使用普通`pip install -e .`自动准备构建依赖。[详细上手](docs/development/getting-started.md)。未激活虚拟环境也能按上述完整路径运行。

## 需求、设计和模块

- [需求第二版](docs/requirements.md)：FR01—FR11、FR16、NFR01—NFR06共18条，15高/3中，原需求文件不改写。
- [当前Python开发设计](docs/design.md)：技术切换及接口/运行/数据规则。
- [C#设计历史参考](docs/archive/design-csharp-v2.md)：保留原个人设计来源，不当作当前代码接口。
- [实际基础检查记录](docs/development/baseline-verification.md)：只记录真实检查，完整产品验证另行执行。

| 负责人 | 源码入口 |
|---|---|
| A 集合协调/集成 | `src/pmkb/application/collection`、`scripts` |
| B 界面/草稿保存 | `src/pmkb/desktop`、`application/editing` |
| C 规则/解析/查询/数据库 | `domain`、`application/parsing`、`application/query`、`infrastructure/storage` |
| D 导出/备份恢复/文件 | `application/export`、`application/backup_restore`、`infrastructure/file_io` |
| 共同契约 | `src/pmkb/contracts`，改动需提供方及调用方共同核对，最终审查由A负责 |

需求和原设计由A个人完成，不记为团队共同历史成果。B/C/D账号和权限待关联，不代替成员认领或审核。首版不含同步、移动端、插件/主题、多人协作、AI/OCR、自动保存或集合合并；团队Git协作不等于产品协作功能。

不上传个人笔记、本地库、密钥、构建产物、私人记录或课程ZIP。没有声称分支保护已开启；CI定义的实际运行状态以Actions为准。所有PR由A统一人工审查后决定是否合并main，包括A自己的PR；本次流程调整不代表启动PR #5已获批准。
