#!/usr/bin/env python3
r"""
Papers_Converter — 论文 PDF → Pandoc Markdown 转换管线（通用，不依赖 Zotero）

用法:
    python pipeline.py <paper.pdf>               # 完整管线：PDF→MinerU解析→MD
    python pipeline.py <paper.pdf> --no-ocr      # 文字版 PDF（不强制 OCR）
    python pipeline.py <parsed_dir>              # 仅转换已解析产物目录
    python pipeline.py <parsed_key>              # 仅转换（Zotero key，示例数据源）
    python pipeline.py --all                     # 批量转换 parsed/ 下全部论文
    python pipeline.py --all --no-llm            # 批量，纯规则提取元数据

示例:
    python pipeline.py D:\papers\some_paper.pdf
    python pipeline.py 26NNZJHX
    python pipeline.py --all --output-dir F:\papers_md
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path

import config
from metadata import extract_metadata
from content_processor import process_content
from renderer import render_paper
from slug import generate_slug
from zotero_meta import get_zotero_meta

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline")


def convert_single(
    parsed_dir: Path,
    output_dir: Path,
    use_llm: bool = True,
    source_pdf: Path | None = None,
    zotero_key: str | None = None,
) -> Path | None:
    """
    转换单篇论文（从已解析产物目录）。

    Args:
        parsed_dir: 解析产物目录（含 *_content_list.json + images/）
        output_dir: 输出根目录
        use_llm: 是否使用 LLM 提取元数据
        source_pdf: 可选，原 PDF 路径（复制为 source.pdf）
        zotero_key: 可选，Zotero key（仅当来自 Zotero 时作为元数据写入）

    Returns:
        paper.md 路径，失败返回 None
    """
    # 查找 content_list.json
    cl_files = list(parsed_dir.glob("*_content_list.json"))
    if not cl_files:
        logger.error(f"  未找到 content_list.json: {parsed_dir}")
        return None

    with open(cl_files[0], "r", encoding="utf-8") as f:
        content_list = json.load(f)

    if not content_list:
        logger.error(f"  content_list 为空: {parsed_dir}")
        return None

    # 整书守卫（已解析产物同样适用）：页数超上限判为书籍，拒收
    max_page = max((b.get("page_idx", 0) for b in content_list), default=0)
    if max_page + 1 > config.MAX_PAPER_PAGES:
        logger.error(
            f"  该文档共 {max_page + 1} 页（>{config.MAX_PAPER_PAGES}），"
            "论文几乎不可能超过此上限——这更像一本书。\n"
            "  请改用图书馆导入（books_converter），而非论文模块。"
        )
        return None

    logger.info(f"  加载 {len(content_list)} 个内容块")

    # Stage 2: 元数据提取（Zotero/CSL-JSON 权威元数据优先，LLM 只补 abstract）
    zotero_meta = get_zotero_meta(zotero_key) if zotero_key else None
    if zotero_meta:
        logger.info("  命中 Zotero CSL 元数据（author/date/container-title/citekey 以它为准）")
    metadata = extract_metadata(content_list, use_llm=use_llm, zotero_meta=zotero_meta)
    if zotero_key:
        metadata["zotero_key"] = zotero_key

    # 生成 slug（基于论文真实数据，任何语言）
    slug = generate_slug(metadata)
    slug = _dedup_slug(slug, metadata, output_dir)
    logger.info(f"  标题: {metadata.get('title', '?')[:60]}")
    logger.info(f"  Slug: {slug}")

    # Stage 2: 正文处理（use_llm 辅助标题结构分类）
    images_dir = parsed_dir / "images"
    blocks = process_content(content_list, str(images_dir),
                             use_llm=use_llm, title=metadata.get("title", ""))
    logger.info(f"  处理后 {len(blocks)} 个块")

    # Stage 3: 渲染输出
    if source_pdf is None:
        pdf_files = list(parsed_dir.glob("*.pdf"))
        if pdf_files:
            source_pdf = pdf_files[0]

    paper_md = render_paper(
        blocks=blocks,
        metadata=metadata,
        output_dir=output_dir,
        slug=slug,
        source_pdf=source_pdf,
        images_source_dir=images_dir if images_dir.exists() else None,
    )

    return paper_md


def _dedup_slug(slug: str, metadata: dict, output_dir: Path) -> str:
    """slug 碰撞消歧：不同论文算出相同 slug 时加后缀。

    同 slug 目录已存在且属于同一篇（zotero_key 一致）→ 复用（重转覆盖）；
    属于不同论文 → 追加 zotero_key 或标题短哈希后缀（如 chen2023d-ufj6tyeh）。
    """
    paper_md = output_dir / slug / "paper.md"
    if not paper_md.exists():
        return slug
    try:
        # frontmatter 可能很长（折叠 abstract），64KB 足够覆盖
        head = paper_md.read_text(encoding="utf-8")[:65536]
    except OSError:
        return slug
    zkey = metadata.get("zotero_key", "")
    if zkey and re.search(rf"^zotero_key:\s*[\"']?{re.escape(zkey)}[\"']?\s*$",
                          head, re.M):
        return slug
    suffix = (zkey or hashlib.md5(
        (metadata.get("title") or slug).encode("utf-8")).hexdigest()[:6]).lower()
    new_slug = f"{slug}-{suffix}"
    logger.warning(f"  slug 碰撞: {slug} 已被他篇占用，改用 {new_slug}")
    return new_slug


def convert_pdf(
    pdf_path: Path,
    output_dir: Path,
    use_llm: bool = True,
    ocr: bool = True,
    skip_mineru: bool = False,
) -> Path | None:
    """完整管线：PDF → MinerU 云解析 → Pandoc Markdown。

    解析产物落在 output_dir/_staging/{stem}/，重跑时可 --skip-mineru 复用。
    """
    from stage1_mineru import run_mineru, _count_pages

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        logger.error(f"PDF 不存在: {pdf_path}")
        return None

    # 整书守卫：论文几乎不可能超过 200 页，超过即更像一本书，
    # 拒收并引导用户改走图书馆导入（books_converter 路径）
    total_pages = _count_pages(str(pdf_path))
    if total_pages > config.MAX_PAPER_PAGES:
        logger.error(
            f"  该 PDF 共 {total_pages} 页（>{config.MAX_PAPER_PAGES}），"
            "论文几乎不可能超过此上限——这更像一本书。\n"
            "  请改用图书馆导入（books_converter），而非论文模块。"
        )
        return None

    staging_dir = output_dir / "_staging" / pdf_path.stem

    # Stage 1: MinerU 解析（可跳过复用已有产物）
    need_parse = not skip_mineru or not list(staging_dir.glob("*_content_list.json"))
    if need_parse:
        logger.info(f"\n=== Stage 1: MinerU 解析 {pdf_path.name} ===")
        run_mineru(str(pdf_path), str(staging_dir), ocr=ocr,
                   progress=lambda detail, frac=None: logger.info(f"  {detail}"))
    else:
        logger.info(f"  跳过 MinerU，复用已有解析: {staging_dir}")

    # Stage 2/3: 转换（非 Zotero 来源，不写 zotero_key）
    return convert_single(
        staging_dir, output_dir, use_llm=use_llm,
        source_pdf=pdf_path, zotero_key=None,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Papers_Converter — 论文 PDF → Pandoc Markdown（通用管线）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python pipeline.py D:\\papers\\some_paper.pdf    # 完整管线 PDF→MinerU→MD
  python pipeline.py paper.pdf --no-ocr           # 文字版 PDF
  python pipeline.py 26NNZJHX                     # 单篇（Zotero key，示例源）
  python pipeline.py F:\\path\\to\\parsed\\KEY      # 单篇（已解析目录）
  python pipeline.py --all                        # 批量全部
  python pipeline.py --all --no-llm               # 批量，纯规则
        """,
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="PDF 文件 / 已解析目录 / Zotero key",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="批量转换 parsed/ 下全部论文",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=None,
        help="输出目录 (默认: ./output)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="不使用 LLM，纯规则提取元数据",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="文字版 PDF，不强制 OCR",
    )
    parser.add_argument(
        "--skip-mineru",
        action="store_true",
        help="PDF 模式下复用已有解析产物，不重新提交 MinerU",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="批量模式下最多处理 N 篇（调试用）",
    )

    args = parser.parse_args()

    # 确定输出目录
    output_dir = Path(args.output_dir) if args.output_dir else config.DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    use_llm = not args.no_llm

    logger.info("=" * 60)
    logger.info("  Papers_Converter")
    logger.info(f"  输出目录: {output_dir}")
    logger.info(f"  LLM 元数据: {'启用' if use_llm else '禁用'}")
    logger.info("=" * 60)

    if args.all:
        # 批量模式（示例数据源：parsed/ 下的 Zotero 解析缓存）
        if not config.PARSED_DIR.exists():
            logger.error(f"parsed 目录不存在: {config.PARSED_DIR}")
            sys.exit(1)

        dirs = sorted(d for d in config.PARSED_DIR.iterdir() if d.is_dir())
        if args.limit:
            dirs = dirs[:args.limit]

        logger.info(f"  待处理: {len(dirs)} 篇")
        success = 0
        failed = 0
        total_start = time.time()

        for i, d in enumerate(dirs, 1):
            logger.info(f"\n[{i}/{len(dirs)}] {d.name}")
            try:
                result = convert_single(d, output_dir, use_llm=use_llm,
                                        zotero_key=d.name)
                if result:
                    success += 1
                else:
                    failed += 1
            except Exception as e:
                logger.error(f"  转换失败: {e}")
                failed += 1

        elapsed = time.time() - total_start
        logger.info("\n" + "=" * 60)
        logger.info(f"  批量转换完成: 成功 {success}, 失败 {failed}, 耗时 {elapsed:.1f}s")
        logger.info("=" * 60)

    elif args.target:
        target = args.target
        target_path = Path(target)

        # 情况 1：PDF 文件 → 完整管线（PDF→MinerU→MD）
        if target_path.suffix.lower() == ".pdf" or target_path.is_file():
            result = convert_pdf(target_path, output_dir, use_llm=use_llm,
                                 ocr=not args.no_ocr, skip_mineru=args.skip_mineru)
        # 情况 2：已解析目录
        elif target_path.is_dir():
            result = convert_single(target_path, output_dir, use_llm=use_llm)
        # 情况 3：Zotero key（示例数据源）
        else:
            parsed_dir = config.PARSED_DIR / target
            if not parsed_dir.exists():
                logger.error(f"未找到解析目录: {parsed_dir}（也不是 PDF 文件）")
                sys.exit(1)
            result = convert_single(parsed_dir, output_dir, use_llm=use_llm,
                                    zotero_key=target)

        if result:
            logger.info(f"\n  转换成功: {result}")
        else:
            logger.error("\n  转换失败")
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
