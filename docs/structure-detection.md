# 论文 PDF 结构判定：事故复盘、现行逻辑与改造路线

> 2026-08-12 建立。起因：zhao2020rational（Science，Rational design of layered
> oxide materials for sodium-ion batteries）转换产物整页丢失正文第二页（文字+图），
> 排查确认根因在 converter 规则层的封面页误判，而非任何 OCR 引擎。
> 本文档是该主题的推进中枢：复盘 + 现行逻辑全貌 + 外部调研 + AB 记录 + 后续 spec。

## 一、事故复盘（2026-08-12）

### 现象

- zhao2020（5 页 Science 综述）转换后产物只剩 3 页锚点，正文第二页（ionic
  potential 一节，含图）整体消失；
- 换 OCR 提供商（PaddleOCR-VL / MinerU-VLM / MinerU pipeline）复现完全一致；
- 多篇论文受同样影响。

### 根因

旧封面判定（`content_processor.py` 与 `metadata.py` 各一份相同拷贝）：

1. 检查 page 0/1 两页；
2. 把页面**所有块文本**拼成大字符串（正文/页眉/图注/公式不分块类型）；
3. 数 7 个仓库模板关键词命中数，≥2 即**整页丢弃**（无声、无日志、无申诉）。

zhao2020 的 page 1 命中两个词：

- `downloaded from`——Science 期刊每页页眉标配 "Downloaded from science.org..."；
- `university of technology`——页内引文中的机构名。

两个高频正文词凑一对即触发死刑。这解释了"换引擎无效"：丢页发生在引擎产物
**之后**的规则层，是确定性行为。

### 影响面（126 篇 zotero-brain 解析缓存全量扫描 + 事故 staging）

| 篇目 | 页 | 旧判定 | 实际 | 定性 |
|---|---|---|---|---|
| 26NNZJHX | page 0 | 切 | TU/e 仓库引用声明封面（7/7 标记） | 合法切除 |
| D9AVT22J | page 1 | 切 | 正文（prose 5357 字符，含公式+图） | **误杀** |
| L2KRJ8KZ | page 1 | 切 | 正文（prose 5160 字符，含公式+图） | **误杀** |
| zhao2020 | page 1 | 切 | 正文（prose 6028 字符，含公式+图） | **误杀** |

其余 123 篇新旧规则均为空判定，零扰动。

### 关键教训

- 关键词计数式整页切除对"高频正文词撞车"零防御，且失败静默；
- 下游 QC 闸（`qc_paper.py` 页锚对照）能**检出**这类事故，但**救不回**——
  丢页是确定性规则，重试/换引擎每次都切同一页，只会白烧 OCR 配额；
- 结构性丢页的修复必须在规则层根治，QC 闸只作兜底。

## 二、现行结构判定逻辑全貌（修复后）

### 封面页判定（cover_detect.py，2026-08-12 根修）

新判据层层收紧，原则：**宁可漏切噪声封面，绝不误杀正文**。

1. **只判 page 0**——封面只可能在首页，page 1 及以后永不判封面；
2. **标记命中 ≥3**（`COVER_MARKER_THRESHOLD`；真封面样本 26NNZJHX 是 7/7）；
3. **正文信号一票否决**（任一命中即非封面）：
   - 页内含 image/equation/table 块；
   - 散文总量 >1500 字符（`COVER_PROSE_VETO_CHARS`）；
   - 单块散文 >300 字符（`COVER_LONG_BLOCK_VETO_CHARS`，摘要/正文段形态）；
   - 编号章节标题（"1. Introduction" 形态）；
4. **元数据锚定否决**：标记偏弱（<5）且权威标题/DOI（Zotero）出现在页内
   → 保留。真仓库封面模板标记通常全套（≥5）且同样列有论文标题，故锚定
   只拦"标记偏弱"的可疑判定；
5. 每次判定输出 INFO 日志（命中标记 + 否决原因），不再无声丢页。

实测分界：真封面 prose≤1233 字符、无富内容块、长块(>300) 为 0；真首页
prose≥2559 且含 image/equation 或多个长段。分界余量 2 倍以上。

`legacy_detect_cover_pages` 原样保留仅供 AB 对照，管线不再调用。

### 其余结构判定（既有逻辑，未改动）

- **噪声块过滤**：引擎标注的 `header/footer/page_number/aside_text` 直接丢弃；
  `page_footnote` 丢弃（作者信息已提取到 metadata）；
- **出版噪声清洗**（`_clean_paragraph`）：文章类型标签、DOI+日期行、版权声明、
  "You may also like" 推荐列表、RSC 页脚前缀等——块级丢弃或前缀剥离；
- **标题结构分类**（`_classify_headings`）：规则启发式 + 可选 LLM 校正，
  区分真章节/图注/噪声/副标题；
- **References 切分**：固定段标题表（`_FIXED_SECTIONS`）+ 引文条目模式；
- **完整性闸**（`qc_paper.py` + `pipeline.py`）：图/表编号断号、页锚计数对照
  （≤60% 实际页数判整页丢失）→ 打回重解析 → 换引擎链 → 最佳产物保留 →
  仍不完整则交付并打标 `incomplete`（SageRead 侧 toast 警告）。

## 三、外部调研结论（2026-08-12）

业界论文 PDF 结构化的分层格局：

| 层次 | 代表方案 | 结论 |
|---|---|---|
| 解析引擎（布局/公式/表格/阅读顺序） | MinerU 2.5（OmniDocBench 第一）、PaddleOCR-VL、Marker、Docling、olmOCR | 我们已用前两名，引擎层无可薅增量 |
| 结构化元数据/边界提取 | GROBID（ML 切分 title/abstract/refs/sections，事实标准）、Science-Parse v2、S2ORC doc2json | 解决"干净论文结构切分"；GROBID 为 Java 服务引入成本高；对脏 PDF 同样无能为力 |
| 图表专用 | PDFFigures2 | figure_merger 已覆盖同类需求 |

关键结论：

- **没有一家用"关键词计数丢整页"**——业界靠布局模型块标注（MinerU 已提供）、
  元数据锚定（标题/DOI 定位正文起点）或 VLM 整页判断；
- 我们的"辅助模型判页面角色"思路与 olmOCR/GROBID 同构，方向正确；
- 脏 PDF（杂志截页）边界的最佳实践是**用 Zotero 权威标题/DOI 做锚点**切前后
  污染内容——该信号我们手里本来就有，比纯视觉判断更可靠，VLM 作锚点失败兜底。

## 四、辅助模型结构判定通道（spec，默认关闭）

规则到达极限后的升级路径。`config.STRUCTURE_LLM`（默认 off）。

- **输入**：page 0-2 的块文本（截断至预算内，纯文本、禁思考模式、低 max_tokens）；
- **输出**：JSON `{cover_pages: [], body_start_page: int}`；
- **冲突裁决（保守方向优先）**：
  - LLM 判"非封面"可推翻规则的"是封面"——宁可保留疑似噪声页也不丢内容；
  - LLM 判"是封面"仅当规则也判封面或置信极高、且仅对 page 0 时才生效；
- **触发时机**：规则判定结果置信度低（标记数在阈值边缘）或产物被 QC 闸打回时
  才调用，不增加常规转换成本；
- **环境变量**：复用 SageRead 已透传的 DEEPSEEK_* 链路，headless 协议不变。

## 五、脏 PDF 边界检测（spec）

针对"杂志截页"类污染 PDF：目标文献前挂着上一篇的结尾与参考文献，后跟下一篇
的标题摘要。纯规则无法可靠判断文章起止，采用**锚点主通道 + 模型兜底**：

- **主通道（规则，本轮落地最小实现）**：有 Zotero 权威标题时，全文定位目标
  标题出现处，切掉其前的他文内容；References 结束后若再出现新的标题式块且
  其后无本文章节 → 截尾；
- **兜底通道（下一轮）**：锚点 0 命中或多命中时走辅助模型判定文章起止
  （需要真实脏样本验证后启用）。

## 六、AB 测试记录

### 2026-08-12 封面判定新旧规则对照（127 篇）

语料：zotero-brain 126 篇解析缓存 + zhao2020 事故 staging。
复现命令：`python ab_cover_detect.py --extra ".tmp-qc-gate-run\_staging\*\*_content_list.json"`

| 指标 | 旧规则 | 新规则 |
|---|---|---|
| 切页总数 | 4 | 1 |
| 误杀 | 3（D9AVT22J/L2KRJ8KZ/zhao2020 正文页） | 0 |
| 漏切真封面 | 0 | 0（26NNZJHX 仍正确切除） |
| 其余 123 篇 | 空判定 | 空判定（零扰动） |

结论：新规则在语料上零误杀零漏切，判定差异全部朝"保全正文"方向。
回归防线：`test_cover_detect.py`（14 用例，含合成样例 + 真实事故样本）。
