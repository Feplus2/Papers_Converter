"""同图碎块并集重裁（figure_merger）

背景：MinerU 布局检测把一张 Figure 拆成多个块（子图 a/b/c 各一块），合并阈值
硬编码、官方无开关（MinerU issues #4335/#4008 等）。

归组规则（2026-08-13 改为**就近成组**，用户拍板）：
同页连续图片块，中间只夹小文字块（≤20 字符的面板字母/碎片，如 "A"、"(a)"）
即归为一组；大文字块（图注/正文段落）隔开才断开。不再依赖图编号词干——
编号本身可能错（zhao2020 实证：游离 "Fig. 1." 图注未绑回，两个 Figure 1
面板被吞进 Figure 2 组）。

组内取 bbox 并集 → 从源 PDF 整幅光栅化重裁。重裁 = 把并集区域内的原始内容
（位图+矢量+文字）重新光栅化，无损无接缝，不是把已裁碎的小图拼接。
合并失败一律保持原产物，不阻断管线。
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
# 小文字块豁免阈值：面板字母/碎片（"A"、"(a)"、"A C B C"）不打断图组
_MAX_INTERRUPT_CHARS = 20
# 真图注标记（组内存活块优先选带真图注的；编号后必须跟 . 或 : 才算——
# 面板标签 'Figure 2 (a)' 是 _assign_figure_numbers 生成的占位，误当图注
# 会让每个带标块各自成组、永远合并不了）
_REAL_CAPTION_RE = re.compile(r"^\s*(?:Figure|Fig\.?)\s*\d+(?:\.\d+)*\s*[\.\:]", re.I)


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


def _proximity_runs(blocks) -> list[list]:
    """就近成组：同页连续图片块为一组；中间只夹小文字块（≤20 字符的面板
    字母/碎片）不打断；大文字块（图注/正文段落）隔开则断开。"""
    runs: list[list] = []
    cur: list = []
    cur_page = None
    for b in blocks:
        if b.kind == "image" and getattr(b, "bbox", None):
            if cur and b.page_idx != cur_page:
                if len(cur) >= 2:
                    runs.append(cur)
                cur = []
            # 真图注块是组界：当前组若已有真图注成员，说明它是**新图**的主块
            # ——先收尾（不含它）再开新组（madler2001 实证：Fig.7/Fig.8 两图
            # 同页相邻都被扫进一组，Figure 8 整图丢失）
            is_cap = bool(_REAL_CAPTION_RE.match((b.caption or b.content or "").strip()))
            has_cap = any(_REAL_CAPTION_RE.match((m.caption or m.content or "").strip())
                          for m in cur)
            if is_cap and has_cap:
                if len(cur) >= 2:
                    runs.append(cur)
                cur = []
            cur.append(b)
            cur_page = b.page_idx
            if is_cap and not has_cap and len(cur) >= 2:
                # caption-last：真图注块收尾当前组
                runs.append(cur)
                cur = []
                cur_page = None
            continue
        if b.kind == "page_anchor":
            continue  # 页锚透明（页变化会在下一个 image 块触发断组）
        text_len = len((getattr(b, "content", "") or "").strip())
        if text_len <= _MAX_INTERRUPT_CHARS:
            continue  # 小文字块豁免（面板字母/碎片）
        if len(cur) >= 2:
            runs.append(cur)
        cur = []
        cur_page = None
    if len(cur) >= 2:
        runs.append(cur)
    return runs


def merge_split_figures(blocks, pdf_path, images_dir, dpi=_RENDER_DPI, coord_space="mineru") -> int:
    """原地修改 blocks：可合并组的存活块换整幅重裁、其余块移除。返回合并组数。

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

    # 就近成组（不依赖图编号——编号可能错，位置不会）
    runs = _proximity_runs(blocks)
    if not runs:
        return 0

    doc = fitz.open(str(pdf_path))
    merged = 0
    drop_ids = set()
    used_names: set[str] = set()
    try:
        for members in runs:
            pages = {b.page_idx for b in members}
            if len(pages) != 1:
                logger.info(f"  图组跨页 {sorted(pages)}，保持原样")
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
                logger.info(f"  图组并集纵向跨页 {union.height / page.rect.height:.0%}，疑似误并，保持原样")
                continue
            if union.get_area() > page.rect.get_area() * _MAX_AREA_RATIO:
                logger.info(f"  图组并集面积占页 {union.get_area() / page.rect.get_area():.0%}，疑似误并，保持原样")
                continue

            # 存活块：优先带真图注的成员（图注随图走），否则首块
            survivor = next(
                (b for b in members
                 if _REAL_CAPTION_RE.match((b.caption or b.content or "").strip())),
                members[0])
            stem_m = _FIG_STEM_RE.match(survivor.img_new_name or "")
            stem = stem_m.group(1) if stem_m else "fig"
            out_name = f"{stem}_merged.jpg"
            n = 1
            while out_name in used_names:
                n += 1
                out_name = f"{stem}_merged{n}.jpg"
            used_names.add(out_name)
            pix = page.get_pixmap(clip=union, dpi=dpi)
            pix.save(str(Path(images_dir) / out_name), output="jpeg", jpg_quality=90)

            survivor.img_src = out_name  # 渲染器按 img_src 复制为 img_new_name
            for sb in members:
                if sb is not survivor:
                    drop_ids.add(id(sb))
            merged += 1
            logger.info(f"  图组 {stem}: {len(members)} 块 → 整幅重裁 {out_name}")
    finally:
        doc.close()

    if drop_ids:
        blocks[:] = [b for b in blocks if id(b) not in drop_ids]
    return merged
