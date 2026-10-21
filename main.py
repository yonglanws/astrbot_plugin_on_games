
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from astrbot.api import logger
from PIL import Image

try:
    from pypinyin import Style, lazy_pinyin

    PYPINYIN_AVAILABLE = True
except ImportError:  # 可选依赖：没有 pypinyin 时仍可用模糊匹配
    Style = None  # type: ignore
    lazy_pinyin = None  # type: ignore
    PYPINYIN_AVAILABLE = False

try:
    import astrbot.api.message_components as Comp
    from astrbot.api import AstrBotConfig
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, StarTools, register
    from astrbot.core.utils.session_waiter import (
        SessionController,
        SessionFilter,
        session_waiter,
    )
except ImportError:  # 兼容旧版 AstrBot
    logger.error("Failed to import from astrbot.api, attempting fallback.")
    import astrbot.core.message_components as Comp  # type: ignore
    from astrbot.core.plugin import AstrMessageEvent, Context, filter, register
    from astrbot.core.plugin import Plugin as Star  # type: ignore
    from astrbot.core.utils.session_waiter import (  # type: ignore
        SessionController,
        SessionFilter,
        session_waiter,
    )

    class StarTools:  # type: ignore
        @staticmethod
        def get_data_dir(plugin_name: str) -> Path:
            return Path(__file__).parent.parent.parent.parent / "data" / "plugins_data" / plugin_name


try:
    from .services import (
        GAME_LABELS,
        GAMES,
        AssetService,
        AudioService,
        DatabaseService,
        HttpClient,
        ImageService,
        MasterDataService,
    )
    from .services.db_service import (
        DEFAULT_PLATFORM_NAME,
        MAX_BINDINGS_PER_QQ,
        OFFICIAL_PLATFORM_NAME,
    )
    from .services.master_data_service import MIN_CARD_RARITY
except ImportError:  # 以脚本方式加载（单测）时
    from services import (  # type: ignore
        GAME_LABELS,
        GAMES,
        AssetService,
        AudioService,
        DatabaseService,
        HttpClient,
        ImageService,
        MasterDataService,
    )
    from services.db_service import (  # type: ignore
        DEFAULT_PLATFORM_NAME,
        MAX_BINDINGS_PER_QQ,
        OFFICIAL_PLATFORM_NAME,
    )
    from services.master_data_service import MIN_CARD_RARITY  # type: ignore


# --- 插件元数据 ---
PLUGIN_NAME = "astrbot_plugin_on_games"
PLUGIN_AUTHOR = "慵懒午睡"
PLUGIN_DESCRIPTION = "OurNotes/MoeNotes 猜歌、猜卡面、猜曲绘三合一娱乐插件"
PLUGIN_VERSION = "1.0.3"
PLUGIN_REPO_URL = "https://github.com/yonglanws/astrbot_plugin_on_games"

GAME_SONG = "song"
GAME_CARD = "card"
GAME_JACKET = "jacket"

GAME_EMOJI = {
    GAME_SONG: "🎵",
    GAME_CARD: "🃏",
    GAME_JACKET: "🖼️",
}

# 官机 markdown 指令连接模板（与原插件一致）
DEFAULT_CONNECT_TEMPLATE = '<qqbot-cmd-input text="{encoded_command}" show="{encoded_name}" />'
_LEGACY_TEMPLATE_MARKERS = ("{encoded_at_text}", "mqqapi://")

# 角色别名库文件名（中文变体、罗马音与缩写；不含日文，日文名由 master 提供）
CHARACTER_ALIAS_FILE = "character_aliases.json"


def _normalized_session_id(event: AstrMessageEvent) -> str:
    """标准化会话 ID，处理部分适配器在群会话中带 user_id 前缀的情况。"""
    group_id = event.get_group_id()
    original = str(event.unified_msg_origin or "")
    parts = original.split(":", 2)
    if group_id and len(parts) == 3:
        return f"{parts[0]}:{parts[1]}:{group_id}"
    if len(parts) == 3 and "_" in parts[2]:
        return f"{parts[0]}:{parts[1]}:{parts[2].rsplit('_', 1)[-1]}"
    return original


def _normalize_answer(text: str) -> str:
    """答案归一化：全半角、大小写、空白与常见标点。"""
    value = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    value = value.lstrip("!！")
    value = re.sub(
        r"[\s\-_·・,，.。!！?？'\"“”‘’()（）\[\]【】<>《》~～:：;；/\\|]+",
        "",
        value,
    )
    return value


# 指令/唤醒前缀。不同 AstrBot 版本与适配器对 message_str 是否已剥离前缀处理不一，
# 这里统一再剥一次，保证 /猜歌、/1、/高松灯、/确认 都能正常使用。
_COMMAND_PREFIXES = ("/", "／", "!", "！")


def _strip_command_prefix(text: str) -> str:
    """去掉开头的一个或多个指令前缀（/、／、!、！）。"""
    value = str(text or "").strip()
    while value:
        for prefix in _COMMAND_PREFIXES:
            if value.startswith(prefix):
                value = value[len(prefix) :].lstrip()
                break
        else:
            break
    return value


def _command_aliases(*names: str) -> set[str]:
    """生成指令别名集合，同时提供带 / 前缀的写法。"""
    aliases: set[str] = set()
    for name in names:
        aliases.add(name)
        aliases.add(f"/{name}")
    return aliases


# 中文/日文短名（1-2 个字）也可以作为答案，英文名至少 3 个字符。
_CJK_NAME_PATTERN = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")


class GameSessionFilter(SessionFilter):
    """只接收同一会话（群/私聊）中的消息。"""

    def filter(self, event: AstrMessageEvent) -> str:
        return _normalized_session_id(event)


class BindingSessionFilter(SessionFilter):
    """只接收发起绑定的同一用户在同一会话中的确认消息。"""

    def __init__(self, session_id: str, user_id: str):
        self.session_id = str(session_id)
        self.user_id = str(user_id)

    def filter(self, event: AstrMessageEvent) -> str:
        if (
            str(event.unified_msg_origin) != self.session_id
            or str(event.get_sender_id()) != self.user_id
        ):
            return ""
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


@dataclass
class Round:
    """一轮游戏的状态。"""

    game: str
    score: int
    effect_name: str = ""
    song: Optional[dict] = None
    card: Optional[dict] = None
    mode_key: str = "normal"
    game_type: str = ""
    correct_answer_num: int = 0
    options: list[dict] = field(default_factory=list)
    display_answer: str = ""
    reveal_key: str = ""
    reveal_fallback_key: str = ""


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESCRIPTION, PLUGIN_VERSION, PLUGIN_REPO_URL)
class OnGamesPlugin(Star):  # type: ignore
    """OurNotes 三合一娱乐插件。"""

    def __init__(self, context: Context, config: "AstrBotConfig"):
        super().__init__(context)
        self.config = config
        self.plugin_dir = Path(__file__).parent
        self.resources_dir = self.plugin_dir / "res"

        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = self.data_dir / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # --- 网络 / 题库 / 资源 / 数据库 ---
        self.http = HttpClient(
            timeout=int(config.get("request_timeout", 60) or 60),
            concurrency=int(config.get("download_concurrency", 4) or 4),
            retries=int(config.get("download_retries", 3) or 3),
        )
        self.master_data = MasterDataService(
            self.data_dir,
            update_interval_hours=int(config.get("update_interval_hours", 24) or 24),
            request_timeout=int(config.get("request_timeout", 90) or 90),
            retries=int(config.get("download_retries", 3) or 3),
        )
        self.master_data.on_updated = self._on_master_updated

        self.assets = AssetService(self.data_dir, config, self.http)
        self.db = DatabaseService(str(self.data_dir / "on_games.db"))
        self.audio = AudioService(self.assets, config, self.output_dir)
        self.images = ImageService(
            self.resources_dir,
            self.output_dir,
            config,
            self._get_display_name,
        )

        # --- 题库索引 ---
        self.songs: list[dict] = []
        self.cards: list[dict] = []
        self.characters: list[dict] = []
        self._character_alias_map: dict[str, set[int]] = {}
        self._character_extra_aliases: dict[int, list[str]] = {}
        self._song_exact_map: dict[str, set[int]] = {}
        self._song_search_names: dict[int, list[str]] = {}
        self._data_ready = False

        # --- 游戏状态 ---
        self.active_sessions: set[str] = set()
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.last_game_end_time: dict[str, float] = {}
        self.game_sessions: dict[str, Round] = {}

        self._background_tasks: set[asyncio.Task] = set()
        self._init_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._stopping = False

        try:
            asyncio.get_running_loop()
            self._init_task = self._track_task(asyncio.create_task(self._initialize()))
            self._cleanup_task = self._track_task(
                asyncio.create_task(self._periodic_cleanup())
            )
        except RuntimeError:
            pass

        logger.info(f"OurNotes 娱乐插件已初始化 (v{PLUGIN_VERSION})")

    # ---------- 生命周期 ----------

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _load_character_aliases(self) -> dict[int, list[str]]:
        """从 res/character_aliases.json 读取角色别名（中文变体、罗马音与缩写）。"""
        path = self.resources_dir / CHARACTER_ALIAS_FILE
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 角色别名文件解析失败: {exc}")
            return {}
        raw = data.get("characters") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        result: dict[int, list[str]] = {}
        for key, value in raw.items():
            try:
                char_id = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                continue
            names = [str(v).strip() for v in value if str(v).strip()]
            if names:
                result[char_id] = names
        return result

    async def _initialize(self) -> None:
        try:
            await self.http.start()
            await asyncio.to_thread(self.db.init_db)
            await self.master_data.start()
            self._rebuild_indexes()

            if self._data_ready and bool(self.config.get("prefetch_assets", True)):
                keys: list[str] = []
                keys.extend(
                    s["jacket_key"] for s in self.songs if s.get("jacket_key")
                )
                keys.extend(c["image_key"] for c in self.cards if c.get("image_key"))
                self.assets.start_prefetch(keys)

            logger.info(
                f"[OurNotes] 数据就绪：{len(self.songs)} 首歌 / {len(self.cards)} 张卡 / "
                f"{len(self.characters)} 个角色"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 初始化失败: {exc}", exc_info=True)

    async def _periodic_cleanup(self) -> None:
        while not self._stopping:
            await asyncio.sleep(3600)
            try:
                self.assets.cleanup_output(self.output_dir)
                self.audio.cleanup_output()
                await asyncio.to_thread(self.assets.cleanup_cache)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[OurNotes] 周期清理失败: {exc}")

    async def init(self) -> None:
        if self._init_task is None:
            self._init_task = self._track_task(asyncio.create_task(self._initialize()))
        if self._cleanup_task is None:
            self._cleanup_task = self._track_task(
                asyncio.create_task(self._periodic_cleanup())
            )

    async def terminate(self) -> None:
        logger.info("[OurNotes] 正在关闭插件…")
        self._stopping = True
        tasks = [t for t in self._background_tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
        self.session_locks.clear()

        await self.master_data.terminate()
        await self.audio.terminate()
        await self.assets.close()
        await self.http.close()

    # ---------- 题库索引 ----------

    def _on_master_updated(self) -> None:
        try:
            self._rebuild_indexes()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 重建题库索引失败: {exc}", exc_info=True)

    @staticmethod
    def _pinyin_variants(title: str) -> set[str]:
        """从官方中文标题机械生成拼音全拼/首字母，仅用于匹配，不作为显示名。"""
        if not PYPINYIN_AVAILABLE or not title:
            return set()
        if not re.search(r"[\u4e00-\u9fff]", title):
            return set()
        # 含假名/谚文的标题不生成拼音（与 MoeNotes 的判定一致）。
        if re.search(r"[\u3040-\u30ff\uac00-\ud7af]", title):
            return set()
        try:
            full = "".join(lazy_pinyin(title))
            initials = "".join(lazy_pinyin(title, style=Style.FIRST_LETTER))
        except Exception:
            return set()
        variants = {_normalize_answer(full), _normalize_answer(initials)}
        variants.discard("")
        return variants

    def _rebuild_indexes(self) -> None:
        songs = self.master_data.get_songs()
        cards = self.master_data.get_cards()
        characters = self.master_data.get_characters()
        if not (songs and cards and characters):
            return

        self.songs = [
            s for s in songs if s.get("jacket_key") and s.get("audio_key")
        ] or list(songs)
        for song in self.songs:
            song.setdefault("en_name_parts", [])
        # 猜卡面卡池只保留 SR 以上；对旧版派生数据再做一次兜底过滤。
        self.cards = [
            c
            for c in cards
            if c.get("image_key") and int(c.get("rarity") or 0) >= MIN_CARD_RARITY
        ] or [c for c in cards if c.get("image_key")]
        self.characters = list(characters)

        # 人物：master 官方名称/短名/英文名/繁中/日文，
        # 加上 res/character_aliases.json 里的中文变体、罗马音与缩写。
        # "Doloris / 三角初华" 这类官方组合名会按 "/" 拆成两个可接受答案；
        # 英文"姓 名"再拆成姓/名，只接受 3 个字符以上的部分（如 Tomori / Uika）。
        self._character_extra_aliases = self._load_character_aliases()
        alias_map: dict[str, set[int]] = {}
        for char in self.characters:
            raw_names = {
                char.get("name"),
                char.get("short_name"),
                char.get("en_name"),
                char.get("en_short_name"),
                char.get("name_traditional"),
                char.get("short_name_traditional"),
                char.get("name_japanese"),
                char.get("name_english"),
            }
            for part in char.get("en_name_parts") or []:
                raw_names.add(part)
            for extra in self._character_extra_aliases.get(char["id"]) or []:
                raw_names.add(extra)
            for raw_name in raw_names:
                if not raw_name:
                    continue
                for sub in re.split(r"[/／]", str(raw_name)):
                    key = _normalize_answer(sub)
                    # 中文/日文短名 1-2 个字，英文名至少 3 个字符。
                    if key and (
                        len(key) >= 3
                        or _CJK_NAME_PATTERN.search(key)
                    ):
                        alias_map.setdefault(key, set()).add(char["id"])
        self._character_alias_map = alias_map

        # 歌曲：官方各语言标题 + 假名读音 + 拼音（可选）。
        # 英文标题按空格/连字符拆分成单词，玩家答其中任何一个英文单词也算对。
        self._song_exact_map = {}
        self._song_search_names = {}
        for song in self.songs:
            names = {
                song.get("title"),
                song.get("title_ja"),
                song.get("title_traditional"),
                song.get("title_english"),
                song.get("phonetic"),
            }
            english = song.get("title_english") or ""
            if english:
                names.update(
                    part.strip()
                    for part in re.split(r"[\s\-–—:：]+", english)
                    if len(part.strip()) >= 4
                )
            names |= self._pinyin_variants(song.get("title") or "")
            normalized = {_normalize_answer(n) for n in names if n}
            normalized.discard("")
            self._song_search_names[song["id"]] = sorted(normalized)
            for name in normalized:
                self._song_exact_map.setdefault(name, set()).add(song["id"])

        self._data_ready = bool(self.songs and self.cards and self.characters)
        logger.info(
            f"[OurNotes] 索引已重建：{len(self.songs)} 首歌 / {len(self.cards)} 张卡 / "
            f"{len(self.characters)} 个角色。"
        )

    # ---------- 答案判定 ----------

    def _is_known_character_answer(self, text: str) -> bool:
        return _normalize_answer(text) in self._character_alias_map

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        if a in b or b in a:
            shorter, longer = sorted((len(a), len(b)))
            return shorter / max(1, longer)
        return SequenceMatcher(None, a, b).ratio()

    def _match_song_text(self, text: str) -> tuple[set[int], Optional[int], float]:
        """返回 (精确命中的歌曲集合, 最佳模糊匹配歌曲, 相似度)。"""
        key = _normalize_answer(text)
        if not key:
            return set(), None, 0.0
        exact = self._song_exact_map.get(key)
        if exact:
            return set(exact), None, 1.0

        best_id: Optional[int] = None
        best_score = 0.0
        for song_id, names in self._song_search_names.items():
            for name in names:
                score = self._similarity(key, name)
                if score > best_score:
                    best_score = score
                    best_id = song_id
        return set(), best_id, best_score

    def _is_known_song_answer(self, text: str) -> bool:
        exact, best_id, score = self._match_song_text(text)
        if exact:
            return True
        key = _normalize_answer(text)
        if len(key) < 2:
            return False
        threshold = 0.62 if len(key) >= 4 else 0.75
        return best_id is not None and score >= threshold

    def _check_card_answer(self, card: dict, text: str) -> bool:
        key = _normalize_answer(text)
        if not key:
            return False
        matched = self._character_alias_map.get(key)
        if not matched:
            return False
        return bool(matched & set(card.get("character_ids") or []))

    def _check_song_answer(self, song: dict, text: str) -> bool:
        exact, best_id, score = self._match_song_text(text)
        if exact:
            # 精确命中其他歌曲时不能模糊算到当前歌曲。
            return song["id"] in exact
        key = _normalize_answer(text)
        if len(key) < 2:
            return False
        threshold = 0.72 if len(key) >= 4 else 0.82
        return best_id == song["id"] and score >= threshold

    # ---------- 平台 / 账号 ----------

    @staticmethod
    def _get_event_platform_name(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_platform_name", None)
        try:
            platform_name = getter() if callable(getter) else DEFAULT_PLATFORM_NAME
        except Exception:  # noqa: BLE001
            platform_name = DEFAULT_PLATFORM_NAME
        return str(platform_name or DEFAULT_PLATFORM_NAME).strip().lower()

    def _is_qq_official_event(self, event: AstrMessageEvent) -> bool:
        return self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME

    async def _get_account_identity(self, event: AstrMessageEvent) -> tuple[str, str]:
        platform_name = self._get_event_platform_name(event)
        raw_user_id = str(event.get_sender_id())
        resolved = await asyncio.to_thread(
            self.db.resolve_user_id, platform_name, raw_user_id
        )
        if str(resolved) != raw_user_id:
            return str(resolved), DEFAULT_PLATFORM_NAME
        return raw_user_id, platform_name

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """群聊要求 @ 机器人；私聊/C2C 直接放行。"""
        if not event.get_group_id():
            return True
        self_id = str(getattr(event.message_obj, "self_id", "") or "").strip()
        for comp in getattr(event.message_obj, "message", None) or []:
            if isinstance(comp, Comp.At):
                comp_qq = str(getattr(comp, "qq", ""))
                if not self_id or comp_qq == self_id:
                    return True
        return False

    # ---------- 权限 / 配置 ----------

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        whitelist = {str(x) for x in self.config.get("group_whitelist", [])}
        if not whitelist:
            return True
        group_id = event.get_group_id()
        return bool(group_id and str(group_id) in whitelist)

    def _get_whitelist_reject_message(self) -> Optional[str]:
        msg = self.config.get("whitelist_reject_message", "")
        if msg and str(msg).strip():
            return str(msg).strip()
        return None

    def _is_user_blacklisted(self, user_id: str) -> bool:
        return str(user_id) in {str(x) for x in self.config.get("blacklist", [])}

    def _is_event_user_blacklisted(
        self, event: AstrMessageEvent, canonical_user_id: Optional[str] = None
    ) -> bool:
        raw = str(event.get_sender_id())
        return self._is_user_blacklisted(raw) or (
            canonical_user_id is not None
            and self._is_user_blacklisted(canonical_user_id)
        )

    def _is_super_user(self, user_id: str) -> bool:
        super_users = {str(x) for x in self.config.get("super_users", [])}
        return bool(super_users) and str(user_id) in super_users

    def _get_display_name(self, user_id: str, original_name: Optional[str] = None) -> str:
        if self._is_user_blacklisted(user_id):
            return "[此用户已被BOT拉黑]"
        return original_name if original_name else "未知用户"

    def _get_quick_entries(self) -> list[str]:
        entries = self.config.get("quick_entries")
        if not entries:
            return []
        return [str(x).strip() for x in entries if str(x).strip()]

    # ---------- 官机 markdown ----------

    def _build_connect_link(
        self, command: str, self_id: str, show: Optional[str] = None
    ) -> str:
        template = self.config.get("connect_link_template")
        if template is None or any(
            marker in str(template) for marker in _LEGACY_TEMPLATE_MARKERS
        ):
            template = DEFAULT_CONNECT_TEMPLATE
        template = str(template).strip()
        if not template:
            return f"（连接：@{self_id} {command}）"
        display = show or command
        at_text = f"@{self_id} {command}"
        return template.format(
            name=command,
            command=command,
            self_id=self_id,
            at_text=at_text,
            encoded_command=quote(command, safe=""),
            encoded_name=quote(display, safe=""),
            encoded_at_text=quote(at_text, safe=""),
        )

    def _get_official_connect_id(self, event: AstrMessageEvent) -> str:
        return self._get_official_self_id(event) or "qq_official"

    def _get_official_self_id(self, event: AstrMessageEvent) -> str:
        return str(getattr(event.message_obj, "self_id", "") or "").strip()

    async def _send_markdown_text(self, event: AstrMessageEvent, text: str) -> None:
        result = event.make_result()
        result.chain = [Comp.Plain(text)]
        result.use_markdown(True)
        await event.send(result)

    def _build_footer(self, event: AstrMessageEvent, game: Optional[str] = None) -> list[str]:
        """结算消息尾部：官机附当前游戏的排行榜/查分按钮，绑定与解绑放在最下面。

        官机按钮一律不带 on 前缀（官机指令本身就是按钮形式），普通 QQ 不加按钮。
        """
        if self._get_event_platform_name(event) != OFFICIAL_PLATFORM_NAME:
            return []
        self_id = self._get_official_connect_id(event)
        if not self_id:
            return []

        game_key = game or GAME_SONG
        label = GAME_LABELS.get(game_key, "")
        rank_name = f"{label}排行榜" if label else "排行榜"
        score_name = f"{label}个人分数" if label else "分数"

        lines = [
            "  ".join(
                self._build_connect_link(name, self_id)
                for name in (score_name, rank_name)
            )
        ]
        entries = self._get_quick_entries()
        if entries:
            lines.append("快捷入口：")
            lines.append(
                "  ".join(
                    self._build_connect_link(name, self_id) for name in entries
                )
            )
        # 绑定 / 解绑固定置于最底部。
        lines.append(
            "  ".join(
                self._build_connect_link(name, self_id)
                for name in ("绑定QQ", "解绑")
            )
        )
        return lines

    def _append_official_buttons(
        self, text: str, event: AstrMessageEvent, include_answer: bool = True
    ) -> str:
        if self._get_event_platform_name(event) != OFFICIAL_PLATFORM_NAME:
            return text
        self_id = self._get_official_connect_id(event)
        if not self_id:
            return text
        parts = []
        if include_answer:
            parts.append(self._build_connect_link(" ", self_id, show="点击回答"))
        parts.append(self._build_connect_link("退出本局", self_id))
        return text + "\n" + "  ".join(parts)

    # ---------- 开局条件 ----------

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self.session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self.session_locks[session_id] = lock
        return lock

    async def _check_start_conditions(
        self, event: AstrMessageEvent, game: str
    ) -> tuple[bool, Optional[str]]:
        if not self._is_group_allowed(event):
            return False, self._get_whitelist_reject_message()

        if not self._data_ready:
            return False, "题库正在初始化中，请稍后再试…"

        session_id = _normalized_session_id(event)
        if session_id in self.active_sessions:
            return False, "当前已经有一个游戏在进行中啦~ 等它结束后再来玩吧！"

        debug_mode = bool(self.config.get("debug_mode", False))
        if not debug_mode:
            cooldown = int(self.config.get("game_cooldown_seconds", 10) or 0)
            elapsed = time.time() - self.last_game_end_time.get(session_id, 0)
            if elapsed < cooldown:
                remaining = cooldown - elapsed
                display = f"{remaining:.1f}" if remaining < 1 else str(int(remaining))
                return False, f"让我们休息一下吧！{display}秒后再来玩哦~ 😊"

        user_id, platform = await self._get_account_identity(event)
        if self._is_event_user_blacklisted(event, user_id):
            return False, "抱歉，你已被禁止使用此功能 😔"

        if not debug_mode:
            daily_limit = int(self.config.get("daily_play_limit", 10))
            per_game = bool(self.config.get("daily_limit_per_game", False))
            can_play = await asyncio.to_thread(
                self.db.can_play_today,
                user_id,
                platform,
                game,
                daily_limit,
                per_game,
            )
            if not can_play:
                return (
                    False,
                    f"今天的游戏次数已经用完啦~ 明天再来玩吧！"
                    f"每天最多可以玩{daily_limit}次哦~ ✨",
                )
        return True, None

    async def _consume_daily_play(
        self, event: AstrMessageEvent, game: str, user_id: str, user_name: str, platform: str
    ) -> None:
        if bool(self.config.get("debug_mode", False)):
            return
        await asyncio.to_thread(
            self.db.update_user_play, user_id, user_name, platform, game
        )

    # ---------- 资源加载 ----------

    async def _load_card_image(self, card: dict) -> Optional[Image.Image]:
        return await self.assets.fetch_image(card["image_key"])

    async def _load_random_card(self) -> Optional[tuple[dict, Image.Image]]:
        if not self.cards:
            return None
        tried: set[int] = set()
        max_attempts = min(6, len(self.cards))
        for _ in range(max_attempts):
            candidates = [c for c in self.cards if c["id"] not in tried]
            if not candidates:
                break
            card = random.choice(candidates)
            tried.add(card["id"])
            try:
                image = await asyncio.wait_for(
                    self._load_card_image(card), timeout=35
                )
            except asyncio.TimeoutError:
                image = None
            if image is not None:
                return card, image
        return None

    async def _load_jacket_image(self, song: dict) -> Optional[Image.Image]:
        return await self.assets.fetch_image(song["jacket_key"])

    async def _load_random_jacket_song(self) -> Optional[tuple[dict, Image.Image]]:
        if not self.songs:
            return None
        tried: set[int] = set()
        max_attempts = min(6, len(self.songs))
        for _ in range(max_attempts):
            candidates = [s for s in self.songs if s["id"] not in tried]
            if not candidates:
                break
            song = random.choice(candidates)
            tried.add(song["id"])
            try:
                image = await asyncio.wait_for(
                    self._load_jacket_image(song), timeout=35
                )
            except asyncio.TimeoutError:
                image = None
            if image is not None:
                return song, image
        return None

    async def _pick_song_and_clip(
        self, mode_key: str
    ) -> Optional[tuple[dict, str, int, str, str]]:
        """返回 (song, clip_path, score, game_type, effect_display)。"""
        if not self.songs:
            return None
        tried: set[int] = set()
        max_attempts = min(5, len(self.songs))

        if mode_key == "random":
            speed, reverse, score, display = self.audio.get_random_mode()
            game_type = "guess_song_random"
        elif mode_key == "speed":
            speed, reverse, score, display = 2.0, False, int(
                self.config.get("song_2x_score", 1) or 1
            ), "2倍速"
            game_type = "guess_song_speed_2x"
        elif mode_key == "reverse":
            speed, reverse, score, display = 1.0, True, int(
                self.config.get("song_reverse_score", 1) or 1
            ), "倒放"
            game_type = "guess_song_reverse"
        else:
            speed, reverse, score, display = 1.0, False, 1, "普通"
            game_type = "guess_song_normal"

        for _ in range(max_attempts):
            candidates = [s for s in self.songs if s["id"] not in tried]
            if not candidates:
                break
            song = random.choice(candidates)
            tried.add(song["id"])
            try:
                clip_path = await asyncio.wait_for(
                    self.audio.get_song_clip(song, speed=speed, reverse=reverse),
                    timeout=150,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[OurNotes] 音频处理超时，跳过歌曲 {song.get('title')}")
                clip_path = None
            if clip_path:
                return song, clip_path, score, game_type, display
        return None

    # ---------- 通用发送 ----------

    async def _send_intro(
        self,
        event: AstrMessageEvent,
        intro_text: str,
        in_auto_mode: bool = False,
    ) -> None:
        if self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME:
            await self._send_markdown_text(
                event, self._append_official_buttons(intro_text, event)
            )
        else:
            await event.send(event.plain_result(intro_text))

    async def _send_image_path(
        self, event: AstrMessageEvent, path: Optional[str]
    ) -> None:
        if path:
            await event.send(event.chain_result([Comp.Image(file=str(path))]))

    async def _send_reveal_image(
        self, event: AstrMessageEvent, round_data: Round
    ) -> None:
        image = None
        if round_data.reveal_key:
            image = await self.assets.fetch_image(round_data.reveal_key)
        if image is None and round_data.reveal_fallback_key:
            image = await self.assets.fetch_image(round_data.reveal_fallback_key)
        if image is None:
            return
        try:
            compressed = await asyncio.to_thread(
                self.images.compress_image, image, 900, 90, "answer"
            )
        finally:
            image.close()
        if compressed:
            await event.send(event.chain_result([Comp.Image(file=compressed)]))

    # ---------- 猜歌 ----------

    @filter.command(
        "on猜歌",
        alias=_command_aliases(
            "on猜歌", "ournotes猜歌", "moenotes猜歌", "猜歌",
            "on猜歌1", "on猜歌2", "2倍速猜歌", "倒放猜歌",
        ),
    )
    async def start_guess_song(self, event: AstrMessageEvent):
        text = _strip_command_prefix(event.message_str).lower()
        if "随机" in text:
            mode_key = "random"
        elif (
            "2倍速" in text
            or "二倍速" in text
            or re.search(r"猜歌\s*1\b", text)
            or re.search(r"(?:^|\s)1(?:\s|$)", text)
        ):
            mode_key = "speed"
        elif (
            "倒放" in text
            or re.search(r"猜歌\s*2\b", text)
            or re.search(r"(?:^|\s)2(?:\s|$)", text)
        ):
            mode_key = "reverse"
        else:
            mode_key = "normal"
        async for result in self._guess_song_impl(event, mode_key):
            yield result

    @filter.command(
        "随机猜歌",
        alias=_command_aliases(
            "随机猜歌", "on随机猜歌", "ournotes随机猜歌", "moenotes随机猜歌"
        ),
    )
    async def start_random_guess_song(self, event: AstrMessageEvent):
        async for result in self._guess_song_impl(event, "random"):
            yield result

    async def _guess_song_impl(self, event: AstrMessageEvent, mode_key: str):
        session_id = _normalized_session_id(event)
        lock = self._session_lock(session_id)
        async with lock:
            ok, message = await self._check_start_conditions(event, GAME_SONG)
            if not ok:
                if message:
                    yield event.plain_result(message)
                return
            self.active_sessions.add(session_id)

        try:
            initiator_id, initiator_platform = await self._get_account_identity(event)
            initiator_name = event.get_sender_name() or initiator_id

            picked = await self._pick_song_and_clip(mode_key)
            if not picked:
                yield event.plain_result("音频加载失败，请稍后再试。")
                return
            song, clip_path, score, game_type, effect_display = picked

            # 12 个选项（正确 + 11 随机）
            pool = [s for s in self.songs if s["id"] != song["id"]]
            if len(pool) < 11:
                yield event.plain_result("题库歌曲数量不足，无法生成选项。")
                return
            options = [song] + random.sample(pool, 11)
            random.shuffle(options)
            correct_num = options.index(song) + 1

            jacket_images = await asyncio.gather(
                *(self._load_jacket_image(opt) for opt in options)
            )
            options_image_path = await asyncio.to_thread(
                self.images.create_options_image,
                options,
                list(jacket_images),
            )
            for image in jacket_images:
                if image is not None:
                    try:
                        image.close()
                    except Exception:
                        pass

            timeout_seconds = max(5, int(self.config.get("answer_timeout", 30) or 30))
            guesses_per_user = max(1, int(self.config.get("song_guesses_per_user", 2) or 2))
            max_guess_attempts = int(self.config.get("max_guess_attempts", 10) or 0)
            reward_valid_time = max(0, int(self.config.get("reward_valid_time", 5) or 0))
            is_official = self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME

            answer_hint = "@bot发送编号" if is_official else "发送编号"
            intro = f"嗯...\n这首歌是什么呢？请在{timeout_seconds}秒内{answer_hint}回答哦～\n"
            if mode_key != "normal":
                intro += f"本轮效果：{effect_display}\n"
            intro += f"每个玩家有{guesses_per_user}次作答机会"

            round_data = Round(
                game=GAME_SONG,
                score=score,
                effect_name=effect_display,
                song=song,
                mode_key=mode_key,
                game_type=game_type,
                correct_answer_num=correct_num,
                options=options,
                display_answer=song["title"],
                reveal_key=song["jacket_key"],
            )
            self.game_sessions[session_id] = round_data

            # 发送音频 → 开局文字 → 选项图
            try:
                await event.send(event.chain_result([Comp.Record(file=str(clip_path))]))
                await self._send_intro(event, intro)
                await self._send_image_path(event, options_image_path)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[OurNotes] 猜歌发送失败: {exc}", exc_info=True)
                yield event.plain_result("发送游戏消息失败，游戏中断。")
                return

            await self._consume_daily_play(
                event, GAME_SONG, initiator_id, initiator_name, initiator_platform
            )

            if bool(self.config.get("debug_mode", False)):
                yield event.plain_result(
                    f"[调试模式] 正确答案：{correct_num}. {song['title']}"
                )
                await self._send_reveal_image(event, round_data)
                return

            user_guess_counts: dict[tuple[str, str], int] = {}
            correct_players: dict[tuple[str, str], dict] = {}
            first_correct_time: Optional[float] = None
            total_guesses = 0
            quit_ended_round = False
            any_genuine_attempt = False

            @session_waiter(timeout=timeout_seconds)
            async def song_waiter(
                controller: SessionController, answer_event: AstrMessageEvent
            ):
                nonlocal first_correct_time, total_guesses, quit_ended_round, any_genuine_attempt

                if is_official and not self._is_at_bot(answer_event):
                    return

                answer_text = _strip_command_prefix(answer_event.message_str)
                if not answer_text:
                    return

                if answer_text in ("仅退出本局", "退出本局"):
                    quit_ended_round = True
                    controller.stop()
                    return

                if not answer_text.isdigit():
                    return

                answer_num = int(answer_text)
                if not (1 <= answer_num <= len(options)):
                    return

                user_id, platform = await self._get_account_identity(answer_event)
                if self._is_event_user_blacklisted(answer_event, user_id):
                    return

                key = (user_id, platform)
                if key in correct_players:
                    return
                if user_guess_counts.get(key, 0) >= guesses_per_user:
                    return
                user_guess_counts[key] = user_guess_counts.get(key, 0) + 1
                total_guesses += 1
                any_genuine_attempt = True

                is_correct = answer_num == correct_num
                user_name = answer_event.get_sender_name() or user_id
                now = time.time()
                score_to_add = 0

                if is_correct:
                    if first_correct_time is None:
                        first_correct_time = now
                        score_to_add = score
                        correct_players[key] = {"name": user_name, "score": score}
                        if reward_valid_time > 0:
                            async def stop_after_delay():
                                await asyncio.sleep(reward_valid_time)
                                controller.stop()

                            asyncio.create_task(stop_after_delay())
                        else:
                            controller.stop()
                    elif (
                        reward_valid_time > 0
                        and now - first_correct_time <= reward_valid_time
                    ):
                        score_to_add = score
                        correct_players[key] = {"name": user_name, "score": score}

                await asyncio.to_thread(
                    self.db.update_stats,
                    user_id,
                    user_name,
                    platform,
                    GAME_SONG,
                    score_to_add,
                    is_correct,
                )

                if (
                    max_guess_attempts > 0
                    and total_guesses >= max_guess_attempts
                    and not correct_players
                ):
                    controller.stop()

            try:
                await song_waiter(event, session_filter=GameSessionFilter())
            except TimeoutError:
                pass

            # 无人作答时由发起者承担一次失败
            if not any_genuine_attempt:
                await asyncio.to_thread(
                    self.db.update_stats,
                    initiator_id,
                    initiator_name,
                    initiator_platform,
                    GAME_SONG,
                    0,
                    False,
                )

            correct_name = song["title"]
            if quit_ended_round:
                result_text = f"本局已结束（仅退出本局）\n正确答案: {correct_name}"
            elif correct_players:
                names = "、".join(p["name"] for p in correct_players.values())
                if len(correct_players) == 1:
                    result_text = (
                        f"🎉 {names}答对了！获得{score}分！\n正确答案: {correct_name}"
                    )
                else:
                    result_text = (
                        f"🎉 恭喜以下玩家答对！每人获得{score}分！\n"
                        f"{names}\n\n正确答案: {correct_name}"
                    )
            elif total_guesses > 0:
                result_text = f"❌ 无人答对！\n正确答案: {correct_name}"
            else:
                result_text = f"⏰ 时间到！\n正确答案: {correct_name}"

            footer = self._build_footer(event, GAME_SONG)
            if footer:
                result_text += "\n" + "\n".join(footer)

            if is_official:
                await self._send_markdown_text(event, result_text)
            else:
                yield event.plain_result(result_text)

            await self._send_reveal_image(event, round_data)
        finally:
            self.active_sessions.discard(session_id)
            self.game_sessions.pop(session_id, None)
            self.last_game_end_time[session_id] = time.time()

    # ---------- 猜卡面 ----------

    @filter.command(
        "on猜卡面",
        alias=_command_aliases(
            "on猜卡面", "ournotes猜卡面", "moenotes猜卡面",
            "猜卡面", "猜卡", "on猜卡",
        ),
    )
    async def start_guess_card(self, event: AstrMessageEvent):
        async for result in self._guess_card_impl(event):
            yield result

    async def _guess_card_impl(self, event: AstrMessageEvent):
        session_id = _normalized_session_id(event)
        lock = self._session_lock(session_id)
        async with lock:
            ok, message = await self._check_start_conditions(event, GAME_CARD)
            if not ok:
                if message:
                    yield event.plain_result(message)
                return
            self.active_sessions.add(session_id)

        try:
            initiator_id, initiator_platform = await self._get_account_identity(event)
            initiator_name = event.get_sender_name() or initiator_id

            picked = await self._load_random_card()
            if not picked:
                yield event.plain_result("卡面加载失败，请稍后再试。")
                return
            card, image = picked

            # 在玩家思考期间后台预取完整卡面，结算时直接读缓存。
            if card.get("full_key"):
                self._track_task(
                    asyncio.create_task(self.assets.fetch_bytes(card["full_key"]))
                )

            effect_name, effect_display, difficulty = self.images.random_card_effect()
            processed = await asyncio.to_thread(
                self.images.apply_card_effects, image, [effect_name]
            )
            try:
                compressed = await asyncio.to_thread(
                    self.images.compress_image, processed, 900, 90, "question"
                )
            finally:
                if processed is not image:
                    processed.close()
                image.close()

            if not compressed:
                yield event.plain_result("处理卡面图片失败，请稍后再试。")
                return

            timeout_seconds = max(5, int(self.config.get("answer_timeout", 30) or 30))
            max_guess_attempts = int(self.config.get("max_guess_attempts", 10) or 0)
            reward_valid_time = max(0, int(self.config.get("reward_valid_time", 5) or 0))

            rarity_stars = {
                "R": "⭐",
                "SR": "⭐⭐",
                "SSR": "⭐⭐⭐",
                "BD": "⭐⭐⭐⭐",
                "EX": "⭐⭐⭐⭐⭐",
            }.get(card.get("rarity_label", ""), card.get("rarity_label", ""))

            hints = []
            if random.choice([True, False]):
                hints.append(f"星级提示: {rarity_stars}")
            if random.choice([True, False]):
                kind = "成员卡" if card.get("kind") == "member" else "支援卡"
                hints.append(f"卡牌类型: {kind}")
            if len(card.get("character_ids") or []) > 1:
                hints.append(
                    f"这张卡上有 {len(card['character_ids'])} 位角色，答出任意一位即可"
                )

            is_official = self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME
            answer_hint = (
                "@bot发送角色名称进行回答"
                if is_official
                else "发送角色名称进行回答哦（无需@机器人）"
            )
            intro = (
                f"请在{timeout_seconds}秒内{answer_hint}\n"
                f"本轮图片效果: {effect_display}\n"
                f"猜对得分: {difficulty}分\n"
                + ("\n".join(hints) + "\n" if hints else "")
            )

            round_data = Round(
                game=GAME_CARD,
                score=difficulty,
                effect_name=effect_display,
                card=card,
                display_answer=card.get("display_name") or "",
                reveal_key=card.get("full_key") or card.get("image_key", ""),
                reveal_fallback_key=card.get("image_key", ""),
            )
            self.game_sessions[session_id] = round_data

            try:
                await self._send_intro(event, intro)
                await self._send_image_path(event, compressed)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[OurNotes] 猜卡面发送失败: {exc}", exc_info=True)
                yield event.plain_result("发送游戏消息失败，游戏中断。")
                return

            await self._consume_daily_play(
                event, GAME_CARD, initiator_id, initiator_name, initiator_platform
            )

            if bool(self.config.get("debug_mode", False)):
                yield event.plain_result(
                    f"[调试模式] 正确答案：{round_data.display_answer}"
                )
                await self._send_reveal_image(event, round_data)
                return

            recorded_attempts: set[tuple[str, str]] = set()
            failed_attempters: list[dict] = []
            correct_players: dict[tuple[str, str], dict] = {}
            first_correct_time: Optional[float] = None
            total_guesses = 0
            quit_ended_round = False
            any_genuine_attempt = False

            @session_waiter(timeout=timeout_seconds)
            async def card_waiter(
                controller: SessionController, answer_event: AstrMessageEvent
            ):
                nonlocal first_correct_time, total_guesses, quit_ended_round, any_genuine_attempt

                if is_official and not self._is_at_bot(answer_event):
                    return

                answer_text = _strip_command_prefix(answer_event.message_str)
                if not answer_text:
                    return

                if answer_text in ("仅退出本局", "退出本局"):
                    quit_ended_round = True
                    controller.stop()
                    return

                if not self._is_known_character_answer(answer_text):
                    return

                user_id, platform = await self._get_account_identity(answer_event)
                if self._is_event_user_blacklisted(answer_event, user_id):
                    return

                key = (user_id, platform)
                if key in correct_players:
                    return
                total_guesses += 1
                any_genuine_attempt = True
                user_name = answer_event.get_sender_name() or user_id
                is_correct = self._check_card_answer(card, answer_text)

                if is_correct:
                    now = time.time()
                    if first_correct_time is None:
                        first_correct_time = now
                        correct_players[key] = {"name": user_name}
                        if reward_valid_time > 0:
                            async def stop_after_delay():
                                await asyncio.sleep(reward_valid_time)
                                controller.stop()

                            asyncio.create_task(stop_after_delay())
                        else:
                            controller.stop()
                    elif (
                        reward_valid_time > 0
                        and now - first_correct_time <= reward_valid_time
                    ):
                        correct_players[key] = {"name": user_name}
                elif key not in recorded_attempts:
                    recorded_attempts.add(key)
                    failed_attempters.append(
                        {"user_id": user_id, "platform": platform, "name": user_name}
                    )

                if (
                    max_guess_attempts > 0
                    and total_guesses >= max_guess_attempts
                    and not correct_players
                ):
                    controller.stop()

            try:
                await card_waiter(event, session_filter=GameSessionFilter())
            except TimeoutError:
                pass

            for failed in failed_attempters:
                await asyncio.to_thread(
                    self.db.update_stats,
                    failed["user_id"],
                    failed["name"],
                    failed["platform"],
                    GAME_CARD,
                    0,
                    False,
                )

            for key, winner in correct_players.items():
                await asyncio.to_thread(
                    self.db.update_stats,
                    key[0],
                    winner["name"],
                    key[1],
                    GAME_CARD,
                    difficulty,
                    True,
                )

            if not any_genuine_attempt:
                await asyncio.to_thread(
                    self.db.update_stats,
                    initiator_id,
                    initiator_name,
                    initiator_platform,
                    GAME_CARD,
                    0,
                    False,
                )

            correct_name = round_data.display_answer
            if card.get("kind") == "support" and card.get("title"):
                correct_name = f"{correct_name}（{card['title']}）"

            if quit_ended_round:
                result_text = f"本局已结束（仅退出本局）\n正确答案: {correct_name}"
            elif correct_players:
                names = "、".join(p["name"] for p in correct_players.values())
                if len(correct_players) == 1:
                    result_text = (
                        f"🎉 {names}答对了！获得{difficulty}分！\n"
                        f"正确答案: {correct_name}"
                    )
                else:
                    result_text = (
                        f"🎉 恭喜以下玩家答对！每人获得{difficulty}分！\n"
                        f"{names}\n\n正确答案: {correct_name}"
                    )
            elif total_guesses > 0:
                result_text = f"❌ 无人答对！\n正确答案: {correct_name}"
            else:
                result_text = f"⏰ 时间到！\n正确答案: {correct_name}"

            footer = self._build_footer(event, GAME_CARD)
            if footer:
                result_text += "\n" + "\n".join(footer)

            if is_official:
                await self._send_markdown_text(event, result_text)
            else:
                yield event.plain_result(result_text)

            await self._send_reveal_image(event, round_data)
        finally:
            self.active_sessions.discard(session_id)
            self.game_sessions.pop(session_id, None)
            self.last_game_end_time[session_id] = time.time()

    # ---------- 猜曲绘 ----------

    @filter.command(
        "on猜曲绘",
        alias=_command_aliases(
            "on猜曲绘", "ournotes猜曲绘", "moenotes猜曲绘",
            "猜曲绘", "猜封面", "on猜封面",
        ),
    )
    async def start_guess_jacket(self, event: AstrMessageEvent):
        async for result in self._guess_jacket_impl(event):
            yield result

    async def _guess_jacket_impl(self, event: AstrMessageEvent):
        session_id = _normalized_session_id(event)
        lock = self._session_lock(session_id)
        async with lock:
            ok, message = await self._check_start_conditions(event, GAME_JACKET)
            if not ok:
                if message:
                    yield event.plain_result(message)
                return
            self.active_sessions.add(session_id)

        try:
            initiator_id, initiator_platform = await self._get_account_identity(event)
            initiator_name = event.get_sender_name() or initiator_id

            picked = await self._load_random_jacket_song()
            if not picked:
                yield event.plain_result("曲绘加载失败，请稍后再试。")
                return
            song, image = picked

            effect_name, effect_display, difficulty = self.images.random_jacket_effect()
            processed, actual_difficulty = await asyncio.to_thread(
                self.images.apply_jacket_effect, image, effect_name
            )
            try:
                compressed = await asyncio.to_thread(
                    self.images.compress_image, processed, 900, 90, "question"
                )
            finally:
                if processed is not image:
                    processed.close()
                image.close()

            if not compressed:
                yield event.plain_result("处理曲绘图片失败，请稍后再试。")
                return

            timeout_seconds = max(5, int(self.config.get("answer_timeout", 30) or 30))
            max_guess_attempts = int(self.config.get("max_guess_attempts", 10) or 0)
            reward_valid_time = max(0, int(self.config.get("reward_valid_time", 5) or 0))

            is_official = self._get_event_platform_name(event) == OFFICIAL_PLATFORM_NAME
            if is_official:
                intro = (
                    f"请在{timeout_seconds}秒内@bot进行回答：\n"
                    f"效果: {effect_display}\n得分: {actual_difficulty}分\n"
                )
            else:
                intro = (
                    f"请在{timeout_seconds}秒内发送歌曲名回答哦：\n"
                    f"效果: {effect_display}\n得分: {actual_difficulty}分\n"
                )

            round_data = Round(
                game=GAME_JACKET,
                score=actual_difficulty,
                effect_name=effect_display,
                song=song,
                display_answer=song["title"],
                reveal_key=song["jacket_key"],
            )
            self.game_sessions[session_id] = round_data

            try:
                await self._send_intro(event, intro)
                await self._send_image_path(event, compressed)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[OurNotes] 猜曲绘发送失败: {exc}", exc_info=True)
                yield event.plain_result("发送游戏消息失败，游戏中断。")
                return

            await self._consume_daily_play(
                event, GAME_JACKET, initiator_id, initiator_name, initiator_platform
            )

            if bool(self.config.get("debug_mode", False)):
                yield event.plain_result(f"[调试模式] 正确答案：{song['title']}")
                await self._send_reveal_image(event, round_data)
                return

            recorded_attempts: set[tuple[str, str]] = set()
            failed_attempters: list[dict] = []
            correct_players: dict[tuple[str, str], dict] = {}
            first_correct_time: Optional[float] = None
            total_guesses = 0
            quit_ended_round = False
            any_genuine_attempt = False

            @session_waiter(timeout=timeout_seconds)
            async def jacket_waiter(
                controller: SessionController, answer_event: AstrMessageEvent
            ):
                nonlocal first_correct_time, total_guesses, quit_ended_round, any_genuine_attempt

                if is_official and not self._is_at_bot(answer_event):
                    return

                answer_text = _strip_command_prefix(answer_event.message_str)
                if not answer_text:
                    return

                if answer_text in ("仅退出本局", "退出本局"):
                    quit_ended_round = True
                    controller.stop()
                    return

                if not self._is_known_song_answer(answer_text):
                    return

                user_id, platform = await self._get_account_identity(answer_event)
                if self._is_event_user_blacklisted(answer_event, user_id):
                    return

                key = (user_id, platform)
                if key in correct_players:
                    return
                total_guesses += 1
                any_genuine_attempt = True
                user_name = answer_event.get_sender_name() or user_id
                is_correct = self._check_song_answer(song, answer_text)

                if is_correct:
                    now = time.time()
                    if first_correct_time is None:
                        first_correct_time = now
                        correct_players[key] = {"name": user_name}
                        if reward_valid_time > 0:
                            async def stop_after_delay():
                                await asyncio.sleep(reward_valid_time)
                                controller.stop()

                            asyncio.create_task(stop_after_delay())
                        else:
                            controller.stop()
                    elif (
                        reward_valid_time > 0
                        and now - first_correct_time <= reward_valid_time
                    ):
                        correct_players[key] = {"name": user_name}
                elif key not in recorded_attempts:
                    recorded_attempts.add(key)
                    failed_attempters.append(
                        {"user_id": user_id, "platform": platform, "name": user_name}
                    )

                if (
                    max_guess_attempts > 0
                    and total_guesses >= max_guess_attempts
                    and not correct_players
                ):
                    controller.stop()

            try:
                await jacket_waiter(event, session_filter=GameSessionFilter())
            except TimeoutError:
                pass

            for failed in failed_attempters:
                await asyncio.to_thread(
                    self.db.update_stats,
                    failed["user_id"],
                    failed["name"],
                    failed["platform"],
                    GAME_JACKET,
                    0,
                    False,
                )

            for key, winner in correct_players.items():
                await asyncio.to_thread(
                    self.db.update_stats,
                    key[0],
                    winner["name"],
                    key[1],
                    GAME_JACKET,
                    actual_difficulty,
                    True,
                )

            if not any_genuine_attempt:
                await asyncio.to_thread(
                    self.db.update_stats,
                    initiator_id,
                    initiator_name,
                    initiator_platform,
                    GAME_JACKET,
                    0,
                    False,
                )

            correct_name = song["title"]
            if quit_ended_round:
                result_text = f"本局已结束（仅退出本局）\n正确答案: {correct_name}"
            elif correct_players:
                names = "、".join(p["name"] for p in correct_players.values())
                if len(correct_players) == 1:
                    result_text = (
                        f"🎉 {names}答对了！获得{actual_difficulty}分！\n"
                        f"正确答案: {correct_name}"
                    )
                else:
                    result_text = (
                        f"🎉 恭喜以下玩家答对！每人获得{actual_difficulty}分！\n"
                        f"{names}\n\n正确答案: {correct_name}"
                    )
            elif total_guesses > 0:
                result_text = f"❌ 无人答对！\n正确答案: {correct_name}"
            else:
                result_text = f"⏰ 时间到！\n正确答案: {correct_name}"

            footer = self._build_footer(event, GAME_JACKET)
            if footer:
                result_text += "\n" + "\n".join(footer)

            if is_official:
                await self._send_markdown_text(event, result_text)
            else:
                yield event.plain_result(result_text)

            await self._send_reveal_image(event, round_data)
        finally:
            self.active_sessions.discard(session_id)
            self.game_sessions.pop(session_id, None)
            self.last_game_end_time[session_id] = time.time()

    # ---------- 帮助 ----------

    @filter.command(
        "on帮助",
        alias=_command_aliases(
            "on帮助", "ournotes帮助", "moenotes帮助", "on娱乐帮助",
            "猜歌帮助", "猜卡面帮助", "猜曲绘帮助",
        ),
    )
    async def show_help(self, event: AstrMessageEvent):
        if not self._is_group_allowed(event):
            reject = self._get_whitelist_reject_message()
            if reject:
                yield event.plain_result(reject)
            return

        card_effects = "、".join(
            f"{self.images.card_effects[e]['name']}({self.images.card_effects[e]['difficulty']}分)"
            for e in self.images.get_enabled_card_effects()
        ) or "无"
        jacket_effects = "、".join(
            f"{self.images.jacket_effects[e]['name']}({self.images.jacket_effects[e]['difficulty']}分)"
            for e in self.images.get_enabled_jacket_effects()
        ) or "无"

        help_text = (
            "🎵 OurNotes 娱乐指南 🎵\n\n"
            "【猜歌】\n"
            "on猜歌 / 猜歌 - 普通难度\n"
            "on猜歌 1 / 2倍速猜歌 - 2 倍速\n"
            "on猜歌 2 / 倒放猜歌 - 倒放\n"
            "随机猜歌 - 随机效果组合\n\n"
            "【猜卡面】\n"
            "on猜卡面 / 猜卡面 - 卡牌与支援卡统一卡池（SR 以上），猜出卡面上的角色\n"
            "支持输入中文名、简称、英文名与官方别名\n\n"
            "【猜曲绘】\n"
            "on猜曲绘 / 猜曲绘 - 根据处理后的曲绘猜歌名\n\n"
            "【账号与数据】\n"
            "on绑定 QQ号 - QQ官方机器人绑定到普通QQ号（需发送“确认”，每个QQ最多绑定两个官机）\n"
            "on解绑 - 解除官方机与QQ号的绑定（需发送“确认”）\n"
            "on分数 - 查看总分与三个游戏的详细数据\n"
            "on猜歌排行榜 - 猜歌排行榜\n"
            "on猜卡面排行榜 - 猜卡面排行榜\n"
            "on猜曲绘排行榜 - 猜曲绘排行榜\n"
            "on自定义名称 [名称] - 设置排行榜显示名（不带参数则清除）\n\n"
            "【管理员】\n"
            "on刷新题库 - 立即检查并更新 master 数据\n"
            "on题库状态 - 查看题库数量与资源源\n"
            "on重置次数 [QQ号] - 重置今日游戏次数\n\n"
            "【当前启用的猜卡面效果】\n"
            f"{card_effects}\n\n"
            "【当前启用的猜曲绘效果】\n"
            f"{jacket_effects}"
        )
        yield event.plain_result(help_text)

    # ---------- 分数 ----------

    @filter.command(
        "on分数",
        alias=_command_aliases(
            "on分数", "ournotes分数", "moenotes分数", "on个人分数",
            "猜歌分数", "猜卡面分数", "猜曲绘分数",
            "猜歌个人分数", "猜卡面个人分数", "猜曲绘个人分数",
        ),
    )
    async def show_user_score(self, event: AstrMessageEvent):
        if not self._is_group_allowed(event):
            reject = self._get_whitelist_reject_message()
            if reject:
                yield event.plain_result(reject)
            return

        raw_user_id = str(event.get_sender_id())
        raw_platform = self._get_event_platform_name(event)
        user_id, platform = await self._get_account_identity(event)
        if self._is_event_user_blacklisted(event, user_id):
            yield event.plain_result("抱歉，你已被禁止使用此功能 😔")
            return

        text = _strip_command_prefix(event.message_str)
        game: Optional[str] = None
        if "猜歌" in text:
            game = GAME_SONG
        elif "猜卡" in text:
            game = GAME_CARD
        elif "猜曲绘" in text or "猜封面" in text:
            game = GAME_JACKET

        user_name = event.get_sender_name() or raw_user_id
        display_name = self._get_display_name(user_id, user_name)
        platform_display = {
            OFFICIAL_PLATFORM_NAME: "QQ官方机器人",
            DEFAULT_PLATFORM_NAME: "QQ个人号",
        }.get(raw_platform, raw_platform)

        identity_lines = [f"🌐 平台: {platform_display}"]
        if raw_platform == OFFICIAL_PLATFORM_NAME and user_id == raw_user_id:
            identity_lines.extend(
                [
                    "当前官方机器人账号尚未绑定QQ号。",
                    "如需将官方机分数迁移到普通QQ账号，请发送：on绑定 QQ号",
                ]
            )
        elif user_id != raw_user_id:
            identity_lines.append(f"🔗 统计账号: {user_id}")

        stats = await asyncio.to_thread(self.db.get_user_all_stats, user_id, platform)
        if not stats:
            yield event.plain_result(
                "\n".join(identity_lines)
                + f"\n\n{user_name}，你还没有参与过 OurNotes 游戏哦！快来一起玩呀~ 🎮"
            )
            return

        if game:
            row = stats.get(game)
            score, attempts, correct = (row if row else (0, 0, 0))
            rank = await asyncio.to_thread(self.db.get_user_rank, score, game)
            accuracy = (correct * 100 / attempts) if attempts > 0 else 0
            game_label = GAME_LABELS.get(game, game)
            lines = [
                "\n".join(identity_lines),
                f"{GAME_EMOJI.get(game, '')} {display_name} 的{game_label}数据",
                f"🏆 总分: {score} 分",
                f"🎯 正确率: {accuracy:.1f}%",
                f"🎮 作答次数: {attempts} 次",
                f"✅ 答对次数: {correct} 次",
                f"🏅 当前排名: 第 {rank} 名",
            ]
        else:
            score = sum(int(v[0] or 0) for v in stats.values())
            attempts = sum(int(v[1] or 0) for v in stats.values())
            correct = sum(int(v[2] or 0) for v in stats.values())
            rank = await asyncio.to_thread(self.db.get_user_rank, score, None)
            accuracy = (correct * 100 / attempts) if attempts > 0 else 0
            lines = [
                "\n".join(identity_lines),
                f"📊 {display_name} 的 OurNotes 报告",
                f"🏆 总分: {score} 分",
                f"🎯 总正确率: {accuracy:.1f}%",
                f"🎮 总作答次数: {attempts} 次",
                f"✅ 总答对次数: {correct} 次",
                f"🏅 当前排名: 第 {rank} 名",
                "",
            ]
            for g in GAMES:
                g_score, g_attempts, g_correct = stats.get(g, (0, 0, 0))
                g_acc = (g_correct * 100 / g_attempts) if g_attempts > 0 else 0
                lines.append(
                    f"{GAME_EMOJI[g]} {GAME_LABELS[g]}: {g_score} 分 / "
                    f"正确率 {g_acc:.1f}% ({g_correct}/{g_attempts})"
                )

        # 今日剩余
        daily_limit = int(self.config.get("daily_play_limit", 10))
        per_game = bool(self.config.get("daily_limit_per_game", False))
        if daily_limit == -1:
            lines.append("📅 今日剩余: 无限次数")
        elif game:
            plays = await asyncio.to_thread(
                self.db.get_today_plays, user_id, platform, game
            )
            lines.append(f"📅 今日剩余: {max(0, daily_limit - plays)}次")
        elif per_game:
            parts = []
            for g in GAMES:
                plays = await asyncio.to_thread(
                    self.db.get_today_plays, user_id, platform, g
                )
                parts.append(f"{GAME_LABELS[g]} {max(0, daily_limit - plays)}次")
            lines.append("📅 今日剩余: " + " / ".join(parts))
        else:
            plays = await asyncio.to_thread(
                self.db.get_today_plays, user_id, platform, None
            )
            lines.append(f"📅 今日剩余: {max(0, daily_limit - plays)}次")

        yield event.plain_result("\n".join(lines))

    # ---------- 排行榜 ----------

    @filter.command(
        "on排行榜",
        alias=_command_aliases(
            "on排行榜", "ournotes排行榜", "moenotes排行榜", "on排行",
            "猜歌排行榜", "猜卡面排行榜", "猜曲绘排行榜", "猜封面排行榜",
            "本地猜歌排行榜", "本地猜卡排行榜", "本地猜曲绘排行榜",
        ),
    )
    async def show_ranking(self, event: AstrMessageEvent):
        if not self._is_group_allowed(event):
            reject = self._get_whitelist_reject_message()
            if reject:
                yield event.plain_result(reject)
            return

        text = _strip_command_prefix(event.message_str)
        game: Optional[str] = None
        if "猜歌" in text:
            game = GAME_SONG
        elif "猜卡" in text:
            game = GAME_CARD
        elif "猜曲绘" in text or "猜封面" in text:
            game = GAME_JACKET

        if not game:
            # 三个游戏的排行榜相互独立，必须指定游戏。
            yield event.plain_result(
                "猜歌、猜卡面、猜曲绘的排行榜是分开的哦~\n"
                "发送「猜歌排行榜」「猜卡面排行榜」或「猜曲绘排行榜」查看对应排行榜。"
            )
            return

        count = int(self.config.get("ranking_display_count", 10) or 10)
        count = max(1, min(count, 50))
        rows = await asyncio.to_thread(self.db.get_ranking, count, game)
        if not rows:
            yield event.plain_result(
                f"{GAME_LABELS[game]}排行榜还没有人参与过呢~ 快来成为第一个玩家吧！✨"
            )
            return

        title = f"OurNotes {GAME_LABELS[game]}排行榜"
        path = await asyncio.to_thread(
            self.images.create_ranking_image, rows, title
        )
        if path:
            yield event.image_result(path)
        else:
            yield event.plain_result("生成排行榜图片时出错，请联系管理员。")

    # ---------- 绑定 / 解绑 ----------

    @filter.command(
        "on绑定",
        alias=_command_aliases(
            "on绑定", "ournotes绑定", "moenotes绑定",
            "on绑定QQ", "on绑定qq",
            "猜歌绑定", "猜卡面绑定", "猜曲绘绑定",
            "猜歌绑定QQ", "猜卡面绑定QQ", "猜曲绘绑定QQ",
        ),
    )
    async def bind_account(self, event: AstrMessageEvent):
        if not self._is_qq_official_event(event):
            yield event.plain_result("此绑定功能仅支持 QQ 官方机器人使用。")
            return

        parts = _strip_command_prefix(event.message_str).split(maxsplit=1)
        qq_user_id = parts[1].strip() if len(parts) > 1 else ""
        if not re.fullmatch(r"\d{5,12}", qq_user_id):
            yield event.plain_result(
                "请按“on绑定 QQ号”的格式输入，例如：on绑定 21555762216。"
            )
            return

        official_user_id = str(event.get_sender_id())
        current_user_id = await asyncio.to_thread(
            self.db.resolve_user_id, OFFICIAL_PLATFORM_NAME, official_user_id
        )
        if str(current_user_id) != official_user_id:
            yield event.plain_result(
                f"当前官方机器人账号已经绑定至 QQ号 {current_user_id}。"
            )
            return

        bound_count = await asyncio.to_thread(self.db.count_qq_bindings, qq_user_id)
        if bound_count >= MAX_BINDINGS_PER_QQ:
            yield event.plain_result("绑定失败：该QQ号已绑定两个官机账号，无法继续绑定。")
            return

        lines = [f"你确认将账号绑定至  {qq_user_id} ？官方机在三个游戏里的分数将迁移至该账号。"]
        if bound_count == 1:
            lines.append(
                "你已经绑定过一个官机了，你只能绑定两个官机，请确认你是否要绑定此账号。"
            )
        lines.append("发送“确认”将开始绑定。发送“取消”将取消绑定。")
        yield event.plain_result("\n".join(lines))

        decision = None

        @session_waiter(timeout=60)
        async def binding_waiter(
            controller: SessionController, answer_event: AstrMessageEvent
        ):
            nonlocal decision
            answer_text = _strip_command_prefix(answer_event.message_str)
            if answer_text == "确认":
                decision = "confirm"
                controller.stop()
            elif answer_text == "取消":
                decision = "cancel"
                controller.stop()

        try:
            await binding_waiter(
                event,
                session_filter=BindingSessionFilter(
                    event.unified_msg_origin, official_user_id
                ),
            )
        except TimeoutError:
            yield event.plain_result("绑定确认已超时，绑定操作已取消。")
            return

        if decision == "cancel":
            yield event.plain_result("已取消绑定。")
            return
        if decision != "confirm":
            yield event.plain_result("未收到有效的绑定确认，绑定操作已取消。")
            return

        bound = await asyncio.to_thread(
            self.db.bind_official_account,
            OFFICIAL_PLATFORM_NAME,
            official_user_id,
            qq_user_id,
        )
        if bound:
            yield event.plain_result(
                f"绑定成功！官方机的历史分数已迁移至 QQ号 {qq_user_id}。"
            )
        else:
            yield event.plain_result("绑定失败：该官方账号可能已绑定，请稍后重试。")

    @filter.command(
        "on解绑",
        alias=_command_aliases(
            "on解绑", "ournotes解绑", "moenotes解绑",
            "on解绑QQ", "on解绑qq",
            "猜歌解绑", "猜卡面解绑", "猜曲绘解绑",
        ),
    )
    async def unbind_account(self, event: AstrMessageEvent):
        if not self._is_qq_official_event(event):
            yield event.plain_result("此解绑功能仅支持 QQ 官方机器人使用。")
            return

        official_user_id = str(event.get_sender_id())
        current_user_id = await asyncio.to_thread(
            self.db.resolve_user_id, OFFICIAL_PLATFORM_NAME, official_user_id
        )
        if str(current_user_id) == official_user_id:
            yield event.plain_result("当前官方机器人账号尚未绑定QQ号。")
            return

        yield event.plain_result(
            f"你确认解除该官方机器人账号与 QQ号 {current_user_id} 的绑定？"
            "已迁移的历史分数不会退回，绑定名额将释放。\n"
            "发送“确认”将开始解绑。发送“取消”将取消解绑。"
        )

        decision = None

        @session_waiter(timeout=60)
        async def unbinding_waiter(
            controller: SessionController, answer_event: AstrMessageEvent
        ):
            nonlocal decision
            answer_text = _strip_command_prefix(answer_event.message_str)
            if answer_text == "确认":
                decision = "confirm"
                controller.stop()
            elif answer_text == "取消":
                decision = "cancel"
                controller.stop()

        try:
            await unbinding_waiter(
                event,
                session_filter=BindingSessionFilter(
                    event.unified_msg_origin, official_user_id
                ),
            )
        except TimeoutError:
            yield event.plain_result("解绑确认已超时，解绑操作已取消。")
            return

        if decision == "cancel":
            yield event.plain_result("已取消解绑。")
            return
        if decision != "confirm":
            yield event.plain_result("未收到有效的解绑确认，解绑操作已取消。")
            return

        unbound = await asyncio.to_thread(
            self.db.unbind_official_account,
            OFFICIAL_PLATFORM_NAME,
            official_user_id,
        )
        if unbound:
            yield event.plain_result(
                f"解绑成功，该官方机器人账号已解除与 QQ号 {current_user_id} 的绑定。"
            )
        else:
            yield event.plain_result("解绑失败，请稍后重试。")

    # ---------- 自定义名称 ----------

    @filter.command(
        "on自定义名称",
        alias=_command_aliases(
            "on自定义名称", "ournotes自定义名称", "moenotes自定义名称",
            "on昵称", "猜歌自定义名称", "猜卡面自定义名称", "猜曲绘自定义名称",
        ),
    )
    async def set_custom_name(self, event: AstrMessageEvent):
        if not self._is_group_allowed(event):
            reject = self._get_whitelist_reject_message()
            if reject:
                yield event.plain_result(reject)
            return

        user_id, platform = await self._get_account_identity(event)
        if self._is_event_user_blacklisted(event, user_id):
            yield event.plain_result("抱歉，你已被禁止使用此功能 😔")
            return

        parts = _strip_command_prefix(event.message_str).split(maxsplit=1)
        custom_name = parts[1].strip() if len(parts) > 1 else None
        if custom_name:
            if len(custom_name) > 20:
                yield event.plain_result("自定义名称过长，请限制在20个字符以内哦~")
                return
            if any(c in custom_name for c in ["\n", "\r", "\t", "\0"]):
                yield event.plain_result("自定义名称包含非法字符，请重新输入~")
                return
            await asyncio.to_thread(
                self.db.set_custom_name,
                user_id,
                event.get_sender_name() or user_id,
                custom_name,
                platform,
            )
            yield event.plain_result(
                f"好的！你的自定义名称已设置为：{custom_name} ✨"
            )
        else:
            cleared = await asyncio.to_thread(
                self.db.set_custom_name,
                user_id,
                event.get_sender_name() or user_id,
                None,
                platform,
            )
            if cleared:
                yield event.plain_result("好的！你的自定义名称已清除 ✨")
            else:
                yield event.plain_result("你还没有设置过自定义名称哦~ 🎮")

    # ---------- 管理员 ----------

    @filter.command(
        "on重置次数",
        alias=_command_aliases(
            "on重置次数", "ournotes重置次数", "on重置",
            "重置猜歌次数", "重置猜卡面次数", "重置猜曲绘次数",
        ),
    )
    async def reset_daily_limit(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id())
        if not self._is_super_user(sender_id):
            yield event.plain_result("只有管理员才能使用此命令哦~")
            return

        parts = _strip_command_prefix(event.message_str).split()
        target_id = parts[1] if len(parts) > 1 and parts[1].isdigit() else sender_id
        ok = await asyncio.to_thread(self.db.reset_daily_limit, target_id)
        if ok:
            yield event.plain_result(f"已重置用户 {target_id} 的今日游戏次数。")
        else:
            yield event.plain_result(f"未找到用户 {target_id} 的今日游戏记录。")

    @filter.command(
        "on刷新题库",
        alias=_command_aliases(
            "on刷新题库", "ournotes刷新题库",
            "刷新题库", "刷新本地数据", "重载题库",
        ),
    )
    async def refresh_master_data(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id())
        if not self._is_super_user(sender_id):
            yield event.plain_result("只有管理员才能使用此命令哦~")
            return

        yield event.plain_result("正在检查 master 题库更新，请稍候…")
        try:
            updated = await self.master_data.refresh_if_stale(force=True)
            self._rebuild_indexes()
            if updated:
                yield event.plain_result(
                    f"题库更新完成！\n"
                    f"歌曲: {len(self.songs)}\n"
                    f"卡牌: {len(self.cards)}\n"
                    f"角色: {len(self.characters)}"
                )
            else:
                yield event.plain_result("题库已是最新版本。")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 手动刷新题库失败: {exc}", exc_info=True)
            yield event.plain_result(f"刷新失败: {exc}")

    @filter.command(
        "on题库状态",
        alias=_command_aliases(
            "on题库状态", "ournotes题库状态", "题库状态", "题库信息"
        ),
    )
    async def show_master_status(self, event: AstrMessageEvent):
        if not self._is_group_allowed(event):
            reject = self._get_whitelist_reject_message()
            if reject:
                yield event.plain_result(reject)
            return

        member_cards = sum(1 for c in self.cards if c.get("kind") == "member")
        support_cards = sum(1 for c in self.cards if c.get("kind") == "support")
        yield event.plain_result(
            "📚 OurNotes 题库状态\n"
            f"歌曲: {len(self.songs)}\n"
            f"卡牌: {len(self.cards)}（成员卡 {member_cards} / 支援卡 {support_cards}）\n"
            f"角色: {len(self.characters)}\n"
            "数据源: moenotes-masterdata / hk-tw-mo\n"
            f"资源源: {self.assets.base_url}/{self.assets.language}"
        )
