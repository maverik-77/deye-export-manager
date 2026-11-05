# Deye Export Manager (Home Assistant Add-on)

Custom Home Assistant add-on/app for managing a **24-hour export schedule** for a Deye inverter by controlling a Home Assistant `number` entity (default: `number.deye_max_sell_power`). It provides an **ingress UI** in the Home Assistant sidebar for viewing live inverter state, stored schedule slots, mismatch resolution, and direct updates.

> **Core scheduling is handled by the add-on itself.** You do **not** need Home Assistant YAML automations for the basic schedule/apply loop to work, as long as the add-on is running, has Home Assistant API access, and the configured entity is correct.

## Repository

Add this repository in Home Assistant:
**Settings → Apps → ⋮ → Repositories → Add**  
`https://github.com/maverik-77/deye-export-manager`

## Features

- 24-hour schedule with multiple time slots (ordered by start time)
- Add/delete slots; edit all rows in the table and use a **single “Save schedule”** action
- Export power with fine precision (UI supports `0–16 kW`, `0.01 kW` step)
- Stores schedules in the add-on’s local SQLite database (`/data/schedules.db` inside the add-on)
- Pulls **live inverter value** from Home Assistant on page load / after restart
- Compares inverter value vs. **schedule expected now** and prompts you to choose a **master** when they differ
- Applies the correct power based on the current time (scheduler checks every minute) and via **Apply Schedule to Inverter Now**
- Home Assistant **ingress** UI (no host port mapping required)

## Prerequisites

- Home Assistant OS / Supervised environment with **Apps/Add-ons** (Supervisor)
- A working Home Assistant entity that controls Deye max export/sell power (commonly a `number.*` entity exposed by your Deye/Solarman integration)
- The entity must accept the value format your integration expects (the add-on sends watts as an integer to `number.set_value`; UI shows kW; verify your integration’s units/scale)

## Installation

1. Add the repository URL above in **Settings → Apps → ⋮ → Repositories**.
2. **⋮ → Check for updates** / reload if needed.
3. Install **Deye Export Manager**.
4. Open add-on **Configuration** and set:
   - `deye_entity`: your export power control entity (default `number.deye_max_sell_power`)
5. Start the add-on and check **Logs** for errors.
6. Enable **Show in sidebar** (ingress panel: *Deye Export*).

## Configuration

| Option | Description | Default |
|---|---|---|
| `deye_entity` | Home Assistant entity ID used with `number.set_value` | `number.deye_max_sell_power` |

The add-on uses Supervisor/Home Assistant API access (`homeassistant_api: true`, `SUPERVISOR_TOKEN`) and ingress.

## How it works

- **Schedule slots** are stored in the add-on database. For the current local time, the add-on finds the first enabled slot where `start_time <= now < end_time` and applies that power (kW → W). If no enabled slot covers the current time, it applies **0 W** (verify this matches your intent).
- **Page load / restart:** the UI pulls the inverter’s live state and compares it with the schedule’s expected value *now*. If different, it asks whether **inverter** or **schedule** is master and updates the other accordingly.
- **Apply Schedule to Inverter Now:** computes the currently expected value and applies it if needed.

## Optional Home Assistant automations

The add-on handles the main schedule. The automations below are **optional** examples for safety, visibility, and coordination. **Review, adapt entity IDs, and test carefully** before enabling. Use Home Assistant’s automation editor or YAML; validate configuration changes before reloading/restarting.

### 1) Notify if the export-control entity becomes unavailable
Useful if the inverter/integration drops offline.

```yaml
automation:
  - alias: "Deye Export: notify when control entity unavailable"
    id: deye_export_notify_entity_unavailable
    mode: single
    trigger:
      - platform: state
        entity_id: number.deye_max_sell_power  # change to your configured deye_entity
        to:
          - "unavailable"
          - "unknown"
        for: "00:05:00"
    action:
      - service: notify.notify  # change to your notify service (mobile_app_*, persistent_notification, etc.)
        data:
          title: "Deye Export Manager"
          message: >-
            Export control entity {{ trigger.entity_id }} is {{ states(trigger.entity_id) }}.
            The Deye Export Manager add-on may not be able to apply schedules.
```

### 2) Safety clamp / guardrail (optional)
Only use if you understand your inverter/integration limits. This can conflict with the add-on if both try to control the same entity; prefer **one source of truth** (the add-on) unless you have a specific safety need.

```yaml
automation:
  - alias: "Deye Export: safety clamp max export"
    id: deye_export_safety_clamp
    mode: single
    trigger:
      - platform: state
        entity_id: number.deye_max_sell_power  # change if needed
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
          value: 16000  # adjust to your safe maximum (units must match the entity!)
      - service: notify.notify
        data:
          title: "Deye Export Manager safety"
          message: "Export limit exceeded threshold; clamped to safe maximum. Check add-on schedule and inverter state."
```

### 3) Notify when export power is changed manually outside the expected schedule
This helps detect overrides. It compares the entity value to a simple expectation; for precise comparison use a template/sensor that mirrors the add-on’s current-time logic, or rely on the add-on UI mismatch prompt.

```yaml
automation:
  - alias: "Deye Export: notify on manual export change"
    id: deye_export_notify_manual_change
    mode: queued
    trigger:
      - platform: state
        entity_id: number.deye_max_sell_power  # change if needed
    condition:
      - condition: template
        value_template: "{{ trigger.from_state is not none and trigger.to_state is not none }}"
      - condition: template
        value_template: "{{ trigger.to_state.state not in ['unknown','unavailable'] }}"
    action:
      - service: notify.notify  # optional; can be noisy
        data:
          title: "Deye export power changed"
          message: >-
            {{ trigger.entity_id }} changed from {{ trigger.from_state.state }} to {{ trigger.to_state.state }}.
            If unexpected, open Deye Export Manager to check for schedule/inverter mismatch.
```

### 4) Daily reminder to review schedule (optional)
```yaml
automation:
  - alias: "Deye Export: daily schedule review reminder"
    id: deye_export_daily_review
    mode: single
    trigger:
      - platform: time
        at: "08:00:00"
    action:
      - service: notify.notify
        data:
          title: "Deye Export Manager"
          message: "Optional reminder: review your Deye export schedule if tariffs/plans changed."
```

### 5) Re-apply/refresh helper via HA (usually unnecessary)
The add-on applies on its own schedule and provides an **Apply Schedule to Inverter Now** button. If you specifically need HA to nudge behavior, prefer using the add-on UI/API only if you expose it intentionally; do not assume internal unauthenticated endpoints. In most setups, **no automation is required**.

## Important notes

- **Full 24-hour coverage required on save:** when you click **Save schedule**, enabled slots must cover the entire day continuously with no gaps/overlaps (`00:00`–24:00). Use `00:00` as an end time for midnight (e.g. `22:00–00:00`), or `00:00–00:00` for an all-day slot. Gaps can exist temporarily while editing/adding/deleting, but saving enforces a complete schedule; runtime applies **0 W** only if no enabled slot matches (should not occur for a valid saved schedule).
- **Entity units/scale:** integrations differ (W vs kW, different ranges). Confirm that values applied by the add-on match what your Deye/integration expects.
- **Add-on database is separate** from any old standalone app data; schedules won’t auto-import.
- **After updates:** HA may require **Check for updates**, add-on rebuild/update, and restart. If UI/API behavior seems stale, confirm the running version in add-on info/logs.

## Troubleshooting

- **UI shows entity error/not found:** verify `deye_entity` in add-on config and that the entity exists in **Developer Tools → States**.
- **Apply fails / 500 / no effect:** check add-on **Logs**; confirm Supervisor API access and entity accepts `number.set_value`.
- **Mismatch prompt appears:** choose the correct master (*inverter* updates the active slot/baseline in the app DB; *schedule* pushes the expected value to the inverter).
- **Changes not taking effect after update:** ensure HA pulled the latest add-on version (v1.0.7+ includes the async `apply_now` fix), then rebuild/restart the add-on.

## Development

Source in this repo under `deye_export_manager/`. Key files: `config.yaml`, `build.yaml`, `Dockerfile`, `run.sh`, `main.py`, `templates/index.html`.
