# ATVV Remote Voice for Home Assistant

Use an already-paired Android TV Voice over BLE (ATVV) remote as an Assist
microphone. Home Assistant 2026.11.1 or later on Linux with a local BlueZ
adapter is required. Early development: BT19K is the physically tested model;
other supported wire formats have synthetic tests, not hardware certification.

## Install

1. Copy `custom_components/atvv_remote` into `config/custom_components`, then
   restart Home Assistant. HACS custom-repository installation is also supported.
2. Pair the remote on the HA host using an external method. Wake it with an
   arrow button before adding **ATVV Remote Voice** in Devices & Services.
3. Select the paired device and an existing Assist pipeline. Select a speaker
   and command mode in Configure when ready to process requests and hear replies.

The initial mode stops at the STT stage. This does not disable tools embedded
inside a live STT provider: such a provider may act during speech processing.
Use a dedicated transcription-only STT provider when actions must be disabled.

## Configuration reference

Select **Configure** on the integration to change these settings; changes reload
the integration. Every setting has a short description in the form itself; this
section adds when to use each one. All text, including entity names, option
labels and error messages, comes from Home Assistant's translation files
(English is provided). Everything except the pipeline, voice mode and speaker defaults
to the behavior described in the Default column, so an existing setup does not
change unless you choose otherwise.

### Setup (once)

| Setting | What it does |
| --- | --- |
| Paired remote | A device already paired and bonded on the HA host. This integration never creates Bluetooth bonds. |
| Assist pipeline | The pipeline that processes speech: STT, conversation agent and TTS. Any pipeline works, including live speech providers. Changing it later does not need re-pairing. |

### Options

| Option | Choices | Default | Use it when |
| --- | --- | --- | --- |
| Assist pipeline | Any existing pipeline | The one chosen at setup | You want a different agent or speech provider. A removed pipeline fails with a clear error instead of silently falling back. |
| Voice mode | Transcription; Commands | Transcription | **Transcription** stops after speech recognition: nothing is run and no reply plays, which is useful for checking the microphone path and the speech provider. **Commands** runs the whole pipeline and, with a speaker, speaks the reply. |
| Reply speaker | Any media player that can play media, or none | None | You want spoken replies. Used only in Commands mode. With none, the pipeline stops at the intent stage and nothing is played. The player must report when playback starts and finishes. |
| Reply timing | Automatic streaming; Wait for complete reply | Automatic | **Automatic** starts speaking as soon as the pipeline reports a streamed reply, which gives the earliest start if the provider produces speech faster than real time. **Wait for complete reply** plays a finished audio file: the start is later, but it avoids breakup when a provider only produces speech at about real-time pace or the speaker stutters on a live stream. Short replies may not stream at all, in which case the completed reply is played. |
| Streaming head start | 0 to 10 seconds, in 0.5 s steps | 0 | Streamed replies break up in the first seconds. After a streamed reply is ready, the integration waits this long before asking the speaker to play, or starts at once if the reply finishes sooner. It gives the speaker a lead; it cannot help if the provider keeps producing speech more slowly than it plays. It has no effect with Wait for complete reply or when the reply is not streamed. Try 0.5 to 3 seconds. |
| New press while processing | Reject; Queue one; Replace current request | Reject | A press arrives before the previous request is answered. **Reject** is the safest. **Queue one** keeps one pending request and runs it afterward. **Replace** cancels the current request so the new one starts at once; this cannot undo an action the old request already performed. |
| New press during reply | Reject; Queue one; Replace current reply | Reject | A press arrives while a reply is playing. **Reject** lets the reply finish. **Queue one** asks the next question while the reply plays and answers it afterward. **Replace** stops the reply and starts the new request. |
| Conversation context | Reuse; Fresh per request | Reuse | **Reuse** keeps the Home Assistant conversation so follow-ups such as "and double that?" work (while Home Assistant retains it). **Fresh** makes each press independent. |
| Button behavior | One request per hold; Alternating conversation while held | One request | **One request** records one request per hold and ends at release. **Alternating conversation** needs a hold-to-talk remote: speak, pause about 0.8 s, hear the reply, then speak again without releasing. |

Streaming, queueing and replacement work across traditional and live pipelines.
Automatic output uses Home Assistant's streaming signals, not a provider list.
Player buffering still affects the first sound.

Queue one records one bounded pending request and processes it after the current
reply; additional requests are rejected. Pending audio expires 30 seconds after
release and is discarded on unload, disconnection or configuration change.
Replace stops the old response before starting the new one. It never
automatically retries an uncertain command. If speaker-stop ownership or
generation retirement cannot be verified, replacement fails explicitly instead
of overlapping requests or stopping unrelated media.

Alternating held mode discards input during replies to avoid treating the speaker
as a new request, and release stops further listening. This is **not simultaneous
full duplex**; talking over the reply is not supported. Full duplex is reported
unavailable because no complete pipeline interruption, player flush and
echo-reference contract is verified.

### Choosing settings by situation

- **Fast cloud model that answers quickly:** Automatic, head start 0.
- **Live speech provider whose streamed replies stutter at the start:** keep
  Automatic and raise the head start in small steps; if it still breaks up, use
  Wait for complete reply.
- **Follow-up questions in quick succession:** Reuse context, Queue one during
  reply, or Alternating conversation on a hold-to-talk remote.
- **Independent one-shot commands:** Fresh context, Reject while busy.
- **Just checking the microphone path:** Transcription mode.

### Entities

| Entity | Purpose |
| --- | --- |
| Voice (assist satellite) | Runs requests. Attributes show readiness, session, failure, rejection and queue counts, the last outcome and error, and that full duplex is unavailable. |
| Voice button (event) | Announces `pressed`, `released`, `accepted`, `rejected`, `queued`, `failed` and `context_reset`; outcomes carry a stage, code and readable message. |
| Microphone active (binary sensor) | On while the remote is capturing audio. |
| Last error (diagnostic sensor) | The most recent failed request, or `none`. See [Reporting errors](#reporting-errors). |

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
- Single-hold capture is limited to 30 seconds and ends at release. This path
  disables HA's redundant end-of-speech detector, including its 15-second cutoff.
  Request/tap modes retain their local 15-second speech detector and ~0.8-second
  silence boundary. Alternating held mode has 30-second logical requests and a
  120-second physical-hold ceiling; the hardware may impose a shorter limit.
- Bad frames, unresolved sequence gaps, disconnects, queue overflow and missing
  audio abort input. Headerless audio cannot prove packet identity when CTL and
  RX counts disagree, so mismatches fail closed and record anonymous counts.
- Reconnection re-resolves GATT paths, re-subscribes and negotiates again.
  The remote may need waking. Stale busy/hold state is discarded on connection loss.
- Busy behavior is explicit and configurable. Existing pressed/released events
  remain; accepted/rejected/queued/failed/context_reset outcomes add a stage,
  code and readable reason. Satellite attributes show the current error.
- No microphone service opens capture without a physical voice notification.
- No recordings or transcripts are saved by this integration. Diagnostics omit
  identifiers/content and include capabilities, counts, errors and timings.
  Bounded history retains the last 20 captures and at most 64 anonymous boundary
  records each, so a later success does not erase the preceding failure.
  The selected pipeline/provider has its own processing and retention policy;
  cloud pipelines send speech to that provider. HA pipeline Debug can retain text.
- This integration does not change OS handling of power keys. Check host behavior
  before pressing them.

Native speech-to-speech providers can execute tools during their STT stage.
Rejecting corrupted input at the normal HA intent handoff cannot undo those
provider-side actions. Stream failures propagate as failures, never as a clean
release; uncertain actions are not retried automatically. Reusing an HA
conversation ID does not guarantee provider memory survived a cancelled session.

## Reporting errors

The diagnostic **Last error** sensor is an enum sensor whose state is the
root-cause code of the most recent failed request (shown as readable text in the
Home Assistant language), `none`, or `other` for provider-defined codes. The raw
`code`, the `stage`, the `since` time and the request number (`session`) are attributes; use
the `state_translated()` template function for the readable text in automations.
It clears when a later request succeeds or the integration reloads, and stays
available while the remote is disconnected.
Busy rejections are not faults: they appear only in the voice button event and the
satellite attributes. The voice button event still announces every occurrence.

## Troubleshooting

- **No paired devices / Bluetooth unavailable:** check the host bond and HA Core
  access to local system D-Bus. A bond on another computer is insufficient.
- **Wake remote:** press an arrow, wait a few seconds, then retry. Do not use power.
- **Unsupported profile:** download redacted diagnostics and report the model,
  protocol/capabilities and integration/HA version. Do not publish addresses/audio.
- **Audio sequence gap:** retry close to the adapter; inspect last-session frame
  counters and the bounded capture history. Mismatches abort instead of being
  treated as clean EOF. This does not establish whether radio loss, notification
  ordering or another transport condition caused a particular mismatch.
- **No speech / audio stalled:** check the actual microphone control and selected
  speech-to-text provider. A normal HID key cannot substitute for mic activation.
- **No reply:** verify command mode, selected pipeline TTS and configured speaker.
  Speaker busy/unavailable and playback start/finish timeouts are distinct errors.
- **Reconnect:** wake the remote and allow up to 30 seconds for retry. Reload the
  integration if necessary; re-pair externally only when the bond itself is broken.

Physical coverage does not establish all-model compatibility or behavior under
battery depletion, radio interference, adapter removal or a host reboot.

## Acknowledgements

Thanks to the open-source projects whose designs informed this integration:

- [matt123p/ha-gemini-live](https://github.com/matt123p/ha-gemini-live), a Gemini
  Live integration for Home Assistant. Its documentation of streaming TTS,
  low-latency satellite playback and barge-in requirements shaped how replies are
  streamed and what is reported as unavailable here.
- [craig-b/ha-openai-realtime](https://github.com/craig-b/ha-openai-realtime), an
  OpenAI Realtime voice assistant for Home Assistant. Its playback pre-buffering
  and real-time paced delivery informed the streaming head start option.

No source code was copied from either project.

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
configuration, real Core speech-stream duration, early playback, both-stage busy
policies, queue expiration, held logical turns and owned speaker cancellation.
New streaming/held behaviors need physical acceptance testing before a hardware
compatibility claim; unit tests alone do not establish acoustic latency. macOS may have an upstream HA/native
extension shutdown crash; use Linux for the complete lifecycle suite.

For contributions, supply synthetic protocol vectors and failing regression
tests before changing framing/decoder behavior. Include upstream revision and
license attribution. Keep personal configuration, packet captures, recordings,
credentials, deployment notes and research outside this repository. Verify new
remote models physically before upgrading their compatibility claim.

`scripts/check_package.py` creates `dist/atvv_remote.zip` containing only component
files. No public release or publishing is performed automatically.
