# 辅助模型结构判定通道（默认关闭，config.STRUCTURE_LLM）。
#
# 规则到达极限后的升级路径，spec 见 docs/structure-detection.md 第四节。
# 当前落地：封面判定的 LLM 仲裁（纯文本、禁思考、低 max_tokens）。
# 冲突裁决保守方向优先——宁可保留疑似噪声页，也绝不误丢内容：
#   - LLM 判"非封面" → 可撤销规则的"是封面"判定；
#   - LLM 判"是封面" → 仅当规则也判封面、或置信 high 时才生效（且只限 page 0）；
#   - 任何失败/解析异常 → 原样保留规则结果。

import json
import logging
import re

import config
import llm_thinking

logger = logging.getLogger(__name__)

# 页面摘要排除的噪声块类型
_NOISE_BLOCK_TYPES = {"header", "footer", "page_number", "aside_text"}
# 摘要的页数与字符预算（简单判断任务，不送多）
_DIGEST_MAX_PAGES = 2
_DIGEST_MAX_CHARS = 3500
# 单块文本截断长度（保留块首即可判别页面角色）
_BLOCK_TRUNCATE = 200


def page_digest(content_list: list[dict], max_pages: int = _DIGEST_MAX_PAGES,
                max_chars: int = _DIGEST_MAX_CHARS) -> str:
    """把前 max_pages 页压缩成带块类型标注的摘要文本（供 LLM 判断页面角色）。

    块类型标注（[text]/[image]/[equation]/[table]）是关键信号——真首页通常
    含 image/equation 与长段落，仓库封面全是短模板行。
    """
    lines = []
    budget = max_chars
    for pidx in range(max_pages):
        page = [b for b in content_list if b.get("page_idx", 0) == pidx]
        if not page:
            continue
        lines.append(f"=== page {pidx} ===")
        budget -= 14
        for b in page:
            btype = b.get("type", "text")
            if btype in _NOISE_BLOCK_TYPES:
                continue
            text = re.sub(r"\s+", " ", (b.get("text") or "")).strip()
            if btype in ("image", "equation", "table"):
                text = text[:80] or "(no extracted text)"
            else:
                text = text[:_BLOCK_TRUNCATE]
            line = f"[{btype}] {text}"
            if budget - len(line) < 0:
                lines.append("...(truncated)")
                return "\n".join(lines)
            lines.append(line)
            budget -= len(line) + 1
    return "\n".join(lines)


def arbitrate(rule_cover: set[int], answer: dict) -> set[int]:
    """纯裁决函数（与网络调用分离，便于单测）。

    Args:
        rule_cover: 规则判定结果（只可能含 0）
        answer: LLM 回答 {"is_cover": bool, "confidence": "high|medium|low", ...}
    """
    is_cover = bool(answer.get("is_cover"))
    confidence = str(answer.get("confidence", "")).lower()
    if is_cover:
        # 判"是封面"：规则已判则维持；规则未判需 high 置信才采纳（且只限 page 0）
        if 0 in rule_cover or confidence == "high":
            return {0}
        return set()
    # 判"非封面"：撤销规则判定（保守方向——宁可保留疑似噪声页）
    return set()


_PROMPT = """你是学术论文结构分析专家。判断这份文档的 page 0 是否为"仓库/机构封面页"——
即机构知识库（如大学仓库）在论文前加的引用声明页，特征：Citation (APA)、Document
Version、Important note、Takedown policy、Copyright 等模板行，全部是短模板文本，
没有正文段落、图片、公式、表格。

注意区分两点：
1. 仓库封面页通常也会列出论文标题/作者/DOI，所以"页内出现标题"不足以判为正文页；
   关键看页面由短模板行组成，还是由正文段落/图/公式组成。
2. 论文自己的首页（标题+作者+摘要，常有图和公式）不是封面页。

返回严格 JSON（不要 markdown 代码块、不要解释）：
{"is_cover": true 或 false, "confidence": "high/medium/low", "reason": "一句话依据"}

文档前部内容：
---
%s
---"""


def llm_cover_review(content_list: list[dict], rule_cover: set[int]) -> set[int]:
    """LLM 仲裁封面判定。任何失败原样返回规则结果，绝不阻断转换。"""
    if not config.DEEPSEEK_API_KEY:
        logger.info("  结构判定 LLM: 未配置 DEEPSEEK_API_KEY，沿用规则结果")
        return rule_cover
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("  openai 未安装，跳过 LLM 结构判定")
        return rule_cover

    digest = page_digest(content_list)
    try:
        client = OpenAI(api_key=config.DEEPSEEK_API_KEY,
                        base_url=config.DEEPSEEK_BASE_URL)
        resp = llm_thinking.chat_create(
            client,
            model=config.DEEPSEEK_MODEL,
            messages=[
                {"role": "system",
                 "content": "你是学术论文结构分析专家。只返回 JSON，不要 markdown 代码块。"},
                {"role": "user", "content": _PROMPT % digest},
            ],
            temperature=0.0,
            max_tokens=200,
        )
        content = (resp.choices[0].message.content or "").strip()
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        answer = json.loads(content)
    except Exception as e:
        logger.warning(f"  结构判定 LLM 调用失败（沿用规则结果）: {e}")
        return rule_cover

    final = arbitrate(rule_cover, answer)
    if final != rule_cover:
        logger.info(
            f"  结构判定 LLM 仲裁: 规则={sorted(rule_cover)} → 最终={sorted(final)}"
            f"（is_cover={answer.get('is_cover')}，"
            f"confidence={answer.get('confidence')}，{answer.get('reason', '')}）")
    else:
        logger.info("  结构判定 LLM 仲裁: 与规则一致，无变更")
    return final
