# Test fixtures

Real API payloads captured 2026-07-21 from the reference device (AquaHome 20
Smart, features `["regeneration"]` only), copied from `knowledge/api/samples/`.

Modifications relative to the raw captures:

- `summary.json` — account-holder name/email replaced with `Dev User /
  dev@example.com` (PII redaction; structure unchanged).
- `device-detail.json` — **composed** from the real `summary` + `enriched-data`
  + `properties` payloads into the `DeviceObject` shape of
  `GET /devices/{id}?props=true` (no raw capture of that endpoint exists).
  All field values are real.
- `devices-list.json` — the same composed `DeviceObject` wrapped in the
  paginated `GetDevicesOutputBody` shape of `GET /devices`.

Everything else is byte-for-byte the captured payload.
