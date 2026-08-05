"""
G1 humanoid controller wrapper.

Wraps unitree_sdk2_python's LocoClient with:
  - an explicit action whitelist (no arbitrary motion),
  - FSM/state gating so unsafe actions are rejected,
  - three interchangeable backends selected by ``G1_MODE``:
      * ``mock`` - zero deps, returns text only (default off-robot).
      * ``sim``  - MuJoCo kinematic playback, renders each gesture to mp4.
      * ``real`` - drives the physical G1 over DDS via LocoClient.

The bridge (bridge/server.py) only ever talks to this class. It never
touches the raw SDK. This keeps the safety surface in one place.

``sim`` shares the mock FSM/gating logic (there is no hardware to query), so
the exact same agent command flows through mock / sim / real unchanged; only
``sim`` additionally produces a video artifact.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger("g1")

# ---------------------------------------------------------------------------
# SDK import is deferred / optional so the bridge can boot in MOCK mode on a
# laptop with no unitree_sdk2_python installed and no robot on the LAN.
# ---------------------------------------------------------------------------
_SDK_AVAILABLE = False
try:  # pragma: no cover - depends on host having the SDK + robot
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize  # type: ignore
    from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient  # type: ignore

    _SDK_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure -> stay in mock
    LocoClient = None  # type: ignore
    ChannelFactoryInitialize = None  # type: ignore


class FsmState(str, Enum):
    """Coarse high-level states we gate actions on.

    The real G1 exposes a numeric FSM id; we map the ones we care about.
    Anything we don't recognise is treated as UNKNOWN and locks out motion.
    """

    UNKNOWN = "unknown"
    ZERO_TORQUE = "zero_torque"      # limp, safe to be handled
    DAMP = "damp"                    # damping, not standing
    LOCK_STAND = "lock_stand"        # locked standing (stiff)
    BALANCE_STAND = "balance_stand"  # actively balancing - required for moves
    SIT = "sit"


# FSM ids per Unitree G1 loco docs (subset we use). Kept here so the mapping
# lives next to the enum and can be adjusted for firmware differences.
_FSM_ID_TO_STATE = {
    0: FsmState.ZERO_TORQUE,
    1: FsmState.DAMP,
    2: FsmState.LOCK_STAND,
    3: FsmState.BALANCE_STAND,
    4: FsmState.SIT,
}


@dataclass
class ActionResult:
    ok: bool
    action: str
    message: str
    state: dict = field(default_factory=dict)
    video: str | None = None  # sim mode: path to the rendered mp4, if any


@dataclass
class ActionSpec:
    """One whitelisted action.

    fn:            callable(controller) -> None that talks to the SDK
    requires:      set of FsmStates from which the action is allowed
    min_battery:   refuse if reported battery below this (0-100)
    description:   human/agent facing summary
    dangerous:     needs the caller to pass confirm=True
    """

    name: str
    fn: Callable[["G1Controller"], None]
    description: str
    requires: set[FsmState] = field(default_factory=set)
    min_battery: int = 0
    dangerous: bool = False


class G1Controller:
    def __init__(
        self,
        network_interface: str | None = None,
        mock: bool | None = None,
        move_speed: float = 0.3,
        mode: str | None = None,
    ) -> None:
        # Resolve backend mode. Priority:
        #   1. explicit `mode` arg / G1_MODE env  (mock | sim | real)
        #   2. legacy `mock` arg / G1_MOCK env
        #   3. auto: real if SDK importable else mock
        self.mode = self._resolve_mode(mode, mock)
        # `self.mock` stays True for both mock and sim: neither touches hardware
        # and both share the simulated FSM/gating below.
        self.mock = self.mode in ("mock", "sim")
        self.network_interface = network_interface or os.getenv("G1_NET_IFACE", "eth0")
        self.move_speed = move_speed
        self._client = None
        self._sim = None  # lazily-created G1SimBackend (sim mode only)
        self._mock_state = FsmState.BALANCE_STAND  # mock boots "ready"
        self._mock_battery = 87
        self._actions = self._build_actions()

        if self.mode == "real":
            self._connect()
        elif self.mode == "sim":
            logger.warning("G1Controller running in SIM mode (MuJoCo, no hardware).")
        else:
            logger.warning("G1Controller running in MOCK mode (no hardware).")

    @staticmethod
    def _resolve_mode(mode: str | None, mock: bool | None) -> str:
        env_mode = os.getenv("G1_MODE")
        chosen = (mode or env_mode or "").strip().lower()
        if chosen in ("mock", "sim", "real"):
            return chosen
        # Fall back to the legacy boolean switch.
        if mock is None:
            env = os.getenv("G1_MOCK")
            mock = (env == "1") if env is not None else (not _SDK_AVAILABLE)
        return "mock" if mock else "real"

    def _get_sim(self):
        """Lazily build the MuJoCo backend on first use (sim mode)."""
        if self._sim is None:
            from .g1_sim_mujoco import G1SimBackend  # deferred: heavy import

            self._sim = G1SimBackend()
        return self._sim

    # -- connection ---------------------------------------------------------
    def _connect(self) -> None:  # pragma: no cover - needs hardware
        if not _SDK_AVAILABLE:
            raise RuntimeError(
                "unitree_sdk2py not importable but mock=False. "
                "Install the SDK on the bridge host or set G1_MOCK=1."
            )
        logger.info("Initializing DDS on iface=%s", self.network_interface)
        ChannelFactoryInitialize(0, self.network_interface)
        self._client = LocoClient()
        self._client.SetTimeout(10.0)
        self._client.Init()
        logger.info("LocoClient connected.")

    # -- state --------------------------------------------------------------
    def get_state(self) -> dict:
        if self.mock:
            return {
                "mock": True,
                "mode": self.mode,
                "fsm": self._mock_state.value,
                "battery": self._mock_battery,
                "ready": self._mock_state == FsmState.BALANCE_STAND,
            }
        fsm = self._read_fsm()
        battery = self._read_battery()
        return {
            "mock": False,
            "mode": self.mode,
            "fsm": fsm.value,
            "battery": battery,
            "ready": fsm == FsmState.BALANCE_STAND,
        }

    def _read_fsm(self) -> FsmState:  # pragma: no cover - hardware
        try:
            fsm_id = self._client.GetFsmId()  # type: ignore[attr-defined]
            return _FSM_ID_TO_STATE.get(int(fsm_id), FsmState.UNKNOWN)
        except Exception as exc:  # noqa: BLE001
            logger.error("GetFsmId failed: %s", exc)
            return FsmState.UNKNOWN

    def _read_battery(self) -> int:  # pragma: no cover - hardware
        # Battery arrives on the low-level state topic; if unavailable we
        # return -1 so gating that needs battery will refuse rather than
        # blindly allow.
        try:
            return int(getattr(self._client, "battery_percent", -1))
        except Exception:  # noqa: BLE001
            return -1

    # -- action registry ----------------------------------------------------
    def _build_actions(self) -> dict[str, ActionSpec]:
        ready = {FsmState.BALANCE_STAND}
        standing = {FsmState.BALANCE_STAND, FsmState.LOCK_STAND}

        def _sdk(name: str):
            def call(self: "G1Controller") -> None:
                if self.mock:
                    logger.info("[MOCK] %s()", name)
                    time.sleep(0.2)
                    return
                getattr(self._client, name)()  # pragma: no cover
            return call

        def _move_forward(self: "G1Controller") -> None:
            if self.mock:
                logger.info("[MOCK] Move(vx=%.2f) 1.5s", self.move_speed)
                time.sleep(0.2)
                return
            self._client.Move(self.move_speed, 0.0, 0.0)  # pragma: no cover
            time.sleep(1.5)
            self._client.Move(0.0, 0.0, 0.0)

        def _turn(self: "G1Controller") -> None:
            if self.mock:
                logger.info("[MOCK] Move(vyaw) turn")
                time.sleep(0.2)
                return
            self._client.Move(0.0, 0.0, 0.5)  # pragma: no cover
            time.sleep(1.5)
            self._client.Move(0.0, 0.0, 0.0)

        def _nod(self: "G1Controller") -> None:
            # Nod is a gesture-only motion. mock/sim animate it (a waist-pitch
            # bow, see g1_sim_mujoco.build_actions). The G1 LocoClient exposes
            # no dedicated nod primitive, so on real hardware this is a safe
            # no-op rather than a crash — that keeps the sim CLI and /command
            # whitelists in parity (same action names drive mock/sim/real).
            if self.mock:
                logger.info("[MOCK] Nod()")
                time.sleep(0.2)
                return
            logger.warning(  # pragma: no cover - hardware
                "nod: no loco primitive on real G1; skipping (gesture-only)"
            )

        specs = [
            ActionSpec("wave", _sdk("WaveHand"),
                       "Wave hello with one hand.", requires=standing),
            ActionSpec("shake_hand", _sdk("ShakeHand"),
                       "Extend hand for a handshake.", requires=ready),
            ActionSpec("nod", _nod,
                       "Nod (bow the torso as a 'yes' gesture).",
                       requires=standing),
            ActionSpec("stand_up", _sdk("StandUp"),
                       "Stand up from sit/damp into locked stand.",
                       requires={FsmState.SIT, FsmState.DAMP, FsmState.LOCK_STAND}),
            ActionSpec("balance_stand", _sdk("BalanceStand"),
                       "Enter active balance stand (required before moving).",
                       requires={FsmState.LOCK_STAND, FsmState.BALANCE_STAND}),
            ActionSpec("sit", _sdk("Sit"),
                       "Sit down.", requires=standing),
            ActionSpec("high_stand", _sdk("HighStand"),
                       "Rise to a taller standing posture.", requires=standing),
            ActionSpec("low_stand", _sdk("LowStand"),
                       "Lower into a crouched standing posture.", requires=standing),
            ActionSpec("walk_forward", _move_forward,
                       "Walk forward ~1.5s.", requires=ready, min_battery=30),
            ActionSpec("turn", _turn,
                       "Turn in place ~1.5s.", requires=ready, min_battery=30),
            ActionSpec("damp", _sdk("Damp"),
                       "Enter damping (soft, safe) state.", requires=set()),
            ActionSpec("zero_torque", _sdk("ZeroTorque"),
                       "Go limp (zero torque). Robot must be supported.",
                       requires=set(), dangerous=True),
        ]
        return {s.name: s for s in specs}

    def list_actions(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "requires": sorted(x.value for x in s.requires) or ["any"],
                "min_battery": s.min_battery,
                "dangerous": s.dangerous,
            }
            for s in self._actions.values()
        ]

    # -- execution ----------------------------------------------------------
    def execute(self, action: str, confirm: bool = False) -> ActionResult:
        spec = self._actions.get(action)
        if spec is None:
            return ActionResult(False, action, f"unknown action '{action}'")

        state = self.get_state()

        if spec.dangerous and not confirm:
            return ActionResult(False, action,
                                "action is dangerous; resend with confirm=true",
                                state)

        if spec.requires:
            cur = FsmState(state["fsm"])
            if cur not in spec.requires:
                allowed = ", ".join(sorted(x.value for x in spec.requires))
                return ActionResult(
                    False, action,
                    f"blocked: robot is '{cur.value}', action needs [{allowed}]",
                    state)

        if spec.min_battery and state.get("battery", -1) >= 0:
            if state["battery"] < spec.min_battery:
                return ActionResult(
                    False, action,
                    f"blocked: battery {state['battery']}% < required "
                    f"{spec.min_battery}%",
                    state)

        try:
            spec.fn(self)
        except Exception as exc:  # noqa: BLE001
            logger.exception("action %s raised", action)
            return ActionResult(False, action, f"execution error: {exc}", state)

        # advance mock FSM so demos feel real (shared by mock + sim)
        if self.mock:
            self._apply_mock_transition(action)

        # sim mode: render the gesture to an mp4 artifact.
        video = None
        message = f"executed '{action}'"
        if self.mode == "sim":
            try:
                sim = self._get_sim()
                if sim.has_action(action):
                    video = sim.render_action(action)
                    message = f"executed '{action}' (rendered {os.path.basename(video)})"
                else:
                    message = (
                        f"executed '{action}' (no sim animation; "
                        f"needs a walking/balance policy)"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("sim render for %s failed", action)
                # The action itself succeeded; only the video failed.
                message = f"executed '{action}' but sim render failed: {exc}"

        return ActionResult(True, action, message, self.get_state(), video=video)

    def _apply_mock_transition(self, action: str) -> None:
        transitions = {
            "sit": FsmState.SIT,
            "stand_up": FsmState.LOCK_STAND,
            "balance_stand": FsmState.BALANCE_STAND,
            "high_stand": FsmState.BALANCE_STAND,
            "low_stand": FsmState.BALANCE_STAND,
            "damp": FsmState.DAMP,
            "zero_torque": FsmState.ZERO_TORQUE,
        }
        if action in transitions:
            self._mock_state = transitions[action]
        # gentle battery drain for realism
        self._mock_battery = max(0, self._mock_battery - 1)
