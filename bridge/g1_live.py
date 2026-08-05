"""
Real-time G1 window — the "watch it move while you type" backend.

Difference from g1_sim_mujoco.py
---------------------------------
`g1_sim_mujoco.py` renders a gesture to an **mp4 file** (offline, one clip at a
time). This module instead keeps a MuJoCo model **live** in a background thread
and streams the current frame to the browser as MJPEG. You type a sentence on
the left, the robot on the right moves *now*, and you screen-record it. No file
is ever produced.

Shared truth
------------
The camera, the gesture library, the Segment interpolation and the smoothstep
easing all come from ``g1_sim_mujoco`` — so a gesture looks identical whether
you export it to mp4 or watch it live. Only the *sink* differs (mp4 vs. stream).

Threading model
---------------
MuJoCo's ``Renderer`` owns a GL context and must be touched from exactly one
thread. So a single **render thread** owns model/data/renderer and is the only
thing that calls MuJoCo. The web handlers never touch MuJoCo; they only push
action names into a thread-safe queue and read the latest JPEG bytes under a
lock. This keeps the GL context single-threaded and the server responsive.
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

from .g1_sim_mujoco import (
    Segment,
    SimUnavailable,
    build_actions,
    make_camera,
    _DEFAULT_MODEL,
    _MUJOCO_AVAILABLE,
)

logger = logging.getLogger("g1.live")

if _MUJOCO_AVAILABLE:
    import mujoco  # type: ignore


# ---------------------------------------------------------------------------
# Natural-language -> whitelisted action. Deliberately tiny + keyword-based:
# the agent/bankr layer can send a clean action name, but a human typing in the
# left box should also be able to write "wave hello" or "sit down" and have it
# work. First keyword that matches wins; order matters for overlaps.
# ---------------------------------------------------------------------------
_NL_RULES: list[tuple[tuple[str, ...], str]] = [
    (("wave", "hello", "hi ", "hey", "挥手", "打招呼", "你好"), "wave"),
    (("shake", "handshake", "握手"), "shake_hand"),
    (("nod", "yes", "agree", "点头"), "nod"),
    (("sit", "坐下", "坐"), "sit"),
    (("stand up", "get up", "起立", "站起"), "stand_up"),
    (("high", "tall", "踮", "站高"), "high_stand"),
    (("low", "crouch", "蹲", "下蹲"), "low_stand"),
    (("balance", "stand", "站好", "站立", "立正"), "balance_stand"),
    (("damp", "relax", "rest", "放松"), "damp"),
]


def resolve_action(text: str) -> Optional[str]:
    """Map free text to a gesture name, or None if nothing matches."""
    t = f" {text.strip().lower()} "
    # exact action name shortcut
    exact = text.strip().lower()
    actions = build_actions()
    if exact in actions:
        return exact
    for keywords, action in _NL_RULES:
        for kw in keywords:
            if kw in t:
                return action
    return None


class G1LiveEngine:
    """A persistent MuJoCo G1 that plays queued gestures and exposes the latest
    rendered frame as JPEG bytes for MJPEG streaming."""

    def __init__(
        self,
        model_path: str | None = None,
        width: int = 960,
        height: int = 720,
        fps: int = 30,
    ) -> None:
        if not _MUJOCO_AVAILABLE:
            raise SimUnavailable(
                "mujoco not importable. Install it in the bridge venv:\n"
                "  pip install mujoco 'imageio[ffmpeg]' pillow"
            )
        self.model_path = model_path or _DEFAULT_MODEL
        if not os.path.exists(self.model_path):
            raise SimUnavailable(f"G1 model not found: {self.model_path}")

        self.width = width
        self.height = height
        self.fps = fps
        self._actions = build_actions()
        self._camera = make_camera()

        # Loaded lazily inside the render thread so the GL context is created on
        # the same thread that will use it.
        self.model = None
        self.data = None
        self._qadr: dict[str, int] = {}
        self._q_neutral: Optional[np.ndarray] = None
        self._base_z0 = 0.0

        # animation state (only touched by the render thread)
        self._cur_q: Optional[np.ndarray] = None
        self._cur_dz = 0.0
        self._pending: deque[np.ndarray] = deque()  # queued interpolated frames

        # cross-thread command inbox + latest-frame mailbox
        self._cmd_lock = threading.Lock()
        self._cmd_inbox: deque[str] = deque()
        self._frame_lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._last_action: str = "idle"
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- public, thread-safe API -------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="g1-live", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def has_action(self, name: str) -> bool:
        return name in self._actions

    def list_actions(self) -> list[str]:
        return sorted(self._actions)

    def enqueue(self, action: str) -> bool:
        """Queue a gesture by name. Returns False if it's not a known gesture."""
        if action not in self._actions:
            return False
        with self._cmd_lock:
            self._cmd_inbox.append(action)
        return True

    def latest_jpeg(self) -> Optional[bytes]:
        with self._frame_lock:
            return self._latest_jpeg

    @property
    def last_action(self) -> str:
        return self._last_action

    # -- render thread ------------------------------------------------------
    def _run(self) -> None:
        try:
            self._load_model()
        except Exception:
            logger.exception("g1-live: model load failed")
            return

        renderer = mujoco.Renderer(self.model, height=self.height, width=self.width)
        frame_dt = 1.0 / self.fps
        try:
            # prime one frame so the stream isn't blank before any command
            self._render_current(renderer)
            while not self._stop.is_set():
                t0 = time.perf_counter()

                # 1) drain any new commands into interpolated frames
                self._drain_commands()

                # 2) advance one animation frame (or hold if idle)
                if self._pending:
                    self._cur_q = self._pending.popleft()

                # 3) render whatever the current pose is
                self._render_current(renderer)

                # 4) pace to fps
                dt = time.perf_counter() - t0
                if dt < frame_dt:
                    time.sleep(frame_dt - dt)
        finally:
            renderer.close()

    def _load_model(self) -> None:
        model = mujoco.MjModel.from_xml_path(self.model_path)
        model.vis.global_.offwidth = max(self.width, int(model.vis.global_.offwidth))
        model.vis.global_.offheight = max(self.height, int(model.vis.global_.offheight))
        self.model = model
        self.data = mujoco.MjData(model)

        for j in range(model.njnt):
            name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, j)
            if name is not None:
                self._qadr[name] = int(model.jnt_qposadr[j])

        self._q_neutral = model.qpos0.copy()
        self._base_z0 = float(self._q_neutral[2])
        self._cur_q = self._q_neutral.copy()
        self._cur_dz = 0.0
        logger.info(
            "g1-live ready: nq=%d model=%s", model.nq, os.path.basename(self.model_path)
        )

    def _drain_commands(self) -> None:
        with self._cmd_lock:
            if not self._cmd_inbox:
                return
            cmds = list(self._cmd_inbox)
            self._cmd_inbox.clear()
        for action in cmds:
            segs = self._actions.get(action)
            if not segs:
                continue
            self._queue_segments(segs)
            self._last_action = action

    def _queue_segments(self, segments: list[Segment]) -> None:
        """Expand segments into per-frame qpos arrays, appended to _pending.

        Interpolation matches g1_sim_mujoco._frames_for exactly (smoothstep from
        the *current* pose), so a live gesture and its mp4 twin are identical.
        Starts from where the robot currently is (end of the queue if busy).
        """
        assert self._q_neutral is not None
        cur = (self._pending[-1].copy() if self._pending else self._cur_q).copy()
        # recover current dz from the base-z channel
        cur_dz = float(cur[2]) - self._base_z0
        for seg in segments:
            target = cur.copy()
            for jname, val in seg.offsets.items():
                adr = self._qadr.get(jname)
                if adr is None:
                    continue
                target[adr] = self._q_neutral[adr] + val
            nframes = max(1, int(round(seg.seconds * self.fps)))
            for k in range(1, nframes + 1):
                t = _smoothstep(k / nframes)
                q = cur + (target - cur) * t
                dz = cur_dz + (seg.base_dz - cur_dz) * t
                q[2] = self._base_z0 + dz
                self._pending.append(q)
            cur = self._pending[-1].copy()
            cur_dz = seg.base_dz

    def _render_current(self, renderer) -> None:
        self.data.qpos[:] = self._cur_q
        mujoco.mj_forward(self.model, self.data)
        renderer.update_scene(self.data, camera=self._camera)
        rgb = renderer.render()
        jpeg = _encode_jpeg(rgb)
        with self._frame_lock:
            self._latest_jpeg = jpeg


def _smoothstep(x: float) -> float:
    return x * x * (3.0 - 2.0 * x)


def _encode_jpeg(rgb: np.ndarray) -> bytes:
    """RGB uint8 HxWx3 -> JPEG bytes. Prefers Pillow, falls back to imageio."""
    try:
        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    except Exception:
        import imageio.v2 as imageio

        buf = io.BytesIO()
        imageio.imwrite(buf, rgb, format="JPEG")
        return buf.getvalue()
