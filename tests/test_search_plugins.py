"""插件发现、启用和请求失败隔离的离线回归测试。"""

import importlib
import types
import unittest
from unittest.mock import patch

import httpx

from search_engine.base import BaseEngine, TorrentResult
from search_engine.manager import EngineManager
from search_engine.registry import ENGINE_PLUGINS, EnginePlugin, discover_plugins
from search_engine.search_template import TemplateEngine
from urllib.parse import quote


class FixtureEngine(BaseEngine):
    name = "fixture"
    display_name = "测试插件"

    async def search(self, query: str, client: httpx.AsyncClient) -> list[TorrentResult]:
        return []


class PluginDiscoveryTests(unittest.TestCase):
    def discover(self, modules):
        inventory = [types.SimpleNamespace(name="search_" + name) for name in modules]

        def import_module(name):
            module = modules[name.rsplit(".", 1)[-1].removeprefix("search_")]
            if isinstance(module, Exception):
                raise module
            return module

        with patch("search_engine.registry.pkgutil.iter_modules", return_value=inventory), \
                patch("search_engine.registry.importlib.import_module", side_effect=import_module):
            return discover_plugins()

    def test_builtin_plugins_and_generic_helper(self):
        self.assertEqual(set(ENGINE_PLUGINS), {"mikan", "bitsearch", "solidtorrents", "thepiratebay"})
        self.assertTrue(all(plugin.default_enabled for plugin in ENGINE_PLUGINS.values()))
        for name in ENGINE_PLUGINS:
            module = importlib.import_module("search_engine.search_" + name)
            self.assertIs(module.ENGINE_CLASS, ENGINE_PLUGINS[name].engine_class)

    def test_new_plugin_discovered_disabled_until_configured(self):
        discovered = self.discover({"fixture": types.SimpleNamespace(ENGINE_CLASS=FixtureEngine)})
        self.assertFalse(discovered["fixture"].default_enabled)
        with patch("search_engine.manager.ENGINE_PLUGINS", discovered):
            self.assertEqual(EngineManager({}).get_available_sources(), [{"id": "all", "name": "全部聚合引擎"}])
            manager = EngineManager({"engines": {"fixture": {"enabled": True, "weight": 1.5}}})
        self.assertEqual(manager.get_available_sources()[1]["id"], "fixture")
        self.assertEqual(manager.engines["fixture"].weight, 1.5)

    def test_broken_import_and_invalid_contract_are_isolated(self):
        class FutureEngine(FixtureEngine):
            plugin_api_version = 99

        class SyncEngine(FixtureEngine):
            def search(self, query, client):
                return []

        with self.assertLogs("search_engine.registry", level="ERROR"):
            discovered = self.discover({
                "broken": ImportError("missing dependency"),
                "future": types.SimpleNamespace(ENGINE_CLASS=FutureEngine),
                "sync": types.SimpleNamespace(ENGINE_CLASS=SyncEngine),
                "valid": types.SimpleNamespace(ENGINE_CLASS=FixtureEngine),
                "helper": types.SimpleNamespace(),
            })
        self.assertEqual(set(discovered), {"fixture"})

    def test_duplicate_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ID 重复"):
            self.discover({"one": types.SimpleNamespace(ENGINE_CLASS=FixtureEngine),
                           "two": types.SimpleNamespace(ENGINE_CLASS=FixtureEngine)})

    def test_template_is_not_registered(self):
        discovered = self.discover({"template": types.SimpleNamespace(ENGINE_CLASS=FixtureEngine)})
        self.assertEqual(discovered, {})

    def test_initialization_failure_does_not_disable_other_plugins(self):
        class BrokenEngine(FixtureEngine):
            name = "broken"

            def __init__(self, config):
                raise ValueError("bad configuration")

        with patch("search_engine.manager.ENGINE_PLUGINS", {
            "broken": EnginePlugin(BrokenEngine, True),
            "fixture": EnginePlugin(FixtureEngine, True),
        }), self.assertLogs("search_engine.manager", level="ERROR"):
            manager = EngineManager({})
        self.assertEqual(set(manager.engines), {"fixture"})


class PluginHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_regex_template_outputs_common_results(self):
        info_hash = "0123456789abcdef0123456789abcdef01234567"
        uri = "magnet:?xt=urn:btih:" + info_hash + "&dn=" + quote("中文 1080p")

        def handler(request):
            self.assertEqual(request.url.params["q"], "中文 搜索")
            return httpx.Response(200, text=(
                '<article><h2>无效记录</h2><a href="bad">下载</a></article>'
                '<article><h2>中文标题</h2><a href="'
                + uri.replace("&", "&amp;") + '">下载</a></article>'
            ))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            results = await TemplateEngine({}).search("中文 搜索", client)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], TorrentResult)
        self.assertEqual(results[0].info_hash, info_hash)
        self.assertEqual(results[0].title, "中文 1080p")
        self.assertEqual(results[0].quality, "1080p")
        self.assertFalse(results[0].seeders_known)

    async def test_template_copied_with_new_id_is_discoverable(self):
        class MySiteEngine(TemplateEngine):
            name = "my_site"
            display_name = "我的站点"

        module = types.SimpleNamespace(ENGINE_CLASS=MySiteEngine, DEFAULT_ENABLED=False)
        registered = PluginDiscoveryTests().discover({"my_site": module})
        with patch("search_engine.manager.ENGINE_PLUGINS", registered):
            manager = EngineManager({"engines": {"my_site": {"enabled": True}}})
        self.assertIsInstance(manager.engines["my_site"], MySiteEngine)

    async def test_builtin_http_errors_propagate(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(503, text="unavailable")
        )) as client:
            for name, plugin in ENGINE_PLUGINS.items():
                with self.subTest(name=name), self.assertRaises(httpx.HTTPStatusError):
                    await plugin.engine_class({}).search("中文", client)


if __name__ == "__main__":
    unittest.main()
