"""Stage 1 GLM-OCR Provider — 智谱 layout_parsing API → 统一解析产物契约。

接口：POST {GLM_OCR_BASE_URL}/api/paas/v4/layout_parsing（同步返回）。
单次请求上限 100 页 / 50MB，超出按 GLM_OCR_CHUNK_SIZE 分片。

特点（2026-07-30 探针确认）：
- 图注是独立的 figure_title 文本块（按阅读顺序穿插），由 stage1_layout 挂回图片块；
- 复合图会拆 panel，panel 字母标（"A"/"(b)"）也是 figure_title 块，转换时丢弃；
- 裁剪图以带签名 URL 返回（约 24h 过期），解析时立即下载到本地 images/；
- 不返回页脚 → 裸 PDF 时 date/container-title 缺来源（Zotero 流程无影响）；
- 公式有 padding/spaced-letter 伪影，stage1_layout.normalize_math 已规范化。

块转换的共享逻辑在 stage1_layout.py（与 PaddleOCR 适配器共用）。
"""

import base64
import json
import logging
import time
from pathlib import Path

import requests

import config
from ocr_provider import count_pages
from stage1_layout import convert_layout_blocks

logger = logging.getLogger(__name__)

_API_PATH = "/api/paas/v4/layout_parsing"


class GlmOcrProvider:
    """智谱 GLM-OCR（layout_parsing API）→ content_list.json + images/ + md。"""

    name = "glm"

    def parse(self, pdf_path: str, work_dir: str, ocr: bool = True,
              progress=None, model: str | None = None) -> dict:
        """调用 GLM-OCR API 解析 PDF，超 100 页自动分片。

        Args 与返回同 OcrProvider 契约；model/ocr 参数该引擎不支持，忽略。
        """
        if not config.GLM_OCR_API_KEY:
            raise RuntimeError("未配置 GLM_OCR_API_KEY，无法提交 GLM-OCR 解析")

        pdf_path = Path(pdf_path)
        stem = pdf_path.stem
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        images_out = work_dir / "images"
        images_out.mkdir(parents=True, exist_ok=True)

        total_pages = count_pages(str(pdf_path))
        chunk_size = config.GLM_OCR_CHUNK_SIZE
        chunks_needed = (total_pages + chunk_size - 1) // chunk_size

        logger.info(f"Stage 1: GLM-OCR 解析 '{pdf_path.name}'")
        logger.info(f"  {total_pages} 页, 分 {chunks_needed} 片 (每片 ≤{chunk_size} 页)")

        file_payload = ("data:application/pdf;base64,"
                        + base64.b64encode(pdf_path.read_bytes()).decode("ascii"))
        url = config.GLM_OCR_BASE_URL.rstrip("/") + _API_PATH
        headers = {
            "Authorization": f"Bearer {config.GLM_OCR_API_KEY}",
            "Content-Type": "application/json",
        }

        all_markdown = []
        all_blocks = []
        _report = progress or (lambda *a, **kw: None)

        for chunk_idx in range(chunks_needed):
            start_page = chunk_idx * chunk_size + 1
            end_page = min(start_page + chunk_size - 1, total_pages)

            _report(f"片 {chunk_idx + 1}/{chunks_needed}: 第 {start_page}-{end_page} 页 上传...",
                    chunk_idx / chunks_needed)
            logger.info(f"  片 {chunk_idx + 1}/{chunks_needed}: 第 {start_page}-{end_page} 页 ...")
            t0 = time.time()

            data = None
            last_error = None
            for attempt in range(3):
                try:
                    if attempt > 0:
                        _report(f"片 {chunk_idx + 1}/{chunks_needed}: 重试 {attempt + 1}/3...")
                    resp = requests.post(
                        url, headers=headers, timeout=config.GLM_OCR_TIMEOUT,
                        json={
                            "model": "glm-ocr",
                            "file": file_payload,
                            "start_page_id": start_page,
                            "end_page_id": end_page,
                            "return_crop_images": True,
                        },
                    )
                    if resp.status_code != 200:
                        raise RuntimeError(
                            f"HTTP {resp.status_code}: {resp.text[:300]}")
                    data = resp.json()
                    if "error" in data:
                        raise RuntimeError(f"GLM-OCR 错误: {data['error']}")
                    break
                except Exception as e:
                    last_error = e
                    if attempt < 2:
                        wait = (attempt + 1) * 10
                        logger.warning(f"    尝试 {attempt + 1} 失败，{wait}s 后重试: {e}")
                        time.sleep(wait)
            if data is None:
                logger.error(f"    片 {chunk_idx + 1} 重试 3 次后仍失败: {last_error}")
                raise last_error

            elapsed = time.time() - t0
            page_offset = start_page - 1
            n_images = 0
            for page_rel, page_blocks in enumerate(data.get("layout_details") or []):
                page_idx = page_offset + page_rel
                raws = [_normalize(b, page_idx) for b in page_blocks]
                for block in convert_layout_blocks(raws, page_idx, images_out,
                                                   get_image_url=_image_url):
                    if block["type"] == "image":
                        n_images += 1
                    all_blocks.append(block)

            all_markdown.append(data.get("md_results") or "")
            usage = data.get("usage") or {}
            _report(f"片 {chunk_idx + 1}/{chunks_needed}: 完成 — "
                    f"{len(all_blocks)} 块累计, tokens={usage.get('total_tokens', '?')}",
                    (chunk_idx + 1) / chunks_needed)
            logger.info(f"    完成: {len(all_blocks)} 块累计, {n_images} 张图片, "
                        f"耗时 {elapsed:.0f}s")

        merged_md = "\n\n".join(all_markdown)

        md_path = work_dir / f"{stem}.md"
        md_path.write_text(merged_md, encoding="utf-8")
        cl_path = work_dir / f"{stem}_content_list.json"
        cl_path.write_text(json.dumps(all_blocks, ensure_ascii=False), encoding="utf-8")

        logger.info(f"  合并完成: {len(merged_md):,} 字符, {len(all_blocks)} 个内容块")

        return {
            "markdown": merged_md,
            "content_list": all_blocks,
            "images_dir": str(images_out),
        }


def _normalize(raw: dict, page_idx: int) -> dict:
    """GLM layout_details 块 → 共享转换的归一化形态。"""
    label = raw.get("native_label") or raw.get("label") or ""
    return {
        "label": label,
        "content": raw.get("content") or "",
        "index": raw.get("index", 0),
        "_img_name": f"img_p{page_idx:03d}_{raw.get('index', 0):02d}.png",
    }


def _image_url(raw: dict) -> str | None:
    content = raw.get("content") or ""
    return content if content.startswith(("http://", "https://")) else None
