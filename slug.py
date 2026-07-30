"""Slug 生成工具（通用，不依赖任何外部 key）。

规则（对齐 Better BibTeX 风格 author2024keyword）：
  - 第一作者姓氏 + 年份 + 标题首词，全小写无分隔符 → zhao2020rational
  - 中文等非拉丁文本走拼音转写（pypinyin）→ shi2023na
  - 极端退化（无标题/作者/年份）时用标题短哈希兜底，保证唯一
"""

import hashlib
import re
import unicodedata


def _transliterate(text: str) -> str:
    """将拉丁系 Unicode 文本近似转为 ASCII（去掉重音符号等）。"""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _pinyin(text: str) -> str:
    """将文本中的汉字转为拼音连写；非汉字字符原样保留。

    pypinyin 缺失时退化为原文（后续 _clean_word 会清掉非 ASCII）。
    """
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return text
    return "".join(lazy_pinyin(text))


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _clean_word(word: str) -> str:
    """只保留小写字母数字。"""
    return re.sub(r"[^a-z0-9]", "", word.lower())


def _latinize(text: str) -> str:
    """统一转写：含 CJK 走拼音，否则去重音符号。"""
    if _has_cjk(text):
        return _pinyin(text)
    return _transliterate(text)


def _extract_surname(authors: list) -> str:
    """提取第一作者姓氏并转写为 ASCII。

    中文姓名取首字（汉字姓氏通常 1 字）转拼音；
    拉丁姓名取逗号前或最后一个词。
    """
    if not authors:
        return ""
    first = authors[0]
    name = (first.get("name", "") if isinstance(first, dict) else str(first)).strip()
    if not name:
        return ""

    if _has_cjk(name):
        m = re.search(r"[\u4e00-\u9fff]", name)
        return _clean_word(_pinyin(m.group(0))) if m else ""

    if "," in name:
        surname = name.split(",")[0].strip()
    else:
        parts = name.split()
        surname = parts[-1] if parts else ""
    return _clean_word(_transliterate(surname))


def _extract_first_word(title: str) -> str:
    """提取标题首个有意义词并转写为 ASCII。

    中文标题取第一个汉字（1–2 字）转拼音；
    拉丁标题跳过冠词取第一个实词。
    """
    if not title:
        return ""

    if _has_cjk(title):
        m = re.search(r"[\u4e00-\u9fff]{1,2}", title)
        return _clean_word(_pinyin(m.group(0))) if m else ""

    words = re.findall(r"[a-zA-Z]+", _transliterate(title))
    skip = {"a", "an", "the", "on", "in", "of", "for", "to", "and", "or"}
    for w in words:
        if w.lower() not in skip:
            return w.lower()
    return words[0].lower() if words else ""


def generate_slug(metadata: dict) -> str:
    """从论文真实元数据生成 slug（通用，任何语言，不依赖 Zotero key）。

    契约 §二：citekey 优先（Better BibTeX 风格 author2024keyword），
    否则用 姓氏+年份+标题首词。

    Args:
        metadata: 含 title, author, date 等字段的 dict；有 citekey 时优先使用

    Returns:
        slug 字符串，如 "zhao2020rational" / "shi2023na"
    """
    # citekey 优先（Zotero/Better BibTeX 权威标识）；清理文件系统不安全字符
    citekey = (metadata.get("citekey") or "").strip()
    if citekey:
        slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", citekey).strip()
        if slug:
            return slug

    title = metadata.get("title", "") or ""
    surname = _extract_surname(metadata.get("author", []))

    date_str = str(metadata.get("date", ""))
    year_match = re.search(r"(\d{4})", date_str)
    year = year_match.group(1) if year_match else ""

    first_word = _extract_first_word(title)

    parts = [p for p in [surname, year, first_word] if p]
    slug = "".join(parts)
    slug = re.sub(r"[^a-z0-9-]", "", slug.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")

    # 退化兜底：slug 为空/纯年份/过短时，用标题短哈希保证唯一
    # （仍源于论文数据，不依赖任何外部 key）
    if (not slug) or slug.isdigit() or len(slug) < 4:
        h = hashlib.md5(title.encode("utf-8")).hexdigest()[:6]
        base = slug if (slug and not slug.isdigit()) else "paper"
        slug = f"{base}{h}"

    return slug or "untitled"
