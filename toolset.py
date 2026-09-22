"""Small, persistent utility models used by Blob's hover tool layer.

The calculator deliberately evaluates a restricted Python expression tree.  It
does not use ``eval`` and it never executes attributes, imports, or arbitrary
calls from the expression field.  The data file contains only calculator
sessions and calculation history, so the tool remains useful after a restart.
"""
from __future__ import annotations

import ast
import ctypes
import hashlib
import json
import math
import random
import struct
import threading
import urllib.request
import os
import tempfile
import time
import uuid
from ctypes import wintypes
from pathlib import Path

import engine


def _finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("result is not finite")
    return value


def _format_number(value):
    value = _finite(value)
    if value == 0:
        value = 0.0
    return f"{value:.12g}"


class SafeCalculator:
    """One calculator workspace with a scientific, safe expression parser."""

    FUNCTION_NAMES = {
        "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh",
        "sqrt", "cbrt", "ln", "log", "log10", "exp", "abs", "floor", "ceil", "factorial",
        "asinh", "acosh", "atanh", "root",
    }
    CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}

    def __init__(self, state=None):
        state = state or {}
        self.expression = str(state.get("expression", ""))
        self.result = str(state.get("result", "0"))
        self.angle_mode = str(state.get("angle_mode", "DEG")).upper()
        if self.angle_mode not in ("DEG", "RAD"):
            self.angle_mode = "DEG"
        self.last_was_eval = bool(state.get("last_was_eval", False))
        try:
            self.memory = _finite(state.get("memory", 0.0) or 0.0)
        except (ValueError, TypeError):
            self.memory = 0.0
        self.second = bool(state.get("second", False))
        self.CONSTANTS = dict(type(self).CONSTANTS)

    def to_dict(self):
        return {"expression": self.expression, "result": self.result,
                "angle_mode": self.angle_mode, "last_was_eval": self.last_was_eval,
                "memory": self.memory, "second": self.second}

    @staticmethod
    def _source(expression):
        source = str(expression or "").strip()
        source = source.replace("×", "*").replace("÷", "/").replace("−", "-")
        source = source.replace("π", "pi").replace("√", "sqrt")
        source = source.replace("^", "**")
        # A percent button should behave like a familiar calculator percent
        # in simple expressions while preserving modulo when it is followed by
        # a second operand (the parser accepts both forms).
        source = source.replace("%", "/100")
        return source

    def evaluate(self, expression=None):
        source = self._source(self.expression if expression is None else expression)
        if not source:
            return "0"
        if len(source) > 2048:
            raise ValueError("expression is too long")
        tree = ast.parse(source, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 256:
            raise ValueError("expression is too complex")
        value = self._eval_node(tree.body)
        return _format_number(value)

    def _eval_node(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return _finite(node.value)
        if isinstance(node, ast.Name) and node.id in self.CONSTANTS:
            return self.CONSTANTS[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self._eval_node(node.operand)
            return _finite(value if isinstance(node.op, ast.UAdd) else -value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult,
                                                                  ast.Div, ast.Pow, ast.Mod)):
            left, right = self._eval_node(node.left), self._eval_node(node.right)
            if isinstance(node.op, ast.Add):
                return _finite(left + right)
            if isinstance(node.op, ast.Sub):
                return _finite(left - right)
            if isinstance(node.op, ast.Mult):
                return _finite(left * right)
            if isinstance(node.op, ast.Div):
                if right == 0:
                    raise ValueError("cannot divide by zero")
                return _finite(left / right)
            if isinstance(node.op, ast.Pow):
                if abs(right) > 1000:
                    raise ValueError("exponent is too large")
                return _finite(left ** right)
            return _finite(left % right)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            if name == "root" and len(node.args) == 2:
                value, degree = (self._eval_node(arg) for arg in node.args)
                if degree == 0 or (value < 0 and (degree != int(degree) or int(degree) % 2 == 0)):
                    raise ValueError("root is outside the real domain")
                return _finite(math.copysign(abs(value) ** (1 / degree), value))
            if name not in self.FUNCTION_NAMES or len(node.args) != 1:
                raise ValueError("function is not allowed")
            value = self._eval_node(node.args[0])
            if name in ("sin", "cos", "tan"):
                value = math.radians(value) if self.angle_mode == "DEG" else value
                return _finite(getattr(math, name)(value))
            if name in ("sinh", "cosh", "tanh", "asinh", "acosh", "atanh"):
                return _finite(getattr(math, name)(value))
            if name in ("asin", "acos", "atan"):
                value = _finite(getattr(math, name)(value))
                return _finite(math.degrees(value) if self.angle_mode == "DEG" else value)
            if name == "sqrt":
                if value < 0:
                    raise ValueError("square root needs a positive value")
                return _finite(math.sqrt(value))
            if name == "cbrt":
                return _finite(math.copysign(abs(value) ** (1 / 3), value))
            if name == "ln":
                return _finite(math.log(value))
            if name in ("log", "log10"):
                return _finite(math.log10(value))
            if name == "exp":
                return _finite(math.exp(value))
            if name == "abs":
                return _finite(abs(value))
            if name == "floor":
                return _finite(math.floor(value))
            if name == "ceil":
                return _finite(math.ceil(value))
            if name == "factorial":
                if value < 0 or value > 170 or value != int(value):
                    raise ValueError("factorial needs a whole number from 0 to 170")
                return _finite(math.factorial(int(value)))
        raise ValueError("expression contains an unsupported operation")

    def press(self, token):
        token = str(token)
        if token == "mc":
            self.memory = 0.0
            return None
        if token == "m+":
            try:
                self.memory += float(self.evaluate())
            except (ArithmeticError, TypeError, ValueError, SyntaxError):
                pass
            return None
        if token == "m−":
            try:
                self.memory -= float(self.evaluate())
            except (ArithmeticError, TypeError, ValueError, SyntaxError):
                pass
            return None
        if token == "mr":
            self.expression = _format_number(self.memory)
            self.last_was_eval = False
            return None
        if token == "2nd":
            self.second = not self.second
            return None
        if token == "Deg":
            self.angle_mode = "RAD" if self.angle_mode == "DEG" else "DEG"
            return None
        if token == "Rand":
            self.expression = _format_number(random.random())
            self.last_was_eval = False
            return None
        operand = self.result if self.last_was_eval and self.result != "Error" else self.expression
        unary = {"x²": "({})^2", "x³": "({})^3", "eˣ": "exp({})", "10ˣ": "10^({})",
                 "1/x": "1/({})", "²√x": "sqrt({})", "³√x": "cbrt({})", "x!": "factorial({})"}
        if token == "ʸ√x" and operand:
            self.expression, self.last_was_eval = f"root({operand},", False
            return None
        if token in unary and operand:
            self.expression, self.last_was_eval = unary[token].format(operand), False
            return None
        if token in self.FUNCTION_NAMES and operand and not operand.endswith(("+", "−", "×", "÷", "(", ",")):
            self.expression, self.last_was_eval = f"{token}({operand})", False
            return None
        shortcuts = {"x²": "^2", "x³": "^3", "xʸ": "^", "eˣ": "exp(",
                     "10ˣ": "10**(", "1/x": "1/(", "²√x": "sqrt(",
                     "³√x": "cbrt(", "ʸ√x": "cbrt(", "x!": "factorial(",
                     "log₁₀": "log10(", "EE": "*10^"}
        if token in shortcuts:
            token = shortcuts[token]
        if token == "AC":
            self.expression, self.result, self.last_was_eval = "", "0", False
            return None
        if token == "DEL":
            self.expression = self.expression[:-1]
            self.last_was_eval = False
            return None
        if token == "ANGLE":
            self.angle_mode = "RAD" if self.angle_mode == "DEG" else "DEG"
            return None
        if token == "=" or token == "EQUALS":
            expression = self.expression.strip()
            if not expression:
                return None
            try:
                expression += ")" * max(0, expression.count("(") - expression.count(")"))
                self.expression = expression
                self.result = self.evaluate(expression)
                self.last_was_eval = True
                return expression, self.result
            except (ArithmeticError, SyntaxError, TypeError, ValueError, RecursionError):
                self.result = "Error"
                self.last_was_eval = True
                return expression, self.result
        if token == "±":
            if self.expression:
                self.expression = f"-({self.expression})"
            elif self.result not in ("0", "Error"):
                self.expression = f"-({self.result})"
            self.last_was_eval = False
            return None
        if token == "sqrt":
            token = "√("
        elif token in self.FUNCTION_NAMES:
            token += "("
        if self.last_was_eval and token not in ("+", "−", "-", "×", "*", "÷", "/", "^", "%"):
            self.expression = ""
        if self.last_was_eval and token in ("+", "−", "-", "×", "*", "÷", "/", "^", "%"):
            self.expression = self.result if self.result != "Error" else ""
        self.expression += token
        self.last_was_eval = False
        return None


class ToolController:
    """Persistent calculator sessions and their history."""

    def __init__(self, storage_path=None):
        self.path = Path(storage_path or (Path(engine.DATA_DIR) / "tools.json"))
        self.sessions = {}
        self.history = []
        self.active_id = None
        self.mode = "scientific"
        self.notes = ""
        self.convert_kind, self.convert_from, self.convert_to = "Length", "m", "ft"
        self.rates, self.rate_date, self.rate_checked = {}, "", ""
        self.rate_status, self._rate_pending, self._rate_loading = "", None, False
        self._load()
        if not self.sessions:
            self.new_calculator(persist=False)

    @property
    def calculator(self):
        if self.active_id not in self.sessions:
            self.new_calculator(persist=False)
        return self.sessions[self.active_id]

    def new_calculator(self, persist=True):
        ident = uuid.uuid4().hex[:10]
        self.sessions[ident] = SafeCalculator()
        self.active_id = ident
        if persist:
            self.save()
        return ident

    def set_mode(self, mode):
        mode = {"calc": "scientific", "fx": "convert"}.get(str(mode), str(mode))
        self.mode = mode if mode in ("basic", "scientific", "notes", "convert") else "scientific"
        self.save()

    def press(self, token):
        if self.mode == "convert" and token in ("=", "EQUALS"):
            result = self.conversion()
            if result not in ("—", "Rates unavailable"):
                expression = f"{self.calculator.expression or '0'} {self.convert_from} → {self.convert_to}"
                self.history.append({"id": self.active_id, "expression": expression,
                                     "result": result, "time": int(time.time()), "kind": "conversion"})
                self.calculator.result = self.calculator.evaluate()
                self.calculator.last_was_eval = True
                self.save()
                return expression, result
            return None
        result = self.calculator.press(token)
        if result:
            expression, value = result
            self.history.append({"id": self.active_id, "expression": expression,
                                 "result": value, "time": int(time.time()), "angle_mode": self.calculator.angle_mode})
        self.save()
        return result

    def recent(self, limit=4):
        limit = max(0, int(limit))
        return list(reversed(self.history[-limit:])) if limit else []

    def recall(self, index):
        rows = self.recent(len(self.history))
        if not 0 <= index < len(rows):
            return
        entry = rows[index]
        self.calculator.expression = str(entry.get("result", "0") if entry.get("kind") == "conversion" else entry.get("expression", ""))
        self.calculator.result = str(entry.get("result", "0"))
        if entry.get("angle_mode") in ("RAD", "DEG"):
            self.calculator.angle_mode = entry["angle_mode"]
        self.calculator.last_was_eval = True
        self.save()

    def note_results(self):
        calc = SafeCalculator({"angle_mode": self.calculator.angle_mode})
        rows = []
        for line in self.notes.splitlines():
            try:
                source = line.strip()
                if not source:
                    rows.append("")
                    continue
                name, sep, expression = source.partition("=")
                if sep and name.strip().isidentifier():
                    name = name.strip()
                    if name in calc.FUNCTION_NAMES or name.startswith("_"):
                        raise ValueError("reserved name")
                    value = calc.evaluate(expression)
                    calc.CONSTANTS[name] = float(value)
                else:
                    value = calc.evaluate(source.rstrip("="))
                rows.append(value)
            except (ValueError, SyntaxError, TypeError, ArithmeticError, RecursionError):
                rows.append("—")
        return rows

    UNITS = {
        "Length": {"m": 1, "cm": .01, "mm": .001, "km": 1000, "in": .0254, "ft": .3048, "yd": .9144, "mi": 1609.344},
        "Mass": {"kg": 1, "g": .001, "lb": .45359237, "oz": .028349523125, "t": 1000},
        "Temperature": {"°C": 1, "°F": 1, "K": 1},
        "Volume": {"L": 1, "mL": .001, "m³": 1000, "US gal": 3.785411784, "US fl oz": .0295735295625},
        "Area": {"m²": 1, "cm²": .0001, "km²": 1e6, "ft²": .09290304, "acre": 4046.8564224, "ha": 10000},
        "Speed": {"m/s": 1, "km/h": 1/3.6, "mph": .44704, "kn": .514444444444},
        "Currency": dict.fromkeys(("USD", "MXN", "EUR", "GBP", "CAD", "JPY", "CHF", "AUD", "CNY"), 1),
    }

    def select_conversion(self, part, value):
        if part == "kind" and value in self.UNITS:
            self.convert_kind = value
            self.convert_from, self.convert_to = list(self.UNITS[value])[:2]
        elif part in ("from", "to") and value in self.UNITS[self.convert_kind]:
            setattr(self, "convert_" + part, value)
        if self.convert_kind == "Currency":
            self.refresh_rates()
        self.save()

    def conversion(self):
        try:
            value = float(self.calculator.evaluate())
            source, dest = self.convert_from, self.convert_to
            if self.convert_kind == "Temperature":
                celsius = (value-32)*5/9 if source == "°F" else value-273.15 if source == "K" else value
                if celsius < -273.15:
                    raise ValueError("below absolute zero")
                result = celsius*9/5+32 if dest == "°F" else celsius+273.15 if dest == "K" else celsius
            elif self.convert_kind == "Currency":
                if source == dest:
                    return _format_number(value)
                if source not in self.rates or dest not in self.rates:
                    return "Rates unavailable"
                result = value / self.rates[source] * self.rates[dest]
            else:
                units = self.UNITS[self.convert_kind]
                result = value * units[source] / units[dest]
            return _format_number(result)
        except (ValueError, TypeError, SyntaxError, ArithmeticError, KeyError, RecursionError):
            return "—"

    def refresh_rates(self, force=False):
        today = time.strftime("%Y-%m-%d")
        if self._rate_loading or (not force and self.rate_checked == today and self.rates):
            return
        self._rate_loading, self.rate_status = True, "Updating rates…"
        def fetch():
            try:
                # Public reference rates only; no calculations or notes leave the device.
                # API contract: https://frankfurter.dev/
                url = "https://api.frankfurter.app/latest?from=USD"
                request = urllib.request.Request(url, headers={"User-Agent": "Blob/4.0 (desktop calculator)", "Accept": "application/json"})
                with urllib.request.urlopen(request, timeout=6) as response:
                    data = json.loads(response.read(200000))
                if data.get("base") != "USD":
                    raise ValueError("unexpected base")
                rates = {"USD": 1.0}
                for quote, value in data["rates"].items():
                    rate = _finite(value)
                    if rate > 0 and quote in self.UNITS["Currency"]:
                        rates[quote] = rate
                if len(rates) < 2:
                    raise ValueError("empty rates")
                self._rate_pending = (rates, str(data["date"]), today)
            except Exception:
                self._rate_pending = False
        threading.Thread(target=fetch, daemon=True, name="Blob currency rates").start()

    def poll_rates(self):
        if self._rate_pending is None:
            return False
        pending, self._rate_pending = self._rate_pending, None
        self._rate_loading = False
        if pending:
            self.rates, self.rate_date, self.rate_checked = pending
            self.rate_status = ""
            self.save()
        else:
            self.rate_status = "Offline · cached rates" if self.rates else "Rates unavailable · retry"
        return True

    def save(self):
        payload = {"version": 2, "active_id": self.active_id, "mode": self.mode,
                   "sessions": {key: value.to_dict() for key, value in self.sessions.items()},
                   "history": self.history, "notes": self.notes,
                   "convert": [self.convert_kind, self.convert_from, self.convert_to],
                   "rates": self.rates, "rate_date": self.rate_date, "rate_checked": self.rate_checked}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix="blob-tools-", suffix=".tmp",
                                              dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        except (OSError, TypeError, ValueError):
            # Calculator use must never make Blob fail to launch because a
            # profile directory is read-only or a previous file is malformed.
            pass

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            sessions = data.get("sessions", {})
            if isinstance(sessions, dict):
                for ident, state in sessions.items():
                    if isinstance(state, dict):
                        self.sessions[str(ident)] = SafeCalculator(state)
            history = data.get("history", [])
            if isinstance(history, list):
                self.history = [row for row in history if isinstance(row, dict)]
            active = str(data.get("active_id", ""))
            self.active_id = active if active in self.sessions else None
            mode = data.get("mode", "scientific")
            self.mode = {"calc": "scientific", "fx": "convert"}.get(mode, mode)
            if self.mode not in ("basic", "scientific", "notes", "convert"):
                self.mode = "scientific"
            self.notes = str(data.get("notes", ""))[:16000]
            choices = data.get("convert", [])
            if len(choices) == 3 and choices[0] in self.UNITS and all(x in self.UNITS[choices[0]] for x in choices[1:]):
                self.convert_kind, self.convert_from, self.convert_to = choices
            rates = data.get("rates", {})
            if isinstance(rates, dict):
                for key, value in rates.items():
                    try:
                        if key in self.UNITS["Currency"] and _finite(value) > 0:
                            self.rates[key] = float(value)
                    except (ValueError, TypeError):
                        pass
            self.rate_date, self.rate_checked = str(data.get("rate_date", "")), str(data.get("rate_checked", ""))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.sessions, self.history, self.active_id = {}, [], None


class ClipboardController:
    """Small, local-only clipboard history for Blob's utility palette.

    Clipboard contents are intentionally never written to Blob's settings or
    sent anywhere.  The in-memory session retains recent text, file lists and
    image markers while Blob is running; pinned entries simply survive a clear
    action during that same session.
    """

    MAX_ITEMS = 24
    MAX_IMAGE_BYTES = 32 * 1024 * 1024
    CF_BITMAP = 2
    CF_DIB = 8
    CF_UNICODETEXT = 13
    CF_HDROP = 15
    CF_DIBV5 = 17
    GMEM_MOVEABLE = 0x0002

    def __init__(self):
        self.items = []
        self.tab = "recent"
        self.page = 0
        self.selected_id = None
        self.selected_ids = set()
        self.status = "Watching clipboard on this device"
        self._sequence = None

    @staticmethod
    def _summary(kind, content):
        if kind == "Text":
            compact = " ".join(str(content).split())
            return compact[:180] or "Empty text"
        if kind == "Files":
            paths = list(content or [])
            if not paths:
                return "No files"
            first = os.path.basename(paths[0]) or paths[0]
            return first if len(paths) == 1 else f"{first} + {len(paths)-1} more"
        if kind == "Image":
            return "Image available in Windows clipboard"
        return "Clipboard content"

    @staticmethod
    def _image_dimensions(raw):
        """Return the logical size embedded in a DIB without decoding it."""
        if not isinstance(raw, (bytes, bytearray)) or len(raw) < 12:
            return None
        try:
            _header, width, height = struct.unpack_from("<Iii", raw, 0)
            if width and height:
                return (abs(int(width)), abs(int(height)))
        except struct.error:
            pass
        return None

    @staticmethod
    def _signature(kind, content):
        """Fast dedupe that never serializes a full clipboard image to disk."""
        digest = hashlib.blake2s(digest_size=10)
        digest.update(kind.encode("utf-8", "replace"))
        if isinstance(content, bytes):
            digest.update(len(content).to_bytes(8, "little", signed=False))
            digest.update(content[:4096])
            digest.update(content[-4096:])
        elif isinstance(content, str):
            digest.update(content[:8192].encode("utf-8", "replace"))
        else:
            digest.update(repr(content).encode("utf-8", "replace"))
        return digest.hexdigest()

    @staticmethod
    def _native_sequence():
        try:
            sequence = ctypes.windll.user32.GetClipboardSequenceNumber()
            return int(sequence) if sequence else None
        except (AttributeError, OSError):
            return None

    @classmethod
    def _read_native(cls):
        """Read only formats Blob can describe safely and cheaply."""
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            shell32 = ctypes.windll.shell32
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.CloseClipboard.restype = wintypes.BOOL
            user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
            user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
            user32.GetClipboardData.argtypes = [wintypes.UINT]
            user32.GetClipboardData.restype = wintypes.HANDLE
            kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
            kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
            kernel32.GlobalSize.restype = ctypes.c_size_t
            shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                                wintypes.LPWSTR, wintypes.UINT]
            shell32.DragQueryFileW.restype = wintypes.UINT
        except (AttributeError, OSError):
            return None
        if not user32.OpenClipboard(None):
            return None
        try:
            # A file copy can expose a friendly text representation too; keep
            # the real CF_HDROP payload when it exists so paste stays a file
            # paste in mail/chat clients.
            if (user32.IsClipboardFormatAvailable(cls.CF_UNICODETEXT) and
                    not user32.IsClipboardFormatAvailable(cls.CF_HDROP)):
                handle = user32.GetClipboardData(cls.CF_UNICODETEXT)
                pointer = kernel32.GlobalLock(handle) if handle else None
                if pointer:
                    try:
                        size = int(kernel32.GlobalSize(handle) or 0)
                        chars = max(1, min(100000, size // max(1, ctypes.sizeof(ctypes.c_wchar))))
                        text = ctypes.wstring_at(pointer, chars).split("\0", 1)[0]
                    finally:
                        kernel32.GlobalUnlock(handle)
                    if text:
                        return {"kind": "Text", "content": text}
            if user32.IsClipboardFormatAvailable(cls.CF_HDROP):
                handle = user32.GetClipboardData(cls.CF_HDROP)
                count = int(shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)) if handle else 0
                paths = []
                for index in range(min(count, 64)):
                    length = int(shell32.DragQueryFileW(handle, index, None, 0))
                    if length:
                        buffer = ctypes.create_unicode_buffer(length + 1)
                        shell32.DragQueryFileW(handle, index, buffer, length + 1)
                        if buffer.value:
                            paths.append(buffer.value)
                if paths:
                    return {"kind": "Files", "content": paths}
            # DIB data is self-contained, so Blob can retain it in memory and
            # put the exact image back on the Windows clipboard later. A raw
            # HBITMAP is deliberately treated as live-only: it is process
            # owned and cannot be safely cached as a blob of bytes.
            for fmt in (cls.CF_DIBV5, cls.CF_DIB):
                if not user32.IsClipboardFormatAvailable(fmt):
                    continue
                handle = user32.GetClipboardData(fmt)
                pointer = kernel32.GlobalLock(handle) if handle else None
                if not pointer:
                    continue
                try:
                    size = int(kernel32.GlobalSize(handle) or 0)
                    if size <= 0 or size > cls.MAX_IMAGE_BYTES:
                        return {"kind": "Image", "content": b"", "format": fmt,
                                "byte_size": max(0, size), "live_only": True}
                    raw = ctypes.string_at(pointer, size)
                finally:
                    kernel32.GlobalUnlock(handle)
                return {"kind": "Image", "content": raw, "format": fmt,
                        "dimensions": cls._image_dimensions(raw), "byte_size": len(raw)}
            if user32.IsClipboardFormatAvailable(cls.CF_BITMAP):
                return {"kind": "Image", "content": b"", "live_only": True}
            return {"kind": "Other", "content": ""}
        finally:
            user32.CloseClipboard()

    def ingest(self, payload, now=None):
        """Add a clipboard snapshot.  Public so native-free tests stay honest."""
        if not isinstance(payload, dict):
            return False
        kind = str(payload.get("kind", "Other")).title()
        if kind not in ("Text", "Files", "Image", "Other"):
            kind = "Other"
        content = payload.get("content", "")
        if kind == "Text":
            content = str(content).replace("\r\n", "\n")[:100000]
            if not content:
                return False
        elif kind == "Files":
            content = [str(path) for path in content if str(path)][:64]
            if not content:
                return False
        elif kind == "Image":
            content = bytes(content)[:self.MAX_IMAGE_BYTES] if isinstance(content, (bytes, bytearray)) else b""
        else:
            content = ""
        signature = self._signature(kind, content)
        now = int(time.time() if now is None else now)
        for index, item in enumerate(self.items):
            if item["signature"] == signature:
                item["time"] = now
                if index:
                    self.items.insert(0, self.items.pop(index))
                self.selected_id = item["id"]
                self.selected_ids = {item["id"]}
                self.status = "Clipboard updated"
                return False
        item = {"id": uuid.uuid4().hex[:10], "kind": kind, "content": content,
                "summary": self._summary(kind, content), "time": now,
                "pinned": False, "signature": signature}
        if kind == "Image":
            item.update(format=int(payload.get("format", self.CF_DIBV5)),
                        dimensions=payload.get("dimensions") or self._image_dimensions(content),
                        byte_size=int(payload.get("byte_size", len(content)) or 0),
                        live_only=bool(payload.get("live_only", not bool(content))))
        self.items.insert(0, item)
        del self.items[self.MAX_ITEMS:]
        self.selected_id = item["id"]
        self.selected_ids = {item["id"]}
        self.page = 0
        self.status = "Added to this session"
        return True

    def poll(self, force=False):
        sequence = self._native_sequence()
        if sequence is None or (not force and sequence == self._sequence):
            return False
        payload = self._read_native()
        if payload is None:
            self.status = "Clipboard is busy · try refresh"
            return False
        self._sequence = sequence
        return self.ingest(payload)

    def items_for_tab(self):
        if self.tab == "pinned":
            return [item for item in self.items if item.get("pinned")]
        return list(self.items)

    def page_count(self, page_size=5):
        return max(1, math.ceil(len(self.items_for_tab()) / max(1, int(page_size))))

    def page_items(self, page_size=5):
        page_size = max(1, int(page_size))
        pages = self.page_count(page_size)
        self.page = min(max(0, self.page), pages - 1)
        start = self.page * page_size
        return self.items_for_tab()[start:start + page_size]

    def move_page(self, amount, page_size=5):
        self.page = min(max(0, self.page + int(amount)), self.page_count(page_size) - 1)

    def item(self, ident=None, visible_only=False):
        ident = ident or self.selected_id
        rows = self.items_for_tab() if visible_only else self.items
        for item in rows:
            if item["id"] == ident:
                return item
        return rows[0] if rows else None

    def select(self, ident):
        if self.item(ident) is None:
            return None
        self.selected_id = ident
        self.selected_ids = {ident}
        self.status = "Selected · use Copy to place it back on Windows clipboard"
        return self.item(ident)

    def selected_items(self):
        """Items explicitly marked for a grouped text/file paste, in recency order."""
        return [item for item in self.items if item["id"] in self.selected_ids]

    def toggle_selection(self, ident):
        item = self.item(ident)
        if item is None:
            return False
        if ident in self.selected_ids:
            self.selected_ids.remove(ident)
        else:
            self.selected_ids.add(ident)
        marked = self.selected_items()
        self.selected_id = marked[0]["id"] if marked else None
        self.status = (f"{len(marked)} item" + ("s" if len(marked) != 1 else "") + " selected"
                       if marked else "Selection cleared")
        return True

    def set_tab(self, tab):
        self.tab = "pinned" if tab == "pinned" else "recent"
        self.page = 0
        visible = self.items_for_tab()
        visible_ids = {item["id"] for item in visible}
        self.selected_ids.intersection_update(visible_ids)
        if visible and not self.selected_ids:
            self.selected_id = visible[0]["id"]
            self.selected_ids = {self.selected_id}
        elif self.selected_ids:
            self.selected_id = next(item["id"] for item in visible if item["id"] in self.selected_ids)
        else:
            self.selected_id = None

    def toggle_pin(self, ident):
        item = self.item(ident)
        if item is None:
            return False
        item["pinned"] = not bool(item.get("pinned"))
        self.selected_id = item["id"]
        if self.tab == "pinned" and not item["pinned"]:
            self.set_tab("pinned")
        self.status = "Pinned in this session" if item["pinned"] else "Unpinned"
        return True

    def clear_unpinned(self):
        before = len(self.items)
        self.items = [item for item in self.items if item.get("pinned")]
        self.selected_id = self.items[0]["id"] if self.items else None
        self.selected_ids.intersection_update(item["id"] for item in self.items)
        self.page = 0
        removed = before - len(self.items)
        self.status = "Session history cleared" if removed else "No unpinned items to clear"
        return removed

    @staticmethod
    def can_copy(item):
        return bool(item and (item.get("kind") in ("Text", "Files") or
                              (item.get("kind") == "Image" and item.get("content") and
                               not item.get("live_only"))))

    @classmethod
    def can_copy_items(cls, items):
        if not items:
            return False
        if len(items) == 1:
            return cls.can_copy(items[0])
        kinds = {item.get("kind") for item in items}
        return kinds in ({"Text"}, {"Files"})

    @classmethod
    def _write_payload(cls, fmt, raw):
        """Transfer one owned GMEM block to Windows Clipboard on success."""
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.EmptyClipboard.restype = wintypes.BOOL
            user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
            user32.SetClipboardData.restype = wintypes.HANDLE
            kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel32.GlobalAlloc.restype = wintypes.HANDLE
            kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
            kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
        except (AttributeError, OSError):
            return False
        raw = bytes(raw)
        if not raw:
            return False
        handle = kernel32.GlobalAlloc(cls.GMEM_MOVEABLE, len(raw))
        pointer = kernel32.GlobalLock(handle) if handle else None
        if not pointer:
            if handle:
                kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, raw, len(raw))
        kernel32.GlobalUnlock(handle)
        if not user32.OpenClipboard(None):
            kernel32.GlobalFree(handle)
            return False
        transferred = False
        try:
            if user32.EmptyClipboard() and user32.SetClipboardData(int(fmt), handle):
                transferred = True
                return True
            return False
        finally:
            user32.CloseClipboard()
            if not transferred:
                kernel32.GlobalFree(handle)

    @classmethod
    def _write_text(cls, text):
        return cls._write_payload(cls.CF_UNICODETEXT, (str(text)[:100000] + "\0").encode("utf-16-le"))

    @classmethod
    def _write_files(cls, paths):
        """Restore a real CF_HDROP payload so destination apps receive files, not text paths."""
        clean = [str(path) for path in paths if str(path)][:64]
        if not clean:
            return False
        paths_raw = ("\0".join(clean) + "\0\0").encode("utf-16-le")
        # DROPFILES = DWORD offset; POINT x/y; BOOL nonclient; BOOL Unicode.
        header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
        return cls._write_payload(cls.CF_HDROP, header + paths_raw)

    def copy(self, ident=None):
        item = self.item(ident)
        if not item:
            self.status = "Nothing selected"
            return False
        if item["kind"] == "Text":
            text = item["content"]
            copied = self._write_text(text)
        elif item["kind"] == "Files":
            copied = self._write_files(item["content"])
        elif item["kind"] == "Image" and item.get("content") and not item.get("live_only"):
            copied = self._write_payload(item.get("format", self.CF_DIBV5), item["content"])
        else:
            self.status = "This image was too large to cache · use the live clipboard"
            return False
        self.status = "Copied to Windows clipboard" if copied else "Clipboard is busy · try again"
        return copied

    def copy_selected(self):
        """Paste compatible selections as one text or real file-drop payload."""
        items = self.selected_items()
        if len(items) <= 1:
            return self.copy(items[0]["id"] if items else None)
        kinds = {item.get("kind") for item in items}
        if kinds == {"Text"}:
            copied = self._write_text("\r\n\r\n".join(str(item["content"]) for item in items))
        elif kinds == {"Files"}:
            copied = self._write_files([path for item in items for path in item["content"]])
        else:
            self.status = "Copy one image at a time, or select only text or files"
            return False
        self.status = (f"Copied {len(items)} items to Windows clipboard"
                       if copied else "Clipboard is busy · try again")
        return copied
