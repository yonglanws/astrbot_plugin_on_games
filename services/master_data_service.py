"""OurNotes / MoeNotes master 数据同步服务。

数据源（仅拉取游玩所需的最小集合）：
- StarMoe-org/moenotes-masterdata 的 hk-tw-mo 快照：
  MasterLiveMusic.json / MasterMemberCard.json / MasterSupportCard.json /
  MasterCharacter.json / MasterBand.json / MasterSound.json /
  MasterSoundCueSheet.json / MasterText.json
- current_version.json 只用来做版本守卫：版本未变化时不会重新下载大文件。

下载顺序优先 jsDelivr CDN（国内通常明显快于 GitHub raw），再回退 GitHub raw
与 gitmirror。所有原始文件与派生题库都会落盘，游戏运行时只读派生数据。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 单元测试
    import logging

    logger = logging.getLogger("astrbot_plugin_on_games")

from .http_client import HttpClient

REPO = "StarMoe-org/moenotes-masterdata"
BRANCH = "main"
REGION = "hk-tw-mo"

# 派生规则版本：字段/筛选规则变化时 +1，启动时用本地原始文件离线重建。
DERIVED_RULE = 6

# 猜卡面只保留 SR 及以上稀有度（R 卡过于简单，不进卡池）。
MIN_CARD_RARITY = 3

MASTER_FILES = (
    "MasterLiveMusic.json",
    "MasterMemberCard.json",
    "MasterSupportCard.json",
    "MasterCharacter.json",
    "MasterBand.json",
    "MasterSound.json",
    "MasterSoundCueSheet.json",
    "MasterText.json",
)

# 小文件（版本号）优先 CDN。
_VERSION_URLS = (
    f"https://cdn.jsdelivr.net/gh/{REPO}@{BRANCH}/current_version.json",
    f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/current_version.json",
    f"https://raw.gitmirror.com/{REPO}/{BRANCH}/current_version.json",
)


def _file_urls(filename: str) -> list[str]:
    return [
        f"https://cdn.jsdelivr.net/gh/{REPO}@{BRANCH}/{REGION}/{filename}",
        f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{REGION}/{filename}",
        f"https://raw.gitmirror.com/{REPO}/{BRANCH}/{REGION}/{filename}",
    ]


def _localize(row: Optional[dict], field: str = "_simplifiedChinese") -> str:
    """按简中 → 繁中 → 日文 → 英文顺序取文本。"""
    if not row:
        return ""
    for key in (field, "_traditionalChinese", "_japanese", "_english", "_korean"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class MasterDataService:
    """master 数据的下载、缓存、派生与查询。"""

    def __init__(
        self,
        data_dir: Path,
        update_interval_hours: int = 24,
        request_timeout: int = 90,
        retries: int = 3,
    ):
        self.data_dir = Path(data_dir)
        self.root_dir = self.data_dir / "masterdata"
        self.raw_dir = self.root_dir / "raw"
        self.derived_dir = self.root_dir / "derived"
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.derived_dir.mkdir(parents=True, exist_ok=True)

        self.update_interval_hours = max(1, int(update_interval_hours))
        self.request_timeout = max(20, int(request_timeout))
        self.retries = max(1, int(retries))

        self.meta_path = self.root_dir / "meta.json"
        self.meta: dict[str, Any] = {}

        self.songs: list[dict] = []
        self.cards: list[dict] = []
        self.characters: list[dict] = []
        self.bands: list[dict] = []
        self.version: str = "unknown"

        self._http = HttpClient(
            timeout=self.request_timeout,
            concurrency=3,
            retries=self.retries,
        )
        self._fetch_lock = asyncio.Lock()
        self._update_task: Optional[asyncio.Task] = None
        self._stopping = False

        # 由插件注入：题库更新后重建内存索引。
        self.on_updated = None

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        await self._http.start()
        self._load_meta()
        loaded = self._load_derived()
        if loaded:
            logger.info(
                f"[OurNotes] 本地题库加载完成：{len(self.songs)} 首歌 / "
                f"{len(self.cards)} 张卡 / {len(self.characters)} 个角色，"
                f"版本 {self.version}"
            )
            self._notify_updated()

        self._update_task = asyncio.create_task(self._update_loop())

    async def terminate(self) -> None:
        self._stopping = True
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except (asyncio.CancelledError, Exception):
                pass
            self._update_task = None
        await self._http.close()

    # ---------- 查询 ----------

    def is_ready(self) -> bool:
        return bool(self.songs and self.cards and self.characters)

    def get_songs(self) -> list[dict]:
        return self.songs

    def get_cards(self) -> list[dict]:
        return self.cards

    def get_characters(self) -> list[dict]:
        return self.characters

    def get_bands(self) -> list[dict]:
        return self.bands

    def get_version(self) -> str:
        return self.version

    # ---------- 周期更新 ----------

    async def _update_loop(self) -> None:
        # 启动后立即检查一次；之后每 30 分钟检查是否超过更新间隔。
        try:
            await self.refresh_if_stale()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 首次题库检查失败: {exc}", exc_info=True)

        while not self._stopping:
            try:
                await asyncio.sleep(1800)
                await self.refresh_if_stale()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[OurNotes] 周期题库更新失败: {exc}", exc_info=True)

    async def refresh_if_stale(self, force: bool = False) -> bool:
        """按配置间隔刷新题库；返回是否有更新。"""
        async with self._fetch_lock:
            now = time.time()
            interval = self.update_interval_hours * 3600
            last_check = float(self.meta.get("checked_at") or 0)
            if not force and self.is_ready() and now - last_check < interval:
                return False

            remote = await self._fetch_remote_version()
            remote_version = ""
            if isinstance(remote, dict):
                region_info = (remote.get("regions") or {}).get(REGION) or {}
                remote_version = str(region_info.get("version") or "").strip()

            # 派生规则变化时优先用本地原始 master 文件离线重建，避免网络失败后
            # 继续沿用旧规则的派生数据（例如卡面 key 仍是缩略图）。
            rule = int(self.meta.get("derived_rule") or 0)
            if (
                not force
                and rule != DERIVED_RULE
                and (not remote_version or remote_version == self.meta.get("version"))
            ):
                if self._rebuild_from_local_raw(now):
                    return True

            if (
                not force
                and remote_version
                and remote_version == self.meta.get("version")
                and self.is_ready()
                and int(self.meta.get("derived_rule") or 0) == DERIVED_RULE
            ):
                self.meta["checked_at"] = now
                self._save_meta()
                return False

            if not remote_version:
                if not force and self.is_ready():
                    logger.warning(
                        "[OurNotes] 无法获取 master 版本号，继续使用本地题库。"
                    )
                    self.meta["checked_at"] = now
                    self._save_meta()
                    return False
                logger.warning(
                    "[OurNotes] 无法获取 master 版本号，将尝试直接刷新（可能重复下载）。"
                )

            logger.info("[OurNotes] 检测到题库更新，开始下载 master 数据…")
            raw = await self._download_all()
            if raw is None:
                logger.error("[OurNotes] master 数据下载失败，继续使用本地缓存。")
                return False

            try:
                songs, cards, characters, bands = self._build_derived(raw)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[OurNotes] 派生题库构建失败: {exc}", exc_info=True)
                return False

            if not songs or not cards or not characters:
                logger.error("[OurNotes] 派生题库为空，保留旧数据。")
                return False

            self._write_raw(raw)
            self._store_derived(songs, cards, characters, bands)

            self.version = remote_version or datetime.now().strftime("%Y%m%d")
            self.meta = {
                "version": self.version,
                "checked_at": now,
                "updated_at": now,
                "derived_rule": DERIVED_RULE,
                "counts": {
                    "songs": len(songs),
                    "cards": len(cards),
                    "characters": len(characters),
                },
            }
            self._save_meta()
            self._notify_updated()
            logger.info(
                f"[OurNotes] 题库更新完成：{len(songs)} 首歌 / {len(cards)} 张卡 / "
                f"{len(characters)} 个角色，版本 {self.version}"
            )
            return True

    # ---------- 下载 ----------

    async def _fetch_remote_version(self) -> Optional[dict]:
        data = await self._http.get_first(
            _VERSION_URLS,
            retries=2,
            timeout=min(30, self.request_timeout),
            max_bytes=2 * 1024 * 1024,
        )
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OurNotes] current_version.json 解析失败: {exc}")
            return None

    def _load_local_raw(self) -> Optional[dict[str, Any]]:
        region_dir = self.raw_dir / REGION
        raw: dict[str, Any] = {}
        for filename in MASTER_FILES:
            path = region_dir / filename
            if not path.exists():
                return None
            try:
                raw[filename] = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[OurNotes] 本地 master 文件解析失败 {path}: {exc}")
                return None
        return raw

    def _rebuild_from_local_raw(self, now: float) -> bool:
        """用本地原始 master 数据按当前派生规则重建题库，不依赖网络。"""
        raw = self._load_local_raw()
        if raw is None:
            return False
        try:
            songs, cards, characters, bands = self._build_derived(raw)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 本地题库重建失败: {exc}", exc_info=True)
            return False
        if not songs or not cards or not characters:
            return False
        self._store_derived(songs, cards, characters, bands)
        self.version = str(self.meta.get("version") or "")
        self.meta["derived_rule"] = DERIVED_RULE
        self.meta["checked_at"] = now
        self._save_meta()
        self._notify_updated()
        logger.info(
            f"[OurNotes] 已用本地 master 数据重建题库（规则 {DERIVED_RULE}）："
            f"{len(songs)} 首歌 / {len(cards)} 张卡 / {len(characters)} 个角色。"
        )
        return True

    async def _download_all(self) -> Optional[dict[str, Any]]:
        semaphore = asyncio.Semaphore(3)
        results: dict[str, Any] = {}
        errors: list[str] = []

        async def fetch_one(filename: str) -> None:
            async with semaphore:
                data = await self._http.get_first(
                    _file_urls(filename),
                    retries=2,
                    timeout=self.request_timeout,
                    max_bytes=64 * 1024 * 1024,
                )
                if not data:
                    errors.append(filename)
                    return
                try:
                    results[filename] = json.loads(data.decode("utf-8"))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{filename}: {exc}")

        await asyncio.gather(*(fetch_one(name) for name in MASTER_FILES))
        if errors:
            logger.error(f"[OurNotes] 以下 master 文件下载/解析失败: {', '.join(errors)}")
            return None
        return results

    def _write_raw(self, raw: dict[str, Any]) -> None:
        region_dir = self.raw_dir / REGION
        region_dir.mkdir(parents=True, exist_ok=True)
        for filename, data in raw.items():
            self._atomic_write_json(region_dir / filename, data)

    @staticmethod
    def _atomic_write_json(path: Path, data: Any, indent: Optional[int] = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=indent),
            encoding="utf-8",
        )
        tmp.replace(path)

    # ---------- 派生题库 ----------

    def _build_derived(
        self, raw: dict[str, Any]
    ) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        texts = {
            row.get("_id"): row
            for row in (raw.get("MasterText.json") or {}).get("_allData", [])
            if isinstance(row, dict)
        }

        def text(tid: Any) -> str:
            return _localize(texts.get(tid))

        def field(row: Optional[dict], key: str) -> str:
            """只读取指定语言的字段，不做跨语言回退。"""
            if not row:
                return ""
            value = row.get(key)
            return value.strip() if isinstance(value, str) else ""

        def display(row: Optional[dict], primary_key: str, fallback_key: str) -> str:
            """显示名：先取本地化首选语言，再取指定备选语言，都不为空则返回。"""
            value = _localize(row, f"_{primary_key}")
            if value:
                return value
            return field(row, f"_{fallback_key}")

        # 角色
        band_rows = (raw.get("MasterBand.json") or {}).get("_allData", [])
        bands: list[dict] = []
        band_names: dict[int, str] = {}
        for row in band_rows:
            band_id = row.get("_id")
            if band_id is None:
                continue
            name = text(row.get("_nameTextID"))
            band_names[int(band_id)] = name
            bands.append(
                {
                    "id": int(band_id),
                    "name": name,
                    "main_color": row.get("_mainColorCode") or "",
                }
            )

        characters: list[dict] = []
        character_names: dict[int, str] = {}
        for row in (raw.get("MasterCharacter.json") or {}).get("_allData", []):
            char_id = row.get("_id")
            if char_id is None:
                continue
            name = text(row.get("_nameTextID"))
            if not name:
                continue
            name_text = texts.get(row.get("_nameTextID"))
            short_text = texts.get(row.get("_shortNameTextID"))
            short_name = display(short_text, "simplifiedChinese", "japanese") or name
            # 角色英文名：MasterText 的 _english 字段存的是本地化文本，
            # 真正的英文名/英文短名在 _japanese 字段里，因此日英两列都要读。
            en_name_raw = (
                field(texts.get(row.get("_enDisplayNameTextId")), "_japanese")
                or field(texts.get(row.get("_enDisplayNameTextId")), "_english")
            )
            en_short_name = (
                field(texts.get(row.get("_enDisplayShortNameTextId")), "_japanese")
                or field(texts.get(row.get("_enDisplayShortNameTextId")), "_english")
            )
            en_name_parts = [
                part.strip() for part in re.split(r"[\s/]+", en_name_raw) if part.strip()
            ]
            en_name = " ".join(en_name_parts)
            band_id = int(row.get("_bandID") or 0)
            character_names[int(char_id)] = name
            characters.append(
                {
                    "id": int(char_id),
                    "name": name,
                    "short_name": short_name,
                    "en_name": en_name,
                    "en_name_parts": en_name_parts,
                    "en_short_name": en_short_name,
                    "name_traditional": field(name_text, "_traditionalChinese"),
                    "short_name_traditional": field(short_text, "_traditionalChinese"),
                    "name_japanese": field(name_text, "_japanese"),
                    "name_english": field(name_text, "_english"),
                    "band_id": band_id,
                    "band_name": band_names.get(band_id, ""),
                    "main_color": row.get("_mainColorCode") or "",
                }
            )

        # 声音 cue：musicSoundID -> MasterSound -> MasterSoundCueSheet
        sounds = {
            row.get("_id"): row
            for row in (raw.get("MasterSound.json") or {}).get("_allData", [])
            if isinstance(row, dict)
        }
        cue_sheets = {
            row.get("_id"): row.get("_cueSheetName") or ""
            for row in (raw.get("MasterSoundCueSheet.json") or {}).get("_allData", [])
            if isinstance(row, dict)
        }

        # 歌曲
        songs: list[dict] = []
        for row in (raw.get("MasterLiveMusic.json") or {}).get("_allData", []):
            music_id = row.get("_id")
            title = text(row.get("_titleTextID"))
            jacket_asset = (row.get("_jacketAssetName") or "").strip()
            if music_id is None or not title or not jacket_asset:
                continue

            title_text = texts.get(row.get("_titleTextID"))
            # 日文标题：MasterText 的 _japanese 列是日文原文；
            # 英文标题同样在 _japanese 列，_english 列才是英文译文。
            title_ja = field(title_text, "_japanese")
            title_traditional = field(title_text, "_traditionalChinese")
            title_english = field(title_text, "_english")
            phonetic = text(row.get("_phoneticTextID"))
            sound = sounds.get(row.get("_musicSoundID")) or {}
            sheet = cue_sheets.get(sound.get("_soundCueSheetID")) or ""
            cue = str(sound.get("_cueName") or "").strip()
            audio_key = f"Cri/Sound/{sheet}/{cue}.m4a" if sheet and cue else ""

            band_ids = row.get("_bandIDs") or []
            band_id = int(band_ids[0]) if band_ids else 0
            songs.append(
                {
                    "id": int(music_id),
                    "title": title,
                    "title_ja": title_ja if title_ja and title_ja != title else "",
                    "title_traditional": (
                        title_traditional
                        if title_traditional and title_traditional != title
                        else ""
                    ),
                    "title_english": (
                        title_english
                        if title_english and title_english != title
                        else ""
                    ),
                    "phonetic": phonetic if phonetic and phonetic != title else "",
                    "band_id": band_id,
                    "band_name": band_names.get(band_id, ""),
                    "jacket_asset": jacket_asset,
                    "jacket_key": f"Image/Jacket/{jacket_asset}/{jacket_asset}.webp",
                    "audio_key": audio_key,
                    "start_at": row.get("_startAt") or "",
                    "music_type": row.get("_musicType") or 0,
                }
            )

        # 卡牌：成员卡 + 支援卡统一进猜卡面池；R 卡太简单，只保留 SR 以上。
        cards: list[dict] = []
        min_rarity = MIN_CARD_RARITY

        def rarity_label(rarity: int) -> str:
            return {2: "R", 3: "SR", 4: "SSR", 10: "EX", 20: "BD"}.get(
                int(rarity), str(rarity)
            )

        for row in (raw.get("MasterMemberCard.json") or {}).get("_allData", []):
            if int(row.get("_rarity") or 0) < min_rarity:
                continue
            card_id = row.get("_id")
            asset_id = row.get("_assetID")
            char_id = row.get("_characterID")
            if card_id is None or asset_id is None or char_id is None:
                continue
            char_ids = [int(char_id)]
            names = [character_names.get(int(char_id), "")]
            cards.append(
                {
                    "id": int(card_id),
                    "kind": "member",
                    "asset_id": int(asset_id),
                    "character_ids": char_ids,
                    "character_names": [n for n in names if n],
                    "display_name": names[0] if names else "",
                    "rarity": int(row.get("_rarity") or 0),
                    "rarity_label": rarity_label(row.get("_rarity") or 0),
                    "card_type": int(row.get("_cardType") or 0),
                    "title": text(row.get("_subtitleTextID")),
                    # member_thumbnail 本身就是裁切后的区域，题目与结算统一用完整卡面。
                    "image_key": (
                        f"MemberCard/{int(asset_id)}/member_full/member_full.webp"
                    ),
                    "full_key": (
                        f"MemberCard/{int(asset_id)}/member_full/member_full.webp"
                    ),
                    "start_at": row.get("_startAt") or "",
                }
            )

        for row in (raw.get("MasterSupportCard.json") or {}).get("_allData", []):
            if int(row.get("_rarity") or 0) < min_rarity:
                continue
            card_id = row.get("_id")
            asset_id = row.get("_assetID")
            char_ids = [int(x) for x in (row.get("_characterIDs") or [])]
            if card_id is None or asset_id is None or not char_ids:
                continue
            names = [character_names.get(cid, "") for cid in char_ids]
            names = [n for n in names if n]
            card_name = text(row.get("_nameTextID"))
            # 多角色支援卡直接列出角色名，避免把“MyGO!!!!!”当成唯一答案。
            display_name = "、".join(names) if names else card_name
            cards.append(
                {
                    "id": int(card_id),
                    "kind": "support",
                    "asset_id": int(asset_id),
                    "character_ids": char_ids,
                    "character_names": names,
                    "display_name": display_name or (names[0] if names else ""),
                    "card_name": card_name,
                    "rarity": int(row.get("_rarity") or 0),
                    "rarity_label": rarity_label(row.get("_rarity") or 0),
                    "card_type": int(row.get("_cardType") or 0),
                    "title": text(row.get("_descriptionTextID")),
                    "image_key": f"SupportCard/{int(asset_id)}/snap_full/snap_full.webp",
                    "full_key": f"SupportCard/{int(asset_id)}/snap_full/snap_full.webp",
                    "start_at": row.get("_startAt") or "",
                }
            )

        songs.sort(key=lambda x: x["id"])
        cards.sort(key=lambda x: (x["kind"], x["id"]))
        characters.sort(key=lambda x: x["id"])
        return songs, cards, characters, bands

    def _store_derived(
        self,
        songs: list[dict],
        cards: list[dict],
        characters: list[dict],
        bands: list[dict],
    ) -> None:
        self.songs = songs
        self.cards = cards
        self.characters = characters
        self.bands = bands
        self._atomic_write_json(self.derived_dir / "songs.json", songs)
        self._atomic_write_json(self.derived_dir / "cards.json", cards)
        self._atomic_write_json(self.derived_dir / "characters.json", characters)
        self._atomic_write_json(self.derived_dir / "bands.json", bands)
        self._atomic_write_json(
            self.derived_dir / "all.json",
            {
                "songs": songs,
                "cards": cards,
                "characters": characters,
                "bands": bands,
            },
        )

    # ---------- 本地加载 ----------

    def _load_meta(self) -> None:
        if not self.meta_path.exists():
            self.meta = {}
            return
        try:
            self.meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OurNotes] meta.json 解析失败，将重建: {exc}")
            self.meta = {}

    def _save_meta(self) -> None:
        self._atomic_write_json(self.meta_path, self.meta, indent=1)

    def _load_derived(self) -> bool:
        # 派生规则变化（如卡池稀有度筛选）时，本地派生数据不可用，需重建。
        rule = int(self.meta.get("derived_rule") or 0)
        if rule != DERIVED_RULE:
            return False
        all_path = self.derived_dir / "all.json"
        if all_path.exists():
            try:
                data = json.loads(all_path.read_text(encoding="utf-8"))
                songs = data.get("songs") or []
                cards = data.get("cards") or []
                characters = data.get("characters") or []
                bands = data.get("bands") or []
                if songs and cards and characters:
                    self.songs = songs
                    self.cards = cards
                    self.characters = characters
                    self.bands = bands
                    self.version = str(self.meta.get("version") or "unknown")
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[OurNotes] 派生题库解析失败: {exc}")

        # 兼容只有拆分文件的旧缓存
        try:
            songs = json.loads(
                (self.derived_dir / "songs.json").read_text(encoding="utf-8")
            )
            cards = json.loads(
                (self.derived_dir / "cards.json").read_text(encoding="utf-8")
            )
            characters = json.loads(
                (self.derived_dir / "characters.json").read_text(encoding="utf-8")
            )
            bands_path = self.derived_dir / "bands.json"
            bands = (
                json.loads(bands_path.read_text(encoding="utf-8"))
                if bands_path.exists()
                else []
            )
            if songs and cards and characters:
                self.songs = songs
                self.cards = cards
                self.characters = characters
                self.bands = bands
                self.version = str(self.meta.get("version") or "unknown")
                return True
        except Exception:
            pass
        return False

    def _notify_updated(self) -> None:
        if not self.on_updated:
            return
        try:
            result = self.on_updated()
            if asyncio.iscoroutine(result):
                asyncio.get_running_loop().create_task(result)
        except RuntimeError:
            try:
                result.close()  # type: ignore[union-attr]
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OurNotes] 题库更新回调失败: {exc}")
