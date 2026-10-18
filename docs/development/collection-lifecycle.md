# A集合流程：开发命令与队友接入

## 当前能做什么

`CollectionCoordinatorService`已实现API01准备/完成/取消，协调目标检查、全部草稿选择、逐项保存、非覆盖发布、必要登记和最终激活。

最小真实适配能新建符合批准DDL的空SQLite集合、只读打开、检查格式/关键结构/数据库完整性；身份配置只记录集合ID与位置，不含正文。它不实现笔记CRUD、查询、备份恢复，也不宣称完整历史业务语义校验。

## 运行真实创建/打开

先按README安装依赖，在仓库根目录运行。以下路径仅为示例，请选择自己明确准备的测试位置，配置放在集合外；新建目标目录必须事先存在且为空：

```powershell
New-Item -ItemType Directory -Path .\artifacts\demo-collection
.\.venv\Scripts\python.exe scripts/collection.py create .\artifacts\demo-collection --config .\artifacts\demo-config.json
.\.venv\Scripts\python.exe scripts/collection.py open .\artifacts\demo-collection --config .\artifacts\demo-config.json
```

输出JSON含实际状态/阶段/集合位置/身份/上下文；成功退出0，其他状态退出1。该命令是无编辑器的开发入口，每次启动新会话，不表示Qt窗口已切换，也不能拿它绕开真实界面的未保存保护。不要反复对已经有数据的目录运行create；程序会拒绝，不覆盖。artifacts目录已忽略，不入库。

## B：实现CollectionSession

- `context`：当前活动集合及代次。
- `has_unconfirmed_save`：真实保存存在待核实时返回True，阻止新切换。
- `buffers()`：返回全部buffer的稳定快照（ID/edit_version/is_dirty），不只返回脏buffer。任何名称/正文/标签/引用变化都推进版本。
- `save_buffer()`：调用真实编辑保存服务，返回已确认捕获版本或待核实标识；不要假成功。A不实现B的笔记保存。
- `activate(expected,target,expected_buffers,discard_ids,cancel_signals)`：派发到Qt主线程，在同一无等待回调内核对取消信号、Context和全部buffer状态；成功安装新上下文后才放弃明确选择的草稿，返回新Context。拒绝时返回None且不丢任何草稿。

`HeadlessCollectionSession`明确没有编辑器，只供开发命令，不得用作有真实草稿界面的默认会话。

## C/D：后端与登记

- `CollectionBackend`：prepare/validate/publish/cleanup；同步IO由A放到工作线程。C可复用schema.sql和最小目录检查，补齐CRUD/历史数据语义；D可复用发布契约，其他输出能力另行实现。
- `PreparedCollection`是服务内部可信对象，UI只能使用票据ID，不能改临时路径/目标身份欺骗服务。
- 当前publish通过同目录硬链接独占创建knowledge.sqlite，不覆盖；不支持的文件系统拒绝，不用replace降级。cleanup只删确认属于本次操作的临时候选，绝不递归删目标目录。
- `IdentityRegistry.check`只读核对；register返回True=已更新/False=无需更新，失败明确抛CollectionFailure；unknown=True表示结果未知。不得漏return返回None后当成成功。
- 配置JSON含configVersion=1和collections映射；采用旁锁文件、私有暂存、fsync及原子替换。新配置位置可创建，已有配置损坏/不可读不会被覆盖。已知不同存活位置复用集合身份时拒绝。

## 状态和取消

准备不激活。源保存已经成功不回滚；目标已发布/登记成功后停止为PARTIAL；保存、目标发布或登记仍未知为UNCONFIRMED，不能用一次取消断言已回滚。源保存未知带PendingSave/命令ID，文件/登记未知不能交API11假核实。

取消会先记录意图，再等待在途操作得到结果。Python外层任务取消不会直接弃置后台发布；最终Qt回调也必须检查取消，防止已经点取消却在排队回调中放弃草稿。已激活之后按旧Context请求取消会返回STALE_CONTEXT，不撤销新集合。

## 真实检查与限制

本轮全部53项自动检查通过：原15项基础、11项真实目录/SQLite/登记、24项协调流程、3项开发命令。真实文件测试使用临时合成集合；B保存/未知提交与Qt最终激活使用受控编辑会话验证协调契约，没有冒充真实B模块已实现。

这些支持FR16/FR10/NFR02的部分实现和V15相关分支，不能把完整V01—V16验收标通过。Windows11人工界面联调、完整笔记CRUD/引用/恢复、性能/离线/隐私和最终发行包仍待后续真实执行。第一关v3规则未混入。
