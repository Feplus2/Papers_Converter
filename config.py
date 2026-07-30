"""Papers_Converter 配置文件。

从 .env 文件或环境变量读取配置。
"""

import os
from pathlib import Path


def _load_dotenv():
    """从项目根目录的 .env 文件加载环境变量（不覆盖已有的）"""
    env_path = Path(__file__).parent / ".env"
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
# MinerU 云解析（PDF → 结构化产物）
# ============================================================
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

# ============================================================
# 路径
# ============================================================
PROJECT_DIR = Path(__file__).parent
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
