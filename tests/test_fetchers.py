"""数据抓取验证（需要网络；失败时打印错误但不中断模型测试）。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.announcement_fetcher import AnnouncementFetcher
from src.contest_fetcher import ContestFetcher
from src.models import DEFAULT_ANNOUNCEMENT_SOURCES


async def _main():
    fetcher = ContestFetcher()
    await fetcher.initialize()
    results = await fetcher.fetch_all(force=True)
    await fetcher.close()
    for platform, (contests, err) in results.items():
        if err:
            print(f"[{platform}] ERROR: {err}")
            continue
        upcoming = [c for c in contests if c.is_upcoming()]
        print(f"[{platform}] total={len(contests)} upcoming={len(upcoming)}")
        if upcoming:
            first = upcoming[0]
            print("   first:", first.name, first.start_cn().strftime("%Y-%m-%d %H:%M"))

    announcer = AnnouncementFetcher()
    await announcer.initialize()
    for source in DEFAULT_ANNOUNCEMENT_SOURCES:
        announcements, err = await announcer.fetch_source(source, force=True)
        if err:
            print(f"[{source}] ERROR: {err}")
            continue
        print(f"[{source}] total={len(announcements)}")
        for item in announcements[:3]:
            print("   ", item.format_line())
            print("    ", item.url)
    await announcer.close()


if __name__ == "__main__":
    asyncio.run(_main())
