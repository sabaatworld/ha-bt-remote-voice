<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/bt_remote_voice/brand/dark_logo@3x.png">
  <source media="(prefers-color-scheme: light)" srcset="custom_components/bt_remote_voice/brand/logo@3x.png">
  <img src="custom_components/bt_remote_voice/brand/logo@3x.png" width="100%" alt="Bluetooth Remote Voice to Home Assistant">
</picture>

**Talk to Home Assistant with the remote you already own.**

Hold the voice button on a Bluetooth TV remote, say what you want, and hear the answer from
a speaker of your choice. Bluetooth Remote Voice turns the microphone in remotes that speak
the Android TV Voice (ATVV) protocol, or in the Amazon Fire TV voice remote, into a native
Home Assistant Assist satellite: your pipeline and your conversation agent, including live
speech-to-speech providers such as [Gemini Live](https://github.com/matt123p/ha-gemini-live) and
[OpenAI Realtime](https://github.com/craig-b/ha-openai-realtime).

<p align="center">
  <img src="docs/images/how-it-works.gif" width="640" alt="Hold the remote's voice button and speak. The voice goes over Bluetooth to Home Assistant, which turns on the light and answers through a media player.">
</p>

- **Hold to talk, release to send,** or hold and keep a back-and-forth conversation going.
- **Any Assist pipeline,** local or cloud, classic or live.
- **Replies where you want them:** on a media player you choose, optionally only when a
  condition holds, for example only when you are in that room.
- **Local and private.** Bluetooth goes straight to the Home Assistant machine; there is no cloud
  service of its own and no recordings are kept.
- **Built for real remotes.** Hardware-tested, with sleep and wake handling, busy-request policies
  (reject, queue or replace) and Repairs notices when a pipeline or speaker goes missing.

How it works: pair the remote with the Home Assistant machine, add the integration, then hold
the voice button and speak. It needs Home Assistant 2026.11.1 or later on Linux with a Bluetooth
adapter built into or plugged into the Home Assistant machine.

## Install

<!-- ha-marketplace:exclude-start -->

### 1. Install from the Marketplace

[![Open your Home Assistant instance and show this repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=sabaatworld&repository=ha-bt-remote-voice&category=integration)

Or add it by hand (Home Assistant 2026.11 or later, which has the Marketplace built in):

1. Go to **Settings → Marketplace**.
2. Select **Add from link** in the upper-right corner, enter `sabaatworld/ha-bt-remote-voice` (or the
   full GitHub URL) and select **Add**.
3. Select **Bluetooth Remote Voice** in the list, then **Install**, then **Install** again to confirm
   the version. Restart Home Assistant if it asks you to.

<!-- ha-marketplace:exclude-end -->

### Trying a beta release

Beta versions (for example `0.6.0-beta.1`) are published as GitHub pre-releases. The Marketplace offers
stable releases only, until you opt in for this repository:

1. Open **Settings → Devices & services → Marketplace** and select the device for **Bluetooth Remote Voice**.
2. Enable the **Pre-release** entity (it is disabled by default), then turn its switch on.
3. Open **Settings → Marketplace**, select **Bluetooth Remote Voice** and install the update (or use the
   three-dot menu and **Install another version**). Restart Home Assistant when asked.

Turn the switch off again to go back to stable releases. Do not use **Install another version** to roll
back after an update; restore a backup instead. Please report what you find in the
[issues](https://github.com/sabaatworld/ha-bt-remote-voice/issues).

### 2. Pair your remote

Pair the remote with the Home Assistant machine itself; this integration never pairs remotes.
See [Pairing a remote](#pairing-a-remote).

### 3. Add the integration

[![Open your Home Assistant instance and start setting up Bluetooth Remote Voice.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=bt_remote_voice)

Or go to **Settings → Devices & Services → Add integration → Bluetooth Remote Voice**. Wake the
remote with an arrow button, select it, then keep **Preferred pipeline** or choose an existing
Assist pipeline. Later, select a speaker and **Execute commands** in **Configure** when you are
ready to process requests and hear replies.

If setup says the remote is not supported, or it picked the wrong kind of remote, change **Remote type**
on the same screen and submit again; to ask for help, tick **Show remote report** (see
[My remote is not recognised](#my-remote-is-not-recognised)).

The initial mode stops after speech recognition. This does not disable tools embedded inside a live
STT provider: such a provider may act during speech processing. Use a dedicated transcription-only
STT provider when actions must be disabled.

<details>
<summary>Manual installation (without the Marketplace)</summary>

Download `bt_remote_voice.zip` from the latest
[release](https://github.com/sabaatworld/ha-bt-remote-voice/releases), unzip it into
`config/custom_components/bt_remote_voice`, and restart Home Assistant.

</details>

## Supported remotes

Remotes that speak the Android TV Voice (ATVV) protocol over Bluetooth LE can work,
and so can the Amazon Fire TV voice remote (tested for held requests). "Tested" means a
person used it with this integration on real hardware; unit tests with synthetic vectors
do not count.

| | Remote | Voice protocol | Status |
| --- | --- | --- | --- |
| <a href="docs/images/bt19k-tvision-remote.jpg"><img src="docs/images/bt19k-tvision-remote.jpg" width="96" alt="BT19K (T-Mobile TVision) remote"></a> | BT19K (T-Mobile TVision remote) | ATVV 1.0, 16 kHz IMA ADPCM, hold-to-talk | **Tested.** Held requests, queueing, alternating conversation and replies on a media player all verified on hardware. |
| | Other ATVV 0.4 / 1.0 remotes | ATVV, 8 or 16 kHz ADPCM | Supported by protocol and covered by synthetic tests; not hardware tested. Reports welcome. |
| <a href="docs/images/fire-tv-voice-remote.jpg"><img src="docs/images/fire-tv-voice-remote.jpg" width="96" alt="Amazon Fire TV voice remote"></a> | Amazon Fire TV voice remote (Bluetooth LE generation, product ID `0x0413` seen) | HID-over-GATT with Opus audio, **not** ATVV | **Tested for held requests.** On one remote (product ID `0x0413`) a held request records, transcribes and plays a reply on a media player through this integration in Home Assistant OS, including after the remote sleeps and reconnects; the optional **Ignore short presses** setting removes the false failure from the wake-up tap. **Not tested:** holds longer than about 40 seconds and Alternating conversation. It needs access to `/dev/hidraw*` and an Opus decoder; see [Fire TV voice remote](#fire-tv-voice-remote). |
| | Roku voice remote (model `RC-051`) | Wi-Fi Direct to the Roku | **Not usable.** Its FCC grant lists only Wi-Fi bands and no Bluetooth band, and it was not seen advertising over Bluetooth in a scan while idle. It talks only to a Roku device. |
| | Apple Siri Remote | Apple HID-over-GATT with Opus audio | Not supported. The [SiriRemoteESP](https://github.com/Jakkumn/SiriRemoteESP) ESP32 bridge is a working alternative. |

Remotes that use Wi-Fi, infrared or RF for voice, and Bluetooth bridges that need
an ESPHome Bluetooth proxy, cannot be used: this integration needs a Bluetooth
adapter on the Home Assistant machine itself.

## Which remote do I have?

Answer two questions; **Remote type** can stay on **Automatic** unless setup tells you otherwise.

1. **Does the remote come with an Android TV or Google TV box, and does its microphone button start voice by itself?**
   That is an Android TV (ATVV) remote, for example the BT19K. Choose or keep **Automatic**.
2. **Is it an Amazon Fire TV or Alexa voice remote?** Choose or keep **Automatic**; Fire remotes are detected from
   Amazon's identity in their Bluetooth data and from the voice reports their input device declares. These remotes
   stream voice only while the microphone key is held.
3. **Not sure?** Leave **Automatic**. If setup fails, see [My remote is not recognised](#my-remote-is-not-recognised).

Amazon has made many remotes. This table says what is known; "tested" means a person used it with this
integration on real hardware, everything else is based on other people's published notes or on how the remote
identifies itself, and is not a promise.

| Amazon remote | Status |
| --- | --- |
| Alexa Voice Remote with TV controls (the one tested here, product ID `0x0413`) | **Tested** for held requests. |
| Fire TV Stick 4K remote, Fire TV Stick Lite remote | Voice protocol documented by [fire-remote-client](https://github.com/justanotheruseer/fire-remote-client); not tested with this integration. |
| Alexa Voice Remote (3rd gen, 2021 and its 2022 revision; product ID `0x0414` reported) | Setup recognises it as a Fire remote and accepts its voice reports ([issue 1](https://github.com/sabaatworld/ha-bt-remote-voice/issues/1)). Whether voice works end to end is still being confirmed on that remote. |
| Alexa Voice Remote Pro, Enhanced, 2024 release | Untested. They may work the same way; share a remote report to help. |
| Older Fire TV remotes (2014-2017) | **Not supported.** The 2015 voice remotes use Wi-Fi Direct, not Bluetooth. Earlier Bluetooth ones are unverified. |

## Which button do I press?

Press and **hold the remote's own microphone (voice) button**, the one that starts voice on the TV, speak, and
release. This integration cannot choose a different button: on an Android TV remote the remote's firmware starts
voice itself, and a Fire remote only streams audio after its own microphone key is held. Other buttons on the
remote cannot start voice (see [Compatibility and microphone controls](#compatibility-and-microphone-controls)).

- **Not sure which button it is?** In setup tick **Test my remote** and submit, wake the remote with an arrow key,
  then press and hold its voice button for a few seconds. For 20 seconds the integration only listens (it does not
  open the microphone and sends the remote only the standard capability request) and then tells you what the remote did: starts voice while held,
  starts voice on a tap, sent a Fire voice key, sent only ordinary keys, or nothing. The result is also in the
  remote report (counts of key usages only).
- **Android TV (ATVV) remotes:** some start voice while the button is held; some start on a tap and listen
  until you stop speaking. The integration follows what the remote does.
- **Fire remotes:** hold the microphone key for as long as you speak. Right after the remote wakes, it can send
  the key that woke it as a quick tap; the **Ignore short presses** setting filters that.

## My remote is not recognised

If setup fails or rejects your remote, you can send a **remote report** so support can be added. It
takes one minute and needs no technical knowledge.

1. In the setup screen tick **Show remote report** and press **Submit**. The report appears in a box.
2. Select all of it and copy it. You may read it first.
3. [Open a new issue](https://github.com/sabaatworld/ha-bt-remote-voice/issues/new?template=new-remote.yml)
   and paste it, with the remote's brand and model as printed on it.

The report contains the integration, Home Assistant and Python versions, the Bluetooth adapter name
(`hci0`), what the remote's Bluetooth data says about it, whether it provides the voice reports this integration
needs, the settings that matter (never names) and the setup result. It never contains your full Bluetooth
address (only the first three bytes, which for most remotes identify the manufacturer), names, web addresses or recorded audio.
The report has these fields: `integration`, `core`, `python`, `adapter`, `remote`, `hid`, `observation`, `config`, `outcome`.

If setup cannot open at all, save the commands below as `capture.sh` on the Home Assistant host shell
(see [Pairing a remote](#pairing-a-remote)), run `sh capture.sh AA:BB:CC:DD:EE:FF` with your remote's address, and
paste the output instead. It only reads information and sends nothing to the remote; the same commands are in
`scripts/capture_remote_info.sh`. After setup, the **diagnostics** download contains the same report under
`remote_report`.

```sh
# Read-only helper for support requests when setup cannot show the Remote Report.
# Prints what support needs about one paired Bluetooth remote. It writes nothing to the remote,
# uses no GATT access and presses no keys. Addresses are masked to their first three bytes and
# the remote's Name, Alias and HID_NAME lines are left out.
#
# Usage (Home Assistant host shell, remote awake and connected): capture_remote_info.sh AA:BB:CC:DD:EE:FF
set -eu

address="${1:?Usage: $0 <remote Bluetooth address>}"

mask() {
  sed -E 's/([0-9A-Fa-f]{2})[:_-]([0-9A-Fa-f]{2})[:_-]([0-9A-Fa-f]{2})([:_-][0-9A-Fa-f]{2}){3}/\1:\2:\3:xx:xx:xx/g'
}

echo "== bluetoothctl info =="
bluetoothctl info "$address" | grep -v -E '^[[:space:]]*(Name|Alias):' | mask

for dir in /sys/class/hidraw/hidraw*; do
  [ -e "$dir/device/uevent" ] || continue
  if grep -qi "^HID_UNIQ=$address" "$dir/device/uevent"; then
    echo "== $(basename "$dir") uevent =="
    grep -v '^HID_NAME=' "$dir/device/uevent" | mask
    echo "== $(basename "$dir") report descriptor (hex) =="
    od -An -v -tx1 "$dir/device/report_descriptor" | tr -d ' \n'
    echo
  fi
done
```

## Pairing a remote

This integration never creates Bluetooth bonds; pair the remote with the machine
that runs Home Assistant, then add it in Devices & Services. A remote bonds to one
host at a time, so pairing it here breaks its pairing with the TV or box it came
with (you pair it back with that device's own settings afterwards).

**Home Assistant OS** (recommended; it is the setup the integration is tested on) has two
kinds of terminal. They are not the same:

| Terminal | How you reach it | Use it for pairing? |
| --- | --- | --- |
| **Host shell** ("developer SSH") | `ssh -p 22222 root@<ha-host>` after enabling it with the [developer SSH key setup](https://contributors.home-assistant.io/os/operating-system/debugging/); lands on the HA OS prompt | **Yes.** The reliable route: it keeps the pairing agent and the bond. |
| **SSH / Terminal app** | `ssh root@<ha-host>` (port 22) or the web terminal; a container named `core-ssh` | Not recommended. It reaches the same Bluetooth service, but pairing from it has connected and then lost the bond. |

Steps (a hold-to-talk remote that is awake, near the Home Assistant machine):

1. Put the remote into pairing mode as its manual describes (for many remotes,
   hold a button such as Home or a dedicated pairing key for about 10 seconds).
2. In the host shell, start `bluetoothctl` and run:

   ```text
   power on
   agent NoInputNoOutput
   default-agent
   scan on
   ```

   Wait for the remote to appear, then note its address (`<address>`). Remotes
   often use a changing private address; scan again if a pairing attempt times out.
3. Pair, trust and connect. If an `Request authorization` prompt appears, answer
   `yes`:

   ```text
   pair <address>
   trust <address>
   connect <address>
   ```

   A one-shot `bluetoothctl pair <address>` does not keep a pairing agent, so use
   the interactive prompt.
4. Check `bluetoothctl info <address>` shows `Paired: yes`, `Bonded: yes` and
   `Connected: yes`. Then add **Bluetooth Remote Voice** in Home Assistant and wake the
   remote with an arrow button when you submit the form.

If pairing connects but does not stay bonded, put the remote back into pairing
mode (some remotes forget their old pairing only after a factory reset, described
in their manual) and try again. Do not press a remote's power buttons while testing:
on many remotes they also send infrared commands to a TV.

Home Assistant Container and Supervised installs use the same `bluetoothctl`
commands in a shell on the host; a Bluetooth adapter and BlueZ must be available
to Home Assistant itself.

## Configuration reference

Select **Configure** on the integration to change these settings; changes reload
the integration. Every setting has a short description in the form itself; this
section adds when to use each one. All text, including entity names, option
labels and error messages, comes from Home Assistant's translation files
(every Home Assistant language is provided; see [Languages](#languages)). Every option has a default (shown in bold in the table), so a setup behaves the same
until you choose otherwise.

### Setup (once)

| Setting | What it does |
| --- | --- |
| Paired remote | A device already paired and bonded on the HA host. This integration never creates Bluetooth bonds. |
| Assist pipeline | The pipeline that processes speech: STT, conversation agent and TTS. Any pipeline works, including live speech providers. **Preferred pipeline** (the default) uses whichever pipeline Home Assistant currently marks as preferred, so changing the preferred pipeline applies to the next request. Changing it later does not need re-pairing. |

### Options

| Option | What it does | Choices (default in bold) | When to use it |
| --- | --- | --- | --- |
| Assist pipeline | The pipeline that processes speech: speech-to-text, conversation agent and text-to-speech. | **Preferred pipeline** (follows Home Assistant's preferred pipeline) or any existing pipeline; the default for a new remote is Preferred | You want a different agent or speech provider than Home Assistant's preferred one. A removed explicit pipeline, or no preferred pipeline at all, fails with a clear error instead of silently falling back. |
| Voice mode | Decides how far a request goes. **Transcription** stops after speech recognition: nothing is run and no reply plays. **Commands** runs the whole pipeline and, with a speaker, speaks the reply. | **Transcription**; Commands | Start in Transcription to check the microphone path and the speech provider; switch to Commands when you want actions and replies. |
| Reply speaker | The media player that speaks replies. With none, the pipeline stops at the intent stage and nothing is played. It must report when playback starts and finishes. | Any media player that can play media, or **none** | You want spoken replies. Used only in Commands mode. |
| Reply condition | Conditions, built with Home Assistant's condition editor, that must pass for a reply to be spoken. Checked once at the start of each request. See [Reply condition](#reply-condition). | Any Home Assistant conditions (state, numeric state, zone, time, template, and, or, not); **empty (always speak)** | You want a reply spoken only sometimes, for example only when you are in the room the reply speaker is in. |
| Reply timing | When playback of the reply begins. **Automatic** starts speaking as soon as the pipeline reports a streamed reply. **Wait for complete reply** plays a finished audio file. Short replies may not stream at all, in which case the completed reply is played. | **Automatic**; Wait for complete reply | Automatic gives the earliest start if the provider produces speech faster than real time. Choose Wait when a provider only produces speech at about real-time pace or the speaker stutters on a live stream: the start is later but it avoids breakup. |
| Streaming head start | Waits this long after a streamed reply is ready before asking the speaker to play, or starts at once if the reply finishes sooner. It gives the speaker a lead but cannot help if the provider keeps producing speech more slowly than it plays. No effect with Wait for complete reply or when the reply is not streamed. | 0 to 10 seconds in 0.5 s steps (**0**) | Streamed replies break up in the first seconds. Try 0.5 to 3 seconds. |
| New press while processing | What happens when the voice button is pressed before the previous request is answered. **Reject** ignores the press. **Queue one** keeps one pending request and runs it afterward. **Replace** cancels the current request so the new one starts at once; this cannot undo an action the old request already performed. | **Reject**; Queue one; Replace current request | Reject is the safest. Use Queue one for quick follow-ups and Replace when the newest request should win. |
| New press during reply | What happens when the button is pressed while a reply is playing. **Reject** lets the reply finish. **Queue one** asks the next question while the reply plays and answers it afterward. **Replace** stops the reply and starts the new request. | **Reject**; Queue one; Replace current reply | Use Queue one or Replace for fast back-and-forth instead of waiting for each reply to end. |
| Conversation context | Whether requests share one Home Assistant conversation. **Reuse** keeps it, so follow-ups such as "and double that?" work (while Home Assistant retains it). **Fresh** makes every press independent. | **Reuse**; Fresh per request | Use Fresh for independent one-shot commands. |
| Button behavior | How a hold maps to requests. **One request per hold** records one request and ends at release. **Alternating conversation** keeps listening while the button is held: speak, pause about 0.8 s, hear the reply, then speak again without releasing. | **One request**; Alternating conversation | Alternating needs a hold-to-talk remote and suits a longer back-and-forth. |
| Remote type | Which kind of remote this is, so the integration talks to it the right way. **Automatic** uses what was detected when you added the remote. **Android TV / Google TV remote (ATVV)** is for remotes whose own microphone button starts voice. **Amazon Fire TV / Alexa voice remote** is for Fire remotes, which stream voice while the microphone key is held. Changing it reconnects the remote. | **Automatic** | Setup says your remote is not supported, or it was detected as the wrong type. Setup also offers this choice, so you are never stuck on a remote that was not recognised. A manual choice never works if the remote lacks the voice features it needs; the error then says what is missing. |
| Ignore short presses | Ignores voice-button taps shorter than the time you set, so a spurious tap never starts a request (no request, no failure, no Last error). A real hold starts that much later. | 0 to 0.5 seconds in 0.05 s steps (**0**, off) | A Fire TV remote that fails with "No microphone audio" right after waking: a sleeping Fire remote re-sends the key that woke it as an instant tap. Try 0.1 s. Fire TV remotes only; other remotes ignore it. |
| Maximum request length | The longest a single request can last on any remote; reaching it ends the request with a capture-limit error. In One-request mode it is the longest hold; in Alternating mode it bounds each turn. Remotes that are not hold-to-talk also end at about 15 seconds of speech or at silence, whichever comes first. | 5 to 120 seconds in 5 s steps (**30**) | Lower it to stop a stuck button from recording; raise it for long dictation. |
| Maximum conversation length | The longest a whole hold can last in Alternating conversation mode. Must not be shorter than the request length. | 30 to 600 seconds in 10 s steps (**120**) | You use Alternating conversation and want longer or shorter conversations. Ignored in One-request mode. |

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

### Reply condition

With a reply speaker selected, the **Reply condition** decides whether a reply is spoken.
It uses Home Assistant's own condition editor, the same one automations use, so you can
combine state, zone, time and template conditions without writing code.

- **When it is checked:** once at the start of each request, and for each turn in
  Alternating conversation. A queued request is checked when it starts running, not
  when you pressed the button. A change after that is ignored for that request.
- **Empty** (the default) means replies are always spoken.
- **A condition that cannot be evaluated** (for example a state condition on an entity
  that does not exist, or a template error) counts as not met, like in automations,
  and is logged once.
- **What "not met" does:** the request still runs exactly as before (commands still
  execute, the conversation continues, and live and classic pipelines behave the
  same); only the spoken reply is skipped. Speech is still generated, so a skipped
  reply does not save speech-generation cost. The voice button event entity reports a
  `reply_skipped` event with the code `condition_false` or `condition_error`; it is
  not a failure and does not touch the Last error sensor. In Alternating conversation
  the next turn is ready straight away.
- **Template conditions** can use these variables. They are a stable contract:
  - `speaker`: the entity ID of the reply speaker.
  - `remote`: the name of this remote entry.
  - `held`: `true` when Button behavior is Alternating conversation.

Examples: a state condition `binary_sensor.office_occupancy` is `on` speaks only
while the office is occupied; a template condition
`{{ is_state('person.alex', 'home') and speaker == 'media_player.office' }}` ties
the rule to one speaker.

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
| Voice button (event) | Announces `pressed`, `released`, `accepted`, `rejected`, `queued`, `failed`, `context_reset` and `reply_skipped`; outcomes carry a stage, code and readable message. |
| Microphone active (binary sensor) | On while the remote is capturing audio. |
| Last error (diagnostic sensor) | The most recent failed request, or `none`. See [Reporting errors](#reporting-errors). |

## Compatibility and microphone controls

| Protocol | Audio | Voice control | Evidence |
| --- | --- | --- | --- |
| ATVV 1.0 | 8/16 kHz IMA/DVI ADPCM, headerless + sync | Hold, press-to-talk, OnRequest | BT19K 16 kHz hold tested; other combinations synthetic |
| ATVV 0.4 | 8/16 kHz IMA/DVI ADPCM, six-byte frame header | OnRequest voice notification → microphone request | [ATVVoice](https://github.com/b0o/ATVVoice)-derived vectors; hardware untested |
| Amazon Fire TV voice remote | HID-over-GATT; Opus 16 kHz mono, one 80-byte packet per HID report | Hold-to-talk with the microphone key; read through the kernel's `hidraw` device | Tested for held requests on one remote (product ID `0x0413`); streamed 37.8 s continuously in a probe; long holds and Alternating conversation not tested through the integration |
| Apple Siri Remote | Apple HID-over-GATT, Opus | Separate transport | Unsupported; [SiriRemoteESP](https://github.com/Jakkumn/SiriRemoteESP) is a separate ESP32 receiver option |

Capabilities choose the format; there is no model-name allowlist or key-learning
wizard. A standard voice-control notification is the trigger, so no individual
key needs teaching. Ordinary buttons that do not activate the microphone are
outside this integration. Press-to-talk is used only when advertised by the
remote; this integration does not turn BT19K's hold control into tap-to-toggle.
Unknown versions/codecs are rejected rather than guessed. See upstream
attribution in [third-party notices](custom_components/bt_remote_voice/THIRD_PARTY_NOTICES.md).

## Fire TV voice remote

Amazon's Bluetooth LE Fire TV voice remotes do not use ATVV. This integration reads their
voice through the kernel's HID device for the paired remote (`/dev/hidraw*`), because
BlueZ does not let applications write to the HID service. What that needs:

- **Device access.** Home Assistant must be able to open `/dev/hidraw*`. Home Assistant OS
  allows this (its core container sees and can write the node, which appears only while the
  remote is connected). Container and Supervised installs need the device passed through and
  a host kernel that provides `uhid`. A missing or unreadable device is reported as a
  translated error.
- **Opus decoder.** Home Assistant pins PyAV, whose wheel bundles libopus, so none is
  normally needed; the system `libopus` is used as a fallback. If neither exists the remote
  reports a translated error and ATVV remotes are unaffected.
- **Pairing.** Pair the remote on the Home Assistant machine itself (see
  [Pairing a remote](#pairing-a-remote)); for the Fire remote, hold Home for about 10
  seconds until the light blinks fast. Pairing it breaks its pairing with the Fire TV.
- **Limitation: lost audio cannot be detected.** The remote's reports carry no sequence
  numbers, so unlike ATVV a dropped frame cannot be noticed, and damaged audio inside a
  valid packet cannot be detected either. Packets of the wrong size or format, decoder
  failures and stalls still abort the request.
- **Waking.** The remote sleeps when idle and its entities show unavailable while its
  Bluetooth link is down; they return within about two seconds of it reconnecting. On
  the remote tested, the voice button did not wake it (another key did). The remote then
  re-sends the key that woke it about four seconds later as an instant press and release,
  which, if that key was the voice button, starts a request that fails with "No
  microphone audio". The setting **Ignore short presses** (see the configuration
  reference) filters those taps. For roughly the first minute after a wake the remote
  also started streaming audio two to four seconds late (about 0.4 s once settled), so
  the first words can be cut off: wake it with another key (not the voice button) and
  wait a few seconds before speaking.

## Behavior, privacy and limits

- Pairing, trust/bond management and Bluetooth discovery/connect ownership stay
  external. No OS packages, PipeWire daemon or proxy support are required.
- Existing Keyboard Remote and HID input integrations remain independent.
- Single-hold capture ends at release or at the configured maximum request length
  (default 30 seconds). This path disables HA's redundant end-of-speech detector,
  including its 15-second cutoff. Request/tap modes retain their local 15-second
  speech detector and ~0.8-second silence boundary. In Alternating held mode each
  turn is bounded by the maximum request length (default 30 seconds) and the whole
  hold by the maximum conversation length (default 120 seconds); the hardware may
  impose a shorter limit.
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

### Missing pipeline or speaker

If the Assist pipeline you chose explicitly (not **Preferred pipeline**, which follows Home
Assistant) or the reply speaker is later deleted or renamed,
Home Assistant shows a notice under **Settings → System → Repairs** (an error for a
missing pipeline, a warning for a missing speaker). The pipeline is checked when
Home Assistant starts and whenever you save the remote's settings, so a pipeline
deleted while running is noticed at the next restart or save (requests fail with a
clear error meanwhile). The speaker is checked live and only in Voice commands
mode, since that is the only mode that plays replies. The notice clears once you
choose an existing one in **Configure**, or the speaker returns, and it stays
dismissed if you dismissed it. A speaker that is only offline is not reported. In
**Configure**, a missing saved value stays visible with a message instead of being
replaced, and you can still save other settings.

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
- **Unsupported profile / "not supported" at setup:** try the other **Remote type**, then send a
  [remote report](#my-remote-is-not-recognised) (setup) or the diagnostics download (after setup).
  Do not publish addresses/audio.
- **"This Fire TV remote does not report voice data":** the remote's input device does not declare the
  voice reports this integration understands. The remote report shows what it does declare; share it.
- **Detected as the wrong kind of remote:** set **Remote type** to **Android TV / Google TV remote** or
  **Amazon Fire TV / Alexa voice remote** in setup or **Configure**.
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

### Getting more detail

- **Remote report** (setup) and **diagnostics** (after setup) both include **Voice checks**: for each thing the
  integration needs from a Fire remote's input device it says what it found, so a refusal explains itself.
  **Test my remote** adds a short timeline (seconds since the test began) of what the remote sent.
- **Fire remote runtime counters** are in the diagnostics download under `fire_stats`: voice-key presses,
  microphone enables written, audio frames received, the delay to the first frame, the longest gap between
  frames and the Opus header bytes seen. They show whether the remote really streams audio after a press.
- **Debug log:** on the integration's page choose **Enable debug logging**, reproduce the problem, then
  **Disable debug logging** to download the log. For a Fire remote it records the voice key going down and up, the
  microphone enable and the first audio frame; it never contains audio or a full Bluetooth address.

## Languages

The integration is translated into every language Home Assistant supports (63 besides English):
setup, options, entities, errors and Repairs notices. The translations are AI-generated and have not
been reviewed by native speakers, so wording may be off in places. Corrections are welcome as
[issues or pull requests](https://github.com/sabaatworld/ha-bt-remote-voice/issues).

<!-- ha-marketplace:exclude-start -->

## Acknowledgements and references

### Acknowledgements

Thanks to the open-source projects whose designs and research informed this
integration:

- [b0o/ATVVoice](https://github.com/b0o/ATVVoice), a Linux daemon that turns
  Android TV voice remotes (the ATVV protocol) into a PipeWire microphone. Its
  protocol research and implementation, including the ADPCM framing, versions 0.4
  and 1.0 and the vendor handshake some remotes need, guided the ATVV support here.
- [Jakkumn/SiriRemoteESP](https://github.com/Jakkumn/SiriRemoteESP), an ESP32
  receiver for the Apple Siri Remote with ESPHome and MQTT. It is the working
  option for that remote today, and its microphone and Assist lifecycle informed
  how a remote's voice is handled.
- [justanotheruseer/fire-remote-client](https://github.com/justanotheruseer/fire-remote-client)
  and [Staars/berry-examples](https://github.com/Staars/berry-examples)
  (`ble/ble_fireRC.be`), with the write-up
  [Fire TV remote BLE voice](https://reversedwithai.com/p/fire-tv-remote-ble-voice).
  They reverse-engineered the Amazon Fire TV voice remote (HID-over-GATT, report
  `0xF0` audio, `0xF2` microphone enable, Opus), which this integration then
  confirmed on hardware.

The ATVV protocol and IMA/DVI ADPCM implementation was adapted with reference to
[ATVVoice](https://github.com/b0o/ATVVoice) (MIT; its copyright notice and licence text are in
[THIRD_PARTY_NOTICES.md](custom_components/bt_remote_voice/THIRD_PARTY_NOTICES.md)). From the other projects only
ideas, protocol facts and design lessons were taken; no source code was copied.

### Useful references

For live speech-to-speech providers, which can serve as the Assist conversation agent:

- [matt123p/ha-gemini-live](https://github.com/matt123p/ha-gemini-live), a Gemini
  Live integration for Home Assistant. Its documentation of streaming TTS,
  low-latency satellite playback and barge-in requirements is a good companion to the
  streaming reply options here.
- [craig-b/ha-openai-realtime](https://github.com/craig-b/ha-openai-realtime), an
  OpenAI Realtime voice assistant for Home Assistant. Its playback pre-buffering and
  real-time paced delivery is the idea behind the streaming head start option.

For pairing and Bluetooth setup:

- [Home Assistant Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/)
  (adapters and Bluetooth proxies; proxies cannot be used by this integration).
- [Home Assistant Keyboard Remote integration](https://www.home-assistant.io/integrations/keyboard_remote/):
  its prerequisites say to pair a Bluetooth remote with the operating system first,
  which is the same first step as here. It has no `bluetoothctl` walkthrough.
- [Debugging the Home Assistant Operating System](https://contributors.home-assistant.io/os/operating-system/debugging/)
  (enabling the host shell on port 22222).
- [ArchWiki: Bluetooth](https://wiki.archlinux.org/title/Bluetooth) for a general
  `bluetoothctl` walkthrough that applies to any BlueZ system, and the
  [BlueZ project](https://www.bluez.org/).

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

Translations: `custom_components/bt_remote_voice/translations/en.json` is the only source text.
Every other Home Assistant language has its own file with the same keys. When you add or change a
user-visible string, translate it into all of them in the same change, then run
`python scripts/translation_tools.py refresh-hashes`; `tests/test_language_files.py` fails on missing
keys, damaged `{placeholders}` or Markdown, a missing language, or English text that changed without
being retranslated.

For contributions, supply synthetic protocol vectors and failing regression
tests before changing framing/decoder behavior. Include upstream revision and
license attribution. Keep personal configuration, packet captures, recordings,
credentials, deployment notes and research outside this repository. Verify new
remote models physically before upgrading their compatibility claim.

### Releases

Versions follow [SemVer](https://semver.org/) and the version in
`custom_components/bt_remote_voice/manifest.json` must equal the release tag without the `v`
(tag `v0.4.0` for version `0.4.0`). To release, update that version, merge to `main` and push
the tag. For a beta use a suffix (`0.6.0-beta.1`, tag `v0.6.0-beta.1`; only `-beta.N` and `-rc.N` are
accepted): the Release workflow then publishes it as a GitHub pre-release, which the Marketplace offers only
to people who turned on the repository's Pre-release switch, and only while it is newer than the latest
stable release. The Release workflow runs `scripts/check_package.py`, which fails if the tag and manifest
version disagree, builds `dist/bt_remote_voice.zip` (component files only) and attaches it to a
GitHub release with generated notes; the Marketplace installs from that zip. CI also runs hassfest and
the Marketplace (HACS action) validation on every push, and a weekly job runs the tests against the
Home Assistant beta.

<!-- ha-marketplace:exclude-end -->
