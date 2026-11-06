# Deye Export Manager (Home Assistant Add-on)

Custom Home Assistant add-on for managing a **24-hour export-power schedule**, viewing/editing **Deye inverter Time of Use (TOU) programs**, and adjusting selected **Deye inverter configuration** settings. It uses the Supervisor/Home Assistant API and provides an **ingress UI** in the Home Assistant sidebar with **Export**, **Time of Use**, and **Configuration** tabs.

## Required Home Assistant automations: **NONE**

**You do not need to create any Home Assistant automations for this add-on to work.**

- **Export tab (schedule):** core scheduling and applying export power are handled by the add-on itself (checks every minute, plus **Apply schedule to inverter now**), as long as the add-on is running, has Home Assistant API access (`homeassistant_api` / Supervisor token), and `deye_entity` is correct/controllable.
- **Time of Use tab (TOU programs):** values are **pulled live from Home Assistant/inverter on every page load** and changes are written only when you click **Save & sync** on a row and **confirm** the old → new change (or confirm the unsaved-leave prompt). No automations required.
- **Configuration tab:** values are **pulled live** on page load. Changes are written only when you edit a setting, click **Apply**, and **confirm** the old → new change. No automations required.

The [Optional Home Assistant automations](#optional-home-assistant-automations-not-required) section below contains **optional** examples only (notifications/safety). They are not required for setup.

## What it does

### Export tab (local schedule, stored in add-on DB)
- 24-hour export schedule managed in the add-on UI (ingress; no host port mapping).
- Stores schedule in the add-on’s local SQLite DB (`/data/schedules.db` inside the add-on container).
- **Add rows in the table**, edit values, drag rows to rearrange the view, then click **Save schedule** (one action). New rows are created on save; existing rows are updated.
- Per-slot columns (Export tab only): **Start/End**, **Power (W)**, **Default export (W)**, **Battery %** (0–100), **Enabled**. Power/default export are entered in **Watts** (`0–16000 W`).
- **SOC-aware export (re-evaluated every minute, not latched):** for the active slot the add-on reads battery SOC from the configured `battery_percentage` sensor (default `sensor.deye_battery_soc`). If **SOC > Battery %** it applies the slot **Power (W)** to the export entity; if **SOC <= Battery %** it applies **Default export (W)**. If SOC is unavailable/invalid/out of range (not 0–100) it applies **Default export (W)** (safe fallback) and shows status in the UI.
- **Save validation:** enabled slots must cover the full day continuously with **no gaps/overlaps** (`00:00`–24:00). Use `00:00` as end time for midnight (e.g. `22:00–00:00`), or `00:00–00:00` for an all-day slot. `24:00` is not accepted. A single slot cannot cross midnight except by using `00:00` as the end; split cross-midnight ranges into two slots (e.g. `22:00–00:00` and `00:00–06:00`).
- Pulls **live inverter value** from Home Assistant on page load / after restart, compares with the **SOC-effective schedule expected now**, and prompts you to choose **master** (inverter or schedule) if they differ.
- Applies the effective export power automatically (checked every minute) and via **Apply schedule to inverter now**.
- Table display reloads ordered by **start time** (longer duration first when start times match). Schedule logic is time-based.

### Time of Use tab (TOU programs, live read/write; no local copy)
- Shows the **6 Deye Time of Use programs** (`deye_prog1`–`deye_prog6`, as exposed by the Sunsynk/Deye inverter integration/add-on entities in Home Assistant) in **6 rows ordered by program time**.
- **Always pulls live info from Home Assistant/inverter on page load** (and after sync/reload). **No local copy is kept** for TOU data.
- **All 7 fields are editable per row:** Time, Power (W), Capacity (%), Grid charge, Mode, Sell, Voltage (V) — using the entity’s live options/min/max/step where available.
- **Per-row save with safety checks:** **Save & sync** is **disabled until the row is changed**. Clicking it shows an **old → new confirmation** (all row values, changed fields highlighted) and requires explicit confirmation before writing. Saving **directly updates the inverter** via Home Assistant services and reloads live values.
- **Unsaved changes:** if you edit Time of Use rows and try to leave/navigate away without saving, the UI prompts whether to **Sync all & leave**, **Leave without syncing**, or **Cancel**.
- **Partial-sync reporting:** HA service calls are **not atomic**. If a sync fails partway through, the error reports which entities were **Applied** and which **Failed**; review live inverter values before retrying.
- **Select validation (fail-closed):** if a select entity’s state/options cannot be verified, or the selected value is invalid, sync is blocked with a clear error.
- **Entity mapping (current implementation):** for each program `N` = 1–6: `select.deye_progN_time`, `number.deye_progN_power`, `number.deye_progN_capacity`, `select.deye_progN_charge`, `select.deye_progN_mode`, `switch.deye_progN_sell`, `number.deye_progN_voltage`. Verify these entities exist and match your inverter/integration before relying on writes.

### Configuration tab (live read/write; no local copy)
- Shows selected **Deye inverter configuration/settings** pulled **live from Home Assistant** on page load, organized into sections: **Battery Configuration**, **Grid Settings**, **Mode and Control**, **Inverter Status**.
- **No local copy is kept** for Configuration data.
- **Per-entity Apply with safety checks:** editable widgets (select/switch/number) use live HA options/min/max/step/units where available. **Apply is disabled until the value changes**. Clicking Apply shows an **old → new confirmation** and requires explicit confirmation before writing. Applying **directly updates the inverter** via Home Assistant.
- **Read-only status indicators:** status fields are shown but not editable where configured (e.g. `binary_sensor.deye_grid_connected` and the Inverter Enabled status indicator in Inverter Status). `switch.deye_near_realtime` is editable in Inverter Status; `switch.deye_inverter_enabled` remains editable in Mode and Control.
- **Allowlisted writes / validation:** only entities defined for the Configuration tab can be written. Read-only fields are rejected. Select options are validated **fail-closed** when options cannot be verified. Number values are clamped to HA min/max when readable. Errors are shown clearly and values reload live after a successful apply.
- **Current allowlisted Configuration entities (implementation):**
  - **Battery Configuration:** `select.deye_bms_protocol`; `number.deye_battery_max_charge_current`, `number.deye_battery_max_discharge_current`, `number.deye_grid_charge_battery_current`, `number.deye_battery_equalization_voltage`, `number.deye_battery_absorption_voltage`, `number.deye_battery_float_voltage`, `number.deye_battery_shutdown_capacity`, `number.deye_battery_restart_capacity`, `number.deye_battery_low_capacity`, `number.deye_battery_shutdown_voltage`, `number.deye_battery_restart_voltage`, `number.deye_battery_low_voltage`.
  - **Grid Settings:** `switch.deye_grid_charge_enabled`, `switch.deye_grid_always_on`, `switch.deye_grid_peak_shaving`; `number.deye_grid_peak_shaving_power`, `number.deye_grid_trickle_feed_power`.
  - **Mode and Control:** `switch.deye_inverter_enabled`, `switch.deye_use_timer`, `switch.deye_priority_load`, `switch.deye_solar_export`, `switch.deye_peak_shaving`, `switch.deye_gen_peak_shaving`; `number.deye_gen_peak_shaving_power`; `select.deye_load_limit`, `select.deye_aux_port_usage`; `number.deye_max_solar_power`, `number.deye_max_sell_power`.
  - **Inverter Status:** `binary_sensor.deye_grid_connected` (read-only), `switch.deye_inverter_enabled` (read-only status indicator), `switch.deye_near_realtime` (editable).
- **Safety:** verify entity IDs/behavior for your inverter/integration before relying on writes. Configuration changes can affect inverter operation.

## Prerequisites

1. **Home Assistant OS / Supervised** with **Apps** (Add-ons/Supervisor).
2. **Export:** an integration that exposes your Deye inverter’s **max export/sell power** as a controllable Home Assistant entity (commonly `number.*`; default `number.deye_max_sell_power`).
3. **Export SOC rules (optional but recommended):** a battery SOC sensor (default `sensor.deye_battery_soc`) for SOC-aware Export behavior.
4. **Time of Use:** the corresponding Deye/Sunsynk TOU program entities available in Home Assistant (e.g. `select/number/switch.deye_prog1_*` … `deye_prog6_*`) if you want to use the Time of Use tab.
5. **Configuration:** the corresponding `deye_*` entities listed above available in Home Assistant if you want to use the Configuration tab.
6. You can find and control those entities in Home Assistant:
   - Go to **Settings → Devices & services → Entities** or **Developer Tools → States**.
   - Confirm entities can be set (e.g. `number.set_value`, `select.select_option`, `switch.turn_on/off`). **Back up/verify before relying on it** — integrations differ in units, options, and accepted ranges.

> **Units/scale & safety:** The add-on sends values based on entity types/units shown by Home Assistant. Time of Use and Configuration writes change inverter behavior. Verify with small manual tests in **Developer Tools → Actions/Services** before relying on scheduled or manual control.

## Installation

1. In Home Assistant, open **Settings → Apps → ⋮ (top-right) → Repositories**.
2. Click **Add**, paste the repository URL, then add/close:
   ```text
   https://github.com/maverik-77/deye-export-manager
   ```
3. Go back to the Apps/add-on store view, click **⋮ → Check for updates** / reload if needed, then find and install **Deye Export Manager**.
4. Open the add-on page → **Configuration** tab and set:
   - `deye_entity`: the exact Home Assistant entity ID to control for **Export** (default `number.deye_max_sell_power`). Replace with your real entity if different.
   - `battery_percentage`: the battery SOC sensor for Export rules (default `sensor.deye_battery_soc`).
5. Save configuration, **Start** the add-on, and open the **Log** tab. Confirm there are no startup/API errors.
6. Enable **Show in sidebar** (or open via the add-on’s ingress/Open Web UI). The panel title is **Deye Export**.

## Using the UI

### Export tab
1. **Status panel:** shows configured entity, live inverter value (pulled from HA), schedule expected now (SOC-effective/configured), DB/last applied, and coverage/status.
2. **Mismatch prompt:** if inverter value differs from the schedule expected now, choose:
   - **Use inverter value (update app schedule):** updates the currently active slot or creates an all-day baseline in the add-on DB.
   - **Use schedule value (apply to inverter):** sends the schedule’s expected power to the inverter.
3. **Stored Configuration table:**
   - **Add row:** adds an unsaved row in the table. **Remove** (unsaved rows) removes it client-side.
   - **Delete** (saved rows): deletes immediately from the add-on DB.
   - **Drag** handle: rearranges rows for viewing/editing (visual only; schedule is time-based and reloads ordered by start time).
   - Edit Start/End/Power (W)/Default export (W)/Battery %/Enabled, then click **Save schedule**.
4. **Save validation:** enabled slots must form a complete 24-hour partition (00:00–24:00, contiguous, no overlaps). If validation fails, an error is shown and the page reloads from the DB (unsaved edits/new rows from that attempt are not kept).
5. **Apply schedule to inverter now** (in the table controls): applies the currently expected SOC-effective value immediately (if different from last applied).

### Time of Use tab
1. Open **Time of Use** — the page **pulls live TOU program values from HA/inverter**.
2. Review/edit the **6 program rows** (ordered by time). All 7 fields are editable.
3. **Save & sync** is enabled only when a row has changes. Click it to review the **old → new** confirmation, then confirm to write that program to the inverter and reload live values.
4. If you leave with unsaved edits, choose **Sync all & leave**, **Leave without syncing**, or **Cancel**.
5. If an entity is missing/unavailable or options cannot be verified, sync is blocked or the row shows read/sync issues; check entity IDs/integration state and add-on **Log**.

### Configuration tab
1. Open **Configuration** — the page **pulls live configuration/status values from HA**.
2. Review the **Battery Configuration**, **Grid Settings**, **Mode and Control**, and **Inverter Status** sections.
3. Edit a setting (select/switch/number). **Apply** is enabled only when the value differs from the current live value.
4. Click **Apply**, review the **old → new** confirmation, then confirm to **directly update the inverter** via Home Assistant. The page reloads live values after a successful apply.
5. Read-only status fields cannot be applied. If an entity is unavailable or options cannot be verified, apply may be blocked with a clear error; check entity IDs/integration state and add-on **Log**.

## Time & coverage rules (Export)

- Times are `HH:MM`. A slot matches when `start <= current time < end`.
- **Midnight end:** use `00:00` as the end time (e.g. `22:00–00:00`).
- **All day:** `00:00–00:00`.
- **`24:00` not accepted.** Use `00:00` for midnight.
- **Cross-midnight ranges:** a single slot cannot cross midnight except by using `00:00` as the end; split into two slots (e.g. `22:00–00:00` and `00:00–06:00`).
- **No enabled slot covering current time:** runtime applies `0 W`. With a valid saved schedule (full 24h coverage), this should not happen.
- Scheduling uses the add-on/container’s local time (`datetime.now()`). Ensure your system/add-on timezone matches your intended schedule.

## Configuration options

| Option | Description | Default |
|---|---|---|
| `deye_entity` | Home Assistant entity ID used with `number.set_value` for Export schedule control | `number.deye_max_sell_power` |
| `battery_percentage` | Home Assistant sensor entity ID providing battery SOC (%) for Export SOC-aware rules | `sensor.deye_battery_soc` |

> **Note:** Time of Use and Configuration tab entity IDs are currently defined by the add-on implementation (see [What it does](#what-it-does)) and are not separate add-on configuration options.

## Optional Home Assistant automations (NOT required)

These are **optional** only. The add-on works without them. Add them only if you want extra notifications/safety. **Adapt entity IDs, notify services, units, thresholds, and test carefully.** A safety automation that also controls the same entity can conflict with the add-on—prefer one source of truth unless you have a specific reason.

### How to add an optional automation

- **UI (recommended for most users):** **Settings → Automations & scenes → Create automation → Create new automation**. Add the matching trigger/condition/action. Replace `number.deye_max_sell_power` with your `deye_entity`, and replace `notify.notify` with your real notify service (e.g. a mobile app notify service or `persistent_notification.create` action pattern as appropriate in your HA version/UI).
- **YAML:** If you manage automations in `automations.yaml` (UI-managed automations file), add the automation entry there *carefully* (it must remain valid and keep existing automations). If you use a YAML automation include under `configuration.yaml`, follow your existing structure. After YAML changes, validate config and reload automations / restart as needed.

### 1) Optional: notify if the control entity becomes unavailable
```yaml
alias: "Deye Export: notify when control entity unavailable"
id: deye_export_notify_entity_unavailable
mode: single
trigger:
  - platform: state
    entity_id: number.deye_max_sell_power  # CHANGE to your configured deye_entity
    to:
      - "unavailable"
      - "unknown"
    for: "00:05:00"
action:
  - service: notify.notify  # CHANGE to your notify service
    data:
      title: "Deye Export Manager"
      message: >-
        Export control entity {{ trigger.entity_id }} is {{ states(trigger.entity_id) }}.
        The Deye Export Manager add-on may not be able to apply schedules.
```

### 2) Optional: safety clamp (use with caution)
Can conflict with add-on control. Only use if you understand your inverter limits and entity units.

```yaml
alias: "Deye Export: safety clamp max export"
id: deye_export_safety_clamp
mode: single
trigger:
  - platform: state
    entity_id: number.deye_max_sell_power  # CHANGE if needed
condition:
  - condition: template
    value_template: >-
      {{ states('number.deye_max_sell_power') not in ['unknown','unavailable']
         and (states('number.deye_max_sell_power') | float(0)) > 16000 }}
action:
  - service: number.set_value
    target:
      entity_id: number.deye_max_sell_power
    data:
      value: 16000  # CHANGE: safe max; units must match the entity
  - service: notify.notify  # CHANGE to your notify service
    data:
      title: "Deye Export Manager safety"
      message: "Export limit exceeded threshold; clamped. Check schedule/inverter state."
```

### 3) Optional: notify on manual/any export-power change
Can be noisy. Useful for awareness; the UI also shows mismatches on load.

```yaml
alias: "Deye Export: notify on export power change"
id: deye_export_notify_manual_change
mode: queued
trigger:
  - platform: state
    entity_id: number.deye_max_sell_power  # CHANGE if needed
condition:
  - condition: template
    value_template: "{{ trigger.from_state is not none and trigger.to_state is not none }}"
  - condition: template
    value_template: "{{ trigger.to_state.state not in ['unknown','unavailable'] }}"
action:
  - service: notify.notify  # CHANGE to your notify service (optional; can be noisy)
    data:
      title: "Deye export power changed"
      message: >-
        {{ trigger.entity_id }} changed from {{ trigger.from_state.state }} to {{ trigger.to_state.state }}.
        If unexpected, open Deye Export Manager and check for schedule/inverter mismatch.
```

### 4) Optional: daily review reminder
```yaml
alias: "Deye Export: daily schedule review reminder"
id: deye_export_daily_review
mode: single
trigger:
  - platform: time
    at: "08:00:00"
action:
  - service: notify.notify  # CHANGE to your notify service
    data:
      title: "Deye Export Manager"
      message: "Optional reminder: review your Deye export/TOU/settings if tariffs/plans changed."
```

## Troubleshooting

- **Entity error/not found (Export):** check `deye_entity` in add-on Configuration and confirm it exists in **Developer Tools → States**.
- **Time of Use read or sync errors:** confirm the `deye_prog1`–`deye_prog6` entities exist, are available, and accept the selected options/values for your integration. Check add-on **Log** for HA API/service errors.
- **Time of Use sync incomplete:** HA service calls are not atomic; the error shows which entities were **Applied** and which **Failed**. Review inverter/live values before retrying.
- **Configuration read/apply errors:** confirm the relevant `deye_*` entity exists and is available. Select applies are fail-closed when options cannot be verified. Read-only/allowlisted fields cannot be changed from the Configuration tab. Check add-on **Log** for HA API/service errors.
- **Schedule not applying:** check add-on **Log**; verify add-on is running, entity is controllable, and units/scale are correct. Use **Apply schedule to inverter now** to test.
- **SOC unavailable/invalid/out of range:** Export SOC logic treats SOC outside 0–100 (or unavailable/invalid) as unavailable and falls back to **Default export (W)** for the active slot.
- **Save failed / coverage error (Export):** enabled slots must cover 00:00–24:00 with no gaps/overlaps. Use `00:00` end for midnight (`24:00` not accepted); split cross-midnight ranges into two slots. If a stale row id is detected during save, reload the page and try again.
- **Mismatch prompt:** choose the correct master after reviewing live inverter vs. expected schedule.
- **After updating add-on:** **Check for updates**, rebuild/update, restart, and hard-refresh the ingress UI if it looks stale.
- **Separate data:** old standalone app data is not auto-imported into the add-on’s `/data/schedules.db`. Time of Use and Configuration data are not stored locally.

## Repository / development

Repository: `https://github.com/maverik-77/deye-export-manager`  
Source: `deye_export_manager/` (`config.yaml`, `build.yaml`, `Dockerfile`, `run.sh`, `main.py`, `templates/index.html`).
