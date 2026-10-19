"""服务层测试：master 数据、资源缓存、图片效果、音频截取、数据库。

运行：
    python -m pytest tests -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.asset_service import AssetService  # noqa: E402
from services.audio_service import AudioService  # noqa: E402
from services.db_service import GAMES, DatabaseService  # noqa: E402
from services.image_service import ImageService  # noqa: E402
from services.master_data_service import MasterDataService  # noqa: E402


def _config(**overrides) -> dict:
    config = {
        "request_timeout": 90,
        "download_retries": 3,
        "download_concurrency": 4,
        "asset_base_url": "https://assets.bdon.moe",
        "asset_language": "zh-Hans",
        "prefetch_concurrency": 2,
        "asset_cache_max_mb": 1024,
        "clip_duration_seconds": 10,
        "clip_start_ratio_min": 0.1,
        "clip_start_ratio_max": 0.5,
        "random_mode_decay_factor": 0.75,
        "card_effects": {},
        "jacket_effects": {},
    }
    config.update(overrides)
    return config


# ---------------------------------------------------------------- master


@pytest.mark.asyncio
async def test_master_data_download_and_build(tmp_path):
    service = MasterDataService(tmp_path, update_interval_hours=24)
    await service._http.start()
    try:
        updated = await service.refresh_if_stale(force=True)
        assert updated is True
        assert service.is_ready()

        songs = service.get_songs()
        cards = service.get_cards()
        characters = service.get_characters()

        assert len(songs) >= 80, f"songs={len(songs)}"
        assert len(cards) >= 80, f"cards={len(cards)}"
        assert len(characters) >= 20, f"characters={len(characters)}"

        # 猜卡面卡池只保留 SR 以上（成员卡 + 支援卡，R 卡被排除）
        assert cards and all(c["rarity"] >= 3 for c in cards), {
            c["rarity_label"] for c in cards
        }

        song = songs[0]
        assert song["id"] and song["title"]
        assert song["jacket_key"].startswith("Image/Jacket/")
        assert song["audio_key"].startswith("Cri/Sound/")

        kinds = {c["kind"] for c in cards}
        assert kinds == {"member", "support"}
        for card in cards[:10]:
            assert card["image_key"]
            assert card["full_key"]
            assert card["character_ids"]
            assert card["rarity_label"]

        # 派生文件落盘
        assert (tmp_path / "masterdata" / "derived" / "all.json").exists()
        assert (tmp_path / "masterdata" / "derived" / "songs.json").exists()

        # 官方多语言字段：没有中文名时保留原歌名；别名全部来自 master。
        for song in songs:
            assert song.get("title")
            assert "title_traditional" in song
            assert "title_english" in song
        for char in characters:
            assert char.get("name")
            assert "en_short_name" in char
            assert "name_traditional" in char
    finally:
        await service.terminate()


@pytest.mark.asyncio
async def test_master_data_uses_local_cache(tmp_path):
    """第二次启动应读取本地派生数据；版本未变时不重新下载。"""
    first = MasterDataService(tmp_path, update_interval_hours=24)
    await first._http.start()
    try:
        assert await first.refresh_if_stale(force=True)
        version = first.get_version()
        song_count = len(first.get_songs())
        card_count = len(first.get_cards())
    finally:
        await first.terminate()

    second = MasterDataService(tmp_path, update_interval_hours=24)
    await second._http.start()
    try:
        second._load_meta()
        assert second._load_derived()
        assert second.get_version() == version
        assert len(second.get_songs()) == song_count
        assert len(second.get_cards()) == card_count

        # 模拟“已过检查间隔但版本未变”：只拉取很小的 current_version.json 后跳过。
        second.meta["checked_at"] = 0
        updated = await second.refresh_if_stale()
        assert updated is False
    finally:
        await second.terminate()


# ---------------------------------------------------------------- assets
@pytest.mark.asyncio
async def test_asset_download_and_cache(tmp_path):
    assets = AssetService(tmp_path, _config())
    await assets.start()
    try:
        member_key = "MemberCard/1/member_thumbnail/member_thumbnail.webp"
        support_key = "SupportCard/1/snap_thumbnail/snap_thumbnail.webp"
        jacket_key = "Image/Jacket/jkt_001_100001/jkt_001_100001.webp"
        audio_key = "Cri/Sound/M_Mayoiuta/M_Mayoiuta.m4a"

        member = await assets.fetch_image(member_key)
        assert member is not None and member.width > 0 and member.height > 0
        member.close()

        support = await assets.fetch_image(support_key)
        assert support is not None and support.width > 0
        support.close()

        jacket = await assets.fetch_image(jacket_key)
        assert jacket is not None and jacket.width > 0
        jacket.close()

        audio_path = await assets.fetch_audio_path(audio_key)
        assert audio_path is not None and audio_path.exists()
        assert audio_path.stat().st_size > 100_000

        # 第二次读取应命中磁盘缓存
        cached = assets.cache_path(member_key)
        assert cached.exists()
        again = await assets.fetch_bytes(member_key)
        assert again and again == cached.read_bytes()

        assert assets.get_url(member_key).startswith(
            "https://assets.bdon.moe/zh-Hans/MemberCard/"
        )
    finally:
        await assets.close()


# ---------------------------------------------------------------- images


def test_image_effects_and_rendering(tmp_path):
    image_service = ImageService(ROOT / "res", tmp_path, _config())

    base = Image.new("RGBA", (300, 400), (90, 120, 200, 255))
    for name in image_service.card_effects:
        result = image_service.apply_card_effects(base, [name])
        assert isinstance(result, Image.Image)
        assert result.width > 0 and result.height > 0
        result.close()

    for name in image_service.jacket_effects:
        result, difficulty = image_service.apply_jacket_effect(base, name)
        assert isinstance(result, Image.Image)
        assert difficulty >= 1
        result.close()

    # 选项图：12 个曲绘（两列卡片样式）
    options = [
        {"title": f"歌曲{i + 1}", "title_ja": f"Song {i + 1}"} for i in range(12)
    ]
    jackets = [
        Image.new("RGB", (200, 200), (40 * i % 255, 80, 160)) for i in range(12)
    ]
    path = image_service.create_options_image(options, jackets, tmp_path)
    assert path and Path(path).exists()
    with Image.open(path) as img:
        assert img.size == (1020, 1180)

    # 排行榜
    rows = [
        ("10001", "玩家A", "自定义A", 30, 10, 8, 0),
        ("10002", "玩家B", None, 20, 9, 6, 1),
    ]
    ranking = image_service.create_ranking_image(rows, "OurNotes 测试排行榜", tmp_path)
    assert ranking and Path(ranking).exists()
    with Image.open(ranking) as img:
        assert img.width == 850


# ---------------------------------------------------------------- audio


def _make_sine(tmp_path: Path, seconds: int = 30) -> Path:
    from services.audio_service import _find_ffmpeg

    ffmpeg = _find_ffmpeg()
    assert ffmpeg, "需要 ffmpeg（可 pip install imageio-ffmpeg）"
    path = tmp_path / "sine.mp3"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


class _FakeAssets:
    def __init__(self, path: Path):
        self.path = path

    async def fetch_audio_path(self, key: str):
        return self.path


@pytest.mark.asyncio
async def test_audio_clips(tmp_path):
    source = _make_sine(tmp_path, 30)
    service = AudioService(_FakeAssets(source), _config(), tmp_path / "out")
    try:
        normal = await service.get_song_clip({"audio_key": "x", "title": "test"})
        speed = await service.get_song_clip(
            {"audio_key": "x", "title": "test"}, speed=2.0
        )
        reverse = await service.get_song_clip(
            {"audio_key": "x", "title": "test"}, reverse=True
        )
        for path in (normal, speed, reverse):
            assert path and Path(path).exists()
            assert Path(path).stat().st_size > 1000
            duration = service._probe_duration_sync(Path(path))
            assert 8.5 <= duration <= 11.5, (path, duration)

        # 随机模式分数范围
        for _ in range(20):
            speed_mult, is_reverse, score, display = service.get_random_mode()
            assert speed_mult in (1.0, 2.0)
            assert isinstance(is_reverse, bool)
            assert 1 <= score <= 3
            assert display
    finally:
        await service.terminate()


# ---------------------------------------------------------------- database


def test_database_stats_and_binding(tmp_path):
    db = DatabaseService(str(tmp_path / "test.db"))
    db.init_db()

    db.update_stats("u1", "玩家1", "aiocqhttp", "song", 3, True)
    db.update_stats("u1", "玩家1", "aiocqhttp", "card", 2, True)
    db.update_stats("u2", "玩家2", "aiocqhttp", "song", 5, True)
    db.update_stats("u2", "玩家2", "aiocqhttp", "jacket", 1, False)

    total = db.get_user_total_stats("u1", "aiocqhttp")
    assert total == (5, 2, 2)
    assert db.get_user_game_stats("u1", "aiocqhttp", "song") == (3, 1, 1)

    ranking = db.get_ranking(10)
    assert ranking[0][0] == "u2"
    assert ranking[0][3] == 6

    song_ranking = db.get_ranking(10, "song")
    assert song_ranking[0][0] == "u2"

    # 每日次数
    assert db.can_play_today("u1", "aiocqhttp", "song", 1, False) is True
    db.update_user_play("u1", "玩家1", "aiocqhttp", "song")
    assert db.can_play_today("u1", "aiocqhttp", "song", 1, False) is False
    assert db.can_play_today("u1", "aiocqhttp", "song", 2, False) is True
    # 独立次数模式下猜卡面仍然是 0
    assert db.can_play_today("u1", "aiocqhttp", "card", 1, True) is True
    assert db.get_today_plays("u1", "aiocqhttp", None) == 1
    db.reset_daily_limit("u1")
    assert db.get_today_plays("u1", "aiocqhttp", None) == 0

    # 自定义名称
    assert db.set_custom_name("u1", "玩家1", "小灯", "aiocqhttp") is True
    assert db.get_profile("u1", "aiocqhttp")[1] == "小灯"

    # 官机绑定：三个游戏的历史分数一起迁移
    db.update_stats("12345678", "QQ用户", "aiocqhttp", "song", 3, True)
    db.update_stats("12345678", "QQ用户", "aiocqhttp", "card", 2, True)
    for game in GAMES:
        db.update_stats("official_qid", "官机用户", "qq_official", game, 4, True)
    db.update_user_play("official_qid", "官机用户", "qq_official", "song")

    assert db.bind_official_account("qq_official", "official_qid", "12345678")
    assert db.resolve_user_id("qq_official", "official_qid") == "12345678"
    merged = db.get_user_all_stats("12345678", "aiocqhttp")
    assert merged["song"][0] == 3 + 4
    assert merged["card"][0] == 2 + 4
    assert merged["jacket"][0] == 4
    assert db.get_user_all_stats("official_qid", "qq_official") == {}

    # 绑定上限 2
    assert db.bind_official_account("qq_official", "official2", "12345678")
    assert not db.bind_official_account("qq_official", "official3", "12345678")

    assert db.unbind_official_account("qq_official", "official_qid")
    assert db.resolve_user_id("qq_official", "official_qid") == "official_qid"


# ---------------------------------------------------------------- config


def test_config_schema():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    required = {
        "update_interval_hours",
        "asset_base_url",
        "asset_language",
        "prefetch_assets",
        "answer_timeout",
        "daily_play_limit",
        "card_effects",
        "jacket_effects",
        "connect_link_template",
        "quick_entries",
    }
    assert required <= set(schema)
    assert schema["asset_base_url"]["default"] == "https://assets.bdon.moe"
    assert schema["asset_language"]["default"] == "zh-Hans"
    assert set(schema["card_effects"]["items"]) >= {
        "light_blur",
        "heavy_blur",
        "horizontal_slice",
        "vertical_slice",
        "two_strips",
        "three_strips",
    }
    assert set(schema["jacket_effects"]["items"]) == {
        "blur",
        "random_crop",
        "shuffle_blocks_easy",
        "shuffle_blocks_hard",
    }
