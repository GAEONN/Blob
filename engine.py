"""Blob engine — sensor reading and fan-profile control
for the ASUS TUF Gaming A14 (works on most ASUS laptops with the ATKACPI driver).

Sensors (no admin rights needed):
  * CPU temperature + CPU/GPU fan RPM  -> ASUS ATKACPI driver (same interface Armoury Crate uses)
  * NVIDIA GPU temp/load/power         -> NVML, only polled while the dGPU is already awake
  * Motherboard ACPI thermal zone      -> Windows performance counters
  * NVMe SSD temperatures              -> IOCTL_STORAGE_QUERY_PROPERTY
Control:
  * ASUS performance profiles (Silent / Balanced / Turbo), which switch the BIOS fan
    curves + power limits. "Auto" picks the profile from live load and temperature.
"""
import ctypes
import json
import subprocess
import os
import struct
import threading
import time
import traceback
import winreg
from ctypes import wintypes

import psutil

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "Blob")
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


# ─────────────────────────── ASUS ATKACPI ───────────────────────────
class Asus:
    DSTS, DEVS = 0x53545344, 0x53564544
    CPU_FAN, GPU_FAN, CPU_TEMP, MODE = 0x00110013, 0x00110014, 0x00120094, 0x00120075
    CPU_CURVE, GPU_CURVE = 0x00110024, 0x00110025
    MODE_VALUE = {"balanced": 0, "turbo": 1, "silent": 2}
    CURVE_ARG = {"balanced": 0, "silent": 1, "turbo": 2}  # ASUS swaps these for curve reads

    def __init__(self):
        self.lock = threading.Lock()
        self.h = open_device("\\\\.\\ATKACPI")
        if not self.h:
            log("ATKACPI not available")

    def _call(self, method, args, outlen=16):
        if not self.h:
            return None
        with self.lock:
            return ioctl(self.h, 0x0022240C, struct.pack("<II", method, len(args)) + args, outlen)

    def get(self, dev):
        r = self._call(self.DSTS, struct.pack("<II", dev, 0))
        if not r or len(r) < 4:
            return None
        v = struct.unpack_from("<i", r)[0] - 65536
        return v if v >= 0 else None

    def fan_rpm(self, dev):
        v = self.get(dev)
        return None if v is None else v * 100

    def set_mode(self, name):
        r = self._call(self.DEVS, struct.pack("<II", self.MODE, self.MODE_VALUE[name]))
        ok = bool(r) and struct.unpack_from("<i", r)[0] == 1
        log(f"set mode {name}: {'ok' if ok else 'FAILED'}")
        return ok

    def curves(self):
        out = {}
        for mode, arg in self.CURVE_ARG.items():
            out[mode] = {}
            for fan, dev in (("cpu", self.CPU_CURVE), ("gpu", self.GPU_CURVE)):
                r = self._call(self.DSTS, struct.pack("<II", dev, arg), 32)
                if r and len(r) >= 16 and any(r[:8]):
                    out[mode][fan] = [[r[i], r[8 + i]] for i in range(8)]
        return out


# ─────────────────────────── NVMe temperatures ───────────────────────────
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


# ─────────────────────────── Fan-profile controller ───────────────────────────
class Controller:
    MODES = ("auto", "silent", "balanced", "turbo")
    RANK = {"silent": 0, "balanced": 1, "turbo": 2}

    def __init__(self, asus):
        self.asus = asus
        self.cfg = load_config()
        self.mode = self.cfg.get("mode", "auto")
        if self.mode not in self.MODES:
            self.mode = "auto"
        self.active = None  # profile currently applied to the BIOS
        self.reason = ""
        self.candidate, self.cand_since, self.last_switch = None, 0.0, 0.0
        self.ema = {}
        self.plugged = None
        self.last_tick = time.time()
        if self.mode != "auto":
            self._apply(self.mode)

    def _apply(self, profile):
        if not self.asus.h:
            return
        if self.asus.set_mode(profile):
            self.active = profile
            self.last_switch = time.time()

    def set_mode(self, mode):
        if mode not in self.MODES:
            return False
        self.mode = mode
        self.cfg["mode"] = mode
        save_config(self.cfg)
        self.candidate = None
        if mode != "auto":
            self._apply(mode)
            self.reason = ""
        else:
            self.active = None  # next tick applies immediately
        return True

    def _smooth(self, key, value, alpha=0.18):
        if value is None:
            return self.ema.get(key)
        prev = self.ema.get(key)
        self.ema[key] = value if prev is None else prev + alpha * (value - prev)
        return self.ema[key]

    def tick(self, s):
        now = time.time()
        resumed = now - self.last_tick > 15  # woke from sleep
        self.last_tick = now
        cpu_load = self._smooth("cpu_load", s["cpu"]["load"])
        cpu_temp = self._smooth("cpu_temp", s["cpu"]["temp"])
        gpu_load = self._smooth("gpu_load", s["gpu"]["load"] if s["gpu"]["state"] == "active" else 0)
        gpu_temp = s["gpu"]["temp"] if s["gpu"]["state"] == "active" else None
        plugged = s["power"]["plugged"]
        power_changed = self.plugged is not None and plugged != self.plugged
        self.plugged = plugged

        # Armoury Crate re-applies its own profile on AC/DC changes and resume; take it back.
        if (power_changed or resumed) and self.active:
            self._apply(self.active)

        if self.mode != "auto":
            return
        cpu_load, cpu_temp, gpu_load = cpu_load or 0, cpu_temp or 0, gpu_load or 0
        if cpu_temp >= 90 or (gpu_temp or 0) >= 83:
            target, why = "turbo", "running hot"
        elif gpu_load >= 40:
            target, why = "turbo", "GPU busy"
        elif cpu_load >= 55:
            target, why = "turbo", "heavy CPU load"
        elif cpu_load < 15 and gpu_load < 15 and cpu_temp < 72:
            target, why = "silent", "light load"
        else:
            target, why = "balanced", "moderate load"
        if not plugged and target == "turbo":
            target, why = "balanced", "on battery"

        if self.active is None:
            self._apply(target)
            self.reason = why
            return
        if target == self.active:
            self.candidate = None
            self.reason = why
            return
        if target != self.candidate:
            self.candidate, self.cand_since = target, now
        going_up = self.RANK[target] > self.RANK[self.active]
        hold = 6 if going_up else 25  # ramp up fast, calm down slowly
        if now - self.cand_since >= hold and now - self.last_switch >= 8:
            self._apply(target)
            self.candidate = None
            self.reason = why
        else:
            self.reason = f"{why}, switching to {target.title()}…"


# ─────────────────────────── Sampler ───────────────────────────
class HardwareMonitorWMI:
    """Optional extra sensors from LibreHardwareMonitor / OpenHardwareMonitor, when the user
    happens to run one. That covers Intel and AMD machines, where Windows exposes nothing."""

    NAMESPACES = ((r"root\LibreHardwareMonitor", "LibreHardwareMonitor"),
                  (r"root\OpenHardwareMonitor", "OpenHardwareMonitor"))

    def __init__(self):
        self.namespace = None
        self.values = {}
        self.checked = 0.0

    def running(self):
        names = {n.lower() for n in ("LibreHardwareMonitor.exe", "OpenHardwareMonitor.exe")}
        return any((p.info["name"] or "").lower() in names for p in psutil.process_iter(["name"]))

    def poll(self):
        """Returns {'cpu': °C, 'gpu': °C, 'fans': [rpm, ...]} — empty if no such app is running."""
        now = time.time()
        if now - self.checked < 2.0:
            return self.values
        self.checked = now
        if self.namespace is None and not self.running():
            self.values = {}
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
                fans = [r["Value"] for r in rows if r.get("SensorType") == "Fan" and r.get("Value")]
                pick = lambda *keys: next((v for k, v in temps.items()
                                           if any(x in k.lower() for x in keys)), None)
                self.namespace = ns
                self.values = {"cpu": pick("cpu package", "core (tctl", "cpu core", "package"),
                               "gpu": pick("gpu core", "gpu temperature", "gpu hot"),
                               "fans": [round(f) for f in fans]}
                return self.values
            except Exception:
                continue
        self.namespace = None
        self.values = {}
        return self.values


class Monitor:
    def __init__(self):
        self.asus = Asus()
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
        self.controller = Controller(self.asus)
        # what this machine can actually do
        self.caps = {"fan_control": bool(self.asus.h) and os.environ.get("BLOB_NO_ASUS") != "1",
                     "asus": bool(self.asus.h) and os.environ.get("BLOB_NO_ASUS") != "1",
                     "nvidia": bool(self.nvml.lib) and os.environ.get("BLOB_NO_NVML") != "1"}
        self.cpu_source = "asus" if self.caps["asus"] else "none"
        self.lock = threading.Lock()
        self.state = {}
        self.nv_last, self.nv_data, self.nv_time = 0.0, {}, 0.0
        self.ssd, self.ssd_time = [], 0.0
        psutil.cpu_percent()
        cpu = reg_str(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
        model = reg_str(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemProductName")
        nv = next((n for t, n in self.adapters if "NVIDIA" in n.upper()), "NVIDIA GPU")
        igpu = next((n for t, n in self.adapters if "NVIDIA" not in n.upper()), "")
        self.device = {"model": model.split("_")[0].replace("ASUS ", ""), "cpu": cpu.split(" w/")[0],
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
        nv = next((t for t, n in self.adapters if "NVIDIA" in n.upper()), None)
        ig = next((t for t, n in self.adapters if "NVIDIA" not in n.upper()), None)
        return loads.get(nv, 0.0) if nv else None, loads.get(ig, 0.0) if ig else None

    def sample(self):
        now = time.time()
        self.pdh.collect()
        a = self.asus
        asus_ok = self.caps["asus"]
        cpu_temp = a.get(a.CPU_TEMP) if asus_ok else None
        fans = ([{"name": "CPU fan", "rpm": a.fan_rpm(a.CPU_FAN)},
                 {"name": "GPU fan", "rpm": a.fan_rpm(a.GPU_FAN)}] if asus_ok else [])
        extra = self.hwm.poll()
        if cpu_temp is None and extra.get("cpu"):
            cpu_temp = round(extra["cpu"])
            self.cpu_source = "hwmonitor"
        if not fans and extra.get("fans"):
            fans = [{"name": f"Fan {i + 1}", "rpm": rpm} for i, rpm in enumerate(extra["fans"][:3])]
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
            temp = (self.hwm.poll() or {}).get("gpu")
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
        self.controller.tick(s)
        c = self.controller
        s["control"] = {"mode": c.mode, "active": c.active, "reason": c.reason}
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
