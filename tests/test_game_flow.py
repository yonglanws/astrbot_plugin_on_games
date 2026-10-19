"""端到端游戏流程测试：用 stub 模拟 AstrBot 事件与会话等待器。

覆盖：
- 猜歌：下载音频、截取、12 选项图、正确答案计分、结果与答案图
- 猜卡面：下载卡面、效果、角色名判定、正确答案计分
- 猜曲绘：下载曲绘、效果、歌名判定、正确答案计分
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- AstrBot stubs

CURRENT_PLUGIN = None
CURRENT_SESSION = ""
SIMULATED_ANSWERS: list = []
ANSWER_EVENT_FACTORY = None


def _install_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = __import__("logging").getLogger("astrbot_stub")
    api.AstrBotConfig = dict
    api.__path__ = []  # type: ignore[attr-defined]

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
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
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    def session_waiter(timeout=30, **kwargs):
        def deco(func):
            async def wrapper(event, session_filter=None, **kw):
                controller = SessionController()
                for item in list(SIMULATED_ANSWERS):
                    if controller.stopped:
                        break
                    if item == "__CORRECT__":
                        if ANSWER_EVENT_FACTORY is None:
                            continue
                        item = ANSWER_EVENT_FACTORY()
                        if item is None:
                            continue
                    await func(controller, item)
                return None

            return wrapper

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


_install_stubs()

import main as plugin_main  # noqa: E402

# ---------------------------------------------------------------- fake events


class FakeResult:
    def __init__(self):
        self.chain = []
        self.markdown = False

    def use_markdown(self, value=True):
        self.markdown = value


class FakeEvent:
    def __init__(
        self,
        text: str,
        *,
        user_id: str = "10001",
        name: str = "玩家1",
        group: str = "123456",
        platform: str = "aiocqhttp",
    ):
        self.message_str = text
        self.unified_msg_origin = f"{platform}:GroupMessage:{group}"
        self.message_obj = SimpleNamespace(self_id="999999", message=[])
        self._user_id = user_id
        self._name = name
        self._group = group
        self._platform = platform
        self.sent: list = []

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._user_id

    def get_sender_name(self):
        return self._name

    def get_platform_name(self):
        return self._platform

    def plain_result(self, text):
        return ("plain", text)

    def image_result(self, path):
        return ("image", str(path))

    def chain_result(self, chain):
        return ("chain", chain)

    def make_result(self):
        return FakeResult()

    async def send(self, result):
        self.sent.append(result)


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
        "daily_play_limit": -1,
        "daily_limit_per_game": False,
        "game_cooldown_seconds": 0,
        "max_guess_attempts": 10,
        "song_guesses_per_user": 2,
        "reward_valid_time": 0,
        "clip_duration_seconds": 5,
        "clip_start_ratio_min": 0.1,
        "clip_start_ratio_max": 0.5,
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


async def _run_game(plugin, event, game_impl):
    """运行一局，返回所有发送结果。"""
    async for _ in game_impl(event):
        pass
    return event.sent


@pytest.mark.asyncio
async def test_full_game_flow(tmp_path, monkeypatch):
    global CURRENT_PLUGIN, CURRENT_SESSION, SIMULATED_ANSWERS, ANSWER_EVENT_FACTORY

    monkeypatch.setattr(
        plugin_main.StarTools, "get_data_dir", lambda name: tmp_path / name
    )

    plugin = plugin_main.OnGamesPlugin(object(), _config())
    try:
        for _ in range(240):
            if plugin._data_ready:
                break
            await asyncio.sleep(0.25)
        assert plugin._data_ready

        CURRENT_PLUGIN = plugin

        def make_correct_answer_event() -> FakeEvent | None:
            round_data = plugin.game_sessions.get(CURRENT_SESSION)
            if round_data is None:
                return None
            if round_data.game == "song":
                text = str(round_data.correct_answer_num)
            elif round_data.game == "card":
                names = round_data.card.get("character_names") or []
                text = names[0] if names else ""
            else:
                text = round_data.song["title"] if round_data.song else ""
            if not text:
                return None
            return FakeEvent(
                text, user_id="20001", name="答题者", group="123456"
            )

        ANSWER_EVENT_FACTORY = make_correct_answer_event

        # ---------- 猜歌 ----------
        CURRENT_SESSION = "aiocqhttp:GroupMessage:123456"
        SIMULATED_ANSWERS = ["__CORRECT__"]
        initiator = FakeEvent("on猜歌", user_id="10001", name="发起者")
        sent = await _run_game(
            plugin, initiator, lambda e: plugin._guess_song_impl(e, "normal")
        )
        assert sent
        assert any(
            isinstance(item, tuple) and item[0] in ("plain", "chain")
            for item in sent
        )
        song_stats = plugin.db.get_user_game_stats(
            "20001", "aiocqhttp", "song"
        )
        assert song_stats and song_stats[0] >= 1 and song_stats[2] == 1

        # ---------- 猜卡面 ----------
        CURRENT_SESSION = "aiocqhttp:GroupMessage:123456"
        SIMULATED_ANSWERS = ["__CORRECT__"]
        initiator_card = FakeEvent("on猜卡面", user_id="10002", name="发起者2")
        sent_card = await _run_game(
            plugin, initiator_card, plugin._guess_card_impl
        )
        assert sent_card
        assert any(
            isinstance(item, tuple) and item[0] == "chain" for item in sent_card
        )
        card_stats = plugin.db.get_user_game_stats(
            "20001", "aiocqhttp", "card"
        )
        assert card_stats and card_stats[0] >= 1 and card_stats[2] == 1

        # ---------- 猜曲绘 ----------
        CURRENT_SESSION = "aiocqhttp:GroupMessage:123456"
        SIMULATED_ANSWERS = ["__CORRECT__"]
        initiator_jacket = FakeEvent("on猜曲绘", user_id="10003", name="发起者3")
        sent_jacket = await _run_game(
            plugin, initiator_jacket, plugin._guess_jacket_impl
        )
        assert sent_jacket
        assert any(
            isinstance(item, tuple) and item[0] == "chain" for item in sent_jacket
        )
        jacket_stats = plugin.db.get_user_game_stats(
            "20001", "aiocqhttp", "jacket"
        )
        assert jacket_stats and jacket_stats[0] >= 1 and jacket_stats[2] == 1

        # 三局都应消耗发起者的每日次数
        assert plugin.db.get_today_plays("10001", "aiocqhttp", "song") == 1
        assert plugin.db.get_today_plays("10002", "aiocqhttp", "card") == 1
        assert plugin.db.get_today_plays("10003", "aiocqhttp", "jacket") == 1

        # 只答题、未发起游戏的玩家也应出现在排行榜中，并显示其 QQ 名称
        ranking = plugin.db.get_ranking(20)
        name_by_id = {row[0]: row[1] for row in ranking}
        assert name_by_id.get("20001") == "答题者"
        song_ranking = plugin.db.get_ranking(20, "song")
        assert any(
            row[0] == "20001" and row[1] == "答题者" for row in song_ranking
        )
    finally:
        CURRENT_PLUGIN = None
        CURRENT_SESSION = ""
        SIMULATED_ANSWERS = []
        ANSWER_EVENT_FACTORY = None
        await plugin.terminate()


@pytest.mark.asyncio
async def test_score_ranking_help_commands(tmp_path, monkeypatch):
    """分数、排行榜、帮助、题库状态、自定义名称指令。"""
    monkeypatch.setattr(
        plugin_main.StarTools, "get_data_dir", lambda name: tmp_path / name
    )
    plugin = plugin_main.OnGamesPlugin(object(), _config())
    try:
        for _ in range(240):
            if plugin._data_ready:
                break
            await asyncio.sleep(0.25)
        assert plugin._data_ready

        for game in ("song", "card", "jacket"):
            plugin.db.update_stats("10001", "玩家1", "aiocqhttp", game, 5, True)
            plugin.db.update_user_play("10001", "玩家1", "aiocqhttp", game)

        async def collect(gen):
            return [item async for item in gen]

        total = await collect(
            plugin.show_user_score(FakeEvent("on分数", user_id="10001"))
        )
        assert any(
            isinstance(item, tuple) and item[0] == "plain" and "总分" in item[1]
            for item in total
        ), total
        assert any(
            isinstance(item, tuple) and "猜歌" in item[1] and "猜曲绘" in item[1]
            for item in total
        ), total

        song_only = await collect(
            plugin.show_user_score(FakeEvent("猜歌分数", user_id="10001"))
        )
        assert any(
            isinstance(item, tuple) and "猜歌数据" in item[1] for item in song_only
        ), song_only

        # 三个游戏的排行榜相互独立：不带游戏名时给出提示，不返回图片
        no_game = await collect(
            plugin.show_ranking(FakeEvent("on排行榜", user_id="10001"))
        )
        assert not any(
            isinstance(item, tuple) and item[0] == "image" for item in no_game
        ), no_game
        assert any(
            isinstance(item, tuple) and "分开" in item[1] for item in no_game
        ), no_game

        # 指定游戏后返回对应游戏的排行榜图片
        ranking = await collect(
            plugin.show_ranking(FakeEvent("猜歌排行榜", user_id="10001"))
        )
        assert any(
            isinstance(item, tuple) and item[0] == "image" for item in ranking
        ), ranking

        for game, command in (
            ("song", "猜歌排行榜"),
            ("card", "猜卡面排行榜"),
            ("jacket", "猜曲绘排行榜"),
        ):
            rows = plugin.db.get_ranking(20, game)
            assert rows and rows[0][3] == 5, (game, rows)

        # 题库状态不再显示版本号
        status_texts = [
            item[1]
            for item in await collect(
                plugin.show_master_status(FakeEvent("on题库状态", user_id="10001"))
            )
            if isinstance(item, tuple) and item[0] == "plain"
        ]
        assert status_texts and "题库状态" in status_texts[0]
        assert all("版本" not in text for text in status_texts), status_texts

        help_output = await collect(
            plugin.show_help(FakeEvent("on帮助", user_id="10001"))
        )
        assert any(
            isinstance(item, tuple)
            and item[0] == "plain"
            and "OurNotes 娱乐指南" in item[1]
            for item in help_output
        ), help_output

        custom = await collect(
            plugin.set_custom_name(
                FakeEvent("on自定义名称 小灯", user_id="10001")
            )
        )
        assert any(
            isinstance(item, tuple) and item[0] == "plain" and "已设置" in item[1]
            for item in custom
        ), custom
        assert plugin.db.get_profile("10001", "aiocqhttp")[1] == "小灯"
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_official_markdown_flow(tmp_path, monkeypatch):
    """QQ 官方机器人：@bot 应答、markdown 按钮与官机未绑定统计。"""
    global CURRENT_PLUGIN, CURRENT_SESSION, SIMULATED_ANSWERS, ANSWER_EVENT_FACTORY

    monkeypatch.setattr(
        plugin_main.StarTools, "get_data_dir", lambda name: tmp_path / name
    )
    plugin = plugin_main.OnGamesPlugin(object(), _config())
    try:
        for _ in range(240):
            if plugin._data_ready:
                break
            await asyncio.sleep(0.25)
        assert plugin._data_ready

        CURRENT_PLUGIN = plugin
        CURRENT_SESSION = "qq_official:GroupMessage:777777"

        def make_answer():
            round_data = plugin.game_sessions.get(CURRENT_SESSION)
            if not round_data or not round_data.song:
                return None
            event = FakeEvent(
                round_data.song["title"],
                user_id="official_qid",
                name="官机用户",
                group="777777",
                platform="qq_official",
            )
            event.message_obj.message = [plugin_main.Comp.At(qq="999999")]
            return event

        ANSWER_EVENT_FACTORY = make_answer
        SIMULATED_ANSWERS = ["__CORRECT__"]

        initiator = FakeEvent(
            "on猜曲绘",
            user_id="official_qid",
            name="官机用户",
            group="777777",
            platform="qq_official",
        )
        sent = await _run_game(plugin, initiator, plugin._guess_jacket_impl)
        assert sent

        markdown_results = [
            item for item in sent if isinstance(item, FakeResult) and item.markdown
        ]
        assert markdown_results, sent
        assert any(
            "qqbot-cmd-input" in part.text
            for result in markdown_results
            for part in result.chain
            if hasattr(part, "text")
        )
        # 尾部按钮：当前游戏（猜曲绘）的查分/排行榜，绑定与解绑置于最下面，且不带 on 前缀
        # 按钮文字经 URL 编码，先解码再断言。
        footer_texts = [
            unquote(part.text)
            for result in markdown_results
            for part in result.chain
            if hasattr(part, "text") and "qqbot-cmd-input" in part.text
        ]
        assert footer_texts
        joined = "\n".join(footer_texts)
        assert "猜曲绘个人分数" in joined
        assert "猜曲绘排行榜" in joined
        assert "on绑定" not in joined
        assert "on解绑" not in joined
        assert "on排行榜" not in joined
        assert "猜歌排行榜" not in joined
        # 绑定 / 解绑必须排在查分/排行榜按钮之后
        bind_index = next(
            i for i, t in enumerate(footer_texts) if "绑定QQ" in t or "解绑" in t
        )
        assert bind_index > 0, footer_texts

        stats = plugin.db.get_user_game_stats(
            "official_qid", "qq_official", "jacket"
        )
        assert stats and stats[2] == 1
    finally:
        CURRENT_PLUGIN = None
        CURRENT_SESSION = ""
        SIMULATED_ANSWERS = []
        ANSWER_EVENT_FACTORY = None
        await plugin.terminate()


@pytest.mark.asyncio
async def test_binding_flow(tmp_path, monkeypatch):
    """官机绑定/解绑：确认流程与三个游戏的分数迁移。"""
    global SIMULATED_ANSWERS

    monkeypatch.setattr(
        plugin_main.StarTools, "get_data_dir", lambda name: tmp_path / name
    )
    plugin = plugin_main.OnGamesPlugin(object(), _config())
    try:
        for _ in range(240):
            if plugin._data_ready:
                break
            await asyncio.sleep(0.25)
        assert plugin._data_ready

        for game in ("song", "card", "jacket"):
            plugin.db.update_stats(
                "official_qid", "官机用户", "qq_official", game, 5, True
            )

        bind_event = FakeEvent(
            "on绑定 12345678",
            user_id="official_qid",
            name="官机用户",
            group="888888",
            platform="qq_official",
        )
        SIMULATED_ANSWERS = [
            FakeEvent(
                "确认",
                user_id="official_qid",
                name="官机用户",
                group="888888",
                platform="qq_official",
            )
        ]
        bind_outputs = []
        async for out in plugin.bind_account(bind_event):
            bind_outputs.append(out)

        assert any(
            isinstance(item, tuple) and item[0] == "plain" and "绑定成功" in item[1]
            for item in bind_outputs
        ), bind_outputs
        assert plugin.db.resolve_user_id("qq_official", "official_qid") == "12345678"
        for game in ("song", "card", "jacket"):
            stats = plugin.db.get_user_game_stats("12345678", "aiocqhttp", game)
            assert stats and stats[0] == 5

        unbind_event = FakeEvent(
            "on解绑",
            user_id="official_qid",
            name="官机用户",
            group="888888",
            platform="qq_official",
        )
        SIMULATED_ANSWERS = [
            FakeEvent(
                "确认",
                user_id="official_qid",
                name="官机用户",
                group="888888",
                platform="qq_official",
            )
        ]
        unbind_outputs = []
        async for out in plugin.unbind_account(unbind_event):
            unbind_outputs.append(out)
        assert any(
            isinstance(item, tuple) and item[0] == "plain" and "解绑成功" in item[1]
            for item in unbind_outputs
        ), unbind_outputs
        assert plugin.db.resolve_user_id("qq_official", "official_qid") == "official_qid"
    finally:
        SIMULATED_ANSWERS = []
        await plugin.terminate()
