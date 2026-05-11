"""
client_patch/camera_ws_sender.py
=================================
Drop-in enhancement for the student desktop client.

Streams camera + screen frames to the server's /ws/student/<token> endpoint
so the teacher can see them live in the web dashboard.

USAGE — add to exam_window.py or main_app.py after login:

    from client_patch.camera_ws_sender import LiveFrameSender
    sender = LiveFrameSender(token=cloud_auth.token,
                             session_id=cloud_auth.session_id)
    sender.start()

    # In camera monitor callback, after you have a frame:
    sender.push_camera_frame(frame)           # numpy array (BGR)

    # In screen monitor callback, after you have a screenshot:
    sender.push_screen_frame(pil_screenshot)  # PIL Image

    # On exam end:
    sender.stop()
"""

import base64, json, logging, threading, time, queue
import numpy as np
import cv2
import os

try:
    import websocket as _websocket_client
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

try:
    from PIL import Image
    import io as _io
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

log = logging.getLogger("live_sender")

API_URL = os.environ.get("PROCTORING_API_URL", "").rstrip("/")


def _to_ws_url(api_url: str) -> str:
    return api_url.replace("https://", "wss://").replace("http://", "ws://")


def _encode_frame(bgr_frame, quality: int = 60) -> str:
    """Encode BGR numpy frame → base64 JPEG string."""
    ok, buf = cv2.imencode(".jpg", bgr_frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return ""
    return base64.b64encode(buf.tobytes()).decode()


def _encode_pil(pil_img, quality: int = 50) -> str:
    """Encode PIL image → base64 JPEG string."""
    if not _HAS_PIL:
        return ""
    buf = _io.BytesIO()
    pil_img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


class LiveFrameSender:
    """
    Background thread that:
      1. Connects to /ws/student/<token>
      2. Sends camera frames at ~1 fps
      3. Sends screen frames at ~0.5 fps
      4. Reconnects automatically on disconnect
    """

    def __init__(
        self,
        token: str,
        session_id: int,
        student_id: str = "",
        name: str = "",
        api_url: str = "",
        camera_fps: float = 1.0,
        screen_fps: float = 0.5,
    ):
        self._token      = token
        self._session_id = session_id
        self._student_id = student_id
        self._name       = name
        self._api_url    = (api_url or API_URL).rstrip("/")
        self._cam_fps    = camera_fps
        self._scr_fps    = screen_fps

        self._cam_q: queue.Queue = queue.Queue(maxsize=2)
        self._scr_q: queue.Queue = queue.Queue(maxsize=2)

        self._risk_score: float = 0.0
        self._risk_level: str  = "Low Risk"
        self._violations: list  = []

        self._ws       = None
        self._running  = False
        self._thread   = None
        self._lock     = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self):
        if not _HAS_WS:
            log.warning("[LiveSender] websocket-client not installed — live feed disabled.")
            return
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("[LiveSender] Started.")

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        log.info("[LiveSender] Stopped.")

    def push_camera_frame(self, bgr_frame):
        """Call from camera monitor thread with a numpy BGR frame."""
        if not self._running:
            return
        try:
            # Non-blocking: drop old frame if queue full
            if self._cam_q.full():
                try:
                    self._cam_q.get_nowait()
                except queue.Empty:
                    pass
            self._cam_q.put_nowait(bgr_frame)
        except Exception:
            pass

    def push_screen_frame(self, pil_img):
        """Call from screen monitor thread with a PIL Image."""
        if not self._running:
            return
        try:
            if self._scr_q.full():
                try:
                    self._scr_q.get_nowait()
                except queue.Empty:
                    pass
            self._scr_q.put_nowait(pil_img)
        except Exception:
            pass

    def update_risk(self, risk_score: float, risk_level: str, violations: list):
        """Called from risk scorer to keep metadata current."""
        with self._lock:
            self._risk_score = risk_score
            self._risk_level = risk_level
            self._violations = violations[-5:] if violations else []

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_payload(self, frame_type: str, data: str) -> str:
        with self._lock:
            return json.dumps({
                "type":       frame_type,
                "data":       data,
                "student_id": self._student_id,
                "session_id": self._session_id,
                "name":       self._name,
                "risk_score": self._risk_score,
                "risk_level": self._risk_level,
                "violations": self._violations,
                "ts":         time.time(),
            })

    def _run(self):
        ws_base = _to_ws_url(self._api_url)
        url     = f"{ws_base}/ws/student/{self._token}"
        cam_interval = 1.0 / max(self._cam_fps, 0.1)
        scr_interval = 1.0 / max(self._scr_fps, 0.1)
        last_cam = 0.0
        last_scr = 0.0

        while self._running:
            try:
                ws = _websocket_client.create_connection(url, timeout=15)
                self._ws = ws
                log.info("[LiveSender] Connected to %s", url)

                while self._running:
                    now = time.time()

                    # Camera frame
                    if now - last_cam >= cam_interval:
                        try:
                            frame = self._cam_q.get_nowait()
                            b64   = _encode_frame(frame)
                            if b64:
                                ws.send(self._build_payload("camera", b64))
                            last_cam = now
                        except queue.Empty:
                            pass

                    # Screen frame
                    if now - last_scr >= scr_interval:
                        try:
                            pil   = self._scr_q.get_nowait()
                            b64   = _encode_pil(pil)
                            if b64:
                                ws.send(self._build_payload("screen", b64))
                            last_scr = now
                        except queue.Empty:
                            pass

                    time.sleep(0.05)

            except Exception as e:
                log.warning("[LiveSender] WS error: %s — reconnecting in 5 s", e)
                self._ws = None
                if self._running:
                    time.sleep(5)
