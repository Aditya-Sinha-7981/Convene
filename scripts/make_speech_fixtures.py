"""Regenerate the synthetic speech fixtures in tests/fixtures/audio/ with the macOS `say` and `afconvert` tools.

No digits appear in the reference text, so word error rate is not inflated by number formatting ("$50,000" against "fifty thousand dollars").
The clips are text-to-speech, so they are clean, evenly paced and speaker-consistent: good for regression
and for comparing models and window sizes against each other, and NOT evidence of accuracy on real phone
audio. Run on macOS only: `.venv/bin/python scripts/make_speech_fixtures.py`.
"""
import json
import subprocess
import tempfile
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "audio"

# (file stem, voice, text). Short business-meeting sentences, two voices so two "phones" can differ.
CLIPS = [
    ("ship_beta", "Samantha", "We should ship the beta on Friday."),
    ("release_notes", "Daniel", "I can take the release notes and send them tomorrow."),
    ("budget", "Samantha", "The budget for the next quarter is very tight."),
    ("call_client", "Daniel", "Please remind me to call the client on Monday morning."),
    ("design_review", "Samantha", "Let's move the design review to Thursday afternoon."),
    ("alphabet", "Daniel", "Alpha, bravo, charlie, delta, echo."),
    ("interviews", "Samantha", "Priya will handle the customer interviews this week."),
    ("objection", "Daniel", "Does anyone object to the new schedule?"),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    with tempfile.TemporaryDirectory() as tmp:
        for stem, voice, text in CLIPS:
            aiff = Path(tmp) / f"{stem}.aiff"
            wav = OUT / f"{stem}.wav"
            subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
            # 16 kHz, mono, 16-bit little-endian PCM: what the STT stage consumes.
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)], check=True)
            manifest.append({"file": wav.name, "voice": voice, "text": text})
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {len(manifest)} clips to {OUT}")


if __name__ == "__main__":
    main()
