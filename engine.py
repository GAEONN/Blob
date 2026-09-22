"""Vendor-neutral Windows hardware monitoring for Blob.

Windows exposes load, memory, battery, adapter and storage data directly.  CPU/GPU
temperature and fan RPM have no vendor-neutral Windows API, so Blob consumes the
read-only sensor feeds from LibreHardwareMonitor/OpenHardwareMonitor or HWiNFO when
one is running.  NVIDIA NVML is used opportunistically for extra GPU data, but the
application never depends on a particular laptop brand and does not change firmware
fan or power profiles.
"""
import ctypes
import base64
import hashlib
import json
import math
import subprocess
import os
import re
import struct
import threading
import time
import traceback
import urllib.error
import urllib.request
import winreg
from ctypes import wintypes

import psutil

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "Blob-v5")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "blob.log")
os.makedirs(DATA_DIR, exist_ok=True)


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
INVALID_HANDLE = wintypes.HANDLE(-1).value


def open_device(path):
    h = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)
    if h in (None, INVALID_HANDLE):
        h = k32.CreateFileW(path, 0, 3, None, 3, 0, None)
    return None if h in (None, INVALID_HANDLE) else h


def ioctl(handle, code, inbuf, outlen):
    inb = ctypes.create_string_buffer(inbuf, len(inbuf))
    out = ctypes.create_string_buffer(outlen)
    ret = wintypes.DWORD()
    ok = k32.DeviceIoControl(handle, code, inb, len(inbuf), out, outlen, ctypes.byref(ret), None)
    return out.raw[:ret.value] if ok else None


# ─────────────────────────── NVMe temperatures ───────────────────────────
class AsusReadOnly:
    """Optional OEM fallback using only DSTS reads, never fan/profile writes."""
    def __init__(self):
        manufacturer = reg_str(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemManufacturer").lower()
        self.h = open_device(r"\\.\ATKACPI") if "asus" in manufacturer else None

    def read(self, device):
        if not self.h:
            return None
        data = ioctl(self.h, 0x0022240C, struct.pack("<IIII", 0x53545344, 8, device, 0), 16)
        if len(data) < 4:
            return None
        raw = struct.unpack_from("<i", data)[0]
        return raw - 65536 if 65536 <= raw < 131072 else None

    def poll(self):
        values, names, ids = [], [], []
        for device, name in ((0x00110013, "CPU fan"), (0x00110014, "GPU fan")):
            raw = self.read(device)
            if raw is not None and 0 <= raw <= 200:
                values.append(raw*100)
                names.append(name)
                ids.append(hex(device))
        temp = self.read(0x00120094)
        return {"fans": values, "fan_names": names, "fan_ids": ids,
                "cpu": temp if temp is not None and 0 < temp < 150 else None}


def merge_sensor_feeds(*feeds):
    """Fill missing fields without allowing one partial provider to mask another."""
    result = {"cpu": None, "gpu": None, "fans": [], "fan_names": [], "fan_ids": [], "fan_sources": []}
    seen = set()
    for source, feed in feeds:
        for kind in ("cpu", "gpu"):
            value = feed.get(kind)
            if result[kind] is None and isinstance(value, (int, float)) and math.isfinite(value) and 0 < value < 150:
                result[kind], result[kind+"_source"] = value, source
        names = feed.get("fan_names", [])
        ids, sources = feed.get("fan_ids", []), feed.get("fan_sources", [])
        for i, rpm in enumerate(feed.get("fans", [])):
            if not isinstance(rpm, (int, float)) or not math.isfinite(rpm) or not 0 <= rpm <= 30000:
                continue
            name = names[i] if i < len(names) else f"{source} fan {i+1}"
            origin = sources[i] if i < len(sources) else source
            identity = str(ids[i]) if i < len(ids) and ids[i] is not None else str(i)
            key = (origin, identity)
            if key in seen:
                continue
            seen.add(key)
            result["fans"].append(round(rpm))
            result["fan_names"].append(name)
            result["fan_ids"].append(identity)
            result["fan_sources"].append(origin)
    return result


def nvme_temps():
    res = []
    for i in range(4):
        h = open_device(f"\\\\.\\PhysicalDrive{i}")
        if not h:
            continue
        try:
            # STORAGE_PROPERTY_QUERY{StorageDeviceTemperatureProperty=52, PropertyStandardQuery}
            r = ioctl(h, 0x002D1400, struct.pack("<II", 52, 0) + b"\0" * 4, 512)
            if r and len(r) >= 24:
                _, _, crit, warn, cnt = struct.unpack_from("<IIhhH", r)
                temps = [struct.unpack_from("<h", r, 24 + 16 * j + 2)[0] for j in range(cnt)
                         if 24 + 16 * j + 4 <= len(r)]
                temps = [t for t in temps if 0 < t < 150]
                if temps:
                    res.append({"drive": i, "temps": temps, "warn": warn, "crit": crit})
        finally:
            k32.CloseHandle(h)
    return res


# ─────────────────────────── Performance counters (PDH) ───────────────────────────
class PdhFmt(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("pad", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class PdhItem(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", PdhFmt)]


class Pdh:
    def __init__(self, paths):
        self.pdh = ctypes.WinDLL("pdh")
        self.q = ctypes.c_void_p()
        self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.q))
        self.c = {}
        for key, path in paths.items():
            c = ctypes.c_void_p()
            if self.pdh.PdhAddEnglishCounterW(self.q, path, None, ctypes.byref(c)) == 0:
                self.c[key] = c
        self.pdh.PdhCollectQueryData(self.q)

    def collect(self):
        self.pdh.PdhCollectQueryData(self.q)

    def values(self, key):
        c = self.c.get(key)
        if not c:
            return {}
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        self.pdh.PdhGetFormattedCounterArrayW(c, 0x8200, ctypes.byref(size), ctypes.byref(count), None)
        if not size.value:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if self.pdh.PdhGetFormattedCounterArrayW(c, 0x8200, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(PdhItem))
        return {items[i].szName: items[i].FmtValue.doubleValue for i in range(count.value)
                if items[i].FmtValue.CStatus in (0, 1)}


# ─────────────────────────── GPUs ───────────────────────────
class LUID(ctypes.Structure):
    _fields_ = [("Low", wintypes.DWORD), ("High", wintypes.LONG)]


class AdapterInfo(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("Luid", LUID), ("NumOfSources", wintypes.ULONG),
                ("bPrecise", wintypes.BOOL)]


class EnumAdapters2(ctypes.Structure):
    _fields_ = [("NumAdapters", wintypes.ULONG), ("pAdapters", ctypes.POINTER(AdapterInfo))]


class QueryAdapterInfo(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("Type", ctypes.c_int), ("pData", ctypes.c_void_p),
                ("DataSize", wintypes.UINT)]


class AdapterRegInfo(ctypes.Structure):
    _fields_ = [("AdapterString", wintypes.WCHAR * 260), ("BiosString", wintypes.WCHAR * 260),
                ("DacType", wintypes.WCHAR * 260), ("ChipType", wintypes.WCHAR * 260)]


def gpu_adapters():
    """[(luid_tag, name)] for every WDDM adapter, without waking any GPU."""
    out = []
    try:
        gdi = ctypes.WinDLL("gdi32")
        e = EnumAdapters2()
        gdi.D3DKMTEnumAdapters2(ctypes.byref(e))
        arr = (AdapterInfo * e.NumAdapters)()
        e.pAdapters = arr
        if gdi.D3DKMTEnumAdapters2(ctypes.byref(e)) != 0:
            return out
        for a in arr[:e.NumAdapters]:
            ri = AdapterRegInfo()
            q = QueryAdapterInfo(a.hAdapter, 8, ctypes.cast(ctypes.byref(ri), ctypes.c_void_p), ctypes.sizeof(ri))
            name = ri.AdapterString if gdi.D3DKMTQueryAdapterInfo(ctypes.byref(q)) == 0 else ""
            gdi.D3DKMTCloseAdapter(ctypes.byref(wintypes.UINT(a.hAdapter)))
            tag = f"0x{a.Luid.High & 0xFFFFFFFF:08x}_0x{a.Luid.Low:08x}"
            if name and "Basic Render" not in name:
                out.append((tag, name))
    except Exception:
        log("gpu_adapters: " + traceback.format_exc())
    return out


class DevPropKey(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", wintypes.ULONG)]


class NvidiaPower:
    """Reads the dGPU's D-state from the PnP manager (does not wake the GPU)."""

    def __init__(self):
        import uuid
        self.cfg = ctypes.WinDLL("cfgmgr32")
        self.key = DevPropKey()
        self.key.fmtid[:] = list(uuid.UUID("a45c254e-df1c-4efd-8020-67d146a850e0").bytes_le)
        self.key.pid = 32  # DEVPKEY_Device_PowerData
        self.devinst = None
        flt = "{4d36e968-e325-11ce-bfc1-08002be10318}"  # Display class
        n = wintypes.ULONG()
        if self.cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(n), flt, 0x300) == 0 and n.value:
            buf = ctypes.create_unicode_buffer(n.value)
            self.cfg.CM_Get_Device_ID_ListW(flt, buf, n.value, 0x300)
            for dev in buf[:n.value].split("\0"):
                if "VEN_10DE" in dev.upper():
                    inst = wintypes.DWORD()
                    if self.cfg.CM_Locate_DevNodeW(ctypes.byref(inst), dev, 0) == 0:
                        self.devinst = inst
                    break

    def awake(self):
        if self.devinst is None:
            return None
        t, size = wintypes.ULONG(), wintypes.ULONG(64)
        b = ctypes.create_string_buffer(64)
        if self.cfg.CM_Get_DevNode_PropertyW(self.devinst, ctypes.byref(self.key), ctypes.byref(t), b,
                                             ctypes.byref(size), 0) != 0:
            return None
        return struct.unpack_from("<I", b.raw, 4)[0] == 1  # PowerDeviceD0


class NvUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class Nvml:
    def __init__(self):
        self.lib = None
        for p in (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll"),
                  r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll"):
            if os.path.exists(p):
                try:
                    self.lib = ctypes.CDLL(p)
                    break
                except OSError:
                    pass

    def read(self):
        """Init → read → shutdown, so we never hold the dGPU awake."""
        if not self.lib or self.lib.nvmlInit_v2() != 0:
            return None
        try:
            h = ctypes.c_void_p()
            if self.lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
                return None
            t, p, clk, u = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), NvUtil()
            r = {}
            if self.lib.nvmlDeviceGetTemperature(h, 0, ctypes.byref(t)) == 0:
                r["temp"] = t.value
            if self.lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == 0:
                r["load"] = u.gpu
            if self.lib.nvmlDeviceGetPowerUsage(h, ctypes.byref(p)) == 0:
                r["power"] = round(p.value / 1000, 1)
            if self.lib.nvmlDeviceGetClockInfo(h, 0, ctypes.byref(clk)) == 0:
                r["clock"] = clk.value
            return r
        finally:
            self.lib.nvmlShutdown()


# ─────────────────────────── Config ───────────────────────────
def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass


def reg_str(path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            return str(winreg.QueryValueEx(k, name)[0]).strip()
    except OSError:
        return ""


# ─────────────────────────── Sampler ───────────────────────────
class HWiNFOShared:
    """HWiNFO publishes every sensor in shared memory when "Shared Memory Support" is enabled:
    a header, then a table of readings (type, labels, unit, value) as fixed-size C structs."""

    NAME = "Global\\HWiNFO_SENS_SM2"
    # HWiNFO SDK uses #pragma pack(1): neither header nor doubles are padded.
    HEADER = struct.Struct("<4sIIqIIIIII")
    LABEL_USER = 12 + 128        # after type / sensor index / reading id and the original label
    UNIT = LABEL_USER + 128
    VALUE = UNIT + 16
    TEMPERATURE, FAN = 1, 3

    def __init__(self):
        self.values = {}
        self.checked = 0.0

    def poll(self):
        now = time.time()
        if now - self.checked < 2.0:
            return self.values
        self.checked = now
        self.values = {}
        try:
            import mmap
            with mmap.mmap(-1, self.HEADER.size, tagname=self.NAME, access=mmap.ACCESS_READ) as head:
                sig, _ver, _rev, _poll, _s_off, _s_size, _s_num, r_off, r_size, r_num = \
                    self.HEADER.unpack(head[:self.HEADER.size])
            if (sig not in (b"HWiS", b"SiWH") or not self.VALUE+8 <= r_size <= 4096 or
                    not 0 < r_num <= 10000 or r_off < self.HEADER.size or r_off+r_size*r_num > 32*1024*1024 or
                    not 0 <= now-_poll < 30):
                return self.values
            # Windows wants an explicit length, so map exactly as far as the reading table goes
            with mmap.mmap(-1, r_off + r_size * r_num, tagname=self.NAME, access=mmap.ACCESS_READ) as mm:
                temps, fans, fan_names, fan_ids = {}, [], [], []
                for i in range(min(r_num, 800)):
                    base = r_off + i * r_size
                    kind = struct.unpack_from("<I", mm, base)[0]
                    if kind not in (self.TEMPERATURE, self.FAN):
                        continue
                    raw = mm[base + self.LABEL_USER:base + self.UNIT]
                    label = raw.split(b"\x00")[0].decode("latin-1")
                    if not label:
                        label = mm[base+12:base+self.LABEL_USER].split(b"\x00")[0].decode("latin-1")
                    value = struct.unpack_from("<d", mm, base + self.VALUE)[0]
                    if kind == self.TEMPERATURE and 0 < value < 150:
                        temps[label] = value
                    elif kind == self.FAN and 0 <= value < 30000:
                        fans.append(value)
                        fan_names.append(label or f"Fan {len(fans)}")
                        sensor, reading = struct.unpack_from("<II", mm, base+4)
                        fan_ids.append(f"{sensor}:{reading}")
            pick = lambda *keys: next((v for k, v in temps.items()
                                       if any(x in k.lower() for x in keys)), None)
            self.values = {"cpu": pick("cpu package", "cpu (tctl", "core max", "cpu"),
                           "gpu": pick("gpu temperature", "gpu core"),
                           "fans": [round(f) for f in fans], "fan_names": fan_names, "fan_ids": fan_ids}
        except Exception:
            self.values = {}
        return self.values


class HardwareMonitorWMI:
    """Optional extra sensors from LibreHardwareMonitor / OpenHardwareMonitor, when the user
    happens to run one. That covers Intel and AMD machines, where Windows exposes nothing."""

    NAMESPACES = ((r"root\LibreHardwareMonitor", "LibreHardwareMonitor"),
                  (r"root\OpenHardwareMonitor", "OpenHardwareMonitor"))
    REST_URL = "http://127.0.0.1:8085/data.json"
    REST_AUTH = os.path.join(APP_DIR, "tools", "LibreHardwareMonitor", ".blob-http-auth.json")

    def __init__(self):
        self.namespace = None
        self.values = {}
        self.checked = 0.0
        self.launch_checked = 0.0

    @staticmethod
    def _number(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        match = re.search(r"[-+]?\d[\d,.]*", str(value or ""))
        if not match:
            return None
        text = match.group(0)
        if "," in text and "." not in text:
            tail = text.rsplit(",", 1)[1]
            text = text.replace(",", "") if len(tail) == 3 else text.replace(",", ".")
        else:
            text = text.replace(",", "")
        try:
            return float(text)
        except ValueError:
            return None

    @classmethod
    def _rest_sensors(cls, tree):
        rows = []

        def visit(node):
            kind = node.get("Type")
            if kind in ("Temperature", "Fan") and node.get("SensorId"):
                value = cls._number(node.get("RawValue"))
                if value is None:
                    value = cls._number(node.get("Value"))
                if value is not None:
                    rows.append({"Name": node.get("Text") or "Sensor", "SensorType": kind,
                                 "Value": value, "Identifier": node.get("SensorId", "")})
            for child in node.get("Children", []) or []:
                if isinstance(child, dict):
                    visit(child)

        if isinstance(tree, dict):
            visit(tree)
        return rows

    def poll_rest(self):
        """Read current LHM through its authenticated local endpoint when WMI is unavailable."""
        try:
            # Windows PowerShell 5.1 writes UTF-8 JSON with a BOM.
            with open(self.REST_AUTH, encoding="utf-8-sig") as f:
                auth = json.load(f)
            password = str(auth["password"])
            # LHM 0.9.6 hashes the configured password on load, but writes that hash
            # back on a graceful exit. Trying both forms keeps an upgraded or manually
            # closed provider readable without exposing the endpoint unauthenticated.
            passwords = (password, hashlib.sha256(password.encode()).hexdigest())
            rows = None
            for candidate in passwords:
                token = base64.b64encode(
                    f"{auth['username']}:{candidate}".encode()).decode()
                request = urllib.request.Request(
                    self.REST_URL,
                    headers={"Authorization": "Basic " + token, "User-Agent": "Blob/1.0"})
                try:
                    with urllib.request.urlopen(request, timeout=2) as response:
                        rows = self._rest_sensors(json.load(response))
                    break
                except urllib.error.HTTPError as error:
                    if error.code != 401:
                        raise
            if rows is None:
                return {}
        except Exception:
            return {}
        temps = [r for r in rows if r["SensorType"] == "Temperature"]
        fans = [r for r in rows if r["SensorType"] == "Fan" and r["Value"] >= 0]
        # Put spinning fans first so the two at-a-glance card slots do not get
        # consumed by a stopped header or a zero-RPM GPU fan.
        fans.sort(key=lambda row: row["Value"] <= 0)

        def pick(kind, *names):
            candidates = [r for r in temps if kind in r["Identifier"].lower()]
            return next((r["Value"] for r in candidates
                         if any(name in r["Name"].lower() for name in names)),
                        candidates[0]["Value"] if candidates else None)

        cpu = pick("cpu", "package", "core max", "tctl", "cpu core")
        gpu = pick("gpu", "gpu core", "gpu temperature", "gpu hot")
        if cpu is None and gpu is None and not fans:
            return {}
        return {"cpu": cpu, "gpu": gpu, "fans": [r["Value"] for r in fans],
                "fan_names": [r["Name"] for r in fans],
                "fan_ids": [r["Identifier"] for r in fans], "provider": "rest"}

    def running(self):
        names = {n.lower() for n in ("LibreHardwareMonitor.exe", "OpenHardwareMonitor.exe")}
        return any((p.info["name"] or "").lower() in names for p in psutil.process_iter(["name"]))

    def start_installed_provider(self):
        """Ask the elevated startup task installed by Blob to start LHM without another UAC prompt."""
        now = time.time()
        if now - self.launch_checked < 30:
            return
        self.launch_checked = now
        try:
            subprocess.run(["schtasks", "/Run", "/TN", "Blob Sensors"], capture_output=True,
                           timeout=5, creationflags=0x08000000)
        except Exception:
            pass

    def poll(self):
        """Returns {'cpu': °C, 'gpu': °C, 'fans': [rpm, ...]} — empty if no such app is running."""
        now = time.time()
        if now - self.checked < 2.0:
            return self.values
        self.checked = now
        if self.namespace is None and not self.running():
            self.start_installed_provider()
            self.values = {}
            return self.values
        rest = self.poll_rest()
        if rest:
            self.values = rest
            return self.values
        for ns, _ in ([(self.namespace, None)] if self.namespace else self.NAMESPACES):
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     f"Get-CimInstance -Namespace {ns} -ClassName Sensor -ErrorAction Stop | "
                     "Where-Object {$_.SensorType -in 'Temperature','Fan'} | "
                     "ConvertTo-Json -Compress -Depth 2"],
                    capture_output=True, text=True, timeout=6,
                    creationflags=0x08000000).stdout.strip()
                if not out:
                    continue
                data = json.loads(out)
                rows = data if isinstance(data, list) else [data]
                temps = {r["Name"]: r["Value"] for r in rows if r.get("SensorType") == "Temperature"}
                fans = [r for r in rows if r.get("SensorType") == "Fan" and
                        isinstance(r.get("Value"), (int, float)) and math.isfinite(r["Value"]) and
                        0 <= r["Value"] <= 30000]
                pick = lambda *keys: next((v for k, v in temps.items()
                                           if any(x in k.lower() for x in keys)), None)
                self.namespace = ns
                self.values = {"cpu": pick("cpu package", "core (tctl", "cpu core", "package"),
                               "gpu": pick("gpu core", "gpu temperature", "gpu hot"),
                               "fans": [round(r["Value"]) for r in fans],
                               "fan_names": [r["Name"] for r in fans],
                               "fan_ids": [r.get("Identifier", str(i)) for i, r in enumerate(fans)],
                               "provider": "wmi"}
                return self.values
            except Exception:
                continue
        self.namespace = None
        self.values = {}
        return self.values


class Monitor:
    def __init__(self):
        self.nv_power = NvidiaPower()
        self.nvml = Nvml()
        self.adapters = gpu_adapters()
        self.pdh = Pdh({
            "zones": r"\Thermal Zone Information(*)\Temperature",
            "perf": r"\Processor Information(_Total)\% Processor Performance",
            "gpu": r"\GPU Engine(*)\Utilization Percentage",
        })
        self.base_mhz = (psutil.cpu_freq().max if psutil.cpu_freq() else 0) or 2000
        self.hwm = HardwareMonitorWMI()
        self.hwinfo = HWiNFOShared()
        self.oem = AsusReadOnly()
        self.caps = {"nvidia": bool(self.nvml.lib) and os.environ.get("BLOB_NO_NVML") != "1",
                     "fans_readable": False}
        self.cpu_source = "none"
        self.lock = threading.Lock()
        self.state = {}
        self.nv_last, self.nv_data, self.nv_time = 0.0, {}, 0.0
        self.ssd, self.ssd_time = [], 0.0
        psutil.cpu_percent()
        cpu = reg_str(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
        model = reg_str(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemProductName")
        nv = next((n for t, n in self.adapters if "NVIDIA" in n.upper()), "")
        igpu = next((n for t, n in self.adapters if "NVIDIA" not in n.upper()), "")
        self.device = {"model": model.replace("_", " ").strip(), "cpu": cpu.split(" w/")[0],
                       "gpu": nv, "igpu": igpu.replace("(TM)", "")}

    def gpu_loads(self):
        per = {}
        for name, v in self.pdh.values("gpu").items():
            try:
                luid = name.split("_luid_")[1][:21].lower()
                eng = name.rsplit("engtype_", 1)[1]
            except IndexError:
                continue
            per.setdefault(luid, {}).setdefault(eng, 0.0)
            per[luid][eng] += v
        loads = {luid: min(100.0, max(e.values())) for luid, e in per.items()}
        # Prefer a discrete NVIDIA adapter when present; otherwise the first real
        # WDDM adapter is the primary GPU.  This keeps AMD- and Intel-only systems
        # out of the old "iGPU detail only" dead end.
        primary = next((t for t, n in self.adapters if "NVIDIA" in n.upper()), None)
        if primary is None:
            candidates = [t for t, n in self.adapters
                          if "MICROSOFT" not in n.upper() and "BASIC" not in n.upper()]
            primary = max(candidates, key=lambda tag: loads.get(tag, 0.0)) if candidates else None
        other = next((t for t, _ in self.adapters if t != primary), None)
        return (loads.get(primary, 0.0) if primary else None,
                loads.get(other, 0.0) if other else None)

    def sample(self):
        now = time.time()
        self.pdh.collect()
        cpu_temp = None
        fans = []
        self.cpu_source = "none"
        self.caps["fans_readable"] = False
        extra = merge_sensor_feeds(("hwmonitor", self.hwm.poll()), ("hwinfo", self.hwinfo.poll()))
        if extra.get("cpu") is None or not extra["fans"]:
            fallback = self.oem.poll()
            if extra["fans"]:
                fallback = dict(fallback, fans=[], fan_names=[])
            extra = merge_sensor_feeds((extra.get("cpu_source", "hwmonitor"), extra), ("asus", fallback))
        self.cpu_source = extra.get("cpu_source", "none")
        if cpu_temp is None and extra.get("cpu"):
            cpu_temp = round(extra["cpu"])
        if not fans and extra.get("fans"):
            names = extra.get("fan_names", [])
            fans = [{"name": names[i] if i < len(names) else f"Fan {i + 1}", "rpm": round(rpm),
                     "source": extra["fan_sources"][i], "id": extra["fan_ids"][i]}
                    for i, rpm in enumerate(extra["fans"])]
            self.caps["fans_readable"] = True
        perf = self.pdh.values("perf").get("_Total")
        dgpu_load, igpu_load = self.gpu_loads()

        # dGPU: only talk to NVML when it's already awake, so we never keep it from sleeping.
        awake = self.nv_power.awake() if self.caps["nvidia"] else False
        busy = (dgpu_load or 0) > 1
        if awake and (now - self.nv_last) >= (2 if busy else 10):
            self.nv_last = now
            r = self.nvml.read()
            if r:
                self.nv_data, self.nv_time = r, now
        if awake is False:
            gstate = "sleeping"
        elif awake and now - self.nv_time < 4:
            gstate = "active"
        elif self.nv_data:
            gstate = "idle"
        else:
            gstate = "unavailable" if awake is None else "idle"
        if not self.caps["nvidia"]:
            temp = extra.get("gpu")
            gstate = "active" if temp else "unavailable"
            self.nv_data = {"temp": round(temp)} if temp else {}
        gpu = {"state": gstate, "temp": self.nv_data.get("temp") if gstate in ("active", "idle") else None,
               "load": dgpu_load if dgpu_load is not None else self.nv_data.get("load"),
               "power": self.nv_data.get("power") if gstate == "active" else None,
               "clock": self.nv_data.get("clock") if gstate == "active" else None,
               "stale": gstate != "active"}

        if now - self.ssd_time > 5:
            self.ssd, self.ssd_time = nvme_temps(), now
        sensors = []
        for d in self.ssd:
            label = "SSD" if len(self.ssd) == 1 else f"SSD {d['drive']}"
            sensors.append({"name": label, "value": d["temps"][0], "unit": "°C", "temp": True,
                            "warn": d["warn"]})
            if len(d["temps"]) > 1:
                sensors.append({"name": f"{label} controller", "value": max(d["temps"][1:]), "unit": "°C",
                                "temp": True, "warn": d["warn"]})
        for name, kelvin in sorted(self.pdh.values("zones").items()):
            if kelvin > 200:
                sensors.append({"name": "Motherboard" if "TZ01" in name.upper() else "Thermal zone " + name[-4:],
                                "value": round(kelvin - 273.15), "unit": "°C", "temp": True})
        if cpu_temp is None:
            zones = [v for v in self.pdh.values("zones").values() if v > 200]
            if zones:
                cpu_temp = round(max(zones) - 273.15)
                s_cpu = "thermal zone"
                self.cpu_source = "zone"
        batt = psutil.sensors_battery()
        mem = psutil.virtual_memory()
        if igpu_load is not None:
            sensors.append({"name": self.device["igpu"] or "iGPU", "value": round(igpu_load), "unit": "%"})
        sensors.append({"name": "Memory", "value": round(mem.percent), "unit": "%",
                        "detail": f"{mem.used / 2**30:.1f} / {mem.total / 2**30:.0f} GB"})

        s = {
            "t": now,
            "device": self.device,
            "caps": self.caps,
            "cpu_source": self.cpu_source,
            "cpu": {"temp": cpu_temp, "load": psutil.cpu_percent(),
                    "clock": round(self.base_mhz * perf / 100) if perf else None},
            "gpu": gpu,
            "fans": fans,
            "sensors": sensors,
            "power": {"plugged": bool(batt.power_plugged) if batt else True,
                      "battery": round(batt.percent) if batt else None,
                      "secsleft": batt.secsleft if batt and batt.secsleft > 0 else None},
        }
        # Retain a neutral compatibility shape for older layout/test consumers.
        s["control"] = {"mode": None, "active": None, "reason": ""}
        with self.lock:
            self.state = s

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def run(self):
        while True:
            t0 = time.time()
            try:
                self.sample()
            except Exception:
                log("sample: " + traceback.format_exc())
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))
