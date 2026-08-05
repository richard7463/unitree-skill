"""
MuJoCo simulation backend for the G1 bridge.

Why this exists
---------------
The `mock` backend proves the timeline -> bridge -> action path with zero
dependencies, but nothing moves: it just returns text. For a demo video you
want to actually *see* a G1 do the thing. This backend loads the real Unitree
G1 MJCF, animates a whitelisted action, and renders it off-screen to an mp4.

Design choices (deliberate, read before changing)
-------------------------------------------------
* **Kinematic, not dynamic.** We interpolate joint targets and call
  ``mj_forward`` (pose the model) rather than ``mj_step`` (simulate physics).
  A free-floating humanoid under gravity needs a balance controller to not
  fall over; that controller lives on the robot's onboard computer, *not* in
  ``unitree_mujoco``. For safe, standing-in-place gestures (wave / nod /
  squat) kinematic playback is rock solid and never tips over — which is
  exactly the class of action this backend is allowed to perform.
* **Off-screen rendering.** We use ``mujoco.Renderer`` (EGL/CGL offscreen), so
  we never open the interactive viewer. On macOS the interactive viewer needs
  ``mjpython``; off-screen rendering does not. Output is a plain mp4.
* **Whitelist parity.** Action names match ``g1_controller.py`` exactly, so the
  same agent command drives mock / sim / real with no branching upstream.

What it will NOT do
-------------------
Anything that requires a walking / balance policy (``walk_forward``, ``turn``).
Those are refused here and only make sense on the real robot (or a full
``unitree_mujoco`` + RL-policy setup, which is out of scope for a gesture demo).
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

logger = logging.getLogger("g1.sim")

# ---------------------------------------------------------------------------
# MuJoCo is optional at import time so `mock` mode never pays for it. The
# controller only imports this module when G1_MODE=sim.
# ---------------------------------------------------------------------------
try:
    import mujoco  # type: ignore

    _MUJOCO_AVAILABLE = True
except Exception:  # noqa: BLE001
    mujoco = None  # type: ignore
    _MUJOCO_AVAILABLE = False


# Default model path. Reuses the fully-rigged G1 MJCF that ships with
# unitree_lerobot (64 STL meshes, hands included) so we don't vendor assets.
# Override with G1_SIM_MODEL if yours lives elsewhere.
_DEFAULT_MODEL = os.getenv(
    "G1_SIM_MODEL",
    "/Users/yanqing/Documents/GitHub/unitree_lerobot/unitree_lerobot/"
    "eval_robot/assets/g1/g1_body29_hand14.xml",
)


@dataclass
class Segment:
    """One keyframe of an animation: reach these joint offsets over `seconds`.

    offsets: joint_name -> radians, added on top of the model's neutral pose
             (qpos0). Missing joints stay at their previous value.
    base_dz: vertical shift of the floating base in metres (negative = crouch),
             so squats visually lower the whole body.
    """

    offsets: dict[str, float] = field(default_factory=dict)
    seconds: float = 0.6
    base_dz: float = 0.0


class SimUnavailable(RuntimeError):
    pass


class G1SimBackend:
    """Loads the G1 model once and renders whitelisted gestures to mp4."""

    def __init__(
        self,
        model_path: str | None = None,
        width: int = 960,
        height: int = 720,
        fps: int = 30,
        out_dir: str | None = None,
    ) -> None:
        if not _MUJOCO_AVAILABLE:
            raise SimUnavailable(
                "mujoco not importable. Install it in the bridge venv:\n"
                "  pip install mujoco imageio 'imageio[ffmpeg]'\n"
                "(on a slow link use -i https://pypi.tuna.tsinghua.edu.cn/simple)"
            )
        self.model_path = model_path or _DEFAULT_MODEL
        if not os.path.exists(self.model_path):
            raise SimUnavailable(f"G1 model not found: {self.model_path}")

        self.width = width
        self.height = height
        self.fps = fps
        self.out_dir = out_dir or os.getenv(
            "G1_SIM_OUT", os.path.join(os.getcwd(), "sim_out")
        )
        os.makedirs(self.out_dir, exist_ok=True)

        self.model = mujoco.MjModel.from_xml_path(self.model_path)
        # The MJCF's default offscreen framebuffer is 640x480; enlarge it so we
        # can render at the requested resolution without patching the XML.
        self.model.vis.global_.offwidth = max(self.width, int(self.model.vis.global_.offwidth))
        self.model.vis.global_.offheight = max(self.height, int(self.model.vis.global_.offheight))
        self.data = mujoco.MjData(self.model)

        # joint name -> qpos address (only 1-DoF hinge joints matter to us)
        self._qadr: dict[str, int] = {}
        for j in range(self.model.njnt):
            name = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_JOINT, j)
            if name is not None:
                self._qadr[name] = int(self.model.jnt_qposadr[j])

        # neutral standing pose; base z lives at qpos[2]
        self._q_neutral = self.model.qpos0.copy()
        self._base_z0 = float(self._q_neutral[2])

        self._camera = make_camera()
        self._actions = build_actions()
        logger.info(
            "sim backend ready: nq=%d nu=%d model=%s",
            self.model.nq, self.model.nu, os.path.basename(self.model_path),
        )

    # -- public API ---------------------------------------------------------
    def has_action(self, name: str) -> bool:
        return name in self._actions

    def list_actions(self) -> list[str]:
        return sorted(self._actions)

    def render_action(self, name: str, filename: str | None = None) -> str:
        """Animate `name` and write an mp4. Returns the output path."""
        segs = self._actions.get(name)
        if segs is None:
            raise KeyError(f"sim has no animation for action '{name}'")
        frames = self._frames_for(segs)
        out = filename or os.path.join(self.out_dir, f"g1_{name}.mp4")
        self._encode(frames, out)
        logger.info("sim rendered %s -> %s (%d frames)", name, out, len(frames))
        return out

    # -- animation ----------------------------------------------------------
    def _frames_for(self, segments: list[Segment]) -> list[np.ndarray]:
        renderer = mujoco.Renderer(self.model, height=self.height, width=self.width)
        frames: list[np.ndarray] = []
        try:
            cur = self._q_neutral.copy()
            cur_dz = 0.0
            # start with a brief hold on the neutral pose
            for seg in segments:
                target = cur.copy()
                for jname, val in seg.offsets.items():
                    adr = self._qadr.get(jname)
                    if adr is None:
                        logger.warning("sim: unknown joint '%s' ignored", jname)
                        continue
                    target[adr] = self._q_neutral[adr] + val
                nframes = max(1, int(round(seg.seconds * self.fps)))
                for k in range(1, nframes + 1):
                    t = self._ease(k / nframes)
                    q = cur + (target - cur) * t
                    dz = cur_dz + (seg.base_dz - cur_dz) * t
                    q[2] = self._base_z0 + dz
                    self.data.qpos[:] = q
                    mujoco.mj_forward(self.model, self.data)
                    renderer.update_scene(self.data, camera=self._camera)
                    frames.append(renderer.render().copy())
                cur = target
                cur_dz = seg.base_dz
        finally:
            renderer.close()
        return frames

    @staticmethod
    def _ease(x: float) -> float:
        # smoothstep: 0->0, 1->1, zero velocity at both ends (natural motion)
        return x * x * (3.0 - 2.0 * x)

    def _encode(self, frames: list[np.ndarray], path: str) -> None:
        import imageio.v2 as imageio

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        imageio.mimsave(path, frames, fps=self.fps, quality=8)


# ---------------------------------------------------------------------------
# Shared, self-less helpers. Both the mp4 backend (G1SimBackend) and the
# real-time window (bridge/g1_live.py) build the exact same camera + gesture
# library from here, so a gesture looks identical whether you render it to a
# file or watch it live in the browser.
# ---------------------------------------------------------------------------
def make_camera():
    cam = mujoco.MjvCamera()
    cam.lookat[:] = [0.0, 0.0, 0.9]
    cam.distance = 3.2
    cam.azimuth = 135.0
    cam.elevation = -12.0
    return cam


def build_actions() -> dict[str, list[Segment]]:
    """Gesture library. Angles are radians added on top of the neutral
    standing pose; tuned so the whole body stays plausibly standing (no
    physics involved)."""
    # --- wave: raise right arm, swing forearm a few times ---
    wave_up = {
        "right_shoulder_pitch_joint": -0.5,
        "right_shoulder_roll_joint": -1.15,
        "right_elbow_joint": 1.1,
    }
    wave: list[Segment] = [Segment(wave_up, 0.6)]
    for _ in range(3):
        wave.append(Segment({**wave_up, "right_shoulder_roll_joint": -0.7}, 0.28))
        wave.append(Segment({**wave_up, "right_shoulder_roll_joint": -1.35}, 0.28))
    wave.append(Segment({}, 0.6))  # return to neutral

    # --- shake_hand: extend right arm forward, small pump, retract ---
    reach = {
        "right_shoulder_pitch_joint": -0.7,
        "right_shoulder_roll_joint": -0.15,
        "right_elbow_joint": 0.9,
    }
    shake = [
        Segment(reach, 0.7),
        Segment({**reach, "right_elbow_joint": 1.05}, 0.25),
        Segment({**reach, "right_elbow_joint": 0.8}, 0.25),
        Segment({**reach, "right_elbow_joint": 1.05}, 0.25),
        Segment(reach, 0.3),
        Segment({}, 0.6),
    ]

    # --- nod: no neck joint, so bow via waist pitch (approx nod) ---
    nod = [
        Segment({"waist_pitch_joint": 0.28}, 0.4),
        Segment({"waist_pitch_joint": 0.0}, 0.4),
        Segment({"waist_pitch_joint": 0.28}, 0.4),
        Segment({"waist_pitch_joint": 0.0}, 0.4),
    ]

    # --- squat family: bend hips+knees, drop the base ---
    squat_pose = {
        "left_hip_pitch_joint": -0.6, "right_hip_pitch_joint": -0.6,
        "left_knee_joint": 1.2, "right_knee_joint": 1.2,
        "left_ankle_pitch_joint": -0.6, "right_ankle_pitch_joint": -0.6,
    }
    low_stand = [Segment(squat_pose, 0.9, base_dz=-0.18), Segment({}, 0.9)]
    sit_pose = {
        "left_hip_pitch_joint": -1.1, "right_hip_pitch_joint": -1.1,
        "left_knee_joint": 1.6, "right_knee_joint": 1.6,
        "left_ankle_pitch_joint": -0.5, "right_ankle_pitch_joint": -0.5,
    }
    sit = [Segment(sit_pose, 1.1, base_dz=-0.3)]
    stand_up = [Segment(sit_pose, 0.1, base_dz=-0.3), Segment({}, 1.1)]

    # --- high_stand: subtle rise onto toes / tall posture ---
    high = [Segment({}, 0.5, base_dz=0.06), Segment({}, 0.6, base_dz=0.06)]

    # --- damp: relax arms slightly down (soft posture) ---
    damp = [Segment({
        "left_shoulder_roll_joint": 0.1, "right_shoulder_roll_joint": -0.1,
        "left_elbow_joint": 0.2, "right_elbow_joint": 0.2,
    }, 0.9)]

    # neutral standing (balance_stand / stand still)
    stand = [Segment({}, 0.8)]

    return {
        "wave": wave,
        "shake_hand": shake,
        "nod": nod,
        "sit": sit,
        "stand_up": stand_up,
        "balance_stand": stand,
        "high_stand": high,
        "low_stand": low_stand,
        "damp": damp,
    }


# ---------------------------------------------------------------------------
# CLI: render a gesture straight to mp4 (used to author / sanity-check clips).
#   python -m bridge.g1_sim_mujoco wave
#   python -m bridge.g1_sim_mujoco --list
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Render a G1 gesture to mp4.")
    ap.add_argument("action", nargs="?", default="wave")
    ap.add_argument("--list", action="store_true", help="list available gestures")
    ap.add_argument("--out", default=None, help="output mp4 path")
    ap.add_argument("--width", type=int, default=960)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sim = G1SimBackend(width=args.width, height=args.height, fps=args.fps)
    if args.list:
        print("gestures:", ", ".join(sim.list_actions()))
        return 0
    path = sim.render_action(args.action, filename=args.out)
    print(path)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
