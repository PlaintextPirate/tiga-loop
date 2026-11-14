"""Put pictures and animated GIFs on the Zoom75 TIGA screen over Bluetooth.

    tiga-image anim.gif --preview      # write preview.png / preview.gif, send nothing
    tiga-image anim.gif                # animate (up to 50 frames, speed 5)
    tiga-image anim.gif --speed 8 --max-frames 9    # fewer frames, faster upload
    tiga-image photo.jpg               # a still picture
    tiga-image a.png b.png c.png       # your own frames, in order
    tiga-image anim.gif --still        # just one frame of a GIF

The container is built exactly as Pocket Wuque builds it (frame count, per-frame index,
speed byte = 11 - speed), checked byte-for-byte against a captured working upload.

If an upload fails AFTER the keyboard shows its upload (tiger) animation, the screen
may be stuck until you power it off fully: open the case, disconnect the battery for at
least 30 s, reconnect. Unplugging USB is not enough. See the README (Recovery).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from PIL import Image, ImageOps

from .protocol import (BLE_NAME, C1_ACKS, C2_ACKS, CHAR_BULK, CHAR_CONTROL,
                           CHAR_NOTIFY, CHAR_NOTIFY2, COMPLETE, CONTROL2, HEIGHT, MAX_FRAMES,
                           PROGRESS, SERVICE_MEDIA, WIDTH, build_animation, chunk_stream,
                           control1_for)

MS_PER_SPEED_STEP = 15    # preview only, same as the web page; the screen's real unit is unknown


# ------------------------------------------------------------ image -> frame --
def load_frame(path: Path, fit: str, frame: int, bg: str) -> Image.Image:
    img = Image.open(path)
    if getattr(img, "n_frames", 1) > 1:
        img.seek(min(frame, img.n_frames - 1))
    img = ImageOps.exif_transpose(img).convert("RGBA")
    if fit == "cover":
        img = ImageOps.fit(img, (WIDTH, HEIGHT), Image.LANCZOS)
    elif fit == "contain":
        img = ImageOps.contain(img, (WIDTH, HEIGHT), Image.LANCZOS)
    else:
        img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), bg)
    canvas.alpha_composite(img, ((WIDTH - img.width) // 2, (HEIGHT - img.height) // 2))
    return canvas.convert("RGB")


def rgb565_be(img: Image.Image) -> bytes:
    px = img.convert("RGB").tobytes()
    out = bytearray(len(px) // 3 * 2)
    for i in range(0, len(px) // 3):
        r, g, b = px[3 * i], px[3 * i + 1], px[3 * i + 2]
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[2 * i], out[2 * i + 1] = v >> 8, v & 0xFF
    return bytes(out)


def rgb565_preview(frame: bytes) -> Image.Image:
    """What the screen will actually show (16-bit colour)."""
    px = []
    for i in range(0, len(frame), 2):
        v = frame[i] << 8 | frame[i + 1]
        r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
        px.append((r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2))
    im = Image.new("RGB", (WIDTH, HEIGHT))
    im.putdata(px)
    return im


# ---------------------------------------------------------------- BLE upload --
class Failed(Exception):
    def __init__(self, msg: str, after_control2: bool):
        super().__init__(msg)
        self.after_control2 = after_control2


async def find_display(timeout: float):
    from bleak import BleakScanner
    print(f"Scanning for '{BLE_NAME}' ({timeout:.0f} s)...")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    exact, other = [], []
    for dev, adv in found.values():
        name = adv.local_name or dev.name or ""
        if name == BLE_NAME:
            exact.append(dev)
        elif "zoom75" in name.lower() and " bt" not in name.lower():
            other.append(dev)        # skip "Zoom75 Tiga BT1/2/3" typing slots
    return exact + other


async def upload(stream: bytes, scan_s: float, pace_ms: float, block_timeout: float):
    from bleak import BleakClient

    chunks = chunk_stream(stream)
    control1 = control1_for(stream)
    candidates = await find_display(scan_s)
    if not candidates:
        raise Failed("screen not found over Bluetooth. Make sure the keyboard's Bluetooth is on, "
                     "and that no phone (Pocket Wuque) is connected to it.", False)

    for dev in candidates:
        print(f"Connecting to {dev.name} ({dev.address})...")
        try:
            client = BleakClient(dev, timeout=15)
            await client.connect()
        except Exception as e:  # noqa: BLE001
            print(f"  could not connect: {e}")
            continue
        try:
            if client.services.get_service(SERVICE_MEDIA) is None:
                print("  no media service here, trying the next device")
                continue
            bulk = client.services.get_characteristic(CHAR_BULK)
            room = getattr(bulk, "max_write_without_response_size", 182)
            if room < 182:
                raise Failed(f"Bluetooth link only allows {room}-byte writes; need 182. Nothing was sent.", False)
            return await _run_upload(client, chunks, control1, pace_ms, block_timeout)
        finally:
            await client.disconnect()
    raise Failed("found the keyboard but none of its Bluetooth devices offered the media service.", False)


async def _run_upload(client, chunks, control1, pace_ms, block_timeout):
    q: asyncio.Queue[bytes] = asyncio.Queue()
    handler = lambda _c, data: q.put_nowait(bytes(data))  # noqa: E731
    await client.start_notify(CHAR_NOTIFY, handler)
    await client.start_notify(CHAR_NOTIFY2, handler)

    async def wait_for(expected: set[bytes], timeout: float, what: str):
        need, deadline = set(expected), time.monotonic() + timeout
        while need:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no answer while {what}")
            try:
                need.discard(await asyncio.wait_for(q.get(), left))
            except asyncio.TimeoutError:
                raise TimeoutError(f"no answer while {what}") from None

    await asyncio.sleep(0.15)
    while not q.empty():
        q.get_nowait()                                      # stale status messages

    try:
        await client.write_gatt_char(CHAR_CONTROL, control1, response=False)
        await wait_for(set(C1_ACKS), 1.5, "waiting for CONTROL1")
    except TimeoutError as e:
        raise Failed(f"{e}. Nothing was changed on the screen.", False) from None
    await asyncio.sleep(0.06)

    # ---- point of no return: the keyboard now shows its upload animation ----
    try:
        await client.write_gatt_char(CHAR_CONTROL, CONTROL2, response=False)
        await wait_for(set(C2_ACKS), 1.5, "waiting for CONTROL2")
        await asyncio.sleep(0.06)
        total = sum(c.ends_block for c in chunks)
        block, started = 0, time.monotonic()
        for c in chunks:
            await client.write_gatt_char(CHAR_BULK, c.value, response=False)
            if pace_ms:
                await asyncio.sleep(pace_ms / 1000)
            if c.ends_block:
                await wait_for({PROGRESS}, block_timeout, f"waiting for block {block + 1}/{total}")
                block += 1
                print(f"\r  block {block:2}/{total}  {time.monotonic() - started:5.1f} s", end="", flush=True)
        print()
        await wait_for({COMPLETE}, 8 + total * 0.1, "waiting for the final completion")
    except Exception as e:  # noqa: BLE001
        raise Failed(str(e), True) from None
    finally:
        for ch in (CHAR_NOTIFY, CHAR_NOTIFY2):
            try:
                await client.stop_notify(ch)
            except Exception:  # noqa: BLE001
                pass
    return time.monotonic() - started


# ----------------------------------------------------------------------- CLI --
def gif_frames(path: Path) -> list[int]:
    return list(range(getattr(Image.open(path), "n_frames", 1)))


def pick(indices: list[int], limit: int) -> list[int]:
    """Evenly spaced subset, like the app does for long GIFs."""
    if len(indices) <= limit:
        return indices
    step = len(indices) / limit
    return [indices[int(k * step)] for k in range(limit)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", type=Path, nargs="+", help="a GIF, a picture, or several pictures as frames")
    ap.add_argument("--fit", choices=("cover", "contain", "stretch"), default="cover",
                    help="cover = fill and crop (default), contain = letterbox, stretch = distort")
    ap.add_argument("--background", default="black", help="letterbox colour for --fit contain")
    ap.add_argument("--speed", type=int, default=5, choices=range(1, 11), metavar="1-10",
                    help="animation speed as in Pocket Wuque (default 5)")
    ap.add_argument("--max-frames", type=int, default=MAX_FRAMES,
                    help=f"frames to keep from a long GIF (default and maximum {MAX_FRAMES}; "
                         "fewer frames upload faster, about 8 s each)")
    ap.add_argument("--still", action="store_true", help="send only one frame (see --frame)")
    ap.add_argument("--frame", type=int, default=0, help="which GIF frame --still uses (default 0)")
    ap.add_argument("--preview", action="store_true", help="write preview.png (+ preview.gif) and exit")
    ap.add_argument("--save-stream", type=Path, help="also write the upload stream to this file")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--scan", type=float, default=8, help="Bluetooth scan seconds")
    ap.add_argument("--pace-ms", type=float, default=2, help="pause between writes (raise if uploads stall)")
    ap.add_argument("--block-timeout", type=float, default=2.5, help="seconds to wait for each 4 KB ACK")
    args = ap.parse_args()

    if not 1 <= args.max_frames <= MAX_FRAMES:
        sys.exit(f"--max-frames must be 1-{MAX_FRAMES}")

    # (path, frame index) for every frame to send
    if len(args.images) > 1:
        keep = pick(list(range(len(args.images))), args.max_frames)
        if len(keep) < len(args.images):
            print(f"Keeping {len(keep)} of {len(args.images)} pictures, evenly spaced. "
                  f"Use --max-frames (up to {MAX_FRAMES}) to keep more.")
        sources = [(args.images[i], 0) for i in keep]
    elif args.still:
        sources = [(args.images[0], args.frame)]
    else:
        sources = [(args.images[0], i) for i in pick(gif_frames(args.images[0]), args.max_frames)]

    imgs = [load_frame(p, args.fit, i, args.background) for p, i in sources]
    frames = [rgb565_be(im) for im in imgs]
    if len(frames) == 1:
        frames = frames * 2        # the known-good still layout: same picture twice
    stream = build_animation(frames, speed=args.speed if len(sources) > 1 else 1)
    chunks = chunk_stream(stream)  # validates before anything is sent

    shots = [rgb565_preview(f) for f in frames[: len(sources)]]
    preview = Path("preview.png")
    cols = min(len(shots), 5)
    rows = (len(shots) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (WIDTH + 6) - 6, rows * (HEIGHT + 6) - 6), "white")
    for k, im in enumerate(shots):
        sheet.paste(im, ((k % cols) * (WIDTH + 6), (k // cols) * (HEIGHT + 6)))
    sheet.save(preview)
    if len(shots) > 1:
        # GIF viewers treat delays under 20 ms as 100 ms, so the fastest speeds are clamped.
        delay = max(20, MS_PER_SPEED_STEP * (11 - args.speed))
        shots[0].save("preview.gif", save_all=True, append_images=shots[1:], duration=delay, loop=0)
    kb = len(stream) / 1024
    print(f"Prepared {len(sources)} frame(s) -> {kb:.0f} KB, {len(chunks)} writes. Preview: {preview.resolve()}")
    if args.save_stream:
        args.save_stream.write_bytes(stream)
    if args.preview:
        return

    if not args.yes:
        print("\nThe keyboard must be on USB power (a cable, even charger-only): on battery it\n"
              "falls asleep mid-upload and the transfer fails.")
        print(f"Upload takes roughly {max(15, kb / 13):.0f} s. Avoid typing on the keyboard meanwhile.")
        print("If it fails after the tiger animation appears, see the README (battery reset).")
        try:
            answer = input("Upload now? [y/N] ")
        except (KeyboardInterrupt, EOFError):
            answer = ""
            print()
        if answer.strip().lower() != "y":
            print("Cancelled.")
            return
    try:
        secs = asyncio.run(upload(stream, args.scan, args.pace_ms, args.block_timeout))
    except KeyboardInterrupt:
        print("\nUpload interrupted. If the tiger animation had not appeared yet, nothing changed.\n"
              "If it had, the screen may be stuck: wait 2 minutes and retry once; if it still\n"
              "fails, do the battery reset described in the README before trying again.", file=sys.stderr)
        sys.exit(130)
    except Failed as f:
        print(f"\nUpload failed: {f}", file=sys.stderr)
        if f.after_control2:
            print("The screen may show a white or stuck image. Wait 2 minutes and retry once; if it\n"
                  "still fails, do the battery reset described in the README before trying again.", file=sys.stderr)
        sys.exit(1)
    print(f"Done in {secs:.1f} s.")


if __name__ == "__main__":
    main()
