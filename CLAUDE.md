# CLAUDE.md: tiga-loop

Open-source tools for the 320×172 LCD on the **Meletrix / Wuque Studio Zoom75 TIGA** keyboard:

| Tool | Path | Transport | Status |
|---|---|---|---|
| Tiga Loop (web) | `web/` | Web Bluetooth | Uploads verified on hardware (Chrome, macOS) |
| `tiga-image` (CLI) | `src/tiga_tool/image.py` | BLE via `bleak` | Verified: stills, 9/12/50-frame GIFs |
| `tiga-stats` (CLI) | `src/tiga_tool/stats.py` | USB HID via `hidapi` (cable or 2.4G receiver) | Verified: values show on the stats page, over both |

GPL-2.0 because it builds on MichiG21's GPL-2.0 work and ships their captured template. **Never add vendor code** (decompiled app source, firmware, bundled vendor assets) to this repo; only facts about formats belong here.

## Commands

```sh
pip install -e ".[test]"            # Python package + pytest
pytest -q                           # 18 Python tests (offline, no hardware)
node --test web/test/dlx.test.mjs   # 7 JS tests (offline)
python3 -m http.server -d web 8000  # web page; file:// won't work (ES modules are blocked)
tiga-image some.gif --preview       # writes preview.png/.gif, sends nothing
tiga-stats --dry-run --once         # prints a packet, sends nothing
tiga-stats -i --mock                # arrow keys change the rate; clock-driven test values
```

CI (`.github/workflows/tests.yml`) runs both suites; `pages.yml` deploys `web/*.{html,css,js}` to GitHub Pages on pushes to `main` (repo Settings → Pages → Source: GitHub Actions).

## Rules for working on this repo

- **Never send anything to the keyboard without the user asking in that moment.** A bad BLE upload can leave the screen stuck until the battery is physically disconnected.
- **Keep the Python and JS builders byte-identical.** `src/tiga_tool/protocol.py` and `web/dlx.js` must produce the same bytes; the tests pin both against the captured template and against each other (sha256 of a 9-frame stream). If you change one, change both and keep those tests green.
- **Don't change protocol constants** (CONTROL1/2, ACK bytes, preamble, record bytes) without evidence from a capture or the vendor app's behaviour. Everything that's there is either captured or reproduces the capture byte for byte.
- **Don't raise the 50-frame cap.** Both builders refuse more (`MAX_FRAMES`). It is the vendor app's maximum and is confirmed working; above it, an oversized write might overwrite the screen's built-in animations or firmware assets, which can't be restored (no public screen firmware).
- Copy for users follows sentence case, plain verbs, and says what to do next on failure.

## Hardware facts (verified)

- USB IDs: VID `0x1EA7`, PID `0xCEDD`. Raw HID interface: usage page `0xFF60`, usage `0x61` (the QMK/VIA raw HID interface).
- **2.4G receiver** ("Keyboard 2.4G dongle", manufacturer UBEST): VID `0x6D66`; PID `0x8572` here, `0x8888` reported by IwkyL/zoom75-tiga-signalrgb, so match by VID + usage page/usage, not PID. It exposes the same raw HID interface (interface 1) and forwards the `0x1C` vendor frames: time and SYSINFO were sent through it (behind a monitor hub) on 2026-11-14, got the same ACKs as over the cable, and the values showed on the stats page. The link carries about one packet per 80–100 ms (IwkyL). `tiga-stats` tries the cable first, then the receiver.
- The screen is a separate module with its own firmware (reports "screen version 14" here) and its own BLE peripheral named exactly **`Zoom75 Tiga`**. The typing Bluetooth slots advertise as `Zoom75 Tiga BT1/BT2/BT3` and do **not** have the media service — device selection must reject them.
- **Uploads need the keyboard on USB power** (a charger is enough). On battery it falls into deep sleep mid-transfer and the upload fails, even with few frames. The keyboard's Bluetooth must also be on while plugged in.
- Throughput ≈ 13–14 KB/s from macOS (both bleak and Chrome Web Bluetooth). A 9-frame upload ≈ 75 s; 50 frames ≈ 6½ min (1343 blocks, 395 s).
- Typing during an upload didn't break one test upload, but the UI still advises against it.
- There is **one custom slot**. An upload replaces it; nothing is added.
- The tiger/lion animation shown during uploads (and as a fallback when the custom slot is invalid) lives in the screen firmware. There is no read-back command, so the current custom image cannot be backed up.
- Keyboard shortcuts: `Fn+Delete` screen on/off, `Fn+PgUp/PgDn` change page, `Fn+RShift+Delete` keypress overlay on/off, `Fn+Space` wired mode (only with the cable in), `Fn+Z/X/C` Bluetooth 1/2/3 and `Fn+V` 2.4G (short press switches, hold pairs), `Fn+Enter` sub-page, `Fn+RShift+Enter` back, `Fn+Backspace` battery level on the number keys. Source for these: the official "ZOOM75 TIGA Operation Instructions" PDF, which labels `Fn+RShift+Delete` as "LCD On/Off" where this keyboard toggles the keypress overlay.

## USB HID protocol (stats, clock)

32-byte output reports (prefix a 0x00 report ID when writing with hidapi):

```
[0]=0x1C [1]=transport (0x02; time uses 0x03) [5]=len  [6..7]=CRC16 (LE)
[8]=0xA5 [9]=cmd [10]=0x00 [11]=payload len  [12..]=payload  then sum(bytes 9..)^0xFF
```

- CRC: CRC-16/CCITT-FALSE (init 0xFFFF, poly 0x1021) over all 32 bytes with bytes 6–7 zeroed, stored little-endian.
- `0xFF` SYSINFO (telemetry): `len=0x10`, payload `0x0B` bytes = flag(0), CPU °C, GPU °C, unknown, fan RPM, download Mbps×10 (all u16 BE), then a constant `0xFF` (no checksum byte). The screen ACKs with `1c 02 … a5 ff 00 02 fe 00`. Verified live: `sysinfo_packet(42, 37, rpm=1234, mbps=56.7)` showed exactly those values.
- `0x38` time: payload `00 01 YYYY(BE) MM DD hh mm ss weekday(0=Sun)`, transport `0x03`.
- `0x39` navigation: payload `00 <sub>`; sub 1=up, 2=down, 3=select, 4=back.
- Screen info query `a5 fb 00 01 00 ff` returns version + MAC (useful as a liveness check). The keyboard also emits a `1c 01 …` transport ACK before each real reply.
- Only one program can hold the HID interface: close the Meletrix web console (zoom75tiga.meletrix.com) before running `tiga-stats`.
- `tiga-stats` waits for the screen's ACK (`a5 <cmd> 00 02 fe 00`) after every packet (`--timeout`, default 2 s). A missing ACK or a failed write closes the device and retries with a doubling pause (1 s to 60 s, `--retries`, default 10), reopening the device each time; `tests/test_stats.py` covers this with a fake HID device. Untested on hardware: Mac sleep/wake with the receiver behind a monitor hub, and whether the screen still ACKs while it is switched off (`Fn+Delete`). If it doesn't, tiga-stats will give up while the screen is off.

## BLE media protocol (images, GIFs)

Service `1f40eaf8-aab4-14a3-f1ba-f61f35cddbaa`; characteristics `1f400001…` control (write), `…0002` notify, `…0003` bulk (write without response), `…0004` notify2. Subscribe to both notify characteristics.

1. Write **CONTROL1** (24 bytes; bytes 17–20 = DLX file length, u32 BE) → wait for both C1 ACKs (order-insensitive). Failure here changes nothing.
2. Write **CONTROL2** → both C2 ACKs. **Point of no return:** the screen now shows its upload animation.
3. Bulk writes: 181 data bytes + 1 XOR byte, never crossing a 4096-byte boundary of the DLX file (offsets measured from stream byte 25). After every full 4 KB block, wait for `PROGRESS` (`88 00 00 00 00 00 03 0b 08 06 05`).
4. Wait for `COMPLETE` (`… 03 0c 08 06 02`).

Writes need ≥182 bytes per write-without-response. bleak exposes `max_write_without_response_size` and the CLI checks it; Web Bluetooth can't check it, but Chrome on macOS works. Default pacing 2 ms between writes; `--pace-ms 5` if uploads stall.

### Stream / DLX container (as Pocket Wuque builds it)

```
envelope   88 00 00 00 <u24 BE body length> 00
preamble   08 05 01 00 09 00 ff ff ff 00 ff ff ff 02 02 ff ff   (constant; "02 02" is not a frame count)
DLX header 00 44 4c 58 fc ff 00 00 | 0x0160 | 3 (records) | N (frames) | 00 01 | zero-pad to 28 | 324 × ff
table      u32 LE: 368 (records), 452 (frame index), pixel start, pixel end
records    3 × 28 bytes; record 2 = 14 03 00 01 <11-speed> 00 00 <N> … ; all carry 320×172 (01 40 00 ac)
index      N × u32 LE offsets (k × 110080) from pixel start
frames     N × 320×172 RGB565 **big-endian** (110,080 bytes each)
trailer    fc ff 00 00
```

- Speed is the app's 1–10 slider; the stored byte is `11 − speed`. Default 5. The real time unit per step is unknown; previews assume 15 ms per step and say "approximate".
- A still is sent as two identical frames at speed 1. That is exactly the shape of the captured known-good upload (`src/tiga_tool/template/known_good_2frame.bin`, 220,649 bytes, 1,239 writes, 53 blocks), and `build_animation(captured frames, speed=1)` reproduces it byte for byte.
- The vendor app caps frames at 50 on one device type and ~9 on others (the TIGA's type wasn't identified, but 50 works). It center-crops frames to 320×172 and thins long GIFs to evenly spaced frames; we do the same via `pickEven` / `pick`.

## Dead ends (don't repeat these)

- **The Meletrix web console's hidden GIF uploader** (`zoom75tiga.meletrix.com`, card styled `display:none`): it crashes in libgif (a detached `<img>`), and even when patched, the screen ignores its `0xFC` USB upload command — no ACKs. Upstream reports the same faithful rebuild crashed the LCD. It's legacy code for an older format (452-byte header, no DLX prefix).
- **MeletrixID for macOS (v3.1, Aug 2024)** predates the TIGA and only matches PIDs `0x2BA7`/`0x287F`, hence its empty panel. The Windows build (3.3.5) supports the TIGA.
- **Stats over Bluetooth**: Pocket Wuque only *reads* PC stats from the screen over BLE (payload `00 17`, reply command `0x18`: CPU/GPU/disk temps in tenths, fan speed, battery). It has no BLE command that writes them; its BLE setters are time, weather, notes, alarms, notifications and screen style. Wireless stats go through the 2.4G receiver instead.
- **Pocket Wuque on a phone** often can't find the screen. The tools here replace it.
- **Recovering the factory tiger**: no read-back command; the APK's bundled `gif_dial.bin` is an unrelated 2-frame sample, not the tiger; the keyboard firmware hex is far too small to hold animations. Faking a partial upload only shows the tiger temporarily and risks a stuck screen. Untested ideas: the console's "Reset screen" (`0x34` command), or re-uploading a phone recording.

## Known pitfalls in this code

- Chrome `ImageDecoder`: await `decoder.tracks.ready` before reading `selectedTrack.frameCount` (it's `null` until then), then `decoder.completed`.
- `pickScreen()` (Web Bluetooth `requestDevice`) must be called directly from the click handler, before any heavy work, or Chrome rejects it for lacking a user gesture.
- `macmon` occasionally reports bogus low CPU temps (e.g. 40 → 16 °C for a sample). `sensors.Smoothed` drops readings outside 20–110 °C and reports the median of the last 5.
- Fan RPM isn't readable without root on Apple Silicon; it's sent as 0 unless `--rpm` is given.
- Users on conda Python hit `ModuleNotFoundError: bleak` when they skip `pip install` for that interpreter.

## Open questions

- Milliseconds per speed step (compare a known GIF on the screen vs the preview).
- Meaning of the SYSINFO "unknown" field (sent as 0).
- Whether "Reset screen" restores the factory animation.
- Whether a constant stats feed over 2.4G keeps the keyboard awake and how much battery it costs.
- Battery cost of each `tiga-stats` rate preset (eco 5 s, everyday 3 s, fast 1 s, realtime 0.5 s); they were picked by eye over 2.4G with `-i --mock`.
- Windows/Linux BLE behaviour (untested; bleak supports both).

## Before publishing

- The repo is `PlaintextPirate/tiga-loop`; the page is https://plaintextpirate.github.io/tiga-loop/. Commits use the account's GitHub no-reply address (set in this repo's local git config), never a personal name or email.
- Enable Pages with source "GitHub Actions" so `pages.yml` can deploy.

## Credits / sources

- MichiG21/tiga-zoom75-windows-media-display (GPL-2.0): BLE protocol, captured template, SYSINFO layout.
- Pocket Wuque Android app V1.1.3 (`com.app.smartkeyboard`), studied for interoperability: multi-frame layout, speed byte, frame limits, constant preamble.
- Meletrix web configurator: USB framing, CRC, time/nav/theme commands.
- IwkyL/zoom75-tiga-signalrgb: the 2.4G receiver carries the raw HID interface (VIA and Console commands pass through on stock firmware).
