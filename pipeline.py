#!/usr/bin/env python3
r"""
Papers_Converter — 论文 PDF → Pandoc Markdown 转换管线（通用，不依赖 Zotero）

用法:
    python pipeline.py <paper.pdf>               # 完整管线：PDF→引擎解析→MD
    python pipeline.py <paper.pdf> --no-ocr      # 文字版 PDF（不强制 OCR）
    python pipeline.py <paper.pdf> --model pipeline  # MinerU 换 pipeline 后端（A/B）
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
import inspect
import json
import logging
import re
import sys
import time
from pathlib import Path

import config
import quality_guard
import figure_merger
from metadata import extract_metadata
from content_processor import process_content
from progress_headless import HeadlessProgress, emit_error
from qc_paper import qc_paper_md
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
    reporter: HeadlessProgress | None = None,
    coord_normalized: bool = True,
) -> Path | None:
    """
    转换单篇论文（从已解析产物目录）。

    Args:
        parsed_dir: 解析产物目录（含 *_content_list.json + images/）
        output_dir: 输出根目录
        use_llm: 是否使用 LLM 提取元数据
        source_pdf: 可选，原 PDF 路径（复制为 source.pdf）
        zotero_key: 可选，Zotero key（仅当来自 Zotero 时作为元数据写入）
        reporter: 可选，headless 进度报告器（stage 2/3/4 事件 + done 事件）

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
    t2 = time.time()
    if reporter:
        reporter.update_stage(2, "元数据提取", "提取论文元数据...")
    zotero_meta = get_zotero_meta(zotero_key) if zotero_key else None
    if zotero_meta:
        logger.info("  命中 Zotero CSL 元数据（author/date/container-title/citekey 以它为准）")
    metadata = extract_metadata(content_list, use_llm=use_llm, zotero_meta=zotero_meta)
    if zotero_key:
        metadata["zotero_key"] = zotero_key
    if reporter:
        reporter.complete_stage(2, "元数据提取", time.time() - t2)

    # 生成 slug（基于论文真实数据，任何语言）
    slug = generate_slug(metadata)
    slug = _dedup_slug(slug, metadata, output_dir)
    logger.info(f"  标题: {metadata.get('title', '?')[:60]}")
    logger.info(f"  Slug: {slug}")

    # Stage 3: 内容处理（use_llm 辅助标题结构分类）
    t3 = time.time()
    if reporter:
        reporter.update_stage(3, "内容处理", "清洗与结构化正文块...")
    images_dir = parsed_dir / "images"
    blocks = process_content(content_list, str(images_dir),
                             use_llm=use_llm, title=metadata.get("title", ""))
    logger.info(f"  处理后 {len(blocks)} 个块")
    if reporter:
        reporter.complete_stage(3, "内容处理", time.time() - t3)

    # Stage 4: 渲染装订
    if source_pdf is None:
        pdf_files = list(parsed_dir.glob("*.pdf"))
        if pdf_files:
            source_pdf = pdf_files[0]

    # 图组并集重裁：布局检测把一张 Figure 拆碎时，同词干同页块 bbox 并集整幅重裁
    # （区域光栅化，非拼接；失败保持原产物不阻断）
    if config.FIGURE_MERGE and source_pdf:
        try:
            merged = figure_merger.merge_split_figures(blocks, Path(source_pdf), images_dir, normalized=coord_normalized)
            if merged:
                logger.info(f"  图组并集重裁: {merged} 组")
        except Exception as e:
            logger.warning(f"  图组并集重裁失败（保持原产物）: {e}")

    # 退化终检（渲染前对最终正文再查一次，兜住 stage1 重试后仍失控的情形）：
    # 不阻断输出，但命中时 done 事件加 "degenerate": true，
    # SageRead 侧据以提示用户换引擎重新解析
    final_finding = quality_guard.find_degenerate_loop(
        "\n".join(b.content for b in blocks if b.content))
    if final_finding:
        logger.warning(
            f"  最终正文退化检测命中（{quality_guard.describe(final_finding)}），"
            "不阻断输出，done 事件将打标 degenerate")

    t4 = time.time()
    if reporter:
        reporter.update_stage(4, "渲染装订", "渲染 Markdown、复制图片与 source.pdf...")
    paper_md = render_paper(
        blocks=blocks,
        metadata=metadata,
        output_dir=output_dir,
        slug=slug,
        source_pdf=source_pdf,
        images_source_dir=images_dir if images_dir.exists() else None,
    )
    if reporter:
        reporter.complete_stage(4, "渲染装订", time.time() - t4)

    # QC 自检（轻量机械检查，WARN 走 stderr，不阻断转换）
    try:
        qc_paper_md(paper_md)
    except Exception as e:
        logger.warning(f"  QC 自检异常（忽略，不影响产物）: {e}")

    if reporter:
        finish_fields = dict(
            slug=slug,
            paper_dir=str(paper_md.parent.resolve()),
            paper_md=str(paper_md.resolve()),
            title=metadata.get("title", ""),
        )
        if final_finding:
            finish_fields["degenerate"] = True
        reporter.finish(**finish_fields)

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


def _retry_opts(provider, provider_opts: dict | None, attempt: int) -> dict:
    """stage1 第 attempt 次解析（1 基）的引擎参数。

    重试时如 provider 的 parse 显式声明了 temperature/seed 形参则变化之
    （升温/换种子以打破 VLM 模式延续），不支持则原样重跑。
    当前内置的 mineru/glm/paddleocr 均无这两个形参 → 原样重跑。
    """
    opts = dict(provider_opts or {})
    if attempt <= 1:
        return opts
    try:
        params = inspect.signature(provider.parse).parameters
    except (TypeError, ValueError):
        return opts
    if "temperature" in params:
        opts["temperature"] = min(0.2 * (attempt - 1), 1.0)
    if "seed" in params:
        opts["seed"] = (attempt - 1) * 10007
    return opts


def _degenerate_fallback_parse(provider, provider_opts: dict | None, pdf_path: str,
                               staging_dir: Path, ocr: bool, on_progress) -> bool:
    """退化自动降级：同引擎重试仍失控时，换 MinerU pipeline 后端兜底解析。

    VLM 类引擎的循环输出/模式延续是生成式固有风险；pipeline 后端是确定性
    检测识别流水线（无生成式幻觉），公式/表格由识别模型处理，图片完整性
    由下游 figure_merger 保障。mineru 引擎直接切 model=pipeline；其他引擎
    在已配置 MinerU Token 时换 mineru provider。降级产物覆盖 staging（引擎
    语义等同 mineru），返回是否实际执行了降级。
    """
    current_model = (provider_opts or {}).get("model")
    if provider.name == "mineru":
        if current_model == "pipeline":
            return False  # 已是 pipeline 后端，无处可退
        logger.warning("  同引擎重试仍退化，自动降级 MinerU pipeline 后端兜底解析")
        provider.parse(pdf_path, str(staging_dir), ocr=ocr, progress=on_progress, model="pipeline")
    else:
        if not config.MINERU_TOKEN:
            logger.warning("  未配置 MinerU Token，无法自动降级 pipeline（保持当前产物）")
            return False
        try:
            from ocr_provider import get_provider
            fb = get_provider("mineru")
            logger.warning("  同引擎重试仍退化，自动降级 MinerU pipeline 后端兜底解析")
            fb.parse(pdf_path, str(staging_dir), ocr=ocr, progress=on_progress, model="pipeline")
        except Exception as e:
            logger.warning(f"  降级解析失败（保持原产物）: {e}")
            return False
    # 降级后再查一次：pipeline 产物理论无循环退化，结果仅记录不阻断
    finding = quality_guard.check_staging_dir(staging_dir)
    if finding:
        logger.warning(f"  降级产物仍命中退化检测: {quality_guard.describe(finding)}")
    else:
        logger.info("  降级解析完成，产物正常")
    return True


def convert_pdf(
    pdf_path: Path,
    output_dir: Path,
    use_llm: bool = True,
    ocr: bool = True,
    skip_mineru: bool = False,
    provider_name: str | None = None,
    provider_opts: dict | None = None,
    zotero_key: str | None = None,
    headless: bool = False,
) -> Path | None:
    """完整管线：PDF → 解析引擎 → Pandoc Markdown。

    解析产物落在 output_dir/_staging/{stem}/，重跑时可 --skip-mineru 复用。
    provider_name 为空时用 config.OCR_PROVIDER；provider_opts 传给引擎
    （如 MinerU 的 {"model": "pipeline"} 做后端 A/B）。
    zotero_key：批量重解析时传入，元数据走 Zotero 权威并写入 frontmatter。
    headless：开启后进度以 JSON 行打印到 stdout（SageRead sidecar 协议，
    见 progress_headless.py），普通日志仍走 stderr。
    """
    from ocr_provider import count_pages, get_provider

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        logger.error(f"PDF 不存在: {pdf_path}")
        return None

    # 整书守卫：论文几乎不可能超过 200 页，超过即更像一本书，
    # 拒收并引导用户改走图书馆导入（books_converter 路径）
    total_pages = count_pages(str(pdf_path))
    if total_pages > config.MAX_PAPER_PAGES:
        logger.error(
            f"  该 PDF 共 {total_pages} 页（>{config.MAX_PAPER_PAGES}），"
            "论文几乎不可能超过此上限——这更像一本书。\n"
            "  请改用图书馆导入（books_converter），而非论文模块。"
        )
        return None

    provider = get_provider(provider_name)
    reporter = HeadlessProgress(pdf_path.stem, engine=provider.name) if headless else None
    if reporter:
        reporter.start()

    # staging 以 stem+内容哈希命名：不同论文都可能叫 source.pdf，仅按 stem 会碰撞
    import hashlib
    digest = hashlib.md5(pdf_path.read_bytes()).hexdigest()[:6]
    staging_dir = output_dir / "_staging" / f"{pdf_path.stem}-{digest}"

    def _on_progress(detail: str, frac: float | None = None):
        logger.info(f"  {detail}")
        if reporter:
            reporter.update_stage(1, provider.name, detail, frac)

    # Stage 1: 引擎解析（可跳过复用已有产物）
    need_parse = not skip_mineru or not list(staging_dir.glob("*_content_list.json"))
    # 实际解析引擎（退化自动降级后为 mineru）：决定下游图合并的坐标语义
    effective_provider_name = provider.name
    if need_parse:
        logger.info(f"\n=== Stage 1: {provider.name} 解析 {pdf_path.name} ===")
        if reporter:
            reporter.update_stage(1, provider.name, f"{provider.name} 解析 {pdf_path.name}")
        t1 = time.time()
        # 退化检测与打回重解析：VLM 引擎偶发"模式延续"失控（如波长列从真实值
        # 一路编造递增、单词重复数百次），失控是随机的，重跑常能自愈
        for attempt in range(1, quality_guard.MAX_STAGE1_RETRIES + 2):
            provider.parse(str(pdf_path), str(staging_dir), ocr=ocr,
                           progress=_on_progress,
                           **_retry_opts(provider, provider_opts, attempt))
            finding = quality_guard.check_staging_dir(staging_dir)
            if not finding:
                break
            logger.warning(
                f"  stage1 产物退化检测命中: {quality_guard.describe(finding)}")
            if attempt > quality_guard.MAX_STAGE1_RETRIES:
                if _degenerate_fallback_parse(provider, provider_opts, str(pdf_path),
                                              staging_dir, ocr, _on_progress):
                    effective_provider_name = "mineru"
                    if reporter:
                        reporter.update_stage(1, provider.name, "已自动降级 pipeline 后端重解析")
                else:
                    logger.warning("  已达最大重试次数，接受当前产物继续下游"
                                   "（渲染前还会对最终正文再检测一次）")
                break
            detail = f"检测到异常重复内容，正在重试 OCR（第 {attempt} 次）"
            logger.warning(f"  {detail}")
            if reporter:
                reporter.update_stage(1, provider.name, detail)
        if reporter:
            reporter.complete_stage(1, provider.name, time.time() - t1)
    else:
        logger.info(f"  跳过解析，复用已有产物: {staging_dir}")
        if reporter:
            reporter.update_stage(1, provider.name, "复用已有解析产物")
            reporter.complete_stage(1, provider.name, 0.0)

    # Stage 2/3/4: 转换
    return convert_single(
        staging_dir, output_dir, use_llm=use_llm,
        source_pdf=pdf_path, zotero_key=zotero_key,
        reporter=reporter,
        # 图组并集重裁的坐标语义：MinerU content_list 为 0-1000 归一化（目前唯一核实；
        # 退化自动降级后产物等同 mineru，以实际解析引擎为准）
        coord_normalized=(effective_provider_name == "mineru"),
    )


def main():
    parser = argparse.ArgumentParser(
        description="Papers_Converter — 论文 PDF → Pandoc Markdown（通用管线）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python pipeline.py D:\\papers\\some_paper.pdf    # 完整管线 PDF→引擎解析→MD
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
        help="PDF 模式下复用已有解析产物，不重新提交解析（任意 provider 均适用）",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Stage 1 解析引擎（默认取 .env 的 OCR_PROVIDER，当前内置: mineru）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="传给解析引擎的后端/模型名（如 MinerU 的 vlm / pipeline，用于后端 A/B）",
    )
    parser.add_argument(
        "--reparse",
        action="store_true",
        help="批量模式下不用缓存产物，用 --provider 指定的引擎重新解析每篇 PDF",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="批量模式下最多处理 N 篇（调试用）",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无界面模式：进度以 JSON 行打印到 stdout（SageRead sidecar 协议），"
             "仅作用于单篇 PDF 转换路径",
    )

    args = parser.parse_args()

    # headless 模式：捕获首条 ERROR 日志，作为 error 事件的 message（首条最贴近根因）
    err_capture = None
    if args.headless:
        class _FirstErrorCapture(logging.Handler):
            def __init__(self):
                super().__init__(logging.ERROR)
                self.first = ""

            def emit(self, record):
                if not self.first:
                    self.first = record.getMessage()

        err_capture = _FirstErrorCapture()
        logging.getLogger().addHandler(err_capture)

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
                if args.reparse:
                    pdfs = list(d.glob("*.pdf"))
                    if not pdfs:
                        logger.error("  未找到 PDF，跳过")
                        failed += 1
                        continue
                    result = convert_pdf(pdfs[0], output_dir, use_llm=use_llm,
                                         ocr=not args.no_ocr,
                                         skip_mineru=args.skip_mineru,
                                         provider_name=args.provider,
                                         zotero_key=d.name)
                else:
                    result = convert_single(d, output_dir, use_llm=use_llm,
                                            zotero_key=d.name)
                if result:
                    success += 1
                else:
                    failed += 1
            except Exception as e:
                logger.error(f"  转换失败: {e}")
                failed += 1
                # 配额熔断：日配额/限流错误继续跑只会全军覆没
                if any(k in str(e) for k in ("12001", "429", "配额")):
                    logger.error("  检测到配额/限流错误，中止批量（明日配额重置后再续）")
                    break

        elapsed = time.time() - total_start
        logger.info("\n" + "=" * 60)
        logger.info(f"  批量转换完成: 成功 {success}, 失败 {failed}, 耗时 {elapsed:.1f}s")
        logger.info("=" * 60)

    elif args.target:
        target = args.target
        target_path = Path(target)

        # 情况 1：PDF 文件 → 完整管线（PDF→解析引擎→MD）
        if target_path.suffix.lower() == ".pdf" or target_path.is_file():
            provider_opts = {"model": args.model} if args.model else None
            try:
                result = convert_pdf(target_path, output_dir, use_llm=use_llm,
                                     ocr=not args.no_ocr, skip_mineru=args.skip_mineru,
                                     provider_name=args.provider,
                                     provider_opts=provider_opts,
                                     headless=args.headless)
            except Exception as e:
                # headless：栈留 stderr，stdout 发 error 事件后非 0 退出
                logger.exception("  转换失败")
                if args.headless:
                    emit_error(str(e) or (err_capture.first if err_capture else "")
                               or "转换失败")
                sys.exit(1)
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
            if args.headless:
                emit_error((err_capture.first if err_capture else "") or "转换失败")
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
