"""插件数据/缓存目录解析。

统一把可写数据放在 AstrBot 的 data/plugin_data/<插件名>/ 下：

- 插件代码目录在 Windows 上可能位于 Program Files 等只读位置，写入会失败；
- 插件更新/重装会清空代码目录，缓存与数据放在那里会被误删。

旧版本曾把缓存写在插件目录的 data/ 下，migrate_known_caches() 会在启动时
把旧文件**复制**到新位置（不删除原文件，便于回滚）。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

PLUGIN_NAME = "acmer_qq_group_bot"

_LOGGER = logging.getLogger(__name__)

#: 旧版本把缓存写在插件目录的 data/ 下
_LEGACY_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

#: 旧目录里需要迁移的缓存子目录 / 文件
_LEGACY_CACHE_DIRS = ("account_cards", "output_cache")
_LEGACY_CACHE_FILES = ("contest_cache.json", "settle_recent.json")


def plugin_data_dir() -> Path:
    """返回 AstrBot 数据目录下的插件数据目录。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path())
    except Exception:  # noqa: BLE001 - 未运行在 AstrBot 中时回退到插件目录
        return _LEGACY_DATA_DIR
    return base / "plugin_data" / PLUGIN_NAME


def plugin_cache_root() -> Path:
    """返回插件缓存根目录。"""
    return plugin_data_dir() / "cache"


def plugin_cache_dir(name: str) -> Path:
    """返回插件缓存子目录。"""
    return plugin_cache_root() / name


def migrate_known_caches() -> int:
    """把旧版写在插件目录 data/ 下的缓存复制到新位置（只复制，不删除）。"""
    moved = 0
    for name in _LEGACY_CACHE_DIRS:
        source = _LEGACY_DATA_DIR / name
        target = plugin_cache_dir(name)
        if not source.is_dir() or target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, dirs_exist_ok=True)
            moved += 1
        except OSError as exc:
            _LOGGER.warning("旧缓存目录迁移失败（已忽略）: %s -> %s (%s)", source, target, exc)
    for name in _LEGACY_CACHE_FILES:
        source = _LEGACY_DATA_DIR / name
        target = plugin_cache_root() / name
        if not source.is_file() or target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            moved += 1
        except OSError as exc:
            _LOGGER.warning("旧缓存文件迁移失败（已忽略）: %s -> %s (%s)", source, target, exc)
    return moved
