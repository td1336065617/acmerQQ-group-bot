"""赛后赛果推送主流程测试（A1 tick）。

用 `AcmerGroupBot.__new__` 组装最小插件实例（沿用 test_main_accounts 的做法），
不启动 AstrBot、不访问网络。
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import Contest, GroupConfig
from src.settlement import SettleResult, SettleRow, SettlementService

ROOT = Path(__file__).resolve().parent.parent


from test_main_accounts import _load_main_module  # 复用既有的 main.py 装载器

class FakeRegistry:
    def __init__(self, members, accounts):
        self._members = members
        self._accounts = accounts

    async def get_group_member_ids(self, group_id):
        return list(self._members.get(str(group_id), []))

    async def get_all_accounts(self):
        return dict(self._accounts)


class FakeContestFetcher:
    def __init__(self, contests):
        self._contests = contests
        # tick 会读取"上一次缓存里的赛程"一起记入最近比赛记录
        self._cache: dict = {}
        self.calls = 0

    async def fetch_platform(self, platform, force=False):
        self.calls += 1
        return list(self._contests.get(platform, [])), None


class FakeSettlement:
    """只替换 collect；最近比赛记录委托给真实的 SettlementService（纯内存逻辑）。"""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0
        self._real = SettlementService(None)

    async def collect(self, platform, contest, members):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    def remember_contests(self, platform, contests, **kwargs):
        return self._real.remember_contests(platform, contests, **kwargs)

    def settlement_candidates(self, platform, now, delay_minutes, **kwargs):
        return self._real.settlement_candidates(
            platform, now, delay_minutes, **kwargs
        )

    def save_recent_contests(self, *args, **kwargs):
        return None

    def load_recent_contests(self, *args, **kwargs):
        return 0


def _build_bot(main_module, *, groups, contests, members, accounts, settlement, settings=None):
    bot = main_module.AcmerGroupBot.__new__(main_module.AcmerGroupBot)
    bot.fetcher = FakeContestFetcher(contests)
    bot.account_registry = FakeRegistry(members, accounts)
    bot.settlement = settlement
    bot._kv = {}
    bot._sent = []
    bot._images = []

    async def get_kv_data(key, default=None):
        return bot._kv.get(key, default)

    async def put_kv_data(key, value):
        bot._kv[key] = value

    async def get_groups():
        return list(groups)

    async def get_settings():
        base = {
            "push_platforms": ["codeforces", "atcoder", "nowcoder", "luogu"],
            "settle_push_enabled": True,
            "settle_delay_minutes": 10,
            "settle_min_participants": 1,
            "settle_show_unsolved": True,
        }
        base.update(settings or {})
        return base

    async def send_notification(group, text):
        bot._sent.append((group.group_id, text))
        return True

    async def send_group_image(group, path, caption=""):
        bot._images.append((group.group_id, str(path), caption))
        return True

    async def render_settlement_card(sections, **kwargs):
        bot._rendered = (sections, kwargs)
        return None          # 让流程走纯文本兜底，便于断言文案

    bot.get_kv_data = get_kv_data
    bot.put_kv_data = put_kv_data
    bot.get_groups = get_groups
    bot.get_settings = get_settings
    bot.send_notification = send_notification
    bot._send_group_image = send_group_image
    bot._render_settlement_card = render_settlement_card
    return bot


def _contest(hours_ago: float = 0.5, contest_id: str = "2264") -> Contest:
    end = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return Contest(
        platform="codeforces",
        name="Codeforces Round 1121 (Div. 2)",
        start_time=end - timedelta(hours=2),
        end_time=end,
        duration_minutes=120,
        url="https://codeforces.com/contest/2264",
        contest_id=contest_id,
    )


def _result(rows=None) -> SettleResult:
    return SettleResult(
        platform="codeforces",
        contest_id="2264",
        contest_name="Codeforces Round 1121 (Div. 2)",
        rows=rows
        if rows is not None
        else [
            SettleRow(
                platform="codeforces",
                user_id="u1",
                display_name="张三",
                handle="zhangsan",
                rank=120,
                user_count=6243,
                solved=5,
                total_problems=6,
                unsolved=["D"],
                source="cf-standings",
            )
        ],
        note="数据源：Codeforces 官方 standings",
    )


GROUP = GroupConfig(group_id="g1", push_platforms=["codeforces"])
MEMBERS = {"g1": ["u1"]}
ACCOUNTS = {"u1": {"codeforces": {"handle": "zhangsan", "display_name": "张三"}}}


def test_push_once_and_idempotent():
    async def scenario():
        main_module = _load_main_module()
        settlement = FakeSettlement(result=_result())
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [_contest()]},
            members=MEMBERS,
            accounts=ACCOUNTS,
            settlement=settlement,
        )
        pushed = await bot.tick_settlements()
        assert pushed == 1
        assert settlement.calls == 1
        assert bot._sent and "赛果" in bot._sent[0][1]
        assert "本场未通过：D" in bot._sent[0][1]
        assert "评分变化以平台为准" in bot._sent[0][1]
        # 幂等：第二次 tick 不再推送
        pushed = await bot.tick_settlements()
        assert pushed == 0
        assert settlement.calls == 1

    asyncio.run(scenario())


def test_delay_window_filters_contests():
    async def scenario():
        main_module = _load_main_module()
        # 刚结束 1 分钟（未到 10 分钟延迟）→ 不推
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [_contest(hours_ago=1 / 60)]},
            members=MEMBERS,
            accounts=ACCOUNTS,
            settlement=FakeSettlement(result=_result()),
        )
        assert await bot.tick_settlements() == 0
        # 结束 3 小时（超出 2 小时补推窗口）→ 不推
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [_contest(hours_ago=3)]},
            members=MEMBERS,
            accounts=ACCOUNTS,
            settlement=FakeSettlement(result=_result()),
        )
        assert await bot.tick_settlements() == 0

    asyncio.run(scenario())


def test_missing_end_time_uses_duration():
    async def scenario():
        main_module = _load_main_module()
        contest = _contest()
        contest.end_time = None
        contest.start_time = datetime.now(timezone.utc) - timedelta(
            minutes=130
        )          # 120 分钟时长 → 结束 10 分钟前
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [contest]},
            members=MEMBERS,
            accounts=ACCOUNTS,
            settlement=FakeSettlement(result=_result()),
        )
        assert await bot.tick_settlements() == 1

    asyncio.run(scenario())


def test_global_and_group_switches():
    async def scenario():
        main_module = _load_main_module()
        # 全局关闭
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [_contest()]},
            members=MEMBERS,
            accounts=ACCOUNTS,
            settlement=FakeSettlement(result=_result()),
            settings={"settle_push_enabled": False},
        )
        assert await bot.tick_settlements() == 0
        # 群级关闭
        group = GroupConfig(
            group_id="g1", push_platforms=["codeforces"], settle_push_enabled=False
        )
        bot = _build_bot(
            main_module,
            groups=[group],
            contests={"codeforces": [_contest()]},
            members=MEMBERS,
            accounts=ACCOUNTS,
            settlement=FakeSettlement(result=_result()),
        )
        assert await bot.tick_settlements() == 0

    asyncio.run(scenario())


def test_min_participants_blocks_push():
    async def scenario():
        main_module = _load_main_module()
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [_contest()]},
            members=MEMBERS,
            accounts=ACCOUNTS,
            settlement=FakeSettlement(result=_result()),
            settings={"settle_min_participants": 3},
        )
        assert await bot.tick_settlements() == 0

    asyncio.run(scenario())


def test_skip_records_marker_and_repushes_when_membership_changes():
    async def scenario():
        main_module = _load_main_module()
        # 没有绑定该平台的成员：跳过，但要落「带成员指纹的」标记（BUG-039）
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [_contest()]},
            members={"g1": []},
            accounts={},
            settlement=FakeSettlement(result=_result()),
        )
        assert await bot.tick_settlements() == 0
        marker = bot._kv.get("settle_g1_codeforces_2264")
        assert isinstance(marker, dict) and marker.get("skipped") is True
        assert marker.get("members") == ""
        # 成员没变 -> 第二个 tick 不再重复评估
        assert await bot.tick_settlements() == 0
        assert not bot._sent
        # 之后有人绑定该平台 -> 指纹变化 -> 允许补评估一次并推送
        bot.account_registry._members["g1"] = ["u1"]
        bot.account_registry._accounts["u1"] = ACCOUNTS["u1"]
        assert await bot.tick_settlements() == 1
        assert bot._sent and "赛果" in bot._sent[0][1]
        # 采集返回 None（未就绪）→ 不写幂等键，下一 tick 可重试
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [_contest()]},
            members=MEMBERS,
            accounts=ACCOUNTS,
            settlement=FakeSettlement(result=None),
        )
        assert await bot.tick_settlements() == 0
        assert bot._kv == {}

    asyncio.run(scenario())


def test_unsolved_note_can_be_disabled():
    async def scenario():
        main_module = _load_main_module()
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [_contest()]},
            members=MEMBERS,
            accounts=ACCOUNTS,
            settlement=FakeSettlement(result=_result()),
            settings={"settle_show_unsolved": False},
        )
        await bot.tick_settlements()
        assert "本场未通过" not in bot._sent[0][1]

    asyncio.run(scenario())


def test_extra_note_only_platform_still_pushes():
    async def scenario():
        main_module = _load_main_module()
        result = SettleResult(
            platform="luogu",
            contest_id="273413",
            contest_name="洛谷月赛",
            rows=[],
            note="洛谷不公开比赛名次",
            extra_note="洛谷参赛：张三",
        )
        group = GroupConfig(group_id="g1", push_platforms=["luogu"])
        bot = _build_bot(
            main_module,
            groups=[group],
            contests={"luogu": [_contest()]},
            members={"g1": ["u1"]},
            accounts={"u1": {"luogu": {"handle": "100", "display_name": "张三"}}},
            settlement=FakeSettlement(result=result),
        )
        assert await bot.tick_settlements() == 1
        assert "洛谷参赛：张三" in bot._sent[0][1]

    asyncio.run(scenario())


def test_send_failure_keeps_key_for_retry():
    async def scenario():
        main_module = _load_main_module()
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [_contest()]},
            members=MEMBERS,
            accounts=ACCOUNTS,
            settlement=FakeSettlement(result=_result()),
        )

        async def failing_notification(group, text):
            return False

        bot.send_notification = failing_notification
        assert await bot.tick_settlements() == 0
        assert bot._kv == {}          # 发送失败不写键 → 下一 tick 重试

    asyncio.run(scenario())


def test_settlement_exception_does_not_break_tick():
    async def scenario():
        main_module = _load_main_module()
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [_contest()]},
            members=MEMBERS,
            accounts=ACCOUNTS,
            settlement=FakeSettlement(error=RuntimeError("boom")),
        )
        # collect 抛错时应被吞掉（由 SettlementService 内部处理），这里模拟
        # 服务直接抛错的最坏情况：tick 不应崩溃到调用方。
        try:
            await bot.tick_settlements()
        except RuntimeError:
            raise AssertionError("tick 不应向上抛采集异常")

    asyncio.run(scenario())

def test_skip_low_participants_writes_idempotent_key():
    """BUG-039：成员不足时「跳过」也必须落幂等键，否则每个 tick 都会重复评估同一场。"""

    async def scenario():
        main_module = _load_main_module()
        settlement = FakeSettlement(result=_result())
        bot = _build_bot(
            main_module,
            groups=[GROUP],
            contests={"codeforces": [_contest()]},
            members={"g1": []},  # 本群没有任何绑定成员 -> 低于阈值
            accounts=ACCOUNTS,
            settlement=settlement,
        )
        assert await bot.tick_settlements() == 0
        assert settlement.calls == 0
        # 关键：跳过也要落标记（带成员指纹），避免每 tick 空转
        marker = bot._kv.get("settle_g1_codeforces_2264")
        assert isinstance(marker, dict) and marker.get("skipped") is True
        # 第二个 tick 命中幂等键：不再重复评估，也不重复抓取
        assert await bot.tick_settlements() == 0
        assert settlement.calls == 0
        # 推送日志里能查到「跳过」
        logs = bot._kv.get("push_log") or []
        assert any("跳过" in str(item.get("detail", "")) for item in logs)

    asyncio.run(scenario())
