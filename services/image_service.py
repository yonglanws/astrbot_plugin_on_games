"""图片效果与渲染服务。

- 猜卡面：7 种效果（模糊/分块/切割/截取）
- 猜曲绘：4 种效果（模糊/随机裁切/分块简易/分块困难）
- 猜歌：12 个曲绘选项图
- 排行榜：沿用原插件的横向表格 UI
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    from PIL.Image import Resampling

    LANCZOS = Resampling.LANCZOS
except ImportError:  # pragma: no cover
    LANCZOS = 1

from pilmoji import Pilmoji

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger("astrbot_plugin_on_games")


CARD_DEFAULT_EFFECTS = {
    "light_blur": {
        "name": "轻度模糊",
        "description": "轻微高斯模糊",
        "enabled": True,
        "difficulty": 1,
        "blur_radius": 40,
    },
    "heavy_blur": {
        "name": "重度模糊",
        "description": "较强高斯模糊",
        "enabled": True,
        "difficulty": 2,
        "blur_radius": 80,
    },
    "shuffle_blocks_easy": {
        "name": "分块打乱(简易)",
        "description": "较大方块随机重排",
        "enabled": True,
        "difficulty": 1,
        "block_size": 128,
    },
    "shuffle_blocks_hard": {
        "name": "分块打乱(困难)",
        "description": "较小方块随机重排",
        "enabled": True,
        "difficulty": 3,
        "block_size": 48,
    },
    "horizontal_slice": {
        "name": "横向切割",
        "description": "横向长条随机重排",
        "enabled": True,
        "difficulty": 2,
        "slice_count": 16,
    },
    "vertical_slice": {
        "name": "纵向切割",
        "description": "纵向长条随机重排",
        "enabled": True,
        "difficulty": 2,
        "slice_count": 16,
    },
    "crop_area": {
        "name": "截取区域",
        "description": "随机显示一部分区域",
        "enabled": True,
        "difficulty": 1,
        "crop_ratio": 0.35,
    },
}

JACKET_DEFAULT_EFFECTS = {
    "blur": {
        "name": "模糊",
        "description": "高斯模糊",
        "enabled": True,
        "difficulty": 1,
        "blur_radius": 35,
    },
    "random_crop": {
        "name": "随机裁切",
        "description": "只显示封面的一小部分",
        "enabled": True,
        "difficulty": 1,
        "crop_ratio": 0.3,
    },
    "shuffle_blocks_easy": {
        "name": "分块打乱(简易)",
        "description": "较大方块随机重排",
        "enabled": True,
        "difficulty": 1,
        "block_size": 50,
    },
    "shuffle_blocks_hard": {
        "name": "分块打乱(困难)",
        "description": "较小方块随机重排",
        "enabled": True,
        "difficulty": 3,
        "block_size": 20,
    },
}


class ImageService:
    def __init__(
        self,
        resources_dir: Path,
        output_dir: Path,
        config,
        display_name_resolver: Optional[Callable[[str, Optional[str]], str]] = None,
    ):
        self.resources_dir = Path(resources_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.display_name_resolver = display_name_resolver
        self.card_effects = self._build_effects(CARD_DEFAULT_EFFECTS, "card_effects")
        self.jacket_effects = self._build_effects(
            JACKET_DEFAULT_EFFECTS, "jacket_effects"
        )
        self._load_fonts()

    # ---------- 配置 ----------

    @staticmethod
    def _deep_copy(effects: dict) -> dict:
        return {k: dict(v) for k, v in effects.items()}

    def _build_effects(self, defaults: dict, config_key: str) -> dict:
        effects = self._deep_copy(defaults)
        overrides = self.config.get(config_key, {}) if self.config else {}
        if not isinstance(overrides, dict):
            return effects
        for name, values in overrides.items():
            if name not in effects or not isinstance(values, dict):
                continue
            if "enabled" in values:
                effects[name]["enabled"] = bool(values["enabled"])
            if "difficulty" in values:
                try:
                    val = int(values["difficulty"])
                    effects[name]["difficulty"] = max(1, min(10, val))
                except (TypeError, ValueError):
                    pass
            for param in ("blur_radius", "block_size", "slice_count"):
                if param in values:
                    try:
                        effects[name][param] = max(1, int(values[param]))
                    except (TypeError, ValueError):
                        pass
            for param in ("crop_ratio",):
                if param in values:
                    try:
                        effects[name][param] = max(0.1, min(1.0, float(values[param])))
                    except (TypeError, ValueError):
                        pass
        return effects

    def get_enabled_card_effects(self) -> list[str]:
        return [k for k, v in self.card_effects.items() if v.get("enabled", True)]

    def get_enabled_jacket_effects(self) -> list[str]:
        return [k for k, v in self.jacket_effects.items() if v.get("enabled", True)]

    def random_card_effect(self) -> tuple[str, str, int]:
        enabled = self.get_enabled_card_effects()
        if not enabled:
            return "none", "无效果", 1
        name = random.choice(enabled)
        info = self.card_effects[name]
        return name, info["name"], int(info.get("difficulty", 1))

    def random_jacket_effect(self) -> tuple[str, str, int]:
        enabled = self.get_enabled_jacket_effects()
        if not enabled:
            return "none", "无效果", 0
        name = random.choice(enabled)
        info = self.jacket_effects[name]
        return name, info["name"], int(info.get("difficulty", 1))

    # ---------- 字体 ----------

    def _load_fonts(self) -> None:
        font_path = self.resources_dir / "font.ttf"
        default = ImageFont.load_default()
        try:
            if font_path.exists():
                self.title_font = ImageFont.truetype(str(font_path), 48)
                self.header_font = ImageFont.truetype(str(font_path), 28)
                self.body_font = ImageFont.truetype(str(font_path), 26)
                self.id_font = ImageFont.truetype(str(font_path), 16)
                self.medal_font = ImageFont.truetype(str(font_path), 36)
                self.option_header_font = ImageFont.truetype(str(font_path), 36)
                self.card_title_font = ImageFont.truetype(str(font_path), 24)
                self.card_subtitle_font = ImageFont.truetype(str(font_path), 16)
                return
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 字体加载失败: {exc}")
        self.title_font = default
        self.header_font = default
        self.body_font = default
        self.id_font = default
        self.medal_font = default
        self.option_header_font = default
        self.card_title_font = default
        self.card_subtitle_font = default

    # ---------- 基础效果 ----------

    @staticmethod
    def _shuffle_blocks(img: Image.Image, block_size: int = 50) -> Image.Image:
        try:
            w, h = img.size
            result = Image.new(img.mode, (w, h))
            positions = [
                (x, y)
                for y in range(0, h, block_size)
                for x in range(0, w, block_size)
            ]
            shuffled = positions.copy()
            random.shuffle(shuffled)
            for idx, (orig_x, orig_y) in enumerate(positions):
                current_w = min(block_size, w - orig_x)
                current_h = min(block_size, h - orig_y)
                block = img.crop((orig_x, orig_y, orig_x + current_w, orig_y + current_h))
                new_x, new_y = shuffled[idx]
                new_w = min(block_size, w - new_x)
                new_h = min(block_size, h - new_y)
                if current_w != new_w or current_h != new_h:
                    try:
                        block = block.resize((new_w, new_h), LANCZOS)
                    except Exception:
                        block = block.resize((new_w, new_h))
                result.paste(block, (new_x, new_y))
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 分块打乱失败: {exc}", exc_info=True)
            return img

    @staticmethod
    def _horizontal_slice(img: Image.Image, slice_count: int = 8) -> Image.Image:
        try:
            w, h = img.size
            slice_count = max(2, int(slice_count))
            slice_height = h // slice_count
            slices = []
            for i in range(slice_count):
                y_start = i * slice_height
                y_end = (i + 1) * slice_height if i < slice_count - 1 else h
                slices.append(img.crop((0, y_start, w, y_end)))
            random.shuffle(slices)
            result = Image.new(img.mode, (w, h))
            y_offset = 0
            for piece in slices:
                result.paste(piece, (0, y_offset))
                y_offset += piece.size[1]
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 横向切割失败: {exc}", exc_info=True)
            return img

    @staticmethod
    def _vertical_slice(img: Image.Image, slice_count: int = 8) -> Image.Image:
        try:
            w, h = img.size
            slice_count = max(2, int(slice_count))
            slice_width = w // slice_count
            slices = []
            for i in range(slice_count):
                x_start = i * slice_width
                x_end = (i + 1) * slice_width if i < slice_count - 1 else w
                slices.append(img.crop((x_start, 0, x_end, h)))
            random.shuffle(slices)
            result = Image.new(img.mode, (w, h))
            x_offset = 0
            for piece in slices:
                result.paste(piece, (x_offset, 0))
                x_offset += piece.size[0]
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 纵向切割失败: {exc}", exc_info=True)
            return img

    @staticmethod
    def _crop_area(img: Image.Image, crop_ratio: float = 0.5) -> Image.Image:
        try:
            w, h = img.size
            crop_w = max(int(w * crop_ratio), 50)
            crop_h = max(int(h * crop_ratio), 50)
            max_x = max(0, w - crop_w)
            max_y = max(0, h - crop_h)
            x = random.randint(0, max_x) if max_x else 0
            y = random.randint(0, max_y) if max_y else 0
            return img.crop((x, y, x + crop_w, y + crop_h))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 截取区域失败: {exc}", exc_info=True)
            return img

    def _apply_card_effect(self, img: Image.Image, name: str) -> Image.Image:
        info = self.card_effects.get(name)
        if not info:
            return img
        if name == "light_blur":
            return img.filter(ImageFilter.GaussianBlur(radius=info.get("blur_radius", 40)))
        if name == "heavy_blur":
            return img.filter(ImageFilter.GaussianBlur(radius=info.get("blur_radius", 80)))
        if name in ("shuffle_blocks_easy", "shuffle_blocks_hard"):
            return self._shuffle_blocks(img, int(info.get("block_size", 128)))
        if name == "horizontal_slice":
            return self._horizontal_slice(img, int(info.get("slice_count", 16)))
        if name == "vertical_slice":
            return self._vertical_slice(img, int(info.get("slice_count", 16)))
        if name == "crop_area":
            return self._crop_area(img, float(info.get("crop_ratio", 0.35)))
        return img

    def apply_card_effects(self, img: Image.Image, names: list[str]) -> Image.Image:
        result = img.copy()
        for name in names:
            if name in self.card_effects:
                result = self._apply_card_effect(result, name)
        return result

    def apply_jacket_effect(self, img: Image.Image, name: str) -> tuple[Image.Image, int]:
        info = self.jacket_effects.get(name)
        if not info:
            return img, 0
        difficulty = int(info.get("difficulty", 1))
        if name == "blur":
            return (
                img.filter(ImageFilter.GaussianBlur(radius=info.get("blur_radius", 35))),
                difficulty,
            )
        if name == "random_crop":
            return self._crop_area(img, float(info.get("crop_ratio", 0.3))), difficulty
        if name in ("shuffle_blocks_easy", "shuffle_blocks_hard"):
            return self._shuffle_blocks(img, int(info.get("block_size", 50))), difficulty
        return img, difficulty

    # ---------- 选项图（两列卡片，封面在右、标题在左） ----------

    @staticmethod
    def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
        try:
            return int(draw.textlength(text, font=font))
        except Exception:
            try:
                bbox = font.getbbox(text)
                return int(bbox[2] - bbox[0])
            except Exception:
                return len(text) * 10

    def _truncate_to_width(self, draw, text: str, font, max_width: int) -> str:
        if not text:
            return ""
        if self._text_width(draw, text, font) <= max_width:
            return text
        suffix = "..."
        while text and self._text_width(draw, text + suffix, font) > max_width:
            text = text[:-1]
        return (text + suffix) if text else ""

    @staticmethod
    def _square_cover(image: Image.Image, size: int) -> Image.Image:
        """居中裁成正方形并加圆角，避免非正方形曲绘被拉伸。"""
        width, height = image.size
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        cover = (
            image.crop((left, top, left + side, top + side))
            .convert("RGBA")
            .resize((size, size), LANCZOS)
        )
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, size, size], radius=10, fill=255
        )
        output = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        output.paste(cover, (0, 0))
        output.putalpha(mask)
        cover.close()
        return output

    def create_options_image(
        self,
        options: list[dict],
        jackets: list[Optional[Image.Image]],
        output_dir: Optional[Path] = None,
    ) -> Optional[str]:
        output_dir = Path(output_dir or self.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            columns = 2
            card_w, card_h = 480, 160
            gap, padding, cover_size = 20, 15, 130
            header_h = 80
            rows = max(1, (len(options) + columns - 1) // columns)
            img_w = columns * card_w + (columns + 1) * gap
            img_h = header_h + rows * card_h + (rows + 1) * gap

            img = Image.new("RGB", (img_w, img_h), (248, 249, 250))
            draw = ImageDraw.Draw(img)

            header = f"请发送正确答案 (1-{len(options)})"
            header_w = self._text_width(draw, header, self.option_header_font)
            draw.text(
                ((img_w - header_w) // 2, 20),
                header,
                font=self.option_header_font,
                fill=(102, 126, 234),
            )

            for i, option in enumerate(options[: columns * rows]):
                row_idx, col_idx = i // columns, i % columns
                x = gap + col_idx * (card_w + gap)
                y = header_h + gap + row_idx * (card_h + gap)

                draw.rounded_rectangle(
                    [x, y, x + card_w, y + card_h],
                    radius=15,
                    fill=(255, 255, 255),
                    outline=(222, 226, 230),
                    width=1,
                )

                cover_x = x + card_w - cover_size - padding
                cover_y = y + (card_h - cover_size) // 2
                jacket = jackets[i] if i < len(jackets) else None
                if jacket is not None:
                    try:
                        cover = self._square_cover(jacket, cover_size)
                        img.paste(cover, (cover_x, cover_y), cover)
                        cover.close()
                    except Exception:
                        jacket = None
                if jacket is None:
                    draw.rounded_rectangle(
                        [
                            cover_x,
                            cover_y,
                            cover_x + cover_size,
                            cover_y + cover_size,
                        ],
                        radius=10,
                        fill=(233, 236, 239),
                    )
                    draw.text(
                        (cover_x + cover_size / 2, cover_y + cover_size / 2),
                        "?",
                        font=self.card_title_font,
                        fill=(173, 181, 189),
                        anchor="mm",
                    )

                text_x = x + padding
                max_text_w = card_w - cover_size - padding * 3
                draw.text(
                    (text_x, y + padding),
                    f"{i + 1}.",
                    font=self.card_title_font,
                    fill=(102, 126, 234),
                )

                title = self._truncate_to_width(
                    draw,
                    str(option.get("title") or ""),
                    self.card_title_font,
                    max_text_w,
                )
                draw.text(
                    (text_x + 35, y + padding),
                    title,
                    font=self.card_title_font,
                    fill=(33, 37, 41),
                )

                subtitle = self._truncate_to_width(
                    draw,
                    str(option.get("band_name") or ""),
                    self.card_subtitle_font,
                    max_text_w,
                )
                if subtitle:
                    # 小字与黑色标题左对齐（即与标题同一 x 坐标，而非编号）。
                    draw.text(
                        (text_x + 35, y + padding + 35),
                        subtitle,
                        font=self.card_subtitle_font,
                        fill=(108, 117, 125),
                    )

            path = output_dir / f"song_options_{time.time_ns()}.png"
            img.save(path)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 生成选项图失败: {exc}", exc_info=True)
            return None

    # ---------- 排行榜 ----------

    def create_ranking_image(
        self,
        rows: list[tuple],
        title_text: str,
        output_dir: Optional[Path] = None,
    ) -> Optional[str]:
        output_dir = Path(output_dir or self.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            width = 850
            base_height = 250
            item_height = 70
            height = base_height + len(rows) * item_height

            bg_start = (230, 240, 255)
            bg_end = (200, 210, 240)
            img = Image.new("RGB", (width, height), bg_start)
            draw_bg = ImageDraw.Draw(img)
            for y in range(height):
                ratio = y / height
                color = tuple(
                    int(bg_start[i] + (bg_end[i] - bg_start[i]) * ratio) for i in range(3)
                )
                draw_bg.line([(0, y), (width, y)], fill=color)

            if img.mode != "RGBA":
                img = img.convert("RGBA")
            white_overlay = Image.new("RGBA", img.size, (255, 255, 255, 100))
            img = Image.alpha_composite(img, white_overlay)

            font_color = (30, 30, 50)
            shadow_color = (180, 180, 190, 128)
            header_color = (80, 90, 120)
            score_color = (235, 120, 20)
            accuracy_color = (0, 128, 128)

            with Pilmoji(img) as pilmoji:
                center_x, title_y = int(width / 2), 80
                pilmoji.text(
                    (center_x + 2, title_y + 2),
                    title_text,
                    font=self.title_font,
                    fill=shadow_color,
                    anchor="mm",
                    emoji_position_offset=(0, 6),
                )
                pilmoji.text(
                    (center_x, title_y),
                    title_text,
                    font=self.title_font,
                    fill=font_color,
                    anchor="mm",
                    emoji_position_offset=(0, 6),
                )

                headers = ["排名", "玩家", "总分", "正确率", "总次数"]
                col_positions_header = [40, 150, 500, 610, 720]
                title_height = pilmoji.getsize(title_text, font=self.title_font)[1]
                current_y = title_y + int(title_height / 2) + 45
                for header in headers:
                    pilmoji.text(
                        (col_positions_header.pop(0), current_y),
                        header,
                        font=self.header_font,
                        fill=header_color,
                    )

                current_y += 55
                rank_icons = ["🥇", "🥈", "🥉"]

                for i, row in enumerate(rows):
                    user_id = str(row[0])
                    user_name = row[1] or ""
                    custom_name = row[2] if len(row) > 2 else None
                    score = str(row[3] if len(row) > 3 else 0)
                    attempts_raw = row[4] if len(row) > 4 else 0
                    correct_raw = row[5] if len(row) > 5 else 0
                    is_unbound_official = bool(row[6]) if len(row) > 6 else False

                    attempts = int(attempts_raw or 0)
                    correct = int(correct_raw or 0)
                    preferred = custom_name if custom_name else user_name
                    if self.display_name_resolver:
                        display_name = self.display_name_resolver(user_id, preferred)
                    else:
                        display_name = preferred or "未知用户"
                    accuracy = (
                        f"{(correct * 100 / attempts if attempts > 0 else 0):.1f}%"
                    )

                    rank = i + 1
                    col_positions = [40, 150, 500, 610, 720]
                    rank_num_align_x = 130
                    pilmoji.text(
                        (rank_num_align_x, current_y),
                        str(rank),
                        font=self.body_font,
                        fill=font_color,
                        anchor="ra",
                    )
                    if i < 3:
                        pilmoji.text(
                            (col_positions[0], current_y - 30),
                            rank_icons[i],
                            font=self.medal_font,
                            fill=font_color,
                        )

                    name_x = col_positions[1]
                    if is_unbound_official:
                        badge_text = "未绑定QQ"
                        badge_width = (
                            pilmoji.getsize(badge_text, font=self.id_font)[0] + 16
                        )
                        badge_height = 26
                        badge_y = current_y + 3
                        draw = ImageDraw.Draw(img)
                        draw.rounded_rectangle(
                            [
                                name_x,
                                badge_y,
                                name_x + badge_width,
                                badge_y + badge_height,
                            ],
                            radius=8,
                            fill=(115, 125, 150, 230),
                        )
                        pilmoji.text(
                            (name_x + 8, badge_y + 4),
                            badge_text,
                            font=self.id_font,
                            fill=(255, 255, 255, 255),
                        )
                        name_x += badge_width + 10

                    max_name_width = col_positions[2] - name_x - 20
                    try:
                        if self.body_font.getbbox(display_name)[2] > max_name_width:
                            while (
                                self.body_font.getbbox(display_name + "...")[2]
                                > max_name_width
                                and len(display_name) > 0
                            ):
                                display_name = display_name[:-1]
                            display_name += "..."
                    except Exception:
                        pass

                    pilmoji.text(
                        (name_x, current_y),
                        display_name,
                        font=self.body_font,
                        fill=font_color,
                    )
                    id_text = f"{user_name} ID: {user_id}"
                    max_id_width = col_positions[2] - col_positions[1] - 20
                    try:
                        if self.id_font.getbbox(id_text)[2] > max_id_width:
                            while (
                                self.id_font.getbbox(id_text + "...")[2] > max_id_width
                                and len(id_text) > 0
                            ):
                                id_text = id_text[:-1]
                            id_text += "..."
                    except Exception:
                        pass
                    pilmoji.text(
                        (col_positions[1], current_y + 32),
                        id_text,
                        font=self.id_font,
                        fill=header_color,
                    )
                    pilmoji.text(
                        (col_positions[2], current_y),
                        score,
                        font=self.body_font,
                        fill=score_color,
                    )
                    pilmoji.text(
                        (col_positions[3], current_y),
                        accuracy,
                        font=self.body_font,
                        fill=accuracy_color,
                    )
                    pilmoji.text(
                        (col_positions[4], current_y),
                        str(attempts),
                        font=self.body_font,
                        fill=font_color,
                    )

                    separator_y = current_y + 60
                    if i < len(rows) - 1:
                        draw = ImageDraw.Draw(img)
                        draw.line(
                            [(30, separator_y), (width - 30, separator_y)],
                            fill=(200, 200, 210, 128),
                            width=1,
                        )
                    current_y += 70

                footer_text = (
                    f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                pilmoji.text(
                    (center_x, height - 25),
                    footer_text,
                    font=self.id_font,
                    fill=header_color,
                    anchor="ms",
                )

            path = output_dir / f"ranking_{time.time_ns()}.png"
            img.save(path)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 渲染排行榜失败: {exc}", exc_info=True)
            return None

    # ---------- 压缩 ----------

    def compress_image(
        self,
        img: Image.Image,
        max_size: int = 800,
        quality: int = 90,
        prefix: str = "answer",
    ) -> Optional[str]:
        try:
            img.thumbnail((max_size, max_size), LANCZOS)
            if img.mode in ("RGBA", "P", "LA"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                mask = img.split()[-1] if img.mode in ("RGBA", "LA") else None
                background.paste(img, mask=mask)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            os.makedirs(self.output_dir, exist_ok=True)
            path = self.output_dir / f"{prefix}_{time.time_ns()}.jpg"
            img.save(path, format="JPEG", quality=quality, optimize=True)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[OurNotes] 压缩图片失败: {exc}")
            return None
