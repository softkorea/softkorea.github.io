"""Generate favicon set from existing avatar for Chirpy theme."""

from PIL import Image
import os
import struct

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AVATAR_PATH = os.path.join(PROJ_ROOT, "assets", "img", "avatar.png")
OUT_DIR = os.path.join(PROJ_ROOT, "assets", "img", "favicons")


def create_ico(img, path, sizes=((16, 16), (32, 32), (48, 48))):
    """Create a multi-size .ico file."""
    imgs = []
    for s in sizes:
        resized = img.resize(s, Image.LANCZOS).convert("RGBA")
        imgs.append(resized)
    imgs[0].save(path, format="ICO", sizes=sizes)


def create_webmanifest(path):
    """Create site.webmanifest for PWA."""
    content = """{
  "name": "Doc, Code, Log",
  "short_name": "DCL",
  "icons": [
    {
      "src": "/assets/img/favicons/android-chrome-192x192.png",
      "sizes": "192x192",
      "type": "image/png"
    },
    {
      "src": "/assets/img/favicons/android-chrome-512x512.png",
      "sizes": "512x512",
      "type": "image/png"
    }
  ],
  "theme_color": "#1d1e28",
  "background_color": "#1d1e28",
  "display": "standalone"
}
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def create_browserconfig(path):
    """Create browserconfig.xml for Windows tiles."""
    content = """<?xml version="1.0" encoding="utf-8"?>
<browserconfig>
    <msapplication>
        <tile>
            <square150x150logo src="/assets/img/favicons/mstile-150x150.png"/>
            <TileColor>#1d1e28</TileColor>
        </tile>
    </msapplication>
</browserconfig>
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    avatar = Image.open(AVATAR_PATH).convert("RGBA")

    # PNG favicons at various sizes
    sizes = {
        "favicon-96x96.png": 96,
        "favicon-32x32.png": 32,
        "favicon-16x16.png": 16,
        "apple-touch-icon.png": 180,
        "mstile-150x150.png": 150,
        "android-chrome-192x192.png": 192,
        "android-chrome-512x512.png": 512,
    }

    for filename, size in sizes.items():
        resized = avatar.resize((size, size), Image.LANCZOS)
        out_path = os.path.join(OUT_DIR, filename)
        resized.save(out_path, "PNG")
        print(f"  {filename} ({size}x{size})")

    # favicon.ico (multi-size)
    ico_path = os.path.join(OUT_DIR, "favicon.ico")
    create_ico(avatar, ico_path)
    print(f"  favicon.ico (16+32+48)")

    # site.webmanifest
    create_webmanifest(os.path.join(OUT_DIR, "site.webmanifest"))
    print(f"  site.webmanifest")

    # browserconfig.xml
    create_browserconfig(os.path.join(OUT_DIR, "browserconfig.xml"))
    print(f"  browserconfig.xml")

    print(f"\nAll favicons saved to: {OUT_DIR}")


if __name__ == "__main__":
    main()
