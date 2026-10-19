# D 公共故障样例索引

更新日期：2026-10-18。适用当前 Python 团队基线，覆盖 D00—D03 的有限故障集合。
职责依据：[D 分工](../tasks/D-export-backup-restore.md)；统一进度入口：[D 总清单](d-progress.md)。

这里将可复核场景对应到实际测试方法，不新增业务需求，不接管 A/B/C 的全部测试。
全部输入是合成文本、虚构身份、临时目录或故障注入；不得替换成真实个人笔记、数据库、凭据或提交 ZIP。

## 1. 怎样读取实际状态

- **组件通过**：D00 的组件及组合检查已有实际通过记录，见[组件批记录](d-backup-components.md#5-实际检查与可复核入口)和[首批记录](d-independent-file-layer.md)。它们不证明真实保存投影或数据库正确。
- **替身服务通过**：本轮已执行相应 D 服务模块测试。B/C/A 依赖由显式测试替身提供；正式接口和真实联调尚未完成，不能写作完整 V07/V08/V09/V16 通过。
- **候选通过**：使用真实临时文件、合成归档进行提取/故障保护检查；载荷不是有效 SQLite 库，不等同于恢复产品验收。
- **权限跳过**：实际创建符号链接的宿主检查因 Windows 缺少特权跳过；归档内伪造链接元数据的拒绝检查已执行。跳过不能记作通过。

编写本索引没有额外执行全量检查，也不把各批测试数量相加作为新总计。
最终统一检查的命令、结果和跳过项以 [D 总清单](d-progress.md#4-最近检查与-git-交付状态)及本轮交付记录为准。
下列方法名已逐项对照现有测试源码。

## 2. D00：导出与归档组件故障

| 场景 ID / 故障 | 触发方法 | 必须保护的对象和判定 | 准确测试入口 | 范围 / 实际状态 |
|---|---|---|---|---|
| DF01 文件名非法、扩展名及保留设备名 | 传入路径、ADS、控制字符、尾随点/空格、保留名或非 `.md` 扩展名 | 拒绝不合法自选名；默认名只处理导出名称，不修改笔记标题 | [test_d_export_helpers.py](../../tests/test_d_export_helpers.py)：`test_path_ads_controls_and_trailing_characters_are_rejected`；`test_reserved_names_and_extension_variants_are_rejected`；`test_user_choice_is_never_silently_sanitized` | 命名纯规则 / 组件通过 |
| DF02 授权跨度错位或切开 Unicode | 提供错位、越界、重叠、错误标记或不合法 UTF-16 跨度 | 拒绝猜测修复；不重解析或改写未授权原文 | [test_d_export_helpers.py](../../tests/test_d_export_helpers.py)：`test_bad_spans_marker_mismatch_or_kind_fail_closed`；[test_d_restore_references.py](../../tests/test_d_restore_references.py)：`test_span_splitting_emoji_surrogate_pair_is_rejected` | 导出/恢复纯转换 / 组件通过 |
| DF03 目标名注入、代码与外链被误改 | 使用 Markdown 特殊字符目标名，并将相同标记放进代码和外链区域 | 显示名称按普通文本转义；只转换授权跨度，代码、外部链接、图片路径保留 | [test_d_export_helpers.py](../../tests/test_d_export_helpers.py)：`test_names_cannot_inject_markdown_links_or_html`；`test_only_supplied_span_is_replaced_not_identical_code_or_links` | 保存投影之外不做解析 / 组件通过 |
| DF04 既有目标、并发抢占及默认名碰撞 | 预放已知目标；两个发布者竞争；不同标题处理成同一文件名 | 既有文件字节不变；不覆盖、不自动改名；至多一方成功 | [test_d_publication.py](../../tests/test_d_publication.py)：`test_concurrent_publishers_have_one_winner`；[test_d_file_workflow.py](../../tests/test_d_file_workflow.py)：`test_different_titles_colliding_after_sanitization_do_not_overwrite` | 真实临时文件 / 组件通过 |
| DF05 暂存写入、生产者及刷盘失败 | 注入分块生成失败、错误 chunk 或 `fsync` 失败 | 不发布不完整文件；仅清理能确认归属的暂存物 | [test_d_publication.py](../../tests/test_d_publication.py)：`test_failure_during_iteration_cleans_only_own_temporary`；`test_fsync_failure_does_not_publish`；`test_invalid_chunk_does_not_publish` | 文件发布器 / 组件通过 |
| DF06 发布前与发布后的取消 | 在开始、暂存中或已知发布之后设置取消事件 | 前期取消不产生正式输出；已知成功不得因迟到取消撤回或误报取消 | [test_d_publication.py](../../tests/test_d_publication.py)：`test_cancel_during_staging_has_no_publication`；`test_cancel_after_publication_does_not_undo_success` | 文件发布器 / 组件通过 |
| DF07 发布回执丢失与暂存身份替换 | 实际发布后抛异常；替换暂存路径；注入清理失败 | 未知保留正式/候选文件，不重试、不交 SAVE 核实；外来替换物不能删除 | [test_d_publication.py](../../tests/test_d_publication.py)：`test_publish_then_exception_is_unknown_and_formal_file_is_retained`；`test_temporary_replacement_is_neither_published_nor_deleted`；`test_cleanup_failure_retains_temporary_and_warns` | 本地 I/O 故障与竞争模型 / 组件通过 |
| DF08 ZIP 条目逃逸、重名、NUL 和特殊类型 | 制作穿越路径、大小写别名、截断 NUL、目录/链接/特殊条目 | 拒绝归档；不调用无约束提取，不把路径正规化成合法条目 | [test_d_zip_guard.py](../../tests/test_d_zip_guard.py)：`test_unsafe_names_rejected`；`test_duplicate_names_and_case_aliases_rejected_before_allowlist`；`test_nul_in_original_filename_is_not_accepted_after_zipfile_truncation`；`test_directories_symlinks_and_special_entries_rejected` | ZIP 只读检查 / 组件通过 |
| DF09 ZIP 损坏、CRC、头信息及不支持格式 | 截断归档、篡改 CRC/本地头、加密或使用不支持压缩 | 失败不写文件；不能把“能列目录”当成内容已验证 | [test_d_zip_guard.py](../../tests/test_d_zip_guard.py)：`test_crc_failure_and_truncated_container_rejected`；`test_local_header_name_disagreement_rejected`；`test_encryption_and_unsupported_compression_rejected` | ZIP 只读检查 / 组件通过 |
| DF10 元数据和实际解压字节超预算 | 限制总文件、条目数、声明大小、实际流式字节及 capture 大小 | 在对应边界拒绝；不只相信 ZIP 声明大小，不引入产品笔记容量上限 | [test_d_zip_guard.py](../../tests/test_d_zip_guard.py)：`test_container_size_checked_before_zipfile_parses_central_directory`；`test_actual_stream_bytes_are_bounded_even_if_metadata_is_smaller`；`test_actual_total_read_and_capture_budgets_are_also_checked` | 显式技术资源预算 / 组件通过 |
| DF11 冻结源读取期间变化或与清单摘要不符 | 在打开前替换源文件、流式读取中修改，或在清单捕获后改写 | 拒绝发布；源文件不删除；实际字节与预期摘要/大小绑定 | [test_d_zip_writer.py](../../tests/test_d_zip_writer.py)：`test_source_changed_during_streaming_is_not_published_or_deleted`；`test_path_replaced_before_open_is_rejected_instead_of_reading_new_file`；`test_mutated_source_since_manifest_capture_rejected_by_hash_binding` | ZIP 写出 / 组件通过 |
| DF12 ZIP 构建、回读、关闭或发布边界失败 | 注入短写、关闭失败、回读 CRC 错误、发布整体回执丢失 | 校验完成前不发布；已跨发布边界而回执未知时保留，不降成“肯定失败” | [test_d_zip_writer.py](../../tests/test_d_zip_writer.py)：`test_short_staging_write_is_rejected`；`test_archive_close_failure_never_reaches_publication`；`test_corrupted_archive_fails_readback_crc_before_publication`；`test_unexpected_publication_boundary_exception_is_unknown_not_false_failure` | ZIP 构建和安全发布组合 / 组件通过 |
| DF13 清单 JSON 形状、重复字段、数字类型和版本错误 | 缺字段、未知字段、重复键、无效 UTF-8/非有限数、bool/float 冒充整数或不支持版本 | 严格拒绝；版本去重前校验原元素，不做数字或文本猜测转换 | [test_d_manifest.py](../../tests/test_d_manifest.py)：`test_missing_unknown_or_wrong_top_shape_fields_fail_closed`；`test_duplicates_are_rejected_including_nested_objects`；`test_wire_numbers_do_not_coerce`；`test_policy_rejects_bool_or_float_before_deduplicating_equal_versions` | D 内部清单 profile，正式格式待共同确认 / 组件通过 |
| DF14 清单时刻精度、时区和来源位置误用 | 输入非 UTC/无时区/不可表达精度；使用不存在的来源位置 | 不悄悄截断时间，不套入 v3 固定毫秒；来源只作历史识别，恢复不访问它 | [test_d_manifest.py](../../tests/test_d_manifest.py)：`test_invalid_time_or_unrepresentable_precision_is_not_silently_truncated`；`test_source_path_is_only_lexically_checked_never_opened`；`test_repeated_encoding_is_deterministic_and_does_not_read_clock` | 清单编解码纯规则 / 组件通过 |
| DF15 清单声明与实际快照或独立事实不符 | 修改摘要、大小、保存数、来源版本或四类引用出现次数 | 拒绝不一致；清单不能覆盖快照业务事实；摘要吻合也不等于数据库有效 | [test_d_manifest.py](../../tests/test_d_manifest.py)：`test_every_snapshot_fact_is_compared_independently`；[test_d_backup_components.py](../../tests/test_d_backup_components.py)：`test_manifest_hash_mismatch_is_rejected_before_publication`；`test_tampered_manifest_is_rejected_on_read_without_mutating_source`；[test_d_file_workflow.py](../../tests/test_d_file_workflow.py)：`test_rejected_container_does_not_extract_or_mutate_any_file` | 容器/清单组合，非 SQLite 校验 / 组件通过 |
| DF16 恢复引用误换绑、失效复活或新身份碰撞 | 同局部 note ID 存在于其他集合、同名已删/缺失目标、新集合 ID 等于外来标记归属 | 仅转换源归属；保持 note ID 与原分类；第三集合引用不改；冲突拒绝，不按名字猜目标 | [test_d_restore_references.py](../../tests/test_d_restore_references.py)：`test_same_note_id_in_other_collection_does_not_rebind`；`test_deleted_and_missing_same_name_remain_invalid_and_keep_note_id`；`test_new_identity_matching_known_foreign_marker_is_rejected`；`test_same_backup_converts_independently_to_two_new_collections` | 纯文本转换，非初始化回执 / 组件通过 |

## 3. D01：会话与三个服务故障

| 场景 ID / 故障 | 触发方法 | 必须保护的对象和判定 | 准确测试入口 | 范围 / 实际状态 |
|---|---|---|---|---|
| DF17 会话错误、未确认 SAVE、过期 Context 或草稿决策失效 | 使用坏 buffer/ack 形状、假 `NO_UNSAVED`、捕获时新草稿或新编辑版本 | 阻止输出，保留草稿；导出只检查选中笔记，备份检查全部；不能沿用旧版本批准 | [test_d_output_support.py](../../tests/test_d_output_support.py)：`test_state_rejects_mutable_duplicate_and_malformed_buffer_fields`；`test_unconfirmed_save_and_stale_context_block_output`；`test_new_draft_or_new_edit_requires_new_decision_during_capture`；`test_export_choice_only_covers_selected_note_but_backup_covers_all` | 纯会话规则 / 本轮模块检查通过，非真实 Qt 会话 |
| DF18 外层协程取消、子任务丢失及旧结果投递 | 取消等待者或底层任务，在 worker 中挂起实际工作 | 取消只记录意图；等待实际结果；未知不伪造回滚；delivery 保持来源 Context | [test_d_output_support.py](../../tests/test_d_output_support.py)：`test_outer_cancel_sets_intent_but_waits_for_actual_result`；`test_underlying_cancel_is_not_fabricated_as_rollback`；`test_result_delivery_stays_bound_to_origin_and_status_shape_is_checked` | 通用服务支撑 / 本轮模块检查通过 |
| DF19 API07 无保存版、错误投影或脏版本变化 | 选中新草稿、C 返回错 Context/修订、捕获或渲染期间继续编辑 | 不读草稿、不自动保存；无保存版不读源；错投影不发布；选中脏版本变化须重新确认 | [test_d_export_service.py](../../tests/test_d_export_service.py)：`test_new_draft_rejected_without_source_read`；`test_saved_projection_identity_and_revision_fail_closed`；`test_dirty_changes_after_render_before_publish_abort`；`test_dirty_selected_note_uses_saved_name_body_and_relations` | API07 D 侧编排 / 替身服务通过 |
| DF20 API07 文件失败、回执形状错误、发布未知和迟到取消 | 出现既有目标、坏 ERROR/CANCELLED 回执、回执丢失，或发布时取消/切换 Context | 非覆盖；坏回执保守 UNKNOWN，不删文件不重发；已知成功和来源 delivery 不撤回 | [test_d_export_service.py](../../tests/test_d_export_service.py)：`test_target_collision_never_overwrites`；`test_malformed_failure_and_unknown_receipts_are_not_known_failures`；`test_outer_cancel_during_publication_waits_for_real_success`；`test_context_changes_after_known_publish_keep_success_on_original_context` | API07 D 侧编排 / 替身服务通过 |
| DF21 API08 全部草稿提示、捕获事实或源上下文错误 | 存在新建/正文/名称/标签/引用脏状态，或捕获身份、计数、摘要错误 | 备份只含实际保存快照；批准绑定全部当前脏版本；时间来自捕获，错误不伪造回复 | [test_d_backup_service.py](../../tests/test_d_backup_service.py)：`test_all_draft_categories_block_untrue_no_unsaved_claim`；`test_edited_confirmed_draft_during_capture_is_not_silently_excluded`；`test_independent_c_facts_mismatch_never_reaches_publisher`；`test_real_archive_uses_capture_facts_and_microsecond_timestamp` | API08 D 侧编排、真实临时 ZIP，C 捕获为替身 / 替身服务通过 |
| DF22 API08 捕获未知、发布未知、释放失败及外层取消 | 捕获无法确认、发布回执丢失、C 资源释放抛错，或释放时取消等待者 | 不编造时间/数量/发布成功；释放仅由 C 清理其资源；已知备份成功只附警告，不删除或重发 | [test_d_backup_service.py](../../tests/test_d_backup_service.py)：`test_capture_unknown_has_no_fake_time_or_publication`；`test_unknown_publisher_outcome_is_not_replayed_or_deleted`；`test_release_failure_is_warning_not_false_publication_failure`；`test_outer_cancel_during_source_release_waits_without_retracting_backup` | API08 D 侧编排 / 替身服务通过 |
| DF23 API09 非空目标、事实不符或初始化回执不合法 | 目标预放文件；C 检查事实与清单不符；新身份/目标/逐篇回执绑定错误 | 目标既有内容、原集合和备份不变；未通过真实受控初始化不允许发布；不自动打开 | [test_d_restore_service.py](../../tests/test_d_restore_service.py)：`test_nonempty_target_is_rejected_without_changing_existing_content`；`test_independent_facts_mismatch_stops_before_initialization`；`test_invalid_initialization_receipt_count_or_binding_never_publishes`；`test_complete_d_flow_reports_only_substitute_publication_and_never_activates` | API09 D 侧编排，C/A 均为显式替身 / 替身服务通过 |
| DF24 API09 初始化未知、坏发布回执及迟到 Context/取消 | 初始化未知或修改输入快照；发布后丢回执；裸字符串冒充状态枚举；取消或切换发生在已知发布之后 | 未知保留候选，不宣称已发布；正式产物不清理；不把文件未知送 SAVE 核实，不自动激活 | [test_d_restore_service.py](../../tests/test_d_restore_service.py)：`test_unknown_initialization_retains_candidates_without_claiming_publication`；`test_snapshot_mutation_by_initializer_is_detected_before_publication`；`test_malformed_publication_reply_is_unknown_not_cleaned_as_failed`；`test_string_publication_status_is_never_mistaken_for_known_failure`；`test_late_cancel_after_known_success_remains_success`；`test_context_change_after_publication_does_not_undo_output` | API09 D 侧编排 / 替身服务通过 |

## 4. D02：受控候选提取和归属保护

| 场景 ID / 故障 | 触发方法 | 必须保护的对象和判定 | 准确测试入口 | 范围 / 实际状态 |
|---|---|---|---|---|
| DF25 检查后换包、CRC 错误或无限制读取 | 记录打开次数/读取块大小；篡改内容 CRC；验证仅接受两项必需条目 | 在同一次受控读取中验证并提取；原备份不变；提取候选不能冒称有效数据库 | [test_d_restore_candidate.py](../../tests/test_d_restore_candidate.py)：`test_source_is_opened_once_and_members_are_read_in_bounded_chunks`；`test_corrupt_snapshot_crc_rejects_without_touching_backup`；`test_only_two_required_entries_are_accepted` | 真实临时归档/文件 / 候选通过 |
| DF26 提取预算、写入/刷盘/关闭失败或中途取消 | 限制实际成员字节；注入写、flush、fsync、close 失败；流中设置取消 | 不发布候选；不创建缺失父目录；仅回收已确认自有未发布产物 | [test_d_restore_candidate.py](../../tests/test_d_restore_candidate.py)：`test_actual_oversized_member_bytes_are_rejected_not_just_metadata`；`test_write_flush_fsync_and_close_failures_are_typed_and_cleaned`；`test_midstream_cancel_cleans_own_candidate`；`test_missing_parent_is_typed_failure_not_created` | 文件流水线，不写 SQLite / 候选通过 |
| DF27 输入或候选同路径替换、同大小隐蔽改写 | 打开前替换源；读取后改写源；保持候选大小和时间戳但改变摘要 | 拒绝被替换/变更内容；不只用路径或大小/时间证明内容有效；原备份不删除 | [test_d_restore_candidate.py](../../tests/test_d_restore_candidate.py)：`test_source_replaced_before_open_is_rejected`；`test_source_change_after_read_is_rejected_and_candidate_removed`；`test_candidate_digest_check_catches_same_size_timestamp_preserving_mutation` | 受控 I/O 与候选绑定 / 候选通过 |
| DF28 候选归属无法确认、外来文件抢占、清理失败或源链接 | 替换自有目录/文件、抢先创建候选名、注入 unlink 失败，或尝试链接源 | 外来内容绝不截断/删除；归属未知保留并警告；只允许对确认自有文件重试清理 | [test_d_restore_candidate.py](../../tests/test_d_restore_candidate.py)：`test_cleanup_never_removes_replacement_or_unrelated_file`；`test_foreign_file_winning_exclusive_candidate_name_is_not_truncated`；`test_unlink_failure_is_reported_and_retry_only_cleans_confirmed_owned_file`；`test_actual_source_link_is_rejected_without_following_or_deleting` | 普通候选故障通过；实际链接源创建项为权限跳过。既有 [ZIP 源链接项](../../tests/test_d_zip_guard.py) `test_final_symlink_rejected_when_host_allows_creating_it` 同样跳过，不算通过 |

## 5. A 既有协作边界，仅提供导航

以下属于 A 的既有受控模块检查，本轮没有代替 A 新增执行或填写产品验收结果：

- [test_collection_io.py](../../tests/test_collection_io.py)：`test_publication_cannot_overwrite_file_created_after_prepare`、`test_cancel_cleanup_removes_only_owned_candidate`，用于对齐正式集合发布/清理边界。
- [test_collection_coordinator.py](../../tests/test_collection_coordinator.py)：`test_registration_failure_after_publication_retains_valid_new_collection`、`test_publication_unknown_is_not_treated_as_source_save_unknown`，用于对齐发布/登记/激活和 SAVE 未知的区别。
- [test_qt_runtime.py](../../tests/test_qt_runtime.py)：`test_stale_result_is_dropped_when_queued_callback_actually_runs`、`test_shutdown_preserves_unknown_artifacts_and_reports_inspection_needed`，用于对齐真实 Qt 回调和退出收尾。D 服务替身测试不能替代这层接线。

## 6. 复现与尚未执行的产品场景

在仓库根目录可单独复核 D 测试或按准确文件名选择一组：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_d_*.py" -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_d_restore_candidate.py" -v
```

完整仓库检查使用 `.\.venv\Scripts\python.exe scripts/check.py`。只建立索引、读取源码或看到测试方法名，均不等于本次执行通过。

以下仍需 D04/D05 真实接口接入后执行，不能由上表勾选代替：

- B 的实际编辑器保存版/草稿分离、完整限制提示、所有草稿选择和可辨成功/失败/未知反馈。
- C 的一致 SQLite 保存快照、共享解析授权跨度、业务事实校验和逐篇 `RESTORE_NOTE_INIT` 真实回执。
- A 的真实恢复产物发布、身份登记、显式打开和最终集合激活；恢复本身不自动打开。
- 完整 V07/V08/V09/V16 及 D 所配合的 V14/V15、离线/隐私/Windows 路径场景。性能规模也必须实际运行后另记，不用本故障索引声称通过。

出现新需求、共同接口变更或真实故障时，先在 [D 总清单](d-progress.md)登记影响，再扩展对应 DF 行或明确增加场景；常规实现步骤不重复包装成新发现的工作包。
