"""
Generate error screen MP4 clips served to Plex when a movie can't play.
Images are rendered with Pillow, encoded to 10-second MP4 via FFmpeg.
Generated once at startup, cached in /data/error_screens/.
"""

import logging
import os
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("error_screens")

SCREEN_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "error_screens"
ASSETS_DIR = Path(__file__).parent / "assets"
BG_PATH = ASSETS_DIR / "bg.jpg"

W, H = 1920, 1080
DURATION = 10

CARD_COLOR = (20, 20, 45, 210)
TEXT_COLOR = (230, 230, 240)
SUBTEXT_COLOR = (175, 175, 190)
SEPARATOR_COLOR = (80, 80, 110)

SCREENS = {
    "dead": {
        "title": "Movie Temporarily Removed",
        "message": "This movie has been temporarily removed\nand is currently unavailable.\n\nPlease check back later.",
        "accent": (231, 76, 60),
    },
    "busy": {
        "title": "Streaming Service Busy",
        "message": "The streaming service is temporarily busy.\n\nPlease try again in 5-10 minutes.",
        "accent": (243, 156, 18),
    },
    "removed": {
        "title": "Content Not Available",
        "message": "This movie is no longer available\nfrom the content provider.\n\nIt has been removed from the library.",
        "accent": (149, 165, 166),
    },
}

_mp4_cache: dict[str, Path] = {}


def _find_font(bold: bool = False) -> str:
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    for f in candidates:
        if os.path.exists(f):
            return f
    return ""


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = _find_font(bold)
    if path:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _render_image(key: str) -> Image.Image:
    sc = SCREENS[key]

    if BG_PATH.exists():
        bg = Image.open(BG_PATH).convert("RGB").resize((W, H), Image.LANCZOS)
        from PIL import ImageEnhance
        img = ImageEnhance.Brightness(bg).enhance(0.7)
    else:
        img = Image.new("RGB", (W, H), (12, 12, 28))

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    lines = sc["message"].split("\n")
    card_h = 280 + len(lines) * 36
    card_w = 950
    card_x = (W - card_w) // 2
    card_y = (H - card_h) // 2
    card_radius = 24

    shadow_offset = 8
    odraw.rounded_rectangle(
        [card_x + shadow_offset, card_y + shadow_offset,
         card_x + card_w + shadow_offset, card_y + card_h + shadow_offset],
        radius=card_radius, fill=(0, 0, 0, 120),
    )

    accent_rgba = sc["accent"] + (220,)
    odraw.rounded_rectangle(
        [card_x, card_y, card_x + card_w, card_y + card_h],
        radius=card_radius, fill=CARD_COLOR, outline=accent_rgba, width=2,
    )

    odraw.rectangle(
        [card_x + 1, card_y + 1, card_x + card_w - 1, card_y + 6],
        fill=sc["accent"] + (255,),
    )

    img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay)
    img = img.convert("RGB")

    draw = ImageDraw.Draw(img)

    icon_y = card_y + 58
    icon_r = 32
    icon_cx = W // 2
    accent_dim = tuple(int(c * 0.4) for c in sc["accent"])
    draw.ellipse(
        [icon_cx - icon_r, icon_y - icon_r, icon_cx + icon_r, icon_y + icon_r],
        fill=accent_dim, outline=sc["accent"], width=2,
    )
    draw.text((icon_cx, icon_y), "!", fill=sc["accent"],
              font=_get_font(38, bold=True), anchor="mm")

    title_y = icon_y + icon_r + 28
    draw.text((W // 2, title_y), sc["title"], fill=TEXT_COLOR,
              font=_get_font(38, bold=True), anchor="mt")

    sep_y = title_y + 48
    sep_margin = 120
    draw.line(
        [(card_x + sep_margin, sep_y), (card_x + card_w - sep_margin, sep_y)],
        fill=SEPARATOR_COLOR, width=1,
    )

    msg_font = _get_font(24)
    msg_y = sep_y + 28
    for line in lines:
        if line.strip():
            draw.text((W // 2, msg_y), line.strip(), fill=SUBTEXT_COLOR,
                      font=msg_font, anchor="mt")
        msg_y += 36

    return img


def _encode_mp4(png_path: Path, mp4_path: Path) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(png_path),
        "-c:v", "libx264",
        "-t", str(DURATION),
        "-pix_fmt", "yuv420p",
        "-vf", f"scale={W}:{H}",
        "-r", "1",
        "-preset", "ultrafast",
        "-crf", "28",
        "-movflags", "+faststart",
        str(mp4_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            logger.error("FFmpeg failed for %s: %s", mp4_path.name, result.stderr.decode()[-500:])
            return False
        return True
    except Exception as e:
        logger.error("FFmpeg exception for %s: %s", mp4_path.name, e)
        return False


def generate_error_screens():
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    generated = 0

    for key in SCREENS:
        mp4_path = SCREEN_DIR / f"error_{key}.mp4"

        if mp4_path.exists() and mp4_path.stat().st_size > 0:
            _mp4_cache[key] = mp4_path
            logger.info("Error screen '%s' already cached: %s", key, mp4_path)
            generated += 1
            continue

        png_path = SCREEN_DIR / f"error_{key}.png"
        img = _render_image(key)
        img.save(str(png_path), "PNG")

        if _encode_mp4(png_path, mp4_path):
            _mp4_cache[key] = mp4_path
            generated += 1
            logger.info("Generated error screen '%s': %s (%d bytes)",
                        key, mp4_path, mp4_path.stat().st_size)
        else:
            logger.error("Failed to generate error screen '%s'", key)

        png_path.unlink(missing_ok=True)

    logger.info("Error screens ready: %d/%d", generated, len(SCREENS))


def get_error_screen(screen_type: str) -> Path | None:
    return _mp4_cache.get(screen_type)


def get_error_screen_data(screen_type: str) -> bytes | None:
    path = _mp4_cache.get(screen_type)
    if path and path.exists():
        return path.read_bytes()
    return None


def get_error_screen_size(screen_type: str) -> int:
    path = _mp4_cache.get(screen_type)
    if path and path.exists():
        return path.stat().st_size
    return 0
