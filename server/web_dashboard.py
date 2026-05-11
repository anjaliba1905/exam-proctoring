"""
server/web_dashboard.py  —  AI Exam Proctoring  Teacher Dashboard  v2.0
========================================================================
NEW in v2:
  • Live Camera Feed panel — teacher clicks any student to see their
    camera + screen in real time (WebSocket /ws/teacher/<token>)
  • Live Roster sidebar — green dot = student online right now
  • All original panels preserved (Overview, Violations, Student Manager)
  • Real PostgreSQL database connection via PROCTORING_API_URL

Deploy:
  Render → Web Service → Python
  Build:  pip install -r requirements_dashboard.txt
  Start:  streamlit run server/web_dashboard.py --server.port $PORT --server.address 0.0.0.0
  Env:    PROCTORING_API_URL = https://your-fastapi.onrender.com
          TEACHER_USERNAME   = admin
          TEACHER_PASSWORD   = yourpassword
"""

import os, time, json, base64, threading
import requests
import pandas as pd
import streamlit as st
import plotly.express as px
from datetime import datetime
import websocket   # websocket-client

API = os.environ.get("PROCTORING_API_URL", "https://ai-exam-proctoring-api.onrender.com").rstrip("/")

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="AI Exam Proctoring — Live Monitor",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
html, body, [data-testid="stAppViewContainer"] {
    background-color: #0a0d12 !important;
    color: #e8edf5 !important;
    font-family: 'Segoe UI', Arial, sans-serif;
}
[data-testid="stSidebar"] { background-color: #10151e !important; border-right: 1px solid #252f44; }
[data-testid="stSidebar"] * { color: #e8edf5 !important; }
h1,h2,h3,h4 { color: #e8edf5 !important; }
.stMetric label { color: #8892a4 !important; font-size: 11px !important; }
.stMetric [data-testid="stMetricValue"] { color: #e8edf5 !important; }
div[data-testid="stDataFrame"] { background: #161d2b; border-radius: 10px; }
.badge-high   { background:#2d0505; color:#ef4444; border:1px solid #991b1b;
                border-radius:20px; padding:3px 12px; font-size:12px; font-weight:600; }
.badge-medium { background:#1c1501; color:#f59e0b; border:1px solid #92400e;
                border-radius:20px; padding:3px 12px; font-size:12px; font-weight:600; }
.badge-low    { background:#052e16; color:#22c55e; border:1px solid #166534;
                border-radius:20px; padding:3px 12px; font-size:12px; font-weight:600; }
.student-card {
    background: #161d2b; border: 1px solid #252f44; border-radius: 12px;
    padding: 14px 16px; margin-bottom: 8px; cursor: pointer;
}
.student-card:hover { border-color: #3b82f6; background: #1e2840; }
.live-dot { color: #22c55e; font-size: 10px; }
.offline-dot { color: #4a5568; font-size: 10px; }
.feed-box {
    background: #0d1117; border: 1px solid #252f44; border-radius: 10px;
    padding: 4px; min-height: 240px; display: flex; align-items: center;
    justify-content: center;
}
.viol-pill {
    display: inline-block; border-radius: 12px; padding: 2px 10px;
    font-size: 11px; font-weight: 600; margin: 2px;
    background: #1c1501; color: #f59e0b; border: 1px solid #92400e;
}
stButton > button { border-radius: 8px !important; }
</style>
""", unsafe_allow_html=True)

RISK_COLORS = {
    "High Risk":   "#ef4444",
    "Medium Risk": "#f59e0b",
    "Low Risk":    "#22c55e",
}
VIOL_COLORS = {
    "phone_detected": "#ef4444", "multiple_faces": "#f59e0b", "no_face": "#eab308",
    "gaze_away": "#60a5fa", "tab_switch": "#c084fc", "audio_alert": "#fb923c",
}
VIOL_ICONS = {
    "phone_detected": "📱", "multiple_faces": "👥", "no_face": "👤",
    "gaze_away": "👁", "tab_switch": "🖥", "audio_alert": "🔊",
}


# ── Session state init ────────────────────────────────────────────────────────
def _ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

_ss("token",         "")
_ss("ws_frames",     {})   # student_id → {camera: b64, screen: b64, risk_score, risk_level, violations}
_ss("ws_events",     [])   # list of violation events
_ss("ws_roster",     [])   # list of live student dicts
_ss("ws_thread",     None)
_ss("ws_running",    False)
_ss("watched",       "*")  # currently watched student_id or "*"


# ── Auth helpers ──────────────────────────────────────────────────────────────
def _api(method, path, **kw):
    headers = {"Authorization": f"Bearer {st.session_state.token}"}
    try:
        r = requests.request(method, f"{API}{path}", headers=headers, timeout=12, **kw)
        if r.status_code == 401:
            st.session_state.token = ""
            st.rerun()
        return r
    except Exception as e:
        st.error(f"API unreachable: {e}")
        return None


def _login_page():
    st.title("🎓 AI Exam Proctoring — Teacher Login")
    st.markdown("<br>", unsafe_allow_html=True)
    _, col, _ = st.columns([1, 2, 1])
    with col:
        u = st.text_input("Username", value=os.environ.get("TEACHER_USERNAME", "admin"))
        p = st.text_input("Password", type="password")
        if st.button("Login →", use_container_width=True, type="primary"):
            try:
                r = requests.post(f"{API}/auth/teacher",
                                  json={"username": u, "password": p}, timeout=20)
                if r.status_code == 200:
                    st.session_state.token = r.json()["access_token"]
                    st.rerun()
                else:
                    st.error("Invalid credentials")
            except Exception as e:
                st.error(f"Cannot reach server: {e}")


if not st.session_state.token:
    _login_page()
    st.stop()


# ── WebSocket thread — receives live frames from server ───────────────────────

def _ws_thread_fn(token: str, watch: str):
    """Background thread: connects to /ws/teacher/<token> and pushes frames into session_state."""
    ws_url = API.replace("https://", "wss://").replace("http://", "ws://")
    url    = f"{ws_url}/ws/teacher/{token}?watch={watch}"

    def on_message(ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return

        ev = msg.get("event")

        if ev == "roster":
            st.session_state.ws_roster = msg.get("students", [])
            return

        if ev in ("student_connected", "student_disconnected"):
            # Refresh roster
            st.session_state.ws_events.insert(0, msg)
            if len(st.session_state.ws_events) > 100:
                st.session_state.ws_events = st.session_state.ws_events[:100]
            return

        if ev == "violation":
            st.session_state.ws_events.insert(0, msg)
            if len(st.session_state.ws_events) > 100:
                st.session_state.ws_events = st.session_state.ws_events[:100]
            return

        # Frame message
        student_id = msg.get("student_id")
        if not student_id:
            return

        frame_type = msg.get("type")  # "camera" or "screen"
        data       = msg.get("data", "")

        if student_id not in st.session_state.ws_frames:
            st.session_state.ws_frames[student_id] = {
                "camera": None, "screen": None,
                "risk_score": 0, "risk_level": "Low Risk",
                "violations": [], "name": msg.get("name", student_id),
                "last_seen": time.time(),
            }
        f = st.session_state.ws_frames[student_id]
        if frame_type == "camera":
            f["camera"]     = data
        elif frame_type == "screen":
            f["screen"]     = data
        f["risk_score"]  = msg.get("risk_score", f["risk_score"])
        f["risk_level"]  = msg.get("risk_level",  f["risk_level"])
        f["violations"]  = msg.get("violations",  f["violations"])
        f["name"]        = msg.get("name", f["name"])
        f["last_seen"]   = time.time()

    def on_error(ws, err):
        log_msg = f"[WS] error: {err}"
        st.session_state.ws_events.insert(0, {"event": "ws_error", "msg": str(err)})

    def on_close(ws, *a):
        st.session_state.ws_running = False

    def on_open(ws):
        st.session_state.ws_running = True

    wsa = websocket.WebSocketApp(
        url,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open,
    )
    wsa.run_forever(ping_interval=20, ping_timeout=10)


def _ensure_ws():
    if st.session_state.ws_thread is None or not st.session_state.ws_thread.is_alive():
        t = threading.Thread(
            target=_ws_thread_fn,
            args=(st.session_state.token, st.session_state.watched),
            daemon=True
        )
        t.start()
        st.session_state.ws_thread = t


# ── Cached data fetchers ──────────────────────────────────────────────────────
@st.cache_data(ttl=6)
def _sessions():
    r = _api("GET", "/dashboard/sessions")
    return pd.DataFrame(r.json() if r and r.status_code == 200 else [])

@st.cache_data(ttl=6)
def _violations(session_id=None):
    path = f"/dashboard/violations?session_id={session_id}" if session_id else "/dashboard/violations"
    r = _api("GET", path)
    return pd.DataFrame(r.json() if r and r.status_code == 200 else [])

@st.cache_data(ttl=30)
def _students():
    r = _api("GET", "/dashboard/students")
    return pd.DataFrame(r.json() if r and r.status_code == 200 else [])


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎓 Exam Proctoring")
    st.markdown("---")

    page = st.radio(
        "nav",
        ["🔴 Live Monitor", "📊 Overview", "👤 Student Sessions",
         "📋 Violations Log", "🧑‍🎓 Students", "📝 Questions", "⚙️ Settings"],
        label_visibility="collapsed"
    )

    st.markdown("---")
    auto = st.toggle("Auto-refresh (5 s)", value=True)
    if st.button("🔄 Refresh Now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    # Server health
    try:
        h = requests.get(f"{API}/health", timeout=5)
        d = h.json()
        if h.status_code == 200:
            db_type = d.get("db", "?")
            st.success(f"🟢 Server Online · {db_type}")
        else:
            st.warning("🟡 Degraded")
    except Exception:
        st.error("🔴 Server Offline")

    # WS status
    if st.session_state.ws_running:
        st.success("📡 Live feed connected")
    else:
        st.info("📡 Live feed standby")

    st.markdown("---")
    if st.button("Logout", use_container_width=True):
        st.session_state.clear()
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE: 🔴 Live Monitor
#  Teacher sees all active students as thumbnails + can click one for full view
# ══════════════════════════════════════════════════════════════════════════════
if page == "🔴 Live Monitor":
    _ensure_ws()

    st.title("🔴 Live Student Monitor")
    st.caption(f"Watching: {st.session_state.watched}  ·  {datetime.now().strftime('%H:%M:%S')}")

    # Control bar
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    with c1:
        st.markdown(
            f"**WebSocket:** `{API.replace('https://','wss://').replace('http://','ws://')}/ws/teacher/<token>`"
        )
    with c2:
        if st.button("👁 Watch All", use_container_width=True):
            st.session_state.watched = "*"
            st.session_state.ws_thread = None
            st.rerun()
    with c3:
        st.metric("Live Students", len(st.session_state.ws_frames))
    with c4:
        st.metric("Events", len(st.session_state.ws_events))

    st.divider()

    frames = st.session_state.ws_frames

    if not frames:
        st.info("⏳ No students connected yet. Students appear here automatically when they start their exam client.")

        # Show any recent events
        if st.session_state.ws_events:
            st.subheader("Recent Events")
            for ev in st.session_state.ws_events[:10]:
                st.write(ev)
    else:
        # ── Student selector ──────────────────────────────────────────────────
        student_ids = list(frames.keys())
        names       = [frames[s].get("name", s) for s in student_ids]
        options     = ["All Students (Grid)"] + [f"{n}  ({s})" for n, s in zip(names, student_ids)]
        chosen      = st.selectbox("Select student to monitor", options)

        if chosen == "All Students (Grid)":
            # ── Grid view: all student thumbnails ─────────────────────────────
            st.subheader("All Active Students — Camera Grid")
            cols_per_row = 3
            cols = st.columns(cols_per_row)
            for i, sid in enumerate(student_ids):
                f   = frames[sid]
                col = cols[i % cols_per_row]
                with col:
                    rl   = f.get("risk_level", "Low Risk")
                    rs   = f.get("risk_score", 0)
                    name = f.get("name", sid)
                    rcol = RISK_COLORS.get(rl, "#22c55e")
                    age  = time.time() - f.get("last_seen", time.time())
                    status = "🟢 Live" if age < 10 else f"🟡 {int(age)}s ago"

                    st.markdown(f"**{name}** ({sid})")
                    st.markdown(f'<span style="color:{rcol}">● {rl} · {rs:.0f}</span>  {status}', unsafe_allow_html=True)

                    cam = f.get("camera")
                    if cam:
                        try:
                            img_bytes = base64.b64decode(cam)
                            st.image(img_bytes, use_container_width=True,
                                     caption=f"Camera — {name}")
                        except Exception:
                            st.markdown('<div class="feed-box">📷 Camera feed loading…</div>',
                                        unsafe_allow_html=True)
                    else:
                        st.markdown('<div class="feed-box" style="text-align:center;color:#4a5568;">📷 Waiting for camera frame…</div>',
                                    unsafe_allow_html=True)

                    # Recent violations
                    viols = f.get("violations", [])
                    if viols:
                        pills = " ".join(
                            f'<span class="viol-pill">{VIOL_ICONS.get(v, "⚠")} {v}</span>'
                            for v in viols[-3:]
                        )
                        st.markdown(pills, unsafe_allow_html=True)

                    if st.button(f"🔍 Focus on {name}", key=f"focus_{sid}"):
                        st.session_state.watched = sid
                        st.session_state.ws_thread = None
                        st.rerun()
                    st.markdown("---")

        else:
            # ── Single student full view ──────────────────────────────────────
            idx = options.index(chosen) - 1
            sid = student_ids[idx]
            f   = frames[sid]

            rl   = f.get("risk_level", "Low Risk")
            rs   = f.get("risk_score", 0)
            name = f.get("name", sid)
            rcol = RISK_COLORS.get(rl, "#22c55e")

            # Header
            hc1, hc2, hc3, hc4 = st.columns([3, 1, 1, 1])
            with hc1:
                st.subheader(f"🎓 {name}  ({sid})")
            with hc2:
                st.metric("Risk Score", f"{rs:.0f}")
            with hc3:
                st.markdown(f'<br><span style="color:{rcol};font-weight:bold">● {rl}</span>',
                            unsafe_allow_html=True)
            with hc4:
                age = time.time() - f.get("last_seen", time.time())
                st.metric("Last Frame", f"{age:.1f}s ago")

            # Camera + Screen side by side
            left, right = st.columns(2)
            with left:
                st.markdown("#### 📷 Camera Feed")
                cam = f.get("camera")
                if cam:
                    try:
                        st.image(base64.b64decode(cam), use_container_width=True)
                    except Exception:
                        st.warning("Camera frame decode error")
                else:
                    st.markdown(
                        '<div class="feed-box" style="height:280px;display:flex;'
                        'align-items:center;justify-content:center;color:#4a5568;">'
                        '📷 Waiting for camera…</div>',
                        unsafe_allow_html=True
                    )

            with right:
                st.markdown("#### 🖥️ Screen Feed")
                scr = f.get("screen")
                if scr:
                    try:
                        st.image(base64.b64decode(scr), use_container_width=True)
                    except Exception:
                        st.warning("Screen frame decode error")
                else:
                    st.markdown(
                        '<div class="feed-box" style="height:280px;display:flex;'
                        'align-items:center;justify-content:center;color:#4a5568;">'
                        '🖥️ Waiting for screen…</div>',
                        unsafe_allow_html=True
                    )

            # Violations
            viols = f.get("violations", [])
            if viols:
                st.markdown("**Recent violations:**  " + " ".join(
                    f'<span class="viol-pill">{VIOL_ICONS.get(v,"⚠")} {v}</span>'
                    for v in viols
                ), unsafe_allow_html=True)

            st.divider()

            # Session violations from DB
            df_s = _sessions()
            if not df_s.empty and "student_id" in df_s.columns:
                stu_sess = df_s[df_s["student_id"] == sid]
                if not stu_sess.empty:
                    sess_id = int(stu_sess.iloc[0]["id"])
                    vdf = _violations(sess_id)
                    if not vdf.empty:
                        st.subheader("DB Violation Log")
                        st.dataframe(vdf[["timestamp","violation_type","details","risk_delta"]],
                                     use_container_width=True, height=200)

    # ── Live event feed ───────────────────────────────────────────────────────
    with st.expander("📡 Live Event Stream", expanded=False):
        for ev in st.session_state.ws_events[:20]:
            etype = ev.get("event", "?")
            sid   = ev.get("student_id", "")
            ts    = ev.get("ts", "")
            if etype == "violation":
                vt  = ev.get("violation_type", "")
                col = VIOL_COLORS.get(vt, "#8892a4")
                st.markdown(
                    f'<span style="color:{col}">{VIOL_ICONS.get(vt,"⚠")} '
                    f'**{vt}**</span> — {sid}  `{ts}`',
                    unsafe_allow_html=True
                )
            elif etype == "student_connected":
                st.success(f"🟢 {sid} connected")
            elif etype == "student_disconnected":
                st.error(f"🔴 {sid} disconnected")
            else:
                st.write(ev)


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE: Overview
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📊 Overview":
    st.title("📊 Exam Overview")
    st.caption(f"Updated: {datetime.now().strftime('%H:%M:%S')}  ·  {API}")
    df = _sessions()
    if df.empty:
        st.info("No exam sessions yet.")
    else:
        active  = df[df["status"] == "active"] if "status" in df.columns else pd.DataFrame()
        high    = df[df["risk_level"] == "High Risk"] if "risk_level" in df.columns else pd.DataFrame()
        live_c  = len([s for s in st.session_state.ws_frames.keys()])
        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("Total Sessions",  len(df))
        c2.metric("Active",          len(active))
        c3.metric("🔴 High Risk",    len(high))
        c4.metric("📡 Live Now",     live_c)
        c5.metric("Violations",      len(_violations()))
        st.divider()
        show = [c for c in ["student_id","name","department","start_time","status",
                             "score","risk_score","risk_level","live"] if c in df.columns]
        st.dataframe(df[show], use_container_width=True, height=380)
        cl, cr = st.columns(2)
        if "risk_level" in df.columns:
            with cl:
                st.subheader("Risk Distribution")
                cnt = df["risk_level"].value_counts().reset_index()
                cnt.columns = ["Level","Count"]
                fig = px.pie(cnt, names="Level", values="Count",
                             color="Level", color_discrete_map=RISK_COLORS, hole=0.4)
                fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                                  plot_bgcolor="rgba(0,0,0,0)",
                                  font_color="#e8edf5", margin=dict(t=10,b=10))
                st.plotly_chart(fig, use_container_width=True)
        if "score" in df.columns:
            with cr:
                st.subheader("Score Distribution")
                fig2 = px.histogram(df, x="score", nbins=10,
                                    color_discrete_sequence=["#3b82f6"])
                fig2.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                                   plot_bgcolor="rgba(0,0,0,0)",
                                   font_color="#e8edf5", margin=dict(t=10,b=10))
                st.plotly_chart(fig2, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE: Student Sessions
# ══════════════════════════════════════════════════════════════════════════════
elif page == "👤 Student Sessions":
    st.title("👤 Student Session Detail")
    df = _sessions()
    if df.empty:
        st.info("No sessions found.")
    else:
        labels = df.apply(
            lambda r: f"{r.get('name','?')}  ·  Session {r['id']}  ({r.get('status','?')})",
            axis=1
        ).tolist()
        sel  = st.selectbox("Select session", labels)
        idx  = labels.index(sel)
        sess = df.iloc[idx]
        sid  = int(sess["id"])
        rl   = sess.get("risk_level","Low Risk")
        rcls = {"High Risk":"badge-high","Medium Risk":"badge-medium","Low Risk":"badge-low"}.get(rl,"badge-low")
        st.markdown(f'<span class="{rcls}">{rl}</span>', unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)
        c1,c2,c3,c4 = st.columns(4)
        c1.metric("Score",       f"{sess.get('score',0):.1f}/100")
        c2.metric("Risk Score",  f"{sess.get('risk_score',0):.1f}")
        c3.metric("Status",      sess.get("status","?").capitalize())
        c4.metric("Student ID",  sess.get("student_id","?"))

        # Quick jump to live monitor
        if sess.get("live") or _monitor:
            student_id = sess.get("student_id")
            if st.button(f"🔴 Watch {student_id} Live"):
                st.session_state.watched = student_id
                st.session_state.ws_thread = None
                st.session_state["_nav"] = "🔴 Live Monitor"
                st.rerun()

        st.divider()
        vdf = _violations(sid)
        if vdf.empty:
            st.success("✅ No violations.")
        else:
            st.subheader(f"Violations ({len(vdf)})")
            cnt = vdf["violation_type"].value_counts().reset_index()
            cnt.columns = ["Type","Count"]
            fig = px.bar(cnt, x="Type", y="Count", color="Type",
                         color_discrete_map=VIOL_COLORS)
            fig.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                              plot_bgcolor="rgba(0,0,0,0)",
                              font_color="#e8edf5", showlegend=False,
                              margin=dict(t=30,b=10))
            st.plotly_chart(fig, use_container_width=True)
            if "timestamp" in vdf.columns:
                vdf["timestamp"] = pd.to_datetime(vdf["timestamp"], errors="coerce")
                fig2 = px.scatter(
                    vdf.sort_values("timestamp"), x="timestamp", y="violation_type",
                    color="violation_type", color_discrete_map=VIOL_COLORS,
                    hover_data=["details"] if "details" in vdf.columns else [],
                )
                fig2.update_layout(paper_bgcolor="rgba(0,0,0,0)",
                                   plot_bgcolor="rgba(0,0,0,0)",
                                   font_color="#e8edf5", showlegend=False,
                                   margin=dict(t=30,b=10))
                st.plotly_chart(fig2, use_container_width=True)
            st.dataframe(vdf, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE: Violations Log
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📋 Violations Log":
    st.title("📋 All Violations")
    vdf = _violations()
    if vdf.empty:
        st.info("No violations logged yet.")
    else:
        st.metric("Total", len(vdf))
        all_types = ["All"] + sorted(vdf["violation_type"].unique().tolist()) if "violation_type" in vdf.columns else ["All"]
        filt = st.selectbox("Filter by type", all_types)
        if filt != "All":
            vdf = vdf[vdf["violation_type"] == filt]
        st.dataframe(vdf, use_container_width=True, height=500)
        csv = vdf.to_csv(index=False).encode()
        st.download_button("⬇️ Export CSV", csv, "violations.csv", "text/csv")


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE: Students
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🧑‍🎓 Students":
    st.title("🧑‍🎓 Student Manager")
    sdf = _students()
    if not sdf.empty:
        st.subheader(f"Registered Students ({len(sdf)})")
        # Add live badge
        if "live" in sdf.columns:
            sdf["Status"] = sdf["live"].apply(lambda x: "🟢 Live" if x else "⚫ Offline")
        st.dataframe(sdf, use_container_width=True)

    st.divider()
    st.subheader("➕ Add Student")
    with st.form("add_student"):
        c1, c2 = st.columns(2)
        with c1:
            new_id   = st.text_input("Student ID", placeholder="STU006")
            new_name = st.text_input("Name")
            new_dept = st.text_input("Department")
        with c2:
            new_email = st.text_input("Email")
            new_pass  = st.text_input("Password", type="password")
        if st.form_submit_button("Add Student", type="primary"):
            r = _api("POST", "/students", json={
                "student_id": new_id, "name": new_name,
                "email": new_email, "password": new_pass, "department": new_dept
            })
            if r and r.status_code == 200:
                st.success(f"Student {new_id} added!")
                st.cache_data.clear()
                st.rerun()
            else:
                st.error(f"Error: {r.text if r else 'No response'}")

    st.divider()
    st.subheader("🗑️ Remove Student")
    if not sdf.empty:
        del_id = st.selectbox("Select student to delete",
                              sdf["student_id"].tolist() if "student_id" in sdf.columns else [])
        if st.button("Delete", type="secondary"):
            r = _api("DELETE", f"/students/{del_id}")
            if r and r.status_code == 200:
                st.success(f"Deleted {del_id}")
                st.cache_data.clear()
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE: Questions
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📝 Questions":
    st.title("📝 Exam Questions")
    st.subheader("➕ Add Question")
    with st.form("add_q"):
        q    = st.text_area("Question")
        c1,c2 = st.columns(2)
        with c1:
            oa = st.text_input("Option A")
            ob = st.text_input("Option B")
            cat = st.selectbox("Category", ["General","Mathematics","Science","Computer Science","English"])
        with c2:
            oc = st.text_input("Option C")
            od = st.text_input("Option D")
            diff = st.selectbox("Difficulty", ["Easy","Medium","Hard"])
        ans = st.selectbox("Correct Answer", ["A","B","C","D"])
        if st.form_submit_button("Add Question", type="primary"):
            r = _api("POST", "/questions", json={
                "question": q, "option_a": oa, "option_b": ob,
                "option_c": oc, "option_d": od,
                "answer": ans, "category": cat, "difficulty": diff
            })
            if r and r.status_code == 200:
                st.success("Question added!")
            else:
                st.error(f"Error: {r.text if r else 'No response'}")


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE: Settings
# ══════════════════════════════════════════════════════════════════════════════
elif page == "⚙️ Settings":
    st.title("⚙️ Configuration")
    ws_url = API.replace("https://","wss://").replace("http://","ws://")
    st.markdown(f"""
| Setting | Value |
|---|---|
| API URL | `{API}` |
| Teacher WS (live frames) | `{ws_url}/ws/teacher/<teacher_token>?watch=*` |
| Student WS (send frames) | `{ws_url}/ws/student/<student_token>` |
| Student auth | `POST {API}/auth/token` |
| Violation endpoint | `POST {API}/violations` |
| Add student (API) | `POST {API}/students` |
| Add question (API) | `POST {API}/questions` |
""")
    st.divider()
    st.subheader("Render Environment Variables")
    st.code("""
JWT_SECRET_KEY     = <64-char random hex>
TEACHER_USERNAME   = admin
TEACHER_PASSWORD   = <your secure password>
DATABASE_URL       = postgresql://...  (Supabase / Neon / Render Postgres)
JWT_EXPIRE_HOURS   = 9
""")
    st.divider()
    st.subheader("Student Client .env  (on each student PC)")
    st.code("""
PROCTORING_API_URL = https://your-fastapi-app.onrender.com
TEACHER_PASSWORD   = <same as above>
""")
    st.divider()
    st.subheader("Student Client Patch — Live Frame Sender")
    st.info("Apply `client_patch/cloud_auth_v2.py` and `client_patch/camera_ws_sender.py` to send live frames.")


# ── Auto-refresh ──────────────────────────────────────────────────────────────
if auto:
    time.sleep(5)
    st.rerun()
