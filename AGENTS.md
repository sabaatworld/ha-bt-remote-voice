# AGENTS.md — ha-atvv-remote

Instructions for coding agents working on this reusable Home Assistant custom
integration. `CLAUDE.md` only links here.

## What this is

A Home Assistant custom integration (`custom_components/atvv_remote`) that turns an
already-paired Android TV Voice (ATVV) BLE remote into an Assist microphone.
Linux with local BlueZ is the target; Python 3.14, HA 2026.11.1+.

Module map:

- `bluez.py`, `protocol.py` — BlueZ GATT transport; ATVV control/audio parsing, capability profiles.
- `session.py` — one physical capture: strict frame/sync validation, ADPCM decode, release timing, capture limit.
- `held.py` — alternating conversation: logical `Utterance` PCM streams cut from a held capture.
- `coordinator.py` — owns connection, capture ownership, busy policy (reject/queue/replace), recovery gates, outcomes.
- `assist_satellite.py`, `pipeline.py` — runs HA's native pipeline; early streamed playback; generation retirement.
- `playback.py` — plays replies on a media player and observes real start/finish.
- `preferences.py`, `config_flow.py`, `strings.json`, `translations/en.json` — options, validation, UI text.
- `event.py`, `binary_sensor.py`, `sensor.py`, `diagnostics.py`, `history.py` — entities, diagnostics, bounded anonymous history.

## Commands

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q
ruff check . && ruff format --check .
python scripts/check_package.py
```

Install `requirements-dev.txt` into a virtualenv first. Run the full suite on
**Linux** (CI or a container) after any source change: macOS can crash on an
upstream native-extension shutdown. A Linux container needs a C++ compiler for
the HA audio dependencies; mount the repo read-only and set
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`.

## Rules

- **Fix bugs test-first.** Write a failing regression test, watch it fail for
  the right reason, then fix. A fix without a test that failed first is unverified.
- **Fail closed on input.** Bad frames, sequence gaps, disconnects, overflow and
  missing audio abort the capture; never present corrupt input as a clean EOF.
  Do not relax a guard to make a scenario pass; prove the guard is wrong first.
- **No hard-coded household data.** No device addresses, entity IDs, speaker
  names, pipeline IDs, provider names or model-name allowlists in source.
  Provider behavior is detected from HA's signals, never guessed from names.
- **Privacy.** History, diagnostics, events and attributes never contain PCM,
  transcripts, Bluetooth addresses, URLs or other identifiers; only counts,
  fixed codes and numeric timings. Keep this when adding fields.
- **Keep private material out of this repo.** Packet captures, recordings,
  credentials, deployment notes, household research and test logs belong in the
  maintainer's private workspace, not here.
- **Translations, always.** No user-visible text in code: entity names via
  `translation_key`, choice labels via `SelectSelector(translation_key=...)` with
  `selector` entries, every form field needs `data` and `data_description`,
  every error/abort key exists, and failure codes are `ERROR_CODES` with a state
  translation each. Edit `strings.json` and keep `translations/en.json` identical
  (`tests/test_translations.py` enforces this, including field coverage).
- **Behavior-preserving defaults.** A new option must default to existing behavior,
  be validated in `preferences.py`, appear in `config_flow.py`, `strings.json`
  and `translations/en.json`, and be documented in the README's Configuration
  reference with *when to use it*. Update the README in the same change.
- **HA thread-safety and deprecations.** State-change and event listeners passed
  to HA must be `@callback` functions (an undecorated listener runs in an executor
  thread and writes state off-loop). Pass explicit arguments HA is deprecating
  (for example `context=` to `async_get_chat_session`) and watch the HA log for
  deprecation warnings after changes.
- **Task and timer hygiene.** Every task, timer and handle you create must be
  cancelled on stop, unload, disconnect and cancellation. Use ownership, not
  mutable globals: disconnect decisions use the input the running pipeline
  owns (`active_input`), not the current physical session. Do not cancel a task
  twice into another component's cleanup (generation retirement belongs to the
  satellite; the coordinator waits).
- **Play only what we own.** Stop only media this request started, and verify the
  stop before retiring a request. Unknown ownership fails explicitly.
- **Honest claims.** Streaming latency, audio smoothness and hardware
  compatibility need physical acceptance tests; unit tests do not establish
  them. Timings are *request/report* times, not acoustic first-sound. Full
  duplex stays reported unavailable until a complete interruption contract is verified.
- **Hardware safety.** Never press or script a remote's power buttons. Do not
  open a microphone without a physical voice notification.
- **Git.** Do not commit or push unless asked. Do not add AI co-author trailers.
  `scripts/check_package.py` builds the release archive but publishes nothing.
