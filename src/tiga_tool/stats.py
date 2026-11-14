"""Feed your computer's CPU/GPU temps and download speed to the Zoom75 TIGA screen
over USB: the cable, or the 2.4G receiver.

    tiga-stats              # sync the clock, then keep sending stats
    tiga-stats --rate eco   # eco (5 s), everyday (3 s), fast (1 s) or realtime (0.5 s)
    tiga-stats --dry-run    # print the packets, touch nothing
    tiga-stats --once       # one packet, then exit
    tiga-stats -i           # arrow keys change the update rate, T sets the clock

The values appear on the screen's built-in PC-stats page (Fn + PgUp/PgDn to reach it).
This uses only the telemetry and time commands; it never touches the custom image slot.
The clock is set at the start and then once an hour (--no-time turns that off).

If the keyboard stops answering (computer asleep, receiver unplugged, keyboard off), tiga-stats
retries with a growing pause (1, 2, 4 ... 60 s) and gives up after --retries attempts.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from .protocol import (PRODUCT_ID, RECEIVER_VENDOR_ID, USAGE, USAGE_PAGE, VENDOR_ID,
                           sysinfo_packet, time_packet)
from .sensors import NetSpeed, temperature_source

RATES = (0.5, 1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120)    # seconds, for --interactive
# Seconds between updates. Chosen by eye on a TIGA over the 2.4G receiver; the battery cost of
# each has not been measured.
PRESETS = {"eco": 5.0, "everyday": 3.0, "fast": 1.0, "realtime": 0.5}
PRESET_NAMES = {v: k for k, v in PRESETS.items()}

NOT_FOUND = ("Zoom75 TIGA not found. Plug in the cable (Fn+Space for wired mode) or the 2.4G "
             "receiver (Fn+V). Close the Meletrix web console tab if it is connected.")
GAVE_UP = ("No answer from the keyboard after {n} retries. Check that it is on and in wired mode "
           "(Fn+Space) or 2.4G mode (Fn+V), then run tiga-stats again.")


class LinkLost(Exception):
    """The keyboard stopped answering, or its USB device went away."""


class Backoff:
    """Retry pauses that double from START up to MAX, for a limited number of retries."""

    START, MAX = 1.0, 60.0

    def __init__(self, retries: int):
        self.retries, self.failures = retries, 0

    def fail(self) -> float | None:
        """Seconds to wait before the next retry, or None when the retries are used up."""
        self.failures += 1
        if self.failures > self.retries:
            return None
        return min(self.MAX, self.START * 2 ** (self.failures - 1))

    def ok(self) -> bool:
        """Reset after a success. True if that ended a run of failures."""
        recovered, self.failures = self.failures > 0, 0
        return recovered


def is_ack(reply: bytes, cmd: int) -> bool:
    """The screen's answer to command `cmd`: 1c .. a5 <cmd> 00 02 fe 00."""
    return len(reply) > 9 and reply[8] == 0xA5 and reply[9] == cmd


class Link:
    """The keyboard's raw HID interface: the cable first, then the 2.4G receiver.

    Every failure raises LinkLost, and the next write reopens the device, so a receiver
    that dropped off USB (computer asleep, hub powered down) is found again.
    """

    def __init__(self):
        self.dev = None
        self.wireless = False
        self.via = ""

    def open(self):
        import hid  # pip install hidapi
        self.close()
        for vid, pid, via in ((VENDOR_ID, PRODUCT_ID, "USB cable"), (RECEIVER_VENDOR_ID, 0, "2.4G receiver")):
            for d in hid.enumerate(vid, pid):
                if d.get("usage_page") == USAGE_PAGE and d.get("usage") == USAGE:
                    dev = hid.device()
                    try:
                        dev.open_path(d["path"])
                        dev.set_nonblocking(True)
                    except (OSError, ValueError) as e:
                        raise LinkLost(f"Couldn't open the {via} ({e})") from None
                    self.dev, self.wireless, self.via = dev, vid == RECEIVER_VENDOR_ID, via
                    return
        raise LinkLost("Keyboard not found on USB")

    def close(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except (OSError, ValueError):
                pass
            self.dev = None

    def write(self, raw: bytes):
        if self.dev is None:
            self.open()
        try:
            n = self.dev.write(b"\x00" + raw)         # leading 0x00 = report ID
        except (OSError, ValueError) as e:
            raise LinkLost(f"USB write failed ({e})") from None
        if n < 0:
            raise LinkLost("USB write failed")

    def replies(self) -> list[bytes]:
        """Everything the keyboard has sent since the last call (never blocks)."""
        out = []
        if self.dev is None:
            return out
        try:
            while True:
                r = self.dev.read(64)
                if not r:
                    return out
                out.append(bytes(r))
        except (OSError, ValueError) as e:
            raise LinkLost(f"USB read failed ({e})") from None

    def request(self, raw: bytes, timeout: float, stopped=lambda: False):
        """Send a packet and wait for the screen to acknowledge it."""
        self.write(raw)
        deadline = time.monotonic() + timeout
        while not stopped():
            if any(is_ack(r, raw[9]) for r in self.replies()):
                return
            if time.monotonic() >= deadline:
                raise LinkLost(f"No reply within {timeout:g} s")
            time.sleep(0.01)


def interactive(link, reading, interval, stopped, backoff, timeout):
    """Change the update rate live with the arrow keys, and watch the screen's replies.

    For finding a rate that saves battery: the reply counter shows whether the keyboard is
    still answering, and pausing shows how long the screen keeps the last values.
    """
    try:
        import select
        import termios
        import tty
    except ImportError:
        raise SystemExit("--interactive needs macOS or Linux. Use --rate or --interval instead.") from None
    if not sys.stdin.isatty():
        raise SystemExit("--interactive needs a terminal. Use --rate or --interval instead.")

    idx = min(range(len(RATES)), key=lambda i: abs(RATES[i] - interval))
    paused, sent, acked = False, 0, 0
    last_send = last_ack = None
    awaiting = None            # when the oldest unanswered packet was sent
    retry_at, problem = None, ""
    note, note_until = "", 0.0         # short-lived message, e.g. after a clock sync
    cpu = gpu = rpm = mbps = 0.0

    def failed(why: str):
        nonlocal awaiting, retry_at, problem
        link.close()
        awaiting = None
        wait = backoff.fail()
        if wait is None:
            raise SystemExit("\n" + GAVE_UP.format(n=backoff.retries))
        retry_at, problem = time.monotonic() + wait, why

    print("Up/Down: update rate   Space: send now   T: set the clock   P: pause or resume   Q: quit")
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        while not stopped():
            now = time.monotonic()
            try:
                if retry_at is not None:
                    due = now >= retry_at
                else:
                    due = not paused and (last_send is None or now - last_send >= RATES[idx])
                if due:
                    retry_at = None
                    cpu, gpu, rpm, mbps = reading()
                    if link is not None:
                        link.write(sysinfo_packet(cpu, gpu, rpm=rpm, mbps=mbps))
                        awaiting = now if awaiting is None else awaiting
                    sent, last_send = sent + 1, now
                if link is not None:
                    for reply in link.replies():
                        if is_ack(reply, 0xFF):
                            acked, last_ack, awaiting = acked + 1, time.monotonic(), None
                            backoff.ok()
                        elif is_ack(reply, 0x38):
                            note, note_until = "clock set", time.monotonic() + 4
                    if awaiting is not None and time.monotonic() - awaiting > timeout:
                        failed(f"no reply within {timeout:g} s")
            except LinkLost as e:
                failed(str(e))

            if link is None:
                replies = "dry run, nothing sent"
            elif last_ack is None:
                replies = f"replies 0/{sent}"
            else:
                replies = f"replies {acked}/{sent}, last {time.monotonic() - last_ack:.0f} s ago"
            name = PRESET_NAMES.get(RATES[idx])
            if retry_at is not None:
                state = (f"NO ANSWER ({problem}), retry {backoff.failures}/{backoff.retries} "
                         f"in {max(0, retry_at - time.monotonic()):.0f} s")
            elif paused:
                state = "PAUSED"
            else:
                state = f"every {RATES[idx]:g} s" + (f" ({name})" if name else "")
            if time.monotonic() < note_until:
                state += f" | {note}"
            print(f"\r\x1b[K{state} | {replies} | sent CPU {cpu:.0f}°C GPU {gpu:.0f}°C "
                  f"fan {rpm:.0f} down {mbps:.1f} Mbps", end="", flush=True)

            if not select.select([fd], [], [], 0.05)[0]:
                continue
            key = os.read(fd, 64)                           # may hold several repeated arrows
            ups = sum(key.count(k) for k in (b"\x1b[A", b"\x1bOA", b"+"))
            downs = sum(key.count(k) for k in (b"\x1b[B", b"\x1bOB", b"-"))
            if ups or downs:                                # up = longer gap between updates
                idx = max(0, min(len(RATES) - 1, idx + ups - downs))
            elif key == b" ":
                last_send = None
                retry_at = now if retry_at is not None else None    # retry right away
            elif key in (b"t", b"T"):
                try:
                    if link is not None:
                        link.write(time_packet())
                    note = "clock sync sent, waiting for the screen" if link is not None else "clock sync (dry run)"
                except LinkLost as e:
                    note = f"clock not set: {e}"
                note_until = time.monotonic() + 4
            elif key in (b"p", b"P"):
                paused = not paused
            elif key in (b"q", b"Q", b"\x03", b"\x04"):
                break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="change the update rate with the arrow keys and watch the screen's replies")
    ap.add_argument("--mock", action="store_true",
                    help="send clock-driven test values to see how often the screen updates: CPU counts "
                         "up from 10 once a second, GPU counts down from 99, fan = tenths of a second "
                         "since the start, download = packet number")
    ap.add_argument("--dry-run", action="store_true", help="print packets instead of sending")
    ap.add_argument("--once", action="store_true", help="send one stats packet and exit")
    ap.add_argument("--rate", choices=tuple(PRESETS),
                    help="how often to update: eco = every 5 s, everyday = 3 s, fast = 1 s, realtime = 0.5 s "
                         "(faster rates probably use more battery). Default: everyday over the 2.4G "
                         "receiver, fast over the cable")
    ap.add_argument("--interval", type=float, help="exact seconds between updates, instead of --rate")
    ap.add_argument("--timeout", type=float, default=2.0,
                    help="seconds to wait for the screen to acknowledge a packet (default 2)")
    ap.add_argument("--retries", type=int, default=10,
                    help="retries after a missing reply or lost connection before giving up; the pause "
                         "doubles each time, 1 s up to 60 s (default 10, about 5 minutes)")
    ap.add_argument("--rpm", type=float, default=0, help="fixed fan RPM to show (Apple Silicon fans can't be read without root)")
    ap.add_argument("--no-time", action="store_true", help="don't sync the clock at start")
    ap.add_argument("--resync-minutes", type=float, default=60, help="re-sync the clock this often (0 = never)")
    args = ap.parse_args()

    if args.interval is not None and args.interval <= 0:
        ap.error("--interval must be more than 0")
    if args.timeout <= 0:
        ap.error("--timeout must be more than 0")
    if args.retries < 0:
        ap.error("--retries must be 0 or more")

    link = None
    if not args.dry_run:
        link = Link()
        try:
            link.open()
        except LinkLost:
            raise SystemExit(NOT_FOUND) from None
        print(f"Connected over the {link.via}.")
    if args.interval is None:
        args.interval = PRESETS[args.rate or ("everyday" if link is not None and link.wireless else "fast")]
    if not args.once:
        print(f"Updating every {args.interval:g} s.")

    temps, net = temperature_source(), NetSpeed()
    warned = False

    def warn_once():
        nonlocal warned
        if temps.error and not warned:
            print(f"\nwarning: {temps.error}; temperatures will show 0", file=sys.stderr)
            warned = True
    warn_once()

    stop = False
    def _stop(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    def nap(seconds: float):
        end = time.monotonic() + seconds
        while not stop and time.monotonic() < end:
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))

    def send(raw: bytes):
        if link is None:
            print("  " + raw.hex(" "))
        else:
            link.request(raw, args.timeout, lambda: stop)

    packets, mock_start = 0, 0.0

    def reading():
        """(CPU °C, GPU °C, fan RPM, download Mbps) for the next packet."""
        nonlocal packets, mock_start
        packets += 1
        if args.mock:
            # Values follow the clock, not the packet count, so they advance at the same
            # pace whatever the update rate: the screen shows how stale each update is.
            mock_start = mock_start or time.monotonic()
            elapsed = time.monotonic() - mock_start
            second = int(elapsed) % 90
            return 10 + second, 99 - second, int(elapsed * 10) % 10000, float(packets % 1000)
        return temps.cpu or 0, temps.gpu or 0, args.rpm, net.mbps()

    backoff = Backoff(args.retries)
    last_sync = 0.0
    deadline = time.monotonic() + (0 if args.mock else temps.warmup)
    while not stop and not temps.error and temps.cpu is None and time.monotonic() < deadline:
        time.sleep(0.2)                   # wait for the first plausible temperature sample
    try:
        if args.interactive:
            if not args.no_time:          # once at the start; T sets it again
                try:
                    if link is not None:
                        link.request(time_packet(), args.timeout, lambda: stop)
                    print("clock synced")
                except LinkLost as e:
                    print(f"Clock not set: {e}. Press T to try again.")
            interactive(link, reading, args.interval, lambda: stop, backoff, args.timeout)
            return
        while not stop:
            warn_once()
            wait = args.interval
            try:
                if not args.no_time and args.resync_minutes >= 0 and (
                        last_sync == 0 or (args.resync_minutes and time.time() - last_sync > args.resync_minutes * 60)):
                    send(time_packet())
                    last_sync = time.time()
                    print("clock synced")
                cpu, gpu, rpm, mbps = reading()
                send(sysinfo_packet(cpu, gpu, rpm=rpm, mbps=mbps))
                if stop:
                    break
                if backoff.ok():
                    print("\nThe keyboard is answering again.")
                print(f"\rCPU {cpu:5.1f}°C  GPU {gpu:5.1f}°C  down {mbps:7.1f} Mbps ", end="", flush=True)
                if args.once:
                    break
            except LinkLost as e:
                link.close()
                last_sync = 0.0           # sync the clock again once it is back
                wait = backoff.fail()
                if wait is None:
                    raise SystemExit("\n" + GAVE_UP.format(n=args.retries)) from None
                print(f"\n{e}. Retry {backoff.failures} of {args.retries} in {wait:g} s.", end="", flush=True)
            nap(wait)
    finally:
        print()
        temps.stop()
        if link is not None:
            link.close()


if __name__ == "__main__":
    main()
