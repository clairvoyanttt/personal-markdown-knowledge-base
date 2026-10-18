# Python开发上手

## 环境与安装

目标系统Windows11。团队默认使用标准CPython3.12和venv；项目声明3.12—3.14，当前实际检查的是3.12，其他版本尚未验证。不使用作者电脑绝对路径。需要Git及本人GitHub权限，B/C/D权限不能假定已具备。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts/check.py
.\.venv\Scripts\python.exe -m pmkb
```

当前只安装PySide6-Essentials及shiboken6，均固定6.12.0，导入方式仍是`from PySide6...`。SQLite来自Python标准库，不需要安装名为sqlite3的第三方包。首次准备开发环境需要下载依赖；产品离线发行是后续单独任务，不等于让最终用户联网安装pip依赖。

若`py`不可用，确认`python --version`是标准CPython3.12后换成`python -m venv .venv`。没有pip/Qt轮子不兼容时更换标准Python，不将问题误判为业务失败。

## 运行与测试

当前窗口只显示开发状态，不读写用户笔记或配置。`scripts/check.py`执行Python语法编译、公共契约导入、pip依赖检查和标准库unittest；基础测试包含离屏Qt窗口事件循环/关闭。离屏检查不是人工Windows界面验收，不代表所有字体/交互正常。

启动壳不支持编辑保存，点击/演示不能冒充完整软件。后续每个模块自行增加真实行为用例和回归；不要写“测试数大于0”替代具体验收。

## 依赖方向

桌面组合入口调用应用服务；应用服务使用领域规则/契约和基础设施端口，基础设施实现这些端口。规则/契约不导入PySide6、sqlite3具体连接或桌面控件；ViewModel不直接SQL。B的界面/应用状态不依赖Markdig或WPF，这些属于旧技术方案。

API01/02/08/11已有Python类型/Protocol，可直接引用；API03—07/09/10的精确Python对象由对应负责人先提契约PR，不直接返回随意字典。用`snake_case`命名、类型标注和frozen数据类，服务入口仍需运行时校验。

仓库基线PR尚未合并main时，可先基于`chore/a-team-baseline`建个人任务分支，在PR中标明依赖。合并基线后更新main再联调；不要各自复制文档或骨架形成分叉。
