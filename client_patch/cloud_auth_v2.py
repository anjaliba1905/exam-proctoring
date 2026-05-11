"""
client_patch/cloud_auth_v2.py
==============================
Drop-in replacement for cloud_auth.py.

Adds:
  • Live frame streaming via LiveFrameSender (starts automatically after login)
  • All original methods preserved (login, start_session, end_session)

SWAP IN:
  In main_app.py (or wherever you import cloud_auth):
    # OLD: from cloud_auth import CloudAuth
    # NEW:
    from client_patch.cloud_auth_v2 import CloudAuthV2 as CloudAuth
"""

import os, time, logging, requests
from typing import Optional

from client_patch.camera_ws_sender import LiveFrameSender

log = logging.getLogger("cloud_auth_v2")

API_URL          = os.environ.get("PROCTORING_API_URL", "").rstrip("/")
_LOGIN_TIMEOUT   = 65
_DEFAULT_TIMEOUT = 10
_MAX_RETRIES     = 2


class CloudAuthV2:
    def __init__(self, api_url: Optional[str] = None):
        self._api_url    = (api_url or API_URL).rstrip("/")
        self._token: str = ""
        self._session_id: int = 0
        self._online: bool    = False
        self._student_id: str = ""
        self._name: str       = ""
        self._sender: Optional[LiveFrameSender] = None

    # ── Public ────────────────────────────────────────────────────────────────

    def login(self, student_id: str, password: str) -> bool:
        if not self._api_url:
            log.warning("[CloudAuthV2] PROCTORING_API_URL not set — offline mode.")
            return False
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    f"{self._api_url}/auth/token",
                    data={"username": student_id, "password": password,
                          "grant_type": "password"},
                    timeout=_LOGIN_TIMEOUT,
                )
                if resp.status_code == 200:
                    data             = resp.json()
                    self._token      = data["access_token"]
                    self._student_id = student_id
                    self._name       = data.get("student", {}).get("name", student_id)
                    os.environ["PROCTORING_AUTH_TOKEN"] = self._token
                    self._online = True
                    log.info("[CloudAuthV2] Logged in: %s (%s)", student_id, self._name)
                    return True
                elif resp.status_code == 401:
                    log.warning("[CloudAuthV2] Bad credentials for %s", student_id)
                    return False
                else:
                    log.warning("[CloudAuthV2] HTTP %d: %s", resp.status_code, resp.text[:120])
                    if attempt < _MAX_RETRIES:
                        time.sleep(2)
            except requests.Timeout:
                log.warning("[CloudAuthV2] Timeout attempt %d", attempt)
                if attempt < _MAX_RETRIES:
                    time.sleep(5)
            except requests.RequestException as e:
                log.warning("[CloudAuthV2] Network error: %s — offline.", e)
                return False
        return False

    def start_session(self, exam_id: str = "default") -> int:
        if not self._token:
            return 0
        try:
            resp = requests.post(
                f"{self._api_url}/sessions/start",
                json={"exam_id": exam_id},
                headers=self._auth_headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            if resp.status_code == 200:
                self._session_id = resp.json()["session_id"]
                log.info("[CloudAuthV2] Session started: id=%d", self._session_id)
                # Start live frame streaming NOW
                self._start_live_sender()
                return self._session_id
        except Exception as e:
            log.warning("[CloudAuthV2] start_session error: %s", e)
        return 0

    def end_session(self, session_id: int, risk_score: float, risk_level: str):
        # Stop live feed first
        if self._sender:
            self._sender.stop()
            self._sender = None

        if not self._token or not session_id:
            return
        try:
            requests.post(
                f"{self._api_url}/sessions/end",
                json={"session_id": session_id,
                      "final_risk_score": float(risk_score),
                      "risk_level": str(risk_level)},
                headers=self._auth_headers(),
                timeout=_DEFAULT_TIMEOUT,
            )
            log.info("[CloudAuthV2] Session %d ended. Risk=%.1f %s",
                     session_id, risk_score, risk_level)
        except Exception as e:
            log.warning("[CloudAuthV2] end_session error: %s", e)

    # ── Live sender bridge ────────────────────────────────────────────────────

    def push_camera_frame(self, bgr_frame):
        """Call from camera_monitor.py with the latest numpy BGR frame."""
        if self._sender:
            self._sender.push_camera_frame(bgr_frame)

    def push_screen_frame(self, pil_img):
        """Call from screen_monitor.py with the latest PIL screenshot."""
        if self._sender:
            self._sender.push_screen_frame(pil_img)

    def update_risk(self, risk_score: float, risk_level: str, violations: list):
        """Call from risk_scoring.py whenever risk changes."""
        if self._sender:
            self._sender.update_risk(risk_score, risk_level, violations)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def token(self) -> str:
        return self._token

    @property
    def session_id(self) -> int:
        return self._session_id

    @property
    def is_online(self) -> bool:
        return self._online

    @property
    def sender(self) -> Optional[LiveFrameSender]:
        return self._sender

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _start_live_sender(self):
        self._sender = LiveFrameSender(
            token      = self._token,
            session_id = self._session_id,
            student_id = self._student_id,
            name       = self._name,
            api_url    = self._api_url,
            camera_fps = 1.0,
            screen_fps = 0.5,
        )
        self._sender.start()
        log.info("[CloudAuthV2] Live frame sender started for session %d.", self._session_id)


# ── Module-level shims (backward compat) ──────────────────────────────────────
_default_auth = CloudAuthV2()

def cloud_login(student_id: str, password: str) -> bool:
    return _default_auth.login(student_id, password)

def cloud_start_session(exam_id: str = "default") -> int:
    return _default_auth.start_session(exam_id)

def cloud_end_session(session_id: int, risk_score: float, risk_level: str):
    _default_auth.end_session(session_id, risk_score, risk_level)
