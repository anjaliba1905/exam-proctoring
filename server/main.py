"""
server/main.py  —  AI Exam Proctoring FastAPI Backend  v2.0
Enhanced with:
  • Real-time student camera/screen frame streaming → teacher
  • Per-student WebSocket channels
  • Teacher can select any student and see LIVE feed
  • Supabase PostgreSQL real connection
  • All original endpoints preserved

Endpoints:
  POST /auth/token              ← OAuth2 student login → JWT
  POST /auth/teacher            ← teacher login → JWT
  GET  /health                  ← keep-alive ping
  POST /sessions/start          ← open exam session
  POST /sessions/end            ← close exam session
  POST /violations              ← log a violation event
  GET  /dashboard/sessions      ← teacher: all sessions
  GET  /dashboard/violations    ← teacher: violations (filter by session)
  GET  /dashboard/students      ← teacher: student list
  POST /students                ← add a new student
  DELETE /students/{student_id} ← remove a student
  POST /questions               ← add exam question
  GET  /questions               ← get all questions
  WS   /ws/student/{token}      ← student sends live frames → server
  WS   /ws/teacher/{token}      ← teacher receives live frames + events
  WS   /ws/live/{token}         ← teacher live event feed (backward compat)
"""

import os, base64, time, json, asyncio, hashlib, logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Set

from fastapi import (FastAPI, WebSocket, WebSocketDisconnect,
                     Depends, HTTPException, Form, Query)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import jwt   # PyJWT

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("proctoring")

# ── Env config ────────────────────────────────────────────────────────────────
SECRET_KEY       = os.environ.get("JWT_SECRET_KEY", "891a3fef8a94b96f29a0fd17972c793a1a0383f787c593d0bd22d1f691886dc9")
TEACHER_USERNAME = os.environ.get("TEACHER_USERNAME", "admin")
TEACHER_PASSWORD = os.environ.get("TEACHER_PASSWORD", "admin123")
JWT_HOURS        = int(os.environ.get("JWT_EXPIRE_HOURS", "9"))
DATABASE_URL     = os.environ.get("DATABASE_URL", "")

# ── DB helpers ────────────────────────────────────────────────────────────────
_USE_PG = DATABASE_URL.startswith("postgresql") 
_SQLITE_PATH = os.environ.get("SQLITE_PATH", "/app/data/violations.db")

if _USE_PG:
    import psycopg2
    from psycopg2 import pool as _pg_pool_mod
    _pg_pool = None

    def _get_pg_pool():
        global _pg_pool
        if _pg_pool is None:
            _pg_pool = _pg_pool_mod.ThreadedConnectionPool(1, 10, DATABASE_URL)
        return _pg_pool

import sqlite3


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


class _DB:
    """Tiny DB abstraction: same API for SQLite and PostgreSQL."""

    def __init__(self):
        if _USE_PG:
            self._conn = _get_pg_pool().getconn()
            self._pg   = True
        else:
            os.makedirs(os.path.dirname(_SQLITE_PATH), exist_ok=True)
            self._conn = sqlite3.connect(_SQLITE_PATH, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._pg   = False
        self._cur = self._conn.cursor()

    def execute(self, sql: str, params=()):
        if self._pg:
            sql = sql.replace("?", "%s") \
                     .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY") \
                     .replace("INSERT OR IGNORE", "INSERT") \
                     .replace("ON CONFLICT DO NOTHING", "")
        self._cur.execute(sql, params)
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        if self._pg:
            cols = [d[0] for d in self._cur.description]
            return dict(zip(cols, row))
        return dict(row)

    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        if self._pg:
            cols = [d[0] for d in self._cur.description]
            return [dict(zip(cols, r)) for r in rows]
        return [dict(r) for r in rows]

    @property
    def lastrowid(self):
        if self._pg:
            self._cur.execute("SELECT lastval()")
            return self._cur.fetchone()[0]
        return self._cur.lastrowid

    def commit(self):
        self._conn.commit()

    def close(self):
        if self._pg:
            _get_pg_pool().putconn(self._conn)
        else:
            self._conn.close()


def get_db():
    db = _DB()
    try:
        yield db
    finally:
        db.close()


def _init_schema():
    db = _DB()
    stmts = [
        """CREATE TABLE IF NOT EXISTS students (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT UNIQUE NOT NULL,
            name       TEXT NOT NULL,
            email      TEXT UNIQUE NOT NULL,
            password   TEXT NOT NULL,
            department TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS exam_sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time   TEXT,
            status     TEXT DEFAULT 'active',
            score      REAL DEFAULT 0,
            risk_score REAL DEFAULT 0,
            risk_level TEXT DEFAULT 'Low Risk')""",
        """CREATE TABLE IF NOT EXISTS violations (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     INTEGER NOT NULL,
            student_id     TEXT NOT NULL,
            timestamp      TEXT NOT NULL,
            violation_type TEXT NOT NULL,
            details        TEXT,
            risk_delta     REAL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS questions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            question   TEXT NOT NULL,
            option_a   TEXT NOT NULL,
            option_b   TEXT NOT NULL,
            option_c   TEXT NOT NULL,
            option_d   TEXT NOT NULL,
            answer     TEXT NOT NULL,
            category   TEXT DEFAULT 'General',
            difficulty TEXT DEFAULT 'Medium')""",
    ]
    for s in stmts:
        try:
            if _USE_PG:
                s = s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY") \
                     .replace("TEXT DEFAULT CURRENT_TIMESTAMP", "TIMESTAMPTZ DEFAULT NOW()")
            db.execute(s)
        except Exception as e:
            log.warning("Schema stmt warning: %s", e)
    db.commit()

    # Seed demo students
    demo = [
        ("STU001", "Aarav Shah",   "aarav@exam.com",  "pass123", "Computer Science"),
        ("STU002", "Priya Patel",  "priya@exam.com",  "pass123", "Information Technology"),
        ("STU003", "Rohan Mehta",  "rohan@exam.com",  "pass123", "Electronics"),
        ("STU004", "Sneha Joshi",  "sneha@exam.com",  "pass123", "Mathematics"),
        ("STU005", "Kiran Desai",  "kiran@exam.com",  "pass123", "Computer Science"),
    ]
    for sid, name, email, pw, dept in demo:
        try:
            if _USE_PG:
                db.execute(
                    "INSERT INTO students (student_id,name,email,password,department) "
                    "VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (sid, name, email, _hash(pw), dept)
                )
            else:
                db.execute(
                    "INSERT OR IGNORE INTO students (student_id,name,email,password,department) "
                    "VALUES(?,?,?,?,?)", (sid, name, email, _hash(pw), dept)
                )
        except Exception:
            pass
    db.commit()
    db.close()
    log.info("DB schema ready (using %s).", "PostgreSQL" if _USE_PG else "SQLite")


# ── JWT helpers ───────────────────────────────────────────────────────────────
_bearer = HTTPBearer()


def _make_token(data: dict) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=JWT_HOURS)
    return jwt.encode({**data, "exp": exp}, SECRET_KEY, algorithm="HS256")


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired — please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")


def req_student(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    d = _decode(creds.credentials)
    if d.get("role") != "student":
        raise HTTPException(403, "Student token required")
    return d


def req_teacher(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    d = _decode(creds.credentials)
    if d.get("role") not in ("teacher", "admin"):
        raise HTTPException(403, "Teacher token required")
    return d


# ── Live Monitoring Manager ───────────────────────────────────────────────────
class LiveMonitor:
    """
    Manages real-time camera/screen frame relay from students → teachers.

    Student WebSocket: sends JSON frames
      { "type": "camera"|"screen", "data": "<base64 jpeg>",
        "student_id": "STU001", "session_id": 1, "risk_score": 12.5,
        "risk_level": "Low Risk", "violations": [...] }

    Teacher WebSocket: subscribes to a student_id (or "all")
      Receives same frames + violation events
    """

    def __init__(self):
        # student_id → WebSocket (student connection)
        self._students: Dict[str, WebSocket] = {}
        # student_id → latest frame info (for new teacher connections)
        self._latest: Dict[str, dict] = {}
        # student_id → session metadata
        self._session_meta: Dict[str, dict] = {}
        # teacher WebSocket → set of subscribed student_ids ("*" = all)
        self._teachers: Dict[WebSocket, Set[str]] = {}
        # event-only teachers (backward compat /ws/live/)
        self._event_teachers: List[WebSocket] = []

    # ── Student connections ────────────────────────────────────────────────────

    async def student_connect(self, ws: WebSocket, student_id: str, session_id: int, name: str):
        await ws.accept()
        self._students[student_id] = ws
        self._session_meta[student_id] = {
            "student_id": student_id,
            "session_id": session_id,
            "name": name,
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "risk_score": 0,
            "risk_level": "Low Risk",
            "last_seen": time.time(),
        }
        log.info("Student %s connected for live monitoring.", student_id)
        # Notify all teachers
        await self._broadcast_event({
            "event": "student_connected",
            "student_id": student_id,
            "session_id": session_id,
            "name": name,
        })

    def student_disconnect(self, student_id: str):
        self._students.pop(student_id, None)
        self._session_meta.pop(student_id, None)
        self._latest.pop(student_id, None)
        log.info("Student %s disconnected.", student_id)
        asyncio.create_task(self._broadcast_event({
            "event": "student_disconnected",
            "student_id": student_id,
        }))

    # ── Frame relay ───────────────────────────────────────────────────────────

    async def relay_frame(self, student_id: str, payload: dict):
        """Called when a student sends a frame. Fan-out to subscribed teachers."""
        self._latest[student_id] = payload
        if student_id in self._session_meta:
            self._session_meta[student_id]["risk_score"]  = payload.get("risk_score", 0)
            self._session_meta[student_id]["risk_level"]  = payload.get("risk_level", "Low Risk")
            self._session_meta[student_id]["last_seen"]   = time.time()

        dead = []
        for t_ws, subs in list(self._teachers.items()):
            if "*" in subs or student_id in subs:
                try:
                    await t_ws.send_json(payload)
                except Exception:
                    dead.append(t_ws)
        for ws in dead:
            self._teachers.pop(ws, None)

    # ── Teacher connections ───────────────────────────────────────────────────

    async def teacher_connect(self, ws: WebSocket, subscribe_to: str = "*"):
        """
        subscribe_to: student_id to watch, or "*" for all.
        """
        await ws.accept()
        subs = {"*"} if subscribe_to == "*" else {subscribe_to}
        self._teachers[ws] = subs
        log.info("Teacher connected. Watching: %s", subscribe_to)
        # Send current roster immediately
        await ws.send_json({
            "event":    "roster",
            "students": list(self._session_meta.values()),
        })
        # Send latest frame for each watched student
        for sid, frame in self._latest.items():
            if "*" in subs or sid in subs:
                try:
                    await ws.send_json(frame)
                except Exception:
                    break

    def teacher_subscribe(self, ws: WebSocket, student_id: str):
        if ws in self._teachers:
            self._teachers[ws] = {student_id}

    def teacher_subscribe_all(self, ws: WebSocket):
        if ws in self._teachers:
            self._teachers[ws] = {"*"}

    def teacher_disconnect(self, ws: WebSocket):
        self._teachers.pop(ws, None)

    # ── Event broadcast (violations, session events) ───────────────────────────

    async def _broadcast_event(self, payload: dict):
        dead = []
        # All teacher WS get events
        for ws in list(self._teachers.keys()):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._teachers.pop(ws, None)
        # Legacy event feed
        dead2 = []
        for ws in list(self._event_teachers):
            try:
                await ws.send_json(payload)
            except Exception:
                dead2.append(ws)
        for ws in dead2:
            try:
                self._event_teachers.remove(ws)
            except ValueError:
                pass

    async def broadcast_violation(self, payload: dict):
        await self._broadcast_event(payload)

    # ── Legacy event-only teacher feed ────────────────────────────────────────

    async def legacy_connect(self, ws: WebSocket):
        await ws.accept()
        self._event_teachers.append(ws)

    def legacy_disconnect(self, ws: WebSocket):
        try:
            self._event_teachers.remove(ws)
        except ValueError:
            pass

    # ── Roster ────────────────────────────────────────────────────────────────

    def get_active_students(self) -> List[dict]:
        return list(self._session_meta.values())

    def is_student_online(self, student_id: str) -> bool:
        return student_id in self._students


_monitor = LiveMonitor()


# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_schema()
    log.info("Server ready.")
    yield
    log.info("Shutting down.")


app = FastAPI(title="AI Exam Proctoring API v2", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ── Pydantic models ───────────────────────────────────────────────────────────
class SessionStartReq(BaseModel):
    exam_id: str = "default"

class SessionEndReq(BaseModel):
    session_id:       int
    final_risk_score: float
    risk_level:       str

class ViolationItem(BaseModel):
    session_id:     int
    violation_type: str
    details:        str = ""
    risk_delta:     float = 0.0

class TeacherAuthReq(BaseModel):
    username: str
    password: str

class AddStudentReq(BaseModel):
    student_id: str
    name:       str
    email:      str
    password:   str
    department: str = ""

class AddQuestionReq(BaseModel):
    question:   str
    option_a:   str
    option_b:   str
    option_c:   str
    option_d:   str
    answer:     str
    category:   str = "General"
    difficulty: str = "Medium"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "ts": time.time(), "db": "postgresql" if _USE_PG else "sqlite"}


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.post("/auth/token")
def student_login(
    username:   str = Form(...),
    password:   str = Form(...),
    grant_type: str = Form(default="password"),
    db: _DB = Depends(get_db),
):
    row = db.execute(
        "SELECT * FROM students WHERE student_id=? AND password=?",
        (username, _hash(password))
    ).fetchone()
    if not row:
        raise HTTPException(401, "Invalid student ID or password")
    token = _make_token({
        "role":       "student",
        "student_id": row["student_id"],
        "name":       row["name"],
    })
    return {"access_token": token, "token_type": "bearer",
            "student": {k: row[k] for k in ("student_id", "name", "email", "department")}}


@app.post("/auth/teacher")
def teacher_login(req: TeacherAuthReq):
    if req.username != TEACHER_USERNAME or req.password != TEACHER_PASSWORD:
        raise HTTPException(401, "Invalid teacher credentials")
    token = _make_token({"role": "teacher", "username": req.username})
    return {"access_token": token, "token_type": "bearer"}


# ── Sessions ──────────────────────────────────────────────────────────────────

@app.post("/sessions/start")
def session_start(req: SessionStartReq, user=Depends(req_student), db: _DB = Depends(get_db)):
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO exam_sessions (student_id, start_time, status) VALUES (?,?,?)",
        (user["student_id"], now, "active")
    )
    db.commit()
    sid = db.lastrowid
    log.info("Session %d started for %s", sid, user["student_id"])
    return {"session_id": sid, "start_time": now}


@app.post("/sessions/end")
async def session_end(req: SessionEndReq, user=Depends(req_student), db: _DB = Depends(get_db)):
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE exam_sessions SET end_time=?,status='completed',risk_score=?,risk_level=? WHERE id=?",
        (now, req.final_risk_score, req.risk_level, req.session_id)
    )
    db.commit()
    payload = {
        "event":      "session_ended",
        "session_id": req.session_id,
        "student_id": user["student_id"],
        "risk_score": req.final_risk_score,
        "risk_level": req.risk_level,
    }
    await _monitor.broadcast_violation(payload)
    return {"ok": True}


# ── Violations ────────────────────────────────────────────────────────────────

@app.post("/violations")
async def log_violations(item: ViolationItem, user=Depends(req_student), db: _DB = Depends(get_db)):
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO violations (session_id,student_id,timestamp,violation_type,details,risk_delta) "
        "VALUES (?,?,?,?,?,?)",
        (item.session_id, user["student_id"], now,
         item.violation_type, item.details, item.risk_delta)
    )
    db.commit()
    payload = {
        "event":          "violation",
        "session_id":     item.session_id,
        "student_id":     user["student_id"],
        "violation_type": item.violation_type,
        "details":        item.details,
        "risk_delta":     item.risk_delta,
        "ts":             now,
    }
    await _monitor.broadcast_violation(payload)
    return {"logged": True}


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard/sessions")
def dashboard_sessions(user=Depends(req_teacher), db: _DB = Depends(get_db)):
    rows = db.execute("""
        SELECT es.*, s.name, s.department
        FROM exam_sessions es
        JOIN students s ON es.student_id = s.student_id
        ORDER BY es.start_time DESC LIMIT 200
    """).fetchall()
    # Enrich with live status
    for r in rows:
        r["live"] = _monitor.is_student_online(r["student_id"])
    return rows


@app.get("/dashboard/violations")
def dashboard_violations(
    session_id: Optional[int] = None,
    user=Depends(req_teacher),
    db: _DB = Depends(get_db)
):
    if session_id:
        rows = db.execute(
            "SELECT * FROM violations WHERE session_id=? ORDER BY timestamp DESC",
            (session_id,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM violations ORDER BY timestamp DESC LIMIT 500"
        ).fetchall()
    return rows


@app.get("/dashboard/students")
def dashboard_students(user=Depends(req_teacher), db: _DB = Depends(get_db)):
    rows = db.execute(
        "SELECT student_id,name,email,department,created_at FROM students ORDER BY name"
    ).fetchall()
    for r in rows:
        r["live"] = _monitor.is_student_online(r["student_id"])
    return rows


@app.get("/dashboard/live_roster")
def live_roster(user=Depends(req_teacher)):
    """Returns currently connected students with their latest risk scores."""
    return _monitor.get_active_students()


# ── Student Management ────────────────────────────────────────────────────────

@app.post("/students")
def add_student(req: AddStudentReq, user=Depends(req_teacher), db: _DB = Depends(get_db)):
    try:
        if _USE_PG:
            db.execute(
                "INSERT INTO students (student_id,name,email,password,department) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (req.student_id, req.name, req.email, _hash(req.password), req.department)
            )
        else:
            db.execute(
                "INSERT OR IGNORE INTO students (student_id,name,email,password,department) "
                "VALUES(?,?,?,?,?)",
                (req.student_id, req.name, req.email, _hash(req.password), req.department)
            )
        db.commit()
        return {"ok": True, "student_id": req.student_id}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete("/students/{student_id}")
def delete_student(student_id: str, user=Depends(req_teacher), db: _DB = Depends(get_db)):
    db.execute("DELETE FROM students WHERE student_id=?", (student_id,))
    db.commit()
    return {"ok": True}


# ── Questions ─────────────────────────────────────────────────────────────────

@app.get("/questions")
def get_questions(user=Depends(req_student), db: _DB = Depends(get_db)):
    return db.execute("SELECT * FROM questions ORDER BY id").fetchall()


@app.post("/questions")
def add_question(req: AddQuestionReq, user=Depends(req_teacher), db: _DB = Depends(get_db)):
    db.execute(
        "INSERT INTO questions (question,option_a,option_b,option_c,option_d,answer,category,difficulty) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (req.question, req.option_a, req.option_b, req.option_c, req.option_d,
         req.answer, req.category, req.difficulty)
    )
    db.commit()
    return {"ok": True, "id": db.lastrowid}


# ── WebSocket: Student live feed sender ───────────────────────────────────────

@app.websocket("/ws/student/{token}")
async def ws_student(websocket: WebSocket, token: str):
    """
    Student desktop client connects here.
    Sends JSON frames:
    {
      "type": "camera" | "screen" | "status",
      "data": "<base64-encoded JPEG>",       # for camera/screen frames
      "student_id": "STU001",
      "session_id": 1,
      "risk_score": 12.5,
      "risk_level": "Low Risk",
      "violations": ["gaze_away"],           # recent violations
      "name": "Aarav Shah"
    }
    """
    try:
        claims = _decode(token)
        if claims.get("role") != "student":
            await websocket.close(code=4003)
            return
    except HTTPException:
        await websocket.close(code=4001)
        return

    student_id = claims["student_id"]
    name       = claims.get("name", student_id)
    session_id = 0

    await _monitor.student_connect(websocket, student_id, session_id, name)
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                await websocket.send_json({"event": "ping"})
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Update session_id if provided
            if payload.get("session_id"):
                session_id = payload["session_id"]
                _monitor._session_meta.get(student_id, {})["session_id"] = session_id

            payload["student_id"] = student_id
            payload["name"]       = name
            await _monitor.relay_frame(student_id, payload)

    except WebSocketDisconnect:
        pass
    finally:
        _monitor.student_disconnect(student_id)


# ── WebSocket: Teacher live monitor receiver ──────────────────────────────────

@app.websocket("/ws/teacher/{token}")
async def ws_teacher(
    websocket: WebSocket,
    token: str,
    watch: str = Query(default="*")   # student_id or "*"
):
    """
    Teacher connects here to receive live frames.
    Query param ?watch=STU001  or  ?watch=* (all students)

    Teacher can also send control messages:
    { "cmd": "watch", "student_id": "STU002" }   ← switch focus
    { "cmd": "watch_all" }                        ← see all thumbnails
    """
    try:
        claims = _decode(token)
        if claims.get("role") not in ("teacher", "admin"):
            await websocket.close(code=4003)
            return
    except HTTPException:
        await websocket.close(code=4001)
        return

    await _monitor.teacher_connect(websocket, subscribe_to=watch)
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=60)
                msg = json.loads(raw)
                cmd = msg.get("cmd")
                if cmd == "watch" and msg.get("student_id"):
                    _monitor.teacher_subscribe(websocket, msg["student_id"])
                elif cmd == "watch_all":
                    _monitor.teacher_subscribe_all(websocket)
                elif cmd == "ping":
                    await websocket.send_json({"event": "pong"})
            except asyncio.TimeoutError:
                # Send heartbeat with live roster
                await websocket.send_json({
                    "event":    "roster",
                    "students": _monitor.get_active_students(),
                })
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        _monitor.teacher_disconnect(websocket)


# ── WebSocket — legacy teacher live event feed ────────────────────────────────

@app.websocket("/ws/live/{token}")
async def ws_live(websocket: WebSocket, token: str):
    """Backward-compatible event-only teacher feed (no frames)."""
    try:
        _decode(token)
    except HTTPException:
        await websocket.close(code=4001)
        return

    await _monitor.legacy_connect(websocket)
    try:
        while True:
            await asyncio.sleep(25)
            await websocket.send_json({"event": "ping", "ts": time.time()})
    except WebSocketDisconnect:
        _monitor.legacy_disconnect(websocket)
