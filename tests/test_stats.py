"""tiga-stats reconnect and retry behaviour, against a fake HID device (no hardware)."""
import sys
import types

import pytest

from tiga_tool import stats
from tiga_tool.protocol import RECEIVER_VENDOR_ID, USAGE, USAGE_PAGE, sysinfo_packet


class FakeHid(types.ModuleType):
    """Stands in for the `hid` module. `plan(n)` says what the n-th write does:
    "ack" (the screen answers), "silent" (no answer) or "error" (the write fails)."""

    def __init__(self, plan):
        super().__init__("hid")
        self.plan, self.opens, self.writes = plan, 0, 0

    def enumerate(self, vid, pid):
        if vid != RECEIVER_VENDOR_ID:
            return []
        return [{"usage_page": USAGE_PAGE, "usage": USAGE, "path": b"fake"}]

    def device(self):
        hub = self

        class Dev:
            def __init__(self):
                self.queue = []

            def open_path(self, path):
                hub.opens += 1

            def set_nonblocking(self, on):
                pass

            def write(self, data):
                hub.writes += 1
                what = hub.plan(hub.writes)
                if what == "error":
                    return -1
                if what == "ack":       # data[0] is the report ID, so the command is data[10]
                    self.queue.append([0x1C, 2, 0, 0, 0, 7, 0, 0, 0xA5, data[10], 0, 2, 0xFE, 0])
                return len(data)

            def read(self, n):
                return self.queue.pop(0) if self.queue else []

            def close(self):
                pass

        return Dev()


def run(monkeypatch, plan, *argv):
    fake = FakeHid(plan)
    monkeypatch.setitem(sys.modules, "hid", fake)
    monkeypatch.setattr(stats.Backoff, "START", 0.01)
    monkeypatch.setattr(sys, "argv", ["tiga-stats", "--mock", "--no-time", "--interval", "0.01",
                                      "--timeout", "0.05", *argv])
    return fake


def test_backoff_doubles_up_to_the_cap_then_runs_out():
    b = stats.Backoff(9)
    assert [b.fail() for _ in range(10)] == [1, 2, 4, 8, 16, 32, 60, 60, 60, None]
    b = stats.Backoff(3)
    assert b.fail() == 1 and b.fail() == 2
    assert b.ok() is True and b.ok() is False       # a success resets the count
    assert b.fail() == 1


def test_request_waits_for_the_matching_ack(monkeypatch):
    monkeypatch.setitem(sys.modules, "hid", FakeHid(lambda n: "ack" if n == 1 else "silent"))
    link = stats.Link()
    link.open()
    assert link.wireless and link.via == "2.4G receiver"
    link.request(sysinfo_packet(40, 50), timeout=0.05)
    with pytest.raises(stats.LinkLost, match="No reply within"):
        link.request(sysinfo_packet(40, 50), timeout=0.05)


def test_gives_up_after_the_retries_and_says_what_to_do(monkeypatch, capsys):
    fake = run(monkeypatch, lambda n: "ack" if n <= 2 else "silent", "--retries", "3")
    with pytest.raises(SystemExit) as stop:
        stats.main()
    assert "after 3 retries" in str(stop.value) and "run tiga-stats again" in str(stop.value)
    assert fake.writes == 2 + 1 + 3                 # two good, the first miss, three retries
    assert fake.opens == 1 + 3                      # the device is reopened for every retry
    out = capsys.readouterr().out
    assert "Retry 1 of 3 in 0.01 s" in out and "Retry 3 of 3 in 0.04 s" in out


def test_recovers_when_the_keyboard_comes_back(monkeypatch, capsys):
    fake = run(monkeypatch, lambda n: "error" if n == 1 else "silent" if n == 2 else "ack", "--once")
    stats.main()
    out = capsys.readouterr().out
    assert "USB write failed. Retry 1 of 10" in out and "No reply within 0.05 s. Retry 2 of 10" in out
    assert "The keyboard is answering again." in out
    assert fake.writes == 3 and fake.opens == 3
