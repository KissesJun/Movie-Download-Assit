"""发现并校验搜索插件；仅在应用启动/模块加载时执行，不创建后台进程。"""

import importlib
import inspect
import logging
import pkgutil
import re
from dataclasses import dataclass
from pathlib import Path

from .base import BaseEngine

logger = logging.getLogger(__name__)
PLUGIN_API_VERSION = 1


@dataclass(frozen=True)
class EnginePlugin:
    engine_class: type[BaseEngine]
    default_enabled: bool = False


def discover_plugins() -> dict[str, EnginePlugin]:
    """加载本地可信插件。导入/接口错误隔离；重复 ID 拒绝启动。"""
    registered = {}
    module_path = [str(Path(__file__).resolve().parent)]
    for info in sorted(pkgutil.iter_modules(module_path), key=lambda item: item.name):
        if not info.name.startswith("search_") or info.name == "search_template":
            continue
        try:
            module = importlib.import_module(f"{__package__}.{info.name}")
            engine_class = getattr(module, "ENGINE_CLASS", None)
            if engine_class is None:
                continue  # 通用 HTML 适配器等共享模块，不声明固定站点。
            if (not inspect.isclass(engine_class)
                    or not issubclass(engine_class, BaseEngine)
                    or inspect.isabstract(engine_class)):
                raise ValueError("ENGINE_CLASS 必须是具体的 BaseEngine 子类")
            if engine_class.plugin_api_version != PLUGIN_API_VERSION:
                raise ValueError("不支持的搜索插件接口版本")
            if not re.fullmatch(r"[a-z][a-z0-9_]*", engine_class.name) or engine_class.name == "all":
                raise ValueError("引擎 name 必须是小写唯一 ID，且不能为 all")
            if not engine_class.display_name.strip():
                raise ValueError("引擎 display_name 不能为空")
            if not inspect.iscoroutinefunction(engine_class.search):
                raise ValueError("search 必须是 async 方法")
            default_enabled = getattr(module, "DEFAULT_ENABLED", False)
            if not isinstance(default_enabled, bool):
                raise ValueError("DEFAULT_ENABLED 必须是布尔值")
        except Exception:
            logger.exception("跳过无法加载的搜索插件: %s", info.name)
            continue
        if engine_class.name in registered:
            raise ValueError(f"搜索插件 ID 重复: {engine_class.name}")
        registered[engine_class.name] = EnginePlugin(engine_class, default_enabled)
    return registered


ENGINE_PLUGINS = discover_plugins()
AVAILABLE_ENGINES = {key: plugin.engine_class for key, plugin in ENGINE_PLUGINS.items()}
