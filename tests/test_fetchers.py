"""数据抓取验证与缓存测试。"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contest_fetcher import OFFLINE_PLATFORM, ContestFetcher
from src.models import OfflineContest


def test_offline_cache_round_trip(tmp_path):
    async def scenario():
        cache_path = tmp_path / "contest_cache.json"
        first = ContestFetcher(cache_path=cache_path)
        first._cache[OFFLINE_PLATFORM] = (
            time.time(),
            [
                OfflineContest(
                    name="CCPC 广州站",
                    start_date=date(2026, 10, 3),
                    venue="广州",
                    source_url="https://www.xcpc.link/",
                )
            ],
        )
        first._source_urls[OFFLINE_PLATFORM] = "https://www.xcpc.link/"
        first._save_persistent_cache()

        second = ContestFetcher(cache_path=cache_path)
        second._load_persistent_cache()
        assert OFFLINE_PLATFORM in second._cache
        assert second._cache[OFFLINE_PLATFORM][1][0].name == "CCPC 广州站"
        assert second.source_url(OFFLINE_PLATFORM) == "https://www.xcpc.link/"

    asyncio.run(scenario())


def test_cache_hit_does_not_fetch(tmp_path, monkeypatch):
    async def scenario():
        fetcher = ContestFetcher(cache_path=tmp_path / "contest_cache.json")
        fetcher._cache[OFFLINE_PLATFORM] = (time.time(), [])

        async def unexpected_fetch():
            raise AssertionError("不应访问网络")

        monkeypatch.setattr(fetcher, "_fetch_offline", unexpected_fetch)
        contests, error = await fetcher.fetch_platform(OFFLINE_PLATFORM)
        assert contests == []
        assert error is None

    asyncio.run(scenario())


def test_concurrent_expired_requests_fetch_once(tmp_path, monkeypatch):
    async def scenario():
        fetcher = ContestFetcher(
            offline_cache_ttl=0, cache_path=tmp_path / "contest_cache.json"
        )
        calls = 0

        async def fake_fetch():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return []

        monkeypatch.setattr(fetcher, "_fetch_offline", fake_fetch)
        results = await asyncio.gather(
            fetcher.fetch_platform(OFFLINE_PLATFORM),
            fetcher.fetch_platform(OFFLINE_PLATFORM),
            fetcher.fetch_platform(OFFLINE_PLATFORM),
        )
        assert calls == 1
        assert all(error is None and contests == [] for contests, error in results)

    asyncio.run(scenario())


def test_stale_cache_is_fallback_on_fetch_error(tmp_path, monkeypatch):
    async def scenario():
        fetcher = ContestFetcher(
            offline_cache_ttl=0, cache_path=tmp_path / "contest_cache.json"
        )
        stale = OfflineContest(
            name="旧赛程",
            start_date=date(2026, 10, 3),
            source_url="https://www.xcpc.link/",
        )
        fetcher._cache[OFFLINE_PLATFORM] = (time.time() - 60, [stale])

        async def failed_fetch():
            raise RuntimeError("network down")

        monkeypatch.setattr(fetcher, "_fetch_offline", failed_fetch)
        contests, error = await fetcher.fetch_platform(OFFLINE_PLATFORM)
        assert contests == [stale]
        assert error is None

    asyncio.run(scenario())


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
