# A运行接入：后台服务、Qt回调与退出

本轮实现公共运行部件，供B/C/D接入；不是笔记编辑器或完整产品。需求仍依据第一关第二版，v3未同步。GUI目前仍为启动说明窗口，不读写用户笔记、不自动打开或创建集合。

## 1. 已有部件与线程归属

| 部件 | 入口 | 责任 |
|---|---|---|
| AsyncServiceRuntime | `pmkb.application.runtime` | 一个非守护服务线程及asyncio循环；执行工厂、持有任务结果、协作取消、收尾 |
| QtDispatcher | `pmkb.desktop.dispatcher` | Qt应用主线程创建；queued signal执行同步UI回调；异常回传，不在工作线程碰控件 |
| ApplicationServices | `pmkb.desktop.composition` | 显式接入CollectionSession、配置文件路径和运行器，组装集合服务并登记收尾 |
| StartupWindow | `pmkb.desktop.app` | 启动壳接入运行器与分发器，关闭时非阻塞等待后台结束 |

`submit(factory)`中的工厂在服务循环内调用，参数是threading.Event取消意图，返回可等待对象。不要预先创建协程再提交，也不要每次请求另起asyncio.run。同步IO需服务自己使用asyncio.to_thread；有连接亲和性的SQLite写队列仍由C实现，不能把同一连接随意交给线程池。

UI状态由B拥有。CollectionSession的同步context/buffers/has_unconfirmed_save会在后台读取，B必须提供带锁的不可变快照，不能从这些属性直接读取Qt控件。全部草稿变化及保存结果应用后要同步更新快照。最终activate另走Qt回调做真实状态核对。

## 2. 组装与提交

以下是接入代码示意，`editor_session`由B实现，`config_file`由桌面入口明确提供绝对路径。组装本身不创建配置文件、不打开集合、不启动编辑器；禁止默认回退到HeadlessCollectionSession。开发CLI的无编辑器会话仍只用于CLI和明确标注的组件测试。

```python
from pmkb.application.runtime import AsyncServiceRuntime
from pmkb.desktop.dispatcher import QtDispatcher
from pmkb.desktop.composition import ApplicationServices

runtime = AsyncServiceRuntime()
dispatcher = QtDispatcher()  # QApplication创建之后，在主线程创建
services = ApplicationServices.compose(editor_session, config_file, runtime)
handle = runtime.submit(
    lambda cancel: services.collections.prepare_activation(request, cancel)
)
```

桌面入口持有运行器/分发器/服务到退出完成。配置仍是必要身份及位置登记，计划位置为用户LOCALAPPDATA下专用目录，与程序目录和集合目录分离；本轮没有擅自建立真实用户配置或自动打开最近集合。

`handle.outcome`是保留真实结果的Future；它的cancel()返回False。`handle.request_cancel()`仅置Event，不能据此显示已撤销。操作仍需返回OK/ERROR/CANCELLED/PARTIAL/UNCONFIRMED及真实阶段。不要在Qt线程调用未完成Future.result()、wait_stopped()或join()。

完成回调内可以读取已完成的outcome。成功取得OperationResult后，用`dispatcher.deliver(result, current_context, apply, stale)`排队投递。current_context、apply和可选stale均在Qt线程执行；按**实际投递时的delivery_context**判断。激活成功的结果携带新代次，不能拿request_context过滤。stale可记录不含正文的任务状态，不更新当前集合界面；原始结果仍留在句柄，丢弃展示不等于撤销IO或核实未知保存。异常也须消费并显示可读原因，避免把异常冒充业务成功。

此门禁只检查集合与代次。B/C还必须检查请求ID、edit_version/revision等各接口要求，避免同一集合内迟到预览/查询/保存覆盖更新的状态。

## 3. 最终激活桥

B的CollectionSession.activate可用`await dispatcher.invoke(install)`；install是**同步、无等待的回调**，在Qt线程依次检查：

1. 两个取消信号均未置位，预期Context仍匹配。
2. 全部buffer的ID/edit_version/is_dirty与expected_buffers完全一致，且没有待核实保存。
3. 准备好新视图后，一次安装新Context和新视图，只有实际成功后才放弃明确选择的草稿。
4. 拒绝时返回None，原草稿和上下文不变；成功返回递增一次的新Context。

使用已有ActiveContextGate完成最终内存代次核对。不要在install里做文件读取、SQL、解析或await。invoke的等待不会通过外层取消自动取消UI动作，因此**回调自身仍必须检查取消信号**。旧资源清理失败必须保留实际已激活事实，不能报成未切换。

## 4. 两阶段退出与资源清理

运行器不解释编辑草稿、保存回执或退出许可，不能代替B的FR10/CB02业务退出协调。

| 阶段 | 调用/行为 | 要求 |
|---|---|---|
| 业务收束 | UI暂禁新用户操作，request_cancel_all()，保持Qt处理事件 | 只发意图；等待pending_count归零并处理结果回调，不声称回滚 |
| 退出决策 | B检查全部最新buffer及PendingSave，必要时继续提交API11核实 | 此时运行器仍接收核实/保存操作；失败或取消退出仍可继续编辑 |
| 最终关闭 | 用户许可仍有效后begin_shutdown() | 不再接受新任务；等待已接收任务，逆序执行资源finalizer，等待IO执行器与异步生成器收尾 |
| 窗口销毁 | 非阻塞轮询is_stopped，处理必要结果后close分发器 | 不在Qt主线程join；未结束不强杀；shutdown_errors须可见 |

资源所有者用add_finalizer(async_close)登记，后登记先关闭。compose已登记集合服务close：清理本次拥有的候选，不递归删除目录；已发布集合保留，UNKNOWN产物保留并报告需检查。不改变源集合/草稿，不把清理成功当成SAVE证据。

若任务或IO长期不返回，运行器保持等待，不承诺强制中断安全；B必须提供真实状态提示。最终业务退出需遵守CB02的明确风险说明；不承诺程序关闭后恢复内存草稿。当前StartupWindow没有编辑器，所以只演示最后资源关闭阶段，不是已经实现完整F09或API11。

常规用户关闭窗口使用上述路径；操作系统强制结束或直接终止进程不能保证收尾。禁止把app.quit()当成有真实草稿时的常规退出捷径。

## 5. 本轮实际检查及待接入

在本地Windows、Qt offscreen事件循环下，实际检查后台循环复用、取消意图和真实结果、关闭等待、逆序清理及错误、Qt线程回调/异常、投递时旧Context拒绝、新Context激活结果接收、真实临时集合激活、候选清理及正式/未知产物保留。

真实集合激活测试使用**无编辑器测试会话+Qt桥**；B真实草稿、C写队列/回执、D输出恢复尚待联调。完整V01—V16、Windows11人工界面、性能/隐私/离线验收仍未执行。没有新增产品功能或修改需求优先级。

实现参考：[Python线程间协程调度](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.run_coroutine_threadsafe)、[Qt queued connection](https://doc.qt.io/qtforpython-6/PySide6/QtCore/Qt.html#PySide6.QtCore.Qt.ConnectionType)。本实现自行持有操作结果并用call_soon_threadsafe排队，避免取消Future丢失业务结果。
