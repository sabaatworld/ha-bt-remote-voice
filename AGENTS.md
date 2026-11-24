# AGENTS.md — ha-bt-remote-voice

Instructions for coding agents working on this reusable Home Assistant custom
integration. `CLAUDE.md` only links here.

## What this is

**Bluetooth Remote Voice** (full name: Bluetooth Remote Voice to Home Assistant; repo
`ha-bt-remote-voice`, domain `bt_remote_voice`): a Home Assistant custom integration
(`custom_components/bt_remote_voice`) that turns an already-paired Bluetooth TV remote into an
Assist microphone. It supports remotes that speak the Android TV Voice (ATVV) protocol and the
Amazon Fire TV voice remote, and may support others later (for example the Apple Siri Remote).
Linux with local BlueZ is the target; Python 3.14, HA 2026.11.1+.

Module map:

- `bluez.py`, `protocol.py` — BlueZ GATT transport; ATVV control/audio parsing, capability profiles.
- `session.py` — one physical capture: strict frame/sync validation, ADPCM decode, release timing, capture limit.
- `held.py` — alternating conversation: logical `Utterance` PCM streams cut from a held capture.
- `coordinator.py` — owns connection, capture ownership, busy policy (reject/queue/replace), recovery gates, outcomes.
- `assist_satellite.py`, `pipeline.py` — runs HA's native pipeline; early streamed playback; generation retirement.
- `playback.py` — plays replies on a media player and observes real start/finish.
- `preferences.py`, `config_flow.py`, `translations/en.json` — options, validation, UI text (`en.json` is the only source; the other languages sit beside it).
- `fire.py`, `opus.py` — Fire TV voice remote: hidraw transport (ATVV-shaped control + raw Opus packets to the coordinator) and the Opus decoder adapter (PyAV, ctypes fallback).
- `reply_condition.py` — the reply-speaker condition (raw-list selector, per-request decision).
- `event.py`, `binary_sensor.py`, `sensor.py`, `diagnostics.py`, `history.py` — entities, diagnostics, bounded anonymous history.

## Commands

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q
ruff check . && ruff format --check .
python scripts/check_package.py
python scripts/translation_tools.py status   # every language file clean
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
  fixed codes and numeric timings. Keep this when adding fields. The one exception
  is the user-initiated Remote Report (setup's "Show remote report" and the
  `remote_report` entry in diagnostics): it may show the first three bytes of a
  remote's address (usually the public manufacturer prefix) with the rest masked,
  plus the remote's own Bluetooth identity data as it advertises it (UUIDs,
  product id, manufacturer-data bytes, HID descriptor), but never a name or alias. `report.mask_address`
  is the only place that decides this, and a report field change bumps `SCHEMA`.
- **Keep private material out of this repo.** Packet captures, recordings,
  credentials, deployment notes, household research and test logs belong in the
  maintainer's private workspace, not here. A remote's HID report descriptor (hex)
  is configuration data, not a capture, and may be kept as a test fixture.
- **Translations, always.** No user-visible text in code: entity names via
  `translation_key`, choice labels via `SelectSelector(translation_key=...)` with
  `selector` entries, every form field needs `data` and `data_description`,
  every error/abort key exists, and failure codes are `ERROR_CODES` with a state
  translation each. `translations/en.json` is the only source (there is no
  `strings.json`).
- **Every language, every time.** The integration ships a file for every language in
  `homeassistant.generated.languages.LANGUAGES` (63 besides `en`). Whenever you add or change
  a user-visible string, add or update it in **all** of those files in the same change: keep
  every `{placeholder}`, Markdown marker and newline, leave product terms (Assist, ATVV, Fire TV,
  BlueZ, Opus, Bluetooth, Home Assistant) untranslated, use Home Assistant's own wording for UI
  terms, and write real UTF-8 characters. Batch the languages across subagents when the change is
  large. Then run `python scripts/translation_tools.py status` and
  `python scripts/translation_tools.py refresh-hashes`, and make `tests/test_language_files.py`
  pass. Never leave a language behind, never edit a translation without its English source, and
  tell the user the translations are AI-generated if asked about their quality.
- **Repairs for external dependencies.** Any option that points at something this
  integration does not own (an Assist pipeline, a media player, and any future
  entity, device, service or other reference) must raise a Repairs issue in
  `issues.py` when the target is missing: created at startup (after HA has
  started) and re-checked when the target changes, cleared when it returns and on
  unload, with the `issues` text (title, description with the Configure
  path) in every translation file. The options form must also keep a missing
  saved value visible with a field error, never silently substitute another, and
  must still let other settings be saved. Test all of it (`tests/test_issues.py`,
  `tests/test_config_flow.py`). Temporary unavailability is not a repair.
- **Option order.** The options form lists fields in a deliberate order (pipeline,
  voice mode, reply speaker, reply condition, reply timing, head start, busy
  policies, context, button behavior, remote type, short-press filter, then limits). Put new options in that
  grouping and keep the README configuration table in the same order.
- **Behavior-preserving defaults.** A new option must default to existing behavior,
  be validated in `preferences.py`, appear in `config_flow.py` and in every translation file (`en.json` first),
  and be documented in the README's Configuration
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
- **Supported remotes.** The README's "Supported remotes" table lists only what was
  physically tested as tested. Record every remote you evaluate (supported,
  unsupported, or unusable) with the evidence, never a guess, and keep the
  pairing guide accurate (HAOS host shell vs terminal app).
- **Honest claims.** Streaming latency, audio smoothness and hardware
  compatibility need physical acceptance tests; unit tests do not establish
  them. Timings are *request/report* times, not acoustic first-sound. Full
  duplex stays reported unavailable until a complete interruption contract is verified.
- **A reply speaker never costs a command.** An unavailable, busy or unable speaker skips only the
  spoken reply (`reply_skipped`, not a failure; `playback.SKIPPABLE`) and the pipeline keeps running.
  Anything that may leave audio playing stays a failure: a start timeout, a speaker lost after
  playback began (`AudioFailure.after_start`), interrupted or unfinished playback, and every
  `playback_stop_*`. The Repairs hint `speaker_unavailable` is raised by a coordinator counter (3 lost
  replies in a row; busy neither counts nor resets), never from `sync_issues` alone.
- **Naming.** The product is "Bluetooth Remote Voice". Write "ATVV" only when naming that
  protocol (profiles, parsing, compatibility rows, the ATVVoice credit). Background task names,
  docstrings and user-facing text must not present the whole integration as ATVV.
- **Marketplace publishing.** Home Assistant 2026.11 replaced HACS with the built-in Marketplace
  (same `hacs.json`, same catalog). Keep `hacs.json` to documented keys, the manifest `version`
  equal to the release tag, `issue_tracker`, `documentation` and 1 to 3 `categories`, a public
  repo with description, topics and issues, and the GitHub-only README sections wrapped in
  `<!-- ha-marketplace:exclude-start -->` / `<!-- ha-marketplace:exclude-end -->`. Install
  instructions say Marketplace, not HACS. **Betas:** tag `vX.Y.Z-beta.N` (or `-rc.N`) with the
  same manifest `version`; the Release workflow flags it as a GitHub pre-release, and users opt in with the
  repository's Pre-release switch (Marketplace docs). Never publish a beta as a regular release, and verify a
  release candidate (full tests, CI on `main`, the built zip) before pushing its tag. The `hacs/default` listing pull request needs the
  maintainer's go-ahead. Spec: https://contributors.home-assistant.io/marketplace/guides/integrations/
- **Hardware safety.** Never press or script a remote's power buttons. Do not
  open a microphone without a physical voice notification.
- **Image resolution.** Generate and use `@3x` raster variants wherever supported,
  including the README. Retain editable SVG sources. Where Home Assistant requires
  specific image sizes or filenames, supply its prescribed variants instead.
- **Regenerate edited assets.** Keep generated files in sync with their sources.
  After editing branding SVGs in `docs/images/` or `scripts/generate_branding.sh`,
  run `scripts/generate_branding.sh`, verify the outputs, and include all updated
  PNG variants in `custom_components/bt_remote_voice/brand/` in the same change.
  Edit the SVG sources rather than generated branding PNGs.
- **Branding.** The "Emerald Solar" icon combines a voice remote, a speech bubble and two motion
  strokes, with forest `#10332C`, emerald `#25B981` and warm gold `#E8B64F` on transparent
  artwork for both light and dark. Editable SVGs (`icon.svg`, `logo.svg`, `dark_logo.svg`) live in
  `docs/images/`; the wordmark reads "Bluetooth Remote Voice" with the subtitle "to Home Assistant".
  Home Assistant loads the PNGs in `custom_components/bt_remote_voice/brand/` automatically; the
  README uses their 3× variants. `scripts/generate_branding.sh` (needs `rsvg-convert` from
  librsvg) renders 1×, 2× and 3× PNGs from the SVGs, works from any directory and replaces
  outputs only after every render succeeds. Release zips contain the PNGs, not the SVGs.
- **Git.** Do not commit or push unless asked. Do not add AI co-author trailers.
  `scripts/check_package.py` builds the release archive but publishes nothing.
