"""
Azure Speech SDK - single-shot pronunciation assessment with prosody (en-US).

* Scripted   : you read a reference text aloud.
* Unscripted : you speak freely; no reference text.

Each run records ONE utterance (the SDK caps a single recognition at ~30 s;
no streaming / continuous recognition). Results are saved in
Documents/PronunciationAssessment/ as, for example:

    scripted_20261010.json     scripted_20261010.txt
    unscripted_20261010.json   unscripted_20261010.txt

* .json : every run of that mode/day, with the full raw service response.
* .txt  : human-readable report (successful runs only), suitable for sharing.
"""

import array
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

import azure.cognitiveservices.speech as speechsdk
import sounddevice as sd
from dotenv import load_dotenv

LOCALE = "en-US"
def documents_dir() -> Path:
    """Real Documents folder (follows OneDrive 'Known Folder Move' redirection)."""
    try:
        import winreg  # Windows only

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Personal")
        return Path(os.path.expandvars(value))
    except Exception:  # noqa: BLE001
        return Path.home() / "Documents"


OUTPUT_DIR = documents_dir() / "PronunciationAssessment"

MAX_SECONDS = 30  # hard cap on a single recording
SAMPLE_RATE = 16000  # 16 kHz, 16-bit, mono is what the Speech service expects


def load_env_file() -> None:
    """Load SPEECH_KEY / SPEECH_REGION from the first .env file found.

    Search order: next to the exe (or script), the current directory, then
    the results folder. Real environment variables take precedence.
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent
    for candidate in (base / ".env", Path.cwd() / ".env", OUTPUT_DIR / ".env"):
        if candidate.is_file():
            load_dotenv(candidate)
            return


load_env_file()

SCRIPTED = "scripted"
UNSCRIPTED = "unscripted"


# --------------------------------------------------------------------------- #
# Azure assessment
# --------------------------------------------------------------------------- #
def list_input_devices() -> list[tuple[int | None, str]]:
    """Return [(device_index, name)], starting with the system default (None)."""
    devices: list[tuple[int | None, str]] = [(None, "System default")]
    try:
        # Windows exposes each mic once per host API; keep only the default one.
        host = sd.query_devices(kind="input")["hostapi"]
    except Exception:  # noqa: BLE001
        host = None
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and (host is None or d["hostapi"] == host):
                devices.append((i, d["name"]))
    except Exception:  # noqa: BLE001
        pass
    return devices


def measure_mic_level(device: int | None, seconds: float = 3.0) -> int:
    """Open the mic for a few seconds; return the loudest sample (0-32767)."""
    peak = 0

    def on_audio(indata, frames, time_info, status):  # noqa: ARG001
        nonlocal peak
        samples = array.array("h")
        samples.frombytes(bytes(indata))
        if samples:
            peak = max(peak, max(abs(x) for x in samples))

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="int16",
        blocksize=SAMPLE_RATE // 10, device=device, callback=on_audio,
    ):
        time.sleep(seconds)
    return peak


def run_assessment(
    key: str,
    region: str,
    mode: str,
    reference_text: str,
    device: int | None = None,
    device_name: str = "System default",
) -> dict:
    """Record one utterance, assess it, and return a JSON-serializable record."""
    speech_config = speechsdk.SpeechConfig(subscription=key, region=region)
    speech_config.speech_recognition_language = LOCALE
    # Tolerate natural pauses mid-sentence; give up if nothing is said in 5 s.
    speech_config.set_property(
        speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs, "2000"
    )
    speech_config.set_property(
        speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs, "5000"
    )

    # We capture the mic ourselves (so you can pick the device) and push the
    # audio to the service; this also lets us enforce the 30 s cap exactly.
    push_stream = speechsdk.audio.PushAudioInputStream(
        stream_format=speechsdk.audio.AudioStreamFormat(
            samples_per_second=SAMPLE_RATE, bits_per_sample=16, channels=1
        )
    )
    audio_config = speechsdk.audio.AudioConfig(stream=push_stream)
    recognizer = speechsdk.SpeechRecognizer(
        speech_config=speech_config, audio_config=audio_config
    )

    scripted = mode == SCRIPTED
    pron_config = speechsdk.PronunciationAssessmentConfig(
        reference_text=reference_text if scripted else "",
        grading_system=speechsdk.PronunciationAssessmentGradingSystem.HundredMark,
        granularity=speechsdk.PronunciationAssessmentGranularity.Phoneme,
        enable_miscue=scripted,  # omission/insertion detection needs a reference
    )
    pron_config.phoneme_alphabet = "IPA"
    pron_config.enable_prosody_assessment()
    pron_config.apply_to(recognizer)

    # Single-shot: recognition ends at the first utterance or after 30 s.
    finished = threading.Event()
    recognizer.recognized.connect(lambda _evt: finished.set())
    recognizer.canceled.connect(lambda _evt: finished.set())
    recognizer.session_stopped.connect(lambda _evt: finished.set())
    future = recognizer.recognize_once_async()

    audio_q: queue.Queue = queue.Queue()

    peak = [0]  # loudest sample seen (0-32767), to detect a silent mic

    def on_audio(indata, frames, time_info, status):  # noqa: ARG001
        data = bytes(indata)
        samples = array.array("h")
        samples.frombytes(data)
        if samples:
            peak[0] = max(peak[0], max(abs(x) for x in samples))
        audio_q.put(data)

    started = time.monotonic()
    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=SAMPLE_RATE // 10,  # 100 ms chunks
            device=device,
            callback=on_audio,
        ):
            while not finished.is_set() and time.monotonic() - started < MAX_SECONDS:
                try:
                    push_stream.write(audio_q.get(timeout=0.1))
                except queue.Empty:
                    pass
    finally:
        push_stream.close()  # signals end of audio; service finalizes the result

    result = future.get()

    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "timestamp_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "locale": LOCALE,
        "microphone": device_name,
        "peak_audio_level": peak[0],
        "reference_text": reference_text if scripted else None,
        "status": result.reason.name,
    }

    if result.reason == speechsdk.ResultReason.RecognizedSpeech:
        raw = json.loads(
            result.properties.get(speechsdk.PropertyId.SpeechServiceResponse_JsonResult)
        )
        nbest = (raw.get("NBest") or [{}])[0]
        record["recognized_text"] = result.text
        record["summary_scores"] = nbest.get("PronunciationAssessment", {})
        record["prosody"] = extract_prosody(nbest)
        record["raw_response"] = raw
    elif result.reason == speechsdk.ResultReason.NoMatch:
        record["error"] = "No speech could be recognized (nothing heard or too quiet)."
        if peak[0] < 500:
            record["error"] += (
                f" The microphone captured almost no sound (peak level {peak[0]} of "
                "32767): check the selected device, its volume, and Windows "
                "microphone privacy settings."
            )
    elif result.reason == speechsdk.ResultReason.Canceled:
        details = result.cancellation_details
        record["error"] = f"{details.reason.name}: {details.error_details}"

    if peak[0] < 500:
        record["warning"] = (
            f"The microphone captured almost no sound (peak level {peak[0]} of 32767). "
            "These scores are not meaningful. Check mute/volume, Windows microphone "
            "privacy settings, or choose another microphone."
        )

    return record


def _ticks_to_seconds(value):
    """The service reports times in 100-nanosecond ticks."""
    return None if value is None else value / 10_000_000


def extract_prosody(nbest: dict) -> dict:
    """Flatten the per-word prosody feedback into plain numbers."""
    pa = nbest.get("PronunciationAssessment", {})
    words = []
    for w in nbest.get("Words", []):
        w_pa = w.get("PronunciationAssessment") or {}
        prosody = (w_pa.get("Feedback") or {}).get("Prosody") or {}
        brk = prosody.get("Break") or {}
        inton = prosody.get("Intonation") or {}
        words.append(
            {
                "word": w.get("Word"),
                "start_seconds": _ticks_to_seconds(w.get("Offset")),
                "duration_seconds": _ticks_to_seconds(w.get("Duration")),
                "accuracy_score": w_pa.get("AccuracyScore"),
                "error_type": w_pa.get("ErrorType"),
                "break_error_types": [t for t in brk.get("ErrorTypes", []) if t != "None"],
                "unexpected_break_confidence": (brk.get("UnexpectedBreak") or {}).get("Confidence"),
                "missing_break_confidence": (brk.get("MissingBreak") or {}).get("Confidence"),
                "break_length_seconds": _ticks_to_seconds(brk.get("BreakLength")),
                "intonation_error_types": [
                    t for t in inton.get("ErrorTypes", []) if t != "None"
                ],
                "monotone_confidence": (inton.get("Monotone") or {}).get(
                    "SyllablePitchDeltaConfidence"
                ),
                "phonemes": [
                    {
                        "phoneme": ph.get("Phoneme"),
                        "accuracy_score": (ph.get("PronunciationAssessment") or {}).get(
                            "AccuracyScore"
                        ),
                    }
                    for ph in w.get("Phonemes", [])
                ],
            }
        )
    return {"prosody_score": pa.get("ProsodyScore"), "words": words}


# --------------------------------------------------------------------------- #
# Saving / formatting
# --------------------------------------------------------------------------- #
def output_paths(mode: str) -> tuple[Path, Path]:
    """e.g. scripted_20261010.json / scripted_20261010.txt"""
    base = OUTPUT_DIR / f"{mode}_{datetime.now():%Y%m%d}"
    return base.with_suffix(".json"), base.with_suffix(".txt")


def save_record(record: dict) -> tuple[Path, Path | None]:
    """Append to the day's JSON file; append successful runs to the .txt report."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path, txt_path = output_paths(record["mode"])

    records: list = []
    if json_path.exists():
        try:
            records = json.loads(json_path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                records = []
        except json.JSONDecodeError:
            json_path.rename(json_path.with_suffix(".corrupt.json"))
            records = []
    records.append(record)
    json_path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")

    if record.get("status") != "RecognizedSpeech":
        return json_path, None  # failed attempts are kept in the JSON only

    run_no = sum(1 for r in records if r.get("status") == "RecognizedSpeech")
    with txt_path.open("a", encoding="utf-8") as f:
        f.write(f"RUN {run_no}\n{format_report(record)}\n\n")
    return json_path, txt_path


def _num(value, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _pct(value) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def format_report(record: dict) -> str:
    """Human-readable report used for both the on-screen view and the .txt file."""
    if "error" in record:
        return f"[{record['status']}] {record['error']}"

    s = record.get("summary_scores", {})
    prosody = record["prosody"]
    words = prosody["words"]
    scripted = record["mode"] == SCRIPTED

    header = (
        f"{'Word':<14}{'Start(s)':>9}{'Len(s)':>8}{'Accuracy':>10}  {'Error':<18}"
        f"{'Unexp.break':>12}{'Missing brk':>12}{'Break(s)':>10}{'Monotone':>10}"
    )
    width = len(header)
    out = [
        "=" * width,
        f"PRONUNCIATION ASSESSMENT - {record['mode'].upper()}",
        "=" * width,
        f"Date        : {record.get('timestamp_local', '')}",
        f"Language    : {record['locale']}",
        f"Microphone  : {record.get('microphone', '')}",
        f"Reference   : {record['reference_text'] if scripted else '(none - unscripted)'}",
        f"Recognized  : {record.get('recognized_text', '')}",
    ]
    if record.get("warning"):
        out += ["", "WARNING: " + record["warning"]]

    completeness = _num(s.get("CompletenessScore")) if scripted else "n/a (unscripted)"
    out += [
        "",
        "OVERALL SCORES (0-100)",
        f"  Pronunciation : {_num(s.get('PronScore'))}",
        f"  Accuracy      : {_num(s.get('AccuracyScore'))}",
        f"  Fluency       : {_num(s.get('FluencyScore'))}",
        f"  Completeness  : {completeness}",
        f"  Prosody       : {_num(prosody.get('prosody_score'))}",
        "",
        "WORD DETAIL",
        header,
        "-" * width,
    ]
    for w in words:
        out.append(
            f"{(w['word'] or '')[:13]:<14}"
            f"{_num(w['start_seconds'], 2):>9}"
            f"{_num(w['duration_seconds'], 2):>8}"
            f"{_num(w['accuracy_score']):>10}  "
            f"{(w['error_type'] or '-'):<18}"
            f"{_pct(w['unexpected_break_confidence']):>12}"
            f"{_pct(w['missing_break_confidence']):>12}"
            f"{_num(w['break_length_seconds'], 2):>10}"
            f"{_pct(w['monotone_confidence']):>10}"
        )

    def flagged(label: str, test) -> str:
        names = [w["word"] for w in words if test(w)]
        return f"  {label:<30}: {len(names)}" + (f"  ({', '.join(names)})" if names else "")

    out += [
        "",
        "PROSODY SUMMARY",
        flagged("Words with unexpected breaks", lambda w: "UnexpectedBreak" in w["break_error_types"]),
        flagged("Words with missing breaks", lambda w: "MissingBreak" in w["break_error_types"]),
        flagged("Words flagged monotone", lambda w: "Monotone" in w["intonation_error_types"]),
    ]

    phoneme_lines = [
        f"  {w['word']}: "
        + "  ".join(f"/{ph['phoneme']}/ {_num(ph['accuracy_score'], 0)}" for ph in w["phonemes"])
        for w in words
        if w["phonemes"]
    ]
    if phoneme_lines:
        out += ["", "PHONEME ACCURACY (IPA, 0-100)"] + phoneme_lines

    out += [
        "",
        "HOW TO READ THIS",
        "  Accuracy     : 0-100, how closely a word or sound matches clear native speech.",
        "  Unexp. break : confidence (%) that a pause after the word is out of place.",
        "  Missing brk  : confidence (%) that an expected pause after the word was absent.",
        "  Break(s)     : length of the pause after the word, in seconds.",
        "  Monotone     : confidence (%) that the pitch was flat on this word.",
        "  '-' means not flagged or not reported by the service.",
        "",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Pronunciation Assessment (en-US)")
        self.geometry("980x780")

        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=8, pady=8)

        # Credentials
        cred = ttk.LabelFrame(frm, text="Azure Speech resource")
        cred.pack(fill="x", **pad)
        ttk.Label(cred, text="Key").grid(row=0, column=0, sticky="w", **pad)
        self.key_var = tk.StringVar(value=os.environ.get("SPEECH_KEY", ""))
        ttk.Entry(cred, textvariable=self.key_var, show="*", width=50).grid(
            row=0, column=1, sticky="we", **pad
        )
        ttk.Label(cred, text="Region").grid(row=1, column=0, sticky="w", **pad)
        self.region_var = tk.StringVar(value=os.environ.get("SPEECH_REGION", ""))
        ttk.Entry(cred, textvariable=self.region_var, width=20).grid(
            row=1, column=1, sticky="w", **pad
        )
        cred.columnconfigure(1, weight=1)

        # Microphone
        mic_frm = ttk.LabelFrame(frm, text="Microphone")
        mic_frm.pack(fill="x", **pad)
        self.mic_var = tk.StringVar()
        self.mic_combo = ttk.Combobox(
            mic_frm, textvariable=self.mic_var, state="readonly", width=60
        )
        self.mic_combo.pack(side="left", fill="x", expand=True, **pad)
        ttk.Button(mic_frm, text="Refresh", command=self._refresh_mics).pack(
            side="left", **pad
        )
        ttk.Button(mic_frm, text="Test mic (3 s)", command=self._test_mic).pack(
            side="left", **pad
        )
        self.devices: list[tuple[int | None, str]] = []
        self._refresh_mics()

        # Mode
        mode_frm = ttk.LabelFrame(frm, text="Mode")
        mode_frm.pack(fill="x", **pad)
        self.mode_var = tk.StringVar(value=SCRIPTED)
        ttk.Radiobutton(
            mode_frm, text="Scripted (read the text below)", value=SCRIPTED,
            variable=self.mode_var, command=self._update_mode,
        ).pack(anchor="w", **pad)
        ttk.Radiobutton(
            mode_frm, text="Unscripted (speak freely)", value=UNSCRIPTED,
            variable=self.mode_var, command=self._update_mode,
        ).pack(anchor="w", **pad)

        # Reference text
        ref_frm = ttk.LabelFrame(frm, text="Reference text (scripted mode)")
        ref_frm.pack(fill="x", **pad)
        self.ref_text = tk.Text(ref_frm, height=4, wrap="word")
        self.ref_text.pack(fill="x", **pad)
        self.ref_text.insert("1.0", "Today was a beautiful day. We had a great time at the park.")

        # Controls
        ctl = ttk.Frame(frm)
        ctl.pack(fill="x", **pad)
        self.record_btn = ttk.Button(ctl, text="Record (up to 30 s)", command=self._start)
        self.record_btn.pack(side="left")
        ttk.Button(ctl, text="Open results folder", command=self._open_folder).pack(
            side="left", padx=8
        )
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(frm, textvariable=self.status_var).pack(anchor="w", **pad)

        # Output
        out_frm = ttk.Frame(frm)
        out_frm.pack(fill="both", expand=True, **pad)
        self.output = tk.Text(
            out_frm, height=16, wrap="none", state="disabled", font=("Consolas", 9)
        )
        yscroll = ttk.Scrollbar(out_frm, orient="vertical", command=self.output.yview)
        xscroll = ttk.Scrollbar(out_frm, orient="horizontal", command=self.output.xview)
        self.output.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.output.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        out_frm.rowconfigure(0, weight=1)
        out_frm.columnconfigure(0, weight=1)

        self._update_mode()

    def _refresh_mics(self) -> None:
        self.devices = list_input_devices()
        self.mic_combo["values"] = [name for _, name in self.devices]
        self.mic_combo.current(0)

    def _test_mic(self) -> None:
        device, _ = self.devices[max(self.mic_combo.current(), 0)]
        self.status_var.set("Testing microphone... speak for 3 seconds.")
        threading.Thread(target=self._test_worker, args=(device,), daemon=True).start()

    def _test_worker(self, device: int | None) -> None:
        try:
            peak = measure_mic_level(device)
            if peak < 500:
                msg = (f"Mic level {peak}/32767: essentially silent. Check mute, volume, "
                       "privacy settings, or pick another microphone.")
            else:
                msg = f"Mic level {peak}/32767: microphone is working."
        except Exception as exc:  # noqa: BLE001
            msg = f"Could not open microphone: {exc}"
        self.after(0, self.status_var.set, msg)

    def _update_mode(self) -> None:
        state = "normal" if self.mode_var.get() == SCRIPTED else "disabled"
        self.ref_text.configure(state=state)

    def _set_output(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def _open_folder(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(OUTPUT_DIR)  # Windows only

    def _start(self) -> None:
        key, region = self.key_var.get().strip(), self.region_var.get().strip()
        mode = self.mode_var.get()
        reference = self.ref_text.get("1.0", "end").strip() if mode == SCRIPTED else ""

        if not key or not region:
            messagebox.showwarning("Missing info", "Enter your Speech key and region.")
            return
        if mode == SCRIPTED and not reference:
            messagebox.showwarning("Missing info", "Enter the reference text to read.")
            return

        device, device_name = self.devices[max(self.mic_combo.current(), 0)]

        self.record_btn.configure(state="disabled")
        self._set_output("")
        self.status_var.set("Listening... speak now (up to 30 seconds).")
        threading.Thread(
            target=self._worker,
            args=(key, region, mode, reference, device, device_name),
            daemon=True,
        ).start()

    def _worker(
        self, key: str, region: str, mode: str, reference: str,
        device: int | None, device_name: str,
    ) -> None:
        try:
            record = run_assessment(key, region, mode, reference, device, device_name)
            paths = save_record(record)
            self.after(0, self._done, record, paths, None)
        except Exception as exc:  # noqa: BLE001 - surface any SDK/IO error to the UI
            self.after(0, self._done, None, None, exc)

    def _done(self, record: dict | None, paths, exc: Exception | None) -> None:
        self.record_btn.configure(state="normal")
        if exc is not None:
            self.status_var.set("Failed.")
            messagebox.showerror("Error", str(exc))
            return
        self._set_output(format_report(record))
        json_path, txt_path = paths
        saved = [json_path.name] + ([txt_path.name] if txt_path else [])
        self.status_var.set(f"Saved to {OUTPUT_DIR}: " + ", ".join(saved))


if __name__ == "__main__":
    App().mainloop()
