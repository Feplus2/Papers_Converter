> **⚠️ 历史调研档案（止于 2026-08-03）**：本文保留作调研记录，不再维护。
> 文中"碎图发生在识别阶段、后处理无法无损还原，出路是换/多配上游引擎"的
> 结论已被 **2026-08-11 图组并集光栅重裁**（`figure_merger.py`，见 README
> "图组并集重裁"节）推翻——碎图按 bbox 并集从源 PDF 整幅重裁，MinerU 与
> PaddleOCR 两引擎均受益。引擎现状（GLM 已下线）、默认引擎与换引擎兜底链
> 一律以 README 为准。

# OCR/文档解析引擎调研与多引擎计划（2026-07-30）

本文档沉淀 2026-07-30 的调研结论、架构决策和后续路线图。背景：MinerU+无视觉 LLM
方案在"图很多的论文"上效果不佳——MinerU 把复合图切得很碎、图文位置匹配差，
下游 LLM 修不好。结论：**把 Stage 1 抽象成可插拔 Provider，官方维护 2–3 个云
API 适配器，本地引擎留给开发者用户自行适配。**

## 一、关键判断（先读这个）

1. **"图切碎"发生在引擎的识别阶段，不是后处理能修的。** MinerU VLM 后端把复合图
   拆成子图是设计行为（为图表解析服务），官方确认无配置项可关闭
   （[Issue #4163](https://github.com/opendatalab/MinerU/issues/4163)、
   [#4008](https://github.com/opendatalab/MinerU/issues/4008)）。像素层面的拆分
   一旦发生，任何下游 LLM（有无视觉）都无法无损还原。因此出路是换/多配上游引擎，
   后处理只负责"不重排像素"的工作：caption 对齐、跨页合并、层级修复。
2. **"任意引擎都能接"的通用框架做不到。** SageRead 契约要求 `images/` 里有真实
   裁剪图（`![Figure 3: caption](images/fig3.png)`）。但很多引擎不返回裁剪图：
   dots.ocr/dots.mocr 只给 bbox（图表甚至直接转 SVG），olmOCR 2 纯文本不抽图，
   DeepSeek-OCR 原始输出是 grounding 坐标要自己裁。能同时满足"返回裁剪图 +
   结构化布局"的云 API 就那么几家（见下表）——所以框架通用（统一产物契约 +
   注册表），但官方只维护少数适配器。
3. **MinerU 云端的 vlm/pipeline 两个后端都值得保留。** `vlm` 精度高但拆子图；
   `pipeline`（DocLayout-YOLO 路线）按整块裁图不拆子图，是碎图问题的天然对照组。
   用 `--model pipeline` 即可 A/B（见第四节）。

## 二、候选引擎对比（云 API，2026-07 核实）

| 候选 | 云 API | 裁图返回 | 布局 JSON | 公式/表格 | 价格量级 | 中文 | 结论 |
|---|---|---|---|---|---|---|---|
| **MinerU 云**（当前） | ✅ | ✅ | ✅ content_list | 强 | 免费 1000 页/日 | 强 | 基线，保留 |
| **百度 PaddleOCR-VL API** | ✅ | ✅ | ✅ Markdown/JSON | 强；**图表→结构化数据（独家）** | ¥0.07–0.18/页 | 强 | **优先新增** |
| **智谱 GLM-OCR** | ✅ bigmodel.cn | 部分 | ✅ | 强；OmniDocBench v1.6 一度登顶(95.15) | 按 token，低 | 强 | **优先新增** |
| **Mistral OCR 3** | ✅ | ✅ base64+bbox | ✅ | 强（以 arXiv 为标杆） | $1–2/1000 页 | 中 | 海外用户首选 |
| TextIn xParse（合合） | ✅ | ✅ | ✅ 带坐标溯源 | 中强 | 商用按页，有免费额度 | 强 | 备选（中文稳定性好） |
| Reducto / LandingAI ADE | ✅ | ✅ | ✅ chunk 级 bbox（图文对齐标杆） | 中 | $15–30/1000 页，免费额度大 | 弱 | 备选/借鉴其 chunk 结构 |
| LlamaParse | ✅ | ✅ | 部分 | 中 | 免费 1 万页/月 | 弱 | 可白嫖实测，不优先 |
| DeepSeek-OCR(2) 托管（硅基/Novita） | ✅ | ❌ 需自裁 | 原始坐标 | 中 | 极便宜 | 强 | 只适合自建管线，不接 |
| Qwen3-VL / 阿里 Document Mind | ✅ | ❌ | qwenvl html 带坐标 | 中 | 按 token | 强 | 通用 VLM，可作"二次精读"工具 |
| Mathpix | ✅ | ❌ | ❌ | **公式最强**，版面弱 | $3.5/1000 页 | 弱 | 公式兜底通道（Image API $0.002/张） |
| Azure / Google / AWS 文档 AI | ✅ | ❌ | ✅ | 弱 | $10–15/1000 页 | 中 | **排除**（学术弱且贵） |
| 百度 Unlimited OCR | ❌ 尚无独立 API（2026-07 刚曝光） | — | — | — | — | — | 观望 |

本地开源引擎（不接，但适配器接口对开发者开放）：MinerU 本地版、PaddleOCR-VL-1.6
（`pip install paddleocr`，0.9B 硬件要求最低）、Chandra 2（`pip install chandra-ocr`，
英语学术最强梯队，注意商用授权）、MonkeyOCR（块间关系 middle.json 对图文匹配最友好，
但需 clone+LMDeploy）、Marker（插件式后处理框架，见其 Processor 设计）。

## 三、现成后处理工作流调研结论

**没有完全能抄的成品**，但有三样可借：

- **Marker**（`pip install marker-pdf`）："解析 + Processor 插件链 + `--use_llm`
  混合修正"框架。LLM 只做跨页表格合并、块修正、图片描述——这正是后处理的正确
  边界。我们的 content_processor.py 已是这个思路，不换，但新增 provider 时可参考
  其插件机制。
- **Docling 的 DoclingDocument**：业界最成熟的统一文档 IR。决策：**不迁移**，
  继续用 MinerU content_list 格式作为契约（官方承诺 middle_json 兼容下游二次开发，
  已是事实标准；现有后处理迁移成本高）。
- **OmniDocBench 工具链**：内置约 30 个引擎的推理脚本和归一化评测代码。多引擎
  A/B 时用它做 benchmark 脚手架，别自己拍脑袋看效果。

## 四、Provider 契约（已实现，见 `ocr_provider.py`）

```python
class OcrProvider(Protocol):
    name: str
    def parse(self, pdf_path, work_dir, ocr=True, progress=None, **opts) -> dict:
        ...
```

落盘产物约定（pipeline 只依赖落盘产物，不依赖返回值）：

```
{work_dir}/{stem}_content_list.json   # 块列表：type/text/text_level/page_idx/img_path/...
{work_dir}/{stem}.md                  # 引擎直出 markdown（留档，下游不消费）
{work_dir}/images/                    # 裁剪图文件，content_list 的 img_path 指向文件名
```

新增引擎两种方式：

1. 内置：写 `stage1_xxx.py` 实现协议，在 `ocr_provider._BUILTIN` 登记。
   layout 系引擎（块标签为 text/figure_title/chart/... 风格）直接复用
   `stage1_layout.convert_layout_blocks()`，只需写原始块归一化 + 图片 URL 解析。
2. 外挂（本地引擎/私有适配）：`ocr_provider.register(name, factory)`。

当前内置：`mineru`（默认）、`glm`、`paddleocr`。

使用：

```bash
# .env: OCR_PROVIDER=mineru（默认）
python pipeline.py paper.pdf --provider mineru --model pipeline   # 后端 A/B
```

## 五、路线图

- [x] 升级检查：mineru-open-sdk 已是最新 0.2.5（云端模型服务端自动是最新，
  vlm 即 MinerU2.5-Pro 系）；`--model pipeline` 提供整块裁图对照后端
- [x] Stage 1 抽象为 Provider 契约 + 注册表（`ocr_provider.py`），MinerU 适配器落地
- [x] **A/B：MinerU vlm vs pipeline**（hu2011macrophages，9 页，2026-07-30，
  产物在 `output_ab_pipeline/`）：**pipeline 不是碎图问题的解法，vlm 保持默认**。
  - pipeline 修好了 vlm 的知名 badcase：Figure 7 图注正确绑回（vlm 版只能以编号段落保留）
  - 但 pipeline 整页漏检 Figure 1/5/6（28 张图全属于 Fig 2/3/4/7），且公式质量明显
    退化（`$^ { \star \star \star } P <$`、`$( 1 \times 1 0 ^ { 8 } )$` 间距碎裂，
    vlm 输出干净）；还出现 `huCD45+⁺` 这类残留上标伪影
  - 本文子图本就是物理分离的 panel（两后端各得 28 张），"碎图"在该样本上不成立；
    真正的改进要等 PaddleOCR-VL / GLM-OCR 适配器同集对比
- [ ] **新增百度 PaddleOCR-VL 适配器**（重点：图表→结构化数据；确认裁图返回形式）
- [x] **新增智谱 GLM-OCR 适配器**（`stage1_glm.py`，`--provider glm`）
- [x] **新增百度 PaddleOCR-VL 适配器**（`stage1_paddleocr.py`，`--provider paddleocr`）
- [x] **抽出共享转换层 `stage1_layout.py`**：GLM 与 PaddleOCR-VL 标签体系几乎相同
  （PP-DocLayout 风格），标签映射/图注绑定/公式规范化只维护一份；新 layout 系引擎
  接入只需写"原始块归一化 + 图片 URL 解析"两层薄皮
- [x] **GLM 公式伪影修复**（`stage1_layout.normalize_math`）：` $ ^{1-3} $ ` padding
  定界符收紧 + `\mathrm{C D 3 4}` spaced-letter 合并。该 bug 同时是作者栏解析
  炸裂的根因（元数据规则按 `$^{...}$` 无空格形态编写），修复后作者/摘要提取正常
- [x] 三引擎同集对比（hu2011/teoh2010/cao2022/chen2019，结论见五之三；未用 OmniDocBench）
- [ ] （可选）Mistral OCR 适配器（海外用户）；Reducto 免费额度验证图文对齐

## 五之二、GLM-OCR 适配器实录（2026-07-30，hu2011macrophages 9 页）

接入：POST `{GLM_OCR_BASE_URL}/api/paas/v4/layout_parsing`，base64 data-URI 上传，
同步返回 `layout_details`（逐页块：bbox_2d/content/index/label/native_label）
+ `md_results`；`return_crop_images=true` 时图片块 content 为带签名裁剪图 URL
（约 24h 过期，须即时下载）。单请求 ≤100 页/50MB。

**结果对比（同篇 hu2011，产物 `output_ab_glm/`）**：

| 维度 | MinerU vlm | GLM-OCR | PaddleOCR-VL-1.6 |
|---|---|---|---|
| 图绑定 | Figure 7 图注绑不回（知名 badcase） | **Figure 1–7 全部正确绑定** | **Figure 1–7 全部正确绑定** |
| 标题层级 | 正确 | 正确 | 正确 |
| 图数量 | 28 | 32（都会拆子图，粒度不同） | 32 |
| 公式 | 干净 | 干净（伪影已由 normalize_math 修复） | 干净 |
| 页脚/DOI | 有 footer 块 | **不返回页脚** → date 缺源 | **有 footnote** → date=2011、DOI 提取成功 |
| 速度/费用 | 免费额度，~30s | ~40s，9 页 ≈ 44k tokens | **~13s**（异步 job），有每日页数配额 |
| frontmatter(qc) | 全 | date 缺 | container-title 缺（无刊名来源，Zotero 流程无影响） |

**PaddleOCR-VL 接入要点**（2026-07-30 探针确认）：

- 异步 job API（文档：[异步API使用文档](https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5)）：
  multipart 提交 → 5s 轮询 → `resultUrl.jsonUrl`；单任务 ≤1000 页无需分片。
- 块结构在 `prunedResult.parsing_res_list`（**阅读顺序**，标签体系与 GLM 几乎
  相同）——不要用 markdown 切分，用块结构。
- 图片块 `block_content` 为空，裁剪图在 `markdown.images`，键名内嵌 bbox
  （`img_in_chart_box_x1_y1_x2_y2.jpg`），实测 33/33 精确匹配。
- 有每日页数配额（错误码 12001），批量转换时注意。

**适配器设计要点**（踩过的坑，后续适配器参考；1–4 已沉淀为 `stage1_layout.py`
共享层，glm 与 paddleocr 两个适配器共用）：

1. **图注必须在适配器内挂回图片块**（归一化为 MinerU `image_caption` 语义）。
   GLM 图注是独立 figure_title 块，直接透传会让下游分组规则拿不到组边界，
   整篇图坍缩成一个未编号大组。启发式：图注后紧跟连续图片段→绑该段末图
   （图注在前版式）；否则绑图注前最近的未绑图（图注在后版式）；都不满足
   →降级文本块走下游游离图注绑回。
2. panel 字母标（"A"/"(b)"）也是 figure_title 块，直接丢弃。
3. 页眉/页脚块要**保留文本**（元数据规则提取读它们找 DOI）；但 header_image
   的 content 是 URL，必须置空。
4. 下游 `_assign_figure_numbers` 无编号组字母越界 bug 已修（`letters[j % 26]`）。
5. GLM 的块类型：label∈{text,image,…}，native_label 已见 text/paragraph_title/
   doc_title/abstract/reference_content/figure_title/image/chart/header_image；
   ~~表格/公式标签待验证~~ → 已由 teoh2010 表格样本验证（见五之三对比）。
6. `normalize_math` 的双 padding 规则必须有"起始 $ 前向排除"（前一个公式收尾 $
   会被误当起始，把两段公式间的正文吞成 $…$），改动后必须跑全量单测用例。
- [ ] （可选）公式兜底：主引擎低置信公式走 Mathpix Image API（$0.002/张）
- [ ] SageRead 侧无需改动（格式契约不变）
- [ ] Paddle 的 Unicode 下标风格（LiCoO₂）与契约 `$...$` 公式的统一决策：
  渲染都能显示，但库内风格混用；如需统一可在适配器加 Unicode↔LaTeX 转换
- [ ] GLM/Paddle 的跨页续表未触发合并（续页表头列序乱 → 保守拒绝），
  需要时研究放宽 `_merge_cross_page_tables` 判据
- [ ] Paddle 表格列错位案例收集（teoh Table 1 局部）

## 五之三、三方对比与定论（2026-07-30，hu2011/teoh2010/cao2022/chen2019）

产物：`output_cmp_{mineru,glm,paddleocr}/`（裸 PDF + --no-llm，44 页 × 3 引擎）。

| 维度 | MinerU vlm | GLM-OCR | PaddleOCR-VL-1.6 |
|---|---|---|---|
| 图注绑定 | **差**：cao2022 只绑 5/9（Fig 2/3/6/8 图注游离成正文段）；hu2011 Fig 7 绑不回 | **优**：9/9、7/7 | **优**：9/9、7/7 |
| 表格（teoh 跨 7 页大表） | **优**：rowspan 正确、单元格干净、跨页合并成 1 表 | 差：7 表未合并，单元格串接（"Li-ZnOLiMn2O4…"）丢数据 | 中：5 表未合并，LaTeX 好但有列错位（字面 `\n` 已修） |
| 正文公式 | 干净 LaTeX | 残留 `\mathrm{LiCo O_{2}}` 空格、`\left\right` 膨胀；发现 1 处坐标轴文字（"Voltage (V)"）泄进正文 | 干净但**用 Unicode 下标**（LiCoO₂、Li⁺/Na⁺）而非 `$...$`（见待办） |
| 引文标记 | `$^{[33]}$`（上标化，偏离契约"保留 [12]"） | `[33]` 保留 ✓ | `[33]` 保留 ✓ |
| 元数据（裸 PDF） | 有 footer，date/DOI 可提取 | **无页脚**，date 缺源 | **有 footnote**，date+DOI 提取成功 |
| 速度 | ~30s/9页 | ~40s/9页 | **~13s/9页** |
| 费用 | 免费 1000 页/日 | token 计费（9 页≈44k，便宜） | **免费 3000 页/日/模型**（[配额文档](https://ai.baidu.com/ai-doc/AISTUDIO/Xmjclapam)，超限 429；无充值入口是因当前免费） |

**定论：默认引擎切到 `paddleocr`**（`config.OCR_PROVIDER` 默认值已改）。
理由：图注绑定（本项目最痛的点）与 GLM 并列最优且远超 MinerU；元数据、
速度、配额全部占优；表格弱于 MinerU 是真损失，但在 SageRead 阅读场景中
图绑定失败的代价比表格 rowspan 瑕疵大。MinerU 保留为表格密集内容的备选
（`--provider mineru`），GLM 为第二备选。

**共同边界**（三引擎都翻车）：chen2019 这类带"近期发表推荐"的封面页会把
别的论文标题/作者混进元数据（date 还从 DOI 里误提了 "1916"）——这是
metadata 规则提取的弱点，与引擎无关，Zotero 流程不受影响。

## 五之四、针对性优化（2026-07-30 第二轮）

方针：契约（content_list）保持统一，契约内的归一化各引擎自行针对性优化；
只有纯文本级修复放适配器，需要 IR 上下文的才动共享下游。

1. **跨页续表合并：模型误差与逻辑不兼容各占一半**（teoh Table 1 实证）。
   已修（逻辑侧）：(a) `stage1_layout` 把 table_title 块绑到相邻表格块的
   `table_caption`（与图注同机制）；(b) 下游 `_has_text_between` 容忍与表头
   单元格逐字相同的短碎片文本块。效果：11 个碎表块 → 5 张表（GLM 7→5）。
   未修（模型侧）：个别续页表头识别错（列数 6→5、"Metal products"、西里尔
   字母混入），列数不兼容时合并器保守拒绝——把错列数据塞进 6 列表比分开
   放更糟，维持保守。
2. **Paddle Unicode 上下标 → LaTeX**（`stage1_paddleocr._unicode_scripts_to_latex`）：
   正文 LiCoO₂/Li⁺/Na⁺/H₂O → `$_{2}$`/`$^{+}$` 等（数学区外才转换；下标小数
   `$_{0}$.$_{25}$` 自动合并为 `$_{0.25}$`；标前空格贴回主体）。~~实测残留 0~~
   **更正（五之六）**：该结论只覆盖 `$_{0}$.$_{25}$` 形态；全量产出里另存在
   上游数学区切断形态 `$Li_{0$.75}` / `$Li_{1$/3}$`（1001 处），由五之六修复。
3. **metadata**：(a) 规则 fallback 加年份合理性窗口（1950..今年+1），
   chen2019 从 DOI 误提 "1916"（学会创立年）的问题已修；(b) 实测确认
   **默认 LLM 路径能扛住封面页污染**——chen2019 开 LLM 后真实 6 位作者、
   date=2019、container-title 全部正确（此前三方对比的脏元数据是 --no-llm
   对比产物）。规则路径的封面页污染（把别家论文标题当作者）仍是已知边界。
4. **staging 目录碰撞修复**：不同论文都叫 source.pdf，`_staging/{stem}` 会互相
   覆盖；现改为 `_staging/{stem}-{md5前6位}`。

## 五之五、PaddleOCR 全量重跑与精读（2026-07-31）

**批量结果**：148 篇中 125 篇成功（`output_paddle/`）；23 篇失败全部是
zotero-brain 缓存缺口（22 篇无 PDF、1 篇 yan2020nanozymology 563 页被整书
守卫拒收），有 PDF 的论文 **100% 解析成功**，未触发 3000 页/日配额熔断。
`--all --reparse --provider paddleocr` 支持 Zotero 权威元数据与 citekey slug。

**qc 对比**（12 异常/10 篇）：与 MinerU 版逐条比对后，真正的新增问题只有
表组对账 3 篇（见下）；container-title/date 缺失是 Zotero 数据缺口（两版相同）；
H1 过多（more2024progress 15、wang2024routes 13、mhaske2023minireview 13）
是 **qc 阈值误报**——长综述真有 11/8 个顶级章节，MinerU 版同样被旗。
注意 qc 表组对账的"源"是 MinerU 缓存，跨引擎对账天然虚增异常。

**精读发现与处置**：

| 发现 | 性质 | 处置 |
|---|---|---|
| he2025self 5 张"丢失"表格是嵌在图版里的 EDS 成分微表，Paddle 判为图片 | 模型取舍（tradeoff） | 不修：留在图里合理；qc 对账改看 MinerU 源才报警 |
| lamb2020 两张 morphology 小表同样 image 化 | 同上 | 不修 |
| liu2021review 缩略语表被拍平成一行流水文本 | 模型侧失败 | 不修（无结构可恢复），已知局限 |
| xiang2015 表格区炸成 15 个 `$$` 碎片（单元格变独立公式） | 模型侧失败 | 碎片公式本身已修干净；区域结构不可恢复 |
| `\mathrm{N a_{1.0}}` 空格字母不合并：`\b` 边界在下划线前失效 | **管线 bug** | 已修（否定前瞻 `(?![A-Za-z0-9])`） |
| Paddle 部分段落裸伪公式（`Na_xCoO_2`、`g^-1`、`10^-10`）KaTeX 不渲染 | 引擎特性 | 已修：保守包裹 `$...$`（URL/DOI 防护、ref_text/equation 豁免、下标小数合并、标前空格贴回） |
| 下标字符表漏 `ᵧ`（NaV_xOᵧ） | 管线 bug | 已修 |
| 零星转录错误（"chagre"、`\mathrm{$Na}` 包装怪癖） | 模型本质 | 不修，已知局限 |

**$$ 污染事故（教训记录）**：文本迁移时 `$$` 被 `$...$` 切分正则当成"空数学
区段"，显示公式失去保护，伪公式包裹污染了 35 篇的 `\mathrm` 内容；信息
mangled 不可逆，重转修复。教训：(1) 数学区切分必须 `$$` 优先；(2) 批量迁移
前先单篇金丝雀验证 + 备份 staging；(3) 文本规范化变换每次改动跑全量单测。

**迁移技巧**：适配器文本级修复无需重调 API——staging content_list 落盘后，
可对 staging 应用幂等变换再重跑 Stage 2/3（`convert_single`），零配额成本。

## 五之六、SageRead 入库质量门：公式断裂 / References 标题 / 引文格式（2026-08-03）

**背景**：SageRead 侧对全量产出（126 MinerU + 125 paddle）逐篇验收（pandoc
texmath 全扫 + 契约逐项 + 图片对账），骨架（目录/frontmatter/LF/图片 0 缺失/
页码锚点/legacy TeX 0 次）达标，但有三项契约不达。本批修复并零配额重跑复扫。

### 审查发现（修复前）

1. **paddle 下标小数/分数断裂**：`$Li_{0$.75}`、`$Li_{1$/3}$`——上游把十进制
   点/分数线切断在数学区外，`{` 未闭合，pandoc/texmath 报 unexpected eof
   （**1001 处**，KaTeX 同挂）。五之四"残留 0"结论只覆盖 `$_{0}$.$_{25}$`
   形态，属回归/未全量复扫（该结论已更正）。
2. **~25% 论文参考文献裸列无 `# References`**（MinerU 30/126、paddle 32/125），
   SageRead 按 heading 切片会把整段参考文献并入前一章节；另有 `# REFERENCES`
   全大写变体与"References"被当正文段落两形态。
3. **MinerU 引文上标化** `$^{[1-7]}$`（契约要求 `[1-7]`）；paddle 表格内亦有
   `$^{[n]}$`；形态含收尾空格（`$^{[42]} $`）、子标签（`$^{[13b]}$`）、
   GB/T 文献类型标记（`$^{[J]}$`）、无收尾 `$` 的裸上标（`}$^{[52]}`）。

### 修复（全部在 Stage 2/3，staging 零配额重跑生效）

- `content_processor._repair_script_frac`（进 `_normalize_inline` + 表体分支）：
  断裂修复全形态——`{N$.M}`/`{N$/M}`（含欧式逗号 `{0$,78}`、价态 `{3+$/4+}`、
  区间 `{0$.5-$x}`、负号 `{3$-δ}`、命令 `{3+$\delta}`、括号 `{(1-x$)$}` /
  `{(O-Na-O$)}`、撇号 `{2$'}`、闭合 `$g^{-1$}$`、斜杠 `_{2}/3}`、越位 `Na}^+`）；
  `\mathrm{$X}` 伪包裹污染解包。
- `stage1_paddleocr._PSEUDO_MATH_RE` lookbehind 增补 `{` 与 `\`：从源头封堵
  伪公式包裹污染 `\mathrm{...}` 命令体（新解析不再产生；存量由上一行修复兜底）。
- `content_processor._ensure_references_heading`（`_build_ir` 尾部）：
  无标题补 `# References`；"References" 正文段落升级为标题；`REFERENCES` 归一。
- `content_processor._normalize_citation_sup`（`_normalize_inline` + 表体分支）：
  `$^{[n]}$` 全形态 → `[n]`；作者单位脚注 `$^{[a]}$`（小写字母）不动。

### 复扫结果（125 篇 paddle 全量，pandoc texmath）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| unexpected eof（KaTeX 必挂级） | 1001 | **20**（残留 7 篇，见下） |
| 断裂 `{N$.M}` | 大面积 | 0 |
| 参考文献有条目无 heading | 32 | **0** |
| 引文上标化残留 | 134 | 4（均为作者单位 `$^{[a]}$`，有意保留） |

重跑方式：`_staging/{key}-{hash}` 逐目录 `convert_single`（零 OCR 配额；
LLM 元数据/标题分类与首轮同路径），三轮迭代修复后单篇补转。

### 残留与已知边界

- **20 处 unexpected eof（7 篇）**：lamb2020synthesis 12（表格单元格内
  `$$Cu^{{2+}$$^{{87}} $}` 数学/引文/括号三重缠绕）、li2016flame 2
  （`\underline{\text{Al}_{2}O_{3}$` underline 截断）、he2024review 2
  （水合点 `$_{0.9$\cdot$2.9$$H_{2}O$$}$`）、zhao2024interfacial /
  wang2018layered(×2 目录) / hwang2017-ibrpe633 各 1。形态高度定制、逐条
  正则边际收益低，挂账待下期；不影响其余 118 篇"公式 KaTeX 渲染无报错"达标。
- **LLM 元数据非确定性导致 slug 漂移**：重跑时个别论文年份判定在两次运行间
  翻转（wang2018layered ↔ wang2013layered、shinali ↔ paperfc8896、
  energy2020nali ↔ energy2020），产生过双份目录（已手工去重）。后续建议
  slug 以 zotero_key 锚定或缓存元数据判定。
- **Zotero 库重复条目**（8 组）双份产出仍在（he2016/hwang2017 等），
  去重属 Zotero 库治理，未在本批处理。
- **MinerU 输出（`output/` 126 篇）未重跑**：引擎基线定为 paddle
  （段落/图注/引文/错字优），MinerU 保留为表格密集论文备选
  （`--provider mineru`）；其 References heading/引文格式修复已进共享
  Stage 2/3 代码，下次重跑自动生效。

## 六、主要信息来源

- MinerU：[官方 API 文档](https://mineru.net/apiManage/docs)、[GitHub](https://github.com/opendatalab/MinerU)、碎图 issue [#4163](https://github.com/opendatalab/MinerU/issues/4163)
- 百度：[PaddleOCR-VL 文档解析 API 公告](https://ai.baidu.com/support/news?action=detail&id=3256)、[调用文档](https://ai.baidu.com/ai-doc/AISTUDIO/2mh4okm66)
- 智谱：[GLM-OCR API 文档](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-ocr)
- Mistral：[OCR 3 发布](https://jls42.org/en/news/mistral-decembre-2025)
- 榜单/评测：[OmniDocBench](https://github.com/opendatalab/OmniDocBench)、[dots.ocr 汇总表](https://github.com/rednote-hilab/dots.ocr)
- 框架：[Marker](https://github.com/datalab-to/marker)、[Docling](https://github.com/docling-project/docling)、[Chandra](https://github.com/datalab-to/chandra)
