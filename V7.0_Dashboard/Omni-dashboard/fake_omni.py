"""Stand-in devices for Omni: three Polar H10s, three O2 rings and three radios, so the multi-person dashboard runs
without any hardware or Bluetooth:  OMNI_FAKE=1 python3 omni.py   (or OMNI_FAKE=1 ./run_dashboard.sh)

It replaces bleak (BleakScanner, BleakClient), polar_python (PolarDevice) and bt_debug.list_adapters before omni.py
imports them. What they send, in the formats omni.py reads:
  H10   ECG 130 Hz in 73-sample frames (µV, stamped on the strap's own clock), accelerometer ~200 Hz frames (mg),
        HR + R-R once a second. Strap A beats at ~62 bpm, B at ~74, C at ~88, each with its own variability.
  O2    a 0x55 real-time frame (SpO2 at byte 7, HR at byte 8) after each poll: 97 / 95 / 92 % around the same HR.
  radios  hci0 (internal) and hci1, hci2 (external, USB), each "hears" every device.
Each device takes one connection at a time, like the real ones. Names carry no real IDs.
"""
import asyncio
import math
import random
import sys
import time
import types

RADIOS = [("hci0", "STAND-IN-RADIO-0", "internal"), ("hci1", "STAND-IN-RADIO-1", "external"),
          ("hci2", "STAND-IN-RADIO-2", "external")]
H10S = [("Polar H10 0FA7E001", "STAND-IN-H10-A", 62, 45), ("Polar H10 0FA7E002", "STAND-IN-H10-B", 74, 28),
        ("Polar H10 0FA7E003", "STAND-IN-H10-C", 88, 14)]           # name, address, bpm, beat-to-beat spread (ms)
O2S = [("O2Ring 0FA1", "STAND-IN-O2-A", 97, 62), ("O2Ring 0FA2", "STAND-IN-O2-B", 95, 74),
       ("O2Ring 0FA3", "STAND-IN-O2-C", 92, 88)]                    # name, address, SpO2, HR
VIATOM_SVC = "14839ac4-7d7e-415c-9a42-167340cf2339"
NOTIFY_UUID = "0734594a-a8e7-4b1a-a6b1-cd5243059a57"
WRITE_UUID = "8b00ace7-eb0a-49b0-b977-10a8d4d5e82f"

_connected = set()          # addresses with a live connection (one at a time per device)
_hidden = set()             # addresses switched "off" (tests: out of range)


class BleakError(Exception):
    pass


class FakeDevice:
    def __init__(self, name, address, hci):
        self.name, self.address = name, address
        self.details = {"path": f"/org/bluez/{hci}/dev_{address.replace('-', '_')}"}


class FakeAdv:
    def __init__(self, name, rssi, uuids=()):
        self.local_name, self.rssi, self.service_uuids = name, rssi, list(uuids)


def _all_devices():
    for i, (name, addr, *_rest) in enumerate(H10S):
        yield name, addr, -55 - 4 * i, ()
    for i, (name, addr, *_rest) in enumerate(O2S):
        yield name, addr, -60 - 3 * i, (VIATOM_SVC,)


class FakeScanner:
    def __init__(self, callback=None, bluez=None, **kw):
        self.cb, self.hci = callback, (bluez or {}).get("adapter") or "hci0"
        self.task = None

    async def __aenter__(self):
        async def run():
            while True:
                for name, addr, rssi, uuids in _all_devices():
                    if addr in _connected or addr in _hidden:
                        continue                    # connected devices stop advertising, like the H10 and the ring
                    self.cb(FakeDevice(name, addr, self.hci), FakeAdv(name, rssi + random.randint(-3, 3), uuids))
                await asyncio.sleep(0.4)
        self.task = asyncio.ensure_future(run())
        return self

    async def __aexit__(self, *exc):
        self.task.cancel()

    @staticmethod
    async def find_device_by_address(address, timeout=10.0, bluez=None, **kw):
        await asyncio.sleep(0.05)
        hci = (bluez or {}).get("adapter") or "hci0"
        for name, addr, _rssi, _u in _all_devices():
            if addr.upper() == str(address).upper() and addr not in _hidden:
                return FakeDevice(name, addr, hci)
        return None


# ---------------------------------------------------------------- Polar H10
class _HR:
    def __init__(self, bpm, rrs):
        self.heartrate, self.rr_intervals = bpm, rrs


class _Frame:
    def __init__(self, ts_ns, data):
        self.timestamp, self.data = ts_ns, data


def _ecg_uv(t, beats):
    """A PQRST-ish trace (µV) at time t from the beat times around it."""
    v = 0.0
    for b in beats:
        d = t - b
        if -0.3 < d < 0.5:
            v += 90 * math.exp(-((d + 0.16) / 0.025) ** 2)          # P
            v -= 120 * math.exp(-((d + 0.025) / 0.008) ** 2)        # Q
            v += 1100 * math.exp(-(d / 0.011) ** 2)                 # R
            v -= 250 * math.exp(-((d - 0.03) / 0.012) ** 2)         # S
            v += 260 * math.exp(-((d - 0.24) / 0.05) ** 2)          # T
    return v


class FakePolar:
    def __init__(self, device):
        self.dev = device
        spec = next(h for h in H10S if h[1] == device.address)
        self.bpm, self.spread = spec[2], spec[3]
        self.tasks, self.beats = [], []
        self.rng = random.Random(hash(device.address) & 0xFFFF)
        self.clock0 = time.time() - 1000.0 * (1 + H10S.index(spec))   # the strap's clock isn't real time

    async def __aenter__(self):
        if self.dev.address in _connected:
            raise BleakError(f"{self.dev.address}: already connected")
        _connected.add(self.dev.address)
        await asyncio.sleep(0.2)
        t, mean = time.time() - 3, 60.0 / self.bpm
        while t < time.time() + 3600:
            self.beats.append(t)
            breath = 0.04 * math.sin(2 * math.pi * t / 5.0)
            t += mean + breath * (self.spread / 45) + self.rng.gauss(0, self.spread / 1000 / 3)
        return self

    async def __aexit__(self, *exc):
        for k in self.tasks:
            k.cancel()
        _connected.discard(self.dev.address)

    def _strap_ns(self, t):
        return int((t - self.clock0) * 1e9)

    async def start_hr_stream(self, cb):
        async def run():
            sent = time.time()
            while True:
                await asyncio.sleep(1.0)
                now = time.time()
                new = [b for b in self.beats if sent < b <= now]
                rrs = [round((b - self.beats[self.beats.index(b) - 1]) * 1000) for b in new]
                sent = now
                bpm = round(60000 / (sum(rrs) / len(rrs))) if rrs else self.bpm
                cb(_HR(bpm, rrs))
        self.tasks.append(asyncio.ensure_future(run()))

    async def start_acc_stream(self, cb, *args):
        async def run():
            n, per = 36, 1 / 205.6
            t = time.time()
            while True:
                await asyncio.sleep(n * per)
                ts = [t + (i + 1) * per for i in range(n)]
                t = ts[-1]
                data = [(int(20 * math.sin(2 * math.pi * x / 4)), int(-990 + 15 * math.sin(2 * math.pi * x / 5)),
                         int(60 + 8 * math.sin(2 * math.pi * x / 3))) for x in ts]
                cb(_Frame(self._strap_ns(ts[-1]), data))
        self.tasks.append(asyncio.ensure_future(run()))

    async def start_ecg_stream(self, cb, *args):
        async def run():
            n, per = 73, 1 / 130.0
            t = time.time()
            while True:
                await asyncio.sleep(n * per)
                ts = [t + (i + 1) * per for i in range(n)]
                t = ts[-1]
                near = [b for b in self.beats if ts[0] - 1 < b < ts[-1] + 1]
                cb(_Frame(self._strap_ns(ts[-1]), [int(_ecg_uv(x, near) + self.rng.gauss(0, 12)) for x in ts]))
        self.tasks.append(asyncio.ensure_future(run()))


# ---------------------------------------------------------------- O2 ring
class _Char:
    def __init__(self, uuid, props):
        self.uuid, self.properties = uuid, props


class _Svc:
    characteristics = [_Char(NOTIFY_UUID, ["notify"]), _Char(WRITE_UUID, ["write", "write-without-response"])]


class _Services:
    def get_service(self, uuid):
        return _Svc() if uuid.lower() == VIATOM_SVC else None


class FakeClient:
    def __init__(self, device, timeout=15.0, disconnected_callback=None, bluez=None, **kw):
        self.address = device if isinstance(device, str) else device.address
        self.on_disc = disconnected_callback
        self.services, self.mtu_size, self.is_connected = _Services(), 247, False
        self.cb, self.n = None, 0
        spec = next(o for o in O2S if o[1] == self.address)
        self.spo2, self.hr = spec[2], spec[3]

    async def __aenter__(self):
        if self.address in _connected:
            raise BleakError(f"{self.address}: already connected")
        _connected.add(self.address)
        self.is_connected = True
        return self

    async def __aexit__(self, *exc):
        self.is_connected = False
        _connected.discard(self.address)

    async def start_notify(self, uuid, cb):
        self.cb = cb

    async def write_gatt_char(self, uuid, data, response=True):
        if self.address in _hidden:                 # went out of range
            self.is_connected = False
            _connected.discard(self.address)
            if self.on_disc:
                self.on_disc(self)
            raise BleakError("not connected")
        self.n += 1
        if self.cb:
            if self.n == 1:
                self.cb(None, bytearray([0xA5]))       # settling first, like the ring
            else:
                spo2 = self.spo2 + (1 if self.n % 7 == 0 else 0) - (1 if self.n % 11 == 0 else 0)
                hr = self.hr + int(3 * math.sin(self.n / 4))
                self.cb(None, bytearray([0x55, 0, 0, 0, 0, 0, 0, spo2, hr, 0, 0]))
                self.cb(None, bytearray([0x12, 0x34]))  # a continuation fragment: must be skipped


async def fake_list_adapters():
    return [{"name": n, "address": a, "powered": True, "placement": pl, "vendor": "Stand-in",
             "product": "fake radio", "bus": "usb" if pl == "external" else "uart"} for n, a, pl in RADIOS]


def install():
    bleak = types.ModuleType("bleak")
    bleak.BleakScanner, bleak.BleakClient, bleak.BleakError = FakeScanner, FakeClient, BleakError
    pp = types.ModuleType("polar_python")
    pp.PolarDevice = FakePolar
    sys.modules["bleak"], sys.modules["polar_python"] = bleak, pp
    import bt_debug
    bt_debug.list_adapters = fake_list_adapters


def hide(address, hidden=True):
    """Tests: take a device out of range (or bring it back)."""
    (_hidden.add if hidden else _hidden.discard)(address)
