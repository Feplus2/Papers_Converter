"""Stage 1 MinerU Provider — MinerU 云解析 PDF → 统一解析产物契约。

支持超大 PDF 自动分片，绕过免费 API 的页数限制。
产物目录结构与 zotero-brain/parsed 一致，可供后续 Stage 2/3 直接消费。

后端选择（model 选项，对应云端 model_version）：
- vlm（默认）：MinerU VLM 后端，精度高，但会把复合图拆成子图（识别阶段行为，
  官方无配置关闭，见 docs/ocr-providers.md）
- pipeline：DocLayout-YOLO 路线，按整块裁图不拆子图，可作为碎图问题的对照
"""

import json
import logging
import time
from pathlib import Path

import config
from ocr_provider import count_pages

logger = logging.getLogger(__name__)


class MinerUProvider:
    """MinerU 云 API（mineru-open-sdk）→ content_list.json + images/ + md。"""

    name = "mineru"

    def parse(self, pdf_path: str, work_dir: str, ocr: bool = True,
              progress=None, model: str | None = None) -> dict:
        """调用 MinerU API 解析 PDF，超大文件自动分片。

        Args:
            pdf_path: 待解析 PDF 路径
            work_dir: 解析产物输出目录（会写入 {stem}_content_list.json + images/）
            ocr: 是否强制 OCR（扫描版 True，文字版可 False）
            progress: 可选回调 progress(detail: str, fraction: float|None)
            model: 覆盖 config.MINERU_MODEL（vlm / pipeline），用于后端 A/B 对比

        Returns:
            {"content_list": [...], "images_dir": "...", "markdown": "..."}
        """
        if not config.MINERU_TOKEN:
            raise RuntimeError("未配置 MINERU_TOKEN，无法提交 MinerU 解析")

        from mineru import MinerU  # 延迟导入：仅真正解析时才需要 SDK

        model = model or config.MINERU_MODEL
        pdf_path = Path(pdf_path)
        stem = pdf_path.stem
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        total_pages = count_pages(str(pdf_path))
        chunk_size = config.MINERU_CHUNK_SIZE
        chunks_needed = (total_pages + chunk_size - 1) // chunk_size

        logger.info(f"Stage 1: MinerU({model}) 解析 '{pdf_path.name}'")
        logger.info(f"  {total_pages} 页, 分 {chunks_needed} 片 (每片 {chunk_size} 页), "
                    f"OCR={'强制' if ocr else '自动'}")

        all_markdown = []
        all_blocks = []
        page_offset = 0
        _report = progress or (lambda *a, **kw: None)

        # 构建可选参数（language 为空则不传，走自动检测）
        extra = {}
        if config.MINERU_LANGUAGE:
            extra["language"] = config.MINERU_LANGUAGE

        client = MinerU(config.MINERU_TOKEN)
        try:
            for chunk_idx in range(chunks_needed):
                start_page = chunk_idx * chunk_size + 1
                end_page = min(start_page + chunk_size - 1, total_pages)
                page_range = f"{start_page}-{end_page}"

                _report(f"片 {chunk_idx + 1}/{chunks_needed}: 第 {start_page}-{end_page} 页 上传...",
                        chunk_idx / chunks_needed)
                logger.info(f"  片 {chunk_idx + 1}/{chunks_needed}: 第 {start_page}-{end_page} 页 ...")
                t0 = time.time()

                # 重试逻辑：处理间歇性 SSL/CDN 错误
                result = None
                last_error = None
                for attempt in range(3):
                    try:
                        if attempt > 0:
                            _report(f"片 {chunk_idx + 1}/{chunks_needed}: 重试 {attempt + 1}/3...")
                        result = client.extract(
                            str(pdf_path),
                            model=model,
                            ocr=ocr,
                            formula=config.MINERU_ENABLE_FORMULA,
                            table=config.MINERU_ENABLE_TABLE,
                            pages=page_range,
                            timeout=config.MINERU_TIMEOUT,
                            **extra,
                        )
                        break
                    except Exception as e:
                        last_error = e
                        if attempt < 2:
                            wait = (attempt + 1) * 10
                            logger.warning(f"    尝试 {attempt + 1} 失败，{wait}s 后重试: {e}")
                            time.sleep(wait)
                if result is None:
                    logger.error(f"    片 {chunk_idx + 1} 重试 3 次后仍失败: {last_error}")
                    raise last_error

                elapsed = time.time() - t0
                if result.state != "done":
                    raise RuntimeError(f"片 {chunk_idx + 1} 失败: state={result.state}")

                md_chunk = result.markdown or ""
                blocks_chunk = result.content_list or []

                # 调整 page_idx：MinerU 的 page_idx 从 0 开始且相对当前 chunk
                for block in blocks_chunk:
                    if "page_idx" in block:
                        block["page_idx"] = block["page_idx"] + page_offset

                all_markdown.append(md_chunk)
                all_blocks.extend(blocks_chunk)
                page_offset += (end_page - start_page + 1)

                # 保存图片到磁盘（MinerU SDK 以 bytes 形式返回）
                if result.images:
                    images_out = work_dir / "images"
                    images_out.mkdir(parents=True, exist_ok=True)
                    for img in result.images:
                        with open(images_out / img.name, "wb") as f:
                            f.write(img.data)

                _report(f"片 {chunk_idx + 1}/{chunks_needed}: 完成 — "
                        f"{len(md_chunk):,} 字符, {len(result.images)} 张图片",
                        (chunk_idx + 1) / chunks_needed)
                logger.info(f"    完成: {len(md_chunk):,} 字符, {len(blocks_chunk)} blocks, "
                            f"{len(result.images)} 张图片, 耗时 {elapsed:.0f}s")

            merged_md = "\n\n".join(all_markdown)

            # 写入解析产物（与 zotero-brain/parsed 结构一致）
            md_path = work_dir / f"{stem}.md"
            md_path.write_text(merged_md, encoding="utf-8")
            cl_path = work_dir / f"{stem}_content_list.json"
            cl_path.write_text(json.dumps(all_blocks, ensure_ascii=False), encoding="utf-8")

            logger.info(f"  合并完成: {len(merged_md):,} 字符, {len(all_blocks)} 个内容块")

            return {
                "markdown": merged_md,
                "content_list": all_blocks,
                "images_dir": str(work_dir / "images"),
            }

        finally:
            client.close()


# ------------------------------------------------------------------
# 向后兼容：旧调用点/脚本直接用函数形式
# ------------------------------------------------------------------

_count_pages = count_pages  # 兼容旧导入


def run_mineru(pdf_path: str, work_dir: str, ocr: bool = True,
               progress=None, model: str | None = None) -> dict:
    """MinerUProvider().parse() 的函数式包装。"""
    return MinerUProvider().parse(pdf_path, work_dir, ocr=ocr,
                                  progress=progress, model=model)
