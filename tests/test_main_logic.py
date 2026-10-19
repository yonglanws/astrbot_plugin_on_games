"""main.py 逻辑测试：用最小 AstrBot stub 验证导入、索引与答案判定。

不启动真实 AstrBot，也不发送消息；只验证插件核心逻辑与题库索引。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = __import__("logging").getLogger("astrbot_stub")
    api.AstrBotConfig = dict
    api.__path__ = []  # type: ignore[attr-defined]

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # noqa: D401
        pass

    class EventMessageType:
        ALL = 0

    class _Filter:
        def command(self, name, alias=None):
            def deco(func):
                return func

            return deco

        def event_message_type(self, *args, **kwargs):
            def deco(func):
                return func

            return deco

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.EventMessageType = EventMessageType
    event_mod.filter = _Filter()

    star_mod = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context
            self.name = "astrbot_plugin_on_games"

    def register(*args, **kwargs):
        def deco(cls):
            return cls

        return deco

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name):
            return Path(__import__("tempfile").gettempdir()) / "on_games_stub" / plugin_name

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register
    star_mod.StarTools = StarTools

    comp_mod = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class Image:
        def __init__(self, file=""):
            self.file = file

    class Record:
        def __init__(self, file=""):
            self.file = file

    class At:
        def __init__(self, qq=""):
            self.qq = qq

    comp_mod.Plain = Plain
    comp_mod.Image = Image
    comp_mod.Record = Record
    comp_mod.At = At

    core = types.ModuleType("astrbot.core")
    core.__path__ = []  # type: ignore[attr-defined]
    utils = types.ModuleType("astrbot.core.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    waiter = types.ModuleType("astrbot.core.utils.session_waiter")

    class SessionFilter:
        def filter(self, event):
            return ""

    class SessionController:
        def stop(self):
            pass

    def session_waiter(*args, **kwargs):
        def deco(func):
            return func

        return deco

    waiter.SessionFilter = SessionFilter
    waiter.SessionController = SessionController
    waiter.session_waiter = session_waiter

    astrbot.api = api
    astrbot.core = core
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.message_components"] = comp_mod
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.utils"] = utils
    sys.modules["astrbot.core.utils.session_waiter"] = waiter


_install_astrbot_stubs()

import main as plugin_main  # noqa: E402


def _config(**overrides) -> dict:
    config = {
        "update_interval_hours": 24,
        "request_timeout": 90,
        "download_retries": 3,
        "download_concurrency": 4,
        "asset_base_url": "https://assets.bdon.moe",
        "asset_language": "zh-Hans",
        "prefetch_assets": False,
        "prefetch_concurrency": 2,
        "asset_cache_max_mb": 1024,
        "answer_timeout": 5,
        "daily_play_limit": 10,
        "daily_limit_per_game": False,
        "game_cooldown_seconds": 0,
        "max_guess_attempts": 10,
        "song_guesses_per_user": 2,
        "reward_valid_time": 0,
        "clip_duration_seconds": 10,
        "ranking_display_count": 10,
        "super_users": [],
        "blacklist": [],
        "group_whitelist": [],
        "whitelist_reject_message": "",
        "debug_mode": False,
        "card_effects": {},
        "jacket_effects": {},
        "connect_link_template": plugin_main.DEFAULT_CONNECT_TEMPLATE,
        "quick_entries": [],
    }
    config.update(overrides)
    return config


@pytest.mark.asyncio
async def test_plugin_index_and_answer_logic(tmp_path, monkeypatch):
    monkeypatch.setattr(
        plugin_main.StarTools, "get_data_dir", lambda name: tmp_path / name
    )

    plugin = plugin_main.OnGamesPlugin(object(), _config())
    try:
        for _ in range(240):
            if plugin._data_ready:
                break
            await asyncio.sleep(0.25)
        assert plugin._data_ready, "题库未在限定时间内就绪"
        assert len(plugin.songs) >= 80
        assert len(plugin.cards) >= 80
        assert len(plugin.characters) >= 20

        # 猜卡面卡池只保留 SR 以上稀有度（R 卡被排除）
        for card in plugin.cards:
            assert card["rarity"] >= plugin_main.MIN_CARD_RARITY, card
        assert {c["rarity_label"] for c in plugin.cards} <= {"SR", "SSR", "EX", "BD"}

        # 归一化
        assert plugin_main._normalize_answer("  高松灯！ ") == "高松灯"
        assert plugin_main._normalize_answer("Ａｎｏｎ") == "anon"

        # 卡面：角色 1 是 高松灯，角色 2 是 千早爱音
        card_1 = next(
            c for c in plugin.cards if c["kind"] == "member" and 1 in c["character_ids"]
        )
        assert plugin._check_card_answer(card_1, "灯")
        assert plugin._check_card_answer(card_1, "高松灯")
        assert plugin._check_card_answer(card_1, "tomori")
        assert not plugin._check_card_answer(card_1, "爱音")

        # 支援卡：多角色卡任意一位都算对
        support = next(
            c for c in plugin.cards if c["kind"] == "support" and len(c["character_ids"]) > 1
        )
        for char_id in support["character_ids"]:
            name = next(c["name"] for c in plugin.characters if c["id"] == char_id)
            assert plugin._check_card_answer(support, name)

        # 歌曲：官方标题、日文标题、拼音与模糊匹配
        song = plugin.songs[0]
        assert plugin._check_song_answer(song, song["title"])
        if song.get("title_ja"):
            assert plugin._check_song_answer(song, song["title_ja"])
        assert not plugin._check_song_answer(song, "完全不存在的歌名")

        if plugin_main.PYPINYIN_AVAILABLE:
            assert plugin._is_known_song_answer("mixingjiao")
            assert plugin._check_song_answer(song, "mixingjiao")

        # 官方标题的轻微错字可以通过模糊匹配；其他歌曲的精确标题不会算对
        song_long = next(s for s in plugin.songs if s["id"] == 100041)
        assert plugin._is_known_song_answer("残酷天使のテーゼ")
        assert plugin._check_song_answer(song_long, "残酷天使のテーゼ")
        song_a = next(s for s in plugin.songs if s["id"] == 100001)
        song_b = next(s for s in plugin.songs if s["id"] == 100002)
        assert not plugin._check_song_answer(song_a, song_b["title"])

        # 人物：master 官方名称/短名/英文名/组合名拆分 + 别名库
        assert plugin._character_alias_map.get("高松灯") == {1}
        assert plugin._character_alias_map.get("灯") == {1}
        assert plugin._character_alias_map.get("tomori") == {1}
        assert plugin._character_alias_map.get("takamatsu") == {1}
        assert plugin._character_alias_map.get("三角初华") == {6}
        assert plugin._character_alias_map.get("doloris") == {6}
        assert plugin._character_alias_map.get("uika") == {6}
        assert plugin._character_alias_map.get("偷摸零") is None

        # 别名库：中文变体、罗马音全拼与缩写
        assert plugin._character_alias_map.get("长崎素世") == {4}
        assert plugin._character_alias_map.get("素世") == {4}
        assert plugin._character_alias_map.get("soyo") == {4}
        assert plugin._character_alias_map.get("soyorin") == {4}
        assert plugin._character_alias_map.get("tmr") == {1}
        assert plugin._character_alias_map.get("drls") == {6}
        assert plugin._character_alias_map.get("mtsm") == {7}
        assert plugin._character_alias_map.get("kanata") == {1001}
        assert plugin._character_alias_map.get("knt") == {1001}
        assert plugin._character_alias_map.get("kanade") is None
        assert plugin._character_alias_map.get("knd") is None

        # 歌曲：官方标题 + 英文单词拆分
        assert plugin._song_exact_map.get("mayoiuta") == {100001}
        assert plugin._song_exact_map.get("charles") == {100019}
        assert plugin._check_song_answer(
            next(s for s in plugin.songs if s["id"] == 100019), "charles"
        )

        # 官机连接模板
        link = plugin._build_connect_link("on猜歌", "123456")
        assert "qqbot-cmd-input" in link
        assert "on%E7%8C%9C%E6%AD%8C" in link

        # 角色别名库：结构完整，ID 全部能在题库里找到，且不含日文假名
        alias_file = plugin.resources_dir / plugin_main.CHARACTER_ALIAS_FILE
        assert alias_file.exists()
        raw = json.loads(alias_file.read_text(encoding="utf-8"))
        known_ids = {c["id"] for c in plugin.characters}
        for key, names in raw["characters"].items():
            assert int(key) in known_ids, key
            assert isinstance(names, list) and names
            for name in names:
                assert not re.search(r"[\u3040-\u30ff]", name), name

        # 每个角色都至少有一个别名，且都能在别名表中命中
        for char in plugin.characters:
            assert plugin._character_alias_map.get(
                plugin_main._normalize_answer(char["short_name"])
            ), char["name"]

        # 群白名单
        class _Event:
            def get_group_id(self):
                return "999"

        plugin.config["group_whitelist"] = ["999"]
        assert plugin._is_group_allowed(_Event())
        plugin.config["group_whitelist"] = ["111"]
        assert not plugin._is_group_allowed(_Event())

        # 显示名
        assert plugin._get_display_name("u1", "小明") == "小明"
        plugin.config["blacklist"] = ["u1"]
        assert plugin._get_display_name("u1", "小明") == "[此用户已被BOT拉黑]"
    finally:
        await plugin.terminate()
