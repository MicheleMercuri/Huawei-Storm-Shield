# ╔══════════════════════════════════════════════════════════════╗
# ║              🛡️  STORM SHIELD  — v2.1                      ║
# ║    Battery protection + Off-peak night charging             ║
# ║    AppDaemon / Python                                       ║
# ╚══════════════════════════════════════════════════════════════╝
#
# License: MIT
#
# Designed for Huawei SUN2000 inverters with Luna2000 batteries,
# but adaptable to any inverter controllable via Home Assistant.
#
# Features:
#   - Automatic battery protection on severe weather alerts (DPC)
#   - Maintenance discharge (keeps inverter + basic loads alive)
#   - Protection mode after charging, with hysteresis (no ping-pong)
#   - Adaptive discharge driven by real PV production
#   - Blackout detection via grid voltage monitoring
#   - Off-peak night charging with weather-based SOC targets
#   - Telegram + Alexa notifications with unified DND
#   - Fully configurable via apps.yaml (no hardcoded entities)
#
# Changelog v2.3.1:
#   - FIX: a charge left on by a restart is really stopped (charge switch
#     + inverter), not only its flag. Before, if the alert ended while
#     AppDaemon was down, the forced charge stayed on
#   - FIX: a night charge left on resumes its monitor when still inside
#     the charge window, otherwise it is stopped
#
# Changelog v2.3:
#   - Maintenance discharge adjustable from the dashboard
#     (input_number.storm_shield_maintenance_discharge, default 2000W):
#     on a sudden blackout HA has no time to react, with 2000W the
#     house (and the HA server) stays powered
#   - Optional save/restore of battery SOC limits around forced charge
#
# Changelog v2.2:
#   - Protection state after charging with configurable hysteresis:
#     recharge only when SOC drops below (target - hysteresis)
#   - Real PV power vs forecast: discharge released while PV produces,
#     grid charge postponed when panels produce despite a bad forecast
#   - DPC level = highest of today/tomorrow; unavailable sensor handled
#   - Stale runtime flags reset at startup
#   - No forced charge when less than 500W of headroom is available

import appdaemon.plugins.hass.hassapi as hass
import urllib.request
import json as json_module
from datetime import datetime, timedelta, time


class StormShield(hass.Hass):

    VERSION = "2.3.1"

    # ═════════════════════════════════════════════════════════════
    # INIT
    # ═════════════════════════════════════════════════════════════

    def initialize(self):
        self.log("=" * 60)
        self.log(f"🛡️  STORM SHIELD v{self.VERSION} — Starting")
        self.log("=" * 60)

        # ─── Validate required config ───
        self._required_keys = [
            "sensor_soc",
            "sensor_grid",
            "discharge_power_entity",
            "charge_switch",
            "target_soc_entity",
            "charge_power_entity",
        ]
        missing = [k for k in self._required_keys if k not in self.args]
        if missing:
            self.log(f"❌ Missing required config: {', '.join(missing)}",
                     level="ERROR")
            self.log("   See apps.yaml.example for reference.", level="ERROR")
            return

        # ─── Telegram (optional) ───
        self.telegram_bot_token = self.args.get("telegram_bot_token", "")
        self.telegram_chat_id = self.args.get("telegram_chat_id", 0)
        if not self.telegram_bot_token:
            self.log("  ℹ️  Telegram: disabled (no bot_token)")

        # ─── Alexa (optional — list of notify services) ───
        self.alexa_notify_services = self.args.get(
            "alexa_notify_services", [])
        if isinstance(self.alexa_notify_services, str):
            self.alexa_notify_services = [self.alexa_notify_services]
        if not self.alexa_notify_services:
            self.log("  ℹ️  Alexa: disabled (no alexa_notify_services)")

        # ─── Sensors ───
        self.sensor_dpc = self.args.get("sensor_dpc", "sensor.dpc_alert")
        self.sensor_soc = self.args["sensor_soc"]
        self.sensor_grid = self.args["sensor_grid"]
        self.sensor_grid_voltage = self.args.get("sensor_grid_voltage", "")
        self.sensor_sunset = self.args.get("sensor_sunset", "")
        self.sensor_weather = self.args.get("sensor_weather", "")
        self.sensor_forecast = self.args.get("sensor_forecast", "")
        self.sensor_pv_power = self.args.get("sensor_pv_power", "")
        self.ev_charger = self.args.get("ev_charger", "")

        # ─── Blackout thresholds (V) ───
        self.voltage_blackout = float(self.args.get(
            "grid_voltage_blackout", 100))
        self.voltage_restore = float(self.args.get(
            "grid_voltage_restore", 200))

        # ─── Power thresholds (W) ───
        # discharge_maintenance is only a fallback: the dashboard helper
        # input_number.storm_shield_maintenance_discharge wins (v2.3)
        self.discharge_maintenance = int(self.args.get(
            "discharge_maintenance", 2000))
        self.discharge_blackout = int(self.args.get(
            "discharge_blackout", 5000))

        # ─── Battery control entities ───
        self.discharge_entity = self.args["discharge_power_entity"]
        self.charge_switch = self.args["charge_switch"]
        self.target_soc_entity = self.args["target_soc_entity"]
        self.charge_power_entity = self.args["charge_power_entity"]

        # ─── Inverter service (direct call, no HA automations needed) ───
        self.charge_service = self.args.get(
            "charge_service", "")
        self.stop_charge_service = self.args.get(
            "stop_charge_service", "")
        self.inverter_device_id = self.args.get(
            "inverter_device_id", "")

        # ─── Battery SOC limits (optional, v2.3) ───
        # Saved before a forced charge and restored after it, for
        # inverters that reset them when forced charging starts.
        self.backup_soc_entity = self.args.get("backup_soc_entity", "")
        self.end_of_discharge_soc_entity = self.args.get(
            "end_of_discharge_soc_entity", "")
        self._saved_backup_soc = None
        self._saved_eod_soc = None

        # ─── Storm Shield helper entity IDs ───
        pfx_bool = "input_boolean.storm_shield_"
        pfx_num = "input_number.storm_shield_"
        self.h_contract = f"{pfx_num}contract_power"
        self.h_margin = f"{pfx_num}safety_margin"
        self.h_max_charge = f"{pfx_num}max_charge_power"
        self.h_maintenance_discharge = f"{pfx_num}maintenance_discharge"
        self.h_discharge_restore = f"{pfx_num}discharge_restore"
        self.h_target_soc = f"{pfx_num}target_soc"
        self.h_hysteresis = f"{pfx_num}hysteresis"
        self.h_pv_threshold = f"{pfx_num}pv_threshold"
        self.h_active = f"{pfx_bool}active"
        self.h_manual = f"{pfx_bool}manual"
        self.h_bypass = f"{pfx_bool}bypass"
        self.h_charging = f"{pfx_bool}charging"
        self.h_protection = f"{pfx_bool}protection"
        self.h_dnd = f"{pfx_bool}dnd"
        self.h_notify_alexa = f"{pfx_bool}notify_alexa"
        self.h_notify_tg = f"{pfx_bool}notify_telegram"
        self.h_test_mode = f"{pfx_bool}test_mode"
        self.h_test_level = f"{pfx_num}test_level"
        self.h_last_action = "input_text.storm_shield_last_action"
        self.h_blackout = f"{pfx_bool}blackout"

        # ─── Night charging helpers ───
        self.h_f3_enabled = f"{pfx_bool}f3_enabled"
        self.h_f3_charging = f"{pfx_bool}f3_charging"
        self.h_f3_soc_sunny = f"{pfx_num}f3_soc_sunny"
        self.h_f3_soc_cloudy = f"{pfx_num}f3_soc_cloudy"

        # ─── Internal state ───
        self.charge_monitor_timer = None
        self.f3_monitor_timer = None
        self.protection_monitor_timer = None
        self._log_entries = []
        self._blackout_active = False
        self._pv_high_since = None  # since when real PV is above threshold

        # ─── Schedule: hourly alert check at :01 ───
        self.run_hourly(self._hourly_check, time(0, 1, 0))

        # ─── Schedule: Night charging start/stop ───
        f3_start = self._time_from("input_datetime.storm_shield_f3_start")
        f3_end = self._time_from("input_datetime.storm_shield_f3_end")
        if f3_start:
            self.run_daily(self._f3_start_cb, f3_start)
            self.log(f"  🌙 Night charge start: {f3_start.strftime('%H:%M')}")
        if f3_end:
            self.run_daily(self._f3_stop_cb, f3_end)
            self.log(f"  🌙 Night charge stop:  {f3_end.strftime('%H:%M')}")

        # ─── Clean up state left by a restart (HA restores the last
        #     state, but AppDaemon timers and monitors are lost) ───
        # An alert charge left on is really stopped (switch + inverter);
        # the initial check restarts it if the alert is still active.
        try:
            if self.get_state(self.h_charging) == "on":
                self.log("  🔄 Alert charge left on → stop (re-check in 10s)")
                self._stop_charging()
        except Exception as e:
            self.log(f"⚠️ Reset alert charge: {e}", level="WARNING")

        # A night charge left on resumes its monitor inside the window,
        # otherwise it is stopped.
        try:
            if self.get_state(self.h_f3_charging) == "on":
                if self._in_f3_window():
                    self.log("  🔄 Night charge in progress → monitor resumed")
                    self._cancel_f3_monitor()
                    self.f3_monitor_timer = self.run_every(
                        self._f3_monitor_cb,
                        self.datetime() + timedelta(seconds=60), 60)
                else:
                    self.log("  🔄 Night charge outside its window → stop")
                    self._f3_stop_charging()
        except Exception as e:
            self.log(f"⚠️ Reset night charge: {e}", level="WARNING")

        for entity in (self.h_protection, self.h_blackout):
            try:
                if self.get_state(entity) == "on":
                    self.call_service("input_boolean/turn_off",
                                      entity_id=entity)
                    self.log(f"  🔄 Reset: {entity} → off")
            except Exception:
                pass

        # ─── Listeners ───
        self.listen_state(self._on_manual_toggle, self.h_manual)
        self.listen_state(self._on_bypass_toggle, self.h_bypass)
        self.listen_state(self._on_test_toggle, self.h_test_mode)
        self.listen_state(self._on_soc_change, self.sensor_soc)
        if self.sensor_sunset:
            self.listen_state(self._on_sunset_change, self.sensor_sunset)
        if self.sensor_grid_voltage:
            self.listen_state(self._on_grid_voltage_change,
                              self.sensor_grid_voltage)

        # ─── Log config summary ───
        self._log_config()

        # ─── Initial check ───
        self.run_in(self._initial_check, 10)

        self._action(f"🛡️ Storm Shield v{self.VERSION} started")
        self.log(f"🛡️  Storm Shield v{self.VERSION} READY")
        self.log("=" * 60)

    def _log_config(self):
        """Print configuration summary on startup."""
        self.log(f"  📊 SOC sensor:     {self.sensor_soc}")
        self.log(f"  📊 Grid sensor:    {self.sensor_grid}")
        self.log(f"  📊 Grid voltage:   "
                 f"{self.sensor_grid_voltage or 'disabled'}")
        self.log(f"  📊 DPC sensor:     {self.sensor_dpc}")
        self.log(f"  📊 Sunset sensor:  "
                 f"{self.sensor_sunset or 'disabled'}")
        self.log(f"  📊 Weather:        "
                 f"{self.sensor_weather or 'disabled'}")
        self.log(f"  📊 PV power:       "
                 f"{self.sensor_pv_power or 'disabled'}")
        self.log(f"  📊 EV charger:     "
                 f"{self.ev_charger or 'disabled'}")
        self.log(f"  🔋 Discharge ctrl: {self.discharge_entity}")
        self.log(f"  🔋 Charge switch:  {self.charge_switch}")
        self.log(f"  ⚡ Maintenance:    {self._maintenance_w()}W")
        self.log(f"  ⚡ Blackout:       {self.discharge_blackout}W")
        self.log(f"  📢 Telegram:       "
                 f"{'enabled' if self.telegram_bot_token else 'disabled'}")
        self.log(f"  📢 Alexa devices:  "
                 f"{len(self.alexa_notify_services)}")
        if self.sensor_grid_voltage:
            self.log(f"  ⚡ Blackout < {self.voltage_blackout}V | "
                     f"Restore > {self.voltage_restore}V")
        if self.inverter_device_id:
            self.log(f"  🔌 Inverter:       direct ({self.charge_service})")
        else:
            self.log(f"  🔌 Inverter:       via HA automations (no device_id)")
        if self.backup_soc_entity or self.end_of_discharge_soc_entity:
            self.log("  💾 SOC limits:     saved/restored around forced charge")

    # ═════════════════════════════════════════════════════════════
    # LOGGING
    # ═════════════════════════════════════════════════════════════

    def _action(self, msg):
        """Log action: AppDaemon log + input_text + log sensor."""
        self.log(f"  📝 {msg}")
        ts = self.datetime().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"

        try:
            self.call_service("input_text/set_value",
                              entity_id=self.h_last_action,
                              value=msg[:250])
        except Exception:
            pass

        self._log_entries.append(entry)
        if len(self._log_entries) > 30:
            self._log_entries = self._log_entries[-30:]

        try:
            self.set_state("sensor.storm_shield_log",
                           state=msg[:240],
                           attributes={
                               "entries": list(self._log_entries),
                               "count": len(self._log_entries),
                               "last_update": ts,
                               "icon": "mdi:text-box-outline",
                               "friendly_name": "Storm Shield Log",
                           })
        except Exception:
            pass

    # ═════════════════════════════════════════════════════════════
    # UTILITY
    # ═════════════════════════════════════════════════════════════

    def _in_f3_window(self):
        """True if now is inside the night charge window."""
        start = self._time_from("input_datetime.storm_shield_f3_start")
        end = self._time_from("input_datetime.storm_shield_f3_end")
        if not start or not end:
            return False
        now_t = self.datetime().time()
        if start > end:  # window across midnight (e.g. 23:00-05:00)
            return now_t >= start or now_t < end
        return start <= now_t < end

    def _maintenance_w(self):
        """Maintenance discharge (W): dashboard helper, else apps.yaml."""
        return int(self._num(self.h_maintenance_discharge,
                             self.discharge_maintenance))

    def _pv_real(self):
        """Live PV production (W), 0 if no PV sensor is configured."""
        if not self.sensor_pv_power:
            return 0.0
        return self._num(self.sensor_pv_power, 0)

    def _num(self, entity_id, default=0):
        try:
            return float(self.get_state(entity_id))
        except (ValueError, TypeError):
            return default

    def _level_text(self, level):
        return {4: "🔴 RED ALERT", 3: "🟠 ORANGE ALERT",
                2: "🟡 YELLOW ALERT", 1: "🟢 GREEN ALERT"
                }.get(level, "✅ No alert")

    def _time_from(self, entity_id):
        try:
            val = self.get_state(entity_id)
            if val and ":" in val:
                p = val.split(":")
                return time(int(p[0]), int(p[1]), 0)
        except Exception as e:
            self.log(f"⚠️ {entity_id}: {e}", level="WARNING")
        return None

    # ═════════════════════════════════════════════════════════════
    # ALERT CHECK
    # ═════════════════════════════════════════════════════════════

    def _initial_check(self, kwargs):
        self._action("🔍 Initial check")
        self._do_check()

    def _hourly_check(self, kwargs):
        self._action("🔍 Hourly check")
        self._do_check()

    def _do_check(self):
        if self.get_state(self.h_bypass) == "on":
            is_active = self.get_state(self.h_active) == "on"
            is_manual = self.get_state(self.h_manual) == "on"
            if is_active and not is_manual:
                self._action("⏸️ Bypass ON — deactivating")
                self._deactivate()
            else:
                self._action("⏸️ Bypass ON — skip")
            return

        is_active = self.get_state(self.h_active) == "on"
        is_manual = self.get_state(self.h_manual) == "on"
        is_protection = self.get_state(self.h_protection) == "on"
        level, source, info = self._get_alert_level()
        soc = self._num(self.sensor_soc, 0)

        self._action(f"📊 DPC: lv.{level} ({source}: {info}) | "
                     f"SOC: {soc}% | Active: {is_active} | "
                     f"Protected: {is_protection}")

        is_critical = level in (3, 4)

        if is_critical:
            if not is_active and not is_manual:
                self._action(f"🛡️ ACTIVATING — {self._level_text(level)}")
                self._activate(level, source, info)
            elif is_active:
                self._check_charge_needed()
        else:
            if level not in (0, 1, 2, 3, 4):
                self._action(f"⚠️ Anomalous level: {level}")
            if is_active and not is_manual:
                self._action("✅ Alert cleared → deactivating")
                self._deactivate()

    # ═════════════════════════════════════════════════════════════
    # DPC ALERT READING
    # ═════════════════════════════════════════════════════════════

    def _get_alert_level(self):
        if self.get_state(self.h_test_mode) == "on":
            lv = int(max(0, min(4, self._num(self.h_test_level, 0))))
            return (lv, "test", f"Simulated level {lv}")
        dpc_state = self.get_state(self.sensor_dpc)
        if dpc_state in ("unavailable", "unknown", None):
            self.log(f"⚠️ DPC sensor: {dpc_state}", level="WARNING")
            return (0, "N/A", f"DPC sensor: {dpc_state}")

        # v2.2: use the highest level between today and tomorrow
        level_tomorrow, info_tomorrow = 0, "N/A"
        level_today, info_today = 0, "N/A"
        try:
            tomorrow = self.get_state(self.sensor_dpc,
                                      attribute="tomorrow")
            if (tomorrow and isinstance(tomorrow, dict)
                    and "level" in tomorrow):
                level_tomorrow = int(max(0, min(4, int(tomorrow["level"]))))
                info_tomorrow = tomorrow.get("info", "N/A")
            today = self.get_state(self.sensor_dpc, attribute="today")
            if (today and isinstance(today, dict)
                    and "level" in today):
                level_today = int(max(0, min(4, int(today["level"]))))
                info_today = today.get("info", "N/A")
        except Exception as e:
            self.log(f"⚠️ DPC: {e}", level="WARNING")
            return (0, "N/A", "DPC read error")

        if level_tomorrow >= level_today and level_tomorrow > 0:
            return (level_tomorrow, "tomorrow", info_tomorrow)
        if level_today > 0:
            return (level_today, "today", info_today)
        return (0, "N/A", "No data")

    def _get_alert_events(self):
        try:
            for attr in ("events_tomorrow", "events_today"):
                events = self.get_state(self.sensor_dpc, attribute=attr)
                if events and isinstance(events, list) and len(events) > 0:
                    return ", ".join(
                        f"{e.get('risk','?')}: {e.get('info','?')}"
                        for e in events)
        except Exception:
            pass
        return "N/A"

    def _get_alert_zone(self):
        try:
            for attr in ("tomorrow", "today"):
                data = self.get_state(self.sensor_dpc, attribute=attr)
                if data and isinstance(data, dict) and "zone_name" in data:
                    return data["zone_name"]
            return self.get_state(self.sensor_dpc,
                                  attribute="zone_name") or "N/A"
        except Exception:
            return "N/A"

    # ═════════════════════════════════════════════════════════════
    # ACTIVATION / DEACTIVATION
    # ═════════════════════════════════════════════════════════════

    def _activate(self, level, source, info):
        self.call_service("input_boolean/turn_on",
                          entity_id=self.h_active)
        self._set_discharge(self._maintenance_w())

        soc = self._num(self.sensor_soc, 0)
        target = self._num(self.h_target_soc, 100)
        events = self._get_alert_events()
        zone = self._get_alert_zone()

        msg = (f"🛡️ *STORM SHIELD ACTIVATED*\n\n"
               f"⚠️ {self._level_text(level)}\n"
               f"📋 {info}\n📍 {zone}\n🔍 {events}\n"
               f"📅 Source: {source}\n\n"
               f"🔋 Battery: {soc}%\n"
               f"🔒 Discharge: {self._maintenance_w()}W")

        if soc >= target:
            msg += f"\n✅ SOC already at target ({target:.0f}%)!"
            self._enter_protection(soc, target)
            self._notify(msg, f"Storm Shield activated. "
                         f"Battery at {soc:.0f} percent.")
        else:
            pv_ok, detail = self._evaluate_pv()
            if pv_ok:
                msg += f"\n☀️ PV: {detail}\n⏳ Waiting for sunset."
                self._notify(msg, "Storm Shield activated. "
                             "Waiting for sunset to charge.")
            else:
                msg += f"\n🌧️ PV: {detail}\n⚡ Grid charging!"
                self._notify(msg, "Storm Shield activated. "
                             "Starting grid charge.")
                self._start_charging()

    def _deactivate(self):
        if self.get_state(self.h_charging) == "on":
            self._stop_charging()
        restore = self._num(self.h_discharge_restore, 5000)
        self._set_discharge(restore)
        self._cancel_monitor()
        self._cancel_protection_monitor()

        if self.get_state(self.h_protection) == "on":
            self.call_service("input_boolean/turn_off",
                              entity_id=self.h_protection)
        self._pv_high_since = None

        if self._blackout_active:
            self._blackout_active = False
            try:
                self.call_service("input_boolean/turn_off",
                                  entity_id=self.h_blackout)
            except Exception:
                pass

        self.call_service("input_boolean/turn_off",
                          entity_id=self.h_active)
        self.call_service("input_boolean/turn_off",
                          entity_id=self.h_manual)

        soc = self._num(self.sensor_soc, 0)
        self._action(f"🔓 Deactivated — SOC {soc}% — "
                     f"discharge {restore:.0f}W")
        self._notify(f"🛡️ *STORM SHIELD DEACTIVATED*\n\n"
                     f"✅ No active alert.\n🔋 {soc}%\n"
                     f"🔓 Discharge: {restore:.0f}W",
                     f"Storm Shield deactivated. "
                     f"Battery at {soc:.0f} percent.")

    # ═════════════════════════════════════════════════════════════
    # PROTECTION MODE AFTER CHARGING (v2.2, anti ping-pong)
    # ═════════════════════════════════════════════════════════════

    def _enter_protection(self, soc, target):
        """Battery at target during an alert: hold it, adapt discharge."""
        self.call_service("input_boolean/turn_on",
                          entity_id=self.h_protection)
        self._pv_high_since = None
        recharge_at = self._recharge_threshold(target)
        self._action(f"🛡️ PROTECTION ON: SOC {soc}% (target {target:.0f}%) "
                     f"| recharge below {recharge_at:.0f}%")
        self._start_protection_monitor()

    def _exit_protection(self, reason):
        self.call_service("input_boolean/turn_off",
                          entity_id=self.h_protection)
        self._pv_high_since = None
        self._cancel_protection_monitor()
        self._action(f"🛡️ PROTECTION OFF: {reason}")

    def _recharge_threshold(self, target):
        """SOC below which a protected battery is charged again."""
        hysteresis = self._num(self.h_hysteresis, 15)
        return max(20, target - hysteresis)

    def _start_protection_monitor(self):
        """Every 5 minutes: check real PV and SOC."""
        self._cancel_protection_monitor()
        self.protection_monitor_timer = self.run_every(
            self._protection_monitor_cb,
            self.datetime() + timedelta(seconds=300), 300)

    def _cancel_protection_monitor(self):
        if self.protection_monitor_timer is not None:
            try:
                self.cancel_timer(self.protection_monitor_timer)
            except Exception:
                pass
            self.protection_monitor_timer = None

    def _protection_monitor_cb(self, kwargs):
        if self.get_state(self.h_protection) != "on":
            self._cancel_protection_monitor()
            return
        if self.get_state(self.h_active) != "on":
            self._exit_protection("Storm Shield no longer active")
            return
        self._update_protection_discharge()

    def _update_protection_discharge(self):
        """Adaptive discharge while protected, driven by real PV."""
        if self.get_state(self.h_protection) != "on":
            return
        if self._blackout_active:
            return

        pv_power = self._pv_real()
        pv_threshold = self._num(self.h_pv_threshold, 500)
        soc = self._num(self.sensor_soc, 0)
        target = self._num(self.h_target_soc, 100)
        recharge_at = self._recharge_threshold(target)
        restore = self._num(self.h_discharge_restore, 5000)

        import time as _time
        now_ts = _time.time()

        if self.sensor_pv_power and pv_power > pv_threshold:
            if self._pv_high_since is None:
                self._pv_high_since = now_ts
                self._action(f"☀️ Real PV {pv_power:.0f}W > "
                             f"{pv_threshold:.0f}W: watching")
            elif (now_ts - self._pv_high_since) >= 900:  # 15 minutes
                self._set_discharge(restore)
                self._action(f"☀️ Real PV {pv_power:.0f}W stable "
                             f"for 15 min → discharge {restore:.0f}W")
        else:
            if self._pv_high_since is not None:
                self._action(f"🌧️ Real PV {pv_power:.0f}W < "
                             f"{pv_threshold:.0f}W → maintenance")
            self._pv_high_since = None
            self._set_discharge(self._maintenance_w())

        if soc < recharge_at:
            self._exit_protection(
                f"SOC {soc}% < hysteresis threshold {recharge_at:.0f}%")
            pv_ok, detail = self._evaluate_pv()
            if not pv_ok and pv_power < pv_threshold:
                self._start_charging()

    # ═════════════════════════════════════════════════════════════
    # BLACKOUT DETECTION (grid voltage)
    # ═════════════════════════════════════════════════════════════

    def _on_grid_voltage_change(self, entity, attribute, old, new, kwargs):
        """Monitor grid voltage to detect blackout."""
        if self.get_state(self.h_active) != "on":
            return

        try:
            voltage = float(new)
        except (ValueError, TypeError):
            return

        if not self._blackout_active and voltage < self.voltage_blackout:
            self._blackout_active = True
            try:
                self.call_service("input_boolean/turn_on",
                                  entity_id=self.h_blackout)
            except Exception:
                pass
            self._set_discharge(self.discharge_blackout)
            soc = self._num(self.sensor_soc, 0)
            self._action(f"⚡ BLACKOUT! Voltage: {voltage:.0f}V → "
                         f"discharge {self.discharge_blackout}W")
            self._notify(
                f"🛡️ *STORM SHIELD — BLACKOUT!*\n\n"
                f"⚡ Grid voltage: *{voltage:.0f}V*\n"
                f"🔋 Battery: {soc}%\n"
                f"🔌 Discharge: {self.discharge_blackout}W\n"
                f"🏠 Running on battery!",
                f"Blackout detected. Grid voltage {voltage:.0f} volts. "
                f"Running on battery at full power. "
                f"Battery at {soc:.0f} percent.")

        elif self._blackout_active and voltage > self.voltage_restore:
            self._blackout_active = False
            try:
                self.call_service("input_boolean/turn_off",
                                  entity_id=self.h_blackout)
            except Exception:
                pass
            self._set_discharge(self._maintenance_w())
            soc = self._num(self.sensor_soc, 0)
            self._action(f"✅ Grid OK! Voltage: {voltage:.0f}V → "
                         f"discharge {self._maintenance_w()}W")
            self._notify(
                f"🛡️ *STORM SHIELD — GRID RESTORED*\n\n"
                f"✅ Grid voltage: *{voltage:.0f}V*\n"
                f"🔋 Battery: {soc}%\n"
                f"🔒 Discharge: {self._maintenance_w()}W",
                f"Grid restored. Voltage {voltage:.0f} volts. "
                f"Battery at {soc:.0f} percent.")

    # ═════════════════════════════════════════════════════════════
    # PV EVALUATION
    # ═════════════════════════════════════════════════════════════

    def _evaluate_pv(self):
        if not self.sensor_sunset or not self.sensor_weather:
            return (False, "no PV sensors configured")

        GOOD = ("sunny", "partlycloudy", "clear-night")
        import time as _time
        now_ts = _time.time()

        try:
            ss = self.get_state(self.sensor_sunset, attribute="today")
            if ss is None:
                ss = self.get_state(self.sensor_sunset)
            sunset_ts = self.convert_utc(ss).timestamp()
        except Exception as e:
            return (False, f"sunset error: {e}")

        h_left = (sunset_ts - now_ts) / 3600
        if h_left <= 1.5:
            return (False, f"{h_left:.1f}h to sunset")

        try:
            hourly = None
            if self.sensor_forecast:
                hourly = self.get_state(self.sensor_forecast,
                                        attribute="forecast_hourly")
            if not hourly or not isinstance(hourly, list):
                w = self.get_state(self.sensor_weather)
                return (w in GOOD, f"current weather: {w}")

            sun = tot = 0
            for h in hourly:
                try:
                    hdt = h.get("datetime", "")
                    h_ts = (self.convert_utc(hdt).timestamp()
                            if isinstance(hdt, str) else float(hdt))
                    if now_ts < h_ts < sunset_ts:
                        tot += 1
                        if h.get("condition", "") in GOOD:
                            sun += 1
                except Exception:
                    continue
            if tot == 0:
                return (False, "no forecast data")
            return (sun >= tot * 0.5, f"{sun}/{tot}h sun")
        except Exception as e:
            w = self.get_state(self.sensor_weather)
            return (w in GOOD, f"weather: {w} (err: {e})")

    def _evaluate_tomorrow_pv(self):
        """Evaluate PV for tomorrow (used by night charging)."""
        if not self.sensor_weather:
            return (False, "no weather sensor configured")

        GOOD = ("sunny", "partlycloudy", "clear-night")
        try:
            hourly = None
            if self.sensor_forecast:
                hourly = self.get_state(self.sensor_forecast,
                                        attribute="forecast_hourly")
            if not hourly or not isinstance(hourly, list):
                w = self.get_state(self.sensor_weather)
                return (w in GOOD, f"current weather: {w}")

            tomorrow = self.datetime().date() + timedelta(days=1)
            start_ts = datetime.combine(tomorrow, time(8, 0)).timestamp()
            end_ts = datetime.combine(tomorrow, time(18, 0)).timestamp()

            sun = tot = 0
            for h in hourly:
                try:
                    hdt = h.get("datetime", "")
                    h_ts = (self.convert_utc(hdt).timestamp()
                            if isinstance(hdt, str) else float(hdt))
                    if start_ts <= h_ts <= end_ts:
                        tot += 1
                        if h.get("condition", "") in GOOD:
                            sun += 1
                except Exception:
                    continue

            if tot == 0:
                w = self.get_state(self.sensor_weather)
                return (w in GOOD, f"no tomorrow forecast, weather: {w}")

            ok = sun >= tot * 0.5
            return (ok, f"{sun}/{tot}h sun tomorrow")
        except Exception as e:
            return (False, f"forecast error: {e}")

    # ═════════════════════════════════════════════════════════════
    # GRID CHARGING (weather alert)
    # ═════════════════════════════════════════════════════════════

    def _calc_charge_power(self):
        grid = self._num(self.sensor_grid, 0)
        contract = self._num(self.h_contract, 4500)
        margin = self._num(self.h_margin, 500)
        max_ch = self._num(self.h_max_charge, 3000)
        avail = contract - grid - margin
        if avail < 500:
            return 0  # not enough headroom: don't charge
        return int(min(avail, max_ch) / 100) * 100

    def _start_charging(self):
        soc = self._num(self.sensor_soc, 0)
        target = self._num(self.h_target_soc, 100)
        if soc >= target:
            self._action(f"✅ SOC {soc}% ≥ {target}% — no charge needed")
            return
        power = self._calc_charge_power()
        if power < 500:
            self._action(f"⚠️ Not enough power ({power}W): charge postponed")
            return
        self._action(f"⚡ Charging: {power}W ({soc}%→{target:.0f}%)")

        # Protection is suspended while charging
        if self.get_state(self.h_protection) == "on":
            self.call_service("input_boolean/turn_off",
                              entity_id=self.h_protection)
            self._cancel_protection_monitor()

        self.call_service("input_number/set_value",
                          entity_id=self.target_soc_entity, value=target)
        self.call_service("input_number/set_value",
                          entity_id=self.charge_power_entity, value=power)
        self.call_service("input_boolean/turn_on",
                          entity_id=self.charge_switch)
        self.call_service("input_boolean/turn_on",
                          entity_id=self.h_charging)
        self._save_soc_limits()
        self._inverter_charge(target, power)
        self._start_monitor()

        self._notify(f"🛡️ *STORM SHIELD*\n⚡ Charging STARTED\n"
                     f"🔋 {soc}% → {target:.0f}%\n💡 {power}W",
                     f"Grid charging started. Battery at {soc:.0f} percent, "
                     f"target {target:.0f} percent, power {power} watts.")

    def _stop_charging(self):
        self._action("🔌 Charging stopped")
        self._inverter_stop()
        self.run_in(self._delayed_restore_soc, 5)
        self.call_service("input_boolean/turn_off",
                          entity_id=self.charge_switch)
        self.call_service("input_boolean/turn_off",
                          entity_id=self.h_charging)
        self._cancel_monitor()

    def _check_charge_needed(self):
        """Alert still active: charge, protect, or wait for PV."""
        if self.get_state(self.h_charging) == "on":
            return
        soc = self._num(self.sensor_soc, 0)
        target = self._num(self.h_target_soc, 100)

        if self.get_state(self.h_protection) == "on":
            self._update_protection_discharge()
            return

        if soc >= target:
            self._enter_protection(soc, target)
            return

        pv_ok, detail = self._evaluate_pv()
        if not pv_ok:
            pv_real = self._pv_real()
            pv_thr = self._num(self.h_pv_threshold, 500)
            if self.sensor_pv_power and pv_real > pv_thr:
                self._action(f"☀️ Bad forecast ({detail}) but real PV "
                             f"{pv_real:.0f}W > {pv_thr:.0f}W: waiting")
                return
            self._action(f"⚡ No PV ({detail}), SOC {soc}%<{target:.0f}% "
                         f"→ grid charge")
            self._start_charging()

    # ─── Charge monitor (60s) ───

    def _start_monitor(self):
        self._cancel_monitor()
        self.charge_monitor_timer = self.run_every(
            self._monitor_cb,
            self.datetime() + timedelta(seconds=60), 60)

    def _cancel_monitor(self):
        if self.charge_monitor_timer is not None:
            try:
                self.cancel_timer(self.charge_monitor_timer)
            except Exception:
                pass
            self.charge_monitor_timer = None

    def _monitor_cb(self, kwargs):
        if self.get_state(self.h_charging) != "on":
            self._cancel_monitor()
            return
        soc = self._num(self.sensor_soc, 0)
        target = self._num(self.h_target_soc, 100)
        if soc >= target:
            self._action(f"✅ Charge complete: {soc}%")
            self._stop_charging()
            level, _, _ = self._get_alert_level()
            protect = level in (3, 4)
            if protect:
                self._enter_protection(soc, target)
            self._notify(f"🛡️ *STORM SHIELD*\n"
                         f"✅ Battery *{soc:.0f}%*!\n"
                         f"🔒 Discharge: {self._maintenance_w()}W"
                         + ("\n🛡️ Protection active" if protect else ""),
                         f"Charge complete. Battery at {soc:.0f} percent.")
            return
        new = self._calc_charge_power()
        cur = self._num(self.charge_power_entity, 0)
        if abs(new - cur) > 300:
            self._action(f"📊 Power: {cur:.0f}→{new}W")
            self.call_service("input_number/set_value",
                              entity_id=self.charge_power_entity, value=new)
            self._inverter_update_power(new)

    # ═════════════════════════════════════════════════════════════
    # NIGHT CHARGING (off-peak tariff)
    # ═════════════════════════════════════════════════════════════

    def _f3_start_cb(self, kwargs):
        if self.get_state(self.h_f3_enabled) != "on":
            self._action("🌙 Night charge disabled — skip")
            return
        if self.get_state(self.h_active) == "on":
            self._action("🌙 Night charge skip — Storm Shield active")
            self._notify(
                "🌙 *NIGHT CHARGE*\n⏭️ Skip — Storm Shield active",
                "Night charge skipped. Storm Shield active.")
            return
        self._action("🌙 Night charge — evaluating")
        self._f3_evaluate()

    def _f3_evaluate(self):
        soc = self._num(self.sensor_soc, 0)

        if self.ev_charger:
            ev_state = self.get_state(self.ev_charger)
            if ev_state == "on":
                self._action("🌙 Night charge skip — EV charging")
                self._notify(
                    "🌙 *NIGHT CHARGE*\n⏭️ Skip — EV charging",
                    "Night charge skipped. EV charging.")
                return

        pv_ok, detail = self._evaluate_tomorrow_pv()
        self._action(f"🌙 Tomorrow: "
                     f"{'☀️ sunny' if pv_ok else '🌧️ cloudy'} ({detail})")

        soc_sunny = self._num(self.h_f3_soc_sunny, 30)
        soc_cloudy = self._num(self.h_f3_soc_cloudy, 60)

        if pv_ok:
            if soc < soc_sunny:
                target = soc_sunny
            else:
                self._action(f"🌙 Night charge skip — SOC {soc}% ≥ "
                             f"{soc_sunny:.0f}% + sun tomorrow")
                self._notify(
                    f"🌙 *NIGHT CHARGE*\n⏭️ Skip\n"
                    f"🔋 SOC {soc}% ≥ {soc_sunny:.0f}% + ☀️",
                    f"Night charge not needed. "
                    f"Battery at {soc:.0f} percent and sun tomorrow.")
                return
        else:
            if soc < soc_cloudy:
                target = soc_cloudy
            else:
                self._action(f"🌙 Night charge skip — SOC {soc}% ≥ "
                             f"{soc_cloudy:.0f}% OK for cloudy")
                self._notify(
                    f"🌙 *NIGHT CHARGE*\n⏭️ Skip\n"
                    f"🔋 SOC {soc}% ≥ {soc_cloudy:.0f}% + 🌧️",
                    f"Night charge not needed. "
                    f"Battery at {soc:.0f} percent sufficient.")
                return

        power = self._calc_charge_power()
        if power < 500:
            self._action(f"🌙 Night charge skip — "
                         f"insufficient power ({power}W)")
            return

        if target <= 0:
            self._action("🌙 Night charge skip: target SOC = 0%")
            return

        self._action(f"🌙 Night charge: SOC {soc}% → {target:.0f}%")
        self._f3_start_charging(target, power)

    def _f3_start_charging(self, target, power):
        soc = self._num(self.sensor_soc, 0)
        self.call_service("input_number/set_value",
                          entity_id=self.target_soc_entity, value=target)
        self.call_service("input_number/set_value",
                          entity_id=self.charge_power_entity, value=power)
        self.call_service("input_boolean/turn_on",
                          entity_id=self.charge_switch)
        self.call_service("input_boolean/turn_on",
                          entity_id=self.h_f3_charging)
        self._save_soc_limits()
        self._inverter_charge(target, power)

        self._cancel_f3_monitor()
        self.f3_monitor_timer = self.run_every(
            self._f3_monitor_cb,
            self.datetime() + timedelta(seconds=60), 60)

        self._notify(f"🌙 *NIGHT CHARGE STARTED*\n"
                     f"🔋 {soc}% → {target:.0f}%\n💡 {power}W",
                     f"Night charge started. Battery at {soc:.0f} percent, "
                     f"target {target:.0f} percent.")

    def _f3_monitor_cb(self, kwargs):
        if self.get_state(self.h_f3_charging) != "on":
            self._cancel_f3_monitor()
            return
        soc = self._num(self.sensor_soc, 0)
        target = self._num(self.target_soc_entity, 30)
        if soc >= target:
            self._action(f"🌙 Night charge complete: SOC {soc}%")
            self._f3_stop_charging()
            self._notify(f"🌙 *NIGHT CHARGE COMPLETE*\n🔋 {soc}%",
                         f"Night charge complete. "
                         f"Battery at {soc:.0f} percent.")
            return
        new = self._calc_charge_power()
        cur = self._num(self.charge_power_entity, 0)
        if abs(new - cur) > 300:
            self._action(f"🌙 Power: {cur:.0f}→{new}W")
            self.call_service("input_number/set_value",
                              entity_id=self.charge_power_entity, value=new)
            self._inverter_update_power(new)

    def _f3_stop_cb(self, kwargs):
        if self.get_state(self.h_f3_charging) == "on":
            soc = self._num(self.sensor_soc, 0)
            self._action(f"🌙 Night charge window ended — SOC {soc}%")
            self._f3_stop_charging()
            self._notify(f"🌙 *NIGHT CHARGE — WINDOW ENDED*\n🔋 {soc}%",
                         f"Night charge window ended. "
                         f"Battery at {soc:.0f} percent.")
        else:
            self._action("🌙 Night charge window ended — was not charging")

    def _f3_stop_charging(self):
        self._inverter_stop()
        self.run_in(self._delayed_restore_soc, 5)
        self.call_service("input_boolean/turn_off",
                          entity_id=self.charge_switch)
        self.call_service("input_boolean/turn_off",
                          entity_id=self.h_f3_charging)
        self._cancel_f3_monitor()

    def _cancel_f3_monitor(self):
        if self.f3_monitor_timer is not None:
            try:
                self.cancel_timer(self.f3_monitor_timer)
            except Exception:
                pass
            self.f3_monitor_timer = None

    # ═════════════════════════════════════════════════════════════
    # CALLBACKS
    # ═════════════════════════════════════════════════════════════

    def _on_soc_change(self, entity, attribute, old, new, kwargs):
        try:
            soc = float(new)
        except (ValueError, TypeError):
            return

        if self.get_state(self.h_charging) == "on":
            target = self._num(self.h_target_soc, 100)
            if soc >= target:
                self._action(f"✅ SOC {soc}% = alert target!")
                self._stop_charging()
                level, _, _ = self._get_alert_level()
                protect = level in (3, 4)
                if protect:
                    self._enter_protection(soc, target)
                self._notify(
                    f"🛡️ *STORM SHIELD*\n✅ {soc:.0f}%!\n"
                    f"🔒 Discharge: {self._maintenance_w()}W"
                    + ("\n🛡️ Protection active" if protect else ""),
                    f"Target reached. Battery at {soc:.0f} percent.")

        if self.get_state(self.h_f3_charging) == "on":
            target = self._num(self.target_soc_entity, 30)
            if soc >= target:
                self._action(f"🌙 Night charge SOC {soc}% = target!")
                self._f3_stop_charging()
                self._notify(
                    f"🌙 *NIGHT CHARGE COMPLETE*\n🔋 {soc:.0f}%",
                    f"Night charge complete. "
                    f"Battery at {soc:.0f} percent.")

    def _on_sunset_change(self, entity, attribute, old, new, kwargs):
        import time as _time
        try:
            ss = self.get_state(self.sensor_sunset, attribute="today")
            if ss is None:
                ss = self.get_state(self.sensor_sunset)
            sunset_ts = self.convert_utc(ss).timestamp()
            now_ts = _time.time()
            if abs(sunset_ts - now_ts) / 60 > 20:
                return
        except Exception:
            return
        if self.get_state(self.h_active) != "on":
            return
        if self.get_state(self.h_charging) == "on":
            return
        if self.get_state(self.h_protection) == "on":
            return  # already charged and protected
        soc = self._num(self.sensor_soc, 0)
        target = self._num(self.h_target_soc, 100)
        if soc < target:
            self._action(f"🌅 Sunset: SOC {soc}%<{target:.0f}% → charging")
            self._start_charging()

    def _on_bypass_toggle(self, entity, attribute, old, new, kwargs):
        if new == "on":
            self._action("⏸️ Bypass ACTIVATED")
            is_active = self.get_state(self.h_active) == "on"
            is_manual = self.get_state(self.h_manual) == "on"
            if is_active and not is_manual:
                self._action("⏸️ Bypass → deactivating auto-protection")
                self._deactivate()
            elif is_active and is_manual:
                self._notify(
                    "⏸️ *BYPASS ACTIVATED*\n"
                    "🔒 Manual mode still active",
                    "Bypass activated. Manual mode still active.")
            else:
                self._notify(
                    "⏸️ *BYPASS ACTIVATED*\n📡 Alerts ignored",
                    "Bypass activated. Alerts ignored.")
        elif new == "off":
            self._action("▶️ Bypass DEACTIVATED")
            self._notify(
                "▶️ *BYPASS DEACTIVATED*\n📡 Alert monitoring restored",
                "Bypass deactivated. Alert monitoring restored.")
            self.run_in(lambda kwargs: self._do_check(), 2)

    def _on_manual_toggle(self, entity, attribute, old, new, kwargs):
        if new == "on":
            self._action("🔒 MANUAL activation")
            self.call_service("input_boolean/turn_on",
                              entity_id=self.h_active)
            self._set_discharge(self._maintenance_w())
            soc = self._num(self.sensor_soc, 0)
            self._notify(
                f"🛡️ *MANUAL ACTIVATED*\n"
                f"🔋 {soc}%\n"
                f"🔒 Discharge: {self._maintenance_w()}W",
                f"Storm Shield manual activated. "
                f"Battery at {soc:.0f} percent.")
            target = self._num(self.h_target_soc, 100)
            if soc < target:
                pv_ok, _ = self._evaluate_pv()
                if not pv_ok:
                    self._start_charging()
        elif new == "off":
            self._action("🔓 MANUAL deactivation")
            self._deactivate()

    def _on_test_toggle(self, entity, attribute, old, new, kwargs):
        if new == "on":
            lv = self._num(self.h_test_level, 3)
            self._action(f"🧪 TEST ON — level {lv}")
            self._notify(f"🧪 *TEST ACTIVATED*\nSimulated level: {lv:.0f}",
                         f"Storm Shield test activated. Level {lv:.0f}.")
            self._do_check()
        elif new == "off":
            self._action("🧪 TEST OFF — restoring")
            self._deactivate()
            self._notify("🧪 *TEST COMPLETED*",
                         "Storm Shield test completed.")

    # ═════════════════════════════════════════════════════════════
    # BATTERY CONTROL
    # ═════════════════════════════════════════════════════════════

    def _set_discharge(self, value):
        try:
            self.call_service("number/set_value",
                              entity_id=self.discharge_entity, value=value)
        except Exception as e:
            self.log(f"⚠️ Set discharge: {e}", level="WARNING")

    def _inverter_charge(self, target_soc, power):
        """Call inverter service directly for forced charge."""
        if not self.inverter_device_id or not self.charge_service:
            return
        try:
            self.call_service(self.charge_service,
                              device_id=self.inverter_device_id,
                              target_soc=int(target_soc),
                              power=int(power))
            self.log(f"  🔌 Inverter: forced charge "
                     f"SOC={int(target_soc)}% P={int(power)}W")
        except Exception as e:
            self.log(f"⚠️ Inverter charge: {e}", level="WARNING")

    def _inverter_update_power(self, power):
        """Update charge power on the inverter."""
        if not self.inverter_device_id or not self.charge_service:
            return
        try:
            target = self._num(self.target_soc_entity, 100)
            self.call_service(self.charge_service,
                              device_id=self.inverter_device_id,
                              target_soc=int(target),
                              power=int(power))
        except Exception as e:
            self.log(f"⚠️ Inverter update: {e}", level="WARNING")

    def _inverter_stop(self):
        """Stop forced charge on the inverter."""
        if not self.inverter_device_id or not self.stop_charge_service:
            return
        try:
            self.call_service(self.stop_charge_service,
                              device_id=self.inverter_device_id)
            self.log("  🔌 Inverter: stop forced charge")
        except Exception as e:
            self.log(f"⚠️ Inverter stop: {e}", level="WARNING")

    # ─── Battery SOC limits (optional, v2.3) ───

    def _save_soc_limits(self):
        """Save battery SOC limits before a forced charge."""
        if (self._saved_backup_soc is not None
                or self._saved_eod_soc is not None):
            return  # already saved: don't overwrite with post-reset values
        try:
            if self.backup_soc_entity:
                backup = self._num(self.backup_soc_entity, None)
                if backup is not None and backup > 0:
                    self._saved_backup_soc = backup
            if self.end_of_discharge_soc_entity:
                eod = self._num(self.end_of_discharge_soc_entity, None)
                if eod is not None and eod > 0:
                    self._saved_eod_soc = eod
            if self._saved_backup_soc or self._saved_eod_soc:
                self.log(f"  💾 SOC limits saved: "
                         f"backup={self._saved_backup_soc}% "
                         f"eod={self._saved_eod_soc}%")
        except Exception as e:
            self.log(f"⚠️ Save SOC limits: {e}", level="WARNING")

    def _delayed_restore_soc(self, kwargs):
        self._restore_soc_limits()

    def _restore_soc_limits(self):
        """Restore battery SOC limits after a forced charge."""
        try:
            restored = False
            if self._saved_backup_soc is not None:
                self.call_service("number/set_value",
                                  entity_id=self.backup_soc_entity,
                                  value=self._saved_backup_soc)
                restored = True
            if self._saved_eod_soc is not None:
                self.call_service("number/set_value",
                                  entity_id=self.end_of_discharge_soc_entity,
                                  value=self._saved_eod_soc)
                restored = True
            if restored:
                self._action(f"💾 SOC limits restored: "
                             f"backup={self._saved_backup_soc}% "
                             f"eod={self._saved_eod_soc}%")
            self._saved_backup_soc = None
            self._saved_eod_soc = None
        except Exception as e:
            self.log(f"⚠️ Restore SOC limits: {e}", level="WARNING")

    # ═════════════════════════════════════════════════════════════
    # NOTIFICATIONS (unified v2.1)
    # ═════════════════════════════════════════════════════════════

    def _notify(self, tg_message, alexa_message=None):
        """Send notification to Telegram and Alexa (with unified DND)."""
        self._send_tg(tg_message)
        if alexa_message:
            self._send_alexa(alexa_message)

    def _send_tg(self, message):
        if self.get_state(self.h_notify_tg) != "on":
            return
        if self._is_dnd():
            return
        if not self.telegram_bot_token:
            return
        try:
            url = (f"https://api.telegram.org/"
                   f"bot{self.telegram_bot_token}/sendMessage")
            payload = json_module.dumps({
                "chat_id": self.telegram_chat_id,
                "text": message, "parse_mode": "Markdown",
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            self.log(f"⚠️ TG: {e}", level="WARNING")

    def _send_alexa(self, message):
        if self.get_state(self.h_notify_alexa) != "on":
            return
        if self._is_dnd():
            return
        if not self.alexa_notify_services:
            return
        for i, service in enumerate(self.alexa_notify_services):
            delay = i * 3
            if delay == 0:
                self._alexa_announce(service, message)
            else:
                self.run_in(self._alexa_delayed, delay,
                            service=service, message=message)

    def _alexa_announce(self, service, message):
        try:
            self.call_service(service,
                              message=message, data={"type": "announce"})
        except Exception as e:
            self.log(f"⚠️ Alexa ({service}): {e}", level="WARNING")

    def _alexa_delayed(self, kwargs):
        self._alexa_announce(kwargs["service"], kwargs["message"])

    def _is_dnd(self):
        """Unified DND — applies to Telegram and Alexa."""
        if self.get_state(self.h_dnd) == "on":
            return True
        try:
            s = self.get_state("input_datetime.storm_shield_dnd_start")
            e = self.get_state("input_datetime.storm_shield_dnd_end")
            if not s or not e:
                return False
            now_t = self.datetime().time()
            ds = datetime.strptime(s, "%H:%M:%S").time()
            de = datetime.strptime(e, "%H:%M:%S").time()
            if ds > de:
                return now_t >= ds or now_t < de
            return ds <= now_t < de
        except Exception:
            return False
