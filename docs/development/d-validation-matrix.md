# D 需求—实现—验证矩阵与产品执行记录

日期：2026-10-18。范围：Python 团队项目的 D01—D03 独立交付，不是个人课程作业版本。
基线：[需求第二版](../requirements.md)、[团队计划](../team-plan.md)、
[D 分工](../tasks/D-export-backup-restore.md)及 [D 总进度表](d-progress.md)。

本表中的 `FR09.1` 等后缀只定位原需求的验收第几项，不新增或改变需求编号。
API07/09 及 B/C/A 窄端口目前为 **D 内部可执行草案**；API08 沿用已有公共 DTO，
快照/会话适配仍是内部端口。真实依赖未接入，D04 联调和 D05 产品验收尚未完成。
本表列出代码和自动测试入口，不能据此把 V07/V08/V09/V16 填为产品通过。
本轮全量测试的实际结果和跳过原因由 [总进度表](d-progress.md)统一登记，不在这里猜测总数。

## 1. 证据分层及定位约定

| 层级 | 能说明什么 | 不能说明什么 |
|---|---|---|
| 纯组件测试 | 命名、已授权跨度转换、清单结构/比较、UTF-16 边界 | C 解析完整性、目标真实存在、保存版来源、SQLite 正确 |
| 真实临时文件测试 | 非覆盖、流式 ZIP/CRC/摘要、候选归属及清理、受控文件故障 | 该载荷是有效业务数据库、恢复集合可打开、Qt 提示已接入 |
| 服务替身测试 | D 的选择、上下文、版本、编排、结果和收尾规则 | C 一致快照/真实初始化、A 发布/登记实现、B 真实草稿/UI |
| 产品执行记录 | 真实构建、数据、操作、观察及证据路径齐全时可逐项判定 | 自动继承组件结果、没有观察就填通过、AI 代替成员批准 |

实现入口别名（均为当前实际源码）：

| 别名 | 入口 | 职责 |
|---|---|---|
| E | [export/service.py](../../src/pmkb/application/export/service.py) | 单篇保存投影、选择复核、命名渲染与发布编排 |
| B | [backup_service.py](../../src/pmkb/application/backup_restore/backup_service.py) | 全部草稿复核、冻结保存版租约、事实绑定与打包 |
| R | [restore_service.py](../../src/pmkb/application/backup_restore/restore_service.py) | 候选→C 校验→A 目标→C 初始化→A 发布，不打开/激活 |
| S | [output_support.py](../../src/pmkb/application/output_support.py) | 严格代次/版本、当前 dirty 确认、原上下文投递、取消意图与实际结果等待 |
| F | [publication.py](../../src/pmkb/infrastructure/file_io/publication.py) | 完整文件非覆盖发布及未知结果保护 |
| Z | [zip_guard.py](../../src/pmkb/infrastructure/file_io/zip_guard.py)、[zip_writer.py](../../src/pmkb/infrastructure/file_io/zip_writer.py) | 有界归档检查、构建和发布前实际字节核对 |
| M | [manifest.py](../../src/pmkb/application/backup_restore/manifest.py)、[package_components.py](../../src/pmkb/application/backup_restore/package_components.py) | 严格清单及实际快照摘要/大小绑定 |
| CAND | [candidate.py](../../src/pmkb/application/backup_restore/candidate.py) | 同一 ZIP 源句柄校验及流式提取、私有候选身份/内容核验、自有清理 |
| TXT | [rendering.py](../../src/pmkb/application/export/rendering.py)、[references.py](../../src/pmkb/application/backup_restore/references.py) | 只处理 C 授权跨度，不发现/补猜标记，不读库 |

测试入口别名：下文 `TE::test_x` 表示该文件对应测试类中的实际方法。

| 别名 | 文件/测试类 |
|---|---|
| TE | [test_d_export_service.py](../../tests/test_d_export_service.py) / `ExportServiceTests` |
| TB | [test_d_backup_service.py](../../tests/test_d_backup_service.py) / `BackupServiceTests` |
| TR | [test_d_restore_service.py](../../tests/test_d_restore_service.py) / `RestoreServiceTests` |
| TS | [test_d_output_support.py](../../tests/test_d_output_support.py) / `OutputSupportTests`、`SettledOutputTests` |
| TF | [test_d_publication.py](../../tests/test_d_publication.py) / `PublicationTests` |
| TN | [test_d_export_helpers.py](../../tests/test_d_export_helpers.py) / `ExportNamingTests`、`ExportRenderingTests`、`WindowsBasenameTests` |
| TM | [test_d_manifest.py](../../tests/test_d_manifest.py) / `ManifestCodecTests`、`ManifestFactsTests` |
| TP | [test_d_backup_components.py](../../tests/test_d_backup_components.py) / `BackupComponentWorkflowTests` |
| TC | [test_d_restore_candidate.py](../../tests/test_d_restore_candidate.py) / `RestoreCandidateTests` |
| TX | [test_d_restore_references.py](../../tests/test_d_restore_references.py) / `RestoreOwnershipTests` |

## 2. FR09 / V07：单篇导出，原验收 1—12 全覆盖定位

下列各项的完整产品状态均为 **未执行**。测试入口仅说明现有 D 侧证据，右栏列真实联调缺口。

| 原验收 | D 实现/检查 | 自动测试定位 | 仍需 B/C 及产品观察 |
|---|---|---|---|
| FR09.1 导出前说明不可应用内跳转、不打包附件 | E 的调用前置说明；TXT 只生成文本，未实现 Qt 提示 | TN::`test_exact_four_fr09_texts`；无真实 UI 提示通过记录 | B 实际展示两项限制并记录用户可见反馈 |
| FR09.2 未保存时提供返回保存/继续 | S 校验选中笔记的 dirty 版本及确认，E 只消费保存投影 | TE::`test_continue_requires_exact_current_dirty_acknowledgement` | B 真实正文/名称/标签/引用修改都能触发正确选择 |
| FR09.3 返回不导出，继续只含保存版 | E 返回选择不读源、不保存、不发布；C 端口输入冻结投影 | TE::`test_return_to_save_neither_saves_nor_reads`、`test_dirty_selected_note_uses_saved_name_body_and_relations` | C 的真实保存查询与 B 草稿分离；不能用替身构造的正文自证 |
| FR09.4 内部引用不形成有效链接 | TXT 按四分类输出普通文本；不保留应用内标记 | TN::`test_exact_four_fr09_texts`；TE::`test_all_four_authorized_reference_kinds_and_code_unchanged` | C 授权跨度完整；在约定 Markdown 阅读器观察渲染结果 |
| FR09.5 特殊字符名称不注入链接 | TXT 转义 Markdown/HTML 相关 ASCII 标点 | TN::`test_names_cannot_inject_markdown_links_or_html` | 真实已保存目标名称、阅读器渲染及不可点击性 |
| FR09.6 外链、图片/附件路径保留且不复制文件 | TXT 只改授权跨度，E 输出单篇 UTF-8 字节 | TN::`test_only_supplied_span_is_replaced_not_identical_code_or_links`；TE::`test_all_four_authorized_reference_kinds_and_code_unchanged` | C 不授权代码/外链/图片中的伪标记；实际目标目录不多出资源 |
| FR09.7 成功显示位置；失败/取消不误报、不改源及已有目标 | E/F 结果含正式位置和发布状态，未知单列，不自动重试/删除 | TE::`test_publisher_failure_and_cancel_have_no_value`、`test_publisher_unknown_preserves_paths_and_does_not_retry` | B 按 OK/ERROR/CANCELLED/UNCONFIRMED 呈现；真实源库/草稿对比 |
| FR09.8 已知内容目标拒绝覆盖，另选路径可用 | F 独占发布，竞争写入也拒绝覆盖 | TE::`test_target_collision_never_overwrites`、`test_two_exports_competing_for_target_only_one_publishes`；TF::`test_existing_file_is_unchanged` | B 重选/取消流程及文本编辑器读取真实成功产物 |
| FR09.9 中文、代码、其他正文一致可读 | TXT 保留未授权文本，F 不进行换行转换 | TE::`test_default_name_and_utf8_saved_projection`、`test_utf16_reference_after_non_bmp_text`；TF::`test_bytes_are_published_without_newline_or_unicode_changes` | 从真实保存记录逐字符比对；代码显示与换行实际观察 |
| FR09.10 默认已保存名称、自选 .md 扩展 | 命名函数独立于业务名称/草稿名 | TN::`test_confirmed_default_examples_and_no_implicit_title_edit`、`test_user_choice_only_appends_missing_extension`；TE::`test_selected_extension` | B 默认名绑定保存名而非输入框名；Windows 文件选择器接线 |
| FR09.11 清洗重名仍拒绝覆盖，非法/不可写可重选 | 命名只处理文件名；E/F 拒绝冲突或不可用目录 | TE::`test_sanitized_name_collision_does_not_change_either_note`、`test_directory_must_already_exist_and_be_directory` | 不可写实际 ACL/目录场景、重选交互与源库保护 |
| FR09.12 四类、代码、外链、特殊名、未保存引用 | TXT 仅授权原文跨度，E 核对归属与投影版本 | TE::`test_all_four_authorized_reference_kinds_and_code_unchanged`、`test_reference_projection_inconsistency_rejected`、`test_dirty_selected_note_uses_saved_name_body_and_relations` | C 同一保存捕获下的四类真实分类与代码保护；Markdown 阅读器验证 |

CB01 补充：TE::`test_new_draft_rejected_without_source_read`、`test_missing_saved_note`检查无保存版拒绝；
真实新草稿的按钮/提示仍需 B。E/S 不触发 SAVE，选择“返回保存”不是自动保存。

## 3. FR11 / V08、V09、V16：备份与独立恢复，原验收 1—18 全覆盖定位

完整产品状态全部 **未执行**。以下“实际字节”仅指测试临时文件；业务快照由替身提供，
不是已验证的 SQLite。独立清单格式/版本及 C/A 端口仍须共同确认。

| 原验收 | D 实现/检查 | 自动测试定位 | 真实联调/产品缺口 |
|---|---|---|---|
| FR11.1 任一草稿均提醒，返回/继续 | B/S 检查全部 buffer，不信任 NO_UNSAVED 声明；B 界面提示未实现 | TB::`test_all_draft_categories_block_untrue_no_unsaved_claim` | B 真实新增/正文/名称/标签/引用各草稿类别与提示 |
| FR11.2 返回不备份，继续仅保存数据 | B 不 SAVE，租约绑定 context 和当前 dirty 确认；返回不捕获 | TB::`test_return_to_save_does_not_capture_publish_or_save`、`test_continue_only_excludes_explicitly_acknowledged_versions` | C 一致捕获名称/正文/标签/关系/失效信息且排除全部草稿 |
| FR11.3 非覆盖，成功位置/数量且可恢复 | B/M/Z/F 绑定捕获事实、实际条目摘要/大小；OK 含位置数量 | TB::`test_real_archive_uses_capture_facts_and_microsecond_timestamp`、`test_existing_destination_preserves_bytes_and_releases_snapshot`；TP::`test_bytes_roundtrip_checks_container_and_manifest_not_database` | 该测试是真实 ZIP+合成载荷，不是可恢复数据库证明；需真实备份→恢复 |
| FR11.4 失败保源，不报告有效备份 | B 收尾仅委托 C 释放租约；未知独立反馈 | TB::`test_actual_snapshot_hash_or_size_mismatch_fails_before_publication`、`test_unknown_publisher_outcome_is_not_replayed_or_deleted` | 真实源库各字段比对，B 不把失败/未知文件当有效备份 |
| FR11.5 仅空目标，非空保护 | R 预检，A 端口负责准备/发布时重检和独占边界 | TR::`test_nonempty_target_is_rejected_without_changing_existing_content` | A 的真实空目录令牌、并发占用与正式发布，无已有内容损失 |
| FR11.6 成功显示新位置/数量，可打开独立集合 | R 只返回发布位置和新 ID；不激活、不合并 | TR::`test_complete_d_flow_reports_only_substitute_publication_and_never_activates` | 测试发布的是标注非 SQLite 的合成文件；需 C READY 库、A/B 显式打开 |
| FR11.7 内容/标签/有效失效保持，只有归属允许变 | TXT 保留 noteId/分类/历史提示及非授权文本；R 绑定初始化报告 | TX::`test_source_valid_deleted_and_missing_convert_foreign_does_not`、`test_only_authorized_spans_change_not_code_links_images_or_similar_text` | C 真实行/派生关系一致、代码未识别、Y 内跳转/反向引用及失效状态 |
| FR11.8 损坏/不可写失败不改源，不启用半成品 | CAND 同句柄验证提取；R typed 失败与未知阶段保留 | TC::`test_corrupt_snapshot_crc_rejects_without_touching_backup`、`test_write_flush_fsync_and_close_failures_are_typed_and_cleaned`；TR::`test_invalid_zip_is_not_given_to_storage_or_targets` | C 真 SQLite 损坏/不可写库，A 拒开 PREPARED/无效产物，B 反馈 |
| FR11.9 备份后修改源，再恢复不回改源 | D 读冻结源、只写私有候选和 A 新目标；不向源写入 | TB::`test_snapshot_path_is_read_but_never_deleted_by_service`；TC::`test_one_stream_extracts_private_candidate_and_preserves_source` | 真库先后版本对照及恢复后源修改仍在；替身不能证明此业务链 |
| FR11.10 已有备份字节不变，另选路径正常 | Z/F 非覆盖，B 不删除已有目标 | TB::`test_existing_destination_preserves_bytes_and_releases_snapshot`；TF::`test_target_created_after_staging_is_not_overwritten` | B 拒绝/重选/取消操作，真实当前集合对照 |
| FR11.11 来源/实际时刻/数量，换集合不串数据 | B 将 capture context、manifest 与独立 facts 绑定；不读取打包时钟代捕获时间 | TB::`test_real_archive_uses_capture_facts_and_microsecond_timestamp`、`test_capture_context_or_collection_mismatch_is_rejected`、`test_context_change_after_capture_prevents_publication` | C 实际一致捕获的时刻/版本/数量；A→B 真实切换；B 显示与运行时记录 |
| FR11.12 恢复不自动切换，可显式打开并保护原草稿 | R 不调用激活/保存接口；迟到结果保留原投递 context | TR::`test_context_change_after_publication_does_not_undo_output`、`test_restore_is_allowed_without_an_active_collection` | A API01+B FR10 显式打开链、原草稿保存/放弃/取消保护 |
| FR11.13 原位置不可访问仍恢复，引用只在新集合 | M 对 sourceLocation 仅词法识别，CAND 从 ZIP 提取；TXT 改受控归属 | TM::`test_source_path_is_only_lexically_checked_never_opened`；TR 合成来源 `Z:\unavailable\source`；TX 四类转换 | 真正使原路径不可访问后执行 C/A/B 全链；合成不存在路径不是该产品验收通过 |
| FR11.14 无效集合/半成品不能打开，ZIP不能直接编辑 | R 不把输入候选当 READY，也不打开备份 | TR::`test_invalid_initialization_receipt_count_or_binding_never_publishes`；CAND 类型说明 | A 的真实模式/结构/READY 检查与 B 入口，原活动集合保护 |
| FR11.15 X→Y 新集合 ID、保 noteId、有效/反向/失效/代码 | TXT 对授权源归属转换，新旧 ID 相同或已知外来 ID 碰撞拒绝 | TX::`test_source_valid_deleted_and_missing_convert_foreign_does_not`、`test_deleted_and_missing_same_name_remain_invalid_and_keep_note_id`；TR::`test_target_identity_must_be_new_and_destination_bound` | C 全身份账本与派生索引；A 真 ID 预约；Y 内导航及双向关系，不能按报告计数替代 |
| FR11.16 普通复制不做恢复转换、不误关联 | TXT 只由受控恢复调用，外来标记原样保留；D 不接管粘贴 | TX::`test_same_note_id_in_other_collection_does_not_rebind`、`test_foreign_only_has_no_changes` | B 粘贴→C 保存/解析→预览/导航真实链；FR06/08 由 C 主责 |
| FR11.17 同备份→Y/Z 独立，跨复制失效，源/备份不变 | TXT 可分别输入不同新 ID；CAND 每次独占工作区，清理不跨操作 | TX::`test_same_backup_converts_independently_to_two_new_collections`；TC::`test_two_preparations_share_parent_without_sharing_ownership` | A 保证两次实际新 ID/登记独立；C 两真实库关系及 B 跨复制检查 |
| FR11.18 重复身份/关系冲突/假有效目标必须拒绝 | R 要求 C 独立校验 facts、受控初始化并核对报告；不按名称修配 | TR::`test_independent_facts_mismatch_stops_before_initialization`、`test_invalid_initialization_receipt_count_or_binding_never_publishes` | **D 没有实现这些 SQLite 行级损坏检测**；C 必须提供真实损坏库逐类拒绝和源保护证据 |

FR11 的图片/附件范围：D 只处理 `manifest.json` 和 `snapshot.sqlite`，不复制外部资源。
真实快照中相关路径按文本保留仍由 C 检查，不能只靠“两条 ZIP 条目”证明库内路径未被改写。
空保存集合可备份的 D 边界见 TB::`test_empty_saved_collection_is_valid_not_an_empty_draft`；
空恢复库的真实初始化和可打开性仍须 C/A 执行。

## 4. V16 的真实数据库门槛：不得用 DTO 检查替代

依据 [Python 设计的恢复与回执规则](../design.md)。R 的 `InitializedRestore` 只核对
目标 token、新集合 ID、数量、输入摘要及报告的集合修订；它不包含实际 Notes/CommitReceipt 行。
以下各项目前均为 **未联调/未执行产品验收**：

| 门槛 | 必须收集的真实证据 | 主责/协作 |
|---|---|---|
| 一致保存快照 | 同一捕获事务/队列下的源 ID、schema、collectionRevision、名称/正文/标签/四类关系、数量及实际捕获 UTC；与条目摘要和清单逐项比较 | C 提供，D 比较，B 排除草稿 |
| 业务镜像拒绝 | 重复 noteId、有效目标不存在、身份/出现项/派生关系冲突各一个受控库；C 返回拒绝且源字节不改 | C 实现/执行，D 编排复核 |
| 新初始化事务 | 新集合 ID、collectionRevision=1；笔记 revision 保留；旧集合的全部回执移除；新集合级 RESTORE_INIT 独立存在 | C 实际执行，D 复核，A 验发布产物 |
| 逐篇 RESTORE_NOTE_INIT | 每篇恢复笔记有不同 commandId；noteId、afterRevision、collectionId 与该篇一致；beforeRevision 为空；每篇 lastCommitId 指向自身回执，不指向集合级 RESTORE_INIT | C 实际行查询/断言；D 整理证据索引 |
| 不能冒充 SAVE | 将旧集合回执、RESTORE_INIT、RESTORE_NOTE_INIT 分别交 SAVE 核实，均不能证明新集合 SAVE 已提交；无回执也不自动等同回滚 | B+C API11，D 配合，A 复核异常切换 |
| 后续真实手动 SAVE | 恢复后编辑并 SAVE，产生新 SAVE 回执、该笔记 revision 推进、lastCommitId 更新到该 SAVE，其他笔记初始化证据不串用；原备份不变 | B+C 实现/执行，D 对照恢复前后证据 |
| READY 与正式发布 | 初始化真实有效库且 A 校验 READY 后一次非覆盖发布；未知不误删；正式库可按 API01 打开 | C+A，D 编排，B 显式打开 |

现有 [TR](../../tests/test_d_restore_service.py) 的“数量/绑定错误拒绝”测试只证明 D 不接受
矛盾报告；[TP](../../tests/test_d_backup_components.py) 的 facts 比较只证明比较函数被调用。
两者均不能证明上述初始化 SQL、事务、身份账本、lastCommitId 或后续 SAVE 已正确实现。
历史独立 SQL 样例、AI 代码审查与离屏启动也不替代这些真实产品结果。

## 5. FR16 配合及 V14/V15/NFR 验证分界

| 验收定位 | D 已准备的边界/入口 | 真实执行主责与尚缺证据 |
|---|---|---|
| FR16.1–2 创建、重开、非空/不可写保护 | R 只消费 A 目标端口，不新增另一套集合协调器；F 非覆盖与 CAND 自有清理有组件入口 | A+B+C 创建/保存/关闭/重开真实链；D 交叉复核文件保护，不能把 TR 合成文件当可编辑库 |
| FR16.3–4 单活动集合与切换草稿保护 | S 对请求/投影/会话代次及版本复核；E/B/R 不激活；迟到结果固定原 delivery_context | A API01+B 全草稿选择+C 保存证据；切换失败/取消、已保存不回滚、仅实际切换执行放弃 |
| FR16.5 无活动集合边界 | E/B 需活动 context；R 允许 `CallContext(None, generation)` | TR::`test_restore_is_allowed_without_an_active_collection`；A/B 实际按钮可用性及恢复后显式打开 |
| FR16.6 恢复结果打开 | R 返回新位置/ID/数量，不自动切换，已知成功不因迟到取消撤回 | A/B 接入“打开恢复集合”，C 验真库；原集合和草稿保护 |
| FR16.7 断网操作 | D 模块不使用账号/网络；只做本地文件/归档 | A 组织四人断网真实全核心场景；不扩展删除/合并/多集合并行 |
| FR16.8 身份保留与冲突 | R 需 A 预约新 ID；TXT 拒绝新 ID 等于源或输入中已知外来 ID | A/C 真实身份登记、冲突受控样例与正常重开 ID 不变；D 文本检查不是全局身份验证 |
| V14 约束/模块契约，NFR02 配合 | TS 严格 int 非 bool；E 投影绑定；B 租约/事实绑定；R token/数量/摘要报告检查；TXT 拒绝畸形跨度 | C SQLite CHECK/外键/身份/回执语义；A/B/C/D 确认内部端口和正式清单后做契约联调，各人执行自己模块 |
| V15 发布/配置/激活，NFR02/04 配合 | F/Z 未知不重发不清正式文件；R 未知阶段保留；TR 迟到取消/上下文保留已知成功 | A 主责正式发布、身份/位置配置三态及激活；B 反馈“已发布未激活”与原草稿保护，D 交叉复核，不代填批准 |
| NFR01 / V11 离线 | D 合成文件/编排入口可本地运行，不依赖服务端 | A 组织四人真实断网完成并重开；本机跑 unittest 不是整套离线产品验收 |
| NFR02 数据完整/故障保护 | TF/TC/TB/TR 覆盖非覆盖、CRC/摘要、失败/取消/未知和自有清理；TXT 保留非授权字符 | 四人实际源/备份/恢复库字段与字节对照，C 回执和 B 草稿联合验证；不承诺断电后草稿恢复 |
| NFR04 / V12 可辨反馈 | 结果分类和 AppError 给出原因/下一步；位置/数量/未知阶段明确 | B/A 真实 Qt 可读入口、未保存提醒、失败/未知提示；D 对照输出恢复语义，不局部引入待同步 v3 字典 |
| NFR05 / V11 隐私 | D 源码无主动上传/账号调用；样例全为合成内容，不交真实笔记 | 四人/A 实际进程网络记录与发包核查，区分用户主动外链；不能从断网成功推出无主动传输 |
| NFR06 / V01/07/12/14 Windows 兼容 | TN/TF/TE/TC 有中文、换行、非 BMP、Windows 名称/路径与真实临时文件入口 | A 组织四人记录真实 Windows 11/应用构建/设备，操作整条保存→导出→备份恢复→重开链；权限跳过项保留而非记通过 |
| NFR03 / V10 性能配合 | D 不把输出测试耗时当搜索指标，可复核数据/字节/证据完整性 | C+A 主责 1000 篇、每篇 UTF-8≤10240 字节、固定 30 查询且至少29次≤2秒并完整正确；全部30次及环境/计时应实际记录 |
| FR03 / V03 预览配合 | TXT 的代码/外链/图片保留样例可供 B/C 交叉对照，不是预览实现 | B 主责六类 Markdown 当前草稿预览；C 共享解析；D 配合验证不会隐式 SAVE 或改源文本 |

## 6. 可复用的产品执行记录（空白，不预填通过）

状态填写规则：`未执行`＝未运行；`阻断`＝真实依赖/环境未满足、记明原因；
`失败`＝已运行但观察不满足预期；`通过`＝已运行且逐项有实际观察和可复核证据。
自动测试通过不自动改变产品状态；没有运行时间、数据和响应，不填写通过或编造值。

复制以下表格填写一条实际用例，不放入真实个人笔记、凭据或生产数据库：

| 字段 | 实际填写值 |
|---|---|
| 记录 ID / 原需求验收项 / V 编号 | |
| 执行者 / 复核者（仅本人实际参与时填写） | |
| 开始/结束时刻及实际时区 | |
| 应用版本/提交、分支、Python/Qt 版本 | |
| Windows 11 版本、CPU/内存/存储、测试目录类型/权限 | |
| 数据集 ID/生成方式；源/目标集合 ID 与源/目标路径（可脱敏） | |
| 保存版修订/集合修订/数量；草稿类型与 edit_version | |
| 前置条件；原始文件摘要及已有目标内容标识 | |
| 操作步骤；选择/取消/故障注入时点 | |
| 预期响应、应保全文件/数据、禁止的副作用 | |
| 实际返回状态/错误码/位置/数量/阶段/警告 | |
| 真实观察（UI、库行/回执、字段/字节前后对比） | |
| 输入/输出/日志/截图证据相对路径及必要摘要 | |
| 结论：未执行 / 阻断 / 失败 / 通过 | |
| 阻断原因或差异；关联问题 ID；责任对接方 | |
| 清理范围、保留的未知产物；复测记录 ID | |

产品待执行索引（状态不是已经执行失败，也不是已通过）：

| 计划组 | 需求覆盖 | 当前状态 | 解除阻断/执行前置 |
|---|---|---|---|
| D-P07 | FR09.1–12、CB01 / V07 | 未执行 | B 导出提示及 C 一致保存投影/共享解析，真实 Qt/阅读器 |
| D-P08 | FR11.1–4、9–11 / V08 | 未执行 | B 全部草稿状态与 C 真一致快照，正式清单格式确认 |
| D-P09 | FR11.5–8、12–18 / V09 | 未执行 | C 真校验/受控初始化+A 发布/身份+B 打开入口 |
| D-P16 | V16 及本表第4节 | 未执行 | C 行级回执/身份/关系证据、B 后续SAVE/API11、A READY发布 |
| D-P14/15 | FR16 配合、V14/V15 | 未执行 | A/B/C/D 正式接口确认与真实发布—登记—激活链 |
| D-PN | NFR01/02/04/05/06，V03/V10 配合 | 未执行 | A 组织四人真实构建与固定数据/网络/界面/设备执行 |

联调前不得擅自将窄端口替身安装为产品默认实现；不得生成看似有效的 SQLite/READY/回执假数据。
发现新增需求或接口变更时，按 [总进度表](d-progress.md)登记来源和影响；
未同步的固定毫秒、恢复时刻持久化及精确 v3 UI 字典仍由 A 统一协调，不在本矩阵局部采纳。
