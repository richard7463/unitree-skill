"""
Unitree G1 bridge server.

This is the piece that runs on a machine on the SAME LAN as the G1, and is
exposed to the public internet through a tunnel (cloudflared / ngrok). The
bankr agent (or any harness running the SKILL.md) calls these endpoints.

Design goals:
  - Tesla-style "paste your key" UX  -> single bearer token.
  - Never trust the agent: server owns the action whitelist + safety gating
    (that lives in g1_controller.py).
  - Boots fine with no robot present (MOCK mode) so you can wire up the whole
    timeline -> bridge path before the hardware is in the room.

Run:
    export BRIDGE_TOKEN=$(openssl rand -hex 24)
    export G1_MOCK=1                 # drop this once the robot is on the LAN
    uvicorn bridge.server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .g1_controller import G1Controller

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("bridge")

BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN")
RATE_WINDOW_SEC = float(os.getenv("RATE_WINDOW_SEC", "10"))
RATE_MAX = int(os.getenv("RATE_MAX", "5"))

app = FastAPI(title="Unitree G1 Bridge", version="0.1.0")

# single shared controller (one robot per bridge)
controller = G1Controller()

# crude in-memory sliding-window rate limit, keyed per token. Motion on a
# humanoid should not be spammed; this protects the robot, not the server.
_hits: deque[float] = deque(maxlen=256)

# Real-time viewer engine, created on first use of a /live endpoint. It runs a
# persistent MuJoCo G1 in a background thread and streams frames to the browser.
# Kept lazy so mock/real deployments never pay the MuJoCo import cost.
_live_engine = None  # type: ignore[var-annotated]
_live_lock = None


def _get_live():
    """Lazily start the real-time engine. Raises 503 if sim deps are missing."""
    global _live_engine, _live_lock
    if _live_lock is None:
        import threading

        _live_lock = threading.Lock()
    with _live_lock:
        if _live_engine is None:
            from .g1_live import G1LiveEngine, SimUnavailable

            try:
                eng = G1LiveEngine()
                eng.start()
                _live_engine = eng
            except SimUnavailable as exc:
                raise HTTPException(503, f"live viewer unavailable: {exc}")
    return _live_engine


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    if not BRIDGE_TOKEN:
        # fail closed: no token configured => nobody gets in
        raise HTTPException(500, "bridge misconfigured: BRIDGE_TOKEN unset")
    expected = f"Bearer {BRIDGE_TOKEN}"
    if authorization != expected:
        raise HTTPException(401, "invalid or missing bearer token")


def rate_gate() -> None:
    now = time.monotonic()
    while _hits and now - _hits[0] > RATE_WINDOW_SEC:
        _hits.popleft()
    if len(_hits) >= RATE_MAX:
        raise HTTPException(429, f"rate limited: max {RATE_MAX}/{RATE_WINDOW_SEC:.0f}s")
    _hits.append(now)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
class CommandRequest(BaseModel):
    action: str = Field(..., description="whitelisted action name")
    confirm: bool = Field(False, description="required for dangerous actions")


class CommandResponse(BaseModel):
    ok: bool
    action: str
    message: str
    state: dict
    video: Optional[str] = None  # sim mode: path to rendered mp4, if any


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "mock": controller.mock,
        "mode": controller.mode,
        "version": app.version,
    }


@app.get("/state", dependencies=[Depends(require_token)])
def state() -> dict:
    return controller.get_state()


@app.get("/actions", dependencies=[Depends(require_token)])
def actions() -> dict:
    return {"actions": controller.list_actions()}


@app.post(
    "/command",
    response_model=CommandResponse,
    dependencies=[Depends(require_token)],
)
def command(req: CommandRequest) -> CommandResponse:
    rate_gate()
    logger.info("command action=%s confirm=%s", req.action, req.confirm)
    result = controller.execute(req.action, confirm=req.confirm)
    if not result.ok:
        # 422 = agent asked for something we refused (blocked/unknown/needs confirm)
        return JSONResponse(  # type: ignore[return-value]
            status_code=422,
            content=CommandResponse(
                ok=False,
                action=result.action,
                message=result.message,
                state=result.state,
            ).model_dump(),
        )
    return CommandResponse(
        ok=True,
        action=result.action,
        message=result.message,
        state=result.state,
        video=result.video,
    )


# ---------------------------------------------------------------------------
# Real-time viewer: type on the left, watch the G1 move on the right.
# This is the "screen-record it yourself" surface. No auth on the page itself
# so you can just open it in a browser; /live/say still routes through the same
# whitelist + gesture library as everything else.
# ---------------------------------------------------------------------------
class SayRequest(BaseModel):
    text: str = Field(..., description="natural language, e.g. 'wave hello'")


@app.get("/live", response_class=HTMLResponse)
def live_page() -> str:
    return _LIVE_HTML


@app.get("/live/stream")
def live_stream() -> StreamingResponse:
    engine = _get_live()

    def gen():
        boundary = b"--frame"
        while True:
            jpeg = engine.latest_jpeg()
            if jpeg is None:
                time.sleep(0.05)
                continue
            yield (
                boundary + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                + jpeg + b"\r\n"
            )
            time.sleep(1.0 / 30.0)

    return StreamingResponse(
        gen(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.post("/live/say")
def live_say(req: SayRequest) -> dict:
    from .g1_live import resolve_action

    engine = _get_live()
    action = resolve_action(req.text)
    if action is None:
        return {
            "ok": False,
            "text": req.text,
            "action": None,
            "message": "no gesture matched; try: " + ", ".join(engine.list_actions()),
            "actions": engine.list_actions(),
        }
    engine.enqueue(action)
    logger.info("live/say text=%r -> action=%s", req.text, action)
    return {"ok": True, "text": req.text, "action": action, "message": f"playing '{action}'"}


@app.get("/live/actions")
def live_actions() -> dict:
    engine = _get_live()
    return {"actions": engine.list_actions()}


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):  # pragma: no cover
    logger.exception("unhandled error")
    return JSONResponse(status_code=500, content={"ok": False, "message": str(exc)})


# ---------------------------------------------------------------------------
# The live page. Single self-contained HTML: left = input + gesture buttons +
# log, right = the live MJPEG stream. Nothing to build, just open /live.
# ---------------------------------------------------------------------------
_LIVE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>G1 Live</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0b0d10; color: #e7ecf2; height: 100vh; display: flex;
  }
  .left {
    width: 380px; min-width: 320px; padding: 22px; display: flex; flex-direction: column;
    gap: 14px; border-right: 1px solid #1b2027; background: #0e1216;
  }
  .right { flex: 1; display: flex; align-items: center; justify-content: center; background: #05070a; }
  .right img { max-width: 100%; max-height: 100%; border-radius: 8px; }
  h1 { font-size: 16px; margin: 0; letter-spacing: .3px; }
  .sub { color: #7d8794; font-size: 12px; margin-top: -8px; }
  form { display: flex; gap: 8px; }
  input[type=text] {
    flex: 1; padding: 11px 13px; border-radius: 9px; border: 1px solid #262d36;
    background: #141a20; color: #e7ecf2; font-size: 14px; outline: none;
  }
  input[type=text]:focus { border-color: #3b82f6; }
  button {
    padding: 11px 15px; border-radius: 9px; border: 1px solid #2a323c;
    background: #1a222c; color: #e7ecf2; font-size: 13px; cursor: pointer;
  }
  button:hover { background: #223040; border-color: #3b82f6; }
  button.send { background: #2563eb; border-color: #2563eb; font-weight: 600; }
  button.send:hover { background: #1d4ed8; }
  .chips { display: flex; flex-wrap: wrap; gap: 7px; }
  .chips button { padding: 7px 11px; font-size: 12px; }
  .log { flex: 1; overflow-y: auto; font-size: 12px; color: #9aa4b0;
         border-top: 1px solid #1b2027; padding-top: 10px; }
  .log .line { padding: 3px 0; }
  .log .ok { color: #86efac; }
  .log .no { color: #fca5a5; }
</style>
</head>
<body>
  <div class="left">
    <h1>Unitree G1 &mdash; live</h1>
    <div class="sub">Type a command; the robot on the right moves. Screen-record this window.</div>
    <form id="f">
      <input id="t" type="text" placeholder="e.g. wave hello / sit down / nod" autocomplete="off" autofocus/>
      <button class="send" type="submit">Send</button>
    </form>
    <div class="chips" id="chips"></div>
    <div class="log" id="log"></div>
  </div>
  <div class="right">
    <img id="cam" src="/live/stream" alt="G1 live view"/>
  </div>
<script>
  const log = document.getElementById('log');
  function line(msg, cls) {
    const d = document.createElement('div');
    d.className = 'line ' + (cls||'');
    d.textContent = msg;
    log.prepend(d);
  }
  async function say(text) {
    if (!text.trim()) return;
    line('> ' + text);
    try {
      const r = await fetch('/live/say', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({text})
      });
      const j = await r.json();
      line((j.ok?'\u2713 ':'\u2717 ') + (j.message||''), j.ok?'ok':'no');
    } catch (e) { line('\u2717 ' + e, 'no'); }
  }
  document.getElementById('f').addEventListener('submit', (e) => {
    e.preventDefault();
    const t = document.getElementById('t');
    say(t.value); t.value=''; t.focus();
  });
  fetch('/live/actions').then(r=>r.json()).then(j => {
    const box = document.getElementById('chips');
    (j.actions||[]).forEach(a => {
      const b = document.createElement('button');
      b.textContent = a; b.onclick = () => say(a);
      box.appendChild(b);
    });
  });
</script>
</body>
</html>"""
