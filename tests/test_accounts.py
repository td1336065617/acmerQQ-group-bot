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
            "avatar": "https://userpic.codeforces.org/1/avatar/demo.jpg",
            "titlePhoto": "https://userpic.codeforces.org/1/title/demo.jpg",
            "rating": 1800,
            "rank": "expert",
            "maxRating": 1900,
            "organization": "Example University",
        }
    )
    assert cf_profile.handle == "demo"
    assert cf_profile.verification_value == "ACM-TOKEN"
    assert cf_profile.rating == 1800
    assert (
        cf_profile.avatar_url
        == "https://userpic.codeforces.org/1/avatar/demo.jpg"
    )

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
    assert (
        AccountFetcher._extract_atcoder_avatar(
            "<img class='avatar' src='//img.atcoder.jp/assets/icon/avatar.png'>"
        )
        == "//img.atcoder.jp/assets/icon/avatar.png"
    )

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


def test_codeforces_difficulty_distribution_deduplicates_accepted_problems():
    rows = [
        {
            "id": 1,
            "verdict": "OK",
            "problem": {"contestId": 1, "index": "A", "rating": 800},
        },
        {
            "id": 2,
            "verdict": "WRONG_ANSWER",
            "problem": {"contestId": 1, "index": "A", "rating": 800},
        },
        {
            "id": 3,
            "verdict": "OK",
            "problem": {"contestId": 1, "index": "A", "rating": 800},
        },
        {
            "id": 4,
            "verdict": "OK",
            "problem": {"contestId": 2, "index": "B", "rating": 1200},
        },
        {
            "id": 5,
            "verdict": "OK",
            "problem": {"contestId": 3, "index": "C"},
        },
        {
            "id": 6,
            "verdict": "OK",
            "problem": {"contestId": 4, "index": "D", "rating": 2400},
        },
        {
            "id": 7,
            "verdict": "OK",
            "problem": {
                "problemsetName": "练习集",
                "index": "A",
                "rating": 1600,
            },
        },
        {
            "id": 8,
            "verdict": "OK",
            "problem": {
                "problemsetName": "练习集",
                "index": "A",
                "rating": 1600,
            },
        },
    ]

    distribution, solved_count = (
        AccountFetcher._parse_cf_difficulty_distribution(rows)
    )

    assert solved_count == 5
    assert distribution == [
        {"label": "≤999", "count": 1},
        {"label": "1200–1399", "count": 1},
        {"label": "1600–1799", "count": 1},
        {"label": "2400–2599", "count": 1},
        {"label": "未标分", "count": 1},
    ]


def test_codeforces_difficulty_distribution_splits_high_ratings():
    rows = [
        {
            "id": index,
            "verdict": "OK",
            "problem": {
                "contestId": index,
                "index": "A",
                "rating": rating,
            },
        }
        for index, rating in enumerate(
            (2400, 2599, 2600, 2799, 2800, 2999, 3000, 3199, 3200),
            start=1,
        )
    ]

    distribution, solved_count = (
        AccountFetcher._parse_cf_difficulty_distribution(rows)
    )

    assert solved_count == 9
    assert distribution == [
        {"label": "2400–2599", "count": 2},
        {"label": "2600–2799", "count": 2},
        {"label": "2800–2999", "count": 2},
        {"label": "3000–3199", "count": 2},
        {"label": "3200+", "count": 1},
    ]


def test_codeforces_detail_fetches_difficulty_without_recent_submission_list(
    monkeypatch,
):
    async def scenario():
        fetcher = AccountFetcher()
        calls = []

        async def fake_cf_json(method, params):
            calls.append((method, params))
            if method == "user.info":
                return {
                    "status": "OK",
                    "result": [{"handle": "demo", "rating": 1800}],
                }
            if method == "user.rating":
                return {"status": "OK", "result": []}
            if method == "user.status":
                return {
                    "status": "OK",
                    "result": [
                        {
                            "verdict": "OK",
                            "problem": {
                                "contestId": 1,
                                "index": "A",
                                "rating": 1200,
                            },
                        }
                    ],
                }
            raise AssertionError(method)

        monkeypatch.setattr(fetcher, "_cf_json", fake_cf_json)
        profile = await fetcher._fetch_codeforces(
            "demo",
            detail=True,
            include_submissions=False,
            include_difficulty=True,
        )

        assert profile.recent_submissions == []
        assert profile.solved_count == 1
        assert profile.difficulty_distribution == [
            {"label": "1200–1399", "count": 1}
        ]
        status_call = next(
            params for method, params in calls if method == "user.status"
        )
        assert status_call["count"] == "10000"

    asyncio.run(scenario())


def test_luogu_profile_and_verification_use_com_introduction(monkeypatch):
    async def scenario():
        fetcher = AccountFetcher()
        calls = []

        async def fake_fetch_text(url, *, headers=None, retries=2):
            calls.append((url, headers))
            return """
            <script id="lentille-context" type="application/json">
            {"status":200,"data":{"user":{
              "uid":1770958,
              "name":"demo",
              "introduction":"旧内容 ACM-TOKEN",
              "ranking":321,
              "passedProblemCount":12
            }}}
            </script>
            """

        monkeypatch.setattr(fetcher, "_fetch_text", fake_fetch_text)
        profile = await fetcher._fetch_luogu("1770958", detail=False)
        value = await fetcher.get_verification_value(
            "luogu",
            "1770958",
            profile=profile,
            force=True,
        )

        assert profile.profile_url == "https://www.luogu.com/user/1770958"
        assert profile.rating_rank == 321
        assert profile.solved_count == 12
        assert value == "旧内容 ACM-TOKEN"
        assert calls
        assert calls[0][0] == "https://www.luogu.com/user/1770958"

    asyncio.run(scenario())


def test_luogu_com_empty_introduction_is_a_valid_empty_field(monkeypatch):
    async def scenario():
        fetcher = AccountFetcher()

        async def fake_fetch_text(url, *, headers=None, retries=2):
            assert url == "https://www.luogu.com/user/1928724"
            return """
            <script id="lentille-context" type="application/json">
            {"status":200,"data":{"user":{
              "uid":1928724,
              "name":"LovELolita",
              "introduction":"",
              "ranking":54082,
              "passedProblemCount":246
            }}}
            </script>
            """

        monkeypatch.setattr(fetcher, "_fetch_text", fake_fetch_text)
        profile = await fetcher.get_profile(
            "luogu",
            "1928724",
            detail=False,
            force=True,
        )

        assert profile.handle == "LovELolita"
        assert profile.profile_url == "https://www.luogu.com/user/1928724"
        assert profile.rating_rank == 54082
        assert profile.solved_count == 246
        assert profile.verification_value == ""

    asyncio.run(scenario())


def test_luogu_falls_back_to_com_cn_when_com_has_no_target_user(monkeypatch):
    async def scenario():
        fetcher = AccountFetcher()
        requested = []

        async def fake_fetch_text(url, *, headers=None, retries=2):
            requested.append(url)
            if url == "https://www.luogu.com/user/1770958":
                return "<html>暂时没有用户数据</html>"
            return """
            <script id="lentille-context" type="application/json">
            {"data":{"user":{
              "uid":1770958,
              "name":"legacy-demo",
              "introduction":"legacy intro",
              "ranking":321
            }}}
            </script>
            """

        monkeypatch.setattr(fetcher, "_fetch_text", fake_fetch_text)
        profile = await fetcher.get_profile(
            "luogu",
            "1770958",
            detail=False,
            force=True,
        )

        assert requested[:2] == [
            "https://www.luogu.com/user/1770958",
            "https://www.luogu.com.cn/user/1770958",
        ]
        assert profile.handle == "legacy-demo"
        assert profile.source_url == "https://www.luogu.com.cn/user/1770958"

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
    assert "ELYSIAN // PINK PEARL ARCHIVE" in profile_html
    assert "ELYSIAN // PINK PEARL ARCHIVE" in ranking_html
    assert "成员 / 账号" in ranking_html
    assert "近7日变化" in ranking_html


def test_progress_ranking_uses_current_value_in_fourth_column(tmp_path):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    rows = [
        {
            "display_name": "用户",
            "handle": "demo",
            "value": 20,
            "display_value": "+20",
            "metric_label": "近7日变化",
            "delta": 20,
            "current_display_value": "1800",
        }
    ]

    ranking_html = renderer._ranking_html(
        rows,
        title="进步榜",
        subtitle="测试",
        metric_label="近7日变化",
        note="",
        value_header="近7日变化",
        secondary_label="当前指标",
        secondary_value_key="current_display_value",
    )

    assert "当前指标" in ranking_html
    assert "1800" in ranking_html
    assert ">+20<" in ranking_html


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


def test_profile_card_includes_square_avatar_and_group_rank(tmp_path):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    profile = AccountProfile(
        platform="codeforces",
        handle="demo",
        rating=1800,
        avatar_url="//cdn.example.com/avatar.jpg",
    )
    profile_html = renderer._profile_html(
        [profile],
        display_name="测试用户",
        weekly_changes={"codeforces": 20},
        group_ranks={"codeforces": {"rank": 12, "total": 2000}},
    )

    assert 'class="avatar-wrap"' in profile_html
    assert "https://cdn.example.com/avatar.jpg" in profile_html
    assert "object-fit:cover" in profile_html
    assert "aspect-ratio:1 / 1" in profile_html
    assert "本群排行：第" in profile_html


def test_profile_card_includes_dense_stats_and_difficulty_distribution(
    tmp_path,
):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    profile = AccountProfile(
        platform="codeforces",
        handle="demo",
        rating=1800,
        max_rating=2000,
        max_rank_text="candidate master",
        contest_count=80,
        solved_count=5,
        contribution=12,
        difficulty_distribution=[
            {"label": "≤999", "count": 1},
            {"label": "1200–1399", "count": 2},
            {"label": "1600–1799", "count": 1},
            {"label": "未标分", "count": 1},
        ],
    )
    profile.extra = {
        "difficulty_scan_limit": 10000,
        "difficulty_scanned_submissions": 10000,
    }

    profile_html = renderer._profile_html(
        [profile],
        display_name="测试用户",
        weekly_changes={"codeforces": 20},
    )

    assert "已统计题数" in profile_html
    assert "贡献" in profile_html
    assert "最高段位" in profile_html
    assert "CF 做题分布" in profile_html
    assert "1200–1399" in profile_html
    assert "近 10000 条提交" in profile_html


def test_profile_card_includes_charts_and_adaptive_chart_height(tmp_path):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    profile = AccountProfile(
        platform="codeforces",
        handle="demo",
        rating=1820,
        difficulty_distribution=[
            {"label": "≤999", "count": 12},
            {"label": "1200–1399", "count": 18},
            {"label": "1600–1799", "count": 9},
            {"label": "2400–2599", "count": 2},
            {"label": "3200+", "count": 1},
            {"label": "未标分", "count": 3},
        ],
        rating_history=[
            {"rating": 1500, "timestamp": 1},
            {"rating": 1610, "timestamp": 2},
            {"rating": 1580, "timestamp": 3},
            {"rating": 1820, "timestamp": 4},
        ],
    )

    profile_html = renderer._profile_html(
        [profile],
        display_name="测试用户",
        weekly_changes={"codeforces": 30},
    )

    assert 'class="difficulty-bars"' in profile_html
    assert 'class="difficulty-track"' in profile_html
    assert "2400–2599" in profile_html
    assert "3200+" in profile_html
    assert 'class="rating-chart"' in profile_html
    assert "<polyline" in profile_html
    assert "最新 1820" in profile_html
    assert renderer._estimate_height(
        {"kind": "profile", "profiles": [profile.public_dict()]}
    ) > 760


def test_pillow_profile_fallback_renders_charts(tmp_path):
    pytest.importorskip("PIL")
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    profile = AccountProfile(
        platform="codeforces",
        handle="demo",
        rating=1800,
        difficulty_distribution=[
            {"label": "1200–1399", "count": 4},
            {"label": "1800–1999", "count": 2},
            {"label": "3000–3199", "count": 1},
        ],
        rating_history=[
            {"rating": 1500, "timestamp": 1},
            {"rating": 1700, "timestamp": 2},
            {"rating": 1800, "timestamp": 3},
        ],
    )
    image_path = tmp_path / "profile-fallback.png"

    assert renderer._pillow_profile(
        [profile],
        "测试用户",
        {"codeforces": 20},
        {},
        tmp_path,
        image_path,
    )
    from PIL import Image

    with Image.open(image_path) as image:
        assert image.size[0] == 1200
        assert image.size[1] > 760


def test_profile_card_avatar_url_normalization():
    assert (
        AccountCardRenderer._normalize_avatar_url(
            "//cdn.example.com/avatar.jpg",
            "codeforces",
        )
        == "https://cdn.example.com/avatar.jpg"
    )
    assert (
        AccountCardRenderer._normalize_avatar_url(
            "/avatar.png",
            "luogu",
        )
        == "https://www.luogu.com/avatar.png"
    )
    assert AccountCardRenderer._normalize_avatar_url("not-a-url") == ""


def test_avatar_preload_failure_falls_back_to_placeholder(tmp_path, monkeypatch):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    profile = AccountProfile(
        platform="codeforces",
        handle="demo",
        avatar_url="https://cdn.example.com/avatar.jpg",
    )

    def broken_load(*args, **kwargs):
        raise RuntimeError("avatar decoder down")

    def broken_download(*args, **kwargs):
        raise RuntimeError("avatar network down")

    monkeypatch.setattr(renderer, "_load_avatar", broken_load)
    monkeypatch.setattr(renderer, "_download_avatar_data_url", broken_download)

    assert renderer._avatar_html_source(profile) == ""


def test_card_renderer_failure_returns_none(tmp_path, monkeypatch):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    profile = AccountProfile(platform="codeforces", handle="demo")

    def broken_external(*args, **kwargs):
        raise RuntimeError("browser crashed")

    def broken_fallback(*args, **kwargs):
        raise RuntimeError("pillow crashed")

    monkeypatch.setattr(
        AdaptiveOutputRenderer,
        "_find_renderers",
        staticmethod(lambda: [("chromium", "fake-browser")]),
    )
    monkeypatch.setattr(
        AdaptiveOutputRenderer,
        "_run_external_renderer",
        staticmethod(broken_external),
    )
    monkeypatch.setattr(
        AccountCardRenderer,
        "_pillow_profile",
        classmethod(lambda cls, *args, **kwargs: broken_fallback()),
    )

    assert renderer.render_profile([profile]) is None


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


def test_ranking_card_height_covers_wrapped_rows_and_notes(tmp_path):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    rows = [
        {
            "display_name": "用户" + "长昵称" * 12,
            "handle": "contest_user_" + "long_handle_" * 8,
            "value": 3000,
            "display_value": "3000",
            "metric_label": "Rating",
            "delta": 20,
        }
    ] * 30
    note = "共 30 名成员 · 当前显示第 1-30 名 · 下一页：群cf排行 2"

    assert renderer._ranking_row_height(rows[0]) > 104
    assert renderer._ranking_height(rows, note=note) > 3500
    assert renderer._estimate_height(
        {"kind": "ranking", "rows": rows, "note": note}
    ) == renderer._ranking_height(rows, note=note)


def test_overview_card_height_is_based_on_sections_and_note(tmp_path):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    sections = {
        platform: [
            {
                "display_name": f"用户{i}",
                "handle": f"user{i}",
                "value": 3000 - i,
                "display_value": str(3000 - i),
                "delta": i,
            }
            for i in range(5)
        ]
        for platform in ("codeforces", "nowcoder", "atcoder", "luogu")
    }
    height = renderer._estimate_height(
        {
            "kind": "overview",
            "sections": sections,
            "note": "各平台分开排行，完整榜单请使用对应平台排行指令",
        }
    )

    assert height > 1200
    assert height < 1800
    overview_html = renderer._overview_html(
        sections,
        title="群排行",
        subtitle="测试",
        metric_label="Rating",
        note="测试备注",
    )
    assert "mini-header" in overview_html
    assert "当前指标" in overview_html
    assert "近7日变化" in overview_html


def test_pillow_overview_layout_uses_previous_row_max_height(tmp_path):
    renderer = AccountCardRenderer(cache_dir=tmp_path)
    sections = {
        "codeforces": [{} for _ in range(5)],
        "nowcoder": [{} for _ in range(5)],
        "luogu": [{}],
        "atcoder": [{}],
    }

    items, section_heights, row_tops = renderer._pillow_overview_layout(
        sections
    )

    assert [platform for platform, _ in items] == [
        "codeforces",
        "nowcoder",
        "luogu",
        "atcoder",
    ]
    assert section_heights[:2] == [433, 433]
    assert section_heights[2:] == [153, 153]
    assert row_tops[1] >= row_tops[0] + max(section_heights[:2]) + 24


def test_registry_bulk_rating_snapshots_and_deltas():
    async def scenario():
        plugin = FakePlugin()
        registry = AccountRegistry(plugin)
        now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc).timestamp()

        await registry.record_ratings(
            [
                ("user-1", "codeforces", 1800),
                ("user-2", "codeforces", 1600),
            ]
        )
        snapshots = await registry.get_weekly_deltas(
            [("user-1", "codeforces"), ("user-2", "codeforces")],
            now=now,
        )
        assert snapshots[("user-1", "codeforces")] is None
        assert snapshots[("user-2", "codeforces")] is None

        data = await plugin.get_kv_data("account_rating_snapshots", {})
        old_time = now - 8 * 86400
        data["user-1"]["codeforces"].insert(
            0,
            {"timestamp": old_time, "rating": 1700},
        )
        data["user-2"]["codeforces"].insert(
            0,
            {"timestamp": old_time, "rating": 1500},
        )
        await plugin.put_kv_data("account_rating_snapshots", data)

        snapshots = await registry.get_weekly_deltas(
            [("user-1", "codeforces"), ("user-2", "codeforces")],
            now=now,
        )
        assert snapshots == {
            ("user-1", "codeforces"): 100,
            ("user-2", "codeforces"): 100,
        }

    asyncio.run(scenario())
