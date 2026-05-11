# AI Exam Proctoring System v2 — Complete Deployment Guide
## Real Database · Live Student Monitoring · Teacher Camera View

---

## What's New in v2

| Feature | Details |
|---|---|
| **Real Cloud Database** | PostgreSQL via Supabase — persists across reboots, multiple students simultaneously |
| **Live Student Feed** | Teacher sees each student's camera + screen in real time via WebSocket |
| **Teacher Commands** | Warn student, send messages, terminate exam from web dashboard |
| **Multi-Student Simultaneous** | Unlimited students exam at same time, all visible to teacher |
| **WS Fallback** | If WebSocket drops, violations auto-fall back to HTTP POST |
| **Auto-Reconnect** | Student client auto-reconnects WS if network hiccups |

---

## Architecture

```
┌────────────────────────────────────┐      ┌──────────────────────────────────────┐
│  Student PC (local)                │      │  Cloud — Render.com                  │
│                                    │      │                                      │
│  PyQt5 App (main_app.py)           │      │  FastAPI (server/main.py)            │
│  ├── Login Window                  │─────►│  POST /auth/token                    │
│  ├── Permission Gate               │      │  POST /sessions/start                │
│  ├── Exam Window                   │      │  POST /sessions/end                  │
│  │   ├── CameraMonitor (AI)        │─WS──►│  WS /ws/student/{token}              │
│  │   ├── ScreenMonitor             │      │     └─ camera frames                 │
│  │   └── AudioMonitor              │      │     └─ violation events              │
│  └── cloud_reporter.py             │      │     └─ risk updates                  │
│      └─ WS stream frames+events    │      │                                      │
│      └─ HTTP fallback for violations│      │  Supabase PostgreSQL                 │
│                                    │      │  (students, sessions, violations,    │
└────────────────────────────────────┘      │   questions, exam_config)            │
                                            │                                      │
                                            └──────────────────────────────────────┘
                                                          │
                                            ┌─────────────▼───────────────────────┐
                                            │  Teacher Browser                     │
                                            │  Streamlit Dashboard                 │
                                            │  (server/web_dashboard.py)           │
                                            │  • Live student grid                 │
                                            │  • Click any student → camera view   │
                                            │  • Real-time violations              │
                                            │  • Send warnings/commands            │
                                            └──────────────────────────────────────┘
```

---

## STEP 1 — Create Supabase Database (10 min, Free)

1. Go to **supabase.com** → Sign up → **New Project**
2. Name: `exam-proctoring` | Region: **Southeast Asia (Singapore)**
3. Wait ~2 min for provisioning
4. Go to **Settings → Database → Connection String → URI**
5. Copy the URI (looks like):
   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.xxxxxxxxxxxx.supabase.co:5432/postgres
   ```
6. **Save this** — you need it in Step 3

> **Skip Supabase?** Leave `DATABASE_URL` empty in Render → server uses SQLite.
> Works for demos but resets data on redeploy. **Use Supabase for real exams.**

---

## STEP 2 — Push to GitHub (5 min)

```bash
# Create new repo on github.com, then:
git init
git add .
git commit -m "AI Exam Proctoring v2"
git remote add origin https://github.com/YOUR_USERNAME/ai-exam-proctoring.git
git push -u origin main
```

> ⚠️ `.gitignore` already excludes `.env`, `data/`, `models/` — credentials are safe.

---

## STEP 3 — Deploy FastAPI Server on Render (15 min)

1. **render.com** → New → **Web Service**
2. Connect GitHub → select your repo
3. Settings:
   - Name: `ai-exam-proctoring-api`
   - Region: **Singapore**
   - Runtime: **Docker**
   - Dockerfile path: `server/Dockerfile`
   - Docker context: `.`
   - Plan: **Free**

4. **Environment Variables** (Render → Environment tab):

   | Key | Value |
   |-----|-------|
   | `JWT_SECRET_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` |
   | `TEACHER_USERNAME` | `admin` |
   | `TEACHER_PASSWORD` | `YourSecurePassword!` |
   | `DATABASE_URL` | `postgresql://postgres:...@db.xxx.supabase.co:5432/postgres` |
   | `JWT_EXPIRE_HOURS` | `9` |

5. Click **Create Web Service** — first build: 8-12 min

6. Test: open `https://your-app.onrender.com/health`
   Expected: `{"status":"ok","ts":...,"db":"postgresql"}`

---

## STEP 4 — Deploy Teacher Dashboard on Streamlit (10 min)

1. **share.streamlit.io** → Sign in with GitHub
2. New App → your repo → Main file: `server/web_dashboard.py`
3. Advanced Settings → Secrets:
   ```toml
   PROCTORING_API_URL = "https://your-app.onrender.com"
   ```
4. Deploy

Teacher URL: `https://your-app.streamlit.app`
Login: `admin` / `YourSecurePassword!`

---

## STEP 5 — Configure Student PCs (2 min each)

Edit `.env` in the project folder:
```env
PROCTORING_API_URL=https://your-app.onrender.com
TEACHER_USERNAME=admin
TEACHER_PASSWORD=YourSecurePassword!
JWT_SECRET_KEY=same-key-as-render
```

Run setup (first time only):
```
setup.bat         # Windows
```

Start exam:
```
start_exam.bat    # Windows
python main_app.py  # Linux/Mac
```

---

## STEP 6 — Add Students

**Option A — Web dashboard** (teacher):
→ Students tab → Add Student

**Option B — Python** (bulk):
```python
# add_students.py
from database import add_student
students = [
    ("STU006", "Rahul Sharma", "rahul@exam.com", "pass123", "Computer Science"),
    ("STU007", "Anjali Singh", "anjali@exam.com", "pass123", "Mathematics"),
]
for args in students:
    ok, msg = add_student(*args)
    print(msg)
```

Demo students already seeded: STU001–STU005 / pass123

---

## How Live Monitoring Works

1. Student opens app → logs in (PyQt5 UI, unchanged)
2. `cloud_reporter.py` silently calls `/auth/token` → gets JWT
3. `/sessions/start` → creates DB record → gets `session_id`
4. WebSocket `/ws/student/{token}` connects in background
5. `CameraMonitor` runs AI locally → every frame pushed via WS at 1 fps
6. `ScreenMonitor` captures screen → pushed at 0.3 fps
7. Each violation → pushed via WS (instant) + stored in DB
8. **Teacher opens Streamlit** → sees all online students as cards
9. **Teacher clicks any student** → live camera feed opens
10. Teacher can send warnings / terminate from dashboard
11. Exam ends → final score + risk stored in Supabase

---

## Teacher Dashboard Features

| Panel | What you see |
|-------|-------------|
| 🔴 Live Monitor | All online students, risk levels, violation counts |
| 👁 Watch Student | Live camera feed, send commands |
| 📊 Overview | Charts: risk distribution, violation types |
| 👤 Sessions | All exam sessions, drill into any session |
| 📋 Violations | Full log, filter by type or student |
| 🧑‍🎓 Students | Add/remove students |
| 📝 Questions | Add/remove/enable questions |
| ⚙️ Settings | Exam duration, title, config |

---

## Troubleshooting

**"Server unreachable" on student PC**
→ Check `PROCTORING_API_URL` in `.env` matches Render URL exactly
→ Open `https://your-app.onrender.com/health` in browser

**"Login fails — invalid credentials"**
→ Students use `student_id` (e.g. STU001), not email, as username
→ Check password matches what's in DB

**"No live feed in dashboard"**
→ `websocket-client` must be installed: `pip install websocket-client`
→ Check Render logs for WS connection messages

**"DATABASE_URL not set — using SQLite"**
→ Set `DATABASE_URL` in Render env vars to your Supabase URI
→ If DB badge shows `sqlite`, data resets on Render redeploy

**"Camera not found"**
→ Local issue — webcam not connected or in use by another app

**Render cold start (first request takes 60s)**
→ Normal for free tier — student app pings `/health` every 9 min to keep server warm

---

## Security Notes

- [ ] Change `TEACHER_PASSWORD` from `admin123`
- [ ] Generate `JWT_SECRET_KEY` with `python -c "import secrets; print(secrets.token_hex(32))"`
- [ ] Mark Render env vars as **Secret**
- [ ] Student passwords are SHA-256 hashed — never stored plain
- [ ] `.env` is gitignored — never committed to GitHub
