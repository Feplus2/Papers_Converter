# -*- coding: utf-8 -*-
"""MathML（presentation）→ LaTeX 转换器（XML 管线公式通道）。

设计口径（立项五问之三的回答，见 docs/zotero-brain-xml-pipeline-plan.md）：
- 自研规则覆盖期刊全文常见 presentation MathML 子集（mrow/mi/mn/mo/mtext/mfrac/
  msqrt/mroot/msup/msub/msubsup/munder/mover/munderover/mtable/mfenced/mstyle/
  mspace/mpadded/mphantom），KaTeX 可渲染为验收线；
- 未知元素降级为其文本内容拼接（信息保底不伪造结构）；
- 现成库（mathml2latex 等）年久失修且引依赖，exe 打包不友好——与「能用规则
  绝不用模型/外部依赖」同构。
- JATS 侧 tex-math 优先于 MathML（原文 LaTeX 直接可用），本模块只兜没有
  tex-math 的场景（Elsevier ce: 变体 / 部分 PMC 存档）。
"""

import re
import xml.etree.ElementTree as ET

# 常用 mo 运算符/符号 → TeX 命令（KaTeX 全支持）
_OP_MAP = {
    "−": "-",  # U+2212 数学负号：KaTeX 不收 Unicode 负号，统一转 ASCII
    "×": r"\times ", "⋅": r"\cdot ", "·": r"\cdot ", "∗": r"\ast ",
    "≤": r"\le ", "≥": r"\ge ", "≠": r"\ne ", "≈": r"\approx ", "≡": r"\equiv ",
    "±": r"\pm ", "∓": r"\mp ", "∝": r"\propto ", "∞": r"\infty ",
    "→": r"\to ", "←": r"\leftarrow ", "↔": r"\leftrightarrow ",
    "⇒": r"\Rightarrow ", "⇐": r"\Leftarrow ", "⇔": r"\Leftrightarrow ",
    "∂": r"\partial ", "∇": r"\nabla ", "∑": r"\sum ", "∏": r"\prod ",
    "∫": r"\int ", "∮": r"\oint ", "√": r"\sqrt ", "°": r"^{\circ}",
    "…": r"\ldots ", "⋯": r"\cdots ", "⋮": r"\vdots ", "⋱": r"\ddots ",
    "∈": r"\in ", "∉": r"\notin ", "⊂": r"\subset ", "⊆": r"\subseteq ",
    "∪": r"\cup ", "∩": r"\cap ", "∅": r"\emptyset ", "∀": r"\forall ",
    "∃": r"\exists ", "λ": r"\lambda ", "μ": r"\mu ", "π": r"\pi ",
    "α": r"\alpha ", "β": r"\beta ", "γ": r"\gamma ", "δ": r"\delta ",
    "ε": r"\varepsilon ", "θ": r"\theta ", "σ": r"\sigma ", "τ": r"\tau ",
    "φ": r"\varphi ", "ω": r"\omega ", "Δ": r"\Delta ", "Ω": r"\Omega ",
    "Σ": r"\Sigma ", "Γ": r"\Gamma ", "Λ": r"\Lambda ", "Θ": r"\Theta ",
    "ℝ": r"\mathbb{R}", "ℕ": r"\mathbb{N}", "ℤ": r"\mathbb{Z}",
    "ℂ": r"\mathbb{C}", "ℏ": r"\hbar ", "Å": r"\text{\AA}", "‰": r"\perthousand ",
}

# mspace width 值 → 间距命令（常见量级映射，未知宽度给细空格）
_SPACE_MAP = {
    "veryverythinmathspace": r"\,", "verythinmathspace": r"\,",
    "thinmathspace": r"\,", "mediummathspace": r"\:",
    "thickmathspace": r"\;", "verythickmathspace": r"\;", 
    "veryverythickmathspace": r"\;", "1em": r"\quad ", "2em": r"\qquad ",
}

_ACCENT_MAP = {"¯": r"\bar", "‾": r"\bar", "→": r"\vec", "˙": r"\dot",
               "¨": r"\ddot", "∼": r"\tilde", "~": r"\tilde", "^": r"\hat"}


def _text_of(el: ET.Element) -> str:
    return (el.text or "").strip()


def _children_latex(el: ET.Element, ctx: dict) -> str:
    """按顺序转换全部子节点（含 text tail）。"""
    parts = []
    if el.text:
        parts.append(_sanitize_text(el.text))
    for child in el:
        parts.append(_convert(child, ctx))
        if child.tail:
            parts.append(_sanitize_text(child.tail))
    return "".join(parts)


def _sanitize_text(s: str) -> str:
    """XML 文本区直传（去空白收敛；数字/字母/操作符原样）。"""
    return s


def _needs_braces(expr: str) -> bool:
    """上/下标参数是否需要 {} 包裹：单字符裸写，其余包裹。"""
    expr = expr.strip()
    return not (len(expr) == 1 and (expr.isalnum() or expr in "\\,"))


def _script(base: str, script: str, symbol: str) -> str:
    b = base.strip()
    if _needs_braces(script):
        return f"{b}{symbol}{{{script.strip()}}}"
    return f"{b}{symbol}{script.strip()}"


def _convert(el: ET.Element, ctx: dict) -> str:
    tag = el.tag.split("}")[-1] if isinstance(el.tag, str) else ""
    depth = ctx.get("depth", 0)
    ctx["depth"] = depth + 1

    try:
        if depth > 32:
            return _plain_text(el)  # 防御性深度闸（病态嵌套）

        if tag == "math" or tag == "semantics":
            # semantics：取首个 annotation-xml/annotation 或 presentation 分支
            if tag == "semantics":
                for child in el:
                    ct = child.tag.split("}")[-1]
                    if ct not in ("annotation", "annotation-xml"):
                        return _convert(child, ctx)
                return _plain_text(el)
            return _children_latex(el, ctx)

        if tag == "mrow" or tag == "mstyle" or tag == "mpadded" or tag == "merror":
            return _children_latex(el, ctx)

        if tag == "mi":
            t = _text_of(el)
            if not t:
                return ""
            if len(t) > 1 and t.isalpha() and not t.startswith("\\"):
                return r"\mathrm{%s}" % t
            return t

        if tag == "mn":
            return _text_of(el)

        if tag == "mo":
            t = _text_of(el)
            if not t:
                return ""
            return _OP_MAP.get(t, t) + " " if t in _OP_MAP else t

        if tag == "mtext" or tag == "ms":
            t = _text_of(el)
            return r"\text{%s}" % t if t else ""

        if tag == "mspace":
            w = (el.get("width") or "").strip()
            return _SPACE_MAP.get(w, r"\,")

        if tag == "mfrac":
            kids = [c for c in el if isinstance(c.tag, str)]
            num = _convert(kids[0], ctx) if kids else ""
            den = _convert(kids[1], ctx) if len(kids) > 1 else ""
            linthick = el.get("linethickness") or ""
            if linthick in ("0", "0px", "0em"):  # 二项式系数形态
                return r"\binom{%s}{%s}" % (num.strip(), den.strip())
            return r"\frac{%s}{%s}" % (num.strip(), den.strip())

        if tag == "msqrt":
            inner = _children_latex(el, ctx).strip()
            return r"\sqrt{%s}" % inner

        if tag == "mroot":
            kids = [c for c in el if isinstance(c.tag, str)]
            base = _convert(kids[0], ctx).strip() if kids else ""
            idx = _convert(kids[1], ctx).strip() if len(kids) > 1 else "3"
            return r"\sqrt[%s]{%s}" % (idx, base)

        if tag in ("msup", "msub", "msubsup"):
            kids = [c for c in el if isinstance(c.tag, str)]
            base = _convert(kids[0], ctx) if kids else ""
            if tag == "msup":
                script = _convert(kids[1], ctx) if len(kids) > 1 else ""
                return _script(base, script, "^")
            if tag == "msub":
                script = _convert(kids[1], ctx) if len(kids) > 1 else ""
                return _script(base, script, "_")
            sub = _convert(kids[1], ctx) if len(kids) > 1 else ""
            sup = _convert(kids[2], ctx) if len(kids) > 2 else ""
            out = _script(base, sub, "_")
            return _script(out, sup, "^")

        if tag == "munder":
            kids = [c for c in el if isinstance(c.tag, str)]
            base = _convert(kids[0], ctx) if kids else ""
            under = _convert(kids[1], ctx) if len(kids) > 1 else ""
            under_s = under.strip()
            # 下划重音形态（x̲）罕见，主流量纲符号走 underset
            return r"\underset{%s}{%s}" % (under_s, base.strip())

        if tag == "mover":
            kids = [c for c in el if isinstance(c.tag, str)]
            base = _convert(kids[0], ctx) if kids else ""
            over = _convert(kids[1], ctx) if len(kids) > 1 else ""
            over_s = over.strip()
            if el.get("accent") == "true" and len(over_s) <= 2:
                cmd = _ACCENT_MAP.get(over_s)
                if cmd:
                    return r"%s{%s}" % (cmd, base.strip())
            return r"\overset{%s}{%s}" % (over_s, base.strip())

        if tag == "munderover":
            kids = [c for c in el if isinstance(c.tag, str)]
            base = _convert(kids[0], ctx) if kids else ""
            under = _convert(kids[1], ctx) if len(kids) > 1 else ""
            over = _convert(kids[2], ctx) if len(kids) > 2 else ""
            return r"\underset{%s}{\overset{%s}{%s}}" % (
                under.strip(), over.strip(), base.strip())

        if tag == "mfenced":
            inner = _children_latex(el, ctx).strip()
            open_f = el.get("open") or "("
            close_f = el.get("close") or ")"
            return r"\left%s %s \right%s" % (open_f, inner, close_f)

        if tag == "mphantom":
            inner = _children_latex(el, ctx).strip()
            return r"\phantom{%s}" % inner

        if tag == "mtable":
            rows = []
            for mtr in el:
                if mtr.tag.split("}")[-1] not in ("mtr", "mlabeledtr"):
                    continue
                cells = [_convert(c, ctx).strip()
                         for c in mtr if c.tag.split("}")[-1] == "mtd"]
                rows.append(" & ".join(cells))
            return r"\begin{matrix}%s\end{matrix}" % r" \\ ".join(rows)

        if tag in ("mtr", "mlabeledtr", "mtd"):
            return _children_latex(el, ctx)

        if tag in ("annotation", "annotation-xml"):
            return ""  # 语义注记不进 LaTeX

        if tag == "maction":
            kids = [c for c in el if isinstance(c.tag, str)]
            return _convert(kids[0], ctx) if kids else ""

        # 未知元素：降级取文本内容（信息保底）
        return _plain_text(el)
    finally:
        ctx["depth"] = depth


def _plain_text(el: ET.Element) -> str:
    parts = [el.text or ""]
    for child in el:
        parts.append(_plain_text(child))
        parts.append(child.tail or "")
    return "".join(parts).strip()


def mathml_to_latex(el: ET.Element) -> str:
    """MathML 元素 → LaTeX 源码（不含 $ 定界符）。"""
    out = _convert(el, {"depth": 0})
    # 收敛多余空白（保留命令尾部空格语义）
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def mathml_str_to_latex(xml_fragment: str) -> str:
    """MathML 字符串片段（如 <math>...</math>）→ LaTeX；解析失败返回原文。"""
    try:
        root = ET.fromstring(xml_fragment)
        return mathml_to_latex(root)
    except ET.ParseError:
        return xml_fragment.strip()
