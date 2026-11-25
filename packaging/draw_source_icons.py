from pathlib import Path
import struct
import zlib

ROOT = Path(__file__).resolve().parents[1] / "source"
SIZE = 64
GOLD = (233, 191, 103)
BLUE = (116, 169, 219)
RED = (213, 119, 102)
GRAY = (177, 178, 178)
GREEN = (130, 192, 166)


def waveform(color, heights):
    # Five bars represent speech in these project-authored developer icons.
    pixels = bytearray(bytes((37, 34, 33)) * SIZE * SIZE)
    for index, height in enumerate(heights):
        left = 9 + index * 10
        for y in range((SIZE - height) // 2, (SIZE + height) // 2):
            start = (y * SIZE + left) * 3
            pixels[start:start + 18] = bytes(color) * 6
    return pixels


def png(pixels):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))
    rows = b"".join(b"\x00" + pixels[i:i + SIZE * 3]
                    for i in range(0, len(pixels), SIZE * 3))
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def tga(pixels):
    bgr = bytearray(pixels)
    bgr[0::3], bgr[2::3] = pixels[2::3], pixels[0::3]
    return struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, SIZE, SIZE, 24, 32) + bgr


def main():
    addon = ROOT / "lite_addon" / "Icons"
    addon.mkdir(exist_ok=True)
    options = {
        "dmspeaks-mark": (GOLD, [12, 26, 42, 26, 12]),
        "minimap-waveform": (GOLD, [12, 26, 42, 26, 12]),
        "dmspeaks-framed": (GOLD, [12, 26, 42, 26, 12]),
        "fetch-status": (GREEN, [8, 16, 24, 32, 40]),
        "player-names": (GOLD, [24, 36, 16, 32, 20]),
        "alliance-quests-framed": (BLUE, [12, 20, 38, 20, 12]),
        "horde-quests-framed": (RED, [12, 20, 38, 20, 12]),
        "gossip-framed": (GREEN, [10, 32, 18, 28, 10]),
        "shared": (GRAY, [8, 26, 36, 26, 8]),
    }
    for name, (color, heights) in options.items():
        (addon / (name + ".tga")).write_bytes(tga(waveform(color, heights)))
    web = {"dmspeaks-mark": "dmspeaks-mark", "horde": "horde-quests-framed",
           "alliance": "alliance-quests-framed", "unknown": "shared"}
    for name, style in web.items():
        (ROOT / "lite_web" / (name + ".png")).write_bytes(png(waveform(*options[style])))


if __name__ == "__main__":
    main()
