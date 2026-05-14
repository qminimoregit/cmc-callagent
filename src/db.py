# src/db.py
from __future__ import annotations
"""
PostgreSQL database helpers for the CMC Assistant dashboard.
Stores call history, transcripts, bookings, and complaints.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any
import dateparser
import pytz

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
import dateparser
import pytz

load_dotenv()

logger = logging.getLogger(__name__)

# Use environment variable for the Database connection string
# Default to the local docker-compose setup if not provided
DB_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost:5432/cmc_agent")

def _get_conn() -> psycopg.Connection:
    """Returns a new connection to the PostgreSQL database."""
    return psycopg.connect(DB_URL, row_factory=dict_row)

def check_db_connection() -> bool:
    """Check if the database is reachable."""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return True
    except Exception:
        return False

def init_db() -> None:
    """Create tables if they don't already exist."""
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                # Calls table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS calls (
                        call_sid     TEXT PRIMARY KEY,
                        phone_number TEXT DEFAULT '',
                        caller_city  TEXT DEFAULT '',
                        caller_country TEXT DEFAULT '',
                        started_at   TIMESTAMP WITH TIME ZONE,
                        ended_at     TIMESTAMP WITH TIME ZONE,
                        duration_sec INTEGER DEFAULT 0,
                        status       TEXT DEFAULT 'in-progress',
                        escalated    INTEGER DEFAULT 0,
                        turns        JSONB DEFAULT '[]'::jsonb
                    );
                """)
                
                # Bookings table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bookings (
                        id               SERIAL PRIMARY KEY,
                        call_sid         TEXT REFERENCES calls(call_sid) ON DELETE SET NULL,
                        caller_name      TEXT,
                        contact_number   TEXT,
                        service_category TEXT,
                        specific_service TEXT,
                        appointment_date TIMESTAMP WITH TIME ZONE,
                        status           TEXT DEFAULT 'Pending',
                        created_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    );
                """)
                
                # Complaints table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS complaints (
                        id               SERIAL PRIMARY KEY,
                        call_sid         TEXT REFERENCES calls(call_sid) ON DELETE SET NULL,
                        caller_name      TEXT,
                        contact_number   TEXT,
                        service_category TEXT,
                        specific_service TEXT,
                        description      TEXT,
                        location_address TEXT,
                        status           TEXT DEFAULT 'Open',
                        created_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    );
                """)
                
                # Departments table for call routing
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS departments (
                        id                    SERIAL PRIMARY KEY,
                        name                  TEXT UNIQUE,
                        transfer_phone_number TEXT,
                        is_active             BOOLEAN DEFAULT TRUE
                    );
                """)
                
                # Insert default departments if not exist
                cur.execute("SELECT COUNT(*) as count FROM departments")
                if cur.fetchone()["count"] == 0:
                    default_depts = [
                        ("Waste Management", "+94112345678"),
                        ("Public Health", "+94112345679"),
                        ("Civil Works", "+94112345680"),
                        ("Tax and Revenue", "+94112345681"),
                        ("Community Services", "+94112345682")
                    ]
                    for name, phone in default_depts:
                        cur.execute(
                            "INSERT INTO departments (name, transfer_phone_number) VALUES (%s, %s)",
                            (name, phone)
                        )
                
                # Available Slots table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS available_slots (
                        id              SERIAL PRIMARY KEY,
                        department      TEXT NOT NULL,
                        day_of_week     INTEGER NOT NULL, -- 0=Monday, 6=Sunday
                        start_time      TIME NOT NULL,
                        end_time        TIME NOT NULL,
                        slot_duration_minutes INTEGER DEFAULT 30,
                        max_bookings    INTEGER DEFAULT 5,
                        is_active       BOOLEAN DEFAULT TRUE
                    );
                """)
                
                # Seed default slots if empty
                cur.execute("SELECT COUNT(*) as count FROM available_slots")
                if cur.fetchone()["count"] == 0:
                    depts = ["Waste Management", "Public Health", "Civil Works", "Tax and Revenue", "Community Services"]
                    for dept in depts:
                        # Mon-Fri (0-4) 8am-5pm
                        for day in range(5):
                            cur.execute(
                                "INSERT INTO available_slots (department, day_of_week, start_time, end_time) VALUES (%s, %s, %s, %s)",
                                (dept, day, "08:00:00", "17:00:00")
                            )
                        # Saturday (5) for Community Services
                        if dept == "Community Services":
                            cur.execute(
                                "INSERT INTO available_slots (department, day_of_week, start_time, end_time) VALUES (%s, %s, %s, %s)",
                                (dept, 5, "09:00:00", "16:00:00")
                            )
            conn.commit()
        logger.info("Database initialized successfully at %s", DB_URL.split('@')[-1])
    except Exception as e:
        logger.error("Failed to initialize database: %s", e)

def upsert_call(
    call_sid: str,
    phone_number: str = "",
    caller_city: str = "",
    caller_country: str = "",
    status: str = "in-progress",
    escalated: bool = False,
    turns: list | None = None,
    ended_at: str | None = None,
    duration_sec: int = 0,
) -> None:
    """Insert or update a call record."""
    now = datetime.now(timezone.utc)
    # Parse ended_at to datetime if provided as string
    ended_at_dt = None
    if ended_at:
        try:
            ended_at_dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        except ValueError:
            ended_at_dt = now

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT call_sid FROM calls WHERE call_sid = %s", (call_sid,)
            )
            existing = cur.fetchone()

            turns_json = json.dumps(turns or [], ensure_ascii=False)

            if existing:
                cur.execute(
                    """UPDATE calls SET
                        phone_number = %s,
                        caller_city  = %s,
                        caller_country = %s,
                        status       = %s,
                        escalated    = %s,
                        turns        = %s::jsonb,
                        ended_at     = COALESCE(%s, ended_at),
                        duration_sec = %s
                    WHERE call_sid = %s""",
                    (
                        phone_number,
                        caller_city,
                        caller_country,
                        status,
                        int(escalated),
                        turns_json,
                        ended_at_dt,
                        duration_sec,
                        call_sid,
                    ),
                )
            else:
                cur.execute(
                    """INSERT INTO calls
                        (call_sid, phone_number, caller_city, caller_country,
                         started_at, ended_at, status, escalated, turns, duration_sec)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)""",
                    (
                        call_sid,
                        phone_number,
                        caller_city,
                        caller_country,
                        now,
                        ended_at_dt,
                        status,
                        int(escalated),
                        turns_json,
                        duration_sec,
                    ),
                )
        conn.commit()


def get_call(call_sid: str) -> dict | None:
    """Fetch a single call record."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM calls WHERE call_sid = %s", (call_sid,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            
            # Format datetime for JSON serialization
            if row.get("started_at"):
                row["started_at"] = row["started_at"].isoformat()
            if row.get("ended_at"):
                row["ended_at"] = row["ended_at"].isoformat()
            
            return row

def list_calls(limit: int = 50, offset: int = 0, status_filter: str = "") -> list[dict]:
    """Return a list of calls ordered by most recent first."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            if status_filter and status_filter != "all":
                if status_filter == "escalated":
                    cur.execute(
                        "SELECT * FROM calls WHERE escalated = 1 ORDER BY started_at DESC LIMIT %s OFFSET %s",
                        (limit, offset),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM calls WHERE status = %s ORDER BY started_at DESC LIMIT %s OFFSET %s",
                        (status_filter, limit, offset),
                    )
            else:
                cur.execute(
                    "SELECT * FROM calls ORDER BY started_at DESC LIMIT %s OFFSET %s",
                    (limit, offset),
                )
            
            rows = cur.fetchall()
            
            # Format datetime for JSON serialization
            for row in rows:
                if row.get("started_at"):
                    row["started_at"] = row["started_at"].isoformat()
                if row.get("ended_at"):
                    row["ended_at"] = row["ended_at"].isoformat()
                    
            return rows

def get_stats() -> dict:
    """Aggregate stats for the stats bar."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as c FROM calls")
            total = cur.fetchone()["c"]
            
            cur.execute("SELECT COUNT(*) as c FROM calls WHERE escalated = 1")
            escalated = cur.fetchone()["c"]
            
            cur.execute("SELECT COUNT(*) as c FROM calls WHERE status = 'completed'")
            completed = cur.fetchone()["c"]
            
            cur.execute("SELECT COUNT(*) as c FROM calls WHERE DATE(started_at) = CURRENT_DATE")
            today = cur.fetchone()["c"]
            
            cur.execute("SELECT AVG(duration_sec) as a FROM calls WHERE duration_sec > 0")
            avg_dur = cur.fetchone()["a"] or 0
            
            # Bookings count
            cur.execute("SELECT COUNT(*) as c FROM bookings")
            total_bookings = cur.fetchone()["c"]
            
            # Complaints count
            cur.execute("SELECT COUNT(*) as c FROM complaints")
            total_complaints = cur.fetchone()["c"]
            
            # Language breakdown from turns JSONB (approximate approach)
            si_count = en_count = ta_count = 0
            
            cur.execute("SELECT turns FROM calls")
            rows = cur.fetchall()
            for row in rows:
                turns = row["turns"]
                if isinstance(turns, str):
                    try:
                        turns = json.loads(turns)
                    except:
                        turns = []
                for turn in turns:
                    lang = turn.get("lang", "")
                    if lang == "si":
                        si_count += 1
                    elif lang == "en":
                        en_count += 1
                    elif lang == "ta":
                        ta_count += 1
                        
            return {
                "total_calls": total,
                "today_calls": today,
                "escalated": escalated,
                "completed": completed,
                "avg_duration_sec": round(avg_dur),
                "escalation_rate": round((escalated / total * 100) if total else 0, 1),
                "lang_breakdown": {"si": si_count, "en": en_count, "ta": ta_count},
                "total_bookings": total_bookings,
                "total_complaints": total_complaints
            }

# --- New Functions for Bookings and Complaints ---

def create_booking(call_sid: str, caller_name: str, contact_number: str, 
                  service_category: str, specific_service: str, appointment_date: str) -> dict:
    """Create a new booking with robust date parsing and slot validation."""
    colombo_tz = pytz.timezone("Asia/Colombo")
    
    # Try strict ISO parse first
    appt_dt = None
    try:
        appt_dt = datetime.fromisoformat(appointment_date.replace("Z", "+00:00"))
        if appt_dt.tzinfo is None:
            appt_dt = colombo_tz.localize(appt_dt)
    except ValueError:
        pass
    
    if not appt_dt:
        appt_dt = dateparser.parse(
            appointment_date, 
            settings={
                'PREFER_DATES_FROM': 'future', 
                'TIMEZONE': 'Asia/Colombo', 
                'RETURN_AS_TIMEZONE_AWARE': True,
                'RELATIVE_BASE': datetime.now(colombo_tz)
            }
        )
    
    if not appt_dt:
        logger.error("Could not parse date: %s", appointment_date)
        raise ValueError(f"Cannot parse appointment date: {appointment_date!r}")

    # Validate slot availability
    if not is_slot_available(service_category, appt_dt):
        logger.warning("Slot not available: %s at %s", service_category, appt_dt)
        raise ValueError(f"The selected time {appt_dt.strftime('%Y-%m-%d %H:%M')} is not available for {service_category}. Please pick a different time.")
        
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO bookings 
                   (call_sid, caller_name, contact_number, service_category, specific_service, appointment_date) 
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (call_sid, caller_name, contact_number, service_category, specific_service, appt_dt)
            )
            booking_id = cur.fetchone()["id"]
        conn.commit()
    return {"id": booking_id, "status": "Pending"}

def list_bookings(limit: int = 50, offset: int = 0, category_filter: str = "") -> list[dict]:
    """Return a list of bookings."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            if category_filter and category_filter != "all":
                cur.execute(
                    "SELECT * FROM bookings WHERE service_category = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (category_filter, limit, offset),
                )
            else:
                cur.execute(
                    "SELECT * FROM bookings ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (limit, offset),
                )
            
            rows = cur.fetchall()
            for row in rows:
                if row.get("appointment_date"):
                    row["appointment_date"] = row["appointment_date"].isoformat()
                if row.get("created_at"):
                    row["created_at"] = row["created_at"].isoformat()
            return rows

def create_complaint(call_sid: str, caller_name: str, contact_number: str, 
                    service_category: str, specific_service: str, description: str, location_address: str) -> dict:
    """Create a new complaint."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO complaints 
                   (call_sid, caller_name, contact_number, service_category, specific_service, description, location_address) 
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (call_sid, caller_name, contact_number, service_category, specific_service, description, location_address)
            )
            complaint_id = cur.fetchone()["id"]
        conn.commit()
    return {"id": complaint_id, "status": "Open"}

def list_complaints(limit: int = 50, offset: int = 0, category_filter: str = "") -> list[dict]:
    """Return a list of complaints."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            if category_filter and category_filter != "all":
                cur.execute(
                    "SELECT * FROM complaints WHERE service_category = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (category_filter, limit, offset),
                )
            else:
                cur.execute(
                    "SELECT * FROM complaints ORDER BY created_at DESC LIMIT %s OFFSET %s",
                    (limit, offset),
                )
            
            rows = cur.fetchall()
            for row in rows:
                if row.get("created_at"):
                    row["created_at"] = row["created_at"].isoformat()
            return rows

def get_department_transfer_number(department_name: str) -> str | None:
    """Fetch the transfer phone number for a given department."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT transfer_phone_number FROM departments WHERE name = %s AND is_active = TRUE", 
                (department_name,)
            )
            row = cur.fetchone()
            if row:
                return row["transfer_phone_number"]
            return None

def cleanup_stale_calls(timeout_minutes: int = 30) -> int:
    """Mark calls that have been in-progress for too long as completed."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE calls SET status = 'completed', ended_at = NOW() 
                   WHERE status = 'in-progress' 
                   AND started_at < (NOW() - INTERVAL '%s minutes')""",
                (timeout_minutes,)
            )
            count = cur.rowcount
        conn.commit()
    return count

def get_available_slots(department: str, date: str) -> list[str]:
    """Return a list of available time strings for a given department and date."""
    colombo_tz = pytz.timezone("Asia/Colombo")
    try:
        dt = datetime.fromisoformat(date)
        # Make the reference date timezone-aware in Colombo time
        if dt.tzinfo is None:
            dt = colombo_tz.localize(dt)
        else:
            dt = dt.astimezone(colombo_tz)
        day_of_week = dt.weekday()
    except Exception:
        return []

    with _get_conn() as conn:
        with conn.cursor() as cur:
            # Get slot config
            cur.execute(
                "SELECT start_time, end_time, slot_duration_minutes, max_bookings FROM available_slots WHERE department = %s AND day_of_week = %s AND is_active = TRUE",
                (department, day_of_week)
            )
            config = cur.fetchone()
            if not config:
                return []

            start_t = config["start_time"]
            end_t = config["end_time"]
            duration = config["slot_duration_minutes"]
            max_b = config["max_bookings"]

            # Generate timezone-aware slots in Colombo time
            all_slots = []
            curr = colombo_tz.localize(datetime.combine(dt.date(), start_t))
            end_dt = colombo_tz.localize(datetime.combine(dt.date(), end_t))

            while curr < end_dt:
                slot_str = curr.strftime("%H:%M")
                slot_end = curr + timedelta(minutes=duration)

                # Count existing bookings that fall within this slot window
                # (range query avoids sub-second precision mismatches)
                cur.execute(
                    """SELECT COUNT(*) as c FROM bookings
                       WHERE service_category = %s
                         AND appointment_date >= %s
                         AND appointment_date < %s""",
                    (department, curr, slot_end)
                )
                booked = cur.fetchone()["c"]

                if booked < max_b:
                    all_slots.append(slot_str)

                curr = slot_end

            return all_slots

def is_slot_available(department: str, appt_dt: datetime) -> bool:
    """Check if a specific datetime is available for booking."""
    colombo_tz = pytz.timezone("Asia/Colombo")

    # Ensure appt_dt is timezone-aware in Colombo time for correct comparison
    if appt_dt.tzinfo is None:
        appt_dt = colombo_tz.localize(appt_dt)
    else:
        appt_dt = appt_dt.astimezone(colombo_tz)

    day_of_week = appt_dt.weekday()
    # Use the local (Colombo) time for business-hours comparison
    appt_time = appt_dt.time().replace(tzinfo=None)

    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT start_time, end_time, slot_duration_minutes, max_bookings
                   FROM available_slots
                   WHERE department = %s AND day_of_week = %s AND is_active = TRUE""",
                (department, day_of_week)
            )
            config = cur.fetchone()
            if not config:
                return False

            if not (config["start_time"] <= appt_time < config["end_time"]):
                return False

            # Use a range query so sub-second offsets / tz differences don't
            # prevent matching existing bookings in the same slot window.
            duration = config["slot_duration_minutes"]
            slot_end = appt_dt + timedelta(minutes=duration)
            cur.execute(
                """SELECT COUNT(*) as c FROM bookings
                   WHERE service_category = %s
                     AND appointment_date >= %s
                     AND appointment_date < %s""",
                (department, appt_dt, slot_end)
            )
            count = cur.fetchone()["c"]
            return count < config["max_bookings"]

def list_all_slots() -> list[dict]:
    """List all configured slots for management."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM available_slots ORDER BY department, day_of_week")
            rows = cur.fetchall()
            for r in rows:
                r["start_time"] = r["start_time"].strftime("%H:%M")
                r["end_time"] = r["end_time"].strftime("%H:%M")
            return rows

def update_slot_status(slot_id: int, is_active: bool) -> bool:
    """Toggle a slot's active status."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE available_slots SET is_active = %s WHERE id = %s", (is_active, slot_id))
        conn.commit()
    return True
