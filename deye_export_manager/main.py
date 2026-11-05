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
            return {
                "ok": True,
                "entity_id": data.get("entity_id", DEYE_ENTITY),
                "state": data.get("state"),
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

def check_and_apply_schedule():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now = datetime.now()
    current_time = now.strftime("%H:%M")

    c.execute("SELECT * FROM schedule_slots WHERE enabled=1")
    slots = c.fetchall()

    active_power = 0
    for slot in slots:
        if slot["start_time"] <= current_time < slot["end_time"]:
            active_power = int(slot["power_kw"] * 1000)
            break

    c.execute("SELECT value FROM settings WHERE key='last_applied'")
    row = c.fetchone()
    last_applied = int(row["value"]) if row else -1

    if active_power != last_applied:
        asyncio.run(apply_to_ha(active_power))
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_applied', ?)", (str(active_power),))
        conn.commit()

    conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(check_and_apply_schedule, "interval", minutes=1)
scheduler.start()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM schedule_slots ORDER BY start_time")
    rows = c.fetchall()
    slots = []
    for row in rows:
        slots.append({"id": row["id"], "start_time": row["start_time"], "end_time": row["end_time"], "power_kw": row["power_kw"], "enabled": row["enabled"]})
    conn.close()
    template = env.get_template("index.html")
    root_path = request.scope.get("root_path", "")
    ha_status = await get_ha_entity_state()
    last_applied = get_last_applied()
    return HTMLResponse(template.render(
        request=request,
        slots=slots,
        root_path=root_path,
        ha_status=ha_status,
        deye_entity=DEYE_ENTITY,
        ha_url=HA_URL,
        last_applied=last_applied,
        db_path=DB_PATH,
    ))

@app.post("/add_slot")
async def add_slot(request: Request, start_time: str = Form(...), end_time: str = Form(...), power_kw: float = Form(...)):
    if start_time >= end_time:
        raise HTTPException(status_code=400, detail="Start time must be before end time")

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

@app.get("/api/slots")
async def api_slots():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM schedule_slots ORDER BY start_time")
    slots = [dict(row) for row in c.fetchall()]
    conn.close()
    return JSONResponse(slots)

@app.get("/api/status")
async def api_status():
    ha_status = await get_ha_entity_state()
    return JSONResponse({
        "deye_entity": DEYE_ENTITY,
        "ha_url": HA_URL,
        "ha_status": ha_status,
        "last_applied_watts": get_last_applied(),
        "db_path": DB_PATH,
    })

@app.post("/api/apply_now")
async def api_apply_now(request: Request):
    check_and_apply_schedule()
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
        if ha and ha.get("ok") and ha.get("state") is not None:
            try:
                # entity may report W or kW depending on integration; treat numeric as W if >= 50 else kW
                val = float(ha["state"])
                unit = (ha.get("unit") or "").lower()
                if "kw" in unit:
                    power_kw = val
                elif "w" in unit or val >= 50:
                    power_kw = round(val / 1000.0, 2)
                else:
                    power_kw = round(val, 2)
            except Exception:
                power_kw = 0.0
        c.execute(
            "INSERT INTO schedule_slots (start_time, end_time, power_kw, enabled) VALUES (?, ?, ?, 1)",
            ("00:00", "23:59", max(0.0, min(16.0, power_kw))),
        )
        conn.commit()
    conn.close()
    root_path = request.scope.get("root_path", "")
    return RedirectResponse(url=f"{root_path}/", status_code=303)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8099))
    uvicorn.run(app, host="0.0.0.0", port=port)
