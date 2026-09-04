"""竞赛平台账号绑定、历史变化和卡片数据测试。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.account_cards import AccountCardRenderer
from src.account_fetcher import AccountFetcher, normalize_account_identifier
from src.account_models import AccountProfile
from src.account_registry import AccountRegistry
from src.output_renderer import AdaptiveOutputRenderer


class FakePlugin:
    def __init__(self):
        self.store = {}

    async def get_kv_data(self, key, default=None):
        return self.store.get(key, default)

    async def put_kv_data(self, key, value):
        self.store[key] = value


def test_account_identifier_normalization():
    assert (
        normalize_account_identifier(
            "codeforces", "https://codeforces.com/profile/Tourist"
        )
        == "Tourist"
    )
    assert (
        normalize_account_identifier(
            "nowcoder", "https://ac.nowcoder.com/acm/contest/profile/949094787"
        )
        == "949094787"
    )
    assert (
        normalize_account_identifier(
            "atcoder", "https://atcoder.jp/users/user_name"
        )
        == "user_name"
    )
    assert (
        normalize_account_identifier(
            "luogu", "https://www.luogu.com.cn/user/1770958"
        )
        == "1770958"
    )


def test_rating_history_delta():
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc).timestamp()
    rows = [
        {
            "contestName": "旧比赛",
            "rank": 10,
            "oldRating": 1000,
            "newRating": 1050,
            "ratingUpdateTimeSeconds": now - 10 * 86400,
            "contestId": 1,
        },
        {
            "contestName": "近期比赛",
            "rank": 5,
            "oldRating": 1050,
            "newRating": 1175,
            "ratingUpdateTimeSeconds": now - 2 * 86400,
            "contestId": 2,
        },
    ]
    history = AccountFetcher._parse_cf_history(rows, limit=200)
    profile = AccountProfile(
        platform="codeforces",
        handle="demo",
        rating=1175,
        rating_history=history,
    )
    assert AccountFetcher.rating_delta_for_period(profile, now=now) == 125


def test_public_profile_parsers():
    cf_profile = AccountFetcher._profile_from_codeforces_user(
        {
            "handle": "demo",
            "lastName": "ACM-TOKEN",
            "rating": 1800,
            "rank": "expert",
            "maxRating": 1900,
            "organization": "Example University",
        }
    )
    assert cf_profile.handle == "demo"
    assert cf_profile.verification_value == "ACM-TOKEN"
    assert cf_profile.rating == 1800

    nowcoder_history = AccountFetcher._parse_nowcoder_history(
        [
            {
                "contestName": "牛客赛",
                "rank": 12,
                "rating": 1100,
                "changeValue": 100,
                "time": 1_700_000_000_000,
                "contestId": 123,
            }
        ]
    )
    assert nowcoder_history[0]["old_rating"] == 1000
    assert nowcoder_history[0]["delta"] == 100

    atcoder_html = """
    <table>
      <tr><th>Rank</th><td>42nd <span>(Top 1%)</span></td></tr>
      <tr><th>Rating</th><td><span class="user-blue">1234</span></td></tr>
      <tr><th>Highest Rating</th><td><span>1400</span></td></tr>
      <tr><th>Rated Matches <span>help</span></th><td>8</td></tr>
      <tr><th>Affiliation</th><td>ACM-TOKEN</td></tr>
    </table>
    <script>var rating_history=[{"EndTime":1700000000,"NewRating":1234,
    "OldRating":1200,"Place":42,"ContestName":"ABC"}];</script>
    """
    assert AccountFetcher._atcoder_table_value(atcoder_html, "Rated Matches") == "8"
    atcoder_history = AccountFetcher._parse_atcoder_history_html(atcoder_html)
    assert atcoder_history[0]["delta"] == 34

    luogu_payload = {
        "data": {
            "user": {
                "uid": 1770958,
                "name": "demo",
                "introduction": "ACM-TOKEN",
                "ranking": 321,
                "color": "Red",
            }
        }
    }
    luogu_user = AccountFetcher._find_luogu_user(luogu_payload, "1770958")
    assert luogu_user is not None
    assert luogu_user["introduction"] == "ACM-TOKEN"


def test_codeforces_bulk_profiles(monkeypatch):
    async def scenario():
        fetcher = AccountFetcher()

        async def fake_cf_json(method, params):
            assert method == "user.info"
            assert params["handles"] == "alice;bob"
            return {
                "status": "OK",
                "result": [
                    {"handle": "alice", "rating": 1000},
                    {"handle": "bob", "rating": 1200},
                ],
            }

        monkeypatch.setattr(fetcher, "_cf_json", fake_cf_json)
        profiles = await fetcher.get_profiles(
            "codeforces", ["alice", "bob", "alice"]
        )
        assert profiles["alice"].rating == 1000
        assert profiles["bob"].rating == 1200

    asyncio.run(scenario())


def test_new_account_rating_history_uses_old_rating():
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc).timestamp()
    history = [
        {
            "rating": 1234,
            "old_rating": 1000,
            "timestamp": now - 86400,
        }
    ]
    profile = AccountProfile(
        platform="atcoder",
        handle="demo",
        rating=1234,
        rating_history=history,
    )
    assert AccountFetcher.rating_delta_for_period(profile, now=now) == 234


def test_registry_binding_token_and_group_auto_join():
    async def scenario():
        plugin = FakePlugin()
        registry = AccountRegistry(plugin)
        profile = AccountProfile(
            platform="codeforces",
            handle="demo",
            platform_user_id="Demo",
            display_name="demo",
        )
        token = await registry.create_pending("user-1", "codeforces", profile)
        pending = await registry.get_pending("user-1", "codeforces")
        assert pending is not None
        # CF 的 Last name 同样允许保留原姓氏，再追加验证码。
        assert registry.token_matches(
            f"原姓氏 {token}",
            pending["token_hash"],
        )
        assert registry.token_matches(
            f"{token} suffix",
            pending["token_hash"],
        )
        assert token not in str(pending)

        await registry.save_binding(
            "user-1",
            "codeforces",
            profile,
            group_id="group-1",
            qq_name="小明",
        )
        assert await registry.get_group_member_ids("group-1") == ["user-1"]
        accounts = await registry.get_user_accounts("user-1")
        assert accounts["codeforces"]["handle"] == "demo"
        assert accounts["codeforces"]["qq_name"] == "小明"

        with pytest.raises(ValueError, match="其他 QQ 用户"):
            await registry.save_binding(
                "user-2",
                "codeforces",
                profile,
            )

        await registry.set_group_member("group-1", "user-1", False)
        assert await registry.get_group_member_ids("group-1") == []

    asyncio.run(scenario())


def test_profile_and_ranking_html_escape_user_content(tmp_path):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    profile = AccountProfile(
        platform="codeforces",
        handle="<demo>",
        rating=1800,
        rank_text="expert",
    )
    profile_html = renderer._profile_html(
        [profile],
        display_name="<用户>",
        weekly_changes={"codeforces": 20},
    )
    ranking_html = renderer._ranking_html(
        [
            {
                "display_name": "<用户>",
                "handle": "<demo>",
                "value": 1800,
                "delta": 20,
            }
        ],
        title="排行",
        subtitle="测试",
        metric_label="Rating",
        note="备注",
    )
    assert "&lt;demo&gt;" in profile_html
    assert "&lt;用户&gt;" in profile_html
    assert "&lt;demo&gt;" in ranking_html
    assert "ACM // NEURAL CONTEST NETWORK" in profile_html
    assert "ACM // NEURAL CONTEST NETWORK" in ranking_html


def test_profile_card_height_and_single_layout_are_adaptive(tmp_path):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    profile = AccountProfile(
        platform="codeforces",
        handle="tourist",
        rating=3824,
        rank_text="legendary grandmaster",
        max_rating=3979,
        rating_rank=1,
        contest_count=180,
        recent_contests=[
            {"name": "Codeforces Round #999", "delta": 42}
        ],
    )
    source_one = {
        "kind": "profile",
        "profiles": [profile.public_dict()],
    }
    source_two = {
        "kind": "profile",
        "profiles": [profile.public_dict(), profile.public_dict()],
    }
    source_four = {
        "kind": "profile",
        "profiles": [profile.public_dict() for _ in range(4)],
    }
    one_height = renderer._estimate_height(source_one)
    two_height = renderer._estimate_height(source_two)
    four_height = renderer._estimate_height(source_four)

    assert one_height < 760
    assert two_height < 760
    assert four_height > two_height

    profile_html = renderer._profile_html(
        [profile],
        display_name="测试用户",
        weekly_changes={"codeforces": 42},
    )
    assert 'class="page profile-document"' in profile_html
    assert 'class="profile-grid profile-single"' in profile_html
    assert 'class="profile-main"' in profile_html
    assert 'class="profile-meta"' in profile_html


def test_profile_renderer_uses_png_temp_suffix(tmp_path, monkeypatch):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    profile = AccountProfile(
        platform="codeforces",
        handle="demo",
        rating=1200,
    )
    seen_suffixes = []

    def fake_render(kind, executable, html_path, image_path, height):
        seen_suffixes.append(image_path.suffix)
        image_path.write_bytes(b"fake-png")
        return True

    monkeypatch.setattr(
        AdaptiveOutputRenderer,
        "_find_renderers",
        staticmethod(lambda: [("chromium", "fake-browser")]),
    )
    monkeypatch.setattr(
        AdaptiveOutputRenderer,
        "_run_external_renderer",
        staticmethod(fake_render),
    )

    image_path = renderer.render_profile([profile])

    assert image_path is not None
    assert image_path.is_file()
    assert seen_suffixes == [".png"]
