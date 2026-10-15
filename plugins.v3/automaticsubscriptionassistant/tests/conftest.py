"""``tests/`` 的 pytest 引导：把插件目录注册成一个「空壳包」。

**为什么需要**：pytest ≥ 9 收集 ``tests/test_imdb_filters.py`` 时，会把该文件所属的
**外层包**（即插件目录 ``automaticsubscriptionassistant/``，它有 ``__init__.py``）也收成一个
``Package`` 节点，并在 setup 时导入它的 ``__init__.py`` —— 那是插件入口，会拉起
apscheduler / pytz / ``app.*`` 一整套宿主依赖，脱离 MoviePilot 根本导不进来。

这里在收集阶段（早于任何 setup）先往 ``sys.modules`` 放一个只带 ``__path__`` 的空壳模块，
pytest 的 ``import_path`` 见到同名模块已在 ``sys.modules`` 中就会直接返回、不再执行真实
``__init__.py``；而 ``__path__`` 仍在，故 ``automaticsubscriptionassistant.core.filters``
这类子模块导入照常可用（测试模块用真实包名导入被测代码）。

刻意**不放** ``tests/__init__.py``：避免被 MoviePilot 当成插件子包加载。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_PACKAGE = PLUGIN_ROOT.name  # "automaticsubscriptionassistant"

# 空壳包：不执行真实 __init__.py，只提供 __path__ 供子模块导入。
_package = sys.modules.get(PLUGIN_PACKAGE)
if _package is None:
    _package = types.ModuleType(PLUGIN_PACKAGE)
    _package.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PLUGIN_PACKAGE] = _package

# ``providers`` 同理只给包壳：真正执行 providers/__init__.py 会把豆瓣/猫眼等来源一并导入，
# 它们的宿主依赖（DomUtils、PlaywrightHelper…）不在单元测试的替身范围内。
_providers = sys.modules.get(f"{PLUGIN_PACKAGE}.providers")
if _providers is None:
    _providers = types.ModuleType(f"{PLUGIN_PACKAGE}.providers")
    _providers.__path__ = [str(PLUGIN_ROOT / "providers")]
    sys.modules[f"{PLUGIN_PACKAGE}.providers"] = _providers
