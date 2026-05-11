"""
client_patch/camera_monitor_patch.py
=====================================
Minimal patch for monitoring/camera_monitor.py to push frames to teacher.

HOW TO APPLY:
  In monitoring/camera_monitor.py, find the method that processes each
  camera frame (usually called `_process_frame` or inside the capture loop).

  Add these two lines after you have a detected face/frame:

    # At top of camera_monitor.py:
    from client_patch.camera_monitor_patch import push_frame_to_teacher

    # Inside your capture loop, after you have `frame` (numpy BGR array):
    push_frame_to_teacher(frame)

That's it. No other changes needed.
"""

import logging
log = logging.getLogger("cam_patch")

_sender = None   # Set by cloud_auth_v2 after login


def set_sender(sender):
    """Called by CloudAuthV2._start_live_sender() after it creates the sender."""
    global _sender
    _sender = sender
    log.info("[CamPatch] Sender attached.")


def push_frame_to_teacher(bgr_frame):
    """Push the latest BGR frame to the live feed. Safe to call every frame."""
    if _sender is not None:
        _sender.push_camera_frame(bgr_frame)


def push_screen_to_teacher(pil_img):
    """Push the latest screen screenshot to the live feed."""
    if _sender is not None:
        _sender.push_screen_frame(pil_img)
