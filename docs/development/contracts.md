# Python接口责任与边界

## 类型映射

以第一关第二版的业务和已确认设计第二版的接口语义为基础，2026-10-18改为Python。`src/pmkb/contracts`提供API01/02/08/11完整字段和Protocol；这是团队开发契约，尚无对应业务实现。

| 原设计表示 | Python表示与约束 |
|---|---|
| record/DTO | `@dataclass(frozen=True, slots=True)`；字段用snake_case，跨线程按快照传递 |
| enum | StrEnum，保留OK/ERROR等原状态值 |
| Task<T> | `async def ... -> OperationResult[T]`；GUI线程不得直接阻塞等待 |
| CancellationToken | 可选`threading.Event`协作取消请求；不等于事务回滚 |
| long | int，但版本/代次仍限0到2^63-1（保存修订至少1），拒绝bool冒充int |
| nullable | `T | None`，不依赖truthiness把None、False、0混成一类 |
| IReadOnlyList | tuple；服务入口冻结来自UI的可变输入 |
| IReadOnlyDictionary | Mapping标注；必须复制并冻结版本映射，frozen数据类不保证内部容器深度不可变 |
| DateTimeOffset UTC | timezone-aware datetime，UTC offset=0；不接收naive datetime。本轮不引入v3固定毫秒规则 |

公共数据类本身没有完整运行时验证，服务须校验标识、版本、枚举、可空关系、容器及结果形状。

## 服务责任

- A：CollectionCoordinator，prepare_activation / complete_activation / cancel_activation。
- B：NoteService（create_draft/load_note/apply_draft_edit/save_note）和CommitResolver（check_commit/close_unconfirmed_session）；C提供存储证据。
- D：BackupService.backup_collection；C提供一致快照。
- C牵头API04/05/10；B牵头API03/06；D牵头API07/09。未落成的精确Python签名先由提供方/调用方核对并交AI助手技术审查，不能把概念契约误记为已编译接口。

## 结果状态

OK=本接口目标完成；ERROR=明确失败；CANCELLED=确认无本次新增持久化效果的取消；PARTIAL=确定效果已发生但多阶段目标/收尾未完成；UNCONFIRMED=仍有结果未知，优先于其他状态。

原子SAVE确认成功后迟到取消仍OK；API11的OK只表示核实完成，不一定保存成功。NOT_COMMITTED须有确认未提交的证据，无回执本身不够。未知时不直接显示已保存/已回滚，不自动重发写入。

配置登记`configuration_updated`为True/False/None三态，None=未知。取消准备/激活只能清理自有未发布产物，不能误删已发布集合，不能回滚已成功源保存。

## 上下文与字符

CallContext集合标识/代次+草稿edit_version/保存base_revision分别检查。旧DeliveryContext不显示，不撤销真实效果。ActiveContextGate只在最终激活边界切换内存，不代替文件验证、发布、登记或草稿保护。

保留UTF16半开跨度字段。Python字符串按码点索引，使用domain.text_spans明确转换；不能直接用UTF16偏移切Python字符串。Qt光标位置/解析器位置也需在适配层转到同一约定。不得切开代理对；无配对代理项按设计拒绝。名称长度仍按合法Unicode码点，不是UTF16单位。

## 接口变更

PR写旧/新字段、可空性、错误状态、调用方影响和真实回归。提供方和调用方共同核对，AI助手统一技术审查并报告，合并按A授权执行；A协调公共目录。不要混入[v3待同步](pending-requirement-sync.md)规则。

## API01已实现的内部协作端口

`application.collection.ports`定义CollectionBackend、IdentityRegistry、CollectionSession。A协调器实现公开CollectionCoordinator Protocol；B实现CollectionSession的buffer快照、保存和最终Qt线程激活；C/D可复用或替换最小本地适配，但须保持实际状态/异常语义。激活前必须在同一UI回调中复核cancel_signals、Context和全部buffer状态，安装成功后才放弃明确选择的草稿。

HeadlessCollectionSession只供无编辑器的开发命令使用，不得接到已经存在真实草稿的界面。端口及当前限制见[集合接入](collection-lifecycle.md)。
