# Pronunciation Assessment (Azure Speech SDK, en-US)

Single-shot (max ~30 s) scripted and unscripted pronunciation assessment with
prosody. Results are saved in `Documents\PronunciationAssessment\`, one pair of
files per mode per day:

    scripted_20261010.json     scripted_20261010.txt
    unscripted_20261010.json   unscripted_20261010.txt

* `.json` - every attempt that day (including failures), with the full raw
  Azure response.
* `.txt`  - readable report for sharing (successful runs only): overall scores,
  a per-word table with pause and monotone numbers, a prosody summary, and
  phoneme scores.

## Run from source (uv)

    uv sync
    Copy-Item .env.example .env              # then edit .env with your key and region
    uv run python pronunciation_app.py

## Build a Windows executable (PyInstaller)

Folder build (recommended: starts faster, fewer antivirus false positives):

    uv run pyinstaller --noconfirm --windowed --name PronunciationAssessment `
        --collect-all azure.cognitiveservices.speech --collect-all sounddevice `
        --collect-all _sounddevice_data pronunciation_app.py

Output: `dist\PronunciationAssessment\PronunciationAssessment.exe`
(distribute the whole folder, or wrap it with Inno Setup for a real installer).

Single-file build:

    uv run pyinstaller --noconfirm --onefile --windowed --name PronunciationAssessment `
        --collect-all azure.cognitiveservices.speech --collect-all sounddevice `
        --collect-all _sounddevice_data pronunciation_app.py

`--collect-all` is needed so the Speech SDK's native DLLs are bundled.

## Microphone troubleshooting

* Pick the device in the **Microphone** dropdown (Refresh after plugging one in).
* Windows: Settings > Privacy & security > Microphone > turn on
  "Let desktop apps access your microphone".
* The record lists the microphone used under `"microphone"` in the .json file.

## Credentials (.env)

Copy `.env.example` to `.env` and fill in `SPEECH_KEY` and `SPEECH_REGION`.
The app looks for `.env` next to the exe (or script), then the current folder,
then `Documents\PronunciationAssessment\`. Real environment variables win over
`.env`. The key/region boxes in the app are prefilled from these values.
The `.env` is not bundled into the exe; place it beside the exe when you distribute.
