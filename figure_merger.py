"""同图碎块并集重裁（figure_merger）

背景：MinerU 布局检测把一张 Figure 拆成多个块（子图 a/b/c 各一块），合并阈值
硬编码、官方无开关（MinerU issues #4335/#4008 等）。converter 的
_assign_figure_numbers 已把碎块归组为同一 fig{N} 词干，本模块在归组之后：
同词干、同页、≥2 块的组 → bbox 并集 → 从源 PDF 整幅光栅化重裁。

注意：重裁 = 把并集区域内的原始内容（位图+矢量+文字）重新光栅化，无损无接缝，
不是把已裁碎的小图拼接。合并失败一律保持原产物，不阻断管线。
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# fig3.jpg / fig3a.jpg / fig12-4b.png 同词干归组（章节式编号已转连字符）
_FIG_STEM_RE = re.compile(r"^(fig\d[\d-]*?)([a-z])?\.(?:jpe?g|png)$", re.I)

_RENDER_DPI = 200
_PADDING_PT = 5.0
# 版式守卫：并集纵向跨度超过页高 75% 大概率是把上下两张独立图误并，放弃合并
_MAX_Y_SPAN_RATIO = 0.75
# 并集面积超过页面 90% 同理
_MAX_AREA_RATIO = 0.90


def _to_fitz_rect(bbox, page_rect, coord_space):
    """bbox → 页面 pt 矩形。坐标空间：
    - "mineru"：0-1000 归一化
    - "paddleocr"：API 页渲染像素，实测 2 px/pt（144 DPI；block_bbox/2 即 pt）
    其他空间未核实，由调用方拦截。"""
    import fitz  # 延迟导入：纯后处理路径才需要

    x0, y0, x1, y1 = bbox
    if coord_space == "mineru":
        sx = page_rect.width / 1000.0
        sy = page_rect.height / 1000.0
    elif coord_space == "paddleocr":
        sx = sy = 0.5
    else:
        raise ValueError(f"未支持的坐标空间: {coord_space}")
    rect = fitz.Rect(x0 * sx - _PADDING_PT, y0 * sy - _PADDING_PT, x1 * sx + _PADDING_PT, y1 * sy + _PADDING_PT)
    return rect & page_rect  # 交集防越界


_SUPPORTED_SPACES = ("mineru", "paddleocr")


def merge_split_figures(blocks, pdf_path, images_dir, dpi=_RENDER_DPI, coord_space="mineru") -> int:
    """原地修改 blocks：可合并组的主图换整幅重裁、碎块移除。返回合并组数。

    blocks: process_content 输出（已 _assign_figure_numbers，块带 bbox/img_new_name）
    pdf_path: 源 PDF（重裁的画布）
    images_dir: staging 图片目录（重裁产物落此，渲染器按 img_src 复制）
    coord_space: bbox 坐标空间（"mineru" 0-1000 归一化 / "paddleocr" 144DPI 像素；
        其他 provider 未核实，整体跳过合并）
    """
    import fitz

    if coord_space not in _SUPPORTED_SPACES:
        logger.info(f"  坐标空间 {coord_space} 未支持，图组并集重裁跳过")
        return 0

    # 1. 按 fig{N} 词干归组（复用 _assign_figure_numbers 的命名结果，不动其逻辑）
    groups = {}  # stem -> {"main": block|None, "subs": [...]}
    for b in blocks:
        if b.kind != "image" or not getattr(b, "img_new_name", "") or not getattr(b, "bbox", None):
            continue
        m = _FIG_STEM_RE.match(b.img_new_name)
        if not m:
            continue
        g = groups.setdefault(m.group(1), {"main": None, "subs": []})
        if m.group(2):
            g["subs"].append(b)
        else:
            g["main"] = b

    candidates = {
        stem: g for stem, g in groups.items()
        if g["main"] is not None and len(g["subs"]) >= 1
    }
    if not candidates:
        return 0

    doc = fitz.open(str(pdf_path))
    merged = 0
    drop_ids = set()
    try:
        for stem, g in candidates.items():
            members = [g["main"]] + g["subs"]
            pages = {b.page_idx for b in members}
            if len(pages) != 1:
                logger.info(f"  图组 {stem} 跨页 {sorted(pages)}，保持原样")
                continue
            page = doc[members[0].page_idx]

            union = None
            for b in members:
                r = _to_fitz_rect(b.bbox, page.rect, coord_space)
                union = r if union is None else (union | r)
            if union is None or union.is_empty:
                continue
            # 版式守卫：纵向跨度/面积异常→疑似误并两张独立图，保持原样
            if union.height > page.rect.height * _MAX_Y_SPAN_RATIO:
                logger.info(f"  图组 {stem} 并集纵向跨页 {union.height / page.rect.height:.0%}，疑似误并，保持原样")
                continue
            if union.get_area() > page.rect.get_area() * _MAX_AREA_RATIO:
                logger.info(f"  图组 {stem} 并集面积占页 {union.get_area() / page.rect.get_area():.0%}，疑似误并，保持原样")
                continue

            pix = page.get_pixmap(clip=union, dpi=dpi)
            out_name = f"{stem}_merged.jpg"
            pix.save(str(Path(images_dir) / out_name), output="jpeg", jpg_quality=90)

            g["main"].img_src = out_name  # 渲染器按 img_src 复制为 img_new_name
            for sb in g["subs"]:
                drop_ids.add(id(sb))
            merged += 1
            logger.info(f"  图组 {stem}: {len(members)} 块 → 整幅重裁 {out_name}")
    finally:
        doc.close()

    if drop_ids:
        blocks[:] = [b for b in blocks if id(b) not in drop_ids]
    return merged
