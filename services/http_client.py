"""共享异步 HTTP 客户端：连接复用、超时、重试与多镜像回退。

这个游戏的两类网络资源特征不同：
- master 数据托管在 GitHub，国内直连 raw.githubusercontent.com 往往很慢，
  因此优先使用 jsDelivr CDN，再回退 GitHub raw / 镜像。
- 卡面、曲绘、音频托管在 Cloudflare（assets.bdon.moe）。首次请求需要回源，
  之后 CF 边缘节点会缓存；插件侧再做磁盘缓存，保证同一资源只下载一次。

所有下载都使用同一个 aiohttp 连接池（keep-alive），避免每张图重新建连。
"""

from __future__ import annotations

import asyncio
import random
from typing import Iterable, Optional

import aiohttp

try:  # AstrBot 环境
    from astrbot.api import logger
except ImportError:  # 独立运行 / 单元测试
    import logging

    logger = logging.getLogger("astrbot_plugin_on_games")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 这些状态码值得重试；404/403 通常代表资源不存在或没有权限，重试没有意义。
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpClient:
    """带重试和镜像回退的轻量 HTTP 客户端。"""

    def __init__(
        self,
        *,
        timeout: int = 60,
        concurrency: int = 4,
        retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
    ):
        self.timeout = max(5, int(timeout))
        self.concurrency = max(1, int(concurrency))
        self.retries = max(1, int(retries))
        self.user_agent = user_agent
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    async def start(self) -> None:
        await self._get_session()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(
                    limit=max(8, self.concurrency * 3),
                    limit_per_host=max(4, self.concurrency * 2),
                    ttl_dns_cache=300,
                    enable_cleanup_closed=True,
                    force_close=False,
                )
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(
                        total=self.timeout,
                        connect=min(20, self.timeout),
                        sock_read=self.timeout,
                    ),
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "*/*",
                        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
                    },
                )
        return self._session

    async def get_bytes(
        self,
        url: str,
        *,
        retries: Optional[int] = None,
        timeout: Optional[int] = None,
        headers: Optional[dict] = None,
        max_bytes: Optional[int] = None,
    ) -> Optional[bytes]:
        """下载 URL 的完整内容。404 返回 None；其它失败重试后返回 None。"""
        attempts = max(1, int(retries if retries is not None else self.retries))
        total_timeout = max(5, int(timeout if timeout is not None else self.timeout))
        session = await self._get_session()

        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(
                        total=total_timeout,
                        connect=min(20, total_timeout),
                        sock_read=total_timeout,
                    ),
                ) as response:
                    if response.status == 404:
                        return None
                    if response.status != 200:
                        if response.status in _RETRYABLE_STATUS and attempt < attempts:
                            last_error = RuntimeError(f"HTTP {response.status}")
                        else:
                            logger.warning(
                                f"[OurNotes] 下载失败 HTTP {response.status}: {url}"
                            )
                            return None
                    else:
                        if max_bytes is not None:
                            length = response.headers.get("Content-Length")
                            if length and int(length) > max_bytes:
                                logger.warning(
                                    f"[OurNotes] 资源超过大小限制 {max_bytes}: {url}"
                                )
                                return None
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            size += len(chunk)
                            if max_bytes is not None and size > max_bytes:
                                logger.warning(
                                    f"[OurNotes] 资源超过大小限制 {max_bytes}: {url}"
                                )
                                return None
                            chunks.append(chunk)
                        return b"".join(chunks)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络异常种类很多
                last_error = exc

            if attempt < attempts:
                delay = min(8.0, 0.6 * (2 ** (attempt - 1))) + random.uniform(0, 0.4)
                logger.debug(
                    f"[OurNotes] 下载重试 {attempt}/{attempts} ({last_error}): {url}"
                )
                await asyncio.sleep(delay)

        if last_error is not None:
            logger.warning(f"[OurNotes] 下载最终失败: {url}: {last_error}")
        return None

    async def get_first(
        self,
        urls: Iterable[str],
        *,
        retries: int = 1,
        timeout: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> Optional[bytes]:
        """按顺序尝试多个镜像，返回第一个成功的内容。"""
        for url in urls:
            data = await self.get_bytes(
                url,
                retries=retries,
                timeout=timeout,
                max_bytes=max_bytes,
            )
            if data:
                return data
        return None

    async def get_json(
        self,
        url: str,
        *,
        retries: Optional[int] = None,
        timeout: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ):
        import json

        data = await self.get_bytes(
            url,
            retries=retries,
            timeout=timeout,
            max_bytes=max_bytes,
        )
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[OurNotes] JSON 解析失败: {url}: {exc}")
            return None
