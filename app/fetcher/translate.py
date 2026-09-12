"""翻译源：google | deepl | none（由 TRANSLATE_PROVIDER 配置切换）。

- google：deep_translator（国内环境可能不可用，失败返回原文）；
- deepl：DeepL REST API（需要 DEEPL_API_KEY）；
- none：不翻译，返回原文，前端据 zh_quality 显示原文标签。
"""

import logging
import re

import httpx

from app.config import DEEPL_API_KEY, DEEPL_API_URL, TRANSLATE_PROVIDER

logger = logging.getLogger(__name__)

_TRANSLATION_ERROR_PATTERN = re.compile(
    r"<html|<!doctype|error\s+5\d\d|server error|that['’]s an error"
    r"|please try again later|we['’]re sorry|unusual traffic",
    re.IGNORECASE,
)


def provider_enabled() -> bool:
    if TRANSLATE_PROVIDER == "none":
        return False
    if TRANSLATE_PROVIDER == "deepl":
        return bool(DEEPL_API_KEY)
    return True


def translate_sync(text: str) -> str:
    """同步翻译（调用方通常放进线程池）；失败返回原文。"""
    value = str(text or "").strip()
    if not value or TRANSLATE_PROVIDER == "none":
        return value

    if TRANSLATE_PROVIDER == "deepl":
        if not DEEPL_API_KEY:
            logger.warning("TRANSLATE_PROVIDER=deepl but DEEPL_API_KEY is empty; returning original text")
            return value
        try:
            response = httpx.post(
                DEEPL_API_URL,
                data={"text": value[:800], "target_lang": "ZH"},
                headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
                timeout=8.0,
            )
            response.raise_for_status()
            translations = response.json().get("translations") or []
            result = str((translations[0] or {}).get("text") or "").strip()
            if result:
                return result
        except Exception as exc:
            logger.warning("DeepL translation failed: %s", exc)
        return value

    # 默认 google
    try:
        from deep_translator import GoogleTranslator

        result = GoogleTranslator(source="auto", target="zh-CN").translate(value[:800])
        if not result:
            return value
        if _TRANSLATION_ERROR_PATTERN.search(result):
            raise ValueError("translation provider returned an error page")
        return result
    except Exception as exc:
        logger.debug("Google translation failed: %s", exc)
        return value
