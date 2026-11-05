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

app = FastAPI(title="Deye Export Manager")

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
    c.execute("SELECT * FROM schedule_slots ORDER BY start_time")
    rows = c.fetchall()
    slots = [dict(row) for row in rows]
    conn.close()
    return slots




def _hhmm_to_min(t):
    h, m = t.strip().split(":")
    return int(h) * 60 + int(m)


def _slot_interval(start_time, end_time):
    s = _hhmm_to_min(start_time)
    e = _hhmm_to_min(end_time)
    if start_time == "00:00" and end_time == "00:00":
        return 0, 1440
    if end_time == "00:00" and start_time != "00:00":
        e = 1440
    return s, e


def _validate_24h_coverage(slots, require_enabled=True):
    """Return (ok, error). If require_enabled, enabled slots must partition [0,1440)."""
    if not slots:
        return False, "No slots configured."
    for sl in slots:
        try:
            s, e = _slot_interval(sl["start_time"], sl["end_time"])
        except Exception:
            return False, f"Invalid time format in slot {sl.get('id','?')}."
        if e <= s:
            return False, f"Slot {sl.get('id','?')}: end time must be after start time (use 00:00 as end for midnight)."
        p = float(sl["power_kw"])
        if p < 0 or p > 16:
            return False, f"Slot {sl.get('id','?')}: power must be 0–16 kW."
    active = [sl for sl in slots if sl.get("enabled")]
    if require_enabled and not active:
        return False, "At least one enabled slot is required."
    if not require_enabled:
        active = list(slots)
    intervals = []
    for sl in active:
        s, e = _slot_interval(sl["start_time"], sl["end_time"])
        intervals.append((s, e, sl.get("id")))
    intervals.sort()
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


def _compute_and_maybe_get_apply():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now = datetime.now()
    cur = now.hour * 60 + now.minute

    c.execute("SELECT * FROM schedule_slots WHERE enabled=1 ORDER BY start_time")
    slots = c.fetchall()

    active_power = 0
    for slot in slots:
        try:
            s, e = _slot_interval(slot["start_time"], slot["end_time"])
        except Exception:
            continue
        if s <= cur < e:
            active_power = int(float(slot["power_kw"]) * 1000)
            break

    c.execute("SELECT value FROM settings WHERE key='last_applied'")
    row = c.fetchone()
    last_applied = int(row["value"]) if row else -1
    changed = active_power != last_applied
    if changed:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_applied', ?)", (str(active_power),))
        conn.commit()
    conn.close()
    return active_power, changed


def check_and_apply_schedule():
    """Called by APScheduler from a background thread."""
    active_power, changed = _compute_and_maybe_get_apply()
    if changed:
        try:
            asyncio.run(apply_to_ha(active_power))
        except Exception as e:
            print(f"Error applying scheduled power: {e}")

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
    expected_kw, active_slot = expected_power_now(slots)
    now = datetime.now()
    mismatch = None
    # Show mismatch prompt on first load after restart, or whenever values differ (clear and actionable)
    if ha_status.get("ok") and ha_status.get("power_kw") is not None:
        inv_kw = float(ha_status["power_kw"])
        if abs(inv_kw - expected_kw) > 0.011:
            mismatch = {
                "inverter_kw": round(inv_kw, 3),
                "schedule_kw": round(expected_kw, 3),
                "current_time": now.strftime("%H:%M"),
                "active_slot": active_slot,
                "startup_check": not STARTUP_CHECK_DONE,
            }
    STARTUP_CHECK_DONE = True
    coverage_ok, coverage_error = _validate_24h_coverage(slots, require_enabled=True) if slots else (False, "No slots configured.")
    saved = request.query_params.get("saved")
    save_error = request.query_params.get("error")
    return HTMLResponse(template.render(
        request=request,
        slots=slots,
        root_path=root_path,
        ha_status=ha_status,
        deye_entity=DEYE_ENTITY,
        ha_url=HA_URL,
        last_applied=last_applied,
        db_path=DB_PATH,
        expected_kw=round(expected_kw, 3),
        active_slot=active_slot,
        mismatch=mismatch,
        now_str=now.strftime("%Y-%m-%d %H:%M:%S"),
        coverage_ok=coverage_ok,
        coverage_error=coverage_error,
        saved=saved,
        save_error=save_error,
    ))

@app.post("/add_slot")
async def add_slot(request: Request, start_time: str = Form(...), end_time: str = Form(...), power_w: float = Form(...)):
    try:
        s_min, e_min = _slot_interval(start_time, end_time)
        if e_min <= s_min:
            raise ValueError()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid time range. Use HH:MM; for midnight end use 00:00.")
    power_w = max(0.0, min(16000.0, float(power_w)))
    power_kw = round(power_w / 1000.0, 3)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO schedule_slots (start_time, end_time, power_kw) VALUES (?, ?, ?)",
              (start_time, end_time, power_kw))
    conn.commit()
    conn.close()
    root_path = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{root_path}/", status_code=303)

@app.post("/delete_slot/{slot_id}")
async def delete_slot(request: Request, slot_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM schedule_slots WHERE id=?", (slot_id,))
    conn.commit()
    conn.close()
    root_path = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{root_path}/", status_code=303)

@app.post("/toggle_slot/{slot_id}")
async def toggle_slot(request: Request, slot_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE schedule_slots SET enabled = 1 - enabled WHERE id=?", (slot_id,))
    conn.commit()
    conn.close()
    root_path = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{root_path}/", status_code=303)


@app.post("/update_slot/{slot_id}")
async def update_slot(request: Request, slot_id: int, start_time: str = Form(...), end_time: str = Form(...), power_w: float = Form(...), enabled: str = Form(None)):
    try:
        s_min, e_min = _slot_interval(start_time, end_time)
        if e_min <= s_min:
            raise ValueError()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid time range. Use HH:MM; for midnight end use 00:00.")
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
    return RedirectResponse(url=f"{root_path}/", status_code=303)



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
    enableds = form.getlist("enabled")
    if not (len(ids) == len(starts) == len(ends) == len(powers) == len(enableds)):
        from urllib.parse import quote
        return RedirectResponse(url=f"{root_path}/?error={quote('Malformed form submission.')}", status_code=303)
    slots = []
    for i, sid in enumerate(ids):
        try:
            sl = {
                "id": int(sid),
                "start_time": str(starts[i]),
                "end_time": str(ends[i]),
                "power_kw": round(max(0.0, min(16000.0, float(powers[i]))) / 1000.0, 3),
                "enabled": 1 if str(enableds[i]) in ("1", "true", "True", "on", "yes") else 0,
            }
        except Exception:
            from urllib.parse import quote
            return RedirectResponse(url=f"{root_path}/?error={quote(f'Invalid values in row {i+1}.')}", status_code=303)
        slots.append(sl)
    ok, err = _validate_24h_coverage(slots, require_enabled=True)
    if not ok:
        from urllib.parse import quote
        return RedirectResponse(url=f"{root_path}/?error={quote(err)}", status_code=303)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for sl in slots:
        c.execute(
            "UPDATE schedule_slots SET start_time=?, end_time=?, power_kw=?, enabled=? WHERE id=?",
            (sl["start_time"], sl["end_time"], max(0.0, min(16.0, sl["power_kw"])), sl["enabled"], sl["id"]),
        )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"{root_path}/?saved=1", status_code=303)

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
        watts = int(round(expected_kw * 1000))
        await apply_to_ha(watts)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_applied', ?)", (str(watts),))
        conn.commit()
        conn.close()
    elif master == "inverter":
        if not (ha_status.get("ok") and ha_status.get("power_kw") is not None):
            return RedirectResponse(url=f"{root_path}/", status_code=303)
        inv_kw = max(0.0, min(16.0, round(float(ha_status["power_kw"]), 2)))
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM schedule_slots WHERE enabled=1 ORDER BY start_time")
        enabled = c.fetchall()
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
    return RedirectResponse(url=f"{root_path}/", status_code=303)


@app.get("/api/status")
async def api_status():
    slots = get_slots()
    ha_status = await get_ha_entity_state()
    expected_kw, active_slot = expected_power_now(slots)
    return JSONResponse({
        "deye_entity": DEYE_ENTITY,
        "ha_url": HA_URL,
        "ha_status": ha_status,
        "last_applied_watts": get_last_applied(),
        "expected_kw_now": expected_kw,
        "active_slot": active_slot,
        "slots_count": len(slots),
        "db_path": DB_PATH,
    })


@app.post("/api/apply_now")
async def api_apply_now(request: Request):
    active_power, changed = _compute_and_maybe_get_apply()
    if changed:
        await apply_to_ha(active_power)
    root_path = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{root_path}/", status_code=303)


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
    return RedirectResponse(url=f"{root_path}/", status_code=303)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8099))
    uvicorn.run(app, host="0.0.0.0", port=port)
