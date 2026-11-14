import hashlib
from datetime import datetime

from PIL import Image

from tiga_tool.protocol import (BLOCKS, BULK_WRITES, FRAME1, FRAME2, FRAME_BYTES, TEMPLATE,
                           TEMPLATE_SHA256_PREFIX, build_stream, chunk_stream,
                           crc16_ccitt_false, screen_nav_packet, sysinfo_packet,
                           time_packet, validate_stream)
from tiga_tool.image import rgb565_be, rgb565_preview


def test_crc_matches_packet_captured_from_the_console():
    # The console's first image packet, captured live on 2026-11-13 (CRC = bytes 6-7, LE).
    raw = bytearray.fromhex("1c 02 00 00 00 18 bf 93 a5 fc 00 13" + " 00" * 19 + " f0")
    assert len(raw) == 32
    raw[6] = raw[7] = 0
    assert crc16_ccitt_false(raw) == 0x93BF


def test_sysinfo_matches_packet_the_keyboard_acknowledged():
    # Sent through WebHID on 2026-11-13; the screen answered a5 ff 00 02 fe 00 each time.
    acked = bytes.fromhex("1c 02 00 00 00 10 3e 7d a5 ff 00 0b 00 00 2a 00 25 00 00 04 d2 02 37 ff"
                          + " 00" * 8)
    assert sysinfo_packet(42, 37, rpm=1234, mbps=56.7) == acked


def test_sysinfo_clamps():
    p = sysinfo_packet(-5, 999999, rpm=-1, mbps=1e9)
    assert p[13:15] == b"\x00\x00" and p[15:17] == b"\xff\xff" and p[21:23] == b"\xff\xff"


def test_time_packet_layout_matches_console():
    p = time_packet(datetime(2026, 11, 14, 0, 5, 9))   # a Saturday
    assert p[0:2] == b"\x1c\x03" and p[5] == 15
    assert p[8:12] == bytes([0xA5, 0x38, 0x00, 10])
    assert p[12:22] == bytes([0, 1, 0x07, 0xEA, 11, 14, 0, 5, 9, 6])
    assert p[22] == (sum(p[9:22]) ^ 0xFF) & 0xFF
    raw = bytearray(p); raw[6] = raw[7] = 0
    assert crc16_ccitt_false(raw) == p[6] | p[7] << 8


def test_nav_packet_layout():
    p = screen_nav_packet("up")
    assert p[8:15] == bytes([0xA5, 0x39, 0x00, 0x02, 0x00, 0x01, (0x39 + 2 + 1) ^ 0xFF]) and p[5] == 7


def test_template_is_the_known_good_capture():
    t = TEMPLATE.read_bytes()
    assert hashlib.sha256(t).hexdigest().startswith(TEMPLATE_SHA256_PREFIX)
    validate_stream(t)


def test_template_frames_rebuild_byte_exact():
    t = TEMPLATE.read_bytes()
    s = bytearray(build_stream(t[FRAME1:FRAME1 + FRAME_BYTES], t))
    s[FRAME2:FRAME2 + FRAME_BYTES] = t[FRAME2:FRAME2 + FRAME_BYTES]
    assert bytes(s) == t


def test_chunking_matches_pocket_wuque():
    chunks = chunk_stream(TEMPLATE.read_bytes())
    assert len(chunks) == BULK_WRITES and sum(c.ends_block for c in chunks) == BLOCKS
    assert all(len(c.value) <= 182 for c in chunks)
    for c in chunks:
        x = 0
        for b in c.value[:-1]:
            x ^= b
        assert c.value[-1] == x


def test_image_stream_only_changes_pixels():
    img = Image.new("RGB", (320, 172), (255, 0, 0))
    frame = rgb565_be(img)
    assert frame[:2] == b"\xf8\x00"                     # red, big-endian
    s = build_stream(frame)
    t = TEMPLATE.read_bytes()
    assert s[:FRAME1] == t[:FRAME1] and s[FRAME2 + FRAME_BYTES:] == t[FRAME2 + FRAME_BYTES:]
    assert s[FRAME1:FRAME1 + FRAME_BYTES] == frame == s[FRAME2:FRAME2 + FRAME_BYTES]
    assert rgb565_preview(frame).getpixel((0, 0)) == (255, 0, 0)


def test_two_frame_stream_keeps_known_good_layout():
    a = rgb565_be(Image.new("RGB", (320, 172), (255, 0, 0)))
    b = rgb565_be(Image.new("RGB", (320, 172), (0, 255, 0)))
    s = build_stream(a, frame2=b)
    t = TEMPLATE.read_bytes()
    # Same bytes as the captured red/green upload apart from the exact shades.
    assert s[:FRAME1] == t[:FRAME1] and s[FRAME2 + FRAME_BYTES:] == t[FRAME2 + FRAME_BYTES:]
    assert s[FRAME1:FRAME1 + 2] == b"\xf8\x00" and s[FRAME2:FRAME2 + 2] == b"\x07\xe0"
    assert t[FRAME1:FRAME1 + 2] == b"\xf8\x00" and t[FRAME2:FRAME2 + 2] == b"\x07\xe0"
    assert len(chunk_stream(s)) == BULK_WRITES


def test_app_layout_rebuilds_the_capture_byte_exact():
    from tiga_tool.protocol import build_animation
    t = TEMPLATE.read_bytes()
    frames = [t[FRAME1:FRAME1 + FRAME_BYTES], t[FRAME2:FRAME2 + FRAME_BYTES]]
    assert build_animation(frames, speed=1) == t


def test_animation_layout_scales_with_frames():
    import struct
    from tiga_tool.protocol import FILE_START, build_animation, control1_for, CONTROL1
    frames = [bytes([k]) * FRAME_BYTES for k in range(9)]
    s = build_animation(frames, speed=5)
    f = s[FILE_START:]
    assert struct.unpack_from("<H", f, 12)[0] == 9
    assert struct.unpack_from("<4I", f, 352) == (368, 452, 452 + 36, 488 + 9 * FRAME_BYTES)
    assert f[368 + 28 + 4] == 6 and f[368 + 28 + 7] == 9            # delay 11-5, count
    assert struct.unpack_from("<9I", f, 452) == tuple(k * FRAME_BYTES for k in range(9))
    assert f[488 + 3 * FRAME_BYTES:488 + 3 * FRAME_BYTES + 4] == b"\x03" * 4
    assert int.from_bytes(s[4:7], "big") == len(s) - 8
    c1 = control1_for(s)
    assert int.from_bytes(c1[17:21], "big") == len(f) and c1[:17] == CONTROL1[:17]
    assert control1_for(TEMPLATE.read_bytes()) == CONTROL1
    chunks = chunk_stream(s)
    assert sum(c.ends_block for c in chunks) == (len(f)) // 4096
    assert all(len(c.value) <= 182 for c in chunks)


def test_builder_refuses_more_than_the_confirmed_frame_limit():
    import pytest
    from tiga_tool.protocol import MAX_FRAMES, build_animation
    assert MAX_FRAMES == 50
    frame = bytes(FRAME_BYTES)
    build_animation([frame] * MAX_FRAMES)
    with pytest.raises(ValueError):
        build_animation([frame] * (MAX_FRAMES + 1))


def test_temperature_smoothing_ignores_glitches():
    from tiga_tool.sensors import Smoothed
    s = Smoothed()
    for v in (40, 41, 16, 40, 42, 0, 39):
        s.add(v)
    assert s.value == 40          # the 16 and 0 never count
    s2 = Smoothed()
    for v in (40, 41, 38, 25, 40):   # a plausible but brief dip is outvoted by the median
        s2.add(v)
    assert s2.value == 40
