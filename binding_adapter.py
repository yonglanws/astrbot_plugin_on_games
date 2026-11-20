"""绑定适配器：把本插件的官机绑定能力暴露给中央 QQ 官方绑定插件。

中央插件通过 ``star_cls.binding_adapter`` duck-typed 调用本文件中的
``BindingAdapter``，避免两个插件互相 import。本模块只依赖标准库与
AstrBot logger，不导入插件 ``main``（main 会导入本模块，避免循环）。

协议版本：``BINDING_ADAPTER_VERSION = 1``。
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sqlite3
from pathlib import Path
from typing import Any, ClassVar

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 独立测试环境
    import logging

    logger = logging.getLogger("astrbot_plugin_on_games")

BINDING_ADAPTER_VERSION = 1
LOCAL_PLATFORM = "qq_official"
LOCAL_LIMIT = 2
BINDING_TABLE = "account_bindings"
BINDING_COLUMNS = (
    "official_platform",
    "official_user_id",
    "qq_user_id",
    "bound_at",
)
QQ_RE = re.compile(r"\d{5,12}")


def _result(
    status: str,
    message: str = "",
    *,
    rows_affected: int = 0,
    requires_reload: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造适配器统一结果，非法状态按 failed 处理。"""

    if status not in {"ok", "skipped", "conflict", "failed", "unsupported"}:
        status = "failed"
    try:
        rows = max(0, int(rows_affected))
    except (TypeError, ValueError, OverflowError):
        rows = 0
    return {
        "status": status,
        "message": str(message or ""),
        "rows_affected": rows,
        "requires_reload": bool(requires_reload),
        "details": details if isinstance(details, dict) else {},
    }


class BindingAdapter:
    """on_games（OurNotes）绑定适配器。"""

    plugin_name = "astrbot_plugin_on_games"
    display_name = "OurNotes娱乐插件"
    version = BINDING_ADAPTER_VERSION
    capabilities: ClassVar[set[str]] = {
        "bind",
        "unbind",
        "export",
        "has_binding",
        "preflight",
        "health",
    }

    def __init__(self, plugin):
        # 构造阶段不做任何 I/O：on_games 的数据库由后台任务初始化。
        self.plugin = plugin

    # ---------- 内部：数据库访问 ----------

    def _db_path(self) -> str:
        db = getattr(self.plugin, "db", None)
        path = getattr(db, "db_path", None)
        if path:
            return str(path)
        data_dir = getattr(self.plugin, "data_dir", None)
        if data_dir:
            return str(Path(data_dir) / "on_games.db")
        return ""

    @staticmethod
    def _connect_readonly(db_path: str) -> sqlite3.Connection:
        uri = Path(db_path).resolve().as_uri()
        separator = "&" if "?" in uri else "?"
        return sqlite3.connect(
            f"{uri}{separator}mode=ro", uri=True, timeout=2.0
        )

    def _inspect_db(self, db_path: str) -> dict[str, Any]:
        """只读检查数据库；任何异常都收敛为 ready=False，不抛出。"""

        info: dict[str, Any] = {
            "db_path": db_path,
            "ready": False,
            "tables": {},
            "binding_rows": 0,
            "quick_check": "",
            "message": "",
        }
        if not db_path or not Path(db_path).exists():
            info["message"] = "数据库文件不存在"
            return info
        try:
            conn = self._connect_readonly(db_path)
        except Exception as exc:  # noqa: BLE001 - health 不允许抛出
            info["message"] = f"数据库无法打开: {type(exc).__name__}"
            return info
        try:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            info["tables"] = {BINDING_TABLE: BINDING_TABLE in tables}
            if BINDING_TABLE not in tables:
                info["message"] = f"缺少绑定表 {BINDING_TABLE}"
                return info
            columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({BINDING_TABLE})")
            }
            missing = [name for name in BINDING_COLUMNS if name not in columns]
            if missing:
                info["message"] = "绑定表缺少字段: " + ", ".join(missing)
                return info
            check = conn.execute("PRAGMA quick_check").fetchone()
            info["quick_check"] = str(check[0]) if check else ""
            if info["quick_check"] != "ok":
                info["message"] = f"quick_check 未通过: {info['quick_check']}"
                return info
            row = conn.execute(
                f"SELECT COUNT(*) FROM {BINDING_TABLE}"
            ).fetchone()
            info["binding_rows"] = int(row[0] or 0) if row else 0
            info["ready"] = True
            return info
        except Exception as exc:  # noqa: BLE001 - health 不允许抛出
            info["message"] = f"数据库检查失败: {type(exc).__name__}"
            return info
        finally:
            conn.close()

    def _resolve_sync(self, openid: str) -> str:
        db = getattr(self.plugin, "db", None)
        resolve = getattr(db, "resolve_user_id", None)
        if not callable(resolve):
            raise TypeError("本地 resolve_user_id 不可调用")
        return str(resolve(LOCAL_PLATFORM, openid))

    def _is_bound_sync(self, openid: str) -> bool:
        db = getattr(self.plugin, "db", None)
        is_bound = getattr(db, "is_official_account_bound", None)
        if callable(is_bound):
            return bool(is_bound(LOCAL_PLATFORM, openid))
        return self._resolve_sync(openid) != openid

    def _count_sync(self, qq: str) -> int:
        db = getattr(self.plugin, "db", None)
        count = getattr(db, "count_qq_bindings", None)
        if not callable(count):
            raise TypeError("本地 count_qq_bindings 不可调用")
        return int(count(qq) or 0)

    def _local_bind_fn(self):
        db = getattr(self.plugin, "db", None)
        fn = getattr(db, "bind_official_account", None)
        if not callable(fn):
            raise TypeError("本地 bind_official_account 不可调用")
        return fn

    def _local_unbind_fn(self):
        db = getattr(self.plugin, "db", None)
        fn = getattr(db, "unbind_official_account", None)
        if not callable(fn):
            raise TypeError("本地 unbind_official_account 不可调用")
        return fn

    @staticmethod
    def _supports_force(fn) -> bool:
        try:
            return "force" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False

    def _call_local_bind(self, openid: str, qq: str, force: bool) -> bool:
        fn = self._local_bind_fn()
        if self._supports_force(fn):
            return bool(
                fn(LOCAL_PLATFORM, openid, qq, force=bool(force))
            )
        return bool(fn(LOCAL_PLATFORM, openid, qq))

    def _call_local_unbind(self, openid: str) -> bool:
        return bool(self._local_unbind_fn()(LOCAL_PLATFORM, openid))

    def _export_sync(self) -> list[dict[str, Any]]:
        db_path = self._db_path()
        if not db_path or not Path(db_path).exists():
            return []
        conn = self._connect_readonly(db_path)
        try:
            try:
                rows = conn.execute(
                    f"SELECT official_platform, official_user_id, qq_user_id, "
                    f"bound_at FROM {BINDING_TABLE} "
                    "WHERE official_platform = ? "
                    "ORDER BY bound_at, official_user_id",
                    (LOCAL_PLATFORM,),
                ).fetchall()
            except sqlite3.Error:
                return []
        finally:
            conn.close()
        return [
            {
                "source_plugin": self.plugin_name,
                "platform_type": LOCAL_PLATFORM,
                "openid": str(row[1] or ""),
                "qq": str(row[2] or ""),
                "bound_at": str(row[3] or ""),
            }
            for row in rows
        ]

    # ---------- 适配器协议 ----------

    async def has_binding(self, platform_type: str, openid: str) -> bool:
        """platform_type 统一映射为本地 qq_official；异常返回 False。"""

        try:
            openid = str(openid or "").strip()
            if not openid:
                return False
            return await asyncio.to_thread(self._is_bound_sync, openid)
        except Exception as exc:  # noqa: BLE001 - 协议要求不抛出
            logger.warning(
                "[OurNotes] 检查官机绑定失败: %s", type(exc).__name__
            )
            return False

    async def preflight(self, platform_id: str, openid: str, qq: str) -> dict:
        try:
            openid = str(openid or "").strip()
            qq = str(qq or "").strip()
            db_path = self._db_path()
            details: dict[str, Any] = {
                "db_ready": False,
                "bound_qq": None,
                "target_count": 0,
                "would_hit_local_limit": False,
                "has_binding": False,
            }
            if not openid:
                return _result(
                    "failed",
                    "缺少官方账号标识，无法检查绑定状态",
                    details=details,
                )
            info = await asyncio.to_thread(self._inspect_db, db_path)
            if not info.get("ready"):
                details["db_path"] = str(info.get("db_path") or "")
                return _result(
                    "failed",
                    str(info.get("message") or "本地数据库不可用"),
                    details=details,
                )
            details["db_ready"] = True
            bound_qq = await asyncio.to_thread(self._resolve_sync, openid)
            has_binding = bound_qq != openid
            target_count = (
                await asyncio.to_thread(self._count_sync, qq) if qq else 0
            )
            details.update(
                {
                    "bound_qq": bound_qq if has_binding else None,
                    "target_count": target_count,
                    "would_hit_local_limit": target_count >= LOCAL_LIMIT,
                    "has_binding": has_binding,
                }
            )
            if has_binding and bound_qq != qq:
                return _result(
                    "conflict",
                    f"当前官方机器人账号已经绑定至 QQ号 {bound_qq}。",
                    details=details,
                )
            return _result("ok", "", details=details)
        except Exception as exc:  # noqa: BLE001 - 协议要求不抛出
            logger.warning(
                "[OurNotes] 绑定预检失败: %s", type(exc).__name__
            )
            return _result(
                "failed",
                "绑定预检失败：本地数据库异常。",
                details={"db_ready": False},
            )

    async def bind(
        self,
        platform_id: str,
        openid: str,
        qq: str,
        *,
        force: bool = True,
    ) -> dict:
        try:
            openid = str(openid or "").strip()
            qq = str(qq or "").strip()
            force = bool(force)
            if not openid:
                return _result("failed", "缺少官方账号标识，无法绑定")
            if not QQ_RE.fullmatch(qq):
                return _result(
                    "failed", "QQ 号格式不正确，应为 5-12 位数字。"
                )
            if qq == openid:
                return _result(
                    "failed", "目标 QQ 号不能与官方账号标识相同。"
                )
            info = await asyncio.to_thread(self._inspect_db, self._db_path())
            if not info.get("ready"):
                return _result(
                    "failed",
                    str(info.get("message") or "本地数据库不可用"),
                    details={"db_ready": False},
                )
            bound_qq = await asyncio.to_thread(self._resolve_sync, openid)
            if bound_qq == qq:
                return _result(
                    "skipped",
                    f"当前官方机器人账号已绑定至 QQ号 {qq}。",
                    details={"db_ready": True, "bound_qq": bound_qq},
                )
            if bound_qq != openid:
                return _result(
                    "conflict",
                    f"当前官方机器人账号已经绑定至 QQ号 {bound_qq}。",
                    details={"db_ready": True, "bound_qq": bound_qq},
                )
            count = await asyncio.to_thread(self._count_sync, qq)
            bound = await asyncio.to_thread(
                self._call_local_bind, openid, qq, force
            )
            if bound:
                return _result(
                    "ok",
                    "绑定成功。",
                    rows_affected=1,
                    details={"db_ready": True, "bound_qq": qq},
                )
            after = await asyncio.to_thread(self._resolve_sync, openid)
            if after == qq:
                return _result(
                    "skipped",
                    f"当前官方机器人账号已绑定至 QQ号 {qq}。",
                    details={"db_ready": True, "bound_qq": after},
                )
            if after != openid:
                return _result(
                    "conflict",
                    f"当前官方机器人账号已经绑定至 QQ号 {after}。",
                    details={"db_ready": True, "bound_qq": after},
                )
            if count >= LOCAL_LIMIT and not force:
                return _result(
                    "conflict",
                    "绑定失败：该QQ号已绑定两个官机账号，无法继续绑定。",
                    details={
                        "db_ready": True,
                        "target_count": count,
                        "would_hit_local_limit": True,
                    },
                )
            return _result(
                "failed",
                "绑定失败，请稍后重试。",
                details={"db_ready": True, "target_count": count},
            )
        except Exception as exc:  # noqa: BLE001 - 协议要求不抛出
            logger.error(
                "[OurNotes] 适配器绑定失败: %s", type(exc).__name__
            )
            return _result(
                "failed",
                "绑定失败：本地数据库异常。",
                details={"db_ready": False},
            )

    async def unbind(self, platform_id: str, openid: str) -> dict:
        try:
            openid = str(openid or "").strip()
            if not openid:
                return _result("failed", "缺少官方账号标识，无法解绑")
            info = await asyncio.to_thread(self._inspect_db, self._db_path())
            if not info.get("ready"):
                return _result(
                    "failed",
                    str(info.get("message") or "本地数据库不可用"),
                    details={"db_ready": False},
                )
            if not await self.has_binding(LOCAL_PLATFORM, openid):
                return _result(
                    "skipped",
                    "当前官方机器人账号尚未绑定QQ号。",
                    details={"binding_removed": False},
                )
            removed = await asyncio.to_thread(
                self._call_local_unbind, openid
            )
            if removed:
                return _result(
                    "ok",
                    "解绑成功。",
                    rows_affected=1,
                    details={
                        "binding_removed": True,
                        "stats_restored": False,
                    },
                )
            return _result("failed", "解绑失败，请稍后重试。")
        except Exception as exc:  # noqa: BLE001 - 协议要求不抛出
            logger.error(
                "[OurNotes] 适配器解绑失败: %s", type(exc).__name__
            )
            return _result("failed", "解绑失败：本地数据库异常。")

    async def export_bindings(self) -> list[dict]:
        try:
            return await asyncio.to_thread(self._export_sync)
        except Exception as exc:  # noqa: BLE001 - 只读导出不允许抛出
            logger.warning(
                "[OurNotes] 导出旧绑定失败: %s", type(exc).__name__
            )
            return []

    async def health(self) -> dict:
        db_path = ""
        try:
            db_path = self._db_path()
            info = await asyncio.to_thread(self._inspect_db, db_path)
        except Exception as exc:  # noqa: BLE001 - health 不允许抛出
            return {
                "status": "failed",
                "plugin_name": self.plugin_name,
                "db_path": db_path,
                "tables": {},
                "binding_rows": 0,
                "ready": False,
                "message": f"health 检查失败: {type(exc).__name__}",
            }
        ready = bool(info.get("ready"))
        return {
            "status": "ok" if ready else "failed",
            "plugin_name": self.plugin_name,
            "db_path": str(info.get("db_path") or ""),
            "tables": dict(info.get("tables") or {}),
            "binding_rows": int(info.get("binding_rows") or 0),
            "quick_check": str(info.get("quick_check") or ""),
            "ready": ready,
            "message": str(info.get("message") or ""),
        }
