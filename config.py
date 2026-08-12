"""Papers_Converter 配置文件。

从 .env 文件或环境变量读取配置。
"""

import os
import sys
from pathlib import Path

# PyInstaller frozen 时 __file__ 指向 _MEIPASS 临时解压目录（进程退出即删），
# 路径锚点必须改为 exe 所在目录，否则默认输出目录会落进临时目录并随之消失
_ANCHOR_DIR = (Path(sys.executable).parent if getattr(sys, "frozen", False)
               else Path(__file__).parent)


def _load_dotenv():
    """从项目根目录（frozen 时为 exe 目录）的 .env 文件加载环境变量（不覆盖已有的）"""
    env_path = _ANCHOR_DIR / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = True) -> bool:
    val = os.environ.get(key, str(default).lower())
    return val.lower() in ("true", "1", "yes")


# ============================================================
# API 密钥
# ============================================================
DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = _env("DEEPSEEK_MODEL", "deepseek-chat")

# ============================================================
# Stage 1 解析引擎（PDF → 结构化产物，契约见 ocr_provider.py）
# ============================================================
OCR_PROVIDER = _env("OCR_PROVIDER", "paddleocr")  # 可选值见 ocr_provider.provider_names()

# MinerU 云解析
MINERU_TOKEN = _env("MINERU_TOKEN")
MINERU_MODEL = _env("MINERU_MODEL", "vlm")
MINERU_LANGUAGE = _env("MINERU_LANGUAGE", "")   # 空=自动；可填 ch/en 等
MINERU_TIMEOUT = int(_env("MINERU_TIMEOUT", "900"))
MINERU_ENABLE_FORMULA = _env_bool("MINERU_ENABLE_FORMULA", True)
MINERU_ENABLE_TABLE = _env_bool("MINERU_ENABLE_TABLE", True)
# 超大 PDF 分片页数（免费 API 建议值）
MINERU_CHUNK_SIZE = int(_env("MINERU_CHUNK_SIZE", "200"))
# 论文页数上限：论文几乎不可能超过 200 页，超过即判定为书籍，拒收并提示改走图书馆导入
MAX_PAPER_PAGES = int(_env("MAX_PAPER_PAGES", "200"))

# 图组并集重裁（figure_merger）：布局检测把一张 Figure 拆碎时，同词干同页块 bbox 并集整幅重裁
FIGURE_MERGE = _env_bool("FIGURE_MERGE", True)

# 辅助模型结构判定通道（封面判定 LLM 仲裁等，见 docs/structure-detection.md 第四节）。
# 默认关闭：规则判据经 127 篇语料 AB 验证零误杀，到达极限后再开启
STRUCTURE_LLM = _env_bool("STRUCTURE_LLM", False)

# 脏 PDF 文章边界切分（article_boundary）：权威标题锚点头切 + References 后尾切。
# 只吃锚点强信号（详见模块 docstring 与 docs/structure-detection.md 第五节）
ARTICLE_BOUNDARY = _env_bool("ARTICLE_BOUNDARY", True)

# GLM-OCR（智谱 layout_parsing API）
GLM_OCR_API_KEY = _env("GLM_OCR_API_KEY")
GLM_OCR_BASE_URL = _env("GLM_OCR_BASE_URL", "https://open.bigmodel.cn")
GLM_OCR_TIMEOUT = int(_env("GLM_OCR_TIMEOUT", "600"))
# 单次请求页数上限（API 限制 100 页，留余量）
GLM_OCR_CHUNK_SIZE = int(_env("GLM_OCR_CHUNK_SIZE", "100"))

# PaddleOCR-VL（百度 AI Studio 异步 job API）
PADDLEOCR_API_URL = _env("PADDLEOCR_API_URL",
                         "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs")
PADDLEOCR_TOKEN = _env("PADDLEOCR_TOKEN")
PADDLEOCR_MODEL = _env("PADDLEOCR_MODEL", "PaddleOCR-VL-1.6")
PADDLEOCR_TIMEOUT = int(_env("PADDLEOCR_TIMEOUT", "900"))

# ============================================================
# 路径
# ============================================================
PROJECT_DIR = _ANCHOR_DIR
DEFAULT_OUTPUT_DIR = Path(_env("DEFAULT_OUTPUT_DIR", str(PROJECT_DIR / "output")))
PARSED_DIR = Path(_env("PARSED_DIR", r"F:\MyProjects\zotero-brain\parsed"))

# ============================================================
# Zotero（可选）：CSL-JSON 权威元数据
# ============================================================
# 凭据仅供 export_zotero_csl.py 导出使用；转换运行时只读本地导出文件
ZOTERO_API_KEY = _env("ZOTERO_API_KEY")
ZOTERO_USER_ID = _env("ZOTERO_USER_ID")
ZOTERO_LIBRARY_TYPE = _env("ZOTERO_LIBRARY_TYPE", "user")
# CSL-JSON 导出文件：{zotero_key: csl_item}
ZOTERO_CSL_JSON = _env("ZOTERO_CSL_JSON", str(PROJECT_DIR / "data" / "zotero_csl.json"))

# ============================================================
# 处理参数
# ============================================================
# 元数据提取时送给 LLM 的最大前导文本字符数
METADATA_MAX_CHARS = int(_env("METADATA_MAX_CHARS", "6000"))
# 批量处理时的并发数（预留）
BATCH_CONCURRENCY = int(_env("BATCH_CONCURRENCY", "1"))
