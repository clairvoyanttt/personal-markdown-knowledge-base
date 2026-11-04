# B 第一波交付：标签与可选保存版预览

基线：0c5af6644a53f027678dd1aea306800ef95e8c37。分支：feature/b-tags-preview。
按用户交接清单执行，仅恢复第一波功能；不合并 main，不改变原最小验收结论。

## 标签

MVP 恢复标签输入、添加、移除、标签列表和筛选输入。查询不再强制 tags=()。
无关键词但有标签时，后台用 store.snapshot() 同一保存快照过滤；搜索模式由现有查询服务处理。
标签遵循共享校验：空值、控制字符、30码点、20个不同标签上限，大小写区分、重复不新增。
未保存增删不会改变筛选，保存失败保留输入及原保存标签。MVP 其他延期入口保持隐藏。

## 可选预览

默认关闭；运行 pmkb --preview 或 python -m pmkb --preview 启用。
调用方也可传 create_main_window(..., mvp_mode=True, use_collection_storage=True, enable_preview=True)。
不取消 MVP 裁剪。集合成功激活后只绑定 SharedMarkdownPreview(store)，不绑定导出源。

本轮预览按交接文件限定为保存版：先后台 load_note 读保存正文，再后台共享解析/HTML渲染。
未保存修改不进入预览，界面明确提示。新草稿提示先保存。正文 UTF-8 超过 204800 字节（200 KB）
直接提示并跳过渲染，不修改或截断源文本。提交回调校验整个捕获草稿快照和集合上下文，丢弃迟到结果。
外部资源不自动加载；不要求完整 CommonMark，显示标题、段落、列表、链接、引用块和围栏代码。

## 实际开发自检

- 标签提交 377ae23：scripts/check.py 597项发现、595通过、2项符号链接权限跳过，
  21.634秒，exit 0。
- 标签+预览：scripts/check.py 598项发现、596通过、2项同类跳过，52.030秒，exit 0。
- MVP定向回归4项通过：标签保存/失败/筛选/校验、预览开关、只读保存版、超限、
  Qt心跳和服务循环心跳。原有最小核心行为回归仍通过，不能替代7条人工验收。
- scripts/check_b_tags_preview.py：Windows原生Qt临时合成数据检查通过。
  两篇不同标签笔记分别命中；未保存标签移除不影响筛选；六类结构显示；
  源正文未被渲染改写；未保存正文不替代保存预览。退出后runtime和storage关闭。
- git diff --check通过。

本机证据：artifacts/b-tags-preview/result.json、tags-saved-filter.png、
six-structure-preview.png、unsaved-warning.png。脚本可复跑；证据未作为人工签名或正式验收。
检查输出保留于本地 b-tags-check.log 与 b-tags-preview-check.log，不上传个人数据库。

## 限制与未执行

预览为显式开启的保存版预览，不宣称原FR03“即时预览未保存正文”完整达标；
不解释公式、表格、完整嵌套Markdown，链接不自动访问。200KB限制仅针对预览，不限制保存。
未执行人工正式验收、断网验证、干净wheel安装和重新打包；由A集成后再组织。
第二波导出/引用及第三波删除/改名/恢复均未开放；C/D业务文件和README、需求文档未修改。
