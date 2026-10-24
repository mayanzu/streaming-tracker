"""Google 翻译接口（gtx）解析与兜底回归。

运行：python -m pytest tests/test_translate_gtx.py -q
要求：不访问真实网络。
"""

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "DATABASE_URL", os.path.join(tempfile.mkdtemp(prefix="streaming-test-i18n-"), "bootstrap.db")
)

from app.fetcher import translate  # noqa: E402


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_gtx_parses_segments(monkeypatch):
    seen = {}

    def fake_get(url, params=None, timeout=None, headers=None):
        seen["url"] = url
        seen["q"] = (params or {}).get("q")
        return _FakeResponse([[["四个男孩的故事", "A story of four boys", None, None]], None, "en"])

    monkeypatch.setattr(translate.httpx, "get", fake_get)
    assert translate._google_gtx_translate("A story of four boys") == "四个男孩的故事"
    assert seen["url"] == translate._GOOGLE_ENDPOINT
    assert seen["q"] == "A story of four boys"


def test_gtx_empty_payload(monkeypatch):
    monkeypatch.setattr(translate.httpx, "get", lambda *a, **k: _FakeResponse(None))
    assert translate._google_gtx_translate("x") == ""


def test_translate_sync_returns_original_when_all_sources_fail(monkeypatch):
    monkeypatch.setattr(translate, "TRANSLATE_PROVIDER", "google")

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(translate.httpx, "get", boom)

    fake_module = types.ModuleType("deep_translator")

    class GoogleTranslator:
        def __init__(self, **kwargs):
            pass

        def translate(self, text):
            raise RuntimeError("no network")

    fake_module.GoogleTranslator = GoogleTranslator
    monkeypatch.setitem(sys.modules, "deep_translator", fake_module)

    assert translate.translate_sync("hello world") == "hello world"
