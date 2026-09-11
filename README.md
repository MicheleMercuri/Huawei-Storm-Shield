# 🛡️ Huawei Storm Shield

**Automatic battery protection for Home Assistant + AppDaemon**

Storm Shield monitors Italian Civil Protection (DPC) weather alerts and automatically protects your battery storage system during severe weather events. Designed for **Huawei SUN2000 inverters with Luna2000 batteries**, but adaptable to any inverter controllable via Home Assistant.

![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.1+-blue?logo=home-assistant)
![AppDaemon](https://img.shields.io/badge/AppDaemon-4.4+-green)
![License](https://img.shields.io/badge/License-MIT-yellow)
![Version](https://img.shields.io/badge/version-2.3.0-informational)

---
<img width="1331" height="911" alt="Immagine 2026-02-13 120020" src="https://github.com/user-attachments/assets/b82fe22d-5400-401a-9368-deacce67f1c8" />

## Features

- **🛡️ Weather Alert Protection**: activates automatically on DPC orange/red alerts (level 3-4, the highest between today and tomorrow) and limits battery discharge to a maintenance level to preserve charge for emergencies. The maintenance level is set from the dashboard (default 2000W, see below why).
- **🔒 Protection mode** *(v2.2)*: once the battery reaches the target during an alert, Storm Shield holds it there. It charges again only if SOC drops below *target − hysteresis*, so there is no charge/discharge ping-pong.
- **☀️ Real PV override** *(v2.2)*: while protected, if the panels produce more than a threshold for 15 minutes the discharge is released; if the forecast says "no sun" but the panels are producing, grid charging is postponed.
- **⚡ Blackout Detection**: monitors grid voltage; if power goes out, switches to full discharge (5000W) to keep your home running on battery
- **🌙 Off-Peak Night Charging**: charges the battery during cheap tariff hours (F3 in Italy, 23:00-05:00), with weather-based SOC targets (lower target if tomorrow is sunny, higher if cloudy)
- **☀️ Smart PV Evaluation**: checks the solar forecast before deciding to charge from grid; waits for sunset if enough sun is expected
- **💾 SOC limits protection** *(v2.3, optional)*: some inverters reset backup / end-of-discharge SOC when a forced charge starts. Storm Shield saves them before charging and restores them afterwards.
- **📢 Notifications**: Telegram + Alexa announcements on every state change, with unified Do Not Disturb schedule
- **🔒 Manual & Bypass Modes**: override the automation when needed
- **🧪 Test Mode**: simulate any alert level without real DPC data

### Why 2000W of maintenance discharge?

On a sudden blackout Home Assistant has no time to react before the house switches to battery. With the old 500W limit the inverter could not power the house (and the HA server itself), so the blackout detection never got the chance to raise the discharge. With 2000W the essential loads stay on until Storm Shield switches to full power. You can lower it from the dashboard if your base load is smaller.

## How It Works

```
DPC Alert Level 3-4 detected (highest of today / tomorrow)
        │
        ▼
┌─────────────────┐
│  ACTIVATE SHIELD │ → Discharge limited to maintenance (default 2000W)
└────────┬────────┘
         │
    Is SOC < target?
    ┌────┴────┐
   YES       NO → PROTECTED
    │
    ▼
 Sun expected, or panels producing right now?
 ┌────┴────┐
YES       NO → Grid charging (dynamic power) → target reached → PROTECTED
 │
 ▼
Wait for sunset → Then charge from grid
```

**While protected** (checked every 5 minutes):
```
Real PV > threshold for 15 min  → discharge released (restore value)
Real PV below threshold         → maintenance discharge
SOC < target − hysteresis       → leave protection, charge again
```

**During blackout** (grid voltage drops to 0V):
```
Grid voltage < 100V → BLACKOUT → Discharge 5000W (full inverter power)
Grid voltage > 200V → RESTORED → Back to maintenance discharge
```

## Requirements

- **Home Assistant** 2024.1 or later
- **AppDaemon** 4.4 or later (installed as HA add-on or standalone)
- **Huawei Battery Luna2000 + inverter Sun2000** controllable via HA (tested with Huawei SUN2000 + Luna2000) integrated by [Huawei Solar Integration](https://github.com/wlcrs/huawei_solar)
- **DPC Alert sensor**: [DPC Alert custom component](https://github.com/caiosweet/Home-Assistant-custom-components-DPC-Alert) (for Italian weather alerts)

### Optional

- **Grid voltage sensor**: from your energy meter (for blackout detection)
- **Weather/forecast sensor**: for smart PV evaluation
- **PV power sensor**: live production in W (for the real PV override)
- **Telegram bot**: for push notifications
- **Alexa Media Player**: for voice announcements
- **EV charger switch**: to avoid grid overload during night charging

https://github.com/user-attachments/assets/b0308c6a-4fe3-4a40-8a01-0abf67e5f429

## Installation

### 1. Copy the HA Package

Copy `packages/storm_shield.yaml` into your Home Assistant packages folder:

```
config/
└── packages/
    └── storm_shield.yaml
```

Search the file for `YOUR_` and replace the placeholders with your sensors (sunset, forecast, weather, PV power).

Make sure packages are enabled in `configuration.yaml`:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Restart Home Assistant to create all the helper entities.

### 2. Copy the AppDaemon App

Copy `storm_shield.py` into your AppDaemon apps folder:

```
appdaemon/
└── apps/
    └── storm_shield.py
```

### 3. Configure

Copy `apps.yaml.example` to your AppDaemon apps folder and rename it:

```bash
cp apps.yaml.example /path/to/appdaemon/apps/storm_shield.yaml
```

Edit the file and fill in **your** entity IDs. At minimum you need:

```yaml
storm_shield:
  module: storm_shield
  class: StormShield

  # REQUIRED — your battery sensors
  sensor_soc: "sensor.battery_state_of_capacity"
  sensor_grid: "sensor.power_grid_kwp"

  # REQUIRED — your battery control entities
  discharge_power_entity: "number.batteries_max_discharge_power"
  charge_switch: "input_boolean.forcible_charge_switch"
  target_soc_entity: "input_number.target_soc_slider"
  charge_power_entity: "input_number.power_slider"
```

See `apps.yaml.example` for all optional settings.

### 4. Dashboard (Optional)

Copy `dashboard/storm_shield_dashboard.yaml` into your Lovelace config. The PV cards use `sensor.input_power` (Huawei Solar default): replace it with your PV power sensor if different. The dashboard requires:
- [Mushroom cards](https://github.com/piitaya/lovelace-mushroom)
- [card-mod](https://github.com/thomasloven/lovelace-card-mod)

### Upgrading from 2.1

1. Replace `packages/storm_shield.yaml` (new helpers: `storm_shield_protection`, `storm_shield_maintenance_discharge`, `storm_shield_hysteresis`, `storm_shield_pv_threshold`, template sensor `Storm Shield PV Real`), set `YOUR_PV_POWER_SENSOR`, restart HA.
2. Replace `storm_shield.py`.
3. Optional: add `sensor_pv_power`, `backup_soc_entity` and `end_of_discharge_soc_entity` to your AppDaemon config.
4. Optional: update the dashboard.

The maintenance discharge now comes from the dashboard helper (default 2000W). `discharge_maintenance` in `apps.yaml` is only used when the helper doesn't exist.

## Configuration Reference

### Required Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| `sensor_soc` | Battery State of Charge (0-100%) | `sensor.battery_state_of_capacity` |
| `sensor_grid` | Grid power consumption (W) | `sensor.power_grid_kwp` |
| `discharge_power_entity` | Max discharge power control | `number.batteries_max_discharge_power` |
| `charge_switch` | Forced charge enable/disable | `input_boolean.forcible_charge_switch` |
| `target_soc_entity` | Target SOC for forced charge | `input_number.target_soc_slider` |
| `charge_power_entity` | Charge power for forced charge | `input_number.power_slider` |

### Optional Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `sensor_dpc` | `sensor.dpc_alert` | DPC weather alert sensor |
| `sensor_grid_voltage` | *(disabled)* | Grid voltage for blackout detection |
| `sensor_sunset` | *(disabled)* | Sunset time sensor (for PV evaluation) |
| `sensor_weather` | *(disabled)* | Current weather entity |
| `sensor_forecast` | *(disabled)* | Forecast sensor with `forecast_hourly` attribute |
| `sensor_pv_power` | *(disabled)* | Live PV production (W) for the real PV override |
| `ev_charger` | *(disabled)* | EV charger switch (skips night charge if ON) |
| `telegram_bot_token` | *(disabled)* | Telegram bot token from @BotFather |
| `telegram_chat_id` | *(disabled)* | Your Telegram chat ID |
| `alexa_notify_services` | *(disabled)* | List of Alexa notify service names |
| `charge_service` / `stop_charge_service` | *(disabled)* | Inverter services for direct forced charge |
| `inverter_device_id` | *(disabled)* | Device id of your inverter (needed by the services above) |
| `backup_soc_entity` | *(disabled)* | Backup SOC setting to save/restore around forced charge |
| `end_of_discharge_soc_entity` | *(disabled)* | End-of-discharge SOC setting to save/restore |
| `grid_voltage_blackout` | `100` | Voltage threshold for blackout (V) |
| `grid_voltage_restore` | `200` | Voltage threshold for grid restored (V) |
| `discharge_maintenance` | `2000` | Fallback maintenance discharge (W), used only if the dashboard helper is missing |
| `discharge_blackout` | `5000` | Discharge power during blackout (W) |

### Dashboard Controls

All parameters below are adjustable from the HA dashboard at runtime:

| Helper | Default | Description |
|--------|---------|-------------|
| `storm_shield_contract_power` | 4500W | Your grid contract power |
| `storm_shield_safety_margin` | 500W | Safety margin for charge power calculation |
| `storm_shield_max_charge_power` | 3000W | Maximum charge power (battery limit) |
| `storm_shield_maintenance_discharge` | 2000W | Discharge limit during an alert |
| `storm_shield_discharge_restore` | 5000W | Discharge power after deactivation (and with strong real PV) |
| `storm_shield_target_soc` | 100% | Target SOC during weather alert |
| `storm_shield_hysteresis` | 15% | Protected battery is recharged below target − hysteresis |
| `storm_shield_pv_threshold` | 500W | Real PV level that releases the discharge |
| `storm_shield_f3_soc_sunny` | 30% | Night charge target if tomorrow is sunny |
| `storm_shield_f3_soc_cloudy` | 60% | Night charge target if tomorrow is cloudy |
| `storm_shield_dnd_start` | 22:00 | Do Not Disturb start time |
| `storm_shield_dnd_end` | 07:30 | Do Not Disturb end time |
| `storm_shield_f3_start` | 23:00 | Night charge window start |
| `storm_shield_f3_end` | 05:00 | Night charge window end |

## Adapting to Your Setup

### Different Inverter Brand

Storm Shield controls the battery via generic HA services (`number/set_value`, `input_boolean/turn_on`, etc.). If your inverter exposes similar controls in HA, it should work. You need:

1. A **sensor** that reports battery SOC (0-100%)
2. A **number/input_number** entity to set max discharge power
3. A way to **enable forced charging** (boolean or switch)
4. A way to set **charge target SOC** and **charge power**

### Different Alert System

The DPC sensor is specific to Italy. To use a different alert source, you can either:
- Create a template sensor that exposes `today.level` and `tomorrow.level` attributes (0-4 scale)
- Or use **Manual mode** + automations to trigger Storm Shield based on your own alert source

### No Solar Panels

If you don't have PV, simply omit `sensor_sunset`, `sensor_weather`, `sensor_forecast` and `sensor_pv_power`. Storm Shield will always charge from grid immediately when an alert is detected.

## File Structure

```
storm-shield/
├── storm_shield.py              # AppDaemon app (main logic)
├── apps.yaml.example            # Configuration template
├── packages/
│   └── storm_shield.yaml        # HA package (helpers + template sensors)
├── dashboard/
│   └── storm_shield_dashboard.yaml  # Lovelace dashboard
├── .gitignore
├── LICENSE
└── README.md
```

## Changelog

### 2.3.0
- Maintenance discharge adjustable from the dashboard (`storm_shield_maintenance_discharge`, default 2000W).
- Optional save/restore of battery SOC limits around forced charge.

### 2.2.0
- Protection state after charging with configurable hysteresis (no ping-pong).
- Real PV power vs forecast, adaptive discharge while protected.
- DPC level = highest between today and tomorrow; unavailable DPC sensor handled.
- Stale runtime flags reset at startup.
- No forced charge when less than 500W of headroom is available.

### 2.1.0
- First public release.

## License

MIT — see [LICENSE](LICENSE) for details.

## Credits

Built for the Italian energy ecosystem (time-of-use tariffs F1/F2/F3, DPC civil protection alerts, Huawei SUN2000 inverters).
