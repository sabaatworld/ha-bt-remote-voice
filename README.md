# ATVV Remote Voice for Home Assistant

Push-to-talk Assist input from an already-paired Android TV Voice over BLE
(ATVV) remote. Early development; currently targets TVision BT19K and Home
Assistant 2026.11.1 or later on Linux with a local BlueZ adapter.

## Install and configure

1. Copy `custom_components/atvv_remote` into Home Assistant's
   `config/custom_components` directory and restart Home Assistant.
2. Pair the remote externally on the HA host. This integration does not pair,
   unpair, trust or repair bonds. A bond on another computer is not sufficient.
3. Add **ATVV Remote Voice** under Settings → Devices & Services.
4. Pick an already-paired device, or enter its address. Wake it with a normal
   arrow button, select an Assist pipeline and submit to check voice capabilities.
5. Hold the remote's Assistant button while speaking, then release it.

Starts in **Transcription only** mode: no commands execute. Check the Assist
pipeline's debug view for results. Once tested, choose **Execute commands** in
integration options. Pipeline identity is preserved through name changes;
removing the selected pipeline requires choosing another in options.

This version ends at speech-to-text or intent processing. It does not play
spoken replies, support announcements, or enable wake-word listening.

## Behavior and limits

- Exposes a voice satellite, microphone-active sensor and pressed/released event
  entity. No automation is required to start microphone input.
- Existing keyboard/input integrations handle normal buttons independently.
- Local paired devices only. Bluetooth proxies, Apple Siri remotes, arbitrary
  button learning, tap-to-toggle and software-only mic activation are unsupported.
- Only ATVV v1.0, 16 kHz ADPCM and hold-to-talk capabilities are accepted.
- A busy press is ignored until release; queued commands are not replayed.
- Audio buffering is bounded. Corrupt/missing input aborts the utterance; the
  integration does not intentionally execute a partial recording.
- Capture is limited to 30 seconds. Missing-audio and processing timeouts recover
  the listener. Sleep/reconnect recovery may require waking the remote first.
- This integration does not change OS handling of remote power keys. Check your
  host's behavior before pressing a power button.

System D-Bus must be accessible to HA Core. There is no PipeWire service or host
package installation. Remote compatibility requires testing each model.

## Privacy

No audio recordings, transcripts, household configuration or credentials are
included or saved by this integration. Diagnostics omit device addresses and
names. The selected HA pipeline/provider still controls its own processing and
retention; selecting a cloud provider sends speech to that provider.

Keep personal configuration, captures and research outside this repository.
Review changes before committing; ignore rules do not replace that review.

## Development

Use Linux, Python 3.14 and a virtual environment with Home Assistant 2026.11.1,
pytest, pytest-asyncio, dbus-fast, ruff and the optional dependencies declared by
HA's Assist/media components. Tests use synthetic audio and fake BLE objects;
real HA entities are exercised without opening an HTTP listener.

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin
ruff check .
ruff format --check .
```

See [third-party notices](THIRD_PARTY_NOTICES.md) for upstream attribution.
