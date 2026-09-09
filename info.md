# EcoFlow IoT for Home Assistant

> **This is a fork** of [MichelFR/ha-ecoflow-iot](https://github.com/MichelFR/ha-ecoflow-iot).
> It keeps that cloud integration as it is and adds a **local Bluetooth transport** for
> the **Wave 3** air conditioner and the **River 3** family — models the official EcoFlow
> developer API does not serve. Source, releases and issues:
> <https://github.com/nphil/ha-ecoflow-iot>.

Two kinds of config entry:

- **Cloud account** — the **official EcoFlow Developer API** (the `accessKey`/`secretKey`
  IoT Open Platform): **MQTT for live data and control**, automatically **falling back to
  the HTTP REST API** when MQTT is unavailable. One entry covers every device bound to the
  account.
- **Bluetooth device** — one entry per device, holding a **local BLE link**. No API keys
  and no cloud connection while it runs; the only optional online step is a one-time
  lookup of your EcoFlow account user ID during setup.

The cloud path aims for **feature parity with the official EcoFlow developer
documentation** (<https://developer-eu.ecoflow.com/us/document/introduction>): every
supported device's documented quota fields and set-commands are mapped to Home Assistant
entities.

## Features

- **MQTT-first, HTTP fallback** (cloud) — real-time push updates over TLS MQTT; HTTP
  polling kicks in only when MQTT is disconnected or stale, keeping cloud API usage
  minimal.
- **Control over MQTT with HTTP fallback** (cloud) — set commands are published over MQTT
  and confirmed via `set_reply`; if MQTT is down or unacknowledged, the command is retried
  over HTTP `PUT`.
- **Local Bluetooth transport** — a persistently held BLE link per Wave 3 / River 3
  device: values arrive as the device pushes them, commands go out over the same link, and
  reconnects retry forever with jittered, capped backoff. There is no cloud fallback for
  these models, because the developer API does not serve them at all.
- **Connection status sensor** — always available, per device: `connected` /
  `connecting` / `disconnected` plus data source for a cloud device; for a Bluetooth
  device, which adapter or proxy holds the link, drops in the last hour and seconds since
  the last frame.
- **Full entity coverage** — sensors (battery, solar per-MPPT, grid/AC, inverter, power
  flow, energy totals), binary sensors, switches, numbers, selects, and a climate entity
  for the Wave 3.
- Config flow with a transport picker (cloud region + keys, or a Bluetooth device), reauth
  for both, options (cloud: poll interval, MQTT staleness, MQTT on/off; Bluetooth: update
  interval) and redacted diagnostics.

## Supported devices

### Cloud account

Resolved automatically by serial-number prefix. ~1,290 entities are mapped across the
fleet from the official quota schemas. Each link lists every entity for that device:

| Category | Devices |
|---|---|
| **Power Stations** | [Delta 2](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/power_stations/delta_2.md) · [Delta 2 Max](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/power_stations/delta_2_max.md) · [Delta 3 Max](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/power_stations/delta_3_max.md) · [Delta 3 Max Plus](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/power_stations/delta_3_max_plus.md) · [Delta Pro](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/power_stations/delta_pro.md) · [Delta Pro 3](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/power_stations/delta_pro_3.md) · [Delta Pro Ultra](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/power_stations/delta_pro_ultra.md) · [River 2 Pro](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/power_stations/river_2_pro.md) |
| **Solar Systems** | [Stream](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/solar_systems/stream.md) (Ultra / Ultra X / AC / AC Pro / Pro) · [Stream Microinverter](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/solar_systems/stream_microinverter.md) · [PowerStream](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/solar_systems/power_stream.md) |
| **Home Battery** | [PowerOcean](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/home_battery/power_ocean.md) |
| **Smart Living** | [Glacier](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/smart_living/glacier.md) · [Power Kits](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/smart_living/power_kits.md) · [Smart Plug](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/smart_living/smart_plug.md) · [WAVE](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/smart_living/wave.md) (Wave 2, `KT2`) |
| **Whole-Home Backup** | [Smart Home Panel](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/whole_home_backup/smart_home_panel.md) · [Smart Home Panel 2](https://github.com/nphil/ha-ecoflow-iot/blob/main/docs/devices/whole_home_backup/smart_home_panel_2.md) |

### Bluetooth (local)

These models answer the open developer API with error `1006`, so the cloud path skips them
and they are served over a local BLE link instead. The model is resolved from the serial
prefix in the advertisement, before anything connects:

| SN prefix | Model |
|---|---|
| `AC71` | Wave 3 (a different product from the cloud-served Wave 2, `KT2`) |
| `R651` / `R653` | River 3 (245 Wh) / River 3 (230 Wh) |
| `R655` / `R654` | River 3 UPS (245 Wh) / River 3 UPS (230 Wh) |
| `R631` / `R634` | River 3 Plus / River 3 Plus (270) |
| `R635` | River 3 Plus (Wireless) **or** River 3 Pro — one prefix, more than one SKU; the advertised local name separates them (`EF-R3PR…` → Pro), and an unparseable name falls back to the family label rather than guessing |

## Requirements

**Cloud entries**

1. An EcoFlow developer account at <https://developer.ecoflow.com> (EU:
   <https://developer-eu.ecoflow.com>). Under **Security**, create an **AccessKey** and
   **SecretKey** (approval can take a few days).
2. Your device must be bound to the same EcoFlow account.

**Bluetooth entries**

1. A Bluetooth adapter on the Home Assistant host, or an ESPHome Bluetooth proxy in range
   of the device.
2. The **user ID** of the EcoFlow account the device is registered to: every session
   authenticates against it, whatever encryption the device advertises. The config flow
   can look it up once from an e-mail/phone sign-in and keeps only the ID — never the
   password.

## Configure

**Settings → Devices & Services → Add Integration → EcoFlow IoT**, then choose:

- **Cloud API account** — pick your **Region** (Europe = `api-e.ecoflow.com`, Global/US =
  `api.ecoflow.com`) and paste your **Access Key** and **Secret Key**. Every device bound
  to the account is discovered automatically; live values stream over MQTT within seconds.
- **Bluetooth device** — pick one of the supported devices currently being heard, give it
  the name Home Assistant should use (entity ids are minted from it straight away), and
  supply the EcoFlow account user ID once; it is reused for every device added later. A
  Wave 3 or River 3 that Home Assistant hears is also offered on its own as a discovery
  card.

## Using it

- **Entities:** cloud devices get sensors (battery, solar, grid, power flow, energy
  totals…), controls and an always-available **Connection** sensor. Bluetooth devices get
  the sensors, switches, numbers, selects and buttons their own device definition
  supports — for the Wave 3 that includes an **Air conditioner** climate entity, which
  works in the unit set on the appliance itself (16–30 with half-degree steps in Celsius,
  61–86 with whole-degree steps in Fahrenheit). The Wave reports its temperatures in that
  same unit and Home Assistant shows them in your preferred unit either way, so changing
  the setting does not break history. Config-category controls (charge limits, charging
  speed and type, backup reserve, LED, temperature unit, panel temperature display) are
  created disabled; enable the ones you want.
- **Availability:** while a Bluetooth link is down, that device's entities are unavailable
  except the **Connection** sensor, which stays available and reads either the proxy
  holding the link or `disconnected`.
- **Energy dashboard:** cumulative `Wh`/`kWh` sensors carry the right `state_class`, so
  they can be added to Home Assistant's **Energy** dashboard — including the River 3's own
  watt-hour counters over Bluetooth.
- **Options** (gear icon): cloud — HTTP poll interval, MQTT staleness threshold, MQTT
  on/off. Bluetooth — update interval (how often the device is asked to report over the
  link it already holds).
- **Re-authentication:** rotate your API keys and Home Assistant prompts for the new ones;
  a Bluetooth device that rejects the stored user ID prompts for that instead. No need to
  delete anything.

## Holding a Bluetooth link — what it costs

- The held link **occupies a connection slot** on the adapter or ESPHome proxy holding it,
  for as long as the entry is enabled.
- **The EcoFlow app cannot connect while Home Assistant holds the device** — the hardware
  takes one Bluetooth client. To use the vendor app, **disable the config entry** (which
  releases the link) and re-enable it afterwards.
- No proxy is pinned and none is forced to roam; a healthy link is never cycled, and a
  failed attempt is retried with backoff rather than reloading the entry.
- There is **no cloud fallback** for these models: while the link is down their entities
  are unavailable, and the Connection sensor says so.

## Coverage and limits

The Bluetooth protocol is not documented by EcoFlow; it is reverse-engineered upstream,
and coverage is what the decoded telemetry and config messages carry — **not everything
the EcoFlow app can do**, and no app parity is claimed. Several Wave 3 config fields are
deliberately left unwired because their value semantics are unconfirmed (drainage mode,
mood light, screen brightness and timers, pet-care threshold, beeper, system pause); each
one is listed with the evidence it is missing in the README and in `eflib/NOTICE`. The
River 3 decoding is exercised against a real River 3 UPS frame capture; the Wave 3 path is
built from the vendored protocol definitions and unit tests and has not yet been confirmed
against a live unit.

---

**Credits.** The cloud integration is
[MichelFR/ha-ecoflow-iot](https://github.com/MichelFR/ha-ecoflow-iot) (MIT). The local BLE
codec is a vendored minimal subset of
[rabits/ha-ef-ble](https://github.com/rabits/ha-ef-ble) and the reverse engineering in
[rabits/ef-ble-reverse](https://github.com/rabits/ef-ble-reverse) (Apache-2.0), kept under
`custom_components/ecoflow_iot/eflib/` with its own `LICENSE` and `NOTICE`.

Full documentation, install instructions and source:
<https://github.com/nphil/ha-ecoflow-iot>
