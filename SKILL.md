---
name: unitree-g1
version: 0.1.0
description: >-
  Talk to your Unitree humanoid on the timeline. Send natural-language
  commands ("wave hello", "shake hands", "sit down") and the agent translates
  them into safe, whitelisted actions on a real robot via your bridge.
homepage: https://github.com/richard7463/unitree-g1-skill
license: MIT
metadata:
  category: robotics
  hardware: Unitree humanoid
  transport: HTTP bridge (LAN + tunnel)
# The agent needs exactly two things from the user, mirroring the Tesla skill's
# "paste your key" flow:
inputs:
  - name: BRIDGE_URL
    description: Public URL of your Unitree bridge (from cloudflared/ngrok), e.g. https://xxx.trycloudflare.com
    required: true
  - name: BRIDGE_TOKEN
    description: The bearer token you set on the bridge (BRIDGE_TOKEN env var).
    required: true
    secret: true
---

# Unitree Humanoid Skill

You control a **real Unitree humanoid robot** through a small bridge server
the user runs on the robot's local network. You do **not** talk to the robot
directly — you only ever call the bridge's HTTP API. The bridge owns all
safety: it will reject unsafe actions, so trust its responses.

## Setup (tell the user, once)

To use this skill the user must provide:

1. `BRIDGE_URL` — the public URL from their tunnel (cloudflared/ngrok).
2. `BRIDGE_TOKEN` — the secret bearer token configured on the bridge.

Every request includes the header: `Authorization: Bearer <BRIDGE_TOKEN>`.

## How to handle a user request

1. **Map** the user's natural language to exactly one action name from the
   whitelist below. If nothing matches, tell the user what the robot *can* do.
2. **Check state first** for any motion/pose change: call `GET /state`. If the
   robot is not `ready` (fsm != `balance_stand`) and the user asked for
   movement, first issue `balance_stand`, then the action.
3. **Send** `POST /command` with `{"action": "<name>"}`.
4. **Report** results in the same crisp style as the Tesla skill, e.g.:
   > done on your Unitree: waved hello 👋 — robot was balance-standing, battery 87%.
5. If the bridge returns HTTP **422**, it *refused* the action for safety.
   Read `message`, explain plainly, and suggest the fix (e.g. "battery too low",
   "robot is sitting — say 'stand up' first"). Never retry a refused action
   without addressing the reason.
6. If the action is marked **dangerous**, ask the user to confirm, then resend
   with `{"action": "<name>", "confirm": true}`.

## Endpoints

### `GET /state`
Returns `{ fsm, battery, ready, mock }`. Call before motion.

### `GET /actions`
Returns the live whitelist with per-action requirements. Source of truth if in
doubt.

### `POST /command`
Body: `{ "action": string, "confirm"?: boolean }`
Success (200): `{ ok: true, action, message, state }`
Refused (422): `{ ok: false, action, message, state }`

## Action whitelist

| Say something like… | action | Notes |
|---|---|---|
| "wave", "say hi", "wave hello" | `wave` | needs standing |
| "shake hands", "give me a handshake" | `shake_hand` | needs balance stand |
| "nod", "say yes", "nod your head" | `nod` | needs standing; gesture-only |
| "stand up", "get up" | `stand_up` | from sit/damp |
| "get ready", "balance", "steady" | `balance_stand` | required before walking |
| "sit down", "take a seat" | `sit` | |
| "stand tall", "rise up" | `high_stand` | |
| "crouch", "get low" | `low_stand` | |
| "walk forward", "come here", "step forward" | `walk_forward` | needs ready + battery ≥30% |
| "turn around", "spin" | `turn` | needs ready + battery ≥30% |
| "relax", "soften", "damp" | `damp` | safe soft state |
| "go limp", "release" | `zero_torque` | **dangerous** — must be supported; confirm required |

## Example interactions

**User:** wave hello to everyone
→ `GET /state` → ready
→ `POST /command {"action":"wave"}`
→ "done on your Unitree: waved hello 👋 (battery 86%)."

**User:** come here
→ `GET /state` → fsm `sit`, not ready
→ `POST /command {"action":"stand_up"}` → then `{"action":"balance_stand"}`
→ `POST /command {"action":"walk_forward"}`
→ "stood up, balanced, and walked forward. battery 84%."

**User:** go limp
→ dangerous → "That makes the robot go completely limp — make sure it's
supported. Confirm?" → on yes → `POST /command {"action":"zero_torque","confirm":true}`

## Safety contract

- Never invent actions outside the whitelist.
- Never bypass a 422 refusal.
- Always surface battery / state to the user when reporting.
- For anything involving walking or falling risk, prefer confirming with the
  user if context is ambiguous.
