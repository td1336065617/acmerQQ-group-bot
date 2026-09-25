"""报名截止提醒（A4）测试：解析、合并容错、两级幂等、缓存版本、群过滤。

用 `AcmerGroupBot.__new__` 组装最小插件实例（沿用 test_main_accounts 的做法），
不启动 AstrBot、不访问网络。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.contest_fetcher as contest_fetcher_module
from src.contest_fetcher import CACHE_VERSION, ContestFetcher
from src.models import Contest, GroupConfig
from test_main_accounts import _load_main_module  # 复用既有的 main.py 装载器
from test_settlement_tick import FakeContestFetcher

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
END_MS = 1_800_000_000_000  # 2027-01-15 08:00:00 UTC


def _html(*payloads: dict, double: bool = True) -> str:
    """构造牛客比赛列表页片段：data-json 是 HTML 转义的 JSON。

    真实页面（2026-09-18 实测）是**双重转义**（``&amp;quot;``），
    因此默认按双层构造；``double=False`` 时构造单层写法。
    """
    blocks = []
    for payload in payloads:
        escaped = (
            json.dumps(payload, ensure_ascii=False)
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        if double:
            escaped = (
                escaped.replace("&", "&amp;")
                .replace('"', "&quot;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
        blocks.append(f'<div class="contest" data-json="{escaped}"></div>')
    return "<html><body>" + "".join(blocks) + "</body></html>"


def _contest(
    contest_id: str = "133885",
    *,
    name: str = "牛客周赛 Round 170",
    start_delta: timedelta = timedelta(days=3),
    signup_delta: timedelta | None = timedelta(hours=20),
) -> Contest:
    return Contest(
        platform="nowcoder",
        name=name,
        start_time=NOW + start_delta,
        end_time=NOW + start_delta + timedelta(hours=2),
        duration_minutes=120,
        url=f"https://ac.nowcoder.com/acm/contest/{contest_id}",
        contest_id=contest_id,
        signup_end_time=(
            NOW + signup_delta if signup_delta is not None else None
        ),
    )


def _build_bot(main_module, *, contests, groups, send_ok=True):
    bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)
    bot.fetcher = FakeContestFetcher({"nowcoder": list(contests)})
    bot._kv = {}
    bot._sent = []

    async def get_kv_data(key, default=None):
        return bot._kv.get(key, default)

    async def put_kv_data(key, value):
        bot._kv[key] = value

    async def get_groups():
        return list(groups)

    async def send_notification(group, text):
        bot._sent.append((group.group_id, text))
        return send_ok

    bot.get_kv_data = get_kv_data
    bot.put_kv_data = put_kv_data
    bot.get_groups = get_groups
    bot.send_notification = send_notification
    return bot


def test_signup_deadline_parsing_from_escaped_json(monkeypatch):
    async def scenario():
        payloads = [
            {
                "contestId": 133885,
                "contestName": "牛客周赛 Round 170",
                "contestSignUpEndTime": END_MS,
            },
            # 缺 contestSignUpEndTime → 跳过
            {"contestId": 133886, "contestName": "无报名截止"},
            # 非法时间戳 → 跳过
            {"contestId": 133887, "contestSignUpEndTime": "abc"},
        ]
        # 真实页面是双重转义（&amp;quot;）；单层写法也必须兼容
        for double in (True, False):
            html_text = _html(*payloads, double=double)

            async def fake_fetch(session, url, _html=html_text, **kwargs):
                assert "vip-index" in url
                return _html

            monkeypatch.setattr(
                contest_fetcher_module, "fetch_text_with_retry", fake_fetch
            )
            fetcher = ContestFetcher(
                cache_path="/tmp/nonexistent-signup-test.json"
            )
            fetcher.session = object()
            deadlines = await fetcher._fetch_nowcoder_signup_deadlines()
            assert set(deadlines) == {"133885"}, f"double={double}"
            assert deadlines["133885"] == datetime.fromtimestamp(
                END_MS / 1000, tz=timezone.utc
            )

    asyncio.run(scenario())


def test_calendar_merges_deadlines_and_tolerates_failure(monkeypatch):
    async def scenario():
        def _item(contest_id: int, name: str, real_id: int) -> dict:
            start_ms = int((NOW + timedelta(days=3)).timestamp() * 1000)
            return {
                "contestId": contest_id,
                "contestName": name,
                "ojName": "NowCoder",
                "link": f"https://ac.nowcoder.com/acm/contest/{real_id}",
                "startTime": start_ms,
                "endTime": start_ms + 3600_000,
            }

        fetcher = ContestFetcher(cache_path="/tmp/nonexistent-signup-test.json")
        fetcher.session = object()

        async def fake_month(month: str):
            # 日历 contestId（1139935）与真实比赛 ID（139935）不同，
            # 报名截止表用的是真实 ID —— 必须按链接里的 ID 匹配。
            return [_item(1139935, "牛客挑战赛91", 139935)]

        async def fake_deadlines():
            return {
                "139935": datetime.fromtimestamp(
                    END_MS / 1000, tz=timezone.utc
                )
            }

        monkeypatch.setattr(fetcher, "_fetch_nc_month", fake_month)
        monkeypatch.setattr(
            fetcher, "_nc_months", lambda *a, **k: ["2026-9", "2026-10"]
        )
        monkeypatch.setattr(
            fetcher, "_fetch_nowcoder_signup_deadlines", fake_deadlines
        )
        contests = await fetcher._fetch_nowcoder_calendar("nowcoder")
        assert len(contests) == 1
        assert contests[0].contest_id == "1139935"  # 模型里的 ID 语义不变
        assert contests[0].signup_end_time == datetime.fromtimestamp(
            END_MS / 1000, tz=timezone.utc
        )

        # 报名截止解析失败 → 赛程主流程不受影响，字段为 None
        async def broken_deadlines():
            raise RuntimeError("vip-index 页面改版")

        monkeypatch.setattr(
            fetcher, "_fetch_nowcoder_signup_deadlines", broken_deadlines
        )
        contests = await fetcher._fetch_nowcoder_calendar("nowcoder")
        assert len(contests) == 1
        assert contests[0].signup_end_time is None

    asyncio.run(scenario())


def test_two_tier_reminders_are_idempotent():
    async def scenario():
        main_module = _load_main_module()
        group = GroupConfig(group_id="g1", push_platforms=["nowcoder"])
        contests = [
            _contest("1001", name="24 小时档", signup_delta=timedelta(hours=20)),
            _contest("1002", name="2 小时档", signup_delta=timedelta(hours=1)),
            _contest("1003", name="太远", signup_delta=timedelta(hours=30)),
            _contest(
                "1004",
                name="已开始",
                start_delta=timedelta(hours=-1),
                signup_delta=timedelta(hours=1),
            ),
            _contest("1005", name="无报名截止", signup_delta=None),
        ]
        bot = _build_bot(main_module, contests=contests, groups=[group])
        assert await bot.tick_signup_reminders(NOW) == 2
        assert bot._kv.get("signup_1001_24h") is True
        assert bot._kv.get("signup_1002_2h") is True
        # 推送结果同时写入后台「运行状态」日志
        assert [item["kind"] for item in bot._kv["push_log"]] == [
            "signup",
            "signup",
        ]
        texts = [text for _gid, text in bot._sent]
        assert len(texts) == 2
        assert "报名即将截止：24 小时档" in texts[0]
        assert "（北京时间）" in texts[0]
        assert "还有 20 小时" in texts[0]
        assert "还有 1 小时" in texts[1]
        # 第二次 tick 两级都已幂等，不再重复打扰
        assert await bot.tick_signup_reminders(NOW) == 0
        assert len(bot._sent) == 2

    asyncio.run(scenario())


def test_send_failure_keeps_key_for_retry():
    async def scenario():
        main_module = _load_main_module()
        group = GroupConfig(group_id="g1", push_platforms=["nowcoder"])
        bot = _build_bot(
            main_module,
            contests=[_contest("1001", signup_delta=timedelta(hours=20))],
            groups=[group],
            send_ok=False,
        )
        assert await bot.tick_signup_reminders(NOW) == 0
        assert bot._kv == {}  # 发送失败不写幂等键，下一 tick 重试

    asyncio.run(scenario())


def test_non_nowcoder_group_is_not_reminded():
    async def scenario():
        main_module = _load_main_module()
        group = GroupConfig(group_id="g1", push_platforms=["codeforces"])
        bot = _build_bot(
            main_module,
            contests=[_contest("1001", signup_delta=timedelta(hours=20))],
            groups=[group],
        )
        assert await bot.tick_signup_reminders(NOW) == 0
        assert bot._sent == []
        assert bot._kv == {}

    asyncio.run(scenario())


def test_old_cache_version_is_ignored(tmp_path):
    def _write_cache(path: Path, version: int) -> None:
        payload = {
            "version": version,
            "platforms": {
                "nowcoder": {
                    "fetched_at": time.time(),
                    "source_url": "",
                    "contests": [
                        {
                            "platform": "nowcoder",
                            "name": "牛客周赛 Round 170",
                            "start_time": (NOW + timedelta(days=3)).isoformat(),
                            "end_time": (
                                NOW + timedelta(days=3, hours=2)
                            ).isoformat(),
                            "duration_minutes": 120,
                            "url": "https://ac.nowcoder.com/acm/contest/133885",
                            "contest_id": "133885",
                        }
                    ],
                }
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    assert CACHE_VERSION == 2  # 模型新增 signup_end_time → 版本必须提升

    cache_path = tmp_path / "contest_cache.json"
    _write_cache(cache_path, 1)
    fetcher = ContestFetcher(cache_path=cache_path)
    fetcher._load_persistent_cache()
    assert fetcher._cache == {}  # 旧版本缓存被忽略

    _write_cache(cache_path, CACHE_VERSION)
    fetcher = ContestFetcher(cache_path=cache_path)
    fetcher._load_persistent_cache()
    assert len(fetcher._cache["nowcoder"][1]) == 1
    assert fetcher._cache["nowcoder"][1][0].signup_end_time is None
