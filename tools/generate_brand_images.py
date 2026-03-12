"""Generate avatar and og:image for the blog."""

from PIL import Image, ImageDraw, ImageFont
import math

# ── Colors ──
BG_DARK = (18, 24, 38)        # deep navy
ACCENT_TEAL = (0, 188, 180)   # teal / medical green
ACCENT_BLUE = (80, 140, 255)  # code blue
TEXT_WHITE = (240, 240, 245)
TEXT_DIM = (160, 170, 190)
GLOW_TEAL = (0, 188, 180, 60)

# ── Fonts ──
NOTO_BOLD = "C:/Windows/Fonts/NotoSansKR-VF.ttf"
CONSOLAS_BOLD = "C:/Windows/Fonts/consolab.ttf"
CONSOLAS = "C:/Windows/Fonts/consola.ttf"


def draw_rounded_rect(draw, xy, radius, fill):
    """Draw a rounded rectangle."""
    x0, y0, x1, y1 = xy
    draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
    draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    draw.pieslice([x0, y0, x0 + 2 * radius, y0 + 2 * radius], 180, 270, fill=fill)
    draw.pieslice([x1 - 2 * radius, y0, x1, y0 + 2 * radius], 270, 360, fill=fill)
    draw.pieslice([x0, y1 - 2 * radius, x0 + 2 * radius, y1], 90, 180, fill=fill)
    draw.pieslice([x1 - 2 * radius, y1 - 2 * radius, x1, y1], 0, 90, fill=fill)


def draw_ecg_line(draw, x_start, x_end, y_center, amplitude, thickness, color):
    """Draw an ECG/heartbeat line."""
    points = []
    total_w = x_end - x_start
    # Flat start
    for x in range(0, int(total_w * 0.25)):
        points.append((x_start + x, y_center))
    # Small P wave
    p_start = int(total_w * 0.25)
    p_width = int(total_w * 0.08)
    for x in range(p_width):
        t = x / p_width
        y = y_center - amplitude * 0.2 * math.sin(t * math.pi)
        points.append((x_start + p_start + x, y))
    # Brief flat
    flat1_start = p_start + p_width
    for x in range(int(total_w * 0.04)):
        points.append((x_start + flat1_start + x, y_center))
    # QRS complex - sharp spike
    qrs_start = flat1_start + int(total_w * 0.04)
    qrs_w = int(total_w * 0.12)
    # Q dip
    q_w = qrs_w // 3
    for x in range(q_w):
        t = x / q_w
        y = y_center + amplitude * 0.15 * math.sin(t * math.pi)
        points.append((x_start + qrs_start + x, y))
    # R peak
    r_w = qrs_w // 3
    for x in range(r_w):
        t = x / r_w
        y = y_center - amplitude * math.sin(t * math.pi)
        points.append((x_start + qrs_start + q_w + x, y))
    # S dip
    s_w = qrs_w // 3
    for x in range(s_w):
        t = x / s_w
        y = y_center + amplitude * 0.3 * math.sin(t * math.pi)
        points.append((x_start + qrs_start + q_w + r_w + x, y))
    # Brief flat
    flat2_start = qrs_start + qrs_w
    for x in range(int(total_w * 0.06)):
        points.append((x_start + flat2_start + x, y_center))
    # T wave
    t_start = flat2_start + int(total_w * 0.06)
    t_width = int(total_w * 0.12)
    for x in range(t_width):
        t = x / t_width
        y = y_center - amplitude * 0.3 * math.sin(t * math.pi)
        points.append((x_start + t_start + x, y))
    # Flat end
    flat_end_start = t_start + t_width
    remaining = total_w - flat_end_start
    for x in range(max(0, int(remaining))):
        points.append((x_start + flat_end_start + x, y_center))

    if len(points) > 1:
        draw.line(points, fill=color, width=thickness, joint="curve")


def generate_avatar(path, size=512):
    """Generate a 512x512 avatar: code brackets + ECG line."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Circular dark background
    margin = 4
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=BG_DARK,
    )

    # Subtle ring
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        outline=ACCENT_TEAL,
        width=3,
    )

    # Code brackets: { }
    bracket_font = ImageFont.truetype(CONSOLAS_BOLD, 160)
    cx, cy = size // 2, size // 2

    # Left bracket {
    bbox_l = draw.textbbox((0, 0), "{", font=bracket_font)
    lw = bbox_l[2] - bbox_l[0]
    lh = bbox_l[3] - bbox_l[1]
    draw.text(
        (cx - 120 - lw // 2, cy - lh // 2 - 15),
        "{",
        fill=ACCENT_TEAL,
        font=bracket_font,
    )

    # Right bracket }
    bbox_r = draw.textbbox((0, 0), "}", font=bracket_font)
    rw = bbox_r[2] - bbox_r[0]
    rh = bbox_r[3] - bbox_r[1]
    draw.text(
        (cx + 120 - rw // 2, cy - rh // 2 - 15),
        "}",
        fill=ACCENT_TEAL,
        font=bracket_font,
    )

    # ECG line between brackets
    draw_ecg_line(
        draw,
        x_start=cx - 85,
        x_end=cx + 85,
        y_center=cy,
        amplitude=65,
        thickness=5,
        color=ACCENT_BLUE,
    )

    # Small "MD" text below
    md_font = ImageFont.truetype(CONSOLAS_BOLD, 36)
    bbox_md = draw.textbbox((0, 0), "MD", font=md_font)
    md_w = bbox_md[2] - bbox_md[0]
    draw.text(
        (cx - md_w // 2, cy + 95),
        "MD",
        fill=TEXT_DIM,
        font=md_font,
    )

    # Convert to RGB with dark background for final PNG
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    final = Image.alpha_composite(bg, img)
    final.save(path, "PNG")
    print(f"Avatar saved: {path} ({size}x{size})")


def generate_og_image(path, avatar_path, w=1200, h=630):
    """Generate 1200x630 og:image banner."""
    img = Image.new("RGB", (w, h), BG_DARK)
    draw = ImageDraw.Draw(img)

    # Subtle grid pattern
    for x in range(0, w, 40):
        draw.line([(x, 0), (x, h)], fill=(25, 32, 48), width=1)
    for y in range(0, h, 40):
        draw.line([(0, y), (w, y)], fill=(25, 32, 48), width=1)

    # ECG line across the background (decorative)
    draw_ecg_line(draw, 0, w, h // 2, 40, 2, (30, 50, 70))

    # Left section: Avatar
    avatar = Image.open(avatar_path).convert("RGBA")
    avatar_size = 180
    avatar_resized = avatar.resize((avatar_size, avatar_size), Image.LANCZOS)
    # Create circular mask
    mask = Image.new("L", (avatar_size, avatar_size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse([0, 0, avatar_size, avatar_size], fill=255)
    avatar_x = 100
    avatar_y = (h - avatar_size) // 2
    img.paste(
        avatar_resized,
        (avatar_x, avatar_y),
        avatar_resized,
    )

    # Right section: Text
    text_x = avatar_x + avatar_size + 70

    # Title
    title_font = ImageFont.truetype(CONSOLAS_BOLD, 72)
    draw.text((text_x, 160), "Doc, Code, Log", fill=TEXT_WHITE, font=title_font)

    # Accent line
    draw.line(
        [(text_x, 260), (text_x + 400, 260)],
        fill=ACCENT_TEAL,
        width=3,
    )

    # Tagline in Korean
    tagline_font = ImageFont.truetype(NOTO_BOLD, 36)
    draw.text(
        (text_x, 285),
        "의사, 코드, 로그",
        fill=TEXT_DIM,
        font=tagline_font,
    )

    # Description
    desc_font = ImageFont.truetype(NOTO_BOLD, 24)
    draw.text(
        (text_x, 345),
        "의사이자 개발자의 진료실 기술, 코딩, 그리고 일상 기록",
        fill=(120, 130, 150),
        font=desc_font,
    )

    # URL at bottom
    url_font = ImageFont.truetype(CONSOLAS, 22)
    draw.text(
        (text_x, 420),
        "drsoftkorea.com",
        fill=ACCENT_TEAL,
        font=url_font,
    )

    # Bottom accent bar
    draw.rectangle([0, h - 4, w, h], fill=ACCENT_TEAL)

    img.save(path, "PNG", quality=95)
    print(f"OG image saved: {path} ({w}x{h})")


if __name__ == "__main__":
    import os

    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets", "img",
    )
    os.makedirs(out_dir, exist_ok=True)

    avatar_path = os.path.join(out_dir, "avatar.png")
    og_path = os.path.join(out_dir, "og-default.png")

    generate_avatar(avatar_path)
    generate_og_image(og_path, avatar_path)
    print("Done!")
