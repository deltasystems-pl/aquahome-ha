# AquaHome — Home Assistant integration for iQua water softeners

Custom Home Assistant integration for **AquaHome 20 Smart** and other water
treatment devices supported by the iQua mobile app. The integration talks to
the same iQua cloud API the official Android app uses (`api.myiquaapp.com`).

> **Status: under development — not yet released.**

## Planned feature set

- Config-flow setup with iQua account credentials (no YAML), reauth support.
- Water telemetry: salt level, daily/total water usage, treated water
  available, regeneration status and history, device diagnostics.
- Long-term statistics backfill (usage history in the Energy dashboard).
- Salt-consumption intelligence and water-usage analytics (leak detection,
  vacation detection) — see the research reports in `private/knowledge/research/`.
- Controls: regenerate now/schedule/cancel, alarm silence; water shutoff valve
  and leak detectors on hardware that has them (feature-gated).

## Repository layout

- `custom_components/aquahome/` — the integration.
- `private/` — private git submodule with the verified API reference, device
  facts, research reports, and entity analysis the implementation is grounded
  in (`private/knowledge/`, `private/docs/`). Access is restricted.
- `tests/` — test suite (fixtures are real, redacted API payloads).

## Disclaimer

This project is not affiliated with, endorsed by, or supported by iQua,
EcoWater Systems, or Aquahome. Use at your own risk.
