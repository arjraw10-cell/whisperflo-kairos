"""WhisperFlo Kairos: a small, local Windows hold-to-dictate MVP.

Hold Ctrl+Z+X, speak, and release any one of the keys. The captured audio is
transcribed locally by whisper.cpp and pasted into the focused application.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import time
import wave
from collections import deque

import numpy as np
import sounddevice as sd


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"

# Low-level keyboard hook constants.
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
VK_CONTROL = 0x11
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_Z = 0x5A
VK_X = 0x58
CTRL_KEYS = {VK_CONTROL, VK_LCONTROL, VK_RCONTROL}
VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
SHIFT_KEYS = {VK_SHIFT, VK_LSHIFT, VK_RSHIFT}
VK_SPACE = 0x20
KEY_GROUPS = {
    "CTRL": CTRL_KEYS,
    "SHIFT": SHIFT_KEYS,
    "SPACE": {VK_SPACE},
}
VK_TO_KEY = {vk: name for name, keys in KEY_GROUPS.items() for vk in keys}
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

# Clipboard/constants for keyboard injection.
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    # INPUT's union must include the largest member. Omitting MOUSEINPUT
    # makes sizeof(INPUT) wrong on 64-bit Windows, causing SendInput to return 0.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class Config:
    def __init__(self, data: dict):
        self.model = ROOT / data.get("model", "models/ggml-base.en.bin")
        self.whisper_cli = ROOT / data.get(
            "whisper_cli", "bin/Release/whisper-cli.exe"
        )
        self.language = str(data.get("language", "en"))
        self.threads = max(1, int(data.get("threads", max(1, (os.cpu_count() or 4) // 2))))
        self.device = data.get("device", None)
        self.paste = bool(data.get("paste", True))
        self.restore_clipboard = bool(data.get("restore_clipboard", True))
        self.pre_roll_ms = max(0, int(data.get("pre_roll_ms", 350)))
        self.hotkey = tuple(str(x).upper() for x in data.get("hotkey", ["CTRL", "SHIFT", "SPACE"]))
        if not self.hotkey or any(x not in KEY_GROUPS for x in self.hotkey):
            raise ValueError("hotkey must contain names from CTRL, SHIFT, SPACE")
        # Ctrl/Shift/Space have no edit action, so consuming the active chord
        # avoids accidental shortcuts without risking Undo/Cut.
        self.suppress_chord = bool(data.get("suppress_chord", True))
        self.streaming = False
        self.groq_model = str(data.get("groq_model", "qwen/qwen3.6-27b"))
        self.groq_timeout_s = max(2, int(data.get("groq_timeout_s", 15)))


class Clipboard:
    """Minimal native Unicode-text clipboard support."""

    def __init__(self):
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.user32.OpenClipboard.argtypes = [wintypes.HWND]
        self.user32.GetClipboardData.argtypes = [wintypes.UINT]
        self.user32.GetClipboardData.restype = wintypes.HANDLE
        self.user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self.kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self.kernel32.GlobalLock.restype = ctypes.c_void_p
        self.kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self.kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self.kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        self.kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    def get_text(self) -> str | None:
        if not self.user32.OpenClipboard(None):
            return None
        try:
            handle = self.user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = self.kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                self.kernel32.GlobalUnlock(handle)
        finally:
            self.user32.CloseClipboard()

    def set_text(self, text: str) -> bool:
        encoded_size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
        if not self.user32.OpenClipboard(None):
            return False
        handle = None
        try:
            self.user32.EmptyClipboard()
            handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE, encoded_size)
            if not handle:
                return False
            ptr = self.kernel32.GlobalLock(handle)
            if not ptr:
                self.kernel32.GlobalFree(handle)
                return False
            try:
                ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), encoded_size)
            finally:
                self.kernel32.GlobalUnlock(handle)
            if not self.user32.SetClipboardData(CF_UNICODETEXT, handle):
                self.kernel32.GlobalFree(handle)
                return False
            handle = None  # clipboard owns it now
            return True
        finally:
            if handle:
                self.kernel32.GlobalFree(handle)
            self.user32.CloseClipboard()

    def clear(self) -> None:
        if self.user32.OpenClipboard(None):
            try:
                self.user32.EmptyClipboard()
            finally:
                self.user32.CloseClipboard()


class KeyboardHook:
    """Detect the requested three-key chord from anywhere in Windows.

    The hook runs a Windows message loop on its own thread and communicates
    only via a queue, so audio/transcription never happens inside the hook.
    """

    def __init__(self, events: queue.Queue[str], hotkey: tuple[str, ...], suppress_chord: bool = False):
        self.events = events
        self.hotkey = hotkey
        self.suppress_chord = suppress_chord
        self.pressed: set[int] = set()
        self.active = False
        self.stop_requested = threading.Event()
        self.ready = threading.Event()
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, name="keyboard-hook", daemon=True)
        self._proc = None

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(3):
            raise RuntimeError("Keyboard hook did not start")
        if self.error:
            raise self.error

    def stop(self) -> None:
        self.stop_requested.set()
        # Post WM_QUIT to the hook thread's message queue.
        if self.thread.ident:
            ctypes.windll.user32.PostThreadMessageW(self.thread.ident, 0x0012, 0, 0)
        self.thread.join(timeout=2)

    def _run(self) -> None:
        try:
            # LRESULT is a pointer-sized signed integer; wintypes does not
            # expose it on every Python/Windows build.
            callback_type = ctypes.WINFUNCTYPE(
                LRESULT, ctypes.c_int, WPARAM, LPARAM
            )

            call_next = ctypes.windll.user32.CallNextHookEx
            call_next.argtypes = [wintypes.HHOOK, ctypes.c_int, WPARAM, LPARAM]
            call_next.restype = LRESULT

            @callback_type
            def callback(n_code, w_param, l_param):
                # A low-level hook must never let a Python exception escape:
                # Windows may interpret an invalid callback return as a
                # blocked keyboard event. Always fail open and pass the event
                # to the next hook.
                try:
                    if n_code >= 0:
                        info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        vk = int(info.vkCode)
                        down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                        up = w_param in (WM_KEYUP, WM_SYSKEYUP)
                        tracked = set().union(*(KEY_GROUPS[name] for name in self.hotkey))
                        if vk in tracked:
                            was_active = self.active
                            key_name = VK_TO_KEY[vk]
                            if down:
                                self.pressed.add(key_name)
                            elif up:
                                self.pressed.discard(key_name)

                            now_active = set(self.hotkey).issubset(self.pressed)
                            if now_active and not self.active:
                                self.active = True
                                self.events.put("start")
                            elif self.active and not now_active:
                                self.active = False
                                self.events.put("stop")

                            # Optional: consume the chord so the target app does
                            # not see Undo/Cut. Disabled by default because a hook
                            # failure must never leave the user's keyboard stuck.
                            if self.suppress_chord and (self.active or was_active):
                                return 1
                except Exception:
                    logging.exception("Keyboard hook callback failed; passing event through")
                return call_next(None, n_code, w_param, l_param)

            self._proc = callback
            get_module = ctypes.windll.kernel32.GetModuleHandleW
            get_module.argtypes = [wintypes.LPCWSTR]
            get_module.restype = wintypes.HMODULE
            module = get_module(None)
            set_hook = ctypes.windll.user32.SetWindowsHookExW
            set_hook.argtypes = [ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD]
            set_hook.restype = wintypes.HHOOK
            hook = set_hook(WH_KEYBOARD_LL, callback, module, 0)
            if not hook:
                raise ctypes.WinError()
            self.ready.set()
            msg = wintypes.MSG()
            while not self.stop_requested.is_set():
                result = ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
            ctypes.windll.user32.UnhookWindowsHookEx(hook)
        except Exception as exc:  # surfaced by start()
            self.error = exc
            self.ready.set()


class DictationApp:
    def __init__(self, config: Config):
        self.config = config
        self.events: queue.Queue[str] = queue.Queue()
        self.audio_lock = threading.Lock()
        self.recording = False
        self.blocks: list[np.ndarray] = []
        self.pre_roll: deque[np.ndarray] = deque()
        self.pre_roll_samples = int(16000 * config.pre_roll_ms / 1000)
        self.audio_started_at: float | None = None
        self.audio_error: Exception | None = None
        self.stream: sd.InputStream | None = None
        self.keyboard = KeyboardHook(self.events, config.hotkey, config.suppress_chord)
        self.transcribing = threading.Lock()
        self.temp_dir = Path(tempfile.gettempdir()) / "whisperflo-kairos"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> None:
        self._check_files()
        logging.info("Opening microphone (16 kHz mono)...")
        kwargs = {"samplerate": 16000, "channels": 1, "dtype": "float32", "callback": self._audio_callback}
        if self.config.device is not None:
            kwargs["device"] = self.config.device
        self.stream = sd.InputStream(**kwargs)
        self.stream.start()
        self.keyboard.start()
        logging.info("Ready. Hold %s, speak, then release any key. Press Ctrl+C to quit.", "+".join(self.config.hotkey))
        try:
            while True:
                try:
                    event = self.events.get(timeout=0.25)
                except queue.Empty:
                    continue
                if event == "start":
                    self.start_recording()
                elif event == "stop":
                    self.stop_recording()
        except KeyboardInterrupt:
            logging.info("Stopping...")
        finally:
            self.keyboard.stop()
            if self.recording:
                self.stop_recording()
            if self.stream:
                self.stream.stop()
                self.stream.close()

    def _check_files(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("This MVP currently supports Windows only")
        if not self.config.whisper_cli.exists():
            raise FileNotFoundError(f"Missing whisper executable: {self.config.whisper_cli}")
        if not self.config.model.exists():
            raise FileNotFoundError(
                f"Missing model: {self.config.model}\nRun setup.ps1 to download it."
            )

    def _audio_callback(self, indata, frames, _time, status) -> None:
        if status:
            logging.warning("Audio: %s", status)
        block = np.asarray(indata[:, 0], dtype=np.float32).copy()
        with self.audio_lock:
            self.pre_roll.append(block)
            total = sum(len(x) for x in self.pre_roll)
            while total > self.pre_roll_samples and self.pre_roll:
                removed = self.pre_roll.popleft()
                total -= len(removed)
            if self.recording:
                self.blocks.append(block)

    def start_recording(self) -> None:
        with self.audio_lock:
            self.blocks = list(self.pre_roll)
            self.recording = True
            self.audio_started_at = time.monotonic()
        logging.info("[listening]")

    def stop_recording(self) -> None:
        with self.audio_lock:
            if not self.recording:
                return
            self.recording = False
            samples = np.concatenate(self.blocks) if self.blocks else np.array([], dtype=np.float32)
            self.blocks = []
        if len(samples) < 1600:  # less than 100 ms
            logging.info("[ignored: recording too short]")
            return
        # The final decode must run even if a streaming decode is still in
        # progress. It waits for that decode instead of being dropped.
        threading.Thread(target=self.transcribe, args=(samples,), daemon=True).start()

    def _snapshot_samples(self) -> np.ndarray:
        with self.audio_lock:
            return np.concatenate(self.blocks) if self.blocks else np.array([], dtype=np.float32)

    def transcribe(self, samples: np.ndarray) -> None:
        # Nothing is typed while recording. Release ends recording, then the
        # local transcript is optionally cleaned up by Groq before insertion.
        text = self._decode(samples, "final", wait=True)
        if not text:
            logging.info("[no speech detected]")
            return
        logging.info("[text] %s", text)
        formatted = format_with_groq(text, self.config)
        if formatted:
            text = formatted
            logging.info("[formatted] %s", text)
        type_text(text)

    def _decode(self, samples: np.ndarray, label: str, wait: bool = False) -> str:
        acquired = self.transcribing.acquire(blocking=wait)
        if not acquired:
            return ""
        wav_path = self.temp_dir / f"{label}-{os.getpid()}-{time.time_ns()}.wav"
        try:
            write_wav(wav_path, samples)
            command = [
                str(self.config.whisper_cli),
                "-m", str(self.config.model),
                "-f", str(wav_path),
                "-l", self.config.language,
                "-t", str(self.config.threads),
                "-nt", "-np",
            ]
            logging.info("[%s transcribing]", label)
            result = subprocess.run(
                command, capture_output=True, text=True, cwd=ROOT, timeout=45
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-1200:]
                logging.error("whisper.cpp failed (%s): %s", result.returncode, detail)
                return ""
            return clean_transcription(result.stdout)
        except subprocess.TimeoutExpired:
            logging.warning("[%s transcription timed out]", label)
            return ""
        except Exception:
            logging.exception("Transcription failed")
            return ""
        finally:
            self.transcribing.release()
            try:
                wav_path.unlink(missing_ok=True)
            except Exception:
                pass


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def format_with_groq(text: str, config: Config) -> str:
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        logging.info("[Groq skipped: GROQ_API_KEY is not set]")
        return text
    prompt = (
        "Clean up this speech transcription for direct insertion into a text field. "
        "Fix punctuation, capitalization, spacing, and obvious transcription errors. "
        "Preserve the speaker's exact meaning and wording. Do not add, remove, or "
        "explain anything. Return only the cleaned text.\n\nTRANSCRIPTION:\n" + text
    )
    body = json.dumps({
        "model": config.groq_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_completion_tokens": max(256, len(text.split()) * 4),
        "include_reasoning": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.groq_timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = payload["choices"][0]["message"]["content"].strip()
        return result or text
    except Exception as exc:
        logging.warning("[Groq formatting failed; using raw transcript] %s", exc)
        return text


def write_wav(path: Path, samples: np.ndarray) -> None:
    samples = np.clip(samples, -1.0, 1.0)
    pcm = (samples * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm.tobytes())


def clean_transcription(output: str) -> str:
    lines = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("["):
            continue
        # whisper-cli may include a timestamp prefix even with -nt on some builds.
        if "]" in line and line.startswith("["):
            line = line.split("]", 1)[1].strip()
        lines.append(line)
    return " ".join(lines).strip()


def type_text(text: str) -> None:
    """Type Unicode text directly using correctly sized Windows INPUT structs."""
    if not text:
        return
    inputs = []
    for char in text:
        code = 13 if char == "\n" else ord(char)
        units = [code] if code <= 0xFFFF else [
            0xD800 + ((code - 0x10000) >> 10),
            0xDC00 + ((code - 0x10000) & 0x3FF),
        ]
        for unit in units:
            inputs.append(INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, 0))))
            inputs.append(INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))))
    array = (INPUT * len(inputs))(*inputs)
    send_input = ctypes.windll.user32.SendInput
    send_input.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    send_input.restype = wintypes.UINT
    sent = send_input(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        error = ctypes.get_last_error()
        logging.warning(
            "Could not type all text (SendInput returned %s/%s, WinError %s)",
            sent, len(inputs), error,
        )


def erase_text(text: str) -> None:
    """Delete characters previously emitted by streaming typing."""
    if not text:
        return
    count = len(text)
    inputs = []
    for _ in range(count):
        inputs.append(INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0x08, 0, 0, 0, 0))))
        inputs.append(INPUT(INPUT_KEYBOARD, _INPUTUNION(ki=KEYBDINPUT(0x08, 0, KEYEVENTF_KEYUP, 0, 0))))
    array = (INPUT * len(inputs))(*inputs)
    send_input = ctypes.windll.user32.SendInput
    send_input.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    send_input.restype = wintypes.UINT
    send_input(len(inputs), array, ctypes.sizeof(INPUT))


def paste_text(text: str, restore: bool) -> None:
    clipboard = Clipboard()
    previous = clipboard.get_text() if restore else None
    if not clipboard.set_text(text):
        raise RuntimeError("Could not open the Windows clipboard")
    # Ctrl+V through SendInput is accepted by most normal Windows text fields.
    inputs = (INPUT * 4)()
    inputs[0].type = 1
    inputs[0].ki = KEYBDINPUT(VK_CONTROL, 0, 0, 0, None)
    inputs[1].type = 1
    inputs[1].ki = KEYBDINPUT(0x56, 0, 0, 0, None)
    inputs[2].type = 1
    inputs[2].ki = KEYBDINPUT(0x56, 0, 2, 0, None)
    inputs[3].type = 1
    inputs[3].ki = KEYBDINPUT(VK_CONTROL, 0, 2, 0, None)
    send_input = ctypes.windll.user32.SendInput
    send_input.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    send_input.restype = wintypes.UINT
    sent = send_input(4, inputs, ctypes.sizeof(INPUT))
    if sent != 4:
        logging.warning("Could not inject Ctrl+V (SendInput returned %s)", sent)
    time.sleep(0.15)
    if restore and previous is not None:
        clipboard.set_text(previous)


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config({})
    with path.open("r", encoding="utf-8") as file:
        return Config(json.load(file))


def main() -> int:
    parser = argparse.ArgumentParser(description="Local hold-to-talk Whisper dictation")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--no-paste", action="store_true", help="Print text but do not paste it")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.list_devices:
        print(sd.query_devices())
        return 0
    load_dotenv()
    config = load_config(args.config)
    if args.no_paste:
        config.paste = False
    try:
        DictationApp(config).run()
    except Exception as exc:
        logging.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
