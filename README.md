# AquaHome — Home Assistant integration for iQua water softeners

[![CI](https://github.com/deltasystems-pl/aquahome-ha/actions/workflows/ci.yml/badge.svg)](https://github.com/deltasystems-pl/aquahome-ha/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2025.11%2B-41BDF5.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Made for ESPHome-free iQua devices](https://img.shields.io/badge/iot__class-cloud__polling-blue.svg)

Custom Home Assistant integration for **AquaHome 20 Smart** and other water
treatment devices supported by the iQua mobile app. It talks to the same iQua
cloud API the official Android app uses (`api.myiquaapp.com`, with automatic
fallback to the `iqua2.com` host for migrated accounts) — no local access or
extra hardware required.

> **Status: early development (`v0.0.1`) — not yet published to the default HACS store.** Install as a custom repository (below).

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Entities](#entities)
- [Example automations](#example-automations)
- [Blueprints](#blueprints)
- [Development](#development)
- [Continuous integration](#continuous-integration)
- [Repository layout](#repository-layout)
- [Disclaimer](#disclaimer)

## Features

- **UI setup, no YAML** — sign in with your iQua account; the correct cloud
  host is detected automatically. Email-verification and re-authentication
  flows are handled.
- **Water telemetry** — salt level, water used today, treated water available,
  lifetime totals, capacity remaining, out-of-salt estimate, and per-weekday
  average usage.
- **Regeneration** — current status, time remaining, next/last regeneration,
  and total recharge count.
- **Alerts** — low salt, excessive water use, flow-monitor, resin, connection
  and error-code alerts, surfaced as binary sensors plus an `event` entity.
- **Controls** — regenerate now / schedule / cancel, silence alarm, vacation
  mode, and more.
- **Hardware-aware** — the water shutoff valve, leak detectors, and their
  related controls appear **only** on devices that report those capabilities
  (feature-gated), so you never get dead entities.
- **Diagnostics** — downloadable, credential-redacted config-entry
  diagnostics, and stable entity identities preserved across upgrades.

## Requirements

- Home Assistant **2025.11** or newer.
- An iQua app account (email + password) with at least one device.
- Outbound internet access from Home Assistant to the iQua cloud.

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS → Integrations**.
2. Open the **⋮** menu (top-right) → **Custom repositories**.
3. Add the repository URL `https://github.com/deltasystems-pl/aquahome-ha`
   and choose category **Integration**.
4. Find **AquaHome (iQua water softener)** in the list and click **Download**.
5. **Restart Home Assistant.**

### Manual

1. Copy `custom_components/aquahome/` into your Home Assistant
   `config/custom_components/` directory.
2. **Restart Home Assistant.**

## Configuration

After restarting, add the integration from the UI — there is nothing to put in
`configuration.yaml`.

1. Go to **Settings → Devices & Services → Add Integration** and search for
   **AquaHome**.
2. **Sign in** with the email and password you use in the iQua app. The API
   host is probed automatically.
3. **Verify (if prompted).** Some accounts require email verification; enter
   the confirmation code sent to your inbox.
4. The integration discovers the device(s) on the account and creates the
   entities. Done.

If the stored credentials later stop working (password change, expired token),
Home Assistant raises a **Re-authenticate** prompt — just re-enter your
password.

> **Credentials & security:** your password is exchanged for a 24-hour access
> token plus a long-lived refresh token, which are stored in the config entry.
> The integration mimics the official app's request signature and honours the
> cloud's rate limits.

## How it works

`iot_class: cloud_polling`. The integration keeps three independent refresh
cadences so the parts that change often stay fresh without hammering the API:

| Data | Interval |
| --- | --- |
| Device snapshot (salt, usage, regeneration, diagnostics) | every **10 min** |
| Alerts / activity feed | every **30 min** |
| Device settings (hardness, schedules, capabilities) | every **6 h** |

A **Refresh data** button forces an immediate poll on demand. Device
capabilities are re-detected on the settings cadence, so entities for hardware
you add later (e.g. a leak detector) appear automatically.

## Entities

Exactly which entities appear depends on your model and its reported features.

- **Sensors** — salt level, water used today, treated water available, total
  water, capacity remaining, out-of-salt estimate, average daily use (overall
  and per weekday), regeneration status / time remaining / next / last, days
  since last recharge, days powered up, total recharges, total salt used, total
  hardness removed, RF signal strength, model, serial number, controller &
  Wi-Fi firmware, error codes, latest alert.
- **Binary sensors** — online, regenerating, vacation mode, regeneration
  suspended, and the alert flags (salt, error code, flow monitor, connection,
  water usage, resin, alarm sounding, water-to-drain). Shutoff-valve and
  leak-detector states appear on capable hardware.
- **Buttons** — regenerate now, schedule regeneration, cancel regeneration,
  silence alarm, refresh data, advance valve, reset error code(s), vacation
  mode, enable/disable recharge.
- **Number / Select** — device settings surfaced as adjustable entities (e.g.
  water hardness, regeneration timing), built dynamically from what the device
  exposes.
- **Valve** — water shutoff valve (feature-gated).
- **Switch** — leak-detector scan (feature-gated).
- **Event** — an `alert` entity that fires on each new device alert, with a
  normalized `event_type` (low salt, excessive water use, shutoff-valve opened,
  connection online/offline, …).

## Example automations

Notify when the softener runs low on salt:

```yaml
automation:
  - alias: "Water softener low on salt"
    triggers:
      - trigger: state
        entity_id: binary_sensor.aquahome_salt_level_alert
        to: "on"
    actions:
      - action: notify.mobile_app_phone
        data:
          title: "Water softener"
          message: "Salt is low — top up the brine tank."
```

React to any device alert via the `event` entity:

```yaml
automation:
  - alias: "Log AquaHome alerts"
    triggers:
      - trigger: state
        entity_id: event.aquahome_alert
    actions:
      - action: logbook.log
        data:
          name: "AquaHome"
          message: "Alert: {{ trigger.to_state.attributes.event_type }}"
```

## Blueprints

Four ready-made automation blueprints ship with the integration. They build on
the daily water-usage analysis and the opt-in automation switches, and they all
follow the same rule: **nothing that affects the device happens without your
confirmation.** Click a badge to import a blueprint straight into your Home
Assistant, then create an automation from it.

| Blueprint | What it does |
| --- | --- |
| [![Import the AquaHome leak alert blueprint.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fdeltasystems-pl%2Faquahome-ha%2Fmain%2Fblueprints%2Fautomation%2Faquahome%2Fleak_alert.yaml) **Leak alert** | Asks "was this you?" when the nightly analysis suspects a leak, escalates to a high-priority alert on "no" or on silence, and offers to close a shutoff valve — only ever on an explicit tap. |
| [![Import the AquaHome auto vacation blueprint.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fdeltasystems-pl%2Faquahome-ha%2Fmain%2Fblueprints%2Fautomation%2Faquahome%2Fauto_vacation_presence.yaml) **Auto vacation** | Turns vacation deferral on once your presence entity has been away long enough, and off again the moment someone returns. |
| [![Import the AquaHome smart regeneration companion blueprint.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fdeltasystems-pl%2Faquahome-ha%2Fmain%2Fblueprints%2Fautomation%2Faquahome%2Fsmart_regeneration_companion.yaml) **Smart regeneration companion** | Reports every scheduling decision — regeneration scheduled, deferred, or deferral limit reached — with a one-tap "cancel tonight's regeneration" button. |
| [![Import the AquaHome usage anomaly check blueprint.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fdeltasystems-pl%2Faquahome-ha%2Fmain%2Fblueprints%2Fautomation%2Faquahome%2Fanomaly_check.yaml) **Usage anomaly check** | Asks whether an unusually high water day was yours; a "no" gets a short leak-check list and the current leak-watch reading. |

The interactive buttons need the Home Assistant Companion app. With any other
notification service the messages still arrive — the buttons are simply
ignored, and each blueprint documents what happens then.

## Development

Requires Python **3.13**.

```bash
pip install -r requirements_test.txt   # test/lint toolchain (pins HA version)
pre-commit install                     # optional: run checks on commit

ruff check .                           # lint
ruff format --check .                  # formatting
mypy custom_components tests           # static typing
pytest --cov=custom_components.aquahome --cov-report=term-missing   # tests + coverage
```

Tests use `pytest-homeassistant-custom-component` and mock the cloud with
`aioresponses`; fixtures are real, credential-redacted API payloads.

## Continuous integration

Every push to `main`, every pull request, and manual dispatch run
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). Concurrent runs on the
same ref cancel each other. The workflow has five independent jobs:

| Job | What it checks |
| --- | --- |
| **Ruff** | Lint rules and code formatting (`ruff check` + `ruff format --check`). |
| **Mypy** | Static type checking of `custom_components` and `tests`. |
| **Pytest** | Full test suite with coverage report. |
| **Hassfest** | Home Assistant's manifest/structure validation. |
| **HACS validation** | Verifies the repo meets HACS integration requirements. |

## Repository layout

- `custom_components/aquahome/` — the integration (API client, coordinators,
  config flow, and entity platforms).
- `blueprints/automation/aquahome/` — the bundled automation blueprints.
- `tests/` — the test suite.
- `reverse-engineering/` — maintainer-only git submodule (restricted access). It is **not**
  required to build, run, install, or contribute to the integration; a normal
  clone works without it.

## Disclaimer

This project is not affiliated with, endorsed by, or supported by iQua,
EcoWater Systems, or AquaHome. Use at your own risk. Licensed under the
[MIT License](LICENSE).
