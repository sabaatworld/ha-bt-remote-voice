# ATVV Remote Voice for Home Assistant

A Home Assistant custom integration for push-to-talk voice from compatible,
already-paired Android TV Voice over BLE (ATVV) remotes.

## Status

Early development. This repository currently contains project setup only;
there is no installable integration yet.

## Intended scope

- Use an already-paired remote; pairing is managed outside this integration.
- Send microphone audio to a selected Home Assistant Assist pipeline.
- Start with physical hold-to-talk and TVision BT19K support.
- Expose voice-session status for Home Assistant automations.

Additional remote models will require individual compatibility verification.

## Project contents

This repository is for reusable integration code, tests using synthetic data,
and user-facing documentation. Private installation settings, device addresses,
audio recordings, traffic captures, credentials, and personal research notes
must stay outside it.

## Related projects

- [ATVVoice](https://github.com/b0o/ATVVoice)
- [SiriRemoteESP](https://github.com/Jakkumn/SiriRemoteESP)

Any future code reuse must retain the applicable upstream license notices.
