"""Stage 1 PaddleOCR-VL Provider — 百度 AI Studio 异步 job API → 统一解析产物契约。

接口（文档 https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5 ）：
- POST {PADDLEOCR_API_URL}（multipart 上传，≤50MB）→ jobId
- GET {PADDLEOCR_API_URL}/{jobId} 轮询 → done 后取 resultUrl.jsonUrl
- JSONL 逐页：result.layoutParsingResults[].markdown.{text,images{路径:URL}}
- 单任务 ≤1000 页（远超 MAX_PAPER_PAGES），无需分片

与 MinerU/GLM 的关键差异：
- 返回逐页 markdown + **prunedResult.parsing_res_list**（阅读顺序的块结构，
  标签体系与 GLM 相同，转换逻辑共用 stage1_layout）；
- 图片块 content 为空，裁剪图 URL 在 markdown.images，键名内嵌 bbox；
- 会抓到 footnote（投稿/接收日期等）→ 裸 PDF 的 date 元数据有来源（GLM 无）；
- 异步 job，轮询间隔 5s，单任务 ≤1000 页无需分片。
"""

import json
import logging
import re
import time
from pathlib import Path

import requests

import config
from stage1_layout import convert_layout_blocks

logger = logging.getLogger(__name__)


class PaddleOcrProvider:
    """百度 PaddleOCR-VL（异步 job API）→ content_list.json + images/ + md。"""

    name = "paddleocr"

    def parse(self, pdf_path: str, work_dir: str, ocr: bool = True,
              progress=None, model: str | None = None) -> dict:
        if not config.PADDLEOCR_TOKEN:
            raise RuntimeError("未配置 PADDLEOCR_TOKEN，无法提交 PaddleOCR 解析")

        pdf_path = Path(pdf_path)
        stem = pdf_path.stem
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        images_out = work_dir / "images"
        images_out.mkdir(parents=True, exist_ok=True)

        headers = {"Authorization": f"bearer {config.PADDLEOCR_TOKEN}"}
        model = model or config.PADDLEOCR_MODEL
        _report = progress or (lambda *a, **kw: None)

        logger.info(f"Stage 1: PaddleOCR({model}) 解析 '{pdf_path.name}'")

        # 提交任务
        _report("上传并提交解析任务...", 0.0)
        data = {
            "model": model,
            "optionalPayload": json.dumps({
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useChartRecognition": False,
            }),
        }
        with open(pdf_path, "rb") as f:
            resp = requests.post(config.PADDLEOCR_API_URL, headers=headers,
                                 data=data, files={"file": f}, timeout=180)
        if resp.status_code != 200:
            raise RuntimeError(f"提交失败 HTTP {resp.status_code}: {resp.text[:300]}")
        job_id = resp.json()["data"]["jobId"]
        logger.info(f"  jobId: {job_id}")

        # 轮询
        t0 = time.time()
        json_url = None
        while time.time() - t0 < config.PADDLEOCR_TIMEOUT:
            jr = requests.get(f"{config.PADDLEOCR_API_URL}/{job_id}",
                              headers=headers, timeout=60)
            jr.raise_for_status()
            d = jr.json()["data"]
            state = d["state"]
            if state == "done":
                prog = d.get("extractProgress") or {}
                logger.info(f"  完成: {prog.get('extractedPages', '?')} 页, "
                            f"耗时 {time.time() - t0:.0f}s")
                json_url = d["resultUrl"]["jsonUrl"]
                break
            if state == "failed":
                raise RuntimeError(f"PaddleOCR 任务失败: {d.get('errorMsg')}")
            _report(f"解析中（{state}）...", None)
            time.sleep(5)
        if json_url is None:
            raise TimeoutError(f"PaddleOCR 任务超时（>{config.PADDLEOCR_TIMEOUT}s）")

        # 取结果并转换
        _report("下载解析结果...", 0.8)
        text = requests.get(json_url, timeout=180).text
        pages = []
        for line in text.strip().split("\n"):
            if line.strip():
                pages.extend(json.loads(line)["result"]["layoutParsingResults"])

        all_markdown = []
        all_blocks = []
        for page_idx, page in enumerate(pages):
            md = page["markdown"]["text"]
            all_markdown.append(md)
            for block in self._convert_page(page, page_idx, images_out):
                _unicode_scripts_to_latex(block)
                all_blocks.append(block)

        merged_md = "\n\n".join(all_markdown)
        (work_dir / f"{stem}.md").write_text(merged_md, encoding="utf-8")
        (work_dir / f"{stem}_content_list.json").write_text(
            json.dumps(all_blocks, ensure_ascii=False), encoding="utf-8")

        logger.info(f"  合并完成: {len(merged_md):,} 字符, {len(all_blocks)} 个内容块")
        return {
            "markdown": merged_md,
            "content_list": all_blocks,
            "images_dir": str(images_out),
        }

    def _convert_page(self, page: dict, page_idx: int,
                      images_out: Path) -> list[dict]:
        """一页的解析结果 → content_list 块。

        块结构用 prunedResult.parsing_res_list（阅读顺序，标签体系与 GLM 相同）；
        图片块 block_content 为空，裁剪图 URL 在 markdown.images 里，键名内嵌
        bbox（img_in_chart_box_x1_y1_x2_y2.jpg），按 bbox 精确匹配（实测 33/33）。
        """
        image_keys = page["markdown"].get("images") or {}

        def find_key(bbox) -> str | None:
            pat = f"box_{bbox[0]}_{bbox[1]}_{bbox[2]}_{bbox[3]}"
            for k in image_keys:
                if pat in k:
                    return k
            return None

        raws = []
        for b in page["prunedResult"]["parsing_res_list"]:
            label = b.get("block_label") or ""
            img_name = ""
            img_url = None
            if label in ("chart", "image", "table_image"):
                key = find_key(b.get("block_bbox") or [0, 0, 0, 0])
                if key:
                    img_name = f"p{page_idx:03d}_{Path(key).name}"
                    img_url = image_keys[key]
            raws.append({
                "label": label,
                "content": b.get("block_content") or "",
                "index": b.get("block_id", 0),
                "_img_name": img_name,
                "_url": img_url,
            })
        return convert_layout_blocks(raws, page_idx, images_out,
                                     get_image_url=self._image_url)

    @staticmethod
    def _image_url(raw: dict) -> str | None:
        return raw.get("_url")


# ------------------------------------------------------------------
# PaddleOCR 专属：Unicode 上下标 → LaTeX
#
# PaddleOCR-VL 在正文里用 Unicode 上下标（LiCoO₂、Li⁺/Na⁺、H₂O）而非
# 契约要求的 $...$（表格里它反而输出 LaTeX）。逐字 run 转换：
# "LiCoO₂" → "LiCoO$_{2}$"，"Li⁺/Na⁺" → "Li$^{+}$/Na$^{+}$"。
# 已有 $...$ 区段不动。
# ------------------------------------------------------------------
_SUB_MAP = dict(zip("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓᵧ",
                    "0123456789+-=()aehijklmnoprstuvxy"))
_SUP_MAP = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ",
                    "0123456789+-=()ni"))
_SUB_RUN_RE = re.compile("[" + "".join(_SUB_MAP) + "]+")
_SUP_RUN_RE = re.compile("[" + "".join(_SUP_MAP) + "]+")
_MATH_SPAN_SPLIT_RE = re.compile(r"(\$\$[^$]*\$\$|\$[^$]*\$)")
# Unicode 无下标点：下标小数被拆成 "$_{0}$.$_{25}$" → 合并为 "$_{0.25}$"
_SCRIPT_DOT_MERGE_RE = re.compile(r"\$([_^])\{([^{}]*)\}\$\.\$([_^])\{([^{}]*)\}\$")


def _convert_scripts(text: str) -> str:
    text = _SUB_RUN_RE.sub(
        lambda m: "$_{" + "".join(_SUB_MAP[c] for c in m.group(0)) + "}$", text)
    text = _SUP_RUN_RE.sub(
        lambda m: "$^{" + "".join(_SUP_MAP[c] for c in m.group(0)) + "}$", text)
    while True:
        merged = _SCRIPT_DOT_MERGE_RE.sub(
            lambda m: (f"${m.group(1)}{{{m.group(2)}.{m.group(4)}}}$"
                       if m.group(1) == m.group(3) else m.group(0)), text)
        if merged == text:
            return text
        text = merged


def _convert_scripts_outside_math(s: str, wrap_pseudo: bool = True) -> str:
    """按 $...$ 分段（捕获组保留定界段），只转换数学区外的部分。"""
    parts = _MATH_SPAN_SPLIT_RE.split(s)  # 偶数索引 = 数学区外
    for i in range(0, len(parts), 2):
        parts[i] = _convert_scripts(parts[i])
        if wrap_pseudo:
            parts[i] = _wrap_pseudo_math(parts[i])
    text = "".join(parts)
    # 源文本里上下标前的空格是 OCR 噪声（"LiCoO ₂"），转换后贴回主体
    return re.sub(r"(?<=[A-Za-z0-9]) (\$[_^]\{)", r"\1", text)


# Paddle 部分段落输出无 $ 包裹的伪公式（"Na_xCoO_2"、"g^-1"、"10^-10 cm^2"）。
# 论文正文里 _/^ 不会出现在正常英文单词中，含 _/^ 的紧凑 token 可保守包裹。
# 已包 $...$ 的区段不会进入此函数；前邻 / . = & ? $ 或单词字符时不包（护 URL/DOI）；
# 前邻 { 或 \ 时同样不包——那是 \mathrm{...} 等 LaTeX 命令体内，包裹会污染命令内容
# （"\mathrm{$Na}" 这类污染会让 pandoc 误切 span 吃掉 \mathrm{ 导致 unexpected eof）。
_PSEUDO_MATH_RE = re.compile(
    r"(?<![\w$./=&?{\\\\])([A-Za-z0-9)\]}]+(?:[_^][A-Za-z0-9{}(+-]+)+)(?![\w$])")


def _wrap_pseudo_math(s: str) -> str:
    return _PSEUDO_MATH_RE.sub(r"$\1$", s)


def _unicode_scripts_to_latex(block: dict) -> None:
    """就地转换块的文本字段（text/table_body/图注/表注）。
    ref_text（参考文献）不做伪公式包裹——护 URL/DOI；
    equation 块不做伪公式包裹——其文本本身即 LaTeX，包裹会污染 \\mathrm 内容。"""
    wrap = block.get("type") not in ("ref_text", "equation")
    for key in ("text", "table_body"):
        s = block.get(key)
        if s:
            block[key] = _convert_scripts_outside_math(s, wrap_pseudo=wrap)
    for key in ("image_caption", "table_caption"):
        caps = block.get(key)
        if caps:
            block[key] = [_convert_scripts_outside_math(c, wrap_pseudo=wrap)
                          for c in caps]

