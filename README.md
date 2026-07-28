# AquaHome — Home Assistant integration for iQua water softeners

[![CI](https://github.com/deltasystems-pl/aquahome-ha/actions/workflows/ci.yml/badge.svg)](https://github.com/deltasystems-pl/aquahome-ha/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.2%2B-41BDF5.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![iot_class: cloud_polling](https://img.shields.io/badge/iot__class-cloud__polling-blue.svg)

Custom Home Assistant integration for **AquaHome 20 Smart** and other water
treatment devices supported by the iQua mobile app. It talks to the same iQua
cloud API the official app uses — no local access, no extra hardware, no YAML.

Beyond mirroring the app, it adds long-term water statistics imported from the
cloud's own history, a read-only analytics tier (leak watch, usage anomalies,
absence detection, usage forecast), strictly opt-in automation, and an
on-demand live mode that streams water flow and counters within seconds while
you are watching.

> **Status: v1.0.0 — installed as a HACS *custom repository*** (not yet in the
> default HACS store). See [Installation](#installation).

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Setup](#setup)
- [How it works](#how-it-works)
- [Entities](#entities)
- [Live mode](#live-mode)
- [Analytics and automation](#analytics-and-automation)
- [Actions](#actions)
- [Statistics and the Energy dashboard](#statistics-and-the-energy-dashboard)
- [Blueprints](#blueprints)
- [Example automations](#example-automations)
- [Honest limitations](#honest-limitations)
- [Removal](#removal)
- [Translations](#translations)
- [Development](#development)
- [Continuous integration](#continuous-integration)
- [Repository layout](#repository-layout)
- [Disclaimer](#disclaimer)

## Requirements

- Home Assistant **2026.2.0** or newer.
- An iQua app account (email + password) with at least one device.
- Outbound internet access from Home Assistant to the iQua cloud.

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS**.
2. Open the **⋮** menu (top right) → **Custom repositories**.
3. Add the repository URL `https://github.com/deltasystems-pl/aquahome-ha`
   and choose the type **Integration**.
4. Find **AquaHome (iQua water softener)** in the list and **Download** it.
5. **Restart Home Assistant.**

### Manual

1. Download the repository and copy `custom_components/aquahome/` into your
   Home Assistant `config/custom_components/` directory, so that
   `config/custom_components/aquahome/manifest.json` exists.
2. **Restart Home Assistant.**

The blueprints in `blueprints/automation/aquahome/` ship with the integration
either way, and can also be imported one by one from the badges in
[Blueprints](#blueprints).

## Setup

Everything is configured from the UI; there is nothing to put in
`configuration.yaml`.

1. Go to **Settings → Devices & services → Add integration** and search for
   **AquaHome**.
2. Enter the **email and password** you use in the iQua app — that is the whole
   form.
3. **Verify, if prompted.** Some accounts require email verification; enter the
   confirmation code sent to your inbox.
4. The integration discovers every device on the account and creates their
   entities.

**No API host to choose.** iQua accounts live on one of two hosts — the legacy
`api.myiquaapp.com` or the newer `api.iqua2.com` for migrated accounts — and
the setup flow probes both and keeps whichever one answers. That is deliberate:
an "API type" selector would only be a question users cannot answer, and a
re-authentication silently re-probes, which is what heals an account that gets
migrated later.

**Out of scope:** first-generation EcoWater accounts served by
`apioem.ecowater.com` are a different API and are not supported. If your device
is managed by an older EcoWater app rather than by iQua, this integration will
not find it.

If the stored credentials later stop working (password change, revoked token),
Home Assistant raises a **Re-authenticate** prompt — re-enter the password and
the entry heals in place, keeping all entity history.

> **Credentials.** Your password is exchanged for a 24-hour access token plus a
> long-lived refresh token, and only those tokens are stored in the config
> entry. Downloaded diagnostics are credential-redacted.

## How it works

`iot_class: cloud_polling`, with short on-demand websocket bursts (see
[Live mode](#live-mode)). Independent refresh cadences keep the fast-moving data
fresh without hammering the cloud:

| Data | Cadence |
| --- | --- |
| Device snapshot (salt, usage, regeneration, diagnostics) | every **10 min** |
| Alerts and regeneration history | every **30 min** |
| Device settings (hardness, schedules, capabilities) | every **6 h** |
| Water-usage history import (long-term statistics) | every **12 h** |
| Usage analysis (leak watch, anomalies, forecast) | daily at **07:35** device-local |

A **Refresh data** button asks the device to push fresh state and then polls it,
for when you do not want to wait out the interval. Device capabilities are
re-detected on the settings cadence, so entities for hardware added later (a
leak detector, a shutoff valve) appear on their own.

## Entities

Which entities exist depends on the model and on what its cloud payload
reports; nothing is created for data a device does not have. Feature-gated
entities require the matching hardware, and *disabled by default* entities must
be enabled in the entity registry before they appear.

For scale: the reference device (an AquaHome 20 Smart with no shutoff valve and
no leak detectors) registers 83 entities.

### Sensors

40 sensors, plus 2 per paired leak detector.

**Water and regeneration**

| Entity | Notes |
| --- | --- |
| Salt level | Percentage as the device reports it. |
| Water used today | Resets at the device-local midnight. |
| Treated water available | Softened water left before the next regeneration. |
| Total water | Lifetime outlet counter; restores across restarts. |
| Capacity remaining | Percentage of resin capacity left. |
| Average daily water use | The device's own rolling average. |
| Out of salt estimate | Timestamp, from the device's own day estimate. |
| Regeneration status | Enum: idle, scheduled, regenerating, suspended, disabled, disabled by the shutoff valve, error. |
| Regeneration time remaining | Forced to zero unless a regeneration is actually running. |
| Next regeneration | Timestamp of the next scheduled cycle. |
| Last regeneration | Timestamp, from the regeneration history feed. |
| Days since last recharge | |
| Total recharges | Lifetime count. |
| Average use Saturday … Friday | Seven per-weekday averages the device maintains, each with its own freshness attribute. |

**Salt consumption**

| Entity | Notes |
| --- | --- |
| Total salt used | Lifetime weight. |
| Total hardness removed | Lifetime weight. |
| Salt per regeneration | Device average. |
| Daily salt usage estimate | Chemistry-derived from hardness, efficiency and water use. |
| Salt days remaining estimate | Diagnostic. |
| Salt depletion estimate | Timestamp. Diagnostic, disabled by default. |
| Salt efficiency | Diagnostic; attributes name the source of the figure. |

**Analysis and live data**

| Entity | Notes |
| --- | --- |
| Usage forecast | Expected water use tomorrow, with the reasoning in its attributes. |
| Night flow | Minimum overnight flow, the leak-watch input. Diagnostic. |
| Water flow | Current flow rate. Updates within seconds during a live session; see [Live mode](#live-mode). |
| Live mode | Idle / live / reconnect backoff, with session bookkeeping in its attributes. Diagnostic. |

**Device and diagnostics**

| Entity | Notes |
| --- | --- |
| Latest alert | Most recent alert title, with the full alert in its attributes. |
| Error codes | Diagnostic; empty when the device is healthy. |
| Model, Serial number, Controller firmware, Wi-Fi module firmware | Diagnostic. |
| Hardness setting | Diagnostic. |
| RF signal strength | Diagnostic, disabled by default. |
| Days powered up | Diagnostic. |

**Per leak detector** (feature-gated, one sub-device per detector)

| Entity | Notes |
| --- | --- |
| Temperature | |
| Signal strength | Diagnostic, disabled by default. |

### Binary sensors

17, plus 4 per paired leak detector.

| Entity | Notes |
| --- | --- |
| Online | Connectivity. Diagnostic. |
| Salt level alert, Error code alert, Flow monitor alert, Water usage alert, Resin alert | The device's own alert flags. |
| Connection alert | Diagnostic. |
| Alarm sounding | Feature-gated on an audible alarm. |
| Water-to-drain alert | Feature-gated on a water-to-drain or leak sensor. |
| Regenerating | Running. |
| Regeneration suspended | |
| Vacation mode, Recharge off | The device's own recharge-mode states, as reported by the cloud. |
| Shutoff valve closed | Feature-gated on a water shutoff valve. |
| Leak suspected | From the usage analysis, not from a leak sensor. Attributes carry the tier, rate and evidence. |
| Usage anomaly | Unusually high water use for this household and hour. |
| Vacation detected | Sustained multi-day absence inferred from water use. |
| Leak detected, Low battery, Tampered, Connectivity | Per leak detector; the last two are diagnostic. |

### Buttons

8.

| Entity | Notes |
| --- | --- |
| Regenerate now | Unavailable when the device says it cannot start one. |
| Schedule regeneration | Schedules the next regeneration time. |
| Cancel regeneration | Cancels a started or scheduled cycle. |
| Silence alarm | Feature-gated on an audible alarm. |
| Refresh data | Forces an immediate refresh. Diagnostic. |
| Advance valve | Service tool. Configuration, disabled by default. |
| Reset error code | Service tool. Configuration, disabled by default. |
| Reset shutoff valve error | Feature-gated. Configuration, disabled by default. |

### Switches

7 named switches, plus one per boolean device setting the cloud exposes.

| Entity | Default | Notes |
| --- | --- | --- |
| Live view | Off | Holds a live session open while it is on; turns itself off after 30 minutes. |
| Smart live windows | Off | Configuration. Opt-in: live sessions during learned busy hours. |
| Continuous live flow | Off | Configuration. Advanced: keeps a live session open indefinitely. |
| Vacation deferral | Off | Defers scheduled regenerations while the household is away. |
| Auto vacation deferral | Off | Configuration. Lets the absence detector drive the deferral above. |
| Smart regeneration scheduling | Off | Configuration. Schedules regenerations against the usage forecast. |
| Leak detector scan | — | Configuration, feature-gated. Starts/stops a detector scan. |
| *Boolean device settings* | — | Configuration; created from the device's own settings document. |

### Numbers

2 named numbers, plus one per numeric device setting.

| Entity | Default | Notes |
| --- | --- | --- |
| Live sessions per day | 48 | Configuration. Range 4–200. |
| Live session minimum gap | 120 s | Configuration. Range 60–900 s. |
| *Numeric device settings* | — | Configuration; bounds and precision come from the device. |

### Selects

One per device setting that offers a fixed list of options — water hardness,
regeneration time, salt type, efficiency mode, maximum days between recharges,
flow-monitor alert thresholds, and so on. They are built from the settings
document the cloud returns, so their names are the server's own localized
labels and a setting hidden by another setting's value stays visible but
unavailable rather than disappearing.

The account/display preferences among them (volume, weight and hardness units,
date and time format, time zone) are created **disabled by default**: this
integration's sensors bind fixed units, so changing those settings only affects
the phone app.

### Valve

| Entity | Notes |
| --- | --- |
| Water shutoff valve | Feature-gated. Open/close, with the in-flight state shown optimistically for a few seconds. |

### Event

| Entity | Notes |
| --- | --- |
| Alert | Fires on each new device alert with a normalized `event_type` (low salt, excessive water use, shutoff valve opened, connection online/offline, other) and the raw alert in its attributes. |

## Live mode

Polling every 10 minutes is fine for salt and capacity, and useless for
watching a tap. Live mode opens a short websocket session to the iQua cloud
during which the device reports per gallon, so the water counters and the
**Water flow** sensor move within seconds. Live data upgrades the same entities
the poll feeds — it never creates a second set.

**Five ways a session starts**

1. **Live view switch** — turn it on and a session is held open, renewing
   itself window after window, until you turn it off or the 30-minute cap
   expires.
2. **The live dashboard blueprint** — the same switch, driven from an
   "app is open" indicator, so live data runs exactly while you are looking at
   a dashboard. See [Blueprints](#blueprints).
3. **Smart live windows** (opt-in, off by default) — one session at the start
   of the next hour the usage analysis expects to be busy. Never between 01:00
   and 07:00, and suspended for the rest of the day after three windows in a
   row saw no water move.
4. **Event bursts** — a session when a regeneration starts, and one when the
   usage-anomaly binary sensor turns on, so the event is captured at gallon
   resolution instead of in a 10-minute average.
5. **Poll-detected active use** — when today's counter jumps by at least 2
   gallons between two polls, somebody is using water now; a session follows,
   at most one every 30 minutes. These are deliberately allowed at night: an
   unexplained night session is exactly the evidence a leak investigation
   wants.

The **Continuous live flow** switch is the deliberate exception: it holds a
session open indefinitely, reconnecting each time the device's fast-reporting
window ends. It is an advanced setting, off by default, and it means talking to
the vendor's cloud all day long.

**One shared budget.** Every path above draws on the same allowance: by default
**48 sessions per device-local day** with a **120-second minimum gap** between
them. Renewals inside a session already held open do not count as new sessions.
Both numbers are adjustable per device with the **Live sessions per day**
(4–200) and **Live session minimum gap** (60–900 s) entities. When the budget
is spent, or the device is offline, or the cloud is throttling, triggers are
simply declined and polling carries on.

**Failure is quiet.** If the websocket cannot be established, the integration
backs off (a minute, then longer, capped at 30 minutes) and keeps polling —
entities keep updating at the normal cadence, nothing goes unavailable. Only
after five consecutive failures *while the device itself is online* does a
repair issue appear, and it clears itself the moment a session succeeds again.
The **Live mode** diagnostic sensor shows `idle`, `live` or `backoff` at any
time, with the session counters and the last error in its attributes.

**Honestly:** live streaming leans entirely on the vendor's cloud, which
publishes its own rate limits and can change them without notice. The defaults
above are deliberately conservative — comfortably inside the observed limits
even with every trigger enabled — and they are exposed as entities so you can
lower them further, not only raise them.

## Analytics and automation

The integration analyses the imported water-usage history once a day, just
after the overnight window closes. **The analysis itself never touches the
device**: it only publishes sensors and binary sensors.

**Always on, read-only**

- **Leak watch** — minimum overnight flow, classified into information,
  warning and urgent tiers, requiring consecutive nights before it calls a
  leak. Publishes *Leak suspected* and *Night flow*.
- **Usage anomaly** — robust statistics over the learned hour-of-week profile,
  so "unusual" means unusual *for this household at this hour*.
- **Vacation detection** — sustained multi-day low usage with no meaningful
  draws. Publishes *Vacation detected*.
- **Usage forecast** — expected use for the coming days, from the device's own
  weekday averages where they are fresh and from learned statistics otherwise.

Two of these can raise a repair issue: a sustained urgent-tier leak, and a leak
detected while the household appears to be away. Both are notifications, not
actions.

**Opt in before anything moves**

Every automation that affects the device is a switch that ships **off**:

| Switch | What it does when on |
| --- | --- |
| **Vacation deferral** | Cancels scheduled regenerations while the household is away. A regeneration is let through after 21 deferred days to protect the resin bed, and a catch-up cycle is scheduled on return if capacity is low. At most three cancels per day. |
| **Auto vacation deferral** | Lets the absence detector turn the deferral above on and off by itself. A deferral you set by hand is never auto-released. |
| **Smart regeneration scheduling** | Schedules a regeneration when the remaining treated water falls below tomorrow's forecast plus a 50% reserve. |

Two further suggestions arrive as *fixable* repair issues you confirm or
ignore: "an absence looks likely — defer regenerations?" and "your regeneration
time overlaps the hours this household actually uses water — move it?". Neither
writes anything to the device until you press the button.

**Vacation mode here means deferral.** The `aquahome.set_vacation_mode` action
and the *Vacation deferral* switch suppress scheduled regenerations from Home
Assistant's side. They do **not** toggle the vacation tile in the iQua app —
that command's payload is undocumented and unverified, so the integration does
not send it. The *Vacation mode* binary sensor still reports the device's own
state, whatever set it.

## Actions

Four actions, each targeted at an entity (so a multi-device account needs no
device picker, and per-entity permissions apply unchanged). All four are
registered at startup, so automations referencing them keep validating even
while the integration is reloading.

| Action | Target | Fields | Returns |
| --- | --- | --- | --- |
| `aquahome.analyze_usage` | any AquaHome analytics sensor | `refresh` (bool, default false) — recompute from the latest statistics first | The full usage analysis: nightly leak verdicts, daily assessments, anomaly and absence state, tomorrow's forecast, the learned activity grid |
| `aquahome.get_usage_forecast` | any AquaHome analytics sensor | `days` (1–7, default 1) | Expected daily water use for the coming days |
| `aquahome.set_vacation_mode` | the AquaHome *Vacation deferral* switch | `vacation` (bool, required) | — |
| `aquahome.schedule_regeneration` | any AquaHome regeneration button | `mode`: `schedule` (default), `now`, `cancel` | — |

The two read-only actions return response data and change nothing, which makes
them safe to call from a template sensor or a script loop.

```yaml
actions:
  - action: aquahome.get_usage_forecast
    target:
      entity_id: sensor.aquahome_usage_forecast
    data:
      days: 3
    response_variable: forecast
```

## Statistics and the Energy dashboard

Home Assistant's long-term statistics normally start the day an entity does.
The iQua cloud already holds the counter history, so the integration imports it
as an **external statistic** named `aquahome:<device>_water`, reaching as far
back as the cloud still retains readings — hour by hour where hourly readings
survive, day by day beyond that. Runs are idempotent and repeat every 12 hours,
picking up readings the device uploaded late without rewriting settled history.

That gives you two water sources:

- **`sensor.<device>_total_water`** — the live lifetime counter, growing from
  the moment you installed the integration.
- **`aquahome:<device>_water`** — the imported history, including everything
  from before the installation.

Both are selectable in **Settings → Dashboards → Energy → Water consumption**.
**Pick exactly one.** Adding both counts every litre twice — choose the
imported statistic if you want the full history, the sensor if you would rather
keep the Energy dashboard on a live entity.

## Blueprints

Five ready-made automation blueprints ship with the integration. They build on
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
| [![Import the AquaHome live dashboard view blueprint.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fdeltasystems-pl%2Faquahome-ha%2Fmain%2Fblueprints%2Fautomation%2Faquahome%2Flive_dashboard.yaml) **Live dashboard view** | Turns the Live view switch on while the companion app (or any indicator entity you pick) shows Home Assistant open, and off again when it closes — live water data exactly while you are looking at it. |

The interactive buttons need the Home Assistant Companion app. With any other
notification service the messages still arrive — the buttons are simply
ignored, and each blueprint documents what happens then.

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

## Honest limitations

- **Cloud polling every 10 minutes, and that interval is fixed.** There is no
  polling-interval option on purpose: the iQua cloud rate-limits aggressively
  and users of other tools have had accounts blocked for polling it hard. Live
  mode exists precisely so that "I want to see it now" does not turn into "poll
  faster forever".
- **The cloud's curated summary can lag its own raw counters.** Where the two
  disagree the integration prefers the raw counters, but a value that only
  exists in the curated block (some estimates and status strings) can trail the
  device by a poll or two.
- **Leak detection has a floor of roughly 1 gal/h (~91 L/day).** The meter
  reports only when water actually moves, in whole gallons — a drip too slow to
  turn the meter within an hour is invisible to any analysis built on it. The
  same property makes the detection that *does* happen trustworthy: a
  continuous leak above the floor forces a reading every hour, all night.
- **The Water flow sensor publishes on change, not continuously.** It is
  accurate during a live session and goes to zero when flow stops; between
  sessions it holds its last value and can be stale. Treat it as "what the
  flow was the last time the device said anything", not as a live gauge, unless
  a session is running.
- **Vacation mode is deferral only.** It suppresses scheduled regenerations
  from Home Assistant's side and does not touch the vacation tile in the iQua
  app. See [Analytics and automation](#analytics-and-automation).
- **Shutoff valve and leak detectors are implemented but untested on real
  hardware.** The reference device has neither, so the valve entity, the leak
  detector sub-devices, and their related controls are built from the documented
  payloads and verified only against fixtures. They should work; nobody has
  proven it. **If you own that hardware, please
  [open an issue](https://github.com/deltasystems-pl/aquahome-ha/issues) with
  what you see** — good or bad.
- **Weight sensors register their display unit once.** Salt and hardness
  weights are read in pounds and offered in kilograms when the account is set
  to metric, but Home Assistant applies a suggested unit only when the entity
  is first created. Switching your iQua account's units later will not move an
  already-registered sensor; change the unit on the entity itself instead.
- **This uses an unofficial API.** It is not affiliated with, endorsed by, or
  supported by EcoWater Systems or iQua. The vendor can change or withdraw the
  cloud API at any time and the integration will break when they do. Nothing
  here is a substitute for a plumber, a hardware leak sensor, or an actual
  shutoff valve.

## Removal

Delete the integration entry under **Settings → Devices & services → AquaHome**.
Removing the entry also deletes the imported `aquahome:*` water-usage statistics
series and every repair issue the integration created — nothing is left behind.
If you installed through HACS, remove the repository from HACS afterwards (or
delete `custom_components/aquahome/` for a manual install) and restart Home
Assistant. Your iQua account itself is untouched: the integration only ever held
an API session, which expires on its own.

## Translations

English and Polish (`pl`) ship with the integration; Home Assistant picks the
right one from your profile language. Device *settings* — the select, number
and boolean setting entities — are named by the cloud itself and follow your
Home Assistant language wherever the vendor provides a translation.

Translation contributions are welcome: copy
`custom_components/aquahome/translations/en.json` to your language code and open
a pull request.

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
`aioresponses`; the fixtures are real, credential-redacted API payloads, and the
live websocket paths run against a local fake iQua server.

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

A separate weekly run
([`.github/workflows/nightly.yml`](.github/workflows/nightly.yml)) repeats the
lint and test suite against the current Home Assistant stable, beta and dev
channels, so an upstream breaking change surfaces here before it surfaces in
someone's installation.

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
