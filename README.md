# Papers_Converter

论文 PDF → Pandoc Markdown 转换管线（SageRead 论文模块的上游 sidecar，与 books_converter 同级）。

输入：PDF（经可插拔解析引擎，默认 PaddleOCR-VL；三方对比见 `docs/ocr-providers.md`）或已解析产物（`content_list.json`）。
输出：`{slug}/paper.md + images/ + source.pdf`，格式契约见 SageRead `docs/paper-format-contract.md`。
多引擎调研与路线图见 `docs/ocr-providers.md`。

## 快速开始

```bash
# 环境（必须用 .venv：全局 Python 缺 pypinyin，中文 slug 会退化成哈希）
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # Windows
# .venv/bin/pip install -r requirements.txt            # Linux/macOS

# 单篇 PDF（PDF → MinerU 解析 → MD）
.venv/Scripts/python pipeline.py D:\papers\some.pdf

# 批量（Zotero 解析缓存目录，当前示例数据源 148 篇）
.venv/Scripts/python pipeline.py --all

# 单篇（Zotero key 或已解析目录）
.venv/Scripts/python pipeline.py 26NNZJHX
.venv/Scripts/python pipeline.py F:\path\to\parsed\KEY

# 常用开关
#   --no-llm        纯规则（Zotero 元数据齐备时基本够用）
#   --no-ocr        文字版 PDF，不强制 OCR
#   --skip-mineru   复用 _staging 的解析产物，不重新提交解析
#   --provider X    指定 Stage 1 解析引擎（当前内置: mineru, glm, paddleocr）
#   --model X       引擎后端 A/B（如 MinerU 的 vlm / pipeline）
#   -o DIR          输出目录（默认 ./output）
```

## 配置（.env）

| 键 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | LLM：补 abstract、标题结构分类 |
| `OCR_PROVIDER`(paddleocr) | Stage 1 解析引擎选择（契约见 `ocr_provider.py`；三方对比与针对性优化见 `docs/ocr-providers.md` 五之三/五之四） |
| `MINERU_TOKEN` / `MINERU_MODEL`(vlm) / `MINERU_TIMEOUT` / `MINERU_CHUNK_SIZE`(200) | MinerU 云解析（`MINERU_MODEL` 可换 `pipeline` 后端对照碎图问题） |
| `GLM_OCR_API_KEY` / `GLM_OCR_BASE_URL` / `GLM_OCR_TIMEOUT` / `GLM_OCR_CHUNK_SIZE`(100) | GLM-OCR 解析（智谱 layout_parsing API） |
| `PADDLEOCR_TOKEN` / `PADDLEOCR_API_URL` / `PADDLEOCR_MODEL`(PaddleOCR-VL-1.6) / `PADDLEOCR_TIMEOUT` | PaddleOCR-VL 解析（百度 AI Studio 异步 job API，有每日页数配额） |
| `MAX_PAPER_PAGES`(200) | 整书守卫：超页数拒收并提示走图书馆导入 |
| `PARSED_DIR` | 已解析产物目录（默认 `F:\MyProjects\zotero-brain\parsed`） |
| `ZOTERO_API_KEY` / `ZOTERO_USER_ID` / `ZOTERO_LIBRARY_TYPE` | 仅导出 CSL 时用 |
| `ZOTERO_CSL_JSON` | CSL-JSON 权威元数据缓存（默认 `data/zotero_csl.json`） |

## 元数据优先级（重要设计）

1. **Zotero/CSL-JSON 权威**：`author` / `container-title` / `date` / `citekey` / `title` 及 CSL 变量以它为准（`zotero_meta.py` 读 `data/zotero_csl.json`）。
2. 规则提取填缺口；`container-title` 仍缺且有 DOI 时走 CrossRef 兜底。
3. **LLM 只补 abstract**（Zotero 无摘要时）；无 Zotero 来源（PDF 拖入）时 LLM 才做全量提取。
4. slug：citekey 优先，否则 `姓氏+年份+标题首词`；碰撞加 zotero_key/哈希后缀。

刷新 CSL 缓存（库有变动时）：

```bash
.venv/Scripts/python export_zotero_csl.py --env-file F:\MyProjects\zotero-brain\.env
```

## 管线结构

```
ocr_provider.py      Stage 1 引擎抽象：产物契约 + 注册表（多引擎调研见 docs/ocr-providers.md）
stage1_layout.py     layout 系引擎共享转换：标签映射、图注挂回图片块、公式伪影规范化
stage1_mineru.py     MinerU Provider：云解析（分片/重试）→ content_list + images
stage1_glm.py        GLM-OCR Provider：智谱 layout_parsing（同步 API，≤100 页/次分片）
stage1_paddleocr.py  PaddleOCR-VL Provider：百度 AI Studio 异步 job API（≤1000 页/任务）
metadata.py          元数据（规则/LLM/CrossRef；Zotero 优先经 zotero_meta.py）
content_processor.py 正文 IR：噪声清除、封面页检测、表格处理、标题层级重建、
                     图组编号、段落合并、跨页续表合并、游离图注绑回
renderer.py          Pandoc MD 渲染（frontmatter/正文/图片复制，UTF-8+LF）
slug.py              slug 生成（拉丁/拼音转写、citekey 优先）
quality_guard.py     解析退化检测（签名周期法，见下节）+ stage1 打回重解析
qc_scan.py           验收门禁（见下）
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

## 图组并集重裁（figure_merger.py）

MinerU 布局检测会把一张 Figure 拆成多个块（子图 a/b/c 各一块，合并阈值官方
硬编码无开关）。`_assign_figure_numbers` 已把碎块归组为同一 fig{N} 词干，
本模块在其后把**同词干、同页、≥2 块的组**的 bbox 并集，从源 PDF 整幅
光栅化重裁为一张（区域光栅化≠拼接碎图，无损无接缝、矢量图天然覆盖）。
版式守卫：跨页/纵向跨度>75% 页高/面积>90% 页的组保守不动。坐标语义目前仅
支持 MinerU 的 0-1000 归一化（`convert_pdf` 按实际解析引擎传
`coord_normalized`；PaddleOCR/GLM 待补 block_bbox passthrough 后启用）。
开关：`FIGURE_MERGE`（默认开）。

测试：`python -m unittest test_quality_guard`（真实事故样本/正常样本/合成样例
+ 桩 provider 全链路重试协议，离线可跑）。

## 验收门禁

```bash
.venv/Scripts/python qc_scan.py [output_dir]
```

机械检查：frontmatter 必填字段、表格组对账（跨页续表算一组，与 converter 同判据）、
图片重名、`<sup>/<sub>` 残留、CR 字符、非法 `$^{\*}$`、H1 结构 sanity。
PaddleOCR 全量库（`output_paddle/`，125 篇）12 异常/10 篇，逐条已定性
（Zotero 数据缺口、qc 阈值误报、表图 tradeoff），详见 `docs/ocr-providers.md` 五之五。
注意表组对账的"源"是 MinerU 缓存，跨引擎对比会虚增异常。

## 已知局限（诚实清单）

- **碎组图绑定**：MinerU vlm 后端把多图版组图切成大量独立块且图注常绑不回
  （cao2022 只绑 5/9）——这是**识别阶段行为，后处理无法无损还原**。
  已换默认引擎为 PaddleOCR-VL（图注 9/9 绑定，GLM 同优）；MinerU 仅在表格
  密集内容时有优势（rowspan/跨页合并最稳）。子图切分三引擎都有，只能靠
  归组编号缓解。
- **作者 bio 照**：RSC 版式 bio 头像会被并入 Figure 1 子图（fig1a/b/c/d）。
- **MinerU 版面缺陷**：双栏页正文可能被并进表格 HTML（已按判据拆出，但保守判据不保证全覆盖）；
  公式里的 legacy TeX 命令（`\bf/\cal/\sf/\tt/\textcircled`）pandoc/KaTeX 有警告（待扫荡）。
- **Zotero 数据缺口**：个别条目缺 container-title/date/abstract（管线只能兜底到 CrossRef，
  补条目才有解）；整书条目（>200 页拒收；≤200 页的书章会收但元数据是书级）。
- **MinerU 解析失败目录**：约 15% 的 parsed 缓存无 content_list.json（上游 zotero-brain 的缺口，非本管线问题）。

## 提交与维护约定

- 产物目录 `output/`、缓存 `data/`、`.venv/`、`.env` 均在 .gitignore，不入库。
- 批量重转一律用 `.venv/Scripts/python`（pypinyin 依赖）。
- 改动后跑 `qc_scan.py` + 抽查 2-3 篇通读（grep 扫不出的问题只有读能发现——五轮的教训）。
