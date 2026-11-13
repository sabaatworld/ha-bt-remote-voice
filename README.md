# ATVV Remote Voice for Home Assistant

Use an already-paired Android TV Voice over BLE (ATVV) remote as an Assist
microphone. Home Assistant 2026.11.1 or later on Linux with a local BlueZ
adapter is required. Early development: BT19K is the physically tested model;
other supported wire formats have synthetic tests, not hardware certification.

## Install

1. Copy `custom_components/atvv_remote` into `config/custom_components`, then
   restart Home Assistant. For HACS, add this repository as a custom integration
   repository; repository access is required while it is private.
2. Pair the remote **on the HA host** using your preferred external method.
3. Add **ATVV Remote Voice** in Settings → Devices & Services.
4. Select a paired device (or enter its address), wake it with an arrow button,
   and choose an Assist pipeline. Setup checks its voice capabilities.
5. Use the remote's microphone/Assistant control. Hold remotes end capture on
   release; supported tap/request remotes end after speech and a short silence.

Starts in **Transcription only** mode. Inspect results under Settings → Voice
assistants → your pipeline → Debug. After testing, choose **Execute commands**
in the integration's Configure options. The chosen pipeline ID survives rename;
a deleted pipeline must be explicitly replaced in options.

## Spoken replies

Choose any eligible **Reply speaker** in Configure options, or **No spoken
reply**. Command mode runs the selected pipeline through TTS when a speaker is
selected. The remote remains a microphone; replies play on the selected HA
media player. The pipeline must provide TTS and the player must resolve HA media
sources and report playing/idle transitions. The satellite remains responding
until observed playback finishes. Busy/paused players are left alone. Volume,
player configuration and existing media are not modified or restored.

Players that hide announcement state or cannot report playback completion will
report a timeout; they are not treated as successfully finished. No automatic
follow-up listening, announcements or wake words are implemented.

## Compatibility and microphone controls

| Protocol | Audio | Voice control | Evidence |
| --- | --- | --- | --- |
| ATVV 1.0 | 8/16 kHz IMA/DVI ADPCM, headerless + sync | Hold, press-to-talk, OnRequest | BT19K 16 kHz hold tested; other combinations synthetic |
| ATVV 0.4 | 8/16 kHz IMA/DVI ADPCM, six-byte frame header | OnRequest voice notification → microphone request | ATVVoice-derived vectors; hardware untested here |
| Apple Siri Remote | Apple HID/Opus | Separate transport | Unsupported; SiriRemoteESP is a separate receiver option |

Capabilities choose the format; there is no model-name allowlist or key-learning
wizard. A standard voice-control notification is the trigger, so no individual
key needs teaching. Ordinary buttons that do not activate the microphone are
outside this integration. Press-to-talk is used only when advertised by the
remote; this integration does not turn BT19K's hold control into tap-to-toggle.
Unknown versions/codecs are rejected rather than guessed. See upstream
attribution in [third-party notices](THIRD_PARTY_NOTICES.md).

## Behavior, privacy and limits

- Pairing, trust/bond management and Bluetooth discovery/connect ownership stay
  external. No OS packages, PipeWire daemon or proxy support are required.
- Existing Keyboard Remote and HID input integrations remain independent.
- Capture is limited to 30 seconds; request/tap capture has a 15-second total
  limit and ends about 0.8 seconds after speech. Audio buffering is bounded.
- Bad frames, unresolved sequence gaps, disconnects, queue overflow and missing
  audio abort input. Headerless audio cannot prove packet identity when CTL and
  RX counts disagree, so mismatches fail closed and record anonymous counts.
- Reconnection re-resolves GATT paths, re-subscribes and negotiates again.
  The remote may need waking. Stale busy/hold state is discarded on connection loss.
- Busy presses are ignored; old requests are never replayed as new commands.
- No microphone service opens capture without a physical voice notification.
- No recordings or transcripts are saved by this integration. Diagnostics omit
  identifiers/content and include only capabilities, counts, errors and timings.
  The selected pipeline/provider has its own processing and retention policy;
  cloud pipelines send speech to that provider. HA pipeline Debug can retain text.
- This integration does not change OS handling of power keys. Check host behavior
  before pressing them.

## Troubleshooting

- **No paired devices / Bluetooth unavailable:** check the host bond and HA Core
  access to local system D-Bus. A bond on another computer is insufficient.
- **Wake remote:** press an arrow, wait a few seconds, then retry. Do not use power.
- **Unsupported profile:** download redacted diagnostics and report the model,
  protocol/capabilities and integration/HA version. Do not publish addresses/audio.
- **Audio sequence gap:** retry close to the adapter; inspect last-session frame
  counters. Confirmed gaps fail closed instead of sending truncated commands.
- **No speech / audio stalled:** check the actual microphone control and selected
  speech-to-text provider. A normal HID key cannot substitute for mic activation.
- **No reply:** verify command mode, selected pipeline TTS and configured speaker.
  Speaker busy/unavailable and playback start/finish timeouts are distinct errors.
- **Reconnect:** wake the remote and allow up to 30 seconds for retry. Reload the
  integration if necessary; re-pair externally only when the bond itself is broken.

Physical coverage does not establish all-model compatibility or behavior under
battery depletion, radio interference, adapter removal or a host reboot.

## Development and releases

Linux/Python 3.14 is the reference test environment. Install
`requirements-dev.txt` in a virtual environment, then run:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin
ruff check .
ruff format --check .
python scripts/check_package.py
```

CI runs the full HA setup/unload test on Linux. Synthetic tests cover both
protocol versions, frame validation, microphone lifecycle, reconnect reset,
configuration and playback completion. macOS may have an upstream HA/native
extension shutdown crash; use Linux for the complete lifecycle suite.

For contributions, supply synthetic protocol vectors and failing regression
tests before changing framing/decoder behavior. Include upstream revision and
license attribution. Keep personal configuration, packet captures, recordings,
credentials, deployment notes and research outside this repository. Verify new
remote models physically before upgrading their compatibility claim.

`scripts/check_package.py` creates `dist/atvv_remote.zip` containing only component
files. No public release or publishing is performed automatically.
