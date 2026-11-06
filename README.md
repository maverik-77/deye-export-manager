# Deye Export Manager (Home Assistant Add-on)

Custom Home Assistant add-on for managing a **24-hour export-power schedule** and viewing/editing **Deye inverter Time of Use (TOU) programs**. It uses the Supervisor/Home Assistant API and provides an **ingress UI** in the Home Assistant sidebar with **Export** and **Import** tabs.

## Required Home Assistant automations: **NONE**

**You do not need to create any Home Assistant automations for this add-on to work.**

- **Export tab (schedule):** core scheduling and applying export power are handled by the add-on itself (checks every minute, plus **Apply schedule to inverter now**), as long as the add-on is running, has Home Assistant API access (`homeassistant_api` / Supervisor token), and `deye_entity` is correct/controllable.
- **Import tab (TOU programs):** values are **pulled live from Home Assistant/inverter on every page load** and changes are written only when you click **Save & sync** on a row (or confirm the unsaved-leave prompt). No automations required.

The [Optional Home Assistant automations](#optional-home-assistant-automations-not-required) section below contains **optional** examples only (notifications/safety). They are not required for setup.

## What it does

### Export tab (local schedule, stored in add-on DB)
- 24-hour export schedule managed in the add-on UI (ingress; no host port mapping).
- Stores schedule in the add-on’s local SQLite DB (`/data/schedules.db` inside the add-on container).
- **Add rows in the table**, edit values, drag rows to rearrange the view, then click **Save schedule** (one action). New rows are created on save; existing rows are updated.
- Per-slot columns (Export tab only): **Start/End**, **Power (W)**, **Default export (W)**, **Battery %** (0–100), **Enabled**. Power/default export are entered in **Watts** (`0–16000 W`).
- **SOC-aware export (re-evaluated every minute, not latched):** for the active slot the add-on reads battery SOC from the configured `battery_percentage` sensor (default `sensor.deye_battery_soc`). If **SOC > Battery %** it applies the slot **Power (W)** to the export entity; if **SOC <= Battery %** it applies **Default export (W)**. If SOC is unavailable/invalid it applies **Default export (W)** (safe fallback) and shows status in the UI.
- **Save validation:** enabled slots must cover the full day continuously with **no gaps/overlaps** (`00:00`–24:00). Use `00:00` as end time for midnight (e.g. `22:00–00:00`), or `00:00–00:00` for an all-day slot.
- Pulls **live inverter value** from Home Assistant on page load / after restart, compares with the **SOC-effective schedule expected now**, and prompts you to choose **master** (inverter or schedule) if they differ.
- Applies the effective export power automatically (checked every minute) and via **Apply schedule to inverter now**.
- Table display reloads ordered by **start time** (longer duration first when start times match). Schedule logic is time-based.

### Import tab (Deye TOU programs, live read/write; no local copy)
- Shows the **6 Deye Time of Use programs** (`deye_prog1`–`deye_prog6`, as exposed by the Sunsynk/Deye inverter integration/add-on entities in Home Assistant) in **6 rows ordered by program time**.
- **Always pulls live info from Home Assistant/inverter on page load** (and after sync/reload). **No local copy is kept** for TOU data.
- **All 7 fields are editable per row:** Time, Power (W), Capacity (%), Grid charge, Mode, Sell, Voltage (V) — using the entity’s live options/min/max/step where available.
- **Per-row save:** each row has **Save & sync** which writes that program’s values to the inverter via Home Assistant services and reloads live values.
- **Unsaved changes:** if you edit Import rows and try to leave/navigate away without saving, the UI prompts whether to **sync changes to the inverter before leaving**, leave without syncing, or cancel.
- **Entity mapping (current implementation):** for each program `N` = 1–6: `select.deye_progN_time`, `number.deye_progN_power`, `number.deye_progN_capacity`, `select.deye_progN_charge`, `select.deye_progN_mode`, `switch.deye_progN_sell`, `number.deye_progN_voltage`. Verify these entities exist and match your inverter/integration before relying on writes.

## Prerequisites

1. **Home Assistant OS / Supervised** with **Apps** (Add-ons/Supervisor).
2. **Export:** an integration that exposes your Deye inverter’s **max export/sell power** as a controllable Home Assistant entity (commonly `number.*`; default `number.deye_max_sell_power`).
3. **Import (TOU):** the corresponding Deye/Sunsynk TOU program entities available in Home Assistant (e.g. `select/number/switch.deye_prog1_*` … `deye_prog6_*`) if you want to use the Import tab.
4. You can find and control those entities in Home Assistant:
   - Go to **Settings → Devices & services → Entities** or **Developer Tools → States**.
   - Confirm entities can be set (e.g. `number.set_value`, `select.select_option`, `switch.turn_on/off`). **Back up/verify before relying on it** — integrations differ in units, options, and accepted ranges.

> **Units/scale & safety:** The add-on sends values based on entity types/units shown by Home Assistant. TOU writes change inverter behavior. Verify with small manual tests in **Developer Tools → Actions/Services** before relying on scheduled/TOU control.

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
1. **Status panel:** shows configured entity, live inverter value (pulled from HA), schedule expected now, DB/last applied, and coverage status.
2. **Mismatch prompt:** if inverter value differs from the schedule expected now, choose:
   - **Use inverter value (update app schedule):** updates the currently active slot or creates an all-day baseline in the add-on DB.
   - **Use schedule value (apply to inverter):** sends the schedule’s expected power to the inverter.
3. **Stored Configuration table:**
   - **Add row:** adds an unsaved row in the table. **Remove** (unsaved rows) removes it client-side.
   - **Delete** (saved rows): deletes immediately from the add-on DB.
   - **Drag** handle: rearranges rows for viewing/editing (visual only; schedule is time-based and reloads ordered by start time).
   - Edit Start/End/Power (W)/Enabled, then click **Save schedule**.
4. **Save validation:** enabled slots must form a complete 24-hour partition (00:00–24:00, contiguous, no overlaps). If validation fails, an error is shown and the page reloads from the DB (unsaved edits/new rows from that attempt are not kept).
5. **Apply schedule to inverter now** (in the table controls): applies the currently expected schedule value immediately (if different from last applied).

### Import tab
1. Open **Import (TOU)** — the page **pulls live TOU program values from HA/inverter**.
2. Review/edit the **6 program rows** (ordered by time). All 7 fields are editable.
3. Click **Save & sync** on a row to write that program to the inverter and reload live values.
4. If you leave with unsaved edits, choose **Sync all & leave**, **Leave without syncing**, or **Cancel**.
5. If an entity is missing/unavailable, the row shows read/sync issues; check entity IDs/integration state.

## Time & coverage rules (Export)

- Times are `HH:MM`. A slot matches when `start <= current time < end`.
- **Midnight end:** use `00:00` as the end time (e.g. `22:00–00:00`).
- **All day:** `00:00–00:00`.
- **No enabled slot covering current time:** runtime applies `0 W`. With a valid saved schedule (full 24h coverage), this should not happen.
- Scheduling uses the add-on/container’s local time (`datetime.now()`). Ensure your system/add-on timezone matches your intended schedule.

## Configuration options

| Option | Description | Default |
|---|---|---|
| `deye_entity` | Home Assistant entity ID used with `number.set_value` for Export schedule control | `number.deye_max_sell_power` |
| `battery_percentage` | Home Assistant sensor entity ID providing battery SOC (%) for Export SOC-aware rules | `sensor.deye_battery_soc` |

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
      message: "Optional reminder: review your Deye export/TOU settings if tariffs/plans changed."
```

## Troubleshooting

- **Entity error/not found (Export):** check `deye_entity` in add-on Configuration and confirm it exists in **Developer Tools → States**.
- **TOU/Import read or sync errors:** confirm the `deye_prog1`–`deye_prog6` entities exist, are available, and accept the selected options/values for your integration. Check add-on **Log** for HA API/service errors.
- **Schedule not applying:** check add-on **Log**; verify add-on is running, entity is controllable, and units/scale are correct. Use **Apply schedule to inverter now** to test.
- **Save failed / coverage error (Export):** enabled slots must cover 00:00–24:00 with no gaps/overlaps. Use `00:00` end for midnight.
- **Mismatch prompt:** choose the correct master after reviewing live inverter vs. expected schedule.
- **After updating add-on:** **Check for updates**, rebuild/update, restart, and hard-refresh the ingress UI if it looks stale.
- **Separate data:** old standalone app data is not auto-imported into the add-on’s `/data/schedules.db`. TOU data is not stored locally.

## Repository / development

Repository: `https://github.com/maverik-77/deye-export-manager`  
Source: `deye_export_manager/` (`config.yaml`, `build.yaml`, `Dockerfile`, `run.sh`, `main.py`, `templates/index.html`).
