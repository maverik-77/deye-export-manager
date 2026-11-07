#!/usr/bin/env python3
import os
import sqlite3
import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
import httpx

app = FastAPI(title="Deye Manager")

STARTUP_CHECK_DONE = False


@app.middleware("http")
async def ingress_root_path(request: Request, call_next):
    root_path = request.headers.get("X-Ingress-Path", "")
    request.scope["root_path"] = root_path
    return await call_next(request)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "schedules.db")
HA_URL = os.environ.get("HA_URL", "http://supervisor/core").rstrip("/")
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN", "")
DEYE_ENTITY = os.environ.get("DEYE_ENTITY", "number.deye_max_sell_power")
BATTERY_PERCENTAGE = os.environ.get("BATTERY_PERCENTAGE", "sensor.deye_battery_soc")

os.makedirs(DATA_DIR, exist_ok=True)

_template_dirs = [
    Path(__file__).resolve().parent / "templates",
    Path("/app/templates"),
    Path("/homeassistant/addons/deye_export_manager/templates"),
]
_template_dir = next((str(p) for p in _template_dirs if p.exists()), str(_template_dirs[0]))
env = Environment(
    loader=FileSystemLoader(_template_dir),
    autoescape=select_autoescape(["html", "xml"]),
)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT DEFAULT 'Default',
        enabled INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS schedule_slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id INTEGER DEFAULT 1,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        power_kw REAL NOT NULL,
        enabled INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(schedule_id) REFERENCES schedules(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute("INSERT OR IGNORE INTO schedules (id, name) VALUES (1, 'Default')")
    # migrations for SOC/default export columns
    c.execute("PRAGMA table_info(schedule_slots)")
    cols = [r[1] for r in c.fetchall()]
    if "default_export_w" not in cols:
        c.execute("ALTER TABLE schedule_slots ADD COLUMN default_export_w INTEGER DEFAULT 0")
    if "battery_soc_pct" not in cols:
        c.execute("ALTER TABLE schedule_slots ADD COLUMN battery_soc_pct INTEGER DEFAULT 0")
    conn.commit()
    conn.close()

init_db()

async def apply_to_ha(power_watts: int):
    if not HA_TOKEN:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HA_URL}/api/services/number/set_value",
                headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
                json={"entity_id": DEYE_ENTITY, "value": power_watts},
                timeout=10.0,
            )
            return response.status_code in [200, 201]
    except Exception as e:
        print(f"Error applying to HA: {e}")
        return False


def _watts_to_kw_state(val, unit):
    try:
        v = float(val)
    except Exception:
        return None
    unit = (unit or "").lower()
    if "kw" in unit:
        return round(v, 3)
    # if unit is W or unknown but large, assume watts
    if "w" in unit or abs(v) >= 50:
        return round(v / 1000.0, 3)
    return round(v, 3)


async def get_ha_entity_state():
    if not HA_TOKEN:
        return {"ok": False, "error": "No Supervisor token available"}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{HA_URL}/api/states/{DEYE_ENTITY}",
                headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
                timeout=10.0,
            )
            if response.status_code == 404:
                return {"ok": False, "error": f"Entity not found: {DEYE_ENTITY}"}
            if response.status_code >= 400:
                return {"ok": False, "error": f"HA API error {response.status_code}"}
            data = response.json()
            attrs = data.get("attributes") or {}
            kw = _watts_to_kw_state(data.get("state"), attrs.get("unit_of_measurement"))
            return {
                "ok": True,
                "entity_id": data.get("entity_id", DEYE_ENTITY),
                "state": data.get("state"),
                "power_kw": kw,
                "friendly_name": attrs.get("friendly_name"),
                "unit": attrs.get("unit_of_measurement"),
                "min": attrs.get("min"),
                "max": attrs.get("max"),
                "step": attrs.get("step"),
                "last_changed": data.get("last_changed"),
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}




async def get_entity_state(entity_id: str):
    if not HA_TOKEN:
        return {"ok": False, "error": "No Supervisor token available", "entity_id": entity_id}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{HA_URL}/api/states/{entity_id}",
                headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
                timeout=10.0,
            )
            if response.status_code == 404:
                return {"ok": False, "error": f"Entity not found: {entity_id}", "entity_id": entity_id}
            if response.status_code >= 400:
                return {"ok": False, "error": f"HA API error {response.status_code}", "entity_id": entity_id}
            data = response.json()
            attrs = data.get("attributes") or {}
            return {
                "ok": True,
                "entity_id": data.get("entity_id", entity_id),
                "state": data.get("state"),
                "attributes": attrs,
                "unit": attrs.get("unit_of_measurement"),
                "friendly_name": attrs.get("friendly_name"),
            }
    except Exception as e:
        return {"ok": False, "error": str(e), "entity_id": entity_id}




async def get_battery_soc():
    st = await get_entity_state(BATTERY_PERCENTAGE)
    if not st.get("ok"):
        return {"ok": False, "error": st.get("error"), "entity_id": BATTERY_PERCENTAGE, "soc": None}
    val = st.get("state")
    try:
        soc = float(val)
    except Exception:
        return {"ok": False, "error": f"Invalid SOC state: {val}", "entity_id": BATTERY_PERCENTAGE, "soc": None, "state": val}
    # Treat out-of-range SOC as unavailable so callers fall back safely (Default export)
    if soc < 0 or soc > 100:
        return {
            "ok": False,
            "error": f"SOC out of expected range (0-100): {val}",
            "entity_id": BATTERY_PERCENTAGE,
            "soc": None,
            "state": val,
        }
    return {"ok": True, "entity_id": BATTERY_PERCENTAGE, "soc": soc, "state": val}

async def ha_call_service(domain: str, service: str, payload: dict):
    if not HA_TOKEN:
        return False, "No Supervisor token available"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HA_URL}/api/services/{domain}/{service}",
                headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
                json=payload,
                timeout=15.0,
            )
            if response.status_code in (200, 201):
                return True, None
            return False, f"HA API error {response.status_code}: {response.text[:200]}"
    except Exception as e:
        return False, str(e)


TOU_PROGRAM_FIELDS = [
    ("time", "select", "select.deye_prog{n}_time"),
    ("power", "number", "number.deye_prog{n}_power"),
    ("capacity", "number", "number.deye_prog{n}_capacity"),
    ("charge", "select", "select.deye_prog{n}_charge"),
    ("mode", "select", "select.deye_prog{n}_mode"),
    ("sell", "switch", "switch.deye_prog{n}_sell"),
    ("voltage", "number", "number.deye_prog{n}_voltage"),
]

CONFIG_SECTIONS = [
    {
        "id": "battery",
        "title": "Battery Configuration",
        "entities": [
            {"entity_id": "select.deye_bms_protocol", "label": "BMS Protocol", "domain": "select"},
            {"entity_id": "number.deye_battery_max_charge_current", "label": "Max Charge Current", "domain": "number"},
            {"entity_id": "number.deye_battery_max_discharge_current", "label": "Max Discharge Current", "domain": "number"},
            {"entity_id": "number.deye_grid_charge_battery_current", "label": "Grid Charge Battery Current", "domain": "number"},
            {"entity_id": "number.deye_battery_equalization_voltage", "label": "Equalization Voltage", "domain": "number"},
            {"entity_id": "number.deye_battery_absorption_voltage", "label": "Absorption Voltage", "domain": "number"},
            {"entity_id": "number.deye_battery_float_voltage", "label": "Float Voltage", "domain": "number"},
            {"entity_id": "number.deye_battery_shutdown_capacity", "label": "Shutdown Capacity", "domain": "number"},
            {"entity_id": "number.deye_battery_restart_capacity", "label": "Restart Capacity", "domain": "number"},
            {"entity_id": "number.deye_battery_low_capacity", "label": "Low Capacity", "domain": "number"},
            {"entity_id": "number.deye_battery_shutdown_voltage", "label": "Shutdown Voltage", "domain": "number"},
            {"entity_id": "number.deye_battery_restart_voltage", "label": "Restart Voltage", "domain": "number"},
            {"entity_id": "number.deye_battery_low_voltage", "label": "Low Voltage", "domain": "number"},
        ],
    },
    {
        "id": "grid",
        "title": "Grid Settings",
        "entities": [
            {"entity_id": "switch.deye_grid_charge_enabled", "label": "Grid Charge Enabled", "domain": "switch"},
            {"entity_id": "switch.deye_grid_always_on", "label": "Grid Always On", "domain": "switch"},
            {"entity_id": "switch.deye_grid_peak_shaving", "label": "Grid Peak Shaving", "domain": "switch"},
            {"entity_id": "number.deye_grid_peak_shaving_power", "label": "Grid Peak Shaving Power", "domain": "number"},
            {"entity_id": "number.deye_grid_trickle_feed_power", "label": "Grid Trickle Feed Power", "domain": "number"},
        ],
    },
    {
        "id": "mode",
        "title": "Mode and Control",
        "entities": [
            {"entity_id": "switch.deye_inverter_enabled", "label": "Inverter Enabled", "domain": "switch"},
            {"entity_id": "switch.deye_use_timer", "label": "Use Timer", "domain": "switch"},
            {"entity_id": "switch.deye_priority_load", "label": "Priority Load", "domain": "switch"},
            {"entity_id": "switch.deye_solar_export", "label": "Solar Export", "domain": "switch"},
            {"entity_id": "switch.deye_peak_shaving", "label": "Peak Shaving", "domain": "switch"},
            {"entity_id": "switch.deye_gen_peak_shaving", "label": "Gen Peak Shaving", "domain": "switch"},
            {"entity_id": "number.deye_gen_peak_shaving_power", "label": "Gen Peak Shaving Power", "domain": "number"},
            {"entity_id": "select.deye_load_limit", "label": "Load Limit", "domain": "select"},
            {"entity_id": "select.deye_aux_port_usage", "label": "AUX Port Usage", "domain": "select"},
            {"entity_id": "number.deye_max_solar_power", "label": "Max Solar Power", "domain": "number"},
            {"entity_id": "number.deye_max_sell_power", "label": "Max Sell Power", "domain": "number"},
        ],
    },
    {
        "id": "status",
        "title": "Inverter Status",
        "entities": [
            {"entity_id": "binary_sensor.deye_grid_connected", "label": "Grid Connected", "domain": "binary_sensor", "readonly": True},
            {"entity_id": "switch.deye_inverter_enabled", "label": "Inverter Enabled (status)", "domain": "switch", "readonly": True},
            {"entity_id": "switch.deye_near_realtime", "label": "Near Realtime", "domain": "switch"},
        ],
    },
]


async def get_config_sections():
    entity_ids = []
    for sec in CONFIG_SECTIONS:
        for e in sec["entities"]:
            entity_ids.append(e["entity_id"])
    states = await asyncio.gather(*(get_entity_state(eid) for eid in entity_ids))
    by_eid = {st.get("entity_id"): st for st in states}
    # also key by requested if entity_id differs? get_entity_state returns entity_id
    out = []
    for sec in CONFIG_SECTIONS:
        items = []
        for e in sec["entities"]:
            st = by_eid.get(e["entity_id"]) or {"ok": False, "error": "Missing", "entity_id": e["entity_id"], "state": None, "attributes": {}}
            attrs = st.get("attributes") or {}
            items.append({
                **e,
                "ok": st.get("ok"),
                "error": st.get("error"),
                "state": st.get("state"),
                "attributes": attrs,
                "options": attrs.get("options"),
                "min": attrs.get("min"),
                "max": attrs.get("max"),
                "step": attrs.get("step"),
                "unit": attrs.get("unit_of_measurement"),
                "friendly_name": attrs.get("friendly_name"),
            })
        out.append({"id": sec["id"], "title": sec["title"], "entities": items})
    return out



def _tou_time_sort_key(state):
    try:
        h, m = str(state).strip().split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 10**9


def _active_tou_program_n(tou_programs, now=None):
    """Return active TOU program number for current time (server-side at render/save)."""
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    valid = [(p.get("n"), p.get("time_sort")) for p in (tou_programs or []) if isinstance(p.get("time_sort"), int) and p.get("time_sort") < 10**9]
    if not valid:
        return None
    valid.sort(key=lambda x: (x[1], x[0]))
    active = None
    for n, ts in valid:
        if ts <= cur:
            active = n
    if active is None:
        active = valid[-1][0]  # wrapped from last program of previous day
    return active


async def get_tou_programs():
    entity_ids = []
    for n in range(1, 7):
        for _, _, tpl in TOU_PROGRAM_FIELDS:
            entity_ids.append(tpl.format(n=n))
    # fetch all in parallel
    async with httpx.AsyncClient() as client:
        async def fetch(eid):
            if not HA_TOKEN:
                return {"ok": False, "error": "No Supervisor token available", "entity_id": eid}
            try:
                r = await client.get(
                    f"{HA_URL}/api/states/{eid}",
                    headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
                    timeout=10.0,
                )
                if r.status_code == 404:
                    return {"ok": False, "error": "Not found", "entity_id": eid}
                if r.status_code >= 400:
                    return {"ok": False, "error": f"HA API {r.status_code}", "entity_id": eid}
                d = r.json(); a = d.get("attributes") or {}
                return {"ok": True, "entity_id": d.get("entity_id", eid), "state": d.get("state"), "attributes": a}
            except Exception as e:
                return {"ok": False, "error": str(e), "entity_id": eid}
        results = await asyncio.gather(*(fetch(eid) for eid in entity_ids))
    by_eid = {r["entity_id"]: r for r in results}
    programs = []
    for n in range(1, 7):
        fields = {}
        for key, domain, tpl in TOU_PROGRAM_FIELDS:
            eid = tpl.format(n=n)
            st = by_eid.get(eid, {"ok": False, "error": "Missing", "entity_id": eid, "state": None, "attributes": {}})
            attrs = st.get("attributes") or {}
            fields[key] = {
                "key": key, "domain": domain, "entity_id": eid,
                "ok": st.get("ok"), "error": st.get("error"),
                "state": st.get("state"), "attributes": attrs,
                "options": attrs.get("options"),
                "min": attrs.get("min"), "max": attrs.get("max"), "step": attrs.get("step"),
                "unit": attrs.get("unit_of_measurement"),
            }
        time_state = (fields["time"].get("state") or "")
        programs.append({"n": n, "time_state": time_state, "time_sort": _tou_time_sort_key(time_state), "fields": fields})
    programs.sort(key=lambda p: (p["time_sort"], p["n"]))
    return programs


def get_last_applied():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key='last_applied'")
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def get_slots():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM schedule_slots")
    rows = c.fetchall()
    slots = [dict(row) for row in rows]
    conn.close()
    slots.sort(key=_slot_sort_key)
    return slots




def _hhmm_to_min(t):
    h, m = str(t).strip().split(":")
    hh = int(h)
    mm = int(m)
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError(f"Invalid time: {t}")
    return hh * 60 + mm


def _normalize_end_time(end_time):
    et = str(end_time).strip()
    # Reject ambiguous 24:00; require 00:00 for midnight end
    if et == "24:00":
        raise ValueError("Use 00:00 as the end time for midnight (24:00 is not accepted).")
    return et


def _slot_interval(start_time, end_time):
    st = str(start_time).strip()
    et = _normalize_end_time(end_time)
    s = _hhmm_to_min(st)
    if st == "00:00" and et == "00:00":
        return 0, 1440
    if et == "00:00" and st != "00:00":
        return s, 1440
    e = _hhmm_to_min(et)
    return s, e


def _slot_sort_key(sl):
    try:
        s_min, e_min = _slot_interval(sl["start_time"], sl["end_time"])
    except Exception:
        s_min, e_min = 0, 0
    duration = e_min - s_min
    # sort by start asc; if same start, longer duration first (on top)
    # if a disabled one shares same start, longer duration still wins; stable tie-break by id
    try:
        sid = int(sl.get("id") if str(sl.get("id", "")).isdigit() else 0)
    except Exception:
        sid = 0
    return (s_min, -duration, sid)


def _validate_24h_coverage(slots, require_enabled=True):
    """Return (ok, error). If require_enabled, enabled slots must partition [0,1440)."""
    if not slots:
        return False, "No slots configured."
    for sl in slots:
        try:
            s, e = _slot_interval(sl["start_time"], sl["end_time"])
        except Exception as ex:
            msg = str(ex) if str(ex) else f"Invalid time format in slot {sl.get('id','?')}."
            return False, f"Slot {sl.get('id','?')}: {msg}"
        if e <= s:
            return False, (
                f"Slot {sl.get('id','?')}: end time must be after start time. "
                "Use 00:00 as the end for midnight; cross-midnight ranges must be split into two slots (e.g. 22:00–00:00 and 00:00–06:00)."
            )
        p = float(sl["power_kw"])
        if p < 0 or p > 16:
            return False, f"Slot {sl.get('id','?')}: power must be 0–16 kW."
        try:
            dew = float(sl.get("default_export_w", 0) or 0)
            if dew < 0 or dew > 16000:
                return False, f"Slot {sl.get('id','?')}: default export must be 0–16000 W."
        except Exception:
            return False, f"Slot {sl.get('id','?')}: invalid default export."
        try:
            bsp = float(sl.get("battery_soc_pct", 0) or 0)
            if bsp < 0 or bsp > 100:
                return False, f"Slot {sl.get('id','?')}: battery % must be 0–100."
        except Exception:
            return False, f"Slot {sl.get('id','?')}: invalid battery %."
    active = [sl for sl in slots if sl.get("enabled")]
    if require_enabled and not active:
        return False, "At least one enabled slot is required."
    if not require_enabled:
        active = list(slots)
    intervals = []
    for sl in active:
        s, e = _slot_interval(sl["start_time"], sl["end_time"])
        intervals.append((s, e, sl.get("id")))
    intervals.sort(key=lambda x: (x[0], -x[1], x[2]))
    if intervals[0][0] != 0:
        return False, f"Schedule must start at 00:00 (first enabled slot starts at {intervals[0][0]//60:02d}:{intervals[0][0]%60:02d})."
    cur = 0
    for s, e, sid in intervals:
        if s != cur:
            if s < cur:
                return False, f"Slots overlap or are out of order around slot {sid} ({s//60:02d}:{s%60:02d})."
            return False, f"Gap in schedule: {cur//60:02d}:{cur%60:02d}–{s//60:02d}:{s%60:02d} is not covered."
        cur = e
    if cur != 1440:
        return False, f"Schedule must cover until 24:00/00:00 (ends at {cur//60:02d}:{cur%60:02d}). Use 00:00 as end time for midnight."
    return True, None

def expected_power_now(slots, now=None):
    """Return (configured_power_kw, active_slot) based on time only (no SOC)."""
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    for slot in slots:
        if not slot.get("enabled"):
            continue
        try:
            s, e = _slot_interval(slot["start_time"], slot["end_time"])
        except Exception:
            continue
        if s <= cur < e:
            return float(slot["power_kw"]), slot
    return 0.0, None


def _effective_export_w_for_slot(slot, soc):
    """Compute effective export watts for a slot given SOC (float) or None if unavailable.
    Rule (re-evaluated each tick, not latched):
      SOC > battery_soc_pct  -> slot Power (W)
      SOC <= battery_soc_pct -> Default export (W)
      SOC unavailable        -> Default export (W) (safe fallback)
      SOC out of range       -> Default export (W) (safe fallback)
    """
    default_w = int(round(float(slot.get("default_export_w", 0) or 0)))
    default_w = max(0, min(16000, default_w))
    power_w = int(round(float(slot.get("power_kw", 0) or 0) * 1000))
    power_w = max(0, min(16000, power_w))
    try:
        threshold = float(slot.get("battery_soc_pct", 0) or 0)
    except Exception:
        threshold = 0.0
    threshold = max(0.0, min(100.0, threshold))
    if soc is None:
        return default_w, "soc_unavailable"
    try:
        soc_v = float(soc)
    except Exception:
        return default_w, "soc_invalid"
    if soc_v < 0 or soc_v > 100:
        return default_w, "soc_out_of_range"
    if soc_v > threshold:
        return power_w, "soc_above_threshold"
    return default_w, "soc_at_or_below_threshold"


async def effective_expected_now(slots, now=None):
    """Return SOC-aware expected export info for current time."""
    configured_kw, active_slot = expected_power_now(slots, now=now)
    soc_status = await get_battery_soc()
    soc = soc_status.get("soc") if soc_status.get("ok") else None
    if active_slot:
        effective_w, reason = _effective_export_w_for_slot(active_slot, soc)
    else:
        effective_w, reason = 0, "no_active_slot"
    return {
        "configured_kw": round(float(configured_kw), 3),
        "effective_w": int(effective_w),
        "effective_kw": round(int(effective_w) / 1000.0, 3),
        "active_slot": active_slot,
        "soc_status": soc_status,
        "battery_entity": BATTERY_PERCENTAGE,
        "reason": reason,
    }


async def _compute_and_maybe_get_apply_soc():
    """SOC-aware: compute effective export watts for active slot, apply if changed."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now = datetime.now()
    cur = now.hour * 60 + now.minute

    c.execute("SELECT * FROM schedule_slots WHERE enabled=1")
    slots = [dict(r) for r in c.fetchall()]
    slots.sort(key=_slot_sort_key)

    active_slot = None
    for slot in slots:
        try:
            s_min, e_min = _slot_interval(slot["start_time"], slot["end_time"])
        except Exception:
            continue
        if s_min <= cur < e_min:
            active_slot = slot
            break

    soc_status = await get_battery_soc()
    soc = soc_status.get("soc") if soc_status.get("ok") else None
    if active_slot:
        active_power, reason = _effective_export_w_for_slot(active_slot, soc)
    else:
        active_power, reason = 0, "no_active_slot"

    c.execute("SELECT value FROM settings WHERE key='last_applied'")
    row = c.fetchone()
    last_applied = int(row["value"]) if row else -1
    changed = active_power != last_applied
    if changed:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_applied', ?)", (str(active_power),))
        conn.commit()
    conn.close()
    return active_power, changed, {
        "active_slot": active_slot,
        "soc_status": soc_status,
        "reason": reason,
        "effective_w": active_power,
    }


def check_and_apply_schedule():
    """Called by APScheduler from a background thread."""
    try:
        active_power, changed, _info = asyncio.run(_compute_and_maybe_get_apply_soc())
        if changed:
            asyncio.run(apply_to_ha(active_power))
    except Exception as e:
        print(f"Error applying scheduled power (SOC-aware): {e}")

scheduler = BackgroundScheduler()
scheduler.add_job(check_and_apply_schedule, "interval", minutes=1)
scheduler.start()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    global STARTUP_CHECK_DONE
    slots = get_slots()
    template = env.get_template("index.html")
    root_path = request.scope.get("root_path", "")
    ha_status = await get_ha_entity_state()
    last_applied = get_last_applied()
    eff = await effective_expected_now(slots)
    expected_kw = eff["effective_kw"]  # SOC-effective for display/mismatch/apply context
    configured_kw = eff["configured_kw"]
    active_slot = eff["active_slot"]
    soc_status = eff["soc_status"]
    now = datetime.now()
    mismatch = None
    # Show mismatch prompt on first load after restart, or whenever values differ (clear and actionable)
    if ha_status.get("ok") and ha_status.get("power_kw") is not None:
        inv_kw = float(ha_status["power_kw"])
        if abs(inv_kw - expected_kw) > 0.011:
            mismatch = {
                "inverter_kw": round(inv_kw, 3),
                "schedule_kw": round(expected_kw, 3),
                "configured_kw": round(configured_kw, 3),
                "current_time": now.strftime("%H:%M"),
                "active_slot": active_slot,
                "startup_check": not STARTUP_CHECK_DONE,
                "soc_status": soc_status,
                "reason": eff["reason"],
            }
    STARTUP_CHECK_DONE = True
    coverage_ok, coverage_error = _validate_24h_coverage(slots, require_enabled=True) if slots else (False, "No slots configured.")
    saved = request.query_params.get("saved")
    applied = request.query_params.get("applied")
    save_error = request.query_params.get("error")
    tab = (request.query_params.get("tab") or "overview").strip().lower()
    if tab not in ("overview", "export", "tou", "config"):
        tab = "overview"
    tou_programs = await get_tou_programs()
    active_tou_n = _active_tou_program_n(tou_programs, now=now)
    active_slot_id = (active_slot or {}).get("id") if active_slot else None
    tou_saved = request.query_params.get("tou_saved")
    tou_error = request.query_params.get("tou_error")
    config_sections = await get_config_sections()
    config_saved = request.query_params.get("config_saved")
    config_error = request.query_params.get("config_error")
    if tab == "overview":
        try:
            overview_data = await get_overview_data()
        except Exception as e:
            overview_data = {"ok": False, "error": str(e), "groups": [], "graphs": [], "now": now.strftime("%Y-%m-%d %H:%M:%S")}
    else:
        overview_data = {"ok": True, "groups": [], "graphs": [], "now": now.strftime("%Y-%m-%d %H:%M:%S")}
    return HTMLResponse(template.render(
        request=request,
        slots=slots,
        root_path=root_path,
        ha_status=ha_status,
        deye_entity=DEYE_ENTITY,
        battery_percentage=BATTERY_PERCENTAGE,
        ha_url=HA_URL,
        last_applied=last_applied,
        db_path=DB_PATH,
        expected_kw=round(expected_kw, 3),
        configured_kw=round(configured_kw, 3),
        active_slot=active_slot,
        active_slot_id=active_slot_id,
        effective_w=eff["effective_w"],
        soc_status=soc_status,
        soc_reason=eff["reason"],
        mismatch=mismatch,
        now_str=now.strftime("%Y-%m-%d %H:%M:%S"),
        coverage_ok=coverage_ok,
        coverage_error=coverage_error,
        saved=saved,
        applied=applied,
        save_error=save_error,
        tab=tab,
        tou_programs=tou_programs,
        active_tou_n=active_tou_n,
        tou_saved=tou_saved,
        tou_error=tou_error,
        config_sections=config_sections,
        config_saved=config_saved,
        config_error=config_error,
        overview_data=overview_data,
    ))

@app.post("/add_slot")
async def add_slot(request: Request, start_time: str = Form(...), end_time: str = Form(...), power_w: float = Form(...)):
    try:
        s_min, e_min = _slot_interval(start_time, end_time)
        if e_min <= s_min:
            raise ValueError("end time must be after start time. Use 00:00 for midnight end; split cross-midnight ranges into two slots.")
    except Exception as ex:
        detail = str(ex) or "Invalid time range. Use HH:MM; for midnight end use 00:00."
        raise HTTPException(status_code=400, detail=detail)
    power_w = max(0.0, min(16000.0, float(power_w)))
    power_kw = round(power_w / 1000.0, 3)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO schedule_slots (start_time, end_time, power_kw) VALUES (?, ?, ?)",
              (start_time, end_time, power_kw))
    conn.commit()
    conn.close()
    root_path = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{root_path}/?tab=export", status_code=303)

@app.post("/delete_slot/{slot_id}")
async def delete_slot(request: Request, slot_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM schedule_slots WHERE id=?", (slot_id,))
    conn.commit()
    conn.close()
    root_path = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{root_path}/?tab=export", status_code=303)

@app.post("/toggle_slot/{slot_id}")
async def toggle_slot(request: Request, slot_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE schedule_slots SET enabled = 1 - enabled WHERE id=?", (slot_id,))
    conn.commit()
    conn.close()
    root_path = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{root_path}/?tab=export", status_code=303)


@app.post("/update_slot/{slot_id}")
async def update_slot(request: Request, slot_id: int, start_time: str = Form(...), end_time: str = Form(...), power_w: float = Form(...), enabled: str = Form(None)):
    try:
        s_min, e_min = _slot_interval(start_time, end_time)
        if e_min <= s_min:
            raise ValueError("end time must be after start time. Use 00:00 for midnight end; split cross-midnight ranges into two slots.")
    except Exception as ex:
        detail = str(ex) or "Invalid time range. Use HH:MM; for midnight end use 00:00."
        raise HTTPException(status_code=400, detail=detail)
    power_w = max(0.0, min(16000.0, float(power_w)))
    power_kw = round(power_w / 1000.0, 3)
    en = 1 if enabled is not None else 0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE schedule_slots SET start_time=?, end_time=?, power_kw=?, enabled=? WHERE id=?",
              (start_time, end_time, power_kw, en, slot_id))
    conn.commit()
    conn.close()
    root_path = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{root_path}/?tab=export", status_code=303)



@app.post("/save_slots")
async def save_slots(request: Request):
    root_path = request.scope.get("root_path", "")
    form = await request.form()
    ids = form.getlist("id")
    starts = form.getlist("start_time")
    ends = form.getlist("end_time")
    powers = form.getlist("power_w")
    if not powers:
        powers = form.getlist("power_kw")  # fallback
    default_exports = form.getlist("default_export_w")
    battery_pcts = form.getlist("battery_soc_pct")
    enableds = form.getlist("enabled")
    apply_now_requested = str(form.get("apply_now") or request.query_params.get("apply_now") or "").lower() in ("1", "true", "on", "yes")
    # back-compat if columns missing in old form posts
    if len(default_exports) != len(ids):
        default_exports = ["0"] * len(ids)
    if len(battery_pcts) != len(ids):
        battery_pcts = ["0"] * len(ids)
    if not (len(ids) == len(starts) == len(ends) == len(powers) == len(enableds)):
        from urllib.parse import quote
        return RedirectResponse(url=f"{root_path}/?tab=export&error={quote('Malformed form submission.')}", status_code=303)
    slots = []
    for i, sid in enumerate(ids):
        try:
            raw_id = str(sid)
            is_new = raw_id.startswith("new") or raw_id == "" or raw_id == "0"
            sl = {
                "id": raw_id,
                "db_id": None if is_new else int(raw_id),
                "is_new": is_new,
                "start_time": str(starts[i]),
                "end_time": str(ends[i]),
                "power_kw": round(max(0.0, min(16000.0, float(powers[i]))) / 1000.0, 3),
                "default_export_w": int(round(max(0.0, min(16000.0, float(default_exports[i] or 0))))),
                "battery_soc_pct": int(round(max(0.0, min(100.0, float(battery_pcts[i] or 0))))),
                "enabled": 1 if str(enableds[i]) in ("1", "true", "True", "on", "yes") else 0,
            }
        except Exception:
            from urllib.parse import quote
            return RedirectResponse(url=f"{root_path}/?tab=export&error={quote(f'Invalid values in row {i+1}.')}", status_code=303)
        slots.append(sl)
    ok, err = _validate_24h_coverage(slots, require_enabled=True)
    if not ok:
        from urllib.parse import quote
        return RedirectResponse(url=f"{root_path}/?tab=export&error={quote(err)}", status_code=303)
    from urllib.parse import quote
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        for sl in slots:
            if sl["is_new"]:
                c.execute(
                    "INSERT INTO schedule_slots (start_time, end_time, power_kw, default_export_w, battery_soc_pct, enabled) VALUES (?, ?, ?, ?, ?, ?)",
                    (sl["start_time"], sl["end_time"], max(0.0, min(16.0, sl["power_kw"])), sl.get("default_export_w", 0), sl.get("battery_soc_pct", 0), sl["enabled"]),
                )
            else:
                c.execute(
                    "UPDATE schedule_slots SET start_time=?, end_time=?, power_kw=?, default_export_w=?, battery_soc_pct=?, enabled=? WHERE id=?",
                    (sl["start_time"], sl["end_time"], max(0.0, min(16.0, sl["power_kw"])), sl.get("default_export_w", 0), sl.get("battery_soc_pct", 0), sl["enabled"], sl["db_id"]),
                )
                if c.rowcount == 0:
                    conn.rollback()
                    return RedirectResponse(
                        url=f"{root_path}/?tab=export&error={quote('Row with id ' + str(sl['db_id']) + ' no longer exists. Reload the page and try saving again.')}",
                        status_code=303,
                    )
        conn.commit()
    finally:
        conn.close()
    if apply_now_requested:
        active_power, changed, _info = await _compute_and_maybe_get_apply_soc()
        if changed:
            await apply_to_ha(active_power)
        return RedirectResponse(url=f"{root_path}/?tab=export&saved=1&applied=1", status_code=303)
    return RedirectResponse(url=f"{root_path}/?tab=export&saved=1", status_code=303)

@app.get("/api/slots")
async def api_slots():
    slots = get_slots()
    return JSONResponse(slots)


@app.post("/resolve_mismatch")
async def resolve_mismatch(request: Request, master: str = Form(...)):
    root_path = request.scope.get("root_path", "")
    slots = get_slots()
    ha_status = await get_ha_entity_state()
    expected_kw, active_slot = expected_power_now(slots)
    if master == "schedule":
        eff = await effective_expected_now(slots)
        watts = int(eff["effective_w"])
        await apply_to_ha(watts)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_applied', ?)", (str(watts),))
        conn.commit()
        conn.close()
    elif master == "inverter":
        if not (ha_status.get("ok") and ha_status.get("power_kw") is not None):
            return RedirectResponse(url=f"{root_path}/?tab=export", status_code=303)
        inv_kw = max(0.0, min(16.0, round(float(ha_status["power_kw"]), 2)))
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM schedule_slots WHERE enabled=1")
        enabled = [dict(r) for r in c.fetchall()]
        enabled.sort(key=_slot_sort_key)
        cur = datetime.now().hour * 60 + datetime.now().minute
        covered = None
        for sl in enabled:
            try:
                si, ei = _slot_interval(sl["start_time"], sl["end_time"])
            except Exception:
                continue
            if si <= cur < ei:
                covered = sl
                break
        if covered:
            c.execute("UPDATE schedule_slots SET power_kw=? WHERE id=?", (inv_kw, covered["id"]))
        else:
            c.execute("INSERT INTO schedule_slots (start_time, end_time, power_kw, enabled) VALUES ('00:00','00:00',?,1)", (inv_kw,))
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_applied', ?)", (str(int(round(inv_kw * 1000))),))
        conn.commit()
        conn.close()
    return RedirectResponse(url=f"{root_path}/?tab=export", status_code=303)


@app.get("/api/status")
async def api_status():
    slots = get_slots()
    ha_status = await get_ha_entity_state()
    eff = await effective_expected_now(slots)
    return JSONResponse({
        "deye_entity": DEYE_ENTITY,
        "battery_percentage": BATTERY_PERCENTAGE,
        "ha_url": HA_URL,
        "ha_status": ha_status,
        "soc_status": eff["soc_status"],
        "last_applied_watts": get_last_applied(),
        "expected_kw_now": eff["effective_kw"],
        "configured_kw_now": eff["configured_kw"],
        "effective_w_now": eff["effective_w"],
        "soc_reason": eff["reason"],
        "active_slot": eff["active_slot"],
        "slots_count": len(slots),
        "db_path": DB_PATH,
    })


@app.post("/api/apply_now")
async def api_apply_now(request: Request):
    active_power, changed, _info = await _compute_and_maybe_get_apply_soc()
    if changed:
        await apply_to_ha(active_power)
    root_path = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{root_path}/?tab=export", status_code=303)


@app.post("/sync_from_inverter")
async def sync_from_inverter(request: Request):
    """If no slots exist, create a 00:00-23:59 baseline slot from current inverter power."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM schedule_slots")
    n = c.fetchone()["n"]
    if n == 0:
        ha = await get_ha_entity_state()
        power_kw = 0.0
        if ha and ha.get("ok") and ha.get("power_kw") is not None:
            try:
                power_kw = max(0.0, min(16.0, round(float(ha["power_kw"]), 2)))
            except Exception:
                power_kw = 0.0
        c.execute(
            "INSERT INTO schedule_slots (start_time, end_time, power_kw, enabled) VALUES (?, ?, ?, 1)",
            ("00:00", "00:00", power_kw),
        )
        conn.commit()
    conn.close()
    root_path = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{root_path}/?tab=export", status_code=303)



@app.post("/tou/save_program/{n}")
async def tou_save_program(request: Request, n: int, time: str = Form(""), power: str = Form(""), capacity: str = Form(""), charge: str = Form(""), mode: str = Form(""), voltage: str = Form(""), sell: str = Form("off")):
    root_path = request.scope.get("root_path", "")
    from urllib.parse import quote
    if n < 1 or n > 6:
        return RedirectResponse(url=f"{root_path}/?tab=tou&tou_error={quote('Invalid program.')}", status_code=303)
    time_e = f"select.deye_prog{n}_time"
    charge_e = f"select.deye_prog{n}_charge"
    mode_e = f"select.deye_prog{n}_mode"
    power_e = f"number.deye_prog{n}_power"
    cap_e = f"number.deye_prog{n}_capacity"
    volt_e = f"number.deye_prog{n}_voltage"
    sell_e = f"switch.deye_prog{n}_sell"
    states = await asyncio.gather(*(get_entity_state(e) for e in [time_e, charge_e, mode_e, power_e, cap_e, volt_e, sell_e]))
    st_time, st_charge, st_mode, st_power, st_cap, st_volt, _st_sell = states
    def opts(st):
        a = st.get("attributes") or {}
        return a.get("options") or []

    def validate_select(label, value, st):
        if value is None or str(value).strip() == "":
            return f"{label}: missing value"
        # Fail closed when the HA entity/options cannot be verified
        if not st.get("ok"):
            return f"{label}: cannot verify options because entity state could not be read ({st.get('error') or 'unavailable'})"
        options = opts(st)
        if not options:
            return f"{label}: cannot verify options (no options reported by Home Assistant)"
        if value not in options:
            return f"{label}: invalid option '{value}'"
        return None

    def parse_num(raw, st):
        if raw is None or str(raw).strip() == "":
            return None, "missing value"
        try:
            v = float(raw)
        except Exception:
            return None, "not a number"
        a = st.get("attributes") or {}
        mn = a.get("min"); mx = a.get("max")
        if mn is not None: v = max(float(mn), v)
        if mx is not None: v = min(float(mx), v)
        return v, None

    for label, value, st in [
        ("time", time, st_time),
        ("grid-charge", charge, st_charge),
        ("mode", mode, st_mode),
    ]:
        err = validate_select(label, value, st)
        if err:
            return RedirectResponse(url=f"{root_path}/?tab=tou&tou_error={quote(f'Prog {n}: {err}')}", status_code=303)

    power_v, e = parse_num(power, st_power)
    if e: return RedirectResponse(url=f"{root_path}/?tab=tou&tou_error={quote(f'Prog {n}: power {e}.')}", status_code=303)
    cap_v, e = parse_num(capacity, st_cap)
    if e: return RedirectResponse(url=f"{root_path}/?tab=tou&tou_error={quote(f'Prog {n}: capacity {e}.')}", status_code=303)
    volt_v, e = parse_num(voltage, st_volt)
    if e: return RedirectResponse(url=f"{root_path}/?tab=tou&tou_error={quote(f'Prog {n}: voltage {e}.')}", status_code=303)
    sell_on = str(sell).lower() in ("on", "1", "true", "yes")
    calls = [
        ("select", "select_option", {"entity_id": time_e, "option": time}),
        ("number", "set_value", {"entity_id": power_e, "value": power_v}),
        ("number", "set_value", {"entity_id": cap_e, "value": cap_v}),
        ("select", "select_option", {"entity_id": charge_e, "option": charge}),
        ("select", "select_option", {"entity_id": mode_e, "option": mode}),
        ("number", "set_value", {"entity_id": volt_e, "value": volt_v}),
        ("switch", "turn_on" if sell_on else "turn_off", {"entity_id": sell_e}),
    ]
    applied = []
    errors = []
    for dom, svc, payload in calls:
        ok, err = await ha_call_service(dom, svc, payload)
        eid = payload["entity_id"]
        if ok:
            applied.append(eid)
        else:
            errors.append(f"{eid}: {err}")
    if errors:
        msg = (
            f"Prog {n} sync incomplete (HA service calls are not atomic). "
            f"Applied: {', '.join(applied) if applied else 'none'}. "
            f"Failed: {'; '.join(errors)}"
        )
        return RedirectResponse(url=f"{root_path}/?tab=tou&tou_error={quote(msg[:700])}", status_code=303)
    return RedirectResponse(url=f"{root_path}/?tab=tou&tou_saved={n}", status_code=303)



@app.post("/config/apply")
async def config_apply(request: Request, entity_id: str = Form(...), value: str = Form("")):
    root_path = request.scope.get("root_path", "")
    from urllib.parse import quote
    # Allowlist: only entities defined in Configuration tab
    allowed = {}
    for sec in CONFIG_SECTIONS:
        for e in sec["entities"]:
            allowed[e["entity_id"]] = e
    meta = allowed.get(entity_id)
    if not meta:
        return RedirectResponse(url=f"{root_path}/?tab=config&config_error={quote('Entity not allowed for Configuration tab.')}", status_code=303)
    if meta.get("readonly"):
        return RedirectResponse(url=f"{root_path}/?tab=config&config_error={quote(entity_id + ': read-only status field.')}", status_code=303)
    domain = meta.get("domain") or entity_id.split(".", 1)[0]
    st = await get_entity_state(entity_id)

    if domain == "switch" or entity_id.startswith("switch."):
        on = str(value).lower() in ("on", "1", "true", "yes")
        ok, err = await ha_call_service("switch", "turn_on" if on else "turn_off", {"entity_id": entity_id})
        new_label = "on" if on else "off"
    elif domain == "select" or entity_id.startswith("select."):
        if value is None or str(value).strip() == "":
            return RedirectResponse(url=f"{root_path}/?tab=config&config_error={quote(entity_id + ': missing value')}", status_code=303)
        # Fail closed when options cannot be verified
        if not st.get("ok"):
            return RedirectResponse(url=f"{root_path}/?tab=config&config_error={quote(entity_id + ': cannot verify options because entity state could not be read (' + str(st.get('error') or 'unavailable') + ')')}", status_code=303)
        options = (st.get("attributes") or {}).get("options") or []
        if not options:
            return RedirectResponse(url=f"{root_path}/?tab=config&config_error={quote(entity_id + ': cannot verify options (no options reported by Home Assistant)')}", status_code=303)
        if value not in options:
            return RedirectResponse(url=f"{root_path}/?tab=config&config_error={quote(entity_id + ': invalid option')}", status_code=303)
        ok, err = await ha_call_service("select", "select_option", {"entity_id": entity_id, "option": value})
        new_label = value
    elif domain == "number" or entity_id.startswith("number."):
        try:
            v = float(value)
        except Exception:
            return RedirectResponse(url=f"{root_path}/?tab=config&config_error={quote(entity_id + ': value must be a number')}", status_code=303)
        a = st.get("attributes") or {}
        if st.get("ok"):
            if a.get("min") is not None: v = max(float(a.get("min")), v)
            if a.get("max") is not None: v = min(float(a.get("max")), v)
        ok, err = await ha_call_service("number", "set_value", {"entity_id": entity_id, "value": v})
        new_label = str(v)
    else:
        return RedirectResponse(url=f"{root_path}/?tab=config&config_error={quote(entity_id + ': unsupported domain')}", status_code=303)

    if not ok:
        return RedirectResponse(url=f"{root_path}/?tab=config&config_error={quote(entity_id + ': ' + str(err))}", status_code=303)
    label = meta.get("label") or entity_id
    return RedirectResponse(url=f"{root_path}/?tab=config&config_saved={quote(label + ' = ' + new_label)}", status_code=303)



# ---------------- Overview / history (vitals + graphs) ----------------

OVERVIEW_VITALS = [
    {"id": "status", "title": "Status & Battery", "entities": [
        {"key": "grid_connected", "entity_id": "binary_sensor.deye_grid_connected", "label": "Grid Connected"},
        {"key": "battery_soc", "entity_id": BATTERY_PERCENTAGE, "label": "Battery SOC"},
        {"key": "battery_power", "entity_id": "sensor.deye_battery_power", "label": "Battery Power"},
        {"key": "battery_voltage", "entity_id": "sensor.deye_battery_voltage", "label": "Battery Voltage"},
        {"key": "battery_current", "entity_id": "sensor.deye_battery_current", "label": "Battery Current"},
        {"key": "battery_temperature", "entity_id": "sensor.deye_battery_temperature", "label": "Battery Temperature"},
    ]},
    {"id": "power", "title": "Power Flow", "entities": [
        {"key": "pv_power", "entity_id": "sensor.deye_pv_power", "label": "PV Power"},
        {"key": "grid_power", "entity_id": "sensor.deye_grid_power", "label": "Grid Power"},
        {"key": "load_power", "entity_id": "sensor.deye_load_power", "label": "Load Power"},
        {"key": "essential_power", "entity_id": "sensor.deye_essential_power", "label": "Essential Power"},
        {"key": "grid_ct_power", "entity_id": "sensor.deye_grid_ct_power", "label": "Grid CT Power"},
    ]},
    {"id": "grid", "title": "Grid & Inverter", "entities": [
        {"key": "grid_voltage", "entity_id": "sensor.deye_grid_voltage", "label": "Grid Voltage"},
        {"key": "grid_frequency", "entity_id": "sensor.deye_grid_frequency", "label": "Grid Frequency"},
        {"key": "radiator_temperature", "entity_id": "sensor.deye_radiator_temperature", "label": "Radiator Temp"},
        {"key": "dc_transformer_temperature", "entity_id": "sensor.deye_dc_transformer_temperature", "label": "DC Transformer Temp"},
    ]},
    {"id": "energy", "title": "Today / Energy", "entities": [
        {"key": "day_pv_energy", "entity_id": "sensor.deye_day_pv_energy", "label": "Day PV Energy"},
        {"key": "day_load_energy", "entity_id": "sensor.deye_day_load_energy", "label": "Day Load Energy"},
        {"key": "day_grid_import", "entity_id": "sensor.deye_day_grid_import", "label": "Day Grid Import"},
        {"key": "day_grid_export", "entity_id": "sensor.deye_day_grid_export", "label": "Day Grid Export"},
        {"key": "day_battery_charge", "entity_id": "sensor.deye_day_battery_charge", "label": "Day Battery Charge"},
        {"key": "day_battery_discharge", "entity_id": "sensor.deye_day_battery_discharge", "label": "Day Battery Discharge"},
        {"key": "total_pv_energy", "entity_id": "sensor.deye_total_pv_energy", "label": "Total PV Energy"},
    ]},
]

OVERVIEW_GRAPH_SERIES = [
    {"id": "power_flow", "title": "Power Flow", "unit": "W", "type": "line", "entities": [
        ("sensor.deye_pv_power", "PV"),
        ("sensor.deye_grid_power", "Grid"),
        ("sensor.deye_load_power", "Load"),
        ("sensor.deye_battery_power", "Battery"),
        ("sensor.deye_essential_power", "Essential"),
        ("sensor.deye_grid_ct_power", "Grid CT"),
    ]},
    {"id": "soc", "title": "Battery SOC", "unit": "%", "type": "line", "entities": [
        (BATTERY_PERCENTAGE, "SOC"),
    ]},
    {"id": "battery_electrical", "title": "Battery Voltage / Current", "unit": "", "type": "line", "y2": True, "entities": [
        ("sensor.deye_battery_voltage", "Voltage (V)", "y"),
        ("sensor.deye_battery_current", "Current (A)", "y1"),
    ]},
    {"id": "grid_quality", "title": "Grid Voltage / Frequency", "unit": "", "type": "line", "y2": True, "entities": [
        ("sensor.deye_grid_voltage", "Voltage (V)", "y"),
        ("sensor.deye_grid_frequency", "Frequency (Hz)", "y1"),
    ]},
    {"id": "temperatures", "title": "Temperatures", "unit": "°C", "type": "line", "entities": [
        ("sensor.deye_battery_temperature", "Battery"),
        ("sensor.deye_radiator_temperature", "Radiator"),
    ]},
]

ALLOWED_HISTORY_ENTITIES = {eid for g in OVERVIEW_GRAPH_SERIES for (eid, *_) in g["entities"]}
ALLOWED_HISTORY_ENTITIES.update({e["entity_id"] for g in OVERVIEW_VITALS for e in g["entities"]})


def _coerce_history_value(state):
    if state in (None, "unknown", "unavailable", ""):
        return None
    try:
        v = float(state)
        # filter obvious bogus sentinel temps in some integrations
        if abs(v) > 100000:
            return None
        return v
    except Exception:
        return None


async def get_ha_history(entity_ids, start_dt, end_dt=None):
    if not HA_TOKEN:
        return {}
    ids = [e for e in entity_ids if e in ALLOWED_HISTORY_ENTITIES]
    if not ids:
        return {}
    params = {
        "filter_entity_id": ",".join(ids),
        "minimal_response": "1",
        "significant_changes_only": "0",
    }
    if end_dt:
        params["end_time"] = end_dt.isoformat()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{HA_URL}/api/history/period/{start_dt.isoformat()}",
                headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
                params=params,
                timeout=30.0,
            )
            if response.status_code >= 400:
                return {"_error": f"HA history API error {response.status_code}"}
            data = response.json() or []
    except Exception as e:
        return {"_error": str(e)}
    out = {}
    for series in data:
        if not series:
            continue
        eid = series[0].get("entity_id") or (series[-1].get("entity_id") if series else None)
        if not eid:
            continue
        points = []
        for item in series:
            ts = item.get("last_changed") or item.get("last_updated")
            val = _coerce_history_value(item.get("state"))
            if ts and val is not None:
                points.append([ts, val])
        # downsample
        max_points = 240
        if len(points) > max_points:
            step = max(1, len(points) // max_points)
            points = points[::step]
            if points and len(points) > max_points:
                points = points[-max_points:]
        out[eid] = points
    return out


async def get_overview_data():
    # fetch all vital entities
    entity_ids = []
    for g in OVERVIEW_VITALS:
        for e in g["entities"]:
            entity_ids.append(e["entity_id"])
    entity_ids = list(dict.fromkeys(entity_ids))
    states = await asyncio.gather(*(get_entity_state(eid) for eid in entity_ids))
    by = {st.get("entity_id"): st for st in states}
    groups = []
    for g in OVERVIEW_VITALS:
        items = []
        for e in g["entities"]:
            st = by.get(e["entity_id"]) or {"ok": False, "error": "Missing", "entity_id": e["entity_id"], "state": None, "attributes": {}}
            items.append({
                "key": e["key"], "label": e["label"], "entity_id": e["entity_id"],
                "ok": st.get("ok"), "state": st.get("state"), "error": st.get("error"),
                "unit": st.get("unit"), "friendly_name": st.get("friendly_name"),
            })
        groups.append({"id": g["id"], "title": g["title"], "items": items})
    # resolve graph series availability
    graphs = []
    for g in OVERVIEW_GRAPH_SERIES:
        series = []
        for ent in g["entities"]:
            eid = ent[0]; label = ent[1]; axis = ent[2] if len(ent) > 2 else "y"
            st = by.get(eid)
            if st and st.get("ok"):
                series.append({"entity_id": eid, "label": label, "axis": axis})
        if series:
            gg = {k: v for k, v in g.items() if k != "entities"}
            gg["series"] = series
            graphs.append(gg)
    return {"ok": True, "groups": groups, "graphs": graphs, "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/api/overview")
async def api_overview():
    try:
        return JSONResponse(await get_overview_data())
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "groups": [], "graphs": [], "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, status_code=200)


@app.get("/api/history")
async def api_history(request: Request, hours: int = 24):
    try:
        hours = max(1, min(168, int(hours)))
    except Exception:
        hours = 24
    # gather entities for graphs (available ones will be requested client-side too, but return all allowed relevant)
    ids = list(ALLOWED_HISTORY_ENTITIES)
    start_dt = datetime.now().astimezone() - __import__('datetime').timedelta(hours=hours)
    end_dt = datetime.now().astimezone()
    data = await get_ha_history(ids, start_dt, end_dt)
    return JSONResponse({"ok": "_error" not in data, "hours": hours, "series": data})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8099))
    uvicorn.run(app, host="0.0.0.0", port=port)
