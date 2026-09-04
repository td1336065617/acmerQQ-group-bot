"""数据抓取验证（需要网络；失败时打印错误但不中断模型测试）。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contest_fetcher import ContestFetcher


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
            if hasattr(first, "start_cn"):
                started = first.start_cn().strftime("%Y-%m-%d %H:%M")
            else:
                started = first.date_text()
            print("   first:", first.name, started)
        if platform == "offline":
            print("   source:", fetcher.source_url(platform))


if __name__ == "__main__":
    asyncio.run(_main())
