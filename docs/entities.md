# AquaHome entities — the full reference

This page explains every entity the AquaHome integration can create: what it
tells you, where the number comes from, how often it changes, and the handful of
places where the behaviour is surprising. It is written for people who use Home
Assistant, not for people who write it — if a term is unfamiliar, it is in
[Words this page uses](#words-this-page-uses). The entities are grouped the same
way the [bundled dashboard](../dashboards/aquahome-dashboard.yaml) groups them:
by what they are *for*, not by which Home Assistant platform they happen to use.

**Not every device gets every entity.** Three things decide what you end up
with: what hardware your softener reports (a shutoff valve, an audible alarm,
paired leak detectors), which blocks of data the iQua cloud actually sends for
your unit, and which entities ship switched off and wait for you to enable them.
Nothing here is created for data your device does not have. For scale: the
reference softener — an AquaHome 20 Smart with no shutoff valve and no leak
detectors — ends up with 83 entities: 39 sensors, 15 device settings, 14 binary
sensors, 6 buttons, 6 switches, 2 numbers and 1 event.

For installation, setup, the feature overview and the known limitations, see the
[README](../README.md).

## Contents

- [Words this page uses](#words-this-page-uses)
- [How often things update](#how-often-things-update)
- [When entities go unavailable](#when-entities-go-unavailable)
- [Water](#water)
- [Analysis](#analysis)
- [Live mode](#live-mode)
- [Regeneration](#regeneration)
- [Salt](#salt)
- [Automation](#automation)
- [Alerts](#alerts)
- [Device](#device)
- [Shutoff valve](#shutoff-valve)
- [Leak detectors](#leak-detectors)
- [Device settings](#device-settings)
- [Events on the event bus](#events-on-the-event-bus)

## Words this page uses

**Entity ID.** Every entity has an ID like `sensor.softener_salt_level`. The
first part says what kind of entity it is, the middle is your device's name, and
the end is the entity's own name, all in lower-case-with-underscores. The tables
below write that middle part as `<device>` — substitute your own. **If your Home
Assistant was in another language when you set the integration up, your IDs will
read differently** — the IDs are minted once, from the name in the language in
use at that moment, and they do not change afterwards when you switch languages.
Look the real ones up under **Settings → Devices & services → Entities**.

**Diagnostic.** A label Home Assistant uses to file an entity in the *Diagnostic*
section of the device page, out of the way of the things you look at daily. It
works exactly like any other entity; it is just filed separately. This page marks
those *(diagnostic)*.

**Configuration.** The same idea for entities that change a setting rather than
report a measurement — they land in the *Configuration* section. Marked
*(config)*.

**Disabled by default / off by default.** The entity exists but is switched off
in the registry, so it has no state and does not appear on dashboards. Enable it
with **Settings → Devices & services → AquaHome → your device → the entity →
the gear icon → Enabled**, then wait a moment for it to start reporting. These
are marked *(off by default)*, and each one says why it ships off.

**Needs the hardware.** Some entities only exist if your softener reports the
matching hardware — a shutoff valve, an audible alarm, a paired leak detector.
Marked *(needs hardware)*. If a piece of hardware is added later, its entities
appear on their own within about twenty minutes; nothing needs restarting.

**Where the value comes from.** Four different places, and it matters because
they refresh at different speeds:

- *device reading* — a value that comes straight from the softener's own report
  or its record in the cloud, rather than from the cloud's summary. The freshest
  source, and the one live mode can stream.
- *cloud summary* — the tidied-up summary block the iQua cloud builds for its
  own app. Convenient, but it is recomputed on the cloud's schedule and can trail
  the raw readings by a poll or two.
- *worked out here* — the integration calculated it from other values. Nothing
  is asked of the cloud.
- *settings document* — your device's configuration, as the cloud stores it.
- *alerts and history* — the cloud's alert list and regeneration log.

**Live session.** Normally the integration asks the cloud for news every ten
minutes. A *live session* is a short direct connection to the cloud during which
your softener reports every gallon as it happens, so the water counters and the
flow reading move within seconds instead of at the next poll. Sessions are
deliberately short and rationed — see [Live mode](#live-mode).

**What the on/off sensors say.** Home Assistant words a yes/no sensor according
to what kind of thing it is, so several entities here do not read "on" and "off".
The alert flags, *Usage anomaly*, *Regeneration suspended* and *Shutoff valve
closed* read **Problem / OK**; *Leak suspected*, *Water-to-drain alert* and a
detector's *Leak detected* read **Wet / Dry**; *Online* and a detector's
*Connectivity* read **Connected / Disconnected**; *Regenerating* reads
**Running / Not running**; *Alarm sounding*
reads **Detected / Clear**; a detector's *Low battery* reads **Low / Normal** and
its *Tampered* reads **Tampering detected / Clear**. *Vacation mode*, *Recharge
off* and *Vacation detected* read plain **On / Off**.

**Units.** The softener measures in US units, so that is what the integration
reads and stores. Water volumes and flow rates are shown in litres on a metric
Home Assistant and in gallons on a US-customary one, automatically. Weights are
the exception: Home Assistant does not convert them by itself, so the integration
offers kilograms when your iQua account is set to metric — but only when the
entity is first created (see [Salt](#salt)). Any entity's displayed unit can be
changed by hand in its entity settings.

## How often things update

| What | How often |
| --- | --- |
| Device readings (water, salt, regeneration, diagnostics) | every 10 minutes |
| Alerts and regeneration history | every 30 minutes |
| Device settings | every 6 hours, and immediately after you change one |
| Water-usage history import | every 12 hours |
| The daily usage analysis | once a day at 07:35, your device's local time |
| Live mode status and switches | the moment something happens |

Two things jump the queue. The **Refresh data** button asks the softener to push
fresh state to the cloud and then re-reads it about fifteen seconds later. And
when a new alert appears or a regeneration starts or stops, the alert and history
feed is refreshed straight away instead of waiting out its half hour.

## When entities go unavailable

*Unavailable* means "I have nothing trustworthy to show", and this integration
tries hard to say it only when it is true.

- **Most device entities** need two things: the last poll of the cloud worked,
  and the softener reports itself as online. If either fails, they go
  unavailable rather than freeze on an old value.
- **The alert flags and the mode binary sensors deliberately do not** require
  the softener to be online. If your softener drops off the network, "Connection
  alert" and "Online" are exactly what you want to still be readable, so they
  stay available.
- **Alert, Latest alert and Last regeneration** follow the cloud's history feed
  only. That history stays valid while the softener is offline, so they do too.
- **The analysis entities** follow the analysis, which is computed from imported
  history. They keep working while the softener is offline — a leak verdict
  matters most then.
- **Device settings entities** need the settings document to have loaded, and
  need that particular setting to be currently visible on your device.
- **The live-mode switches and numbers, the automation switches, and the Live
  mode sensor are always available.** They hold *your* preferences, not the
  device's state, so a cloud outage can never leave you unable to switch an
  automation off.

Short cloud hiccups do not cause flapping: the integration keeps serving the last
good reading for up to 30 minutes for device readings, 3 hours for alerts and
history, and 24 hours for settings, and only then admits it is unavailable.

## Water

How much water you are using, and how much softened water is left.

| Entity | Entity ID | What it tells you | Where it comes from | Updates |
| --- | --- | --- | --- | --- |
| Water used today | `sensor.<device>_water_used_today` | Water used since the softener's own day started. | device reading, cloud summary as backup | every 10 min; live during a session |
| Total water | `sensor.<device>_total_water` | The lifetime counter: every gallon the softener has ever treated. | device reading, cloud summary as backup | every 10 min; live during a session |
| Water flow | `sensor.<device>_water_flow` | How fast water is flowing through the softener right now. | device reading, in tenths of a gallon per minute — divided by 10 here, and shown in litres per minute | live during a session; otherwise whatever the device last said |
| Treated water available | `sensor.<device>_treated_water_available` | How much softened water is left before the softener needs to regenerate. | cloud summary, read from its fixed-gallons figure rather than the one that follows your iQua account's units | every 10 min |
| Average daily water use | `sensor.<device>_average_daily_water_use` | The softener's own long-run daily average, in gallons. | device reading | every 10 min |
| Capacity remaining | `sensor.<device>_capacity_remaining` | Percentage of the resin's softening capacity left. | device reading, in tenths of a percent — divided by 10 here | every 10 min |
| Hardness setting *(diagnostic)* | `sensor.<device>_hardness_setting` | The water hardness the softener is configured for, in grains per gallon. | device reading | every 10 min |
| Average use Saturday … Friday | `sensor.<device>_average_use_saturday` … `sensor.<device>_average_use_friday` | Seven separate averages in gallons, one per weekday, as the softener keeps them. | device reading | every 10 min |

### Why does Water flow show 0 while I am running a tap?

Because the softener does not report flow continuously. It sends one message when
water starts moving and another when it stops, and nothing in between. During a
[live session](#live-mode) you receive those messages within seconds, so the
reading is genuinely live. Between sessions a ten-minute poll almost never lands
inside a draw, so what you see is the last thing the device said — usually the
closing zero, occasionally a stale non-zero rate from a draw that has not
reported its end yet. Treat this sensor as a live instrument, and trust the
volume counters for how much water actually flowed. That is a property of the
hardware, not a fault in the integration.

### Total water can sit still and then jump

The lifetime counter comes from the softener, and the softener sometimes uploads
late: the number stops for a while and then catches up in one step. On a graph
that step looks like a huge burst in a single hour. This is why, if you are
adding water to the **Energy dashboard**, the imported history series
(`aquahome:<device>_water`) is the better source: it is rebuilt from the cloud's
own hourly meter records rather than sampled from a counter that can lag. Pick
one source, not both — they count the same water. The imported series is not an
entity, so you will not find it in the entity list; it appears in the Energy
dashboard's water-source picker under a name ending in *water usage history*.

The Total water sensor also protects itself from one specific cloud glitch: when
the counter dips slightly, the sensor holds its previous value instead of
reporting the dip, because Home Assistant's long-term statistics would otherwise
read the next rise as a brand-new meter and record a giant phantom consumption. A
large drop is accepted as a genuine reset. The last value is remembered across
restarts.

### The seven weekday averages are the softener's, and they can be old

Each of the seven *Average use …* sensors is a slot the softener maintains and
only refreshes on that weekday. A slot can therefore be a week old, and slots
have been observed weeks out of date. Each sensor carries a `reported` attribute
with the timestamp of the last change the softener recorded for that slot, so you
can see how current the number really is. The weekday labels are the
integration's reading of which slot is which day; if that mapping ever gets
corrected the labels change, but the entity IDs and their history do not.

### Water used today resets on the softener's clock

The softener keeps this counter and rolls it over on its own local day boundary,
in the time zone the device is configured for — not Home Assistant's. If the two
differ, expect the reset at what looks like an odd hour.

## Analysis

Once a day, just after the quiet overnight hours end, the integration analyses the
imported water-usage history and publishes what it found. **The analysis never
touches your softener** — it only reads history and publishes these entities.

| Entity | Entity ID | What it tells you | Where it comes from | Updates |
| --- | --- | --- | --- | --- |
| Leak suspected | `binary_sensor.<device>_leak_suspected` | On when overnight water flow says water is running continuously. | worked out here | once a day at 07:35 |
| Usage anomaly | `binary_sensor.<device>_usage_anomaly` | On when today's use is unusual *for your household at this hour*. | worked out here | once a day at 07:35 |
| Vacation detected | `binary_sensor.<device>_vacation_detected` | On after several days with almost no water use. | worked out here | once a day at 07:35 |
| Usage forecast | `sensor.<device>_usage_forecast` | How much water tomorrow is expected to need, in gallons. | worked out here | once a day at 07:35 |
| Night flow *(diagnostic)* | `sensor.<device>_night_flow` | The quietest hour of the most recent night that could be judged, in litres per hour. | worked out here | once a day at 07:35 |

All five are good automation triggers, and all five are read-only: none of them
can start, stop or change anything on the softener. Two situations also raise a
Home Assistant repair notification — a sustained severe leak, and a leak while
the household appears to be away — and those are notifications too, not actions.

### The analysis counts days from noon to noon

Households use water across midnight — the evening and the night that follows it
belong together — so the analysis cuts its days at midday instead. A day labelled
with a date covers **noon the day before to noon on that date**, in your device's
local time. This is deliberate, and it is why the analysis's daily totals do not
match the Energy dashboard's midnight-to-midnight bars.

### Why do these show *unknown* instead of *off*?

Because "I have not found a leak" and "I cannot tell" are different answers, and
the integration refuses to say the first when it means the second. Each of the
three detectors reports *unknown* while it has nothing it can honestly assess:
before the first analysis has run at all, when there is not enough imported
history yet, or when a night cannot be judged. `Leak suspected` in particular
never declares a leak without regeneration history covering the nights it looked
at — a regeneration draws water at 2 a.m. and would look exactly like a leak. Its
`masking_coverage` attribute tells you whether that history was available, so a
permanently-off leak sensor can be explained rather than guessed at.

Each detector carries its evidence in its attributes: for `Leak suspected` the
tier, the flow rate, how many consecutive nights and which night; for
`Usage anomaly` the reasons, the day, actual versus expected litres; for
`Vacation detected` how many quiet days and since when.

### Night flow only reports nights it could judge

If the most recent night was clean, this reads 0 — the analysis only reaches that
verdict on evidence of a genuinely dry hour. If it found a leak, it reports the
smallest certain hour of that night. If the night could not be judged at all
(masked by a regeneration, or not bounded by meter readings), the sensor stays
*unknown* rather than reporting a comfortable zero. Its attributes name the night
and the verdict.

There is a floor to what any of this can see: the meter counts in whole gallons
and only reports when water actually moves, so a drip too slow to turn the meter
within an hour is invisible. Roughly one gallon an hour is the practical
threshold.

## Live mode

Live mode opens a short direct connection to the iQua cloud, during which the
softener reports every gallon as it happens. It is off unless you ask for it, and
it is rationed, because the cloud allows only a small number of these sessions.

| Entity | Entity ID | What it does | Updates |
| --- | --- | --- | --- |
| Live view | `switch.<device>_live_view` | Turn it on to watch water use as it happens. Holds a session open until you turn it off, or 30 minutes pass. | immediately |
| Smart live windows *(config)* | `switch.<device>_smart_live_windows` | Off by default. Lets the integration open a session by itself during the busy hours it has learned for each weekday. | immediately |
| Continuous live flow *(config)* | `switch.<device>_continuous_live_flow` | Off by default. Advanced: keeps a session open all day long. | immediately |
| Live sessions per day *(config)* | `number.<device>_live_sessions_per_day` | How many sessions a day this device may open. Default 48, range 4–200. | immediately |
| Live session minimum gap *(config)* | `number.<device>_live_session_minimum_gap` | The shortest gap between sessions, in seconds. Default 120, range 60–900. | immediately |
| Live mode *(diagnostic)* | `sensor.<device>_live_mode` | Whether a session is running: *Idle*, *Live*, or *Reconnect backoff*. | when a session starts, ends or fails |

All six are always available: they describe what Home Assistant is doing, not
what the softener reports, so they keep working when the cloud does not.

Sessions do not only start when you ask. Besides the three switches, the
integration opens one when a regeneration starts, when the usage-anomaly sensor
turns on, and when the today counter jumps by at least 2 gallons between two
polls (at most one of those every 30 minutes) — so seeing **Live mode** read
*Live* when you did not touch anything is normal, and its `source` attribute says
which of those it was.

### Live mode does not add sensors — it speeds the ones you have up

There is no separate set of "live" readings. While a session runs, the streamed
values are merged into the ordinary device data, so **Total water**, **Water used
today**, **Water flow**, **Regeneration time remaining** and **RF signal
strength** simply start moving within seconds. When the session ends they go back
to updating every ten minutes. A device that never streams behaves exactly as it
did before.

### Why does Live view turn itself off?

Three reasons, all deliberate:

- **The 30-minute cap.** A hold you forgot about would keep talking to the
  vendor's cloud all day, so it switches itself off after half an hour.
- **The session ended.** Any clean end of a session — the hold was released, the
  device stopped reporting — switches the manual hold off with it. It is meant to
  be a "show me now" button, not a mode you leave on.
- **It was never granted.** Turning the switch on is a *request*. The integration
  refuses it if the softener is offline, if the day's session budget is spent, if
  another session is already streaming, or if the minimum gap has not elapsed. In
  that case the switch stays on and the request is retried as soon as the way is
  clear. So the switch shows what you asked for; the **Live mode** sensor shows
  what is actually running.

If the connection fails, the integration backs off (a minute, then longer, up to
30 minutes) and keeps polling normally — nothing goes unavailable. A repair
notification appears only after five consecutive failures while the softener
itself is online, and clears itself as soon as a session succeeds.

### What each of the three live switches actually does

- **Live view** is a manual hold: one session, renewed as needed, capped at 30
  minutes.
- **Smart live windows** opens a session at the start of a busy hour the analysis
  has learned for that weekday and holds it through the whole hour, so a real
  household draw is recorded gallon by gallon. It never opens between 01:00 and
  07:00, and if three reporting windows in a row see no water move, it gives up
  for the rest of that day. Turning the switch off also ends a peak-hour session
  in progress.
- **Continuous live flow** is the exception to all rationing: it holds a session
  open indefinitely, reconnecting each time the device's reporting window closes.
  It is advanced, off by default, and it means talking to the vendor's cloud all
  day.

Every path shares one budget: at most **Live sessions per day** grants per device
day, never closer together than **Live session minimum gap**. Reconnects inside a
session already held open do not count as new sessions. The **Live mode** sensor's
attributes show the trigger that opened the current session, sessions used today,
reconnects in this session, when the last one ended, the consecutive failure
count and the last error.

## Regeneration

Regeneration (also called a recharge) is the cycle in which the softener rinses
its resin with brine. These entities show what it is doing and let you ask for
one.

| Entity | Entity ID | What it tells you or does | Where it comes from | Updates |
| --- | --- | --- | --- | --- |
| Regeneration status | `sensor.<device>_regeneration_status` | Idle, Scheduled, Regenerating, Suspended, Disabled, Disabled (shutoff valve), or Error. | cloud summary | every 10 min |
| Regenerating | `binary_sensor.<device>_regenerating` | On while a cycle is actually running. | cloud summary | every 10 min |
| Regeneration suspended | `binary_sensor.<device>_regeneration_suspended` | On when the softener has suspended regeneration. | cloud summary | every 10 min |
| Vacation mode | `binary_sensor.<device>_vacation_mode` | On when the *softener* is in its own vacation mode. | cloud summary | every 10 min |
| Recharge off | `binary_sensor.<device>_recharge_off` | On when recharging has been turned off on the softener. | cloud summary | every 10 min |
| Regeneration time remaining | `sensor.<device>_regeneration_time_remaining` | Seconds left in the running cycle. | device reading, cloud summary as backup | every 10 min; live during a session |
| Next regeneration | `sensor.<device>_next_regeneration` | When the next scheduled cycle will start. | worked out here from the scheduled time and the device's time zone | every 10 min |
| Last regeneration | `sensor.<device>_last_regeneration` | When the most recent cycle started. | alerts and history | every 30 min |
| Days since last recharge | `sensor.<device>_days_since_last_recharge` | Whole days since the last cycle. | cloud summary | every 10 min |
| Total recharges | `sensor.<device>_total_recharges` | Lifetime count of cycles. | cloud summary | every 10 min |
| Regenerate now | `button.<device>_regenerate_now` | Starts a regeneration immediately. | — | — |
| Schedule regeneration | `button.<device>_schedule_regeneration` | Schedules one for the next regeneration time. | — | — |
| Cancel regeneration | `button.<device>_cancel_regeneration` | Cancels a started or scheduled cycle. | — | — |

The three buttons exist when the softener advertises regeneration (essentially
every softener). **Regenerate now** and **Schedule regeneration** go unavailable
when the cloud explicitly says the softener cannot do that right now; if the
cloud says nothing either way, the button stays pressable and the cloud has the
final word.

### Regeneration time remaining reads 0 unless a cycle is running

The cloud leaves its countdown sitting at the last value after a cycle ends, so
the integration forces this sensor to 0 whenever no regeneration is actually
running. Without that you would see a stale "42 minutes remaining" hours after
the cycle finished.

### Next regeneration is empty unless one is scheduled

This is only meaningful when the softener says a regeneration is scheduled. Any
other state — idle, running, disabled — leaves it *unknown* rather than guessing
a time. When it is scheduled, the timestamp is the softener's configured
regeneration time on the next applicable day, in the device's own time zone.

### Vacation mode here is the softener's, not Home Assistant's

The **Vacation mode** binary sensor reports the softener's own vacation state,
whatever set it. It is *not* the same thing as the **Vacation deferral** switch
under [Automation](#automation), which suppresses regenerations from Home
Assistant's side and does not touch the softener's vacation tile at all.

## Salt

| Entity | Entity ID | What it tells you | Where it comes from | Updates |
| --- | --- | --- | --- | --- |
| Salt level | `sensor.<device>_salt_level` | How full the salt tank is, in percent. Only exists if salt monitoring is switched on for your unit. | cloud summary | every 10 min |
| Salt level alert | `binary_sensor.<device>_salt_level_alert` | On when the softener itself says salt is low. | cloud summary | every 10 min |
| Out of salt estimate | `sensor.<device>_out_of_salt_estimate` | The day the softener expects to run out. | worked out here from the softener's day countdown and its time zone | every 10 min |
| Total salt used | `sensor.<device>_total_salt_used` | Lifetime salt consumption, in pounds. | device reading, in tenths of a pound — divided by 10 here | every 10 min |
| Total hardness removed | `sensor.<device>_total_hardness_removed` | Lifetime hardness ("rock") removed, in pounds. | device reading, in tenths of a pound — divided by 10 here | every 10 min |
| Salt per regeneration | `sensor.<device>_salt_per_regeneration` | Average salt used per cycle, in pounds. | device reading, sent as ten-thousandths of a pound — divided by 10,000 here | every 10 min |
| Daily salt usage estimate | `sensor.<device>_daily_salt_usage_estimate` | Estimated grams of salt used per day. | worked out here from water use, hardness and efficiency | every 10 min, and when a setting changes |
| Salt days remaining estimate *(diagnostic)* | `sensor.<device>_salt_days_remaining_estimate` | A second opinion, in days, on how much salt is left. | worked out here | every 10 min, and when a setting changes |
| Salt depletion estimate *(diagnostic, off by default)* | `sensor.<device>_salt_depletion_estimate` | The same second opinion expressed as a date. Off by default because *Out of salt estimate* already answers this from the softener's own figure. | worked out here | every 10 min, and when a setting changes |
| Salt efficiency *(diagnostic)* | `sensor.<device>_salt_efficiency` | How much hardness the softener removes per kilogram of salt, in mol/kg. | worked out here from the device's own counters | every 10 min |

The three estimate sensors also read your device's **inlet hardness** setting when
it is available, which is more precise than the whole-number hardness reading;
they update the moment that setting changes rather than waiting for the next poll.
If the settings document has not loaded, they fall back to the device reading and
carry on.

Not every softener gets all of these. **Salt level** needs salt monitoring
switched on; **Out of salt estimate** needs the softener to report its own
days-remaining countdown; the estimates need enough of the underlying counters
(daily water use, hardness, and either the softener's efficiency figure or its
lifetime salt and hardness totals) to do the arithmetic. **Salt efficiency**
prefers the softener's own efficiency figure and falls back to those lifetime
totals; its attributes say which of the two it used, and repeat the figure in the
grains-per-pound form softener documentation usually quotes.

### Why are there two "days of salt left" figures?

**Out of salt estimate** is the softener's own countdown, turned into a date. That
is the primary signal, and it is what the integration's low-salt notifications use
(a warning at 14 days, a stronger one at 7).

**Salt days remaining estimate** is an independent cross-check: the integration
works out how much salt the softener's countdown implies is left, then re-times
that amount using your *current* water use and hardness. Because it uses current
figures rather than long-run averages, it reacts faster — if the household's water
use has just changed, this number moves first and the softener's own countdown
catches up later. It is meant to be read alongside the device figure, not instead
of it. Its attributes show both daily rates and the percentage they differ by.

### Out of salt estimate is a date, not a time of day

The softener reports a number of days, and the integration turns that into
midnight at the start of that day *in the device's time zone*. It is a date
dressed as a timestamp; do not read anything into the time part.

### Total hardness removed can go down

Despite the name, this is not a counter that only rises. The softener derives it
from lifetime salt use multiplied by its efficiency figure, and that efficiency
figure moves in both directions, so the value dips when recent efficiency drops.
The integration declares it accordingly, so a dip is recorded as a dip rather than
as a broken meter.

### Salt weights: pounds or kilograms, decided once

The three weight sensors are read in pounds and offered in kilograms when your
iQua account is set to metric. Home Assistant applies that suggestion only when
the entity is first created, so changing your iQua account's units later will not
move an already-registered sensor. Change the unit on the entity itself instead
(entity settings → unit of measurement).

## Automation

Three opt-in switches. **All three ship off**, and nothing they control happens
until you turn one on.

| Entity | Entity ID | What it does when on | Updates |
| --- | --- | --- | --- |
| Vacation deferral | `switch.<device>_vacation_deferral` | Cancels scheduled regenerations while the household is away. | immediately |
| Auto vacation deferral *(config)* | `switch.<device>_auto_vacation_deferral` | Lets the absence detector turn the switch above on and off by itself. | immediately |
| Smart regeneration scheduling *(config)* | `switch.<device>_smart_regeneration_scheduling` | Schedules a regeneration when the softened water left drops below tomorrow's forecast plus a 50% reserve. | immediately |

All three are **always available**, even when the cloud is unreachable or the
softener is offline: they hold your preference, stored in Home Assistant, not the
device's state. An outage must never leave you unable to switch an automation off.

Deferral is not unlimited. After 21 deferred days a regeneration is let through
anyway to protect the resin, and at most three cancellations happen per day so a
disagreement with the softener's own scheduler can never turn into a fight. The
**Vacation deferral** switch's attributes show who started the deferral (you, or
the detector), when, and how many days it has run — which is how close it is to
that 21-day limit. The **Smart regeneration scheduling** switch's attributes
record the last decision it took and when, so a night that passed without a
regeneration explains itself.

A deferral *you* started — by the switch, by the `aquahome.set_vacation_mode`
action, or from a blueprint — is never released automatically by the absence
detector. Only a deferral the detector started is released by it.

Again: this is deferral from Home Assistant's side. It does not press the vacation
button in the iQua app.

## Alerts

| Entity | Entity ID | What it tells you | Where it comes from | Updates |
| --- | --- | --- | --- | --- |
| Alert | `event.<device>_alert` | Fires once for every new alert the cloud raises, with the alert's text in its attributes. | alerts and history | every 30 min, sooner when a new alert appears |
| Latest alert | `sensor.<device>_latest_alert` | The text of the most recent alert. | alerts and history | every 30 min |
| Error code alert | `binary_sensor.<device>_error_code_alert` | On when the softener reports an error code. | cloud summary | every 10 min |
| Flow monitor alert | `binary_sensor.<device>_flow_monitor_alert` | On when the flow monitor's threshold was exceeded. | cloud summary | every 10 min |
| Water usage alert | `binary_sensor.<device>_water_usage_alert` | On when the softener flags unusual water use. | cloud summary | every 10 min |
| Resin alert | `binary_sensor.<device>_resin_alert` | On when the softener flags a resin problem. | cloud summary | every 10 min |
| Connection alert *(diagnostic)* | `binary_sensor.<device>_connection_alert` | On when the cloud flags a connection problem. | cloud summary | every 10 min |
| Alarm sounding *(needs hardware)* | `binary_sensor.<device>_alarm_sounding` | On while the softener's audible alarm is beeping. | cloud summary | every 10 min |
| Water-to-drain alert *(needs hardware)* | `binary_sensor.<device>_water_to_drain_alert` | On when water is detected where it should not be. | cloud summary | every 10 min |
| Error codes *(diagnostic)* | `sensor.<device>_error_codes` | The active error codes as text, and as a list in its attributes. | cloud summary | every 10 min |
| Silence alarm *(needs hardware)* | `button.<device>_silence_alarm` | Silences the audible alarm. | — | — |

The alert flags stay available while the softener is offline — that is when a
connection alert is worth reading. **Error codes** only exists if your device's
payload carries that field at all; the reference device does not send it, so it
has no such sensor. **Alarm sounding** and **Silence alarm** need an audible
alarm; **Water-to-drain alert** needs a water-to-drain or leak sensor.

The `Alert` entity's `event_type` is one of `salt_level_2` (low salt),
`excessive_water_use_alert`, `water_shutoff_valve_opened`,
`connection_status_online`, `connection_status_offline`, or `other` for anything
the integration has not catalogued. The original vendor wording is always kept in
the `alert_type` attribute, so nothing is lost when an alert falls into `other`.

### Old alerts are not replayed

The first time the integration reads the alert list — at setup, and after every
Home Assistant restart — it takes note of what is already there without firing
anything. Only alerts that appear afterwards fire the `Alert` entity and the
`aquahome_event` bus event. That is deliberate: a restart should not flood you
with last month's notifications. It also means the `Alert` entity reads *unknown*
on a fresh installation until the first genuinely new alert arrives.

## Device

| Entity | Entity ID | What it tells you or does | Where it comes from | Updates |
| --- | --- | --- | --- | --- |
| Online *(diagnostic)* | `binary_sensor.<device>_online` | Whether the softener is reachable through the cloud. | device reading | every 10 min |
| Model *(diagnostic)* | `sensor.<device>_model` | The marketing model name. | cloud summary | every 10 min |
| Serial number *(diagnostic)* | `sensor.<device>_serial_number` | The device serial. | device reading | every 10 min |
| Controller firmware *(diagnostic)* | `sensor.<device>_controller_firmware` | Control-board firmware version. | cloud summary | every 10 min |
| Wi-Fi module firmware *(diagnostic)* | `sensor.<device>_wi_fi_module_firmware` | Wi-Fi module firmware version. | cloud summary | every 10 min |
| Days powered up *(diagnostic)* | `sensor.<device>_days_powered_up` | Days the unit has been powered on. | cloud summary | every 10 min |
| RF signal strength *(diagnostic, off by default)* | `sensor.<device>_rf_signal_strength` | Radio link strength to the valve head, in dBm. Off by default because it is installer-grade detail most households never need. | device reading | every 10 min; live during a session |
| Refresh data *(diagnostic)* | `button.<device>_refresh_data` | Asks the softener to push fresh state now, then re-reads it about 15 seconds later. | — | — |
| Advance valve *(config, off by default)* | `button.<device>_advance_valve` | Service tool: sends the softener's "advance valve" command. Off by default because it is a technician's control, not a daily one. | — | — |
| Reset error code *(config, off by default)* | `button.<device>_reset_error_code` | Service tool: clears the softener's error code. Off by default for the same reason. | — | — |
| Reset shutoff valve error *(config, off by default, needs hardware)* | `button.<device>_reset_shutoff_valve_error` | Service tool: clears the shutoff valve's error code. | — | — |

**Online** is the honest availability signal for the whole device, and it is what
most other entities are gated on: when it is off, the readings that describe the
softener go unavailable rather than showing yesterday's numbers. It reads the
cloud's own online flag first, falls back to the device's internal flag, and
assumes online only when neither is reported.

Pressing a button never changes an entity immediately — a button has no state of
its own, and what it did shows up on a later refresh. **Refresh data** is the one
exception, in that it schedules that refresh for you.

Three buttons the iQua app's recharge tile advertises — vacation mode, recharge
off, enable recharge — are **not created**. Their command format has never been
confirmed against the real app, and the integration does not ship commands it
cannot prove. They exist in the code, switched off, waiting for a supervised
test.

## Shutoff valve

*Only on softeners that report a water shutoff valve.* The reference device does
not have one, so none of this appears there.

| Entity | Entity ID | What it does | Where it comes from | Updates |
| --- | --- | --- | --- | --- |
| Water shutoff valve | `valve.<device>_water_shutoff_valve` | Opens and closes the valve. | cloud summary | every 10 min |
| Shutoff valve closed | `binary_sensor.<device>_shutoff_valve_closed` | On when the softener reports the valve as closed. | cloud summary | every 10 min |
| Reset shutoff valve error *(config, off by default)* | `button.<device>_reset_shutoff_valve_error` | Clears the valve's error code. | — | — |

The valve has no position feedback: it is open, closed, or unknown. Anything the
cloud reports that is not plainly "open" or "close" — manual override, error, not
installed — shows as *unknown* rather than a made-up position. The valve is
unavailable while the cloud says no valve is installed.

### The valve shows its movement before the cloud confirms it

Commands are fire-and-forget and the result only shows up on a later poll, so
after you press open or close the entity displays *opening* / *closing* for about
ten seconds. That display is a hint, not a reading; it clears as soon as a poll
confirms the new position, or when the ten seconds run out. The open/closed state
itself is never faked — only the in-between animation is.

If the cloud attaches a confirmation dialog that explicitly forbids the action,
the integration refuses locally with a clear error instead of sending a command
the server would reject.

**Untested on real hardware.** Nobody in the development group owns a softener
with a shutoff valve, so this is built from the documented cloud payloads and
verified only against test fixtures. It should work. If you have this hardware,
reports — good or bad — are very welcome on the issue tracker.

## Leak detectors

*Only when leak detectors are paired with your softener.* Each detector becomes a
separate device in Home Assistant, listed under the softener and named after the
detector's own nickname. Its entity IDs are built from the *detector's* name, not
the softener's — which is why the IDs below say `<detector>`.

| Entity | Entity ID | What it tells you | Where it comes from | Updates |
| --- | --- | --- | --- | --- |
| Leak detected | `binary_sensor.<detector>_leak_detected` | On when that detector is wet. | cloud summary | every 10 min |
| Low battery *(diagnostic)* | `binary_sensor.<detector>_low_battery` | On when the detector's battery is low. | cloud summary | every 10 min |
| Tampered | `binary_sensor.<detector>_tampered` | On when the detector reports tampering. | cloud summary | every 10 min |
| Connectivity *(diagnostic)* | `binary_sensor.<detector>_connectivity` | Whether the detector is connected. | cloud summary | every 10 min |
| Temperature | `sensor.<detector>_temperature` | The detector's temperature reading, in °F as the cloud reports it. | cloud summary | every 10 min |
| Signal strength *(diagnostic, off by default)* | `sensor.<detector>_signal_strength` | Radio strength, in dBm. Off by default for the same reason as the softener's own RF sensor. | cloud summary | every 10 min |

On the softener itself, one extra control appears when leak detectors are
supported:

| Entity | Entity ID | What it does |
| --- | --- | --- |
| Leak detector scan *(config, needs hardware)* | `switch.<device>_leak_detector_scan` | Starts and stops a scan for new detectors. |

Like the valve, the scan switch shows what you asked for optimistically for about
ten seconds, until a poll reports the real scanning state.

Detector entities are unavailable while the softener is offline or while that
detector is no longer paired. A detector that disappears is never deleted, so its
history and any customisation survive a temporary dropout.

**Untested on real hardware**, exactly as for the shutoff valve.

## Device settings

These are not fixed entities. The integration reads your softener's own settings
document from the cloud and creates one entity per setting it can safely
represent: a **dropdown** for settings with a fixed list of options, a **number**
for numeric ones, a **switch** for on/off ones. On the reference device this
yields 15 dropdowns and no numbers or switches. Typical settings include water
hardness, regeneration time, salt type, efficiency mode, maximum days between
recharges, the flow-monitor alert thresholds, and the display preferences.

All of them are filed under **Configuration**, and writing one sends it straight
to the softener; the integration then reads back the document the cloud returns,
so what you see is what the device accepted. Their IDs look like
`select.<device>_regeneration_time` or `select.<device>_salt_type` — that is,
built from the cloud's own label for the setting, not from a name this
integration chose. They refresh every 6 hours, and immediately after you change
one.

### Their names come from the cloud, in your language

Unlike every other entity here, a setting's display name is not translated by the
integration — it is the label the iQua cloud itself sends, in the language Home
Assistant asked it for. That has two consequences. The visible name follows the
language Home Assistant was set to the last time the integration loaded, so
changing your language and restarting re-labels them. But the **entity ID was
minted once**, from the label in use when the entity was first created, and it
never changes afterwards. Set the integration up in German and later switch to
English, and the names will read English while the IDs still read German.

### Settings can appear and disappear

Some settings only exist while another setting has a particular value — the
chemical-feed settings, for instance, only apply when the auxiliary control is
set to chemical feed. A setting that is currently hidden stays in your entity list
but shows as **unavailable**, rather than vanishing and taking its history with
it. Turn the governing setting back on and it becomes usable again. Settings that
appear for the first time are created within one refresh of the settings document.

### The display preferences are off by default

Six settings — volume units, weight units, hardness units, date format, time
format and time zone — are created but **disabled by default**. They configure how
the *phone app* shows things. This integration's sensors bind fixed units and
convert for display themselves, so changing these has no effect on anything in
Home Assistant. They are there if you want to steer the app from Home Assistant;
enable them if so.

If a dropdown's current value is one the cloud no longer offers as an option, it
is shown as-is rather than blanked out, and two options that translate to the same
label are told apart by appending their raw value.

## Events on the event bus

Alongside the `Alert` entity, the integration fires a single event type,
`aquahome_event`, on Home Assistant's event bus. This is what the bundled
blueprints trigger on, and every one of these shows up in the device's logbook as
a readable sentence.

Every event carries `device_id`, `device` (the device's slug) and `type`, plus the
evidence behind it. The types are:

| `type` | Fired when | Extra fields |
| --- | --- | --- |
| `alert` | The cloud raises a new device alert. | `alert_id`, `alert_type`, `title`, `message`, `level`, `timestamp` |
| `leak_suspected` | The nightly analysis starts suspecting a leak. | `rate_liters_per_hour`, `tier` |
| `leak_cleared` | It stops suspecting one. | — |
| `usage_anomaly` | The analysis flags unusual water use. | `reasons` |
| `usage_anomaly_cleared` | Water use returns to normal. | — |
| `vacation_started` | The analysis concludes the household is away. | `since`, `consecutive_days` |
| `vacation_ended` | It concludes the household is back. | — |
| `leak_while_away` | A leak is suspected while the household appears to be away. | `tier`, `rate_liters_per_hour`, `implied_liters_per_day` |
| `regen_scheduled` | The automation tier scheduled a regeneration. | `reason`, `capacity_gallons`, `forecast_gallons` |
| `regen_deferred` | It cancelled a scheduled regeneration because a deferral is active. | `deferral_source` |
| `regen_deferral_expired` | A deferral hit its 21-day limit and let a regeneration through. | `deferral_source`, `days_deferred` |

The detection events only fire on a genuine change of mind: a detector going from
"cannot tell" to a verdict, or back, is silence — never an alarm and never an
all-clear.

Listen for them with a standard event trigger:

```yaml
triggers:
  - trigger: event
    event_type: aquahome_event
    event_data:
      type: leak_suspected
```
