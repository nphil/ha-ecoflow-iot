# EcoFlow serial-number prefixes

EcoFlow serial numbers start with a device-type code (e.g. `R331…` = Delta 2,
`HJ31…` = PowerOcean). This integration resolves each device **by its SN prefix**, on
both of its transports:

- **Cloud entries** match the serial from the account's device list against
  `sn_prefixes` on each device class in `custom_components/ecoflow_iot/devices/`.
- **Bluetooth entries** match the serial carried in the device's own advertisement
  (the `0xB5B5` manufacturer payload) against the 4-character `SN_PREFIX` tuple on
  each device class in `custom_components/ecoflow_iot/eflib/devices/`. Nothing has to
  connect for that, and the advertised *name* is never used for matching — only, for
  one shared prefix, to tell two SKUs apart.

> ⚠️ **EcoFlow publishes no public prefix→model table.** Its own API resolves devices by
> a `productName`/`productType` field, not by serial prefix. The list below is compiled
> from the EcoFlow developer-doc example serials, real user-reported serials, the
> [ioBroker.ecoflow-mqtt](https://github.com/foxthefox/ioBroker.ecoflow-mqtt) device table
> (`lib/ecoflow_data.js`), and — for the family-code prefixes — the EcoFlow **Android
> app's internal device-code registry** (reverse-engineered). The app keys on short
> family codes and distinct codes per model, so a code won't swallow a sibling model.
> Still treat it as a strong reference, not a guarantee — model *names* are not in the app.

## If you got a "Unsupported EcoFlow device (`XXXX`)" repair

When the integration finds a device whose prefix it doesn't recognise, it raises a
Home Assistant **Repairs** issue showing only the prefix (the full serial is never
shown). If that happens:

1. **Find the prefix in the table below.**
   - If it's listed as **supported here but you still got the repair**, your unit likely
     ships a prefix variant we haven't added yet (this is common — e.g. PowerOcean also
     ships as `J32E`, not just `HJ31`). **Open an issue** with the prefix and your model.
   - If it's listed as **known but not yet implemented**, or **not in the table at all**,
     **open an issue** so support can be added:
     <https://github.com/nphil/ha-ecoflow-iot/issues>
2. In the issue, please include: the **4-char prefix**, the **exact model name**, and —
   if you can — a redacted `quota/all` sample (Developer Tools → the integration's
   diagnostics). **Never post your full serial number** publicly.

New prefixes are cheap to add: it's usually a one-line change to the matching device's
`sn_prefixes`.

## Supported over the cloud API (implemented in this integration)

Prefixes below are the **family codes the EcoFlow app itself matches on** (2–3
characters, cross-checked against the app's internal device-code registry —
reverse-engineered from the Android APK). Matching the family code rather than a
full example serial means model **variants** (a different 4th+ character) resolve
correctly instead of raising an "unsupported device" repair.

| Prefix(es) | Model | Confidence |
|---|---|---|
| `R33` | Delta 2 | high (docs + app registry) |
| `R35` | Delta 2 Max | high (app registry) |
| `R62` | River 2 Pro | high (app registry; `R60/R61/R63/R65/R70` are other River models). Also served over local Bluetooth — see below. |
| `DCAB` | Delta Pro | high (docs `DCABZ` + field-observed variants, issue #10; app family code is broad `DC`) |
| `MR5` | Delta Pro 3 | high (app registry) |
| `Y71` | Delta Pro Ultra | high (app registry) |
| `D3N`, `D3M` | Delta 3 Max / Delta 3 Max Plus | high (`D3M` shared — disambiguated by quota `powGetPv2`) |
| `BK` (`BK0`–`BK6`, `BK11`, `BK12`) | Stream AC / AC Pro / Ultra / Ultra X / Pro / Microinverter | high (app registry) |
| `BK21` | Smart Meter (Stream ecosystem; carved out of the broad `BK` Stream match) | high (field-observed serial, issue #5) |
| `HW51` | PowerStream | high (app registry) |
| `HW52` | Smart Plug | high (app registry) |
| `HJ3`, `J32` | PowerOcean | high (`HJ3` docs + app registry, `J32` field-observed) |
| `BX1` | Glacier | high (app registry) |
| `M10` | Power Kits | high (app registry; `M20/M3H` are other products) |
| `KT2` | WAVE 2 (Air Conditioner) | high (app registry). The **Wave 3** (`AC71`) is a different product and is served over local Bluetooth instead — see below. |
| `SP10` | Smart Home Panel | medium |
| `HD3` | Smart Home Panel 2 | high (app registry) |

## Supported over local Bluetooth (not served by the open API)

Most of these models answer `quota/all` with error `1006`, so the cloud path skips
them; they are served over a local BLE link instead. **River 2 Pro (`R621`/`R623`)
is the exception**: its `quota/all` succeeds, so it already has a working cloud API
entry (see the table above) — the BLE path here is additional, not a fallback,
trading a round trip to EcoFlow's servers for faster local push updates and control
through the house's Bluetooth proxies. A unit reachable both ways ends up as two
separate config entries, one per transport; nothing here merges them. Prefixes are
the full 4-character codes the vendored codec matches on (`SN_PREFIX` in
`custom_components/ecoflow_iot/eflib/devices/`), taken from the advertisement.

| Prefix | Model | Confidence |
|---|---|---|
| `AC71` | Wave 3 | high (upstream `rabits/ha-ef-ble` device module; advertises `EF-AC…`) |
| `R651` | River 3 (245 Wh) | high (upstream device module) |
| `R653` | River 3 (230 Wh) | high (upstream device module) |
| `R654` | River 3 UPS (230 Wh) | high (upstream device module) |
| `R655` | River 3 UPS (245 Wh) | high (upstream device module; field-observed) |
| `R631` | River 3 Plus | high (upstream device module) |
| `R634` | River 3 Plus (270) | high (upstream device module) |
| `R635` | River 3 Plus (Wireless) **or** River 3 Pro | prefix high (upstream device module); **SKU ambiguous** — see below |
| `R621` | River 2 Pro | high (upstream device module; field-observed) |
| `R623` | River 2 Pro | high (upstream device module) |

`R635` is shared by more than one SKU, which upstream labels "River 3 Plus Wireless"
unconditionally. Two units observed in one household differ only in their advertised
local name (`EF-R3PR…` alongside `EF-R3…`), so that name's model segment is what
separates them here: `R3PR` → **River 3 Pro**, `R3` → **River 3 Plus (Wireless)**. A
name that cannot be parsed — EcoFlow firmware advertises `Ecoflow-dev` while
provisioning — falls back to the family label **River 3 Plus** rather than guessing an
SKU. If you have an `R635` unit whose label is wrong, please
[open an issue](https://github.com/nphil/ha-ecoflow-iot/issues) with the advertised
name (e.g. `EF-R3PR1234`) and the model on the box — never the full serial.

A supported device that is being heard but never offered in the Bluetooth flow is
usually one already configured (each device is one config entry, keyed on its serial);
an EcoFlow device that is *not* one of the above is not offered at all, because its
protocol is not vendored here.

## Recognised but not supported (silenced — no repair raised)

Some devices are bound to an EcoFlow account but are **not served by the open
developer API** this integration uses, so their data can't be read or controlled
here. They are recognised only so the integration skips them silently instead of
raising an "unsupported device" repair (see `SILENCED_SN_PREFIXES` in
`custom_components/ecoflow_iot/devices/__init__.py`).

| Prefix(es) | Model | Why unsupported |
|---|---|---|
| `SM2A` | EcoFlow x Shelly Plug (app `sm002`) | Third-party device served only by EcoFlow's private app API (`/iot-smart-voice/thirdDevice/...`, user-login auth). The open API returns no quota. These are genuine Shelly Gen2 devices — use Home Assistant's native **Shelly** integration for local data + control. |
| `SM3A` | EcoFlow x Shelly Pro3EM meter (app `sm003`) | Same as above. |
| `DBAB` | Delta Mini | Legacy model not served by the open API: `quota/all` answers error `1006` ("current device is not allowed to get device info"). Not in the developer docs. Field-observed, issue #13. |
| `R60` | River 2 (256 Wh) | Same as above (`R61` River 2 Max is probably the same, unconfirmed). Field-observed, issue #13. Unlike its `R62` (Pro) sibling, `R60` stays unregistered for BLE too — `river2.Device`'s base class is vendored (see `eflib/NOTICE`) but deliberately left out of `SUPPORTED_DEVICE_CLASSES`, and `R61` (River 2 Max) has no vendored BLE module at all, so both stay unreachable from this integration entirely. |

Any other device whose `quota/all` returns error `1006` is skipped silently as
well, without needing a prefix entry here. That includes the **Wave 3** and the
**River 3** family, which need no entry because they are not merely skipped: they are
served over local Bluetooth instead (see
[above](#supported-over-local-bluetooth-not-served-by-the-open-api)). "Not served by
the open developer API" is a statement about that API, not about the hardware — where
another supported route exists, it is used.

## Sources

- EcoFlow developer documentation example serials (per-device specs).
- Real user-reported serials in community issues
  (e.g. tolwi/hassio-ecoflow-cloud issues).
- ioBroker.ecoflow-mqtt device table:
  <https://github.com/foxthefox/ioBroker.ecoflow-mqtt/blob/main/lib/ecoflow_data.js>
  (most complete community list — hand-maintained, contains placeholders and at least
  one known error, so cross-check before relying on an entry).
- EcoFlow Android app device-code registry (reverse-engineered): the app stores ~110
  device family codes and identifies each model by a 2–3 char SN prefix (plus
  `productType`/`model` integers from the device-list API). The family codes above were
  cross-checked against it; ~40 further codes exist for models this integration does not
  yet support.
- Vendored local-BLE codec device modules
  (`custom_components/ecoflow_iot/eflib/devices/`), derived from
  <https://github.com/rabits/ha-ef-ble> (Apache-2.0): the `SN_PREFIX` tuples above are
  upstream's, cross-checked against advertisements observed in the field. See
  `custom_components/ecoflow_iot/eflib/NOTICE` for provenance and modifications.
