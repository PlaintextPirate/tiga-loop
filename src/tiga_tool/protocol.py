"""Zoom75 TIGA screen protocol: USB HID packets (stats, time) and the BLE media stream.

Sources:
- USB framing and command bytes: Meletrix's own web console (zoom75tiga.meletrix.com) and
  MichiG21/tiga-zoom75-windows-media-display (hardware-verified SYSINFO probe).
- BLE media stream (DLX container, CONTROL1/2, PROGRESS/COMPLETE): MichiG21's capture of a
  working PocketWuque upload. `build_animation` reproduces that capture byte for byte with
  two frames, and uses the same layout for up to MAX_FRAMES frames.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- USB HID ----
VENDOR_ID = 0x1EA7
PRODUCT_ID = 0xCEDD
USAGE_PAGE = 0xFF60
USAGE = 0x61
# The 2.4G receiver ("Keyboard 2.4G dongle") exposes the same raw HID interface. Its product
# ID varies (0x8572 and 0x8888 have been seen), so it is matched by vendor ID and usage only.
RECEIVER_VENDOR_ID = 0x6D66


def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def _finish(raw: bytearray) -> bytes:
    """Fill the CRC16 (little-endian, bytes 6-7) computed with those bytes zeroed."""
    raw[6] = raw[7] = 0
    crc = crc16_ccitt_false(raw)
    raw[6], raw[7] = crc & 0xFF, crc >> 8
    return bytes(raw)


def _u16(v: float) -> int:
    return max(0, min(0xFFFF, int(round(v))))


def sysinfo_packet(cpu_c: float, gpu_c: float, rpm: float = 0, mbps: float = 0,
                   unknown: int = 0) -> bytes:
    """0xFF SYSINFO telemetry: temps in °C, fan RPM, download in Mbps (sent as tenths).

    Layout (all big-endian): flag, CPU, GPU, unknown, RPM, Mbps*10, then a constant 0xFF.
    Verified on hardware: the screen answers `1c 02 .. a5 ff 00 02 fe 00`.
    """
    r = bytearray(32)
    r[0], r[1], r[5] = 0x1C, 0x02, 0x10
    r[8], r[9], r[10], r[11] = 0xA5, 0xFF, 0x00, 0x0B
    r[12] = 0
    struct.pack_into(">HHHHH", r, 13, _u16(cpu_c), _u16(gpu_c), _u16(unknown),
                     _u16(rpm), _u16(mbps * 10))
    r[23] = 0xFF
    return _finish(r)


def _command_packet(cmd: int, payload: bytes, transport: int = 0x02) -> bytes:
    """Console-style command: a5 <cmd> 00 <len> <payload> <sum XOR ff>."""
    r = bytearray(32)
    r[8], r[9], r[10], r[11] = 0xA5, cmd, 0x00, len(payload)
    r[12:12 + len(payload)] = payload
    r[12 + len(payload)] = (sum(r[9:]) ^ 0xFF) & 0xFF
    r[0], r[1], r[5] = 0x1C, transport, 4 + len(payload) + 1
    return _finish(r)


def time_packet(now: datetime | None = None) -> bytes:
    """0x38 date/time, byte-identical to the console's 'Update time' button."""
    n = now or datetime.now()
    payload = bytes([0x00, 0x01, n.year >> 8 & 0xFF, n.year & 0xFF, n.month, n.day,
                     n.hour, n.minute, n.second, (n.isoweekday() % 7)])
    return _command_packet(0x38, payload, transport=0x03)


def screen_nav_packet(action: str) -> bytes:
    """0x39 screen navigation, as the console's buttons send it."""
    sub = {"up": 1, "select": 3, "back": 4, "down": 2}[action]
    return _command_packet(0x39, bytes([0x00, sub]))


# -------------------------------------------------------------- BLE media ----
BLE_NAME = "Zoom75 Tiga"
SERVICE_MEDIA = "1f40eaf8-aab4-14a3-f1ba-f61f35cddbaa"
CHAR_CONTROL = "1f400001-aab4-14a3-f1ba-f61f35cddbaa"
CHAR_NOTIFY = "1f400002-aab4-14a3-f1ba-f61f35cddbaa"
CHAR_BULK = "1f400003-aab4-14a3-f1ba-f61f35cddbaa"
CHAR_NOTIFY2 = "1f400004-aab4-14a3-f1ba-f61f35cddbaa"

WIDTH, HEIGHT = 320, 172
FRAME_BYTES = WIDTH * HEIGHT * 2
MAX_FRAMES = 50                      # the vendor app's maximum; larger writes are untested
FILE_START = 25                     # 8-byte envelope + 17-byte preamble
FRAME1 = FILE_START + 8 + 452        # pixel data start in the stream
FRAME2 = FRAME1 + FRAME_BYTES
STREAM_LEN = 220_649
BULK_WRITES = 1_239
BLOCKS = 53

CONTROL1 = bytes([0x88, 0, 0, 0, 0, 0, 0x10, 0, 0x09, 0x03, 0x04, 0, 0x08, 0, 0, 0xFF,
                  0xFC, 0, 0x03, 0x5D, 0xD0, 0x05, 0, 0])
CONTROL2 = bytes([0x88, 0, 0, 0, 0, 0, 0x0A, 0, 0x08, 0x03, 0, 0xFF, 0xFF, 0xFF, 0,
                  0xFF, 0xFF, 0xFF])
C1_ACKS = (bytes.fromhex("88 00 00 00 00 00 05 0e 07 02 01 09 03"),
           bytes.fromhex("88 00 00 00 00 00 03 0f 09 04 02"))
C2_ACKS = (bytes.fromhex("88 00 00 00 00 00 05 0f 07 02 01 08 03"),
           bytes.fromhex("88 00 00 00 00 00 03 0e 08 04 02"))
PROGRESS = bytes.fromhex("88 00 00 00 00 00 03 0b 08 06 05")
COMPLETE = bytes.fromhex("88 00 00 00 00 00 03 0c 08 06 02")

TEMPLATE = Path(__file__).resolve().parent / "template" / "known_good_2frame.bin"
TEMPLATE_SHA256_PREFIX = "a4be0a5d"


def validate_stream(s: bytes, any_length: bool = False) -> None:
    if not any_length and len(s) != STREAM_LEN:
        raise ValueError(f"stream is {len(s)} bytes, expected {STREAM_LEN}")
    if s[:4] != b"\x88\x00\x00\x00" or s[7] != 0 or int.from_bytes(s[4:7], "big") != len(s) - 8:
        raise ValueError("outer envelope mismatch")
    if s[8:25] != bytes.fromhex("08 05 01 00 09 00 ff ff ff 00 ff ff ff 02 02 ff ff"):
        raise ValueError("upload preamble mismatch")
    if s[25:33] != bytes.fromhex("00 44 4c 58 fc ff 00 00"):
        raise ValueError("DLX prefix mismatch")
    if s[-4:] != bytes.fromhex("fc ff 00 00"):
        raise ValueError("DLX trailer mismatch")


def build_stream(frame: bytes, template: bytes | None = None, frame2: bytes | None = None) -> bytes:
    """Two-frame container (the captured known-good shape). Kept for the tests."""
    tpl = template if template is not None else TEMPLATE.read_bytes()
    validate_stream(tpl)
    s = bytearray(tpl)
    s[FRAME1:FRAME1 + FRAME_BYTES] = frame
    s[FRAME2:FRAME2 + FRAME_BYTES] = frame if frame2 is None else frame2
    validate_stream(s)
    return bytes(s)


def build_animation(frames: list[bytes], speed: int = 5) -> bytes:
    """Multi-frame DLX container, laid out exactly as Pocket Wuque builds it
    (KeyBoardConstant.getGifAArrayData / dealWidthBData, app V1.1.3):

      28-byte header (\\0DLX fc ff 00 00, 0x0160, 3 records, frame count, 00 01) + 324 x ff
      section table: records @368, frame index @452, pixels, end   (u32 LE)
      3 x 28-byte records; record 2 = animation: delay byte (11 - speed), frame count
      frame index: one u32 LE offset per frame;  frames: RGB565 BE;  trailer fc ff 00 00

    With two frames and speed 1 this reproduces the captured known-good upload byte for byte.
    """
    n = len(frames)
    if not 1 <= n <= MAX_FRAMES:
        raise ValueError(f"1-{MAX_FRAMES} frames")
    if any(len(f) != FRAME_BYTES for f in frames):
        raise ValueError(f"every frame must be {FRAME_BYTES} bytes of RGB565 big-endian")
    if not 1 <= speed <= 10:
        raise ValueError("speed must be 1-10")

    head = bytearray(b"\x00DLX\xfc\xff\x00\x00") + struct.pack("<HHH", 352, 3, n) + b"\x00\x01"
    head += bytes(28 - len(head)) + b"\xff" * 324
    wh = bytes([0, 0, 0x01, 0x40, 0x00, 0xAC]) + bytes(14)              # 320 x 172
    records = (bytes([0x15, 1, 0, 0, 0, 0, 0, 1]) + wh +
               bytes([0x14, 3, 0, 1, 11 - speed, 0, 0, n]) + wh +
               bytes([0x14, 4, 0, 0, 0, 0, 0, 1]) + wh)                 # rec 3 as captured
    index = b"".join(struct.pack("<I", k * FRAME_BYTES) for k in range(n))
    pix_at = 368 + len(records) + len(index)
    table = struct.pack("<4I", 368, 368 + len(records), pix_at, pix_at + n * FRAME_BYTES)
    dlx = bytes(head) + table + records + index + b"".join(frames) + b"\xfc\xff\x00\x00"

    body = bytes.fromhex("08 05 01 00 09 00 ff ff ff 00 ff ff ff 02 02 ff ff") + dlx
    stream = b"\x88\x00\x00\x00" + len(body).to_bytes(3, "big") + b"\x00" + body
    validate_stream(stream, any_length=True)
    return stream


def control1_for(stream: bytes) -> bytes:
    """CONTROL1 announces the DLX file length (u32 BE, bytes 17-20)."""
    c = bytearray(CONTROL1)
    c[17:21] = (len(stream) - FILE_START).to_bytes(4, "big")
    return bytes(c)


@dataclass
class Chunk:
    value: bytes          # 181 (or fewer) data bytes + 1 XOR checksum
    ends_block: bool      # the keyboard sends PROGRESS after this write


def xor8(b: bytes) -> int:
    x = 0
    for v in b:
        x ^= v
    return x


def chunk_stream(s: bytes) -> list[Chunk]:
    """Cut into BLE writes exactly as PocketWuque does: 181 data bytes + XOR, never
    crossing a 4096-byte boundary of the DLX file."""
    validate_stream(s, any_length=True)
    out, off = [], 0
    while off < len(s):
        n = min(181, len(s) - off)
        if off >= FILE_START:
            n = min(n, 4096 - ((off - FILE_START) % 4096))
        data = s[off:off + n]
        off += n
        out.append(Chunk(data + bytes([xor8(data)]),
                         off >= FILE_START and (off - FILE_START) % 4096 == 0))
    if len(s) == STREAM_LEN and (len(out) != BULK_WRITES or sum(c.ends_block for c in out) != BLOCKS):
        raise ValueError("chunking does not match the captured upload")
    return out
