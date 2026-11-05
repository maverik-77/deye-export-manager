# Deye Export Manager (Home Assistant Add-on)

Custom Home Assistant add-on for managing a **24-hour export-power schedule** for a Deye inverter. It controls a Home Assistant `number` entity (default: `number.deye_max_sell_power`) through the Supervisor/Home Assistant API and provides an **ingress UI** in the Home Assistant sidebar.

## Required Home Assistant automations: **NONE**

**You do not need to create any Home Assistant automations for this add-on to work.**  
Core scheduling and applying values to the inverter are handled by the add-on itself (checks every minute, plus “Apply Schedule to Inverter Now” in the UI), as long as:

- the add-on is installed, configured, and **running**;
- Home Assistant API access is available to the add-on (it uses `homeassistant_api` / Supervisor token);
- `deye_entity` points to the correct, working Home Assistant entity for your inverter.

The [Optional Home Assistant automations](#optional-home-assistant-automations-not-required) section below contains **optional** examples only (notifications/safety). They are not required for setup.

## What it does

- 24-hour schedule managed in the add-on UI (ingress; no host port mapping).
- Stores schedule in the add-on’s local SQLite DB (`/data/schedules.db` inside the add-on container).
- **Add rows in the table**, edit values, drag rows to rearrange the view, then click **Save schedule** (one action). New rows are created on save; existing rows are updated.
- Power is entered in **Watts** in the UI (`0–16000 W`) and converted/stored internally as needed.
- **Save validation:** enabled slots must cover the full day continuously with **no gaps/overlaps** (`00:00`–24:00). Use `00:00` as end time for midnight (e.g. `22:00–00:00`), or `00:00–00:00` for an all-day slot.
- Pulls **live inverter value** from Home Assistant on page load / after restart, compares with the **schedule expected now**, and prompts you to choose **master** (inverter or schedule) if they differ.
- Applies the expected power automatically when the schedule changes (checked every minute) and via **Apply Schedule to Inverter Now**.
- Table display reloads ordered by **start time** (longer duration first when start times match). Schedule logic is time-based.

## Prerequisites

1. **Home Assistant OS / Supervised** with **Apps** (Add-ons/Supervisor).
2. An integration that exposes your Deye inverter’s **max export/sell power** as a controllable Home Assistant entity (commonly a `number.*` entity; the exact entity ID depends on your integration, e.g. Solarman/SolarmanPV/Deye-related integrations).
3. You can find and control that entity in Home Assistant:
   - Go to **Settings → Devices & services → Entities** or **Developer Tools → States**.
   - Search for terms like `deye`, `sell`, `export`, `max`.
   - Confirm the entity you choose can be set (e.g. `number.set_value` works / the value changes as expected). **Back up/verify before relying on it** — integrations differ in units and accepted ranges.

> **Units/scale:** The add-on sends watts (integer) to `number.set_value` and the UI uses Watts. Some integrations expect different units/scales. Verify with a small manual test in **Developer Tools → Services/Actions** before using scheduled control.

## Installation

1. In Home Assistant, open **Settings → Apps → ⋮ (top-right) → Repositories**.
2. Click **Add**, paste the repository URL, then add/close:
   ```text
   https://github.com/maverik-77/deye-export-manager
   ```
3. Go back to the Apps/add-on store view, click **⋮ → Check for updates** / reload if needed, then find and install **Deye Export Manager**.
4. Open the add-on page → **Configuration** tab and set:
   - `deye_entity`: the exact Home Assistant entity ID to control (default `number.deye_max_sell_power`). Replace with your real entity if different.
5. Save configuration, **Start** the add-on, and open the **Log** tab. Confirm there are no startup/API errors.
6. Enable **Show in sidebar** (or open via the add-on’s ingress/Open Web UI). The panel title is **Deye Export**.

## Using the UI

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
5. **Apply Schedule to Inverter Now:** applies the currently expected schedule value immediately (if different from last applied).

## Time & coverage rules

- Times are `HH:MM`. A slot matches when `start <= current time < end`.
- **Midnight end:** use `00:00` as the end time (e.g. `22:00–00:00`).
- **All day:** `00:00–00:00`.
- **No enabled slot covering current time:** runtime applies `0 W`. With a valid saved schedule (full 24h coverage), this should not happen.
- Scheduling uses the add-on/container’s local time (`datetime.now()`). Ensure your system/add-on timezone matches your intended schedule.

## Configuration option

| Option | Description | Default |
|---|---|---|
| `deye_entity` | Home Assistant entity ID used with `number.set_value` | `number.deye_max_sell_power` |

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
      message: "Optional reminder: review your Deye export schedule if tariffs/plans changed."
```

## Troubleshooting

- **Entity error/not found:** check `deye_entity` in add-on Configuration and confirm it exists in **Developer Tools → States**.
- **Schedule not applying:** check add-on **Log**; verify add-on is running, entity is controllable, and units/scale are correct. Use **Apply Schedule to Inverter Now** to test.
- **Save failed / coverage error:** enabled slots must cover 00:00–24:00 with no gaps/overlaps. Use `00:00` end for midnight; for a final-minute issue, end the last slot at `00:00`.
- **Mismatch prompt:** choose the correct master after reviewing live inverter vs. expected schedule.
- **After updating add-on:** **Check for updates**, rebuild/update, restart, and hard-refresh the ingress UI if it looks stale.
- **Separate data:** old standalone app data is not auto-imported into the add-on’s `/data/schedules.db`.

## Repository / development

Repository: `https://github.com/maverik-77/deye-export-manager`  
Source: `deye_export_manager/` (`config.yaml`, `build.yaml`, `Dockerfile`, `run.sh`, `main.py`, `templates/index.html`).
