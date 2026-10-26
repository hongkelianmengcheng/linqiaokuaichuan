"""Generate the small branded ICO files used by LanBridge.

The application loads these files at runtime (and PyInstaller also uses them
for the executable icon).  Keeping the generator in the repository means the
icons can be recreated deterministically on a clean build machine without
checking binary assets into source control.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


SIZES = (16, 24, 32, 48, 64, 128, 256)


def _rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, fill: str) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def make_icon(*, accent: str, accent_light: str, receiving: bool = False) -> Image.Image:
    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # A dark rounded tile remains legible on both light and dark Windows themes.
    _rounded(draw, (8, 8, 248, 248), 54, "#172033")
    _rounded(draw, (25, 25, 231, 231), 40, accent)

    # Two offset bridge lanes suggest a sender/receiver pair and remain crisp
    # when Windows scales the icon down to 16px.
    draw.rounded_rectangle((58, 77, 146, 112), radius=17, fill="#FFFFFF")
    draw.rounded_rectangle((110, 144, 198, 179), radius=17, fill="#FFFFFF")
    draw.polygon(((137, 90), (181, 90), (181, 66), (211, 95), (181, 124), (181, 100), (137, 100)), fill="#FFFFFF")
    draw.polygon(((119, 166), (75, 166), (75, 190), (45, 161), (75, 132), (75, 156), (119, 156)), fill="#FFFFFF")

    # Small accent nodes make the two directions distinguishable while keeping
    # the same visual identity for both executables.
    draw.ellipse((39, 39, 67, 67), fill=accent_light if not receiving else "#FFFFFF")
    draw.ellipse((189, 189, 217, 217), fill=accent_light if receiving else "#FFFFFF")
    # A directional badge distinguishes the clients without changing palette.
    badge = ((180, 48), (192, 64), (168, 64)) if not receiving else ((180, 68), (192, 52), (168, 52))
    draw.polygon(badge, fill="#FFFFFF")
    return image


def write_icon(path: Path, *, accent: str, accent_light: str, receiving: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = make_icon(accent=accent, accent_light=accent_light, receiving=receiving)
    image.save(path, format="ICO", sizes=[(s, s) for s in SIZES])


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate LanBridge ICO assets")
    parser.add_argument("--output", type=Path, default=Path("assets"), help="directory receiving the ICO files")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    write_icon(output / "sender.ico", accent="#1678BA", accent_light="#55DDF5")
    write_icon(output / "receiver.ico", accent="#1678BA", accent_light="#55DDF5", receiving=True)
    print(f"Generated {output / 'sender.ico'}")
    print(f"Generated {output / 'receiver.ico'}")


if __name__ == "__main__":
    main()
