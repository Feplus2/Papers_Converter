"""Stage 3: Pandoc Markdown 渲染。

将处理后的 IR 渲染为符合 paper-format-contract.md 的 Pandoc Markdown，
并管理图片复制和输出目录结构。
"""

import logging
import re
import shutil
from pathlib import Path

import yaml

from content_processor import ProcessedBlock
from link_extractor import block_anchor_id

logger = logging.getLogger(__name__)


class _FoldedScalar(str):
    """标记为折叠块标量（>-）的字符串，避免 PyYAML 按宽度硬换行。"""
    pass


def _folded_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=">")


yaml.add_representer(_FoldedScalar, _folded_representer)


def render_paper(
    blocks: list[ProcessedBlock],
    metadata: dict,
    output_dir: Path,
    slug: str,
    source_pdf: Path | None = None,
    images_source_dir: Path | None = None,
    link_anchors: set | None = None,
) -> Path:
    """
    渲染论文为 Pandoc Markdown 并输出到目录。

    Args:
        blocks: 处理后的 ProcessedBlock 列表
        metadata: frontmatter 元数据 dict
        output_dir: 输出根目录
        slug: 论文 slug
        source_pdf: 可选的源 PDF 路径
        images_source_dir: 原始图片目录
        link_anchors: 可选，需发射的锚点 id 集合（P1 原生链接保留，
            见 link_extractor；None/空集 → 不发射任何锚点，输出与旧版一致）

    Returns:
        paper.md 的完整路径
    """
    # 创建输出目录
    paper_dir = output_dir / slug
    paper_dir.mkdir(parents=True, exist_ok=True)
    images_out_dir = paper_dir / "images"
    # 清空旧图片，避免多次运行后残留
    if images_out_dir.exists():
        for f in images_out_dir.iterdir():
            if f.is_file():
                f.unlink()
    images_out_dir.mkdir(exist_ok=True)

    # 渲染 frontmatter
    frontmatter_str = _render_frontmatter(metadata)

    # 渲染正文
    body_str = _render_body(blocks, images_source_dir, images_out_dir,
                            link_anchors=link_anchors)

    # 组合最终文档
    doc = frontmatter_str + "\n" + body_str

    # 写入文件（UTF-8 + LF）
    paper_md_path = paper_dir / "paper.md"
    paper_md_path.write_text(doc, encoding="utf-8", newline="\n")

    # 复制 source.pdf（可选）
    if source_pdf and source_pdf.exists():
        shutil.copy2(source_pdf, paper_dir / "source.pdf")

    logger.info(f"  输出: {paper_md_path}")
    return paper_md_path


def _render_frontmatter(metadata: dict) -> str:
    """渲染 YAML frontmatter"""
    # 构建有序的 frontmatter 字段
    fm = {}

    # 必填字段
    fm["title"] = metadata.get("title", "Untitled")

    # author: 结构化作者列表
    authors = metadata.get("author", [])
    if authors:
        fm["author"] = authors
    else:
        fm["author"] = [{"name": "Unknown"}]

    fm["date"] = str(metadata.get("date", ""))

    # abstract 用折叠块标量（>-），避免 PyYAML 按宽度硬换行
    abstract = metadata.get("abstract", "")
    fm["abstract"] = _FoldedScalar(abstract) if abstract else ""

    # 可选字段
    if metadata.get("doi"):
        fm["doi"] = metadata["doi"]
    if metadata.get("container-title"):
        fm["container-title"] = metadata["container-title"]
    if metadata.get("keywords"):
        fm["keywords"] = metadata["keywords"]
    if metadata.get("volume"):
        fm["volume"] = str(metadata["volume"])
    if metadata.get("issue"):
        fm["issue"] = str(metadata["issue"])
    if metadata.get("page"):
        fm["page"] = str(metadata["page"])
    # 额外 CSL 变量原样直传（契约 §三：渲染器忽略未知字段，pandoc --citeproc 可消费）
    for csl_key in ("type", "URL", "ISSN", "publisher"):
        if metadata.get(csl_key):
            fm[csl_key] = str(metadata[csl_key])
    if metadata.get("arxiv"):
        fm["arxiv"] = metadata["arxiv"]
    if metadata.get("zotero_key"):
        fm["zotero_key"] = metadata["zotero_key"]

    fm["lang"] = metadata.get("lang", "en")

    # 使用自定义 Dumper：abstract 走折叠块标量，宽度放大防止硬换行
    yaml_str = yaml.dump(
        fm,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=4096,
    )

    return f"---\n{yaml_str}---\n"


def _render_body(
    blocks: list[ProcessedBlock],
    images_source_dir: Path | None,
    images_out_dir: Path,
    link_anchors: set | None = None,
) -> str:
    """渲染正文 Markdown"""
    lines = []
    prev_kind = None
    emitted_anchors: set = set()  # 锚点去重：同 id 只发射第一次（重复标题/重号图）

    def _anchor_line(block) -> str | None:
        """P1 锚点：块在链接目标集合内且未发射过 → 行内 HTML 锚点标记"""
        if not link_anchors:
            return None
        aid = block_anchor_id(block)
        if aid and aid in link_anchors and aid not in emitted_anchors:
            emitted_anchors.add(aid)
            return f'<a id="{aid}"></a>'
        return None

    for block in blocks:
        # 段落间距控制
        if block.kind in ("heading", "page_anchor") and prev_kind and prev_kind != "page_anchor":
            lines.append("")
        elif block.kind == "paragraph" and prev_kind in (
                "paragraph", "reference", "table", "image", "table_image"):
            lines.append("")
        elif block.kind == "reference" and prev_kind:
            # 每条引用各自成段（连续裸行会被 CommonMark 软换行合并成一个巨型段落）
            lines.append("")
        elif block.kind in ("image", "equation", "table", "table_image") and prev_kind:
            lines.append("")

        if block.kind == "page_anchor":
            lines.append(f"<!-- page: {block.content} -->")

        elif block.kind == "heading":
            anchor = _anchor_line(block)
            if anchor:
                lines.append(anchor)
            prefix = "#" * min(block.level, 6)
            lines.append(f"{prefix} {block.content}")

        elif block.kind == "paragraph":
            # 单行书写
            text = block.content.replace("\n", " ").strip()
            lines.append(text)

        elif block.kind == "image" or block.kind == "table_image":
            anchor = _anchor_line(block)
            if anchor:
                lines.append(anchor)
            # 复制图片
            if images_source_dir and block.img_src:
                src_path = images_source_dir / Path(block.img_src).name
                if not src_path.exists():
                    # 尝试直接用 img_src 作为相对路径
                    src_path = images_source_dir / block.img_src
                if src_path.exists():
                    dst_path = images_out_dir / block.img_new_name
                    shutil.copy2(src_path, dst_path)
                else:
                    logger.warning(f"  图片未找到: {block.img_src}")

            # Markdown 图片语法：alt 只留短标签（如 "Figure 2"），完整图注
            # 作为同段落内的正文文本行（软换行分隔，与 MinerU 产物格式对齐），
            # 保证 SageRead 切块/RAG/翻译与读者均可见（alt 文本对其不可见）。
            # image 的 content 已由 _assign_figure_numbers 格式化为
            # "Figure N: caption"；table_image 保留原表注。
            caption = (block.content or block.caption or "").replace("\n", " ").strip()
            m = re.match(
                r"^((?:Figure|Table|Scheme|Chart)\s+[\w.\-]+(?:\s*\([a-zA-Z0-9]+\))?)\s*:(.*)$",
                caption, re.S)
            if m:
                alt = m.group(1)
            elif caption and len(caption) <= 30:
                # 无 "标签: 正文" 结构的短标签（如子图 "Figure 4 (a)"）
                alt = caption
            else:
                alt = "Figure"
            # alt 内不允许出现方括号
            alt = alt.replace("[", "(").replace("]", ")")
            lines.append(f"![{alt}](images/{block.img_new_name})")
            if caption:
                # 图注文本行与图片行之间不留空行 → 同段落软换行
                lines.append(caption)

        elif block.kind == "equation":
            lines.append(block.content)

        elif block.kind == "reference":
            anchor = _anchor_line(block)
            # 锚点标记置于条目首（行内 HTML，与条目同段）
            lines.append(anchor + block.content if anchor else block.content)

        elif block.kind == "table":
            # HTML 表体原样输出（契约 §四：复杂表格用 HTML <table>）；
            # caption 作独立段落置于表前，表前后留空行避免被 CommonMark
            # HTML 块吞掉后续正文
            anchor = _anchor_line(block)
            if anchor:
                lines.append(anchor)
            if block.caption:
                lines.append(block.caption)
                lines.append("")
            lines.append(block.content)

        prev_kind = block.kind

    # 确保文件以换行结尾
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"

    return text
