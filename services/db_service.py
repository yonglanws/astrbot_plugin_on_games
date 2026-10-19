"""统一 SQLite 数据层：三个游戏的分数、每日次数、自定义名称与官机绑定。

表结构：
- user_stats(user_id, platform_name, game, ...)  每个游戏独立统计
- user_profiles(user_id, platform_name, ...)     显示名 / 自定义名
- daily_plays(user_id, platform_name, game, date, plays)
- account_bindings(...)                          QQ 官机 -> 普通 QQ 绑定

绑定官机时会把该官机在三个游戏里的全部历史分数迁移到 QQ 号上。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date
from typing import Optional

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_on_games")

GAMES = ("song", "card", "jacket")
GAME_LABELS = {
    "song": "猜歌",
    "card": "猜卡面",
    "jacket": "猜曲绘",
}

DEFAULT_PLATFORM_NAME = "aiocqhttp"
OFFICIAL_PLATFORM_NAME = "qq_official"
MAX_BINDINGS_PER_QQ = 2


def _today() -> str:
    return date.today().isoformat()


class DatabaseService:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    # ---------- 基础 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _normalize_platform(platform_name: Optional[str]) -> str:
        return str(platform_name or DEFAULT_PLATFORM_NAME).strip().lower()

    def init_db(self) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id TEXT NOT NULL,
                    platform_name TEXT NOT NULL,
                    game TEXT NOT NULL,
                    user_name TEXT,
                    score INTEGER DEFAULT 0,
                    attempts INTEGER DEFAULT 0,
                    correct_attempts INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, platform_name, game)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT NOT NULL,
                    platform_name TEXT NOT NULL,
                    user_name TEXT,
                    custom_name TEXT,
                    PRIMARY KEY (user_id, platform_name)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_plays (
                    user_id TEXT NOT NULL,
                    platform_name TEXT NOT NULL,
                    game TEXT NOT NULL,
                    play_date TEXT NOT NULL,
                    plays INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, platform_name, game, play_date)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS account_bindings (
                    official_platform TEXT NOT NULL,
                    official_user_id TEXT NOT NULL,
                    qq_user_id TEXT NOT NULL,
                    bound_at TEXT NOT NULL,
                    PRIMARY KEY (official_platform, official_user_id)
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_stats_score "
                "ON user_stats(game, score DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_daily_date "
                "ON daily_plays(play_date)"
            )
            conn.commit()
        finally:
            conn.close()

    # ---------- 绑定 ----------

    def resolve_user_id(self, platform_name: str, user_id: str) -> str:
        platform = self._normalize_platform(platform_name)
        if platform != OFFICIAL_PLATFORM_NAME:
            return str(user_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT qq_user_id FROM account_bindings "
                "WHERE official_platform = ? AND official_user_id = ?",
                (platform, str(user_id)),
            ).fetchone()
            return str(row[0]) if row else str(user_id)
        finally:
            conn.close()

    def is_official_account_bound(self, platform_name: str, user_id: str) -> bool:
        platform = self._normalize_platform(platform_name)
        if platform != OFFICIAL_PLATFORM_NAME:
            return False
        conn = self._connect()
        try:
            return (
                conn.execute(
                    "SELECT 1 FROM account_bindings "
                    "WHERE official_platform = ? AND official_user_id = ?",
                    (platform, str(user_id)),
                ).fetchone()
                is not None
            )
        finally:
            conn.close()

    def count_qq_bindings(self, qq_user_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM account_bindings "
                "WHERE official_platform = ? AND qq_user_id = ?",
                (OFFICIAL_PLATFORM_NAME, str(qq_user_id).strip()),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def bind_official_account(
        self,
        official_platform: str,
        official_user_id: str,
        qq_user_id: str,
    ) -> bool:
        import re

        platform = self._normalize_platform(official_platform)
        source_id = str(official_user_id).strip()
        target_id = str(qq_user_id).strip()

        if (
            platform != OFFICIAL_PLATFORM_NAME
            or not source_id
            or not re.fullmatch(r"\d{5,12}", target_id)
            or source_id == target_id
        ):
            return False

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()

            if cursor.execute(
                "SELECT 1 FROM account_bindings "
                "WHERE official_platform = ? AND official_user_id = ?",
                (platform, source_id),
            ).fetchone():
                conn.rollback()
                return False

            bound_count = cursor.execute(
                "SELECT COUNT(*) FROM account_bindings "
                "WHERE official_platform = ? AND qq_user_id = ?",
                (platform, target_id),
            ).fetchone()
            if int(bound_count[0] or 0) >= MAX_BINDINGS_PER_QQ:
                conn.rollback()
                return False

            # 1) 合并三个游戏的统计
            for game in GAMES:
                source = cursor.execute(
                    "SELECT user_name, score, attempts, correct_attempts "
                    "FROM user_stats WHERE user_id = ? AND platform_name = ? AND game = ?",
                    (source_id, platform, game),
                ).fetchone()
                target = cursor.execute(
                    "SELECT user_name, score, attempts, correct_attempts "
                    "FROM user_stats WHERE user_id = ? AND platform_name = ? AND game = ?",
                    (target_id, DEFAULT_PLATFORM_NAME, game),
                ).fetchone()

                if source and target:
                    cursor.execute(
                        "UPDATE user_stats SET "
                        "user_name = COALESCE(NULLIF(user_name, ''), ?), "
                        "score = score + ?, attempts = attempts + ?, "
                        "correct_attempts = correct_attempts + ? "
                        "WHERE user_id = ? AND platform_name = ? AND game = ?",
                        (
                            source[0],
                            int(source[1] or 0),
                            int(source[2] or 0),
                            int(source[3] or 0),
                            target_id,
                            DEFAULT_PLATFORM_NAME,
                            game,
                        ),
                    )
                elif source:
                    cursor.execute(
                        "INSERT INTO user_stats "
                        "(user_id, platform_name, game, user_name, score, attempts, correct_attempts) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            target_id,
                            DEFAULT_PLATFORM_NAME,
                            game,
                            source[0],
                            int(source[1] or 0),
                            int(source[2] or 0),
                            int(source[3] or 0),
                        ),
                    )

                cursor.execute(
                    "DELETE FROM user_stats "
                    "WHERE user_id = ? AND platform_name = ? AND game = ?",
                    (source_id, platform, game),
                )

            # 2) 合并每日次数（按日期累加）
            daily_rows = cursor.execute(
                "SELECT game, play_date, plays FROM daily_plays "
                "WHERE user_id = ? AND platform_name = ?",
                (source_id, platform),
            ).fetchall()
            for game, play_date, plays in daily_rows:
                cursor.execute(
                    "INSERT INTO daily_plays "
                    "(user_id, platform_name, game, play_date, plays) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id, platform_name, game, play_date) DO UPDATE SET "
                    "plays = plays + excluded.plays",
                    (
                        target_id,
                        DEFAULT_PLATFORM_NAME,
                        game,
                        play_date,
                        int(plays or 0),
                    ),
                )
            cursor.execute(
                "DELETE FROM daily_plays WHERE user_id = ? AND platform_name = ?",
                (source_id, platform),
            )

            # 3) 合并显示名 / 自定义名
            source_profile = cursor.execute(
                "SELECT user_name, custom_name FROM user_profiles "
                "WHERE user_id = ? AND platform_name = ?",
                (source_id, platform),
            ).fetchone()
            target_profile = cursor.execute(
                "SELECT user_name, custom_name FROM user_profiles "
                "WHERE user_id = ? AND platform_name = ?",
                (target_id, DEFAULT_PLATFORM_NAME),
            ).fetchone()
            if source_profile and target_profile:
                cursor.execute(
                    "UPDATE user_profiles SET "
                    "user_name = COALESCE(NULLIF(user_name, ''), ?), "
                    "custom_name = COALESCE(NULLIF(custom_name, ''), ?) "
                    "WHERE user_id = ? AND platform_name = ?",
                    (
                        source_profile[0],
                        source_profile[1],
                        target_id,
                        DEFAULT_PLATFORM_NAME,
                    ),
                )
            elif source_profile:
                cursor.execute(
                    "INSERT INTO user_profiles "
                    "(user_id, platform_name, user_name, custom_name) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        target_id,
                        DEFAULT_PLATFORM_NAME,
                        source_profile[0],
                        source_profile[1],
                    ),
                )
            cursor.execute(
                "DELETE FROM user_profiles WHERE user_id = ? AND platform_name = ?",
                (source_id, platform),
            )

            cursor.execute(
                "INSERT INTO account_bindings "
                "(official_platform, official_user_id, qq_user_id, bound_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    platform,
                    source_id,
                    target_id,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            logger.error(f"[OurNotes] 绑定官机失败: {exc}", exc_info=True)
            return False
        finally:
            conn.close()

    def unbind_official_account(self, official_platform: str, official_user_id: str) -> bool:
        platform = self._normalize_platform(official_platform)
        source_id = str(official_user_id).strip()
        if platform != OFFICIAL_PLATFORM_NAME or not source_id:
            return False
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            if not cursor.execute(
                "SELECT 1 FROM account_bindings "
                "WHERE official_platform = ? AND official_user_id = ?",
                (platform, source_id),
            ).fetchone():
                conn.rollback()
                return False
            cursor.execute(
                "DELETE FROM account_bindings "
                "WHERE official_platform = ? AND official_user_id = ?",
                (platform, source_id),
            )
            conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            logger.error(f"[OurNotes] 解绑官机失败: {exc}", exc_info=True)
            return False
        finally:
            conn.close()

    # ---------- 统计 ----------

    def update_stats(
        self,
        user_id: str,
        user_name: str,
        platform_name: str,
        game: str,
        score: int,
        correct: bool,
    ) -> None:
        platform = self._normalize_platform(platform_name)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO user_stats "
                "(user_id, platform_name, game, user_name, score, attempts, correct_attempts) "
                "VALUES (?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(user_id, platform_name, game) DO UPDATE SET "
                "user_name = excluded.user_name, "
                "score = user_stats.score + excluded.score, "
                "attempts = user_stats.attempts + 1, "
                "correct_attempts = user_stats.correct_attempts + excluded.correct_attempts",
                (
                    str(user_id),
                    platform,
                    game,
                    user_name,
                    int(score),
                    1 if correct else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_user_game_stats(
        self, user_id: str, platform_name: str, game: str
    ) -> Optional[tuple]:
        platform = self._normalize_platform(platform_name)
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT score, attempts, correct_attempts FROM user_stats "
                "WHERE user_id = ? AND platform_name = ? AND game = ?",
                (str(user_id), platform, game),
            ).fetchone()
        finally:
            conn.close()

    def get_user_all_stats(self, user_id: str, platform_name: str) -> dict[str, tuple]:
        platform = self._normalize_platform(platform_name)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT game, score, attempts, correct_attempts FROM user_stats "
                "WHERE user_id = ? AND platform_name = ?",
                (str(user_id), platform),
            ).fetchall()
            return {row[0]: (row[1], row[2], row[3]) for row in rows}
        finally:
            conn.close()

    def get_user_total_stats(self, user_id: str, platform_name: str) -> tuple[int, int, int]:
        stats = self.get_user_all_stats(user_id, platform_name)
        score = sum(int(v[0] or 0) for v in stats.values())
        attempts = sum(int(v[1] or 0) for v in stats.values())
        correct = sum(int(v[2] or 0) for v in stats.values())
        return score, attempts, correct

    def get_user_rank(self, score: int, game: Optional[str] = None) -> int:
        conn = self._connect()
        try:
            if game:
                row = conn.execute(
                    "SELECT COUNT(*) FROM user_stats WHERE game = ? AND score > ?",
                    (game, int(score)),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM ("
                    "  SELECT SUM(score) AS total FROM user_stats "
                    "  GROUP BY user_id, platform_name"
                    ") WHERE total > ?",
                    (int(score),),
                ).fetchone()
            return int(row[0]) + 1 if row else 1
        finally:
            conn.close()

    def get_ranking(self, limit: int, game: Optional[str] = None) -> list[tuple]:
        """返回 (user_id, user_name, custom_name, score, attempts, correct, unbound_official)。"""
        conn = self._connect()
        try:
            if game:
                return conn.execute(
                    "SELECT s.user_id, COALESCE(p.user_name, s.user_name), "
                    "p.custom_name, s.score, "
                    "s.attempts, s.correct_attempts, "
                    "CASE WHEN s.platform_name = ? AND b.official_user_id IS NULL "
                    "THEN 1 ELSE 0 END AS is_unbound_official "
                    "FROM user_stats AS s "
                    "LEFT JOIN user_profiles AS p "
                    "ON p.user_id = s.user_id AND p.platform_name = s.platform_name "
                    "LEFT JOIN account_bindings AS b "
                    "ON b.official_platform = s.platform_name "
                    "AND b.official_user_id = s.user_id "
                    "WHERE s.game = ? "
                    "AND (b.official_user_id IS NULL OR s.platform_name != ?) "
                    "ORDER BY s.score DESC, s.correct_attempts DESC, s.attempts ASC "
                    "LIMIT ?",
                    (
                        OFFICIAL_PLATFORM_NAME,
                        game,
                        OFFICIAL_PLATFORM_NAME,
                        max(1, int(limit)),
                    ),
                ).fetchall()

            return conn.execute(
                "SELECT s.user_id, "
                "MAX(COALESCE(p.user_name, s.user_name)), MAX(p.custom_name), "
                "SUM(s.score), SUM(s.attempts), SUM(s.correct_attempts), "
                "MAX(CASE WHEN s.platform_name = ? AND b.official_user_id IS NULL "
                "THEN 1 ELSE 0 END) AS is_unbound_official "
                "FROM user_stats AS s "
                "LEFT JOIN user_profiles AS p "
                "ON p.user_id = s.user_id AND p.platform_name = s.platform_name "
                "LEFT JOIN account_bindings AS b "
                "ON b.official_platform = s.platform_name "
                "AND b.official_user_id = s.user_id "
                "WHERE (b.official_user_id IS NULL OR s.platform_name != ?) "
                "GROUP BY s.user_id, s.platform_name "
                "ORDER BY SUM(s.score) DESC, SUM(s.correct_attempts) DESC, "
                "SUM(s.attempts) ASC LIMIT ?",
                (
                    OFFICIAL_PLATFORM_NAME,
                    OFFICIAL_PLATFORM_NAME,
                    max(1, int(limit)),
                ),
            ).fetchall()
        finally:
            conn.close()

    # ---------- 每日次数 ----------

    def get_today_plays(
        self, user_id: str, platform_name: str, game: Optional[str] = None
    ) -> int:
        platform = self._normalize_platform(platform_name)
        today = _today()
        conn = self._connect()
        try:
            if game:
                row = conn.execute(
                    "SELECT plays FROM daily_plays "
                    "WHERE user_id = ? AND platform_name = ? AND game = ? AND play_date = ?",
                    (str(user_id), platform, game, today),
                ).fetchone()
                return int(row[0]) if row else 0
            row = conn.execute(
                "SELECT COALESCE(SUM(plays), 0) FROM daily_plays "
                "WHERE user_id = ? AND platform_name = ? AND play_date = ?",
                (str(user_id), platform, today),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def can_play_today(
        self,
        user_id: str,
        platform_name: str,
        game: str,
        daily_limit: int,
        per_game: bool = False,
    ) -> bool:
        if daily_limit == -1:
            return True
        plays = self.get_today_plays(
            user_id, platform_name, game if per_game else None
        )
        return plays < daily_limit

    def update_user_play(
        self, user_id: str, user_name: str, platform_name: str, game: str
    ) -> None:
        platform = self._normalize_platform(platform_name)
        today = _today()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO user_profiles (user_id, platform_name, user_name) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, platform_name) DO UPDATE SET "
                "user_name = COALESCE(NULLIF(excluded.user_name, ''), user_name)",
                (str(user_id), platform, user_name),
            )
            conn.execute(
                "INSERT INTO daily_plays "
                "(user_id, platform_name, game, play_date, plays) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(user_id, platform_name, game, play_date) DO UPDATE SET "
                "plays = plays + 1",
                (str(user_id), platform, game, today),
            )
            conn.commit()
        finally:
            conn.close()

    def reset_daily_limit(
        self, user_id: str, platform_name: Optional[str] = None, game: Optional[str] = None
    ) -> bool:
        conn = self._connect()
        try:
            params: list = [str(user_id)]
            sql = "DELETE FROM daily_plays WHERE user_id = ?"
            if platform_name:
                sql += " AND platform_name = ?"
                params.append(self._normalize_platform(platform_name))
            if game:
                sql += " AND game = ?"
                params.append(game)
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    # ---------- 显示名 ----------

    def set_custom_name(
        self,
        user_id: str,
        user_name: str,
        custom_name: Optional[str],
        platform_name: str,
    ) -> bool:
        platform = self._normalize_platform(platform_name)
        conn = self._connect()
        try:
            if custom_name:
                conn.execute(
                    "INSERT INTO user_profiles "
                    "(user_id, platform_name, user_name, custom_name) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(user_id, platform_name) DO UPDATE SET "
                    "user_name = COALESCE(NULLIF(excluded.user_name, ''), user_name), "
                    "custom_name = excluded.custom_name",
                    (str(user_id), platform, user_name, custom_name),
                )
                conn.commit()
                return True
            cursor = conn.execute(
                "UPDATE user_profiles SET custom_name = NULL "
                "WHERE user_id = ? AND platform_name = ?",
                (str(user_id), platform),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_profile(self, user_id: str, platform_name: str) -> tuple[str, Optional[str]]:
        platform = self._normalize_platform(platform_name)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT user_name, custom_name FROM user_profiles "
                "WHERE user_id = ? AND platform_name = ?",
                (str(user_id), platform),
            ).fetchone()
            if row:
                return str(row[0] or ""), row[1]
            return "", None
        finally:
            conn.close()
