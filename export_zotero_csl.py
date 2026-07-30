#!/usr/bin/env python3
"""从 Zotero Web API 导出全库 CSL-JSON 元数据 → 本地 {key: csl_item} 文件。

产物供 zotero_meta.py 在转换时按 Zotero key 查权威元数据
（author/container-title/date/citekey 以 Zotero 为准，LLM 只补 abstract）。

凭据：ZOTERO_API_KEY / ZOTERO_USER_ID / ZOTERO_LIBRARY_TYPE（user|group），
默认读项目 .env；--env-file 可指定其他 env 文件（如 zotero-brain 的 .env）。

用法：
    python export_zotero_csl.py                          # 导出全库
    python export_zotero_csl.py --env-file ..\\zotero-brain\\.env
    python export_zotero_csl.py --keys 26NNZJHX 2TTNVYWG # 只导出指定 key
    python export_zotero_csl.py --limit 5                # 调试用
"""

import argparse
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config

logger = logging.getLogger("export_zotero_csl")

_API_BASE = "https://api.zotero.org"
_PAGE_SIZE = 100
_SKIP_TYPES = {"attachment", "note", "annotation"}
# Better BibTeX 会把 citation key 写进 extra 字段
_BBT_EXTRA_RE = re.compile(r"^Citation Key:\s*(\S+)\s*$", re.M | re.I)


def _load_env_file(path: Path) -> None:
    """额外加载一个 env 文件（不覆盖已有环境变量）"""
    import os
    if not path.exists():
        logger.warning(f"env 文件不存在: {path}")
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _api_get(path: str, api_key: str, params: dict | None = None,
             retries: int = 4) -> tuple[bytes, dict]:
    """GET Zotero API，返回 (body, headers)；429/5xx 指数退避重试"""
    url = f"{_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    delay = 2.0
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "Zotero-API-Version": "3",
            "Zotero-API-Key": api_key,
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                wait = float(e.headers.get("Retry-After", delay)) if e.headers else delay
                logger.warning(f"  HTTP {e.code}，{wait:.0f}s 后重试 ({attempt + 1}/{retries})")
                time.sleep(wait)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"请求多次失败: {url}")


def _library_prefix(library_type: str, library_id: str) -> str:
    return f"/groups/{library_id}" if library_type == "group" else f"/users/{library_id}"


def list_paper_items(prefix: str, api_key: str, limit: int | None = None) -> list[dict]:
    """翻页拉取全部非附件/笔记条目，返回 [{key, extra}]"""
    items = []
    start = 0
    while True:
        body, headers = _api_get(f"{prefix}/items", api_key,
                                 {"limit": _PAGE_SIZE, "start": start, "format": "json"})
        batch = json.loads(body.decode("utf-8"))
        for it in batch:
            data = it.get("data", {})
            if data.get("itemType") in _SKIP_TYPES:
                continue
            items.append({"key": it.get("key", ""), "extra": data.get("extra", "")})
        total = int(headers.get("Total-Results", 0))
        start += len(batch)
        logger.info(f"  条目列表: {start}/{total}")
        if start >= total or not batch:
            break
        if limit and len(items) >= limit:
            break
        time.sleep(0.2)
    if limit:
        items = items[:limit]
    return items


def fetch_csl_item(prefix: str, api_key: str, key: str) -> dict | None:
    """拉取单条目的 CSL-JSON（{"items": [...]} 形态，取第一项）"""
    try:
        body, _ = _api_get(f"{prefix}/items/{key}", api_key, {"format": "csljson"})
    except Exception as e:
        logger.warning(f"  {key}: CSL-JSON 拉取失败: {e}")
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        logger.warning(f"  {key}: CSL-JSON 响应不是 JSON")
        return None
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"][0] if data["items"] else None
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict) and data:
        return data
    return None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description="导出 Zotero 全库 CSL-JSON 元数据")
    parser.add_argument("--env-file", default=None,
                        help="额外 env 文件（如 zotero-brain 的 .env），在项目 .env 之后加载")
    parser.add_argument("--output", default=None,
                        help=f"输出路径（默认: {config.ZOTERO_CSL_JSON}）")
    parser.add_argument("--keys", nargs="*", default=None,
                        help="只导出指定 Zotero key")
    parser.add_argument("--limit", type=int, default=None, help="最多导出 N 条（调试）")
    args = parser.parse_args()

    if args.env_file:
        _load_env_file(Path(args.env_file))
    # env-file 后加载，重新读环境变量
    import os
    api_key = os.environ.get("ZOTERO_API_KEY", config.ZOTERO_API_KEY)
    user_id = os.environ.get("ZOTERO_USER_ID", config.ZOTERO_USER_ID)
    library_type = os.environ.get("ZOTERO_LIBRARY_TYPE", config.ZOTERO_LIBRARY_TYPE)
    if not api_key or not user_id:
        raise SystemExit("缺少 ZOTERO_API_KEY / ZOTERO_USER_ID（.env 或 --env-file 提供）")

    prefix = _library_prefix(library_type, user_id)
    output = Path(args.output) if args.output else Path(config.ZOTERO_CSL_JSON)

    # 1. 条目列表（含 BBT citation key 的 extra 字段）
    if args.keys:
        items = [{"key": k, "extra": ""} for k in args.keys]
    else:
        logger.info("拉取条目列表...")
        items = list_paper_items(prefix, api_key, limit=args.limit)
    logger.info(f"待导出: {len(items)} 条")

    # 2. 逐条拉 CSL-JSON
    store = {}
    for i, it in enumerate(items, 1):
        key = it["key"]
        csl = fetch_csl_item(prefix, api_key, key)
        if csl:
            # BBT citation key：extra 里的权威值优先（csljson 的 id 可能是 URL）
            m = _BBT_EXTRA_RE.search(it.get("extra") or "")
            if m and not csl.get("citation-key"):
                csl["citation-key"] = m.group(1)
            store[key] = csl
        if i % 20 == 0 or i == len(items):
            logger.info(f"  CSL-JSON: {i}/{len(items)}（成功 {len(store)}）")
        time.sleep(0.15)

    # 3. 落盘
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="\n") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    logger.info(f"导出完成: {len(store)} 条 → {output}")


if __name__ == "__main__":
    main()
