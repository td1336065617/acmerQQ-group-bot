"""公告数据抓取（ICPC 北京总部公告页，aiohttp + 公开页面）。

页面结构（https://icpc.pku.edu.cn/tzgg/index.htm）：

    <ul class="item-list">
      <li><a href="xxx.htm">标题<span class="s-dt">[2026-09-03]</span></a></li>
      ...
    </ul>

页面为静态 HTML，无需登录、无 JS 渲染；响应头不带 charset，需显式按
UTF-8 解码。这里用正则解析（与洛谷/AtCoder 抓取保持一致的零额外依赖风格）。
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import aiohttp

from astrbot.api import logger

from src.models import ANNOUNCEMENT_LABELS, Announcement
from src.utils import USER_AGENT, fetch_text_with_retry

ICPC_PKU_BASE_URL = "https://icpc.pku.edu.cn/tzgg/"
ICPC_PKU_LIST_URL = ICPC_PKU_BASE_URL + "index.htm"

# 公告列表容器；页面其余 <li> 是导航菜单，必须先框定范围再取条目
_LIST_RE = re.compile(r'<ul\s+class="item-list"\s*>(.*?)</ul>', re.S | re.I)
_ITEM_RE = re.compile(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_DATE_RE = re.compile(
    r'<span\s+class="s-dt"\s*>\s*\[?\s*(\d{4}-\d{1,2}-\d{1,2})\s*\]?\s*</span>',
    re.S | re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_title(raw: str) -> str:
    """去掉标题内的标签/实体/多余空白（日期 span 已由调用方剥离）。"""
    text = _TAG_RE.sub("", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_icpc_pku_announcements(
    text: str, base_url: str = ICPC_PKU_BASE_URL
) -> List[Announcement]:
    """解析 ICPC 北京总部公告列表页，按页面顺序（新→旧）返回公告。"""
    section = _LIST_RE.search(text)
    if not section:
        raise ValueError("未找到公告列表（item-list）")
    announcements: List[Announcement] = []
    seen: set = set()
    for href, inner in _ITEM_RE.findall(section.group(1)):
        date_match = _DATE_RE.search(inner)
        published = None
        if date_match:
            try:
                published = datetime.strptime(
                    date_match.group(1), "%Y-%m-%d"
                ).date()
            except ValueError:
                published = None
            inner = inner[: date_match.start()] + inner[date_match.end() :]
        title = _clean_title(inner)
        if not title:
            continue
        url = urljoin(base_url, html.unescape(href.strip()))
        # 详情页文件名（去掉扩展名）就是公告的稳定唯一 ID
        slug = url.rsplit("/", 1)[-1].split("?")[0]
        announcement_id = slug.rsplit(".", 1)[0] or slug or url
        if announcement_id in seen:
            continue
        seen.add(announcement_id)
        announcements.append(
            Announcement(
                source="icpc_pku",
                title=title,
                url=url,
                published=published,
                announcement_id=announcement_id,
            )
        )
    if not announcements:
        raise ValueError("公告列表为空，页面结构可能已变化")
    return announcements


class AnnouncementFetcher:
    """公告抓取器，带 10 分钟内存缓存（公告更新频率远低于比赛）。"""

    def __init__(self, cache_ttl: int = 600) -> None:
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Tuple[float, List[Announcement]]] = {}
        self.session: Optional[aiohttp.ClientSession] = None

    async def initialize(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT}
            )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def fetch_source(
        self, source: str = "icpc_pku", force: bool = False
    ) -> Tuple[List[Announcement], Optional[str]]:
        """抓取指定来源，返回 (公告列表, 错误提示)；失败时列表为空。"""
        if not force:
            cached = self._cache.get(source)
            if cached and time.time() - cached[0] < self.cache_ttl:
                return cached[1], None
        label = ANNOUNCEMENT_LABELS.get(source, source)
        try:
            if source == "icpc_pku":
                announcements = await self._fetch_icpc_pku()
            else:
                return [], "未知公告来源"
        except asyncio.TimeoutError:
            logger.error("公告抓取超时: %s", source)
            return [], f"⏰ {label} 公告获取超时，请稍后重试"
        except aiohttp.ClientError as exc:
            logger.error("公告网络错误(%s): %s", source, exc)
            return [], f"🌐 {label} 公告网络连接失败，请检查网络后重试"
        except (ValueError, KeyError, json.JSONDecodeError, re.error) as exc:
            logger.error("公告解析失败(%s): %s", source, exc)
            return [], f"🔧 {label} 公告页格式有变化，请联系管理员修复"
        except Exception as exc:  # noqa: BLE001
            logger.error("公告未知错误(%s): %s", source, exc, exc_info=True)
            return [], f"❌ 获取 {label} 公告失败，请稍后重试"
        self._cache[source] = (time.time(), announcements)
        return announcements, None

    # ------------------------------------------------------------------
    async def _fetch_icpc_pku(self) -> List[Announcement]:
        if self.session is None:
            raise RuntimeError("session 未初始化")
        # 该站响应头不带 charset，显式指定 UTF-8，否则中文标题会乱码
        text = await fetch_text_with_retry(
            self.session, ICPC_PKU_LIST_URL, encoding="utf-8"
        )
        return parse_icpc_pku_announcements(text, ICPC_PKU_BASE_URL)
