# EcoFlow IoT for Home Assistant

> **This is a fork.** [`nphil/ha-ecoflow-iot`](https://github.com/nphil/ha-ecoflow-iot)
> follows [`MichelFR/ha-ecoflow-iot`](https://github.com/MichelFR/ha-ecoflow-iot) and adds
> a **local Bluetooth transport** for the **Wave 3** air conditioner and the **River 3**
> family — models the official EcoFlow developer API does not serve. The cloud
> integration is upstream's work and is unchanged by it. Install from and report issues
> to this fork; see [Credits](#credits).

The integration serves two kinds of config entry:

- **Cloud account** — the **official EcoFlow Developer API** (the `accessKey`/`secretKey`
  IoT Open Platform): **MQTT for live data and control**, automatically **falling back to
  the HTTP REST API** when MQTT is unavailable. One entry covers every device bound to
  the account.
- **Bluetooth device** — one entry per device, holding a **local BLE link** to it. No API
  keys, no cloud connection while it runs; the only optional online step is a one-time
  account-ID lookup during setup. See
  [Local Bluetooth](#local-bluetooth-wave-3--river-3).

The cloud path aims for **feature parity with the official EcoFlow developer
documentation** (<https://developer-eu.ecoflow.com/us/document/introduction>): every
supported device's documented quota fields and set-commands are mapped to Home Assistant
entities.

Cloud device definitions are organised into category packages that mirror the EcoFlow
developer-docs catalog, and resolution is automatic from each device's serial
number. Adding another cloud device later is a single new module.

## Supported devices

### Cloud account (EcoFlow Developer API)

Resolved automatically by serial-number prefix (with quota-based fallback). ~1,290
entities are mapped across the fleet from the official quota schemas. Each link below
lists every sensor / binary sensor / switch / number / select for that device
(generated from the code — see [`docs/devices/`](docs/devices/README.md)).

| Category | Devices |
|---|---|
| **Power Stations** | [Delta 2](docs/devices/power_stations/delta_2.md) · [Delta 2 Max](docs/devices/power_stations/delta_2_max.md) · [Delta 3 Max](docs/devices/power_stations/delta_3_max.md) · [Delta 3 Max Plus](docs/devices/power_stations/delta_3_max_plus.md) · [Delta Pro](docs/devices/power_stations/delta_pro.md) · [Delta Pro 3](docs/devices/power_stations/delta_pro_3.md) · [Delta Pro Ultra](docs/devices/power_stations/delta_pro_ultra.md) · [River 2 Pro](docs/devices/power_stations/river_2_pro.md) |
| **Solar Systems** | [Stream](docs/devices/solar_systems/stream.md) (Ultra / Ultra X / AC / AC Pro / Pro) · [Stream Microinverter](docs/devices/solar_systems/stream_microinverter.md) · [Smart Meter](docs/devices/solar_systems/smart_meter.md) · [PowerStream](docs/devices/solar_systems/power_stream.md) |
| **Home Battery** | [PowerOcean](docs/devices/home_battery/power_ocean.md) |
| **Smart Living** | [Glacier](docs/devices/smart_living/glacier.md) · [Power Kits](docs/devices/smart_living/power_kits.md) · [Smart Plug](docs/devices/smart_living/smart_plug.md) · [WAVE](docs/devices/smart_living/wave.md) |
| **Whole-Home Backup** | [Smart Home Panel](docs/devices/whole_home_backup/smart_home_panel.md) · [Smart Home Panel 2](docs/devices/whole_home_backup/smart_home_panel_2.md) |

The full entity reference index lives in [`docs/devices/README.md`](docs/devices/README.md);
regenerate it after changing entities with `python3 scripts/gen_device_docs.py`.

Devices are matched by **serial-number prefix**. If Home Assistant shows an
**"Unsupported EcoFlow device (`XXXX`)" repair**, your device's prefix isn't mapped
yet — check [`KNOWN_PREFIXES.md`](KNOWN_PREFIXES.md), and if it's missing please
[open an issue](https://github.com/nphil/ha-ecoflow-iot/issues) with the prefix (not your
full serial) so it can be added.

Some devices **cannot** be served over the cloud because EcoFlow's open API does not
serve them: the legacy **Delta Mini** and **River 2** (API error 1006), and the
**EcoFlow x Shelly** plug / meter (use the native Shelly integration instead). They are
skipped silently; the full list is in
[`KNOWN_PREFIXES.md`](KNOWN_PREFIXES.md#recognised-but-not-supported-silenced--no-repair-raised).

### Bluetooth (local)

These models are refused by the open developer API (`quota/all` answers error `1006`), so
the cloud path skips them and they are served over a local BLE link instead. The model is
resolved from the serial prefix in the advertisement, before anything connects:

| SN prefix | Model | Notes |
|---|---|---|
| `AC71` | Wave 3 | Air conditioner — a different product from the cloud-served **Wave 2** (`KT2`), which stays on the cloud path. |
| `R651` / `R653` | River 3 (245 Wh) / River 3 (230 Wh) | |
| `R655` / `R654` | River 3 UPS (245 Wh) / River 3 UPS (230 Wh) | |
| `R631` | River 3 Plus | |
| `R634` | River 3 Plus (270) | |
| `R635` | River 3 Plus (Wireless) **or** River 3 Pro | EcoFlow ships more than one SKU behind this prefix. The advertised local name is the only observed separator: `EF-R3PR…` → **Pro**, `EF-R3…` → **Plus (Wireless)**. A name that cannot be parsed (firmware advertises `Ecoflow-dev` while provisioning) falls back to the family label **River 3 Plus** instead of guessing an SKU. |

Setup, per-model entity coverage and what holding a link implies are in
[Local Bluetooth](#local-bluetooth-wave-3--river-3).

## Features

- **MQTT-first, HTTP fallback** — real-time push updates over TLS MQTT; HTTP polling
  kicks in only when MQTT is disconnected or stale, keeping cloud API usage minimal.
- **Control over MQTT with HTTP fallback** — set commands are published over MQTT and
  confirmed via `set_reply`; if MQTT is down or unacknowledged, the command is retried
  over HTTP `PUT`.
- **MQTT connection status sensor** — an always-available diagnostic sensor per device
  showing `connected` / `connecting` / `disconnected`, plus the active data source and
  last MQTT update time.
- **Full entity coverage** — sensors (battery, solar per-MPPT, grid/AC, inverter, power
  flow, energy totals), binary sensors, switches, numbers and a mode select.
- **Energy Dashboard ready** — devices expose cumulative `Wh` energy sensors (solar
  production, grid import/export, battery charge/discharge, per-device consumption),
  derived from live power where the API reports only watts, so they plug straight into
  Home Assistant's **Energy** dashboard. See the
  [Energy Dashboard setup guide](docs/energy_dashboard.md) for the per-device mapping and
  the grid/battery sign conventions.
- **Bundled Lovelace card** — an **EcoFlow Energy** card for Stream devices, with the
  device image, an animated battery bar, live Solar and Grid power, today's solar
  production (with an optional forecast comparison) and a tap-to-expand per-panel
  breakdown. Entities are auto-detected; it ships with the integration and registers
  itself, so no manual resource setup is needed. See [EcoFlow Energy card](#ecoflow-energy-card).
- **Local Bluetooth transport** — one config entry per Wave 3 / River 3 device holding a
  persistent BLE link: values arrive as the device pushes them, commands go out over the
  same link, and reconnects run forever with jittered, capped backoff. A **Connection**
  diagnostic sensor names the adapter or proxy holding the link. See
  [Local Bluetooth](#local-bluetooth-wave-3--river-3).
- Config flow with a transport picker — **cloud account** (region, keys, reauth, options:
  poll interval, MQTT staleness, MQTT enable/disable) or a **Bluetooth device** (pick the
  device, name it, supply the EcoFlow account user ID once; option: update interval) —
  and redacted diagnostics for both.

## Requirements

**Cloud entries**

1. An EcoFlow developer account at <https://developer.ecoflow.com> (EU:
   <https://developer-eu.ecoflow.com>). Under **Security**, create an **AccessKey** and
   **SecretKey** (approval can take a few days).
2. Your device must be bound to the same EcoFlow account.

**Bluetooth entries**

1. A Bluetooth adapter on the Home Assistant host, or an ESPHome Bluetooth proxy within
   range of the device — the ordinary Home Assistant Bluetooth setup, nothing extra.
2. The **user ID** of the EcoFlow account the device is registered to. Every session
   authenticates against it whatever encryption the device advertises, so it is required;
   the config flow can look it up once from an e-mail/phone sign-in and keeps only the ID.

## Installation

### Option A — HACS (recommended)

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=nphil&repository=ha-ecoflow-iot&category=integration)

1. Click the button above to add this repository to HACS — or add it manually:
   **HACS → ⋮ (top-right) → Custom repositories**, URL
   `https://github.com/nphil/ha-ecoflow-iot`, category **Integration**.
2. Search for **EcoFlow IoT** in HACS and click **Download**.
3. **Restart Home Assistant.**

Already running upstream's **EcoFlow IoT** from HACS? Remove that custom repository and
download this one in its place: both ship the same integration domain (`ecoflow_iot`), so
only one can be installed at a time. Existing cloud config entries, entities and entity
ids survive the swap — nothing has to be re-added.

### Option B — Manual

1. Download the latest [release](https://github.com/nphil/ha-ecoflow-iot/releases)
   (or clone this repo).
2. Copy the `custom_components/ecoflow_iot` folder into your Home Assistant
   `config/custom_components/` directory (so you have
   `config/custom_components/ecoflow_iot/`).
3. **Restart Home Assistant.**

### Configure

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ecoflow_iot)

1. Go to **Settings → Devices & Services → Add Integration**, search for
   **EcoFlow IoT** (or use the button above), then pick what to add:
   - **Cloud API account** — choose your **Region** (Europe = `api-e.ecoflow.com`,
     Global/US = `api.ecoflow.com`) and paste your **Access Key** and **Secret Key**.
     Every device bound to the account is discovered automatically and its
     sensors/controls are created; live values stream over MQTT within seconds.
   - **Bluetooth device** — pick one of the supported devices currently being heard, name
     it, and (once per account) supply the EcoFlow user ID. A Wave 3 or River 3 Home
     Assistant hears is also offered on its own as a discovery card. Walkthrough:
     [Local Bluetooth](#local-bluetooth-wave-3--river-3).

### Using it

- **Entities:** each device gets sensors (battery, solar, grid, power flow, energy
  totals…), controls (switches/numbers/selects for the documented settings) and an
  always-available **Connection** diagnostic sensor. See
  [Supported devices](#supported-devices) for the full per-device list.
- **Energy dashboard:** the cumulative `Wh`/`kWh` energy sensors carry the right
  `device_class`/`state_class`, so they can be added to Home Assistant's **Energy**
  dashboard. For the Stream, see the [Energy Dashboard setup guide](docs/energy_dashboard.md)
  (which sensor goes in each field, plus the grid/battery sign conventions).
- **Options** (gear icon on the integration): a cloud entry has the HTTP poll interval,
  the MQTT staleness threshold and an MQTT on/off toggle; a Bluetooth entry has the
  update interval. HTTP-only values (marked 🌐 in the per-device docs) are refreshed over
  HTTP on the poll interval even while MQTT is connected, but only for devices that have
  such entities.
- **Re-authentication:** if you rotate your API keys, Home Assistant prompts you to
  re-enter them — no need to delete the integration.

### EcoFlow Energy card

The integration bundles a custom Lovelace card, **EcoFlow Energy**, tailored to Stream
devices (Ultra / Ultra X / AC / AC Pro / Pro). It serves and registers itself, so in the
common case you just add it from the dashboard's card picker.

<p align="center">
  <picture><source srcset="docs/images/energy-card.webp" type="image/webp"><img src="docs/images/energy-card.gif" alt="EcoFlow Energy card" width="460"></picture>
  &nbsp;&nbsp;
  <picture><source srcset="docs/images/energy-card-solar-today.webp" type="image/webp"><img src="docs/images/energy-card-solar-today.png" alt="Solar today graph" width="520"></picture>
</p>

- **Add it:** edit a dashboard → **+ Add Card** → search for **EcoFlow Energy**. With a
  single Stream it auto-detects everything; with more than one, pick the device in the
  card's visual editor.
- **Shows:** the device photo inside a **circular battery gauge** — a state-of-charge ring
  that changes colour and animates (a travelling spark) while charging/discharging, with the
  level as a badge and the charge/discharge speed as a chip —
  **Solar power**, **Grid power** (import/export), the **AC sockets** (shown beside the
  gauge, with live draw + an on/off toggle that asks for confirmation before
  switching a socket off), and **today's solar production** with an optional forecast
  comparison. Tap
  **Solar power** for a per-panel (per-MPPT) breakdown; tap **Solar today** for an hourly
  production graph with the forecast curve overlaid (like the Energy dashboard's Solar
  production card).
- **Solar forecast:** uses the **same forecast configured in Home Assistant's Energy
  dashboard** (Settings → Dashboards → Energy → Solar panels). The card's editor lists
  those forecast providers (with their integration brand icons, like HA does) so you can
  choose which to include; the dashed forecast curve and the "X / Y kWh" comparison are
  drawn from them. Turning off **Compare today's production with the forecast** disables
  the whole forecast section, including the provider options. If no Energy-dashboard
  forecast is set up, the card simply omits the comparison.
- **Follow a date selection:** set an **Energy collection key** (Advanced page in the
  editor) matching a `hui-energy-period-selector` (Energy date selection) card's
  `collection_key`, and the card shows the selected day's production and graph instead of
  today's — so a period selector on the same view drives the card's date.
- **Device image:** auto-selected from the device model, or pick one from a gallery of
  bundled EcoFlow product images (the whole supported fleet) in the editor. When the image
  is turned off, the gauge shows a battery icon instead.
- **Editor:** an ABRP-style visual editor — Appearance toggles + the device-image picker,
  an Entities page for overriding any auto-detected entity (Auto / Entity / Custom-template),
  a **Solar panels** page to rename, re-map or hide each panel, and a **Solar forecast** page
  to pick which forecast providers to use.
- **Today's total** is read from the recorder's statistics for the cumulative Solar
  energy sensor (the same source as the Energy Dashboard), so the **recorder** must be
  enabled (it is by default).
- The card is auto-registered as a dashboard resource when Lovelace runs in **storage
  mode** (the default UI mode). In **YAML mode**, add the resource manually:
  `/ecoflow_iot/ecoflow-energy-card.js` as a **JavaScript Module**.

### EcoFlow House card

A second bundled card, **EcoFlow House**, draws the whole-home energy-flow illustration:
a rendered house with the battery/inverter box in front, the live **Grid**, **Solar** and
**Home** figures across the top, the **battery** state below, and **animated flow lines**
between them — solar when the panels produce, grid in/out by direction, and battery
charge/discharge. It's the same scene the EcoFlow app shows, built from the app's own
artwork and flow animations.

- **Add it:** **+ Add Card** → search for **EcoFlow House**. Entities auto-detect from the
  integration (shared with the Energy card); with more than one Stream, pick the device in
  the editor.
- **Pick the house:** the editor offers **9 house styles** and a **Day / Night / Automatic**
  picture (Automatic follows the sun, falling back to the UI theme).
- **Pick the battery:** choose the device render shown in front of the house from a gallery
  (EcoFlow Stream, Stream Ultra and more). The animated SoC fill and charge/discharge glow
  are shown for the Stream system battery, which they're aligned to; other renders are shown
  statically.
- **Configure:** toggle each flow/figure on or off and override any of the driving sensors.
  The solar flow matches the chosen house.
- Registers itself as a dashboard resource the same way as the Energy card; in **YAML mode**
  it's the same module: `/ecoflow_iot/ecoflow-energy-card.js`.

### EcoFlow Space card

A third bundled card, **EcoFlow Space**, is a **full-screen, fully configurable** whole-home
dashboard modelled on the EcoFlow app's "space" view: the house illustration with a left
icon **sidebar**, a top bar (**clock** + **weather**), **floating overlays** over the scene,
and a row of **stat tiles** along the bottom.

<p align="center">
  <picture><source srcset="docs/images/space-card.webp" type="image/webp"><img src="docs/images/space-card.png" alt="EcoFlow Space card" width="680"></picture>
</p>

- **Add it:** **+ Add Card** → search for **EcoFlow Space**. Out of the box it auto-discovers
  the integration and your Home Assistant **Energy dashboard**, so it looks like the app with
  no manual wiring.
- **Overlays & tiles:** each floating overlay and bottom tile can use a **preset** (Solar /
  Grid / Battery; or today's Solar / Consumption / Energy-independence from the Energy
  dashboard), an explicit **entity**, or a live **Jinja template** — with dynamic value
  colours, per-overlay size, and Home Assistant's native icon/colour pickers in the editor.
- **Sidebar tabs:** the first tab is the scene above; each additional tab **embeds an
  existing Lovelace view by its path**, rendered inline. Icons, labels and alignment are
  configurable.
- **Top bar:** an optional **clock** (with date) and a **weather** widget, each with its own
  size; the tile row scales too.
- **Solar dialog:** tapping the Solar overlay or tile opens the same hourly-production +
  forecast graph and **per-array breakdown** as the Energy card.
- Drag overlays to position them in the visual editor; reuses the House card's artwork and
  flow animations, and fills the screen when cast to a display (e.g. a Nest Hub). Same
  bundled module / resource as the other cards.

## Local Bluetooth (Wave 3 / River 3)

A Bluetooth entry is one device, served entirely locally. Nothing about it touches the
cloud once it is set up.

### Why Bluetooth for these models

- **The cloud API refuses them.** `quota/all` for a Wave 3 or River 3 serial answers
  EcoFlow error `1006` ("current device is not allowed to get device info"), so the cloud
  coordinator skips those devices silently — there is no cloud path to extend for them.
- **No supported LAN route to them is known.** EcoFlow documents no local IP API for
  these models, and none has been found in practice. The EcoFlow Android app does carry
  LAN-mode strings, but the one such mode found there is a per-device capability gate for
  a different product (a PowerInsight hotspot controlling a Power Kits 5 kVA unit) — not
  evidence of a home-LAN API for the River 3 or Wave 3, and the app was read at the
  resource/string level rather than decompiled in full, so absence there proves nothing
  either. All of that is a statement about what is documented and observed today, not a
  proof that none could exist: if a supported local IP path turns up, it belongs
  alongside Bluetooth in the transport layer rather than replacing it.
- **Bluetooth is the local path that works.** The link is encrypted with keys derived
  from the EcoFlow account ID and the device's own advertised scheme, and the device
  pushes its telemetry over it.

### Adding a device

1. **Add Integration → EcoFlow IoT → Bluetooth device**, or accept the discovery card
   Home Assistant raises when it hears one.
2. **Choose the device.** Only supported devices currently being heard are listed, each
   as *name — model (address)*. If yours is missing, wake it up or move it closer to a
   Bluetooth proxy and try again — these devices advertise only every few seconds.
3. **Name it.** The name is asked for here, before any entity exists, because entity ids
   are minted from it the moment entities are created — a device renamed afterwards keeps
   ids built from the old name.
4. **EcoFlow account.** Asked once, then reused for every device added later. Either type
   the account **user ID** (digits only — no password, no request), or leave it blank and
   give the account **e-mail/phone**, **password** and **account region** to look the ID
   up once. Only the resulting ID is written to the config entry; the password is sent to
   EcoFlow for that one lookup and is never stored.

If a device later rejects the stored user ID, Home Assistant raises a re-authentication
prompt for that entry (enter the ID again, or sign in to look it up). Nothing else
triggers reauth: an out-of-range device, a busy adapter or a dropped link are retried, not
escalated.

**Options** (gear icon on the entry): **Update interval (seconds)**, default 10, range
1–600 — how often the device is asked to report over the link it is already holding. It is
applied in place; changing it does not drop a healthy link.

### What you get

Entities exist only for fields the model in question actually declares, and a field the
device has not reported reads as unknown rather than as zero.

**Wave 3 (`AC71`)**

- **Air conditioner** climate entity: on/off, HVAC modes **cool**, **heat**, **fan only**,
  **dry** and **heat/cool** (the device's thermostatic mode), five fan speeds (low →
  high), target temperature in cool/heat (16–30 °C, half-degree steps), a target
  temperature range in thermostatic mode, target humidity 40–80 % in dry mode, current
  temperature and humidity from the ambient sensors, and the device's sub-mode (normal /
  max / sleep / eco) as the preset list where the device offers it (cooling and heating).
  The climate entity works in the unit selected **on the appliance**: on a unit set to
  Fahrenheit the setpoint bounds are converted and the step becomes a whole degree, so
  setpoints round-trip in the unit the protocol actually accepts.
- Sensors: battery level, ambient temperature and humidity, supply-air temperature,
  condensate water level, input/output power, AC input power, battery power. Diagnostic
  and disabled by default: battery cell temperature, fan level, drainage mode, sleep
  state, maximum AC charging power.
- Temperature **units**: the Wave 3 reports its temperatures in the unit set on the
  appliance itself, and Home Assistant shows them in your preferred unit either way —
  changing the setting does not break history. The climate entity is the one place the
  appliance's unit is visible directly (see above), because its setpoints have to
  round-trip in the unit the protocol accepts. The battery **cell** temperature is always
  Celsius: it is a raw pack reading and does not follow the display setting.
- Sensors decoded from the device's runtime-property frame: return-air, outdoor ambient,
  condenser, evaporator and compressor-discharge temperature (the last three diagnostic
  and disabled by default). They are presence-gated — a unit that does not report one
  leaves it unknown — and they are **decoded but not yet confirmed on live hardware**:
  the capture available while this was built carries no runtime-property frame.
- Binary sensors: **draining**, **pet care warning**.
- Switch: **pet care**.
- Numbers (config category, disabled by default): battery charge limit min / max (each
  bounded by the other) and **AC charging speed** in watts, whose ceiling is read from the
  device rather than hardcoded — the same control the River 3 family has.
- Selects (config category, disabled by default): **temperature unit** (Celsius /
  Fahrenheit) — the unit *the appliance itself* uses, so it changes what the Wave's own
  screen shows and the unit the climate setpoints are expressed in, while your Home
  Assistant readings stay in your own unit; and **panel temperature display** (ambient /
  supply air), which chooses which temperature that screen shows.

Not implemented on the Wave 3 — **protocol semantics unconfirmed**, not device
limitations. The `ac517` config message declares these fields, but nothing in the
licensed sources pins what their values mean, and none of them is guessed:

- **Drainage mode** and **mood/ambient light mode**: a bare number with a readback but no
  enum anywhere in the sources. Wave 2's drain-mode control does not carry over — that is
  a different protocol entirely. Drainage state and mode are therefore *reported only*.
- **Screen brightness**, **screen-off time**, **device standby time**, **power-off
  delay**: readbacks exist, but the value scale and time unit are undeclared.
- **Pet-care warning threshold**: readable, but no permitted min/max is declared, and a
  pet-safety threshold is not something to offer a guessed range for.
- **Beeper** and **system pause**: the device publishes no state for either, so a switch
  could only invent its own position.
- **Factory reset** and **SoC calibration**: destructive, with no readback. Deliberately
  not exposed.
- **Device-side schedules/timers**: a separate feature with its own data model, not a
  control.

The field-by-field version, naming the exact missing prerequisite for each, is in
[`eflib/NOTICE`](custom_components/ecoflow_iot/eflib/NOTICE) under *Wave 3 control
coverage*.

**River 3 / River 3 UPS / River 3 Plus / River 3 Pro**

- Sensors: battery level (plus the main pack's own level on Plus/Pro), input and output
  power, AC input and AC output power, solar/DC input power, 12 V, USB-C and USB-A output
  power, and the device's cumulative watt-hour counters (AC in, AC out, solar in, 12 V,
  USB-A, USB-C and the input/output totals) — energy sensors with the device and state
  class the **Energy** dashboard needs. Disabled by default: battery input/output power,
  remaining charge and discharge time, maximum AC charging power, cell temperature.
- Binary sensors: **AC plugged in**, **error** (with the raw code as an attribute),
  **fan running** (diagnostic, disabled), **extra battery connected**.
- Switches: **AC output** and **12 V output** (as outlets), **energy backup**.
- Numbers: **backup reserve level** (available only while energy backup is on), battery
  charge limit min / max, AC charging speed (bounded by the maximum the device reports)
  and DC charging current limit — the last four in the config category, disabled by
  default.
- Selects: **DC charging type** (auto / car / solar); **LED** mode on Plus/Pro (config,
  disabled by default).
- Button: **power off** (disabled by default).
- Plus/Pro extras: second USB-C, second USB-A, second solar input and the 24 V output are
  only created once a unit actually reports them, so an SKU without a port gets no entity
  instead of one stuck at unknown. The add-on battery's charge, cell temperature and
  serial are diagnostic and disabled by default.

**Every Bluetooth device** also gets a **Connection** diagnostic sensor. While the link is
down every other entity of that device is unavailable and this one stays available —
reporting that is its whole purpose, and it is the entity to key an automation off.

- **State** — the adapter or proxy **holding the link right now**, taken from the
  Bluetooth manager's live connection allocations, or `disconnected` when no link is held.
- **`scanner_source`** attribute — the advertisement source of the **last connect
  attempt**, recorded at that moment and not refreshed afterwards. It is not a live view
  of the holding proxy, so a value that differs from the state only means the device was
  heard through one scanner and connected through another. **Divergence alone is not
  roaming** and needs no action.
- Remaining attributes: hold flag, drops in the last hour, last drop, current reconnect
  attempt, and seconds since the last frame (measured from receipt, so a repeated
  identical frame still counts as answering).

Controls in the **config** category (charge limits, charging speed, DC charging type,
backup reserve, LED, temperature unit, panel temperature display) are created **disabled**
across both models: they exist, and you enable the ones you actually want rather than
finding a dashboard full of settings.

### How the link is held

- One supervisor per device connects, authenticates, then **sits idle while the link is
  healthy**. A healthy link is never torn down and rebuilt — not for a command, not for a
  refresh, not on a timer. The update interval asks the device to report over the link it
  already has; it never re-opens one.
- Reconnects retry **forever** with jittered, capped backoff for as long as the entry is
  loaded. A failed attempt never reloads the config entry.
- **No proxy is pinned and none is forced to roam.** Every attempt hands Home Assistant a
  freshly resolved device and lets it score the adapters and proxies itself.
- If the device is not being heard at setup, the entry stays *not ready* and retries as
  soon as the next advertisement arrives, rather than polling for it.

What that costs, stated plainly:

- **The held link occupies a connection slot** on whichever adapter or ESPHome proxy holds
  it, for as long as the entry is enabled. Proxies support only a handful of concurrent
  connections, so several EcoFlow devices behind one proxy compete with everything else
  using it.
- **The EcoFlow app cannot connect to the device while Home Assistant holds it.** The
  hardware accepts one Bluetooth client at a time. To use the vendor app (firmware
  updates, app-only settings), **disable the config entry** — Settings → Devices &
  Services → ⋮ on the entry → *Disable* — which releases the link; re-enable it
  afterwards.
- **There is no cloud fallback.** These models are not served by the developer API, so
  when the link is down their entities are unavailable. Nothing substitutes cloud data for
  them.

### When it stays down

- After **15 continuous minutes with no link**, the integration raises a **repair**
  (Settings → Devices & Services → **Repairs**): *"<device> is unreachable over
  Bluetooth"*. Nothing has been given up at that point — the supervisor is still
  retrying underneath it — the repair exists because by then anything that heals this
  automatically has already had its chance. It **clears itself** the moment a link is
  established, including across a reload of the entry.
- Its **Fix** button walks a recovery ladder, cheapest rung first. Every rung performs
  its action and then waits for the link before reporting back, so nothing claims
  success on the strength of having run:
  1. **Check again** — changes nothing; the supervisor may be mid-attempt.
  2. **Reload the integration** — rebuilds the entry, and with it the link, from nothing.
  3. **Restart the Bluetooth proxy** — offered only when the proxy that holds (or last
     held) the link is an ESPHome node exposing a `restart_proxy` action. That firmware
     **refuses to restart inside its first ~20 minutes of uptime**, and the action
     reports success either way, so this rung may legitimately do nothing — it says as
     much rather than claiming a reboot it cannot verify.
  4. **Power-cycle the device** — last resort: switches a `switch` entity of your choice
     off, waits 10 seconds and switches it back on, **cutting the device's mains
     supply**. The outlet you pick is remembered for next time, and closing the dialog
     mid-cut does not leave the device dark — the switch goes back on regardless.
- While the link is up, the proxy holding it is recorded in the entry's options
  (`last_holding_proxy`). An unreachable device has no holding proxy left to discover,
  so without that record there would be nothing for rung 3 to act on.

## How data flows

**Cloud entries**

```
EcoFlow Cloud ──MQTT (TLS 8883)──▶ live quota push ──▶ entities (instant)
        │                                  ▲
        └──HTTP REST (signed) ─ fallback ──┘  (only when MQTT down/stale)
```

- Reads: MQTT updates merge into a per-device quota snapshot and push to HA. The polling
  tick performs an HTTP `quota/all` refresh only when MQTT is disconnected or no message
  has arrived within the staleness window.
- Writes: published to `/open/{account}/{sn}/set`, correlated to the device's
  `set_reply`; on timeout/disconnect, the same payload is sent via HTTP `PUT`.

**Bluetooth entries**

```
device ──BLE notify (held link)──▶ decoded frame ──▶ entities (as pushed)
   ▲                                                      │
   └────────── BLE write, one command at a time ◀─────────┘
```

- Reads: the device pushes display and runtime property frames. Changed values feed the
  coordinator; the Update interval option (default 10 seconds, 0 for no rate limit)
  coalesces publications, with a trailing update so the final value is not lost.
- Writes: serialised, one command at a time. A successful Bluetooth write confirms
  transport submission, not necessarily that the device accepted or applied the setting.
  Some vendored climate setters reflect the requested value after the write; subsequent
  device reports remain authoritative. This is not a universal device-acknowledgement
  guarantee, and physical Wave 3 control acceptance has not yet been verified.

## Connection resilience, cloud entries (MQTT down → HTTP → auto-recover)

A cloud entry degrades gracefully and recovers on its own — you don't need to
reload it after a network blip:

1. **MQTT keeps reconnecting in the background.** The MQTT client runs paho's own
   network loop, which automatically retries the initial connection and reconnects
   after any unexpected drop, with built-in backoff (≈1s, doubling up to ~120s). The
   loop is only stopped when you remove/reload the integration.
2. **HTTP covers the gap.** While MQTT is not connected (or no MQTT message has
   arrived within the staleness window), the periodic poll falls back to the HTTP
   REST API, so entities keep updating — just at the poll interval instead of in
   real time. The **Connection** sensor shows `connecting` and `data_source: http`.
3. **Auto-recovery.** When MQTT reconnects, it **re-subscribes** to all topics and
   the Connection sensor returns to `connected`. As soon as live messages resume,
   HTTP polling automatically stops again (`data_source: mqtt`).
4. **Rotated credentials.** If the broker rejects the stored credentials (they are
   rotated by EcoFlow during maintenance), the client re-fetches a fresh certificate
   from the API and reconnects, rather than looping on stale credentials.

In short: a temporary MQTT outage causes a short switch to slower HTTP polling and an
automatic return to real-time MQTT once connectivity is restored. A Bluetooth entry has
its own, separate resilience story — one held link, retried forever, with no second data
source to fall back to; see
[How the link is held](#how-the-link-is-held).

## Manual test checklist

**Cloud entry**

1. Add the integration; confirm a device and its entities are created.
2. The **Connection** sensor shows `connected` and `data_source: mqtt`.
3. Battery/solar/grid values update live (within seconds) without polling.
4. Temporarily block outbound MQTT (port 8883): the Connection sensor flips to
   `disconnected`, `data_source` becomes `http`, and values keep updating on the poll
   interval.
5. Toggle an AC socket switch / change the charge-limit number: the device reacts and
   the new state survives a refresh.
6. Download diagnostics (device page → ⋮ → *Download diagnostics*) and verify keys are
   redacted.

**Bluetooth entry**

1. Add a Wave 3 or River 3; confirm the device and its entities are created.
2. The **Connection** sensor reports a live link — the name of the adapter or proxy
   holding it, or plain `connected` when the Bluetooth stack offers no allocation data —
   and its `seconds_since_last_frame` attribute keeps resetting.
3. Toggle an output / change a setpoint: the device reacts, and the entity settles on the
   value the device pushes back.
4. Power the device off or carry it out of range: entities go unavailable, **Connection**
   reads `disconnected` and `reconnect_attempt` climbs. Power it back on: it reconnects on
   its own, with no reload.
5. Disable the config entry: the link is released and the EcoFlow app can connect again.
   Re-enable it and the link comes back.
6. Download diagnostics and verify the address, serial, user ID and proxy name are
   redacted.

## Entities per device

Every device — cloud or Bluetooth — exposes an always-available **Connection**
diagnostic sensor: `connected` / `connecting` / `disconnected` plus data source and
last-update for a cloud device, and the holding adapter/proxy plus link health for a
Bluetooth one.

The complete per-device entity tables for **cloud** devices (sensors, binary sensors,
switches, numbers, selects — with device class, unit, underlying quota key and
diagnostic/disabled flags) are generated from the device definitions and live under
[`docs/devices/`](docs/devices/README.md). See the per-device links in
[Supported devices](#supported-devices) above. Regenerate them with:

```bash
python3 scripts/gen_device_docs.py
```

Bluetooth devices are not in those generated tables: their entities come from the
vendored device definitions rather than the cloud quota schemas, and the per-model list
is in [What you get](#what-you-get) above.

## Architecture

```
custom_components/ecoflow_iot/
├── api/                  # cloud: signing (auth.py), HTTP client, MQTT client, errors
├── devices/              # cloud device layer (mirrors the EcoFlow docs catalog)
│   ├── base.py           #   EcoFlowDevice + entity-description dataclasses
│   ├── commands.py       #   command builders (stream envelope / legacy / cmdSet)
│   ├── __init__.py       #   DEVICE_REGISTRY + resolve_device(sn, quota)
│   ├── power_stations/   #   delta_2, delta_2_max, delta_3_max(_plus), delta_pro(_3/_ultra), river_2_pro
│   ├── solar_systems/    #   stream, power_stream
│   ├── home_battery/     #   power_ocean
│   ├── smart_living/     #   glacier, power_kits, smart_plug, wave
│   └── whole_home_backup/#   smart_home_panel, smart_home_panel_ii
├── coordinator.py        # cloud: MQTT push + HTTP fallback, write dispatch, conn state
├── entity.py             # shared cloud entity base (device info, availability, values)
├── ble/                  # local Bluetooth transport (one entry = one device)
│   ├── __init__.py       #   entry setup/unload, advertisement re-appear watch
│   ├── coordinator.py    #   the link: connect, authenticate, hold, reconnect, commands
│   ├── entity.py         #   shared BLE entity base + control-descriptor binding
│   └── binary_sensor.py / button.py / climate.py / number.py / select.py / sensor.py / switch.py
├── eflib/                # vendored BLE protocol codec (Apache-2.0, see its NOTICE)
│   └── devices/          #   wave3, river3, river3_plus — the models spoken to locally
└── sensor.py / binary_sensor.py / switch.py / number.py / select.py
```

**Adding a device** = drop one `devices/<category>/<model>.py` with an `EcoFlowDevice`
subclass (declaring its `model`, `sn_prefixes`, `matches()`, per-platform
`entity_descriptions()`, and `command_fn`s) and register it in `devices/__init__.py`.
No platform code changes. Each device's command style is handled by its
`command_fn`/`build_command`: the Stream/Delta-3 envelope (`cmdId/cmdFunc`), the legacy
`moduleType`/`operateType` body, the `cmdSet`/`id` body, or `cmdCode` — all built via
`devices/commands.py`.

`resolve_device()` is **SN-prefix-authoritative**: a serial's prefix selects the model;
quota fields only disambiguate models that share a prefix (e.g. Delta 3 Max vs Max Plus)
or identify devices with an unknown serial.

**The Bluetooth side** is separate from all of that. `ble/__init__.py` owns one device per
config entry, and `ble/coordinator.py` owns the link — nothing else touches Bluetooth.
Entities are generated from the vendored device class itself: sensor rows are keyed by the
property name the model declares, and every control (switch, number, select, button,
climate) comes from a `@controls.*` descriptor on that class, which already carries the
field it reads, its bounds, what gates it and whether it is worth enabling. A model
therefore gets exactly the entities its own definition supports; adding a device is a
vendored device module plus one registry line in `eflib/__init__.py`, with no platform
changes.

## Changes in 0.45

**0.45.1**

- **Bluetooth telemetry now reaches the entities.** The vendored library filled its
  property-less update-callback list but never read it, so a live River 3 Pro
  authenticated, parsed frames and decoded fields while every Home Assistant entity sat
  at unknown. A frame that changes at least one field now notifies once — one
  notification per frame, not one per field — and the timer-driven single-field fallback
  notifies too. Details in `eflib/NOTICE`, modification 15.
- **Connection diagnostic sharpened**: the sensor's state is read from the Bluetooth
  manager's live connection allocations (the proxy actually holding the link) and no
  longer doubles a proxy's address in the name, and it degrades to an unnamed proxy
  instead of raising if the Bluetooth stack has gone away — the one entity whose job is
  to report a broken link must not be the one that breaks.

**0.45.0**

- **Local Bluetooth transport** for the **Wave 3** (`AC71`) and the **River 3** family
  (`R651`/`R653`/`R654`/`R655`, `R631`/`R634`/`R635`): one config entry per device, a
  persistently held link, and entities built from the device's own telemetry and config
  messages. These models are refused by the open developer API, so there was no cloud
  path to extend for them.
- **Config flow** now opens with a transport picker. The Bluetooth path lists the
  supported devices currently being heard, asks for the Home Assistant name up front
  (entity ids are minted from it) and asks for the EcoFlow account user ID once, caching
  it for every device added afterwards; the optional e-mail/phone sign-in looks that ID up
  and stores only the ID.
- **Wave 3 climate entity** works in the temperature unit set on the appliance — 16–30
  with half-degree steps in Celsius, 61–86 with whole-degree steps in Fahrenheit — and
  offers only the controls the active mode accepts. Beyond the climate entity, the Wave
  gets the config controls whose protocol semantics are pinned by the licensed sources —
  temperature unit, panel temperature display and AC charging speed — plus its runtime
  temperatures; every config field left unwired is listed with the evidence it is missing,
  in the README and in `eflib/NOTICE`. No app-parity claim.
- **River 3 Plus / Pro**: the shared `R635` prefix resolves to Plus (Wireless) or Pro from
  the advertised name instead of one hard-coded label, and the second USB-C/USB-A, the
  second solar input and the 24 V output become entities once a unit reports them.
- **Per-device Connection diagnostic** for Bluetooth entries: the proxy or adapter
  holding the link (or `disconnected`), plus drops in the last hour, last drop, reconnect
  attempt, seconds since the last frame, and `scanner_source` — the advertisement source
  of the last connect attempt, which is not a live view of the holding proxy and may
  legitimately differ from the state. Address, serial, user ID and proxy names are
  redacted from diagnostics downloads.
- **The cloud path is unchanged.** Existing cloud entries, their entities and their entity
  ids are untouched by this release.

## Credits

- **Upstream integration** — [MichelFR/ha-ecoflow-iot](https://github.com/MichelFR/ha-ecoflow-iot):
  the entire cloud implementation (API signing, the MQTT/HTTP coordinator, the device
  catalog, the bundled Lovelace cards). MIT licensed; see [`LICENSE`](LICENSE).
- **Local BLE protocol** — [rabits/ha-ef-ble](https://github.com/rabits/ha-ef-ble), built
  on the reverse engineering in
  [rabits/ef-ble-reverse](https://github.com/rabits/ef-ble-reverse), both Apache-2.0. A
  minimal subset (only the Wave 3 / River 3 import closure) is vendored at
  [`custom_components/ecoflow_iot/eflib/`](custom_components/ecoflow_iot/eflib/) with its
  own `LICENSE` and a `NOTICE` recording provenance and every modification.
- This fork adds the Home Assistant side of the Bluetooth transport — config flow, link
  supervisor and entity platforms — and the packaging.

## Disclaimer

Not affiliated with or endorsed by EcoFlow. Cloud device definitions are generated from
the official EcoFlow developer documentation (quota schemas + set-commands); some
per-firmware fields, units, or command parameters may still need on-device verification.

The Bluetooth protocol is **not** documented by EcoFlow — it is reverse-engineered
upstream, and coverage here is what the decoded telemetry and config messages carry. That
is not the same as everything the EcoFlow app can do: features the app drives through
paths this protocol is not known to expose are simply absent, and nothing here claims app
parity. The River 3 decoding is exercised against a real River 3 UPS frame capture; the
Wave 3 path is built from the vendored protocol definitions and covered by unit tests, and
has not yet been confirmed against a live unit.
