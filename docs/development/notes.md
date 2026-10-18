# 队友接手须知：从拉代码到提交第一个PR

更新日期：2026-10-18。适用于A、B、C、D，尤其供B/C/D第一次接手使用。

**先完成三件事：取得仓库权限 → 拉取Python启动分支并运行检查 → 认领自己的Issue。** 按依赖尽快推进，不等待原拟定五周。

## 1. 先认清目前有什么

项目是Windows11上个人本地、离线的Markdown笔记工具。当前采用 **Python3.12 / PySide6 Qt Widgets / SQLite**，不是旧C#或WPF方案。

已有：Python工程结构、API01/02/08/11公共类型、活动上下文保护、UTF16跨度转换、开发启动窗口和基础检查。基线发布时，本地15项基础检查已通过；这不代表完整笔记功能已经实现。

尚未实现：完整集合生命周期、编辑保存、SQLite业务存储、搜索/引用、导出备份恢复。V01—V16完整产品验收均未执行。窗口暂时只有说明文字是正常现状，不要为了演示伪造保存成功或显示假数据作为正式结果。

需求与原设计由A个人先完成，不是四人共同完成的历史成果。团队现在开始在共同基线上实现；实际进度以Issue、源码和真实检查记录为准。

## 2. 先确认权限，再开始修改

仓库：https://github.com/clairvoyanttt/personal-markdown-knowledge-base

把自己的GitHub用户名发给A，由A协调仓库权限，并由本人接受邀请。本文件不表示B/C/D已经获得权限。不把密码、令牌或个人笔记发给组员，也不把凭据写进代码。

公开仓库可以读取，不代表你能推送分支。权限未开通时可以先在本地阅读、安装、测试，告知A权限问题；不要擅自换一个仓库作为共同开发位置。

## 3. 第一次获取项目：用正确分支

本次启动代码在 **`chore/a-team-baseline`**，通过[PR #5](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/pull/5)申请合并到main。发布时尚待非作者组员审查，不能默认main已经有这些代码。

没有本地仓库时，在自己准备放项目的父目录运行：

```powershell
git clone --branch chore/a-team-baseline https://github.com/clairvoyanttt/personal-markdown-knowledge-base.git
cd personal-markdown-knowledge-base
```

已经克隆时，先保存自己的改动；工作区有未处理修改时，不要直接切分支、reset或覆盖文件。工作区可安全切换后：

```powershell
git fetch origin
git switch chore/a-team-baseline
```

如果本地还没有该分支，可用：

```powershell
git switch --track origin/chore/a-team-baseline
```

不必重复运行两种switch命令。若A已通知PR合入main，改用`git switch main`与`git pull --ff-only`取得集成代码；启动分支以后可能被删除，以A明确通知和PR实际状态为准，不从旧ZIP复制文件。

## 4. 安装、检查、启动：先跑通再开发

建议Windows11＋标准CPython3.12（含pip）＋Git。避免误用不带pip的MSYS Python。以下命令都在仓库根目录执行，无需激活虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts/check.py
.\.venv\Scripts\python.exe -m pmkb
```

最后一条会打开开发说明窗口，关闭窗口后回到终端。第一次安装依赖需要网络；这不代表最终产品需要联网。不要把`.venv`交给其他成员，每个人在自己电脑创建。

判断标准：安装命令正常结束，检查脚本返回成功，窗口能打开/关闭。若失败，先记录错误，不要写“环境没问题”。检查脚本验证语法、公共契约导入、依赖和基础单元/启动检查，不是完整产品验收。

| 现象 | 先这样处理 |
|---|---|
| 找不到`py` | 检查标准Python3.12是否安装；可用正确解释器执行`python -m venv .venv`，不要盲目切换版本 |
| 找不到pip或Qt没有兼容安装包 | 核对是否误用了MSYS等非标准Python；把`python --version`和解释器路径发给A |
| `No module named pmkb` | 确认在正确分支/目录，并用虚拟环境Python重新执行`pip install -e .` |
| 找不到requirements.txt、src或scripts/check.py | 先确认是否停在尚无基线代码的main，不要自己创建同名空文件应付 |
| 安装下载失败 | 记录网络/安装错误，与A协调；不随机删改版本或关闭证书校验 |
| 没有推送权限 | 让A协调账号权限，不借用别人的令牌 |
| 只有启动窗口，没有编辑器 | 当前正常，业务模块正是接下来要实现的内容 |

## 5. 各自从哪里接手

先在对应Issue留言确认认领；下面只给出工作包归属，不替本人填写已开始。

| 成员 | Issue及先读的任务 | 先交哪一小块 | 主要对接 |
|---|---|---|---|
| A | [#1 启动基线](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/issues/1) | 后续API01集合编排、必要配置登记与集成 | B草稿、C集合验证、D文件发布 |
| B | [#2 界面编辑保存](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/issues/2)；`docs/tasks/B-ui-editing.md` | 内存草稿及编辑状态、Qt界面，再接手动保存 | C提供事务/查询/解析，A提供离开集合流程，D提供输出服务 |
| C | [#3 存储检索引用](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/issues/3)；`docs/tasks/C-storage-search-references.md` | 最小事务/保存快照的契约和输入规则，优先解除B/D依赖 | B保存与查询、D快照与初始化、A模式/身份 |
| D | [#4 导出备份恢复](https://github.com/clairvoyanttt/personal-markdown-knowledge-base/issues/4)；`docs/tasks/D-export-backup-restore.md` | 文件发布契约、导出期望样例，再接保存快照备份 | C快照/解析、B选择与反馈、A恢复后打开 |

源码入口：

- B：`src/pmkb/desktop`、`src/pmkb/application/editing`。
- C：`src/pmkb/domain`、`application/parsing`、`application/query`、`infrastructure/storage`，后四项均在`src/pmkb`下。
- D：`src/pmkb/application/export`、`application/backup_restore`、`infrastructure/file_io`，均在`src/pmkb`下。
- A：`src/pmkb/application/collection`、检查和集成脚本。
- 共同类型：`src/pmkb/contracts`；测试：`tests`。自己的模块由自己补测试和说明，不把所有测试交给D或所有文档交给A。

## 6. 写代码之前先约定接口

已有完整Python类型/Protocol：API01集合、API02草稿保存、API08备份、API11提交核实/退出。它们目前只是调用契约，不是可以直接使用的完整服务实现。

API03删除、API04查询、API05引用、API06预览、API07导出、API09恢复、API10事务/快照的精确Python对象，由相应提供方和调用方一起补齐。先提小的契约PR，写明字段、类型、可空性、返回状态和样例，再并行实现，不各自发明一套字典。

日常对接请明确这六件事：

1. 谁提供、谁调用？
2. 输入来自草稿还是已保存版本？
3. 集合ID、`context_generation`、保存`base_revision`和编辑`edit_version`分别是什么？
4. 成功/失败/取消/部分完成/未知时，各返回什么？
5. 失败后数据、草稿、文件有哪些真实变化？
6. 调用方如何确认结果过期，不把旧集合结果放到当前界面？

桌面界面不直接SQL，纯规则和契约不依赖Qt。解析只有一套共享适配，B预览、C引用和D导出/恢复共同使用，不能各写一个互相不一致的标记解析器。

## 7. 这些业务规则必须保持一致

| 事项 | 要遵守的规则 |
|---|---|
| 草稿与保存版 | 编辑/预览用草稿；搜索、筛选、反向引用、导出、备份用保存版。名称/正文/标签/引用修改都需手动保存 |
| 保存期间又输入 | 成功只确认捕获版本，后来输入仍未保存；失败保留输入和原保存事实 |
| 取消与提交未知 | 取消协程/Event不等于回滚。COMMIT后先核实，未知返回UNCONFIRMED，不自动重写，不断言已保存/已回滚 |
| 回执 | 只接受本集合、本SAVE命令、本笔记与版本/内容匹配的证据；旧集合和恢复初始化回执不证明新SAVE；缺回执本身也不证明失败 |
| 名称/标签 | 名称100码点、标签30码点/每篇20项；去首尾空白、拒绝空值/控制字符；名称英文判重不分大小写并排除自身，标签区分大小写 |
| 字符与跨度 | Python按码点索引，保留的引用跨度按UTF16；使用明确转换，不把UTF16偏移直接切Python字符串，不切开代理对 |
| 引用身份 | 双段各32位十六进制；改名不换绑，删除失效，同名重建不复活，跨集合不按名字或局部ID回退；代码内标记不建引用 |
| SQLite | 连接归专属工作线程，每连接启用外键；正文/标签/引用/回执同事务更新。不能用check_same_thread=False掩盖并发 |
| 输出 | 只用已保存数据，先提示未保存；全新草稿无保存版时不导出，备份排除但仍提示。FR09的导出文本不等于应用内失效提示 |
| 备份恢复 | 既有文件不覆盖；恢复到空位置新集合，不自动打开，不改原集合/备份，只转换恢复产物的引用归属 |
| 发布/激活 | 已发布未激活的有效集合保留，不能当临时文件删除；只有实际激活才丢弃明确选择放弃的草稿 |
| 兼容与范围 | 不支持schemaVersion拒绝，不自动迁移；不加自动保存/同步/移动端/插件/多人协作等范围外功能 |

1000篇/每篇10KB只是性能测试规模，不是笔记容量上限。完整测试后才填写实际结果，基础窗口烟测、历史SQLite小样例或AI代码审查都不能当作产品验收。

第一关v3的精确UI文案、扩充验收和时间/恢复时刻规则尚未同步当前设计。当前遵循第一关第二版需求＋Python开发设计，不从旧聊天或其他提交包里抽取规则局部混入。

## 8. 日常Git流程：小任务、小分支、小PR

### 8.1 建分支

确认Issue和基线后，创建自己的短期分支。基线PR未合并时：

```powershell
git fetch origin
git switch -c feature/b-2-draft origin/chore/a-team-baseline
```

B示例是Issue #2的一个小任务。C可用`feature/c-3-storage-contract`，D可用`feature/d-4-export-fixtures`。若基线已合并，改从更新后的main创建：

```powershell
git switch main
git pull --ff-only
git switch -c feature/b-2-draft
```

不要在main或公共基线分支上直接做日常开发。已有修改先妥善保存，不用reset/强推解决不会处理的状态。

### 8.2 检查与提交

```powershell
.\.venv\Scripts\python.exe scripts/check.py
git status --short
git diff
# 只暂存自己已经检查的明确文件，不直接盲目git add .
# 示例：git add src/pmkb/application/editing/drafts.py tests/test_drafts.py
git diff --cached
git commit -m "feat(editor): add draft editing state (refs #2)"
git push -u origin feature/b-2-draft
```

注释中的文件是未来交付示例，当前并不存在，不要照着创建空文件。将命令里的分支名/Issue替换为自己的真实任务。

### 8.3 提PR和审查

在GitHub提PR，说明：目的、关联Issue、涉及功能/接口、实际检查结果、未执行项、对其他成员的影响。基线PR未合并时标明依赖；尽量先完成一个小闭环，不一次塞入整个模块。部分完成用`Refs #2`等引用，不用`Closes`提前关闭整项任务。

至少一名非作者组员审查，接口提供方和调用方共同核对。A负责一般合并；A的PR由D或其他非作者实际审查，A不能自审。按意见修改并回复位置/理由，阻断问题未解决不合并。合并后更新Issue并删除已合并分支。

冲突联系双方及维护人，解决后重跑检查；不覆盖别人的规则。共享错误追加修复，main回退用revert＋PR，不强推共享历史。已有CI入口，是否运行/通过以Actions实际状态为准，不表示分支保护已配置。

## 9. 提交前必须扫一遍内容

检查`.gitignore`和暂存差异。禁止上传：真实个人笔记、密钥/令牌、个人配置、本地数据库及日志、`.venv`、缓存/构建产物、私人协作记录和课程提交ZIP。测试只用合成数据或生成脚本。

新增包先和受影响成员确认用途，固定版本并记录许可证；不自行换语言/界面框架/数据库，不引入远程服务。开发安装可联网，产品核心运行必须离线；不自动加载远程图片、脚本或HTML。

不要改写别人的提交历史。敏感信息误推后立刻告知A并撤销密钥，只删除文件/revert不代表历史泄露已清除。

## 10. 遇到阻塞怎样报，做到什么才算交付

发一条简短记录即可：

```text
成员/Issue：B，#2
正在做：内存草稿与编辑状态
已完成：具体代码/文档及所在分支
检查：执行命令、实际结果；没跑的明确写未执行
需要协作：需要C确认事务返回中的版本字段
下一步：接口确认后接入保存；当前不承诺完成时间
```

不要把完整错误堆栈里的密钥、私人路径或真实笔记贴到公开Issue。可以提供经过清理的报错、Python版本和复现步骤。接口阻塞及时通知对接人及A，必要时启用备份负责人：A由D备份、B由A备份、C由B备份、D由C备份。

一个小任务交付至少包含：实际源码/文件、对应行为测试或真实人工检查、未执行项、调用方影响说明，以及非作者审查。跨模块功能还要联调，不能只凭函数能导入或窗口能打开就关掉整个Issue。

## 11. 如使用AI辅助，可复制以下说明

将`【B/C/D】`替换为本人角色，并附上自己任务文档；AI不能代替成员认领或批准PR。

```text
请先阅读这个仓库的README、docs/team-plan.md、docs/development/notes.md、
docs/design.md及我对应的docs/tasks任务文档，再完成【B/C/D】工作包中的一个小任务。

当前技术栈是Python3.12、PySide6和SQLite。先核实该任务实际已有代码，
复用src/pmkb/contracts中的类型；缺少接口时先提出具体契约，并说明提供方/调用方。
不要重新选择技术栈，不越界改写其他成员模块，不把第一关v3待同步规则混进来。

保持手动保存、草稿/保存版隔离、引用身份和跨集合保护、独立恢复及未知提交规则。
先写有意义的行为测试，再实现最小功能；真实运行scripts/check.py及相关用例。
未实现功能不能返回假成功，未执行检查不能写通过，不把基础检查当完整产品验收。

只在自己的功能分支工作，不直接提交main，不上传真实笔记、数据库、密钥或课程ZIP。
结束时说明改动文件、接口影响、实际检查/未执行项和下一位成员需要接什么，
未经授权不代替其他成员审查、合并或修改权限。
```

接手完成的标志：**能运行基线、明确自己认领哪项任务、知道第一个交付物和接口对接人、能按规则提交一个可审查的小PR。**
