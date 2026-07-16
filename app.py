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
import subprocess
import sys
import tempfile
import threading
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
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t

# Clipboard constants.
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


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
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


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

    def __init__(self, events: queue.Queue[str]):
        self.events = events
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

            @callback_type
            def callback(n_code, w_param, l_param):
                if n_code >= 0:
                    info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    vk = int(info.vkCode)
                    down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    up = w_param in (WM_KEYUP, WM_SYSKEYUP)
                    tracked = CTRL_KEYS | {VK_Z, VK_X}
                    if vk in tracked:
                        was_active = self.active
                        if down:
                            self.pressed.add(VK_CONTROL if vk in CTRL_KEYS else vk)
                        elif up:
                            self.pressed.discard(VK_CONTROL if vk in CTRL_KEYS else vk)

                        now_active = {VK_CONTROL, VK_Z, VK_X}.issubset(self.pressed)
                        if now_active and not self.active:
                            self.active = True
                            self.events.put("start")
                        elif self.active and not now_active:
                            self.active = False
                            self.events.put("stop")

                        # Consume the whole chord, including the key-up that
                        # ends it, so the target app never sees Undo/Cut.
                        if self.active or was_active:
                            return 1
                call_next = ctypes.windll.user32.CallNextHookEx
                call_next.argtypes = [wintypes.HHOOK, ctypes.c_int, WPARAM, LPARAM]
                call_next.restype = LRESULT
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
        self.audio_error: Exception | None = None
        self.stream: sd.InputStream | None = None
        self.keyboard = KeyboardHook(self.events)
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
        logging.info("Ready. Hold Ctrl+Z+X, speak, then release any key. Press Ctrl+C to quit.")
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
        threading.Thread(target=self.transcribe, args=(samples,), daemon=True).start()

    def transcribe(self, samples: np.ndarray) -> None:
        if not self.transcribing.acquire(blocking=False):
            logging.warning("Still transcribing the previous clip; ignoring this one")
            return
        wav_path = self.temp_dir / f"clip-{os.getpid()}-{time.time_ns()}.wav"
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
            logging.info("[transcribing]")
            result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-1200:]
                logging.error("whisper.cpp failed (%s): %s", result.returncode, detail)
                return
            text = clean_transcription(result.stdout)
            if not text:
                logging.info("[no speech detected]")
                return
            logging.info("[text] %s", text)
            if self.config.paste:
                paste_text(text, self.config.restore_clipboard)
        except Exception:
            logging.exception("Transcription failed")
        finally:
            self.transcribing.release()
            try:
                wav_path.unlink(missing_ok=True)
            except Exception:
                pass


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
    sent = ctypes.windll.user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(INPUT))
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
