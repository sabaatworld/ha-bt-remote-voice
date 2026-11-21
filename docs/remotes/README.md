# Remote evidence

A remote is only called **tested** in the main README when a person used it with this integration on real
hardware. This page says what evidence is kept in the repository and how to add more.

## What may be kept here

- A remote's **HID report descriptor** as hex (`tests/fixtures/*.hex`). It describes the remote's input device;
  it is configuration data, not a recording.
- The **remote report** a user shared (`schema` in `custom_components/bt_remote_voice/report.py`), with the address
  already masked to its first three bytes.

Never commit packet captures, audio, full Bluetooth addresses or household details.

## Evidence in the repository

| Remote | Evidence | Used by |
| --- | --- | --- |
| Amazon Alexa Voice Remote with TV controls (product ID `0x0413`) | Report descriptor `tests/fixtures/fire_0413_report_descriptor.hex` (input reports `0x01`, `0x02`, `0x03`, `0xF0` audio, `0xF1`; output `0xF2` microphone enable, `0xF3`) | `tests/test_hid_descriptor.py`, `tests/test_fire.py`, `tests/test_report.py` |

## Adding a remote

1. Ask the person who owns it to follow [My remote is not recognised](../../README.md#my-remote-is-not-recognised)
   (and **Test my remote** if they can) and share the report in a new issue.
2. From the report, copy `hid.descriptor_hex` into `tests/fixtures/<name>_report_descriptor.hex`.
3. Add tests that parse it (`parse_reports`, `find_voice_reports`, `decode_keys`) and state what the integration
   must do with it. Support is widened only from evidence like this, never from a product ID or a model name.
4. Update the table above and the remotes table in the main README, keeping "tested" for real hardware use only.
