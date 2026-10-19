"""MoeNotes 资源服务：CF 友好的下载、磁盘缓存与后台预热。

资源 URL 规则（与 moenotes 源码一致）：
    {asset_base_url}[/{region}]/{language}/{key}
例如：
    https://assets.bdon.moe/zh-Hans/MemberCard/69/member_full/member_full.webp
    https://assets.bdon.moe/zh-Hans/Image/Jacket/jkt_001_100001/jkt_001_100001.webp
    https://assets.bdon.moe/zh-Hans/Cri/Sound/M_Mayoiuta/M_Mayoiuta.m4a

Cloudflare 首次回源可能较慢，之后边缘缓存会很快。插件侧再做一层磁盘缓存，
并通过后台预热把曲绘/卡面缩略图提前拉下来，避免玩家开局时等待。
"""

from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_on_games")

from .http_client import HttpClient

DEFAULT_ASSET_BASE = "https://assets.bdon.moe"
DEFAULT_ASSET_LANGUAGE = "zh-Hans"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 40 * 1024 * 1024


class AssetService:
    def __init__(self, data_dir: Path, config, http: Optional[HttpClient] = None):
        self.data_dir = Path(data_dir)
        self.cache_dir = self.data_dir / "assets"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.http = http or HttpClient(
            timeout=int(config.get("request_timeout", 60) or 60),
            concurrency=int(config.get("download_concurrency", 4) or 4),
            retries=int(config.get("download_retries", 3) or 3),
        )
        self._owns_http = http is None
        self._locks: dict[str, asyncio.Lock] = {}
        self._prefetch_task: Optional[asyncio.Task] = None
        self._stopping = False

    # ---------- URL / 路径 ----------

    @property
    def base_url(self) -> str:
        value = str(self.config.get("asset_base_url", DEFAULT_ASSET_BASE) or DEFAULT_ASSET_BASE)
        return value.strip().rstrip("/")

    @property
    def language(self) -> str:
        value = str(
            self.config.get("asset_language", DEFAULT_ASSET_LANGUAGE)
            or DEFAULT_ASSET_LANGUAGE
        )
        return value.strip().strip("/")

    def get_url(self, key: str) -> str:
        key = str(key or "").strip().lstrip("/")
        return f"{self.base_url}/{self.language}/{key}"

    def cache_path(self, key: str) -> Path:
        parts = [p for p in str(key or "").replace("\\", "/").split("/") if p]
        # 防御路径穿越
        safe_parts = [p for p in parts if p not in (".", "..")]
        return self.cache_dir.joinpath(*safe_parts)

    def is_cached(self, key: str) -> bool:
        path = self.cache_path(key)
        try:
            return path.exists() and path.stat().st_size > 0
        except OSError:
            return False

    async def start(self) -> None:
        if self._owns_http:
            await self.http.start()

    async def close(self) -> None:
        self._stopping = True
        if self._prefetch_task:
            self._prefetch_task.cancel()
            try:
                await self._prefetch_task
            except (asyncio.CancelledError, Exception):
                pass
            self._prefetch_task = None
        if self._owns_http:
            await self.http.close()

    # ---------- 下载 ----------

    async def fetch_bytes(
        self,
        key: str,
        *,
        max_bytes: int = MAX_IMAGE_BYTES,
        timeout: Optional[int] = None,
    ) -> Optional[bytes]:
        path = self.cache_path(key)
        if path.exists():
            try:
                if path.stat().st_size > 0:
                    return path.read_bytes()
            except OSError:
                pass

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # 等锁期间可能已被其它协程下载好
            if path.exists():
                try:
                    if path.stat().st_size > 0:
                        return path.read_bytes()
                except OSError:
                    pass

            url = self.get_url(key)
            data = await self.http.get_bytes(
                url,
                retries=int(self.config.get("download_retries", 3) or 3),
                timeout=timeout or int(self.config.get("request_timeout", 60) or 60),
                max_bytes=max_bytes,
            )
            if not data:
                return None

            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)
            except OSError as exc:
                logger.warning(f"[OurNotes] 写入资源缓存失败 {path}: {exc}")
            return data

    async def fetch_image(self, key: str, timeout: int = 30) -> Optional[Image.Image]:
        data = await self.fetch_bytes(
            key, max_bytes=MAX_IMAGE_BYTES, timeout=timeout
        )
        if not data:
            return None
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
            return image
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OurNotes] 图片解析失败 {key}: {exc}")
            # 损坏的缓存删除，下次重新下载
            try:
                self.cache_path(key).unlink(missing_ok=True)
            except OSError:
                pass
            return None

    async def fetch_audio_path(self, key: str, timeout: int = 120) -> Optional[Path]:
        data = await self.fetch_bytes(
            key, max_bytes=MAX_AUDIO_BYTES, timeout=timeout
        )
        if not data:
            return None
        path = self.cache_path(key)
        return path if path.exists() else None

    # ---------- 预热 ----------

    def start_prefetch(self, keys: Iterable[str]) -> None:
        if self._prefetch_task and not self._prefetch_task.done():
            return
        self._prefetch_task = asyncio.create_task(self._prefetch(list(dict.fromkeys(keys))))

    async def _prefetch(self, keys: list[str]) -> None:
        if not keys:
            return
        concurrency = max(1, int(self.config.get("prefetch_concurrency", 3) or 3))
        semaphore = asyncio.Semaphore(concurrency)
        done = 0
        failed = 0
        total = len(keys)

        async def one(key: str) -> None:
            nonlocal done, failed
            if self._stopping:
                return
            if self.is_cached(key):
                done += 1
                return
            async with semaphore:
                data = await self.fetch_bytes(key)
                if data:
                    done += 1
                else:
                    failed += 1
                if (done + failed) % 25 == 0:
                    logger.info(
                        f"[OurNotes] 资源预热进度 {done + failed}/{total} "
                        f"(成功 {done}, 失败 {failed})"
                    )

        try:
            await asyncio.gather(*(one(key) for key in keys))
            logger.info(
                f"[OurNotes] 资源预热完成：成功 {done}/{total}，失败 {failed}。"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OurNotes] 资源预热异常: {exc}")

    # ---------- 缓存清理 ----------

    def cleanup_cache(self, max_mb: Optional[int] = None) -> int:
        """按 mtime 从旧到新删除缓存，直到低于上限的 80%。返回删除文件数。"""
        if max_mb is None:
            max_mb = int(self.config.get("asset_cache_max_mb", 1024) or 1024)
        if max_mb <= 0:
            return 0

        files: list[tuple[float, int, Path]] = []
        total = 0
        try:
            for path in self.cache_dir.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size
        except OSError:
            return 0

        limit = max_mb * 1024 * 1024
        if total <= limit:
            return 0

        target = int(limit * 0.8)
        files.sort(key=lambda item: item[0])
        removed = 0
        for _, size, path in files:
            if total <= target:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        if removed:
            logger.info(
                f"[OurNotes] 资源缓存清理完成，删除 {removed} 个文件，"
                f"当前约 {total / 1024 / 1024:.1f} MB。"
            )
        return removed

    def cleanup_output(self, output_dir: Path, max_age_seconds: int = 3600) -> None:
        if not output_dir.exists():
            return
        now = time.time()
        try:
            for path in output_dir.iterdir():
                if path.is_file() and now - path.stat().st_mtime > max_age_seconds:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        except OSError:
            pass
