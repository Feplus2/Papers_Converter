# Papers_Converter

论文 PDF → Pandoc Markdown 转换管线（SageRead 论文模块的上游 sidecar，与 books_converter 同级）。

输入：PDF（经可插拔解析引擎；仓库默认 PaddleOCR-VL（`config.py`），Better
SageRead 集成默认 MinerU-VLM 强制 OCR）或已解析产物（`content_list.json`）。
输出：`{slug}/paper.md + images/ + source.pdf`，格式契约见 SageRead `docs/paper-format-contract.md`。
早期引擎调研见 `docs/ocr-providers.md`（2026-08-03 止的历史档案，部分结论已被
2026-08-11 光栅重裁推翻，见该文文首标注）。

## 快速开始

需要 **Python ≥ 3.10**（代码使用 `str | None` 联合类型语法）。

```bash
# 环境（必须用 .venv：全局 Python 缺 pypinyin，中文 slug 会退化成哈希）
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # Windows
# .venv/bin/pip install -r requirements.txt            # Linux/macOS

# 单篇 PDF（PDF → 解析引擎 → MD）
.venv/Scripts/python pipeline.py D:\papers\some.pdf

# 批量（Zotero 解析缓存目录，由 ZOTERO_PARSED_DIR 配置）
.venv/Scripts/python pipeline.py --all

# 单篇（Zotero key 或已解析目录）
.venv/Scripts/python pipeline.py 26NNZJHX
.venv/Scripts/python pipeline.py F:\path\to\parsed\KEY

# 常用开关
#   --no-llm        纯规则（Zotero 元数据齐备时基本够用）
#   --no-ocr        文字版 PDF，不强制 OCR
#   --skip-mineru   复用 _staging 的解析产物，不重新提交解析
#   --provider X    指定 Stage 1 解析引擎（内置 mineru / paddleocr；glm 已下线，代码保留不推荐）
#   --model X       引擎后端 A/B（如 MinerU 的 vlm / pipeline）
#   -o DIR          输出目录（默认 ./output）
```

## 配置（.env）

| 键 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | LLM：补 abstract、标题结构分类、结构判定仲裁（可选） |
| `OCR_PROVIDER`(paddleocr) | Stage 1 解析引擎选择（契约见 `ocr_provider.py`；早期三方调研见 `docs/ocr-providers.md`） |
| `MINERU_TOKEN` / `MINERU_MODEL`(vlm) / `MINERU_TIMEOUT` / `MINERU_CHUNK_SIZE`(200) | MinerU 云解析（`MINERU_MODEL=pipeline` 换确定性 pipeline 后端——退化失控/内容缺失时的兜底角色，一般由管线自动切换，无需手动指定） |
| `GLM_OCR_API_KEY` / `GLM_OCR_BASE_URL` / `GLM_OCR_TIMEOUT` / `GLM_OCR_CHUNK_SIZE`(100) | GLM-OCR 解析——**已下线**（代码保留，不推荐；不在换引擎兜底链中） |
| `PADDLEOCR_TOKEN` / `PADDLEOCR_API_URL` / `PADDLEOCR_MODEL`(PaddleOCR-VL-1.6) / `PADDLEOCR_TIMEOUT` | PaddleOCR-VL 解析（百度 AI Studio 异步 job API，有每日页数配额） |
| `FIGURE_MERGE`(on) | 图组并集重裁开关（见"图组并集重裁"节） |
| `ARTICLE_BOUNDARY`(on) | 脏 PDF 文章边界切分（见"结构判定"节） |
| `STRUCTURE_LLM`(off) | 结构判定 LLM 仲裁通道，复用 DEEPSEEK 配置（见"结构判定"节） |
| `MAX_PAPER_PAGES`(200) | 整书守卫：超页数拒收并提示走图书馆导入 |
| `ZOTERO_PARSED_DIR`（旧键 `PARSED_DIR` 兼容） | 已解析产物目录（Zotero 解析缓存，批量 `--all` 的数据源；请指向自己的缓存目录） |
| `ZOTERO_API_KEY` / `ZOTERO_USER_ID` / `ZOTERO_LIBRARY_TYPE` | 仅导出 CSL 时用 |
| `ZOTERO_CSL_JSON` | CSL-JSON 权威元数据缓存（默认 `data/zotero_csl.json`） |

## 元数据优先级（重要设计）

1. **Zotero/CSL-JSON 权威**：`author` / `container-title` / `date` / `citekey` / `title` 及 CSL 变量以它为准（`zotero_meta.py` 读 `data/zotero_csl.json`）。
2. 规则提取填缺口；`container-title` 仍缺且有 DOI 时走 CrossRef 兜底。
3. **LLM 只补 abstract**（Zotero 无摘要时）；无 Zotero 来源（PDF 拖入）时 LLM 才做全量提取。
4. slug：citekey 优先，否则 `姓氏+年份+标题首词`；碰撞加 zotero_key/哈希后缀。

刷新 CSL 缓存（库有变动时）：

```bash
.venv/Scripts/python export_zotero_csl.py      # 默认读项目 .env；可用 --env-file 指定其他 env 文件
```

## 管线结构

```
ocr_provider.py      Stage 1 引擎抽象：产物契约 + 注册表（早期多引擎调研见 docs/ocr-providers.md）
stage1_layout.py     layout 系引擎共享转换：标签映射、图注挂回图片块、公式伪影规范化
stage1_mineru.py     MinerU Provider：云解析（分片/重试）→ content_list + images
stage1_paddleocr.py  PaddleOCR-VL Provider：百度 AI Studio 异步 job API（≤1000 页/任务）
stage1_glm.py        GLM-OCR Provider：已下线（代码保留，不推荐；不在兜底链中）
metadata.py          元数据（规则/LLM/CrossRef；Zotero 优先经 zotero_meta.py）
cover_detect.py      封面页判定统一实现（content_processor / metadata 共用）
article_boundary.py  脏 PDF 文章边界切分（默认开，见"结构判定"节）
structure_llm.py     结构判定 LLM 仲裁通道（默认关，见"结构判定"节）
content_processor.py 正文 IR：噪声清除、封面页检测、表格处理、标题层级重建、
                     图组编号、段落合并、跨页续表合并、游离图注绑回（含幻影
                     图注识别）、公式 \tag 伪影去重
figure_merger.py     图组并集重裁：碎图块 bbox 并集 → 源 PDF 整幅光栅化重裁（见专节）
renderer.py          Pandoc MD 渲染（frontmatter/正文/图片复制，UTF-8+LF）
slug.py              slug 生成（拉丁/拼音转写、citekey 优先）
quality_guard.py     解析退化检测（签名周期法，见下节）+ stage1 打回重解析
qc_paper.py          单篇产物 QC：WARN 级检查 + 严重级判据（交付前完整性闸，见专节）
qc_scan.py           验收门禁（见下）
progress_headless.py 无界面进度报告器（SageRead sidecar 的 JSON 行协议）
```

## 退化检测与打回重解析（quality_guard.py）

Stage 1 的 VLM 引擎在长枚举内容上偶发"模式延续"失控（真实事故：波长列从真实
1700 nm 被编造递增到 15800 nm；单词 fire 重复数百次），失控在引擎原始产物即存在。
检测用签名周期法（SageRead 侧 `utils/degenerate.ts` 的 Python 移植，阈值经实测调定）：

- 按行扫描，行长 < 200 不查；文本映射为粗签名（Unicode 字母→`a`、数字→`0`、
  空白折叠）后找短周期（4..50）连续重复 ≥10 次且覆盖 ≥300 字符——数字递增
  （`0000 aa, ` 周期）与精确重复在签名层同构，都能抓住；宽表分隔行
  （`|---|---|`，跨度有限）与正常论文不误中。
- **Stage 1 产物落地即检**：命中则重跑解析，最多 2 次（VLM 失控是随机的，
  原样重跑常能自愈；provider 的 `parse` 若显式声明 `temperature`/`seed` 形参，
  重试会自动升温/换种子——当前内置三引擎均无此形参，原样重跑）。
  headless 模式每次重试发 progress 事件（stage 1，
  detail=`检测到异常重复内容，正在重试 OCR（第 k 次）`），percent 不前进。
- **渲染前终检**：重试耗尽仍命中时不阻断输出，done 事件加 `"degenerate": true`
  （无命中则不加该字段），SageRead 侧据以提示换引擎重新解析。
  非 headless 模式两处命中均只打 WARNING 日志，不改变既有行为。
- **退化自动降级（2026-08-11）**：重试耗尽仍命中时，不再接受产物硬扛——
  自动换 MinerU pipeline 后端兜底重解析（确定性检测识别流水线，无生成式
  循环幻觉；公式/表格由识别模型处理，图片由 figure_merger 保整）。mineru
  引擎直接切 `model=pipeline`；其他引擎在已配置 MinerU Token 时换 mineru
  provider，未配置则维持原接受+打标行为。降级后产物按 mineru 语义走下游。

## 交付前完整性闸（qc_paper.py）

退化检测抓"重复失控"，抓不到"内容丢失"（真实事故：重解析 5 页 Science 论文
丢了 Figure 2/3 与若干段落，页锚标记只剩 3/5）。渲染产物定案前经
`qc_severe_findings` 检查两类严重问题——图/表编号断号（正文引用与实际
图块/表注对账）、整页丢失（页锚标记数 ≤ PDF 实际页数 × 0.6）——命中则打回：

1. **同引擎重试**（至多 `MAX_STAGE1_RETRIES` 次）；
2. **换引擎兜底链**（`_fallback_candidates`）：优先另一家 VLM
   （paddleocr ↔ MinerU-VLM），MinerU pipeline 后端殿后；只列有 Token
   可用的引擎（GLM 已下线不在链中）；
3. **最佳产物保留**：每次不完整尝试按（严重问题数, 图/表数, 页标记数）打分
   并快照，定案交付各次尝试中的最佳者（最后一试未必最好）；
4. 链尽仍不完整：照常交付，done 事件打标 `"incomplete": true` 并附
   `"qc_warnings"`（与 `"degenerate": true` 同通道），SageRead 侧据以提示。

WARN 级检查（`qc_paper_md`，只打日志不阻断）：References 出现在正文节之前、
References 区超长单段（软换行堆叠）等结构异常信号。

## 图组并集重裁（figure_merger.py）

MinerU 布局检测会把一张 Figure 拆成多个块（子图 a/b/c 各一块，合并阈值官方
硬编码无开关；PaddleOCR 也会把 panel 进一步拆成碎片）。本模块把**同页连续
图片块**就近归组（中间只夹 ≤20 字符的面板字母/碎片不打断，图注/正文段落等
大文字块才断开；带真图注的块是组界——2026-08-13 起不再依赖图编号词干，
编号本身可能错），组内 bbox 并集后从源 PDF 整幅光栅化重裁为一张（区域
光栅化≠拼接碎图，无损无接缝、矢量图天然覆盖）。版式守卫：跨页/纵向跨度
>75% 页高/面积>90% 页的组保守不动；合并失败一律保持原产物。
坐标空间（`convert_pdf` 按实际解析引擎传 `coord_space`）：

- **mineru**：content_list bbox 为 0-1000 归一化（退化降级后产物同此语义）
- **paddleocr**：block_bbox 为 API 页渲染像素，实测 2 px/pt（144 DPI，
  值/2 即 pt；经 stage1_layout 透传入 content_list）
- 其他 provider（含 GLM，已下线）：未核实，整体跳过合并保持原产物

开关：`FIGURE_MERGE`（默认开）。

## 结构判定：封面 / 文章边界 / LLM 仲裁

事故复盘与 spec 见 `docs/structure-detection.md`（2026-08-12 整页丢失事故：
旧封面判定把期刊页眉高频词凑对误判，正文第二页被整页静默切除——换任何
OCR 引擎都复现，根因在引擎产物之后的规则层）。现行三个模块层层收紧，
宁可漏切噪声也绝不误杀正文：

- **cover_detect.py**：封面判定根修——只判 page 0；仓库模板关键词命中 ≥3
  才考虑；正文信号（image/equation/table 块、长散文、section 编号标题）
  一票否决；权威标题/DOI 元数据锚定且标记偏弱时否决。
- **article_boundary.py**（`ARTICLE_BOUNDARY` 默认开）：脏 PDF（"杂志截页"
  类——标题前挂上一篇的结尾与参考文献，本文 References 后跟下一篇的开头）
  的文章边界切分。只吃锚点强信号：头切需权威标题锚点且锚点前有足量他文；
  尾切保护 References 后的固定段标题（Acknowledgments/Appendix 等仍是合法
  结构）。每次切除都输出 INFO 日志，绝不无声丢内容。
- **structure_llm.py**（`STRUCTURE_LLM` 默认关）：规则到达极限后的 LLM
  仲裁通道（复用 DEEPSEEK 配置，纯文本、低 max_tokens）。保守裁决——
  LLM 判"非封面"可撤销规则的封面判定，判"是封面"需规则同判或高置信才
  生效，任何失败/解析异常原样保留规则结果。默认关闭：规则判据已经全量
  语料 AB 验证零误杀，到达极限后再开启。

## 验收门禁

```bash
.venv/Scripts/python qc_scan.py [output_dir]
```

机械检查：frontmatter 必填字段、表格组对账（跨页续表算一组，与 converter 同判据）、
图片重名、`<sup>/<sub>` 残留、CR 字符、非法 `$^{\*}$`、H1 结构 sanity。
注意表组对账的"源"是解析缓存目录（`ZOTERO_PARSED_DIR`，不存在时自动跳过
该项），跨引擎对比会虚增异常。

## 测试

```bash
.venv/Scripts/python -m unittest test_quality_guard test_cover_detect test_article_boundary test_equation_tags
```

- `test_quality_guard`：退化检测 + 完整性闸（真实事故样本/正常样本/合成样例
  + 桩 provider 全链路重试协议，离线可跑；真实样本经 `SAGEREAD_BOOKS_DIR`
  指定，未设置时自动 skip）
- `test_cover_detect`：封面判定回归（合成样例自包含，任何环境可跑；真实
  事故样本经 `ZOTERO_PARSED_DIR` 指定，未设置时自动 skip）
- `test_article_boundary`：脏 PDF 边界切分（stub IR 块，无外部依赖）
- `test_equation_tags`：公式 `\tag` 去重（含编号拆行伪影的真实事故形态）

## 已知局限（诚实清单）

- **碎图问题已解决**：MinerU/PaddleOCR 把一张大图切成碎块是识别阶段行为，
  现由图组并集重裁机制修复（见"图组并集重裁"节，大图完整、图注对应）。
  残留边界：命中版式守卫（跨页/超版面占比）的图组保守不合并，按原碎块交付。
- **作者 bio 照**：RSC 版式 bio 头像会被并入 Figure 1 子图（fig1a/b/c/d）。
- **MinerU 版面缺陷**：双栏页正文可能被并进表格 HTML（已按判据拆出，但保守判据不保证全覆盖）；
  公式里的 legacy TeX 命令（`\bf/\cal/\sf/\tt/\textcircled`）pandoc/KaTeX 有警告（待扫荡）。
- **Zotero 数据缺口**：个别条目缺 container-title/date/abstract（管线只能兜底到 CrossRef，
  补条目才有解）；整书条目（>200 页拒收；≤200 页的书章会收但元数据是书级）。

## 提交与维护约定

- 产物目录 `output/`、缓存 `data/`、`.venv/`、`.env` 均在 .gitignore，不入库。
- 批量重转一律用 `.venv/Scripts/python`（pypinyin 依赖）。
- 改动后跑 `qc_scan.py` + 抽查 2-3 篇通读（grep 扫不出的问题只有读能发现——五轮的教训）。

## 致谢

- [MinerU](https://github.com/opendatalab/MinerU)——上海人工智能实验室
  OpenDataLab 开源的文档解析工具，本项目的 OCR 解析基础（云 API 的
  vlm / pipeline 双后端）。
- [PaddleOCR-VL](https://github.com/PaddlePaddle/PaddleOCR)——百度开源的
  多模态文档解析模型（经百度 AI Studio 云 API 调用）。
- [PyMuPDF](https://github.com/pymupdf/PyMuPDF)——PDF 渲染与页面操作，
  图组并集重裁的光栅化基础。
- [pypinyin](https://github.com/mozillazg/python-pinyin)——中文标题 slug
  的拼音转写。
- [DeepSeek](https://www.deepseek.com/)——可选 LLM 后处理（补 abstract、
  结构判定仲裁）。

## 许可证

MIT，见 [LICENSE](LICENSE)。
