# WhisperFlo Kairos MVP

A local Windows hold-to-talk dictation utility powered by [whisper.cpp](https://github.com/ggml-org/whisper.cpp).

## Behavior

Hold **Ctrl+Z+X**, speak, then release any one of those keys. The audio is transcribed locally and pasted into the focused text field. The chord is consumed while active so it should not trigger Undo/Cut.

This MVP uses the CPU whisper.cpp binary. It works with integrated graphics because it does not require a discrete GPU; performance depends on your CPU and model.

## Setup

Requirements:

- Windows 10/11 x64
- Python 3.10+
- A working microphone
- Internet access once, to download dependencies, whisper.cpp, and the model

From PowerShell:

```powershell
cd C:\Users\arjra\whisperFloKairos
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
```

Then run:

```powershell
python app.py
```

Click a text field before dictating. You should see `Ready` in the terminal. Hold the three-key chord, speak, and release it.

To inspect audio devices:

```powershell
python app.py --list-devices
```

Set `device` in `config.json` to a device index if the default microphone is wrong. Set it back to `null` for the default device.

## Configuration

`config.json` controls:

- `model`: defaults to `models/ggml-tiny.en.bin`
- `threads`: CPU threads used by Whisper
- `paste`: set to `false` to only print transcription
- `restore_clipboard`: restore the previous text clipboard after pasting
- `pre_roll_ms`: audio kept before the chord is fully pressed
- `suppress_chord`: leave `false` initially; set `true` only if you want to prevent the target app from seeing Undo/Cut. The safe default lets all keys pass through, so a hook problem cannot make the keyboard appear stuck.

The MVP starts with `tiny.en` for a fast first test. If accuracy is not good enough, download `ggml-base.en.bin` into `models/` and update `model` in `config.json`.

## Notes

- Transcription happens after release, not live while speaking.
- Clipboard insertion works in ordinary desktop text fields. Windows may block injection into an elevated/admin application unless this app is also elevated.
- The app currently runs in a terminal and exits with `Ctrl+C`; tray UI and packaging are later improvements.
- If the keyboard hook gets interrupted, press and release Ctrl, Z, and X once to reset their physical state.
