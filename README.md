# Papers_Converter

论文 PDF → Pandoc Markdown 转换管线（SageRead 论文模块的上游 sidecar，与 books_converter 同级）。

输入：PDF（经 MinerU 云解析）或已解析产物（`content_list.json`）。
输出：`{slug}/paper.md + images/ + source.pdf`，格式契约见 SageRead `docs/paper-format-contract.md`。

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
#   --skip-mineru   复用 _staging 的解析产物，不重新提交 MinerU
#   -o DIR          输出目录（默认 ./output）
```

## 配置（.env）

| 键 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | LLM：补 abstract、标题结构分类 |
| `MINERU_TOKEN` / `MINERU_MODEL`(vlm) / `MINERU_TIMEOUT` / `MINERU_CHUNK_SIZE`(200) | MinerU 云解析 |
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
stage1_mineru.py     PDF → MinerU 云解析（分片/重试）→ content_list + images
metadata.py          元数据（规则/LLM/CrossRef；Zotero 优先经 zotero_meta.py）
content_processor.py 正文 IR：噪声清除、封面页检测、表格处理、标题层级重建、
                     图组编号、段落合并、跨页续表合并、游离图注绑回
renderer.py          Pandoc MD 渲染（frontmatter/正文/图片复制，UTF-8+LF）
slug.py              slug 生成（拉丁/拼音转写、citekey 优先）
qc_scan.py           验收门禁（见下）
```

## 验收门禁

```bash
.venv/Scripts/python qc_scan.py [output_dir]
```

机械检查：frontmatter 必填字段、表格组对账（跨页续表算一组，与 converter 同判据）、
图片重名、`<sup>/<sub>` 残留、CR 字符、非法 `$^{\*}$`、H1 结构 sanity。
当前 126 篇全绿（残留项见下）。

## 已知局限（诚实清单）

- **碎组图绑定**：MinerU 把多图版组图切成大量独立块，图版属于哪张图只有视觉能判定
  （hu2011 案例：Figure 6 图注已绑回，Figure 7 图注只能以编号段落保留）。
  这是与 MinerU-Popo 模型式图文关联的本质差距。
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
