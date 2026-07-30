"""Zotero/CSL-JSON 权威元数据源。

CSL-JSON 导出文件（由 export_zotero_csl.py 生成）提供权威元数据：
author / container-title / date / citekey 以 Zotero 为准，LLM 只补 abstract。

文件结构：{zotero_key: csl_item}，路径由 config.ZOTERO_CSL_JSON 指定。
缺失导出文件或 key 未命中时返回 None，调用方回退到规则/LLM 提取。
"""

import json
import logging
import re
from pathlib import Path

import config
from content_processor import _normalize_inline

logger = logging.getLogger(__name__)

_store_cache: dict | None = None


def _load_store() -> dict:
    """加载 CSL-JSON 导出文件（进程内缓存一次）"""
    global _store_cache
    if _store_cache is not None:
        return _store_cache

    _store_cache = {}
    path = Path(config.ZOTERO_CSL_JSON)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _store_cache = data
                logger.info(f"  加载 Zotero CSL 元数据: {len(data)} 条 ({path})")
            else:
                logger.warning(f"  CSL 导出文件结构异常（应为 key→item 字典）: {path}")
        except Exception as e:
            logger.warning(f"  CSL 导出文件读取失败: {e}")
    else:
        logger.info(f"  无 CSL 导出文件（{path}），Zotero 权威元数据不可用")
    return _store_cache


def get_zotero_meta(zotero_key: str) -> dict | None:
    """按 Zotero key 查权威元数据，返回 frontmatter dict；未命中返回 None"""
    if not zotero_key:
        return None
    csl = _load_store().get(zotero_key)
    if not csl:
        return None
    return _csl_to_meta(csl)


def _csl_to_meta(csl: dict) -> dict:
    """CSL-JSON item → frontmatter dict（字段对齐 paper-format-contract.md）"""
    meta = {}

    # 作者：family+given 组合为 "名 姓"；机构作者用 literal；
    # 无作者时用 editor 顶替（CSL 标准替换规则，如书籍条目只有编者）
    authors = []
    for role in ("author", "editor"):
        for a in csl.get(role) or []:
            if not isinstance(a, dict):
                continue
            name = a.get("literal") or f"{a.get('given', '')} {a.get('family', '')}".strip()
            if name:
                authors.append({"name": name})
        if authors:
            break
    if authors:
        meta["author"] = authors

    # 日期：issued.date-parts 取年份（契约 date 用 4 位年份）
    parts = (csl.get("issued") or {}).get("date-parts") or []
    if parts and parts[0] and parts[0][0]:
        meta["date"] = str(parts[0][0])

    # 直传字段（含额外 CSL 变量，契约 §三允许原样直传）；
    # 标题/期刊名可能带 <sub>/<sup>（如 "Y<sub>3</sub>"），统一归一为 LaTeX
    for key in ("title", "container-title", "volume", "issue", "page",
                "type", "URL", "ISSN", "publisher"):
        val = csl.get(key)
        if val:
            meta[key] = _normalize_inline(str(val))

    # 摘要（Zotero 有则以它为准；剥离可能的 HTML/JATS 标签）
    abstract = csl.get("abstract") or ""
    if abstract:
        abstract = re.sub(r"<[^>]+>", "", abstract)
        abstract = re.sub(r"\s+", " ", abstract).strip()
        if abstract:
            meta["abstract"] = abstract

    # DOI（CSL: DOI → frontmatter doi）
    if csl.get("DOI"):
        meta["doi"] = str(csl["DOI"])

    # citekey：Better BibTeX 导出的 citation-key，或 BBT 风格的 id；
    # 官方导出 id 是 "userID/itemKey" 或 zotero.org URL，都不是 citekey，忽略
    citekey = csl.get("citation-key") or ""
    if not citekey:
        cid = str(csl.get("id") or "")
        if cid and not cid.startswith(("http://", "https://")) \
                and not re.match(r"^\d+/\w+$", cid):
            citekey = cid
    if citekey:
        meta["citekey"] = citekey

    return meta
