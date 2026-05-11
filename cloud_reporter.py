"""
cloud_reporter.py — Single module that handles ALL cloud communication.

Integrates:
  1. HTTP login + session management (REST API)
  2. WebSocket live feed — streams camera/screen frames to teacher
  3. Sends violation events via WS (fast) or HTTP fallback
  4. Receives teacher commands (warn, terminate, message) and calls callbacks

Drop-in:  import cloud_reporter as cloud
          cloud.login(student_id, password)
          cloud.start_session()
          cloud.push_camera_frame(bgr_frame)   # called from CameraMonitor
          cloud.push_screen_frame(pil_img)     # called from ScreenMonitor
          cloud.log_violation(vtype, details, risk_delta)
          cloud.update_risk(score, level)
          cloud.end_session(session_id, risk_score, risk_level)
          cloud.set_command_callback(fn)       # fn(payload dict)
"""

import os, time, json, logging, threading, queue, base64
import requests
import cv2
import numpy as np

log = logging.getLogger("cloud_reporter")

# ── Config ────────────────────────────────────────────────────────────────────
_API_URL       = os.environ.get("PROCTORING_API_URL", "").rstrip("/")
_LOGIN_TIMEOUT = 65
_HTTP_TIMEOUT  = 10
_MAX_RETRIES   = 2

# ── State ─────────────────────────────────────────────────────────────────────
_token:       str  = ""
_session_id:  int  = 0
_student_id:  str  = ""
_name:        str  = ""
_online:      bool = False
_ws                = None
_ws_thread         = None
_stop_ws:     bool = False
_cam_q        = queue.Queue(maxsize=3)
_scr_q        = queue.Queue(maxsize=3)
_risk_score:  float = 0.0
_risk_level:  str   = "Low Risk"
_cmd_callback       = None
_lock         = threading.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────
def _headers():
    return {"Authorization": f"Bearer {_token}"}

def _ws_url():
    base = _API_URL.replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/ws/student/{_token}"

def _encode_frame(bgr, quality=55):
    try:
        small = cv2.resize(bgr, (320, 240))
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return base64.b64encode(buf.tobytes()).decode() if ok else ""
    except Exception:
        return ""

def _encode_pil(pil_img, quality=45):
    try:
        import io
        buf = io.BytesIO()
        pil_img.convert("RGB").resize((480, 300)).save(buf, "JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""

# ── Public API ────────────────────────────────────────────────────────────────

def login(student_id: str, password: str) -> bool:
    """Authenticate student against cloud API. Returns True on success."""
    global _token, _student_id, _name, _online, _API_URL
    _API_URL = os.environ.get("PROCTORING_API_URL", _API_URL).rstrip("/")
    if not _API_URL:
        log.warning("[Cloud] PROCTORING_API_URL not set — offline mode")
        return False

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            r = requests.post(
                f"{_API_URL}/auth/token",
                data={"username": student_id, "password": password, "grant_type": "password"},
                timeout=_LOGIN_TIMEOUT,
            )
            if r.status_code == 200:
                data        = r.json()
                _token      = data["access_token"]
                _student_id = student_id
                _name       = data.get("student", {}).get("name", student_id)
                _online     = True
                os.environ["PROCTORING_AUTH_TOKEN"] = _token
                log.info("[Cloud] Login OK: %s (%s)", student_id, _name)
                return True
            elif r.status_code == 401:
                log.warning("[Cloud] Bad credentials for %s", student_id)
                return False
            else:
                if attempt < _MAX_RETRIES:
                    time.sleep(2)
        except requests.Timeout:
            log.warning("[Cloud] Timeout attempt %d", attempt)
            if attempt < _MAX_RETRIES:
                time.sleep(5)
        except requests.RequestException as e:
            log.warning("[Cloud] Network error: %s", e)
            return False
    return False


def start_session(exam_id: str = "default") -> int:
    """Create exam session in DB. Returns session_id (0 if offline)."""
    global _session_id
    if not _token:
        return 0
    try:
        r = requests.post(
            f"{_API_URL}/sessions/start",
            json={"exam_id": exam_id},
            headers=_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            _session_id = r.json()["session_id"]
            log.info("[Cloud] Session %d started", _session_id)
            _start_ws()
            return _session_id
    except Exception as e:
        log.warning("[Cloud] start_session error: %s", e)
    return 0


def end_session(session_id: int, risk_score: float, risk_level: str, score: float = 0.0):
    """Finalize session in DB and stop WS feed."""
    _stop_ws_connection()
    if not _token or not session_id:
        return
    try:
        requests.post(
            f"{_API_URL}/sessions/end",
            json={"session_id": session_id, "final_risk_score": float(risk_score),
                  "risk_level": str(risk_level), "score": float(score)},
            headers=_headers(),
            timeout=_HTTP_TIMEOUT,
        )
        log.info("[Cloud] Session %d ended. risk=%.1f %s score=%.1f%%",
                 session_id, risk_score, risk_level, score)
    except Exception as e:
        log.warning("[Cloud] end_session error: %s", e)


def log_violation(violation_type: str, details: str = "", risk_delta: float = 0.0):
    """Log violation — tries WebSocket first, HTTP fallback."""
    payload = {"event": "violation", "violation_type": violation_type,
                "details": details, "risk_delta": risk_delta}
    if not _ws_send(payload):
        # HTTP fallback
        if _token and _session_id:
            try:
                requests.post(
                    f"{_API_URL}/violations",
                    json={"session_id": _session_id, "violation_type": violation_type,
                          "details": details, "risk_delta": risk_delta},
                    headers=_headers(),
                    timeout=6,
                )
            except Exception:
                pass


def update_risk(risk_score: float, risk_level: str):
    """Push current risk score to teacher via WS."""
    global _risk_score, _risk_level
    with _lock:
        _risk_score = risk_score
        _risk_level = risk_level
    _ws_send({"event": "risk_update", "risk_score": risk_score, "risk_level": risk_level})


def push_camera_frame(bgr_frame):
    """
    Call from CameraMonitor with numpy BGR frame.
    Frame is queued and sent at LIVE_CAMERA_FPS rate.
    """
    if not _online:
        return
    try:
        if _cam_q.full():
            _cam_q.get_nowait()
        _cam_q.put_nowait(bgr_frame.copy())
    except Exception:
        pass


def push_screen_frame(pil_img):
    """
    Call from ScreenMonitor with PIL Image.
    Frame is queued and sent at LIVE_SCREEN_FPS rate.
    """
    if not _online:
        return
    try:
        if _scr_q.full():
            _scr_q.get_nowait()
        _scr_q.put_nowait(pil_img.copy())
    except Exception:
        pass


def set_command_callback(fn):
    """Register fn(payload: dict) — called when teacher sends command."""
    global _cmd_callback
    _cmd_callback = fn


def is_online() -> bool:
    return _online

# ── WebSocket internals ───────────────────────────────────────────────────────

def _ws_send(payload: dict) -> bool:
    ws = _ws
    if ws is None:
        return False
    try:
        ws.send(json.dumps(payload))
        return True
    except Exception:
        return False


def _start_ws():
    global _ws_thread, _stop_ws
    _stop_ws = False
    _ws_thread = threading.Thread(target=_ws_loop, daemon=True, name="cloud-ws")
    _ws_thread.start()


def _stop_ws_connection():
    global _stop_ws, _ws
    _stop_ws = True
    if _ws:
        try:
            _ws.close()
        except Exception:
            pass
    _ws = None


def _ws_loop():
    global _ws
    try:
        import websocket as _wsc
    except ImportError:
        log.warning("[Cloud] websocket-client not installed. Install: pip install websocket-client")
        return

    url = _ws_url()
    cam_interval = 1.0 / float(os.environ.get("LIVE_CAMERA_FPS", "1.0"))
    scr_interval = 1.0 / float(os.environ.get("LIVE_SCREEN_FPS", "0.3"))
    backoff = 3

    while not _stop_ws:
        try:
            log.info("[Cloud] WS connecting: %s", url.split("/ws/")[0] + "/ws/student/***")
            ws = _wsc.create_connection(url, timeout=15)
            _ws = ws
            backoff = 3
            log.info("[Cloud] WS connected OK")

            last_cam = last_scr = last_ping = 0.0

            while not _stop_ws:
                now = time.time()

                # Camera frame
                if now - last_cam >= cam_interval:
                    try:
                        frame = _cam_q.get_nowait()
                        b64   = _encode_frame(frame)
                        if b64:
                            ws.send(json.dumps({"event": "frame", "type": "camera", "data": b64,
                                                "student_id": _student_id, "session_id": _session_id}))
                        last_cam = now
                    except queue.Empty:
                        pass

                # Screen frame
                if now - last_scr >= scr_interval:
                    try:
                        pil = _scr_q.get_nowait()
                        b64 = _encode_pil(pil)
                        if b64:
                            ws.send(json.dumps({"event": "frame", "type": "screen", "data": b64,
                                                "student_id": _student_id, "session_id": _session_id}))
                        last_scr = now
                    except queue.Empty:
                        pass

                # Keepalive ping every 20s
                if now - last_ping >= 20:
                    ws.send(json.dumps({"event": "ping"}))
                    last_ping = now

                # Check for incoming teacher commands (non-blocking)
                ws.settimeout(0.05)
                try:
                    msg = ws.recv()
                    if msg:
                        payload = json.loads(msg)
                        if payload.get("event") == "teacher_command" and _cmd_callback:
                            _cmd_callback(payload)
                except Exception:
                    pass
                ws.settimeout(None)

                time.sleep(0.04)

        except Exception as e:
            log.warning("[Cloud] WS error: %s — reconnecting in %ds", e, backoff)
            _ws = None
            if not _stop_ws:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
