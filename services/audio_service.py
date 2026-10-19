"""猜歌音频服务：下载整曲、缓存、随机截取、倍速与倒放。

优先使用 ffmpeg（PATH 中的 ffmpeg，或 imageio-ffmpeg 自带的二进制）；
ffmpeg 不可用时回退 pydub。音频源为 MoeNotes 资源站的 m4a。
"""

from __future__ import annotations

import asyncio
import random
import re
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_on_games")

try:
    from pydub import AudioSegment  # type: ignore

    PYDUB_AVAILABLE = True
except Exception:  # noqa: BLE001
    AudioSegment = None  # type: ignore
    PYDUB_AVAILABLE = False

from .asset_service import AssetService


def _find_ffmpeg() -> Optional[str]:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def _find_ffprobe() -> Optional[str]:
    return shutil.which("ffprobe")


class AudioService:
    def __init__(self, asset_service: AssetService, config, output_dir: Path):
        self.assets = asset_service
        self.config = config
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.ffmpeg = _find_ffmpeg()
        self.ffprobe = _find_ffprobe()
        self._duration_cache: dict[str, float] = {}
        if PYDUB_AVAILABLE and AudioSegment is not None and self.ffmpeg:
            # 让 pydub 回退路径也能使用 imageio-ffmpeg 自带的二进制。
            try:
                AudioSegment.converter = self.ffmpeg
                if self.ffprobe:
                    AudioSegment.ffprobe = self.ffprobe
            except Exception:
                pass
        if not self.ffmpeg:
            logger.error(
                "[OurNotes] 未找到 ffmpeg。猜歌音频将尝试使用 pydub，"
                "请安装 ffmpeg 或 pip install imageio-ffmpeg。"
            )

    # ---------- 音频时长 ----------

    def _probe_duration_sync(self, path: Path) -> float:
        key = str(path)
        cached = self._duration_cache.get(key)
        if cached:
            return cached

        duration = 0.0
        if self.ffprobe:
            try:
                result = subprocess.run(
                    [
                        self.ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0 and result.stdout.strip():
                    duration = float(result.stdout.strip())
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[OurNotes] ffprobe 失败: {exc}")

        if duration <= 0 and self.ffmpeg:
            try:
                result = subprocess.run(
                    [self.ffmpeg, "-hide_banner", "-i", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                match = re.search(
                    r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
                    result.stderr or "",
                )
                if match:
                    hours, minutes, seconds = match.groups()
                    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[OurNotes] ffmpeg 探测时长失败: {exc}")

        if duration <= 0 and PYDUB_AVAILABLE and AudioSegment is not None:
            try:
                duration = float(len(AudioSegment.from_file(str(path))) / 1000.0)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[OurNotes] pydub 探测时长失败: {exc}")

        if duration > 0:
            self._duration_cache[key] = duration
        return duration

    # ---------- 截取 ----------

    def _run_ffmpeg_sync(self, args: list[str], timeout: int = 120) -> bool:
        if not self.ffmpeg:
            return False
        try:
            result = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                logger.warning(
                    f"[OurNotes] ffmpeg 返回 {result.returncode}: "
                    f"{(result.stderr or '').strip()[:500]}"
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OurNotes] ffmpeg 执行失败: {exc}")
            return False

    def _pydub_clip_sync(
        self,
        audio_path: Path,
        start_seconds: float,
        source_duration: float,
        speed: float,
        reverse: bool,
        output_path: Path,
    ) -> bool:
        if not PYDUB_AVAILABLE or AudioSegment is None:
            return False
        try:
            audio = AudioSegment.from_file(str(audio_path))
            start_ms = max(0, int(start_seconds * 1000))
            end_ms = min(len(audio), start_ms + int(source_duration * 1000))
            clip = audio[start_ms:end_ms]
            if speed != 1.0:
                clip = clip._spawn(
                    clip.raw_data,
                    overrides={"frame_rate": int(clip.frame_rate * speed)},
                )
            if reverse:
                clip = clip.reverse()
            clip.export(str(output_path), format="mp3", bitrate="128k")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OurNotes] pydub 截取失败: {exc}")
            return False

    async def get_song_clip(
        self,
        song: dict,
        *,
        speed: float = 1.0,
        reverse: bool = False,
    ) -> Optional[str]:
        audio_key = song.get("audio_key")
        if not audio_key:
            return None
        audio_path = await self.assets.fetch_audio_path(audio_key)
        if not audio_path:
            logger.warning(f"[OurNotes] 音频下载失败: {audio_key}")
            return None

        loop = asyncio.get_running_loop()
        total_duration = await loop.run_in_executor(
            self.executor, self._probe_duration_sync, audio_path
        )
        if total_duration <= 0:
            total_duration = 180.0

        clip_duration = max(3.0, float(self.config.get("clip_duration_seconds", 10) or 10))
        speed = float(speed or 1.0)
        source_duration = clip_duration * max(1.0, speed)
        if source_duration > total_duration:
            source_duration = total_duration
            clip_duration = source_duration / max(1.0, speed)

        ratio_min = float(self.config.get("clip_start_ratio_min", 0.08) or 0.08)
        ratio_max = float(self.config.get("clip_start_ratio_max", 0.72) or 0.72)
        ratio_min = max(0.0, min(ratio_min, 0.95))
        ratio_max = max(ratio_min, min(ratio_max, 0.98))
        start_seconds = total_duration * random.uniform(ratio_min, ratio_max)
        max_start = max(0.0, total_duration - source_duration - 1.0)
        start_seconds = min(start_seconds, max_start)

        output_path = self.output_dir / f"clip_{uuid.uuid4().hex}.mp3"

        filters: list[str] = []
        if speed != 1.0:
            filters.append(f"atempo={speed:.3f}")
        if reverse:
            filters.append("areverse")

        args: list[str] = [
            "-ss",
            f"{start_seconds:.3f}",
            "-t",
            f"{source_duration:.3f}",
            "-i",
            str(audio_path),
            "-vn",
        ]
        if filters:
            args += ["-filter:a", ",".join(filters)]
        args += ["-c:a", "libmp3lame", "-b:a", "128k", str(output_path)]

        ok = await loop.run_in_executor(self.executor, self._run_ffmpeg_sync, args)
        if not ok:
            ok = await loop.run_in_executor(
                self.executor,
                self._pydub_clip_sync,
                audio_path,
                start_seconds,
                source_duration,
                speed,
                reverse,
                output_path,
            )
        if not ok or not output_path.exists() or output_path.stat().st_size == 0:
            logger.error(f"[OurNotes] 生成音频片段失败: {song.get('title')}")
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        return str(output_path)

    # ---------- 随机模式 ----------

    def get_random_mode(self) -> tuple[float, bool, int, str]:
        """返回 (倍速, 倒放, 分数, 展示文本)。"""
        decay = float(self.config.get("random_mode_decay_factor", 0.75) or 0.75)
        options = [
            (1.0, False, 1, "普通"),
            (2.0, False, 2, "2倍速"),
            (1.0, True, 2, "倒放"),
            (2.0, True, 3, "2倍速+倒放"),
        ]
        weights = [max(0.01, decay ** (score - 1)) for _, _, score, _ in options]
        choice = random.choices(options, weights=weights, k=1)[0]
        speed, reverse, score, display = choice
        return speed, reverse, score, display

    def cleanup_output(self, max_age_seconds: int = 3600) -> None:
        import time

        now = time.time()
        try:
            for path in self.output_dir.iterdir():
                if (
                    path.is_file()
                    and path.name.startswith("clip_")
                    and now - path.stat().st_mtime > max_age_seconds
                ):
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    async def terminate(self) -> None:
        self.executor.shutdown(wait=False)
