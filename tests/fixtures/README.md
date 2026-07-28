# Test fixtures

Real API payloads captured 2026-07-21 from the reference device (AquaHome 20
Smart, features `["regeneration"]` only).

Modifications relative to the raw captures:

- `summary.json` — account-holder name/email replaced with `Dev User /
  dev@example.com` (PII redaction; structure unchanged).
- All identifiers are synthesized, not captured: the account and device UUIDs,
  the AWS IoT `thing_name`, the product serial (`4213377-30105-2242`, and its
  slugified `4213377_30105_2242` form in entity unique ids), the WiFi module
  serial, the `pwa` hardware serial and the device nickname (`Demo`) are all
  made-up values in the real formats. Only these identity fields were
  substituted — every measurement, counter, timestamp and unit is untouched.
- `device-detail.json` — **composed** from the real `summary` + `enriched-data`
  + `properties` payloads into the `DeviceObject` shape of
  `GET /devices/{id}?props=true` (no raw capture of that endpoint exists).
  All field values are real.
- `devices-list.json` — the same composed `DeviceObject` wrapped in the
  paginated `GetDevicesOutputBody` shape of `GET /devices`.

Everything else is byte-for-byte the captured payload.

## Datapoint graph captures (2026-07-27, same reference device)

Real responses from `GET /devices/{id}/datapoints/total_outlet_water_gals/graph`,
captured for the statistics backfill. The `graph-meter-*` files use
`value_type=max` (raw lifetime-counter readings per bucket; `0` = no reading in
that bucket — responses are always zero-filled, never empty):

- `graph-meter-yearly.json` — `period_type=year`, 2015→2027 depth-probe sweep.
- `graph-meter-monthly.json` — `period_type=month`, 2020→2026-08.
- `graph-meter-daily.json` — `period_type=day`, 2025-09-01→2026-07-27 (the full
  retention window; readings start 2025-09-14).
- `graph-meter-hourly.json` — `period_type=hour`, 2026-07-01→2026-07-27T12:00.
- `graph-meter-hourly-empty.json` — `period_type=hour` for 2025-10-13,
  captured with `value_type=max_diff` on a sparse-reading day: all-zero rows
  (the zero-filled "no readings in this window" shape the backfill's
  walk-backward stop condition keys on). NB the live 2026-07-27 deployment
  showed `value_type=max` readings ARE retained that far back — there is no
  hourly retention cliff on this device; an all-zero `max` window simply means
  the device never pushed during it.
- `graph-usage-daily-pl.json` — `period_type=day`, `value_type=max_diff`,
  requested with `accept-language: pl`: documents that the `units` string is
  server-localized (`"Litry"`) — the PAIN#5 guard fixture.

`graph-daily-usage.json` (2026-07-21) predates these and remains the
`value_type=max_diff` client/model fixture.
