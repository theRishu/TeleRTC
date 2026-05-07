import os
import base64
import secrets
import asyncio
import hmac
import hashlib
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ══════════════════════════════════════════════════════
#  Template renderer  (no Jinja2)
# ══════════════════════════════════════════════════════
_TMPL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

def _render(filename: str, **ctx) -> str:
    path = os.path.join(_TMPL_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    for k, v in ctx.items():
        html = html.replace("{{ " + k + " }}", str(v))
        html = html.replace("{{" + k + "}}", str(v))
    return html

# ══════════════════════════════════════════════════════
#  Static HTML snippets
# ══════════════════════════════════════════════════════
def _expired_html() -> str:
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(145deg,#13111c,#1e1532,#0d1a2e);
  font-family:'Inter',system-ui,sans-serif;color:#fff;flex-direction:column;gap:12px}
.ic{font-size:56px}.t{font-size:22px;font-weight:700}.s{color:rgba(255,255,255,.4);font-size:14px}
</style></head><body>
<div class="ic">🔗</div>
<div class="t">Link Expired</div>
<div class="s">This session is no longer active</div>
</body></html>"""

def _unauthorized_html() -> str:
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(145deg,#13111c,#1e1532,#0d1a2e);
  font-family:'Inter',system-ui,sans-serif;color:#fff;flex-direction:column;gap:12px}
.ic{font-size:56px}.t{font-size:22px;font-weight:700}.s{color:rgba(255,255,255,.4);font-size:14px}
</style></head><body>
<div class="ic">🔒</div>
<div class="t">Unauthorized</div>
<div class="s">Invalid credentials</div>
</body></html>"""

# ══════════════════════════════════════════════════════
#  Config
# ══════════════════════════════════════════════════════
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "rishu")
TURN_HOST      = os.getenv("TURN_HOST", "")       # VPS IP, e.g. "66.23.199.133"
TURN_SECRET    = os.getenv("TURN_SECRET", "")     # must match coturn static-auth-secret

def _turn_credentials() -> dict | None:
    """Return time-limited HMAC ICE server config, or None if TURN not configured."""
    if not TURN_HOST or not TURN_SECRET:
        return None
    expiry   = str(int(time.time()) + 86400)          # expires in 24 h
    username = f"{expiry}:telertc"
    password = base64.b64encode(
        hmac.new(TURN_SECRET.encode(), username.encode(), hashlib.sha1).digest()
    ).decode()
    return {
        "iceServers": [
            {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]},
            {
                "urls": [
                    f"turn:{TURN_HOST}:3478",
                    f"turn:{TURN_HOST}:3478?transport=tcp",
                ],
                "username": username,
                "credential": password,
            },
        ],
        "iceCandidatePoolSize": 10,
        "bundlePolicy": "max-bundle",
        "sdpSemantics": "unified-plan",
    }

# ══════════════════════════════════════════════════════
#  Data models
# ══════════════════════════════════════════════════════
class CreateCallRequest(BaseModel):
    caller_id: str
    callee_id: str


class RoomInfo:
    def __init__(self, room_id: str, caller_id: str, callee_id: str,
                 caller_token: str, callee_token: str):
        self.room_id       = room_id
        self.caller_id     = caller_id
        self.callee_id     = callee_id
        self.caller_token  = caller_token
        self.callee_token  = callee_token
        self.created_at    = datetime.now()
        self.status        = "waiting"   # waiting | active | ended

    def duration_seconds(self) -> int:
        return int((datetime.now() - self.created_at).total_seconds())

    def to_dict(self) -> dict:
        return {
            "room_id":    self.room_id,
            "caller_id":  self.caller_id,
            "callee_id":  self.callee_id,
            "status":     self.status,
            "duration":   self.duration_seconds(),
            "created_at": self.created_at.isoformat(),
        }


# ══════════════════════════════════════════════════════
#  In-memory state
# ══════════════════════════════════════════════════════
rooms: Dict[str, RoomInfo] = {}           # room_id  -> RoomInfo
tokens: Dict[str, dict]    = {}           # token    -> {room_id, role}
ws_connections: Dict[str, Dict[str, WebSocket]] = {}  # room_id -> {peer_id: ws}

# ══════════════════════════════════════════════════════
#  Background housekeeping
# ══════════════════════════════════════════════════════
WAITING_TIMEOUT  = 15 * 60   # 15 min — kill rooms where callee never joined
ENDED_TTL        = 5  * 60   # 5 min  — sweep fully-ended rooms

async def _housekeeping():
    """Periodically remove stale rooms so memory doesn't grow unbounded."""
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        stale = []
        for room_id, room in list(rooms.items()):
            age = (now - room.created_at).total_seconds()
            if room.status == "ended":
                if age > ENDED_TTL:
                    stale.append(room_id)
            elif room.status == "waiting" and age > WAITING_TIMEOUT:
                stale.append(room_id)
        for room_id in stale:
            asyncio.create_task(_cleanup_room(room_id, delay=0))


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_housekeeping())
    yield
    task.cancel()


# ══════════════════════════════════════════════════════
#  App
# ══════════════════════════════════════════════════════
app = FastAPI(title="TeleRTC", lifespan=lifespan)


# ─────────────────────────────────────────────────────
#  Health check
# ─────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse("it working")


# ─────────────────────────────────────────────────────
#  ICE server credentials  (called by call.html on load)
# ─────────────────────────────────────────────────────
@app.get("/api/ice_servers")
async def ice_servers(token: str = Query("")):
    if token not in tokens:
        raise HTTPException(status_code=401, detail="Invalid token")
    creds = _turn_credentials()
    if creds:
        return creds
    # TURN not configured — return Google STUN only
    return {
        "iceServers": [
            {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]},
        ],
        "iceCandidatePoolSize": 10,
        "bundlePolicy": "max-bundle",
        "sdpSemantics": "unified-plan",
    }


# ─────────────────────────────────────────────────────
#  Create call  (called by Telegram bot)
# ─────────────────────────────────────────────────────
@app.post("/api/create_call")
async def create_call(payload: CreateCallRequest):
    room_id       = secrets.token_urlsafe(12)
    caller_token  = secrets.token_urlsafe(24)
    callee_token  = secrets.token_urlsafe(24)

    room = RoomInfo(room_id, payload.caller_id, payload.callee_id,
                    caller_token, callee_token)
    rooms[room_id]          = room
    tokens[caller_token]    = {"room_id": room_id, "role": "caller"}
    tokens[callee_token]    = {"room_id": room_id, "role": "callee"}

    return {
        "caller_token": caller_token,
        "callee_token": callee_token,
        "room_id":      room_id,
    }


# ─────────────────────────────────────────────────────
#  Join room  (user opens their link)
# ─────────────────────────────────────────────────────
@app.get("/join/{token}", response_class=HTMLResponse)
async def join_room(request: Request, token: str):
    td = tokens.get(token)
    if not td:
        return HTMLResponse(_expired_html(), status_code=404)
    room = rooms.get(td["room_id"])
    if not room or room.status == "ended":
        return HTMLResponse(_expired_html(), status_code=404)
    return HTMLResponse(_render("call.html",
        room_id   = room.room_id,
        role      = td["role"],
        token     = token,
        caller_id = room.caller_id,
        callee_id = room.callee_id,
    ))


# ─────────────────────────────────────────────────────
#  Admin panel  (HTTP Basic Auth — browser shows native prompt)
# ─────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    auth = request.headers.get("Authorization", "")
    ok = False
    if auth.startswith("Basic "):
        try:
            _, pwd = base64.b64decode(auth[6:]).decode().split(":", 1)
            ok = (pwd == ADMIN_PASSWORD)
        except Exception:
            pass
    if not ok:
        return HTMLResponse(
            content="Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="TeleRTC Admin"'},
        )
    return HTMLResponse(_render("admin.html", password=ADMIN_PASSWORD))


# ─────────────────────────────────────────────────────
#  Admin API — list all rooms
# ─────────────────────────────────────────────────────
@app.get("/api/admin/rooms")
async def get_all_rooms(password: str = Query("")):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    active_rooms = []
    for r in rooms.values():
        if r.status == "ended":
            continue
        # Count non-spy active websocket connections
        active_peers = [p for p in ws_connections.get(r.room_id, {}) if not p.startswith("spy_")]
        if len(active_peers) >= 2:
            active_rooms.append(r.to_dict())
            
    return active_rooms


# ─────────────────────────────────────────────────────
#  Admin API — create spy token for a room
# ─────────────────────────────────────────────────────
@app.post("/api/admin/spy/{room_id}")
async def create_spy_token(room_id: str, password: str = Query("")):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    spy_token = secrets.token_urlsafe(24)
    tokens[spy_token] = {"room_id": room_id, "role": "spy"}
    return {"spy_token": spy_token, "url": f"/spy/{spy_token}"}


# ─────────────────────────────────────────────────────
#  Spy full view  (opens call.html in spy role)
# ─────────────────────────────────────────────────────
@app.get("/spy/{token}", response_class=HTMLResponse)
async def spy_room(request: Request, token: str):
    td = tokens.get(token)
    if not td or td["role"] != "spy":
        return HTMLResponse(_unauthorized_html(), status_code=403)
    room = rooms.get(td["room_id"])
    if not room:
        return HTMLResponse(_expired_html(), status_code=404)
    return HTMLResponse(_render("call.html",
        room_id   = room.room_id,
        role      = "spy",
        token     = token,
        caller_id = room.caller_id,
        callee_id = room.callee_id,
    ))


# ─────────────────────────────────────────────────────
#  WebSocket signaling
# ─────────────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def websocket_signaling(
    ws: WebSocket,
    room_id: str,
    role:    str = Query("caller"),
    token:   str = Query(""),
):
    # Validate token
    td = tokens.get(token)
    if not td or td["room_id"] != room_id:
        await ws.close(code=4001)
        return

    await ws.accept()

    # Unique peer ID (role prefix helps client identify spy peers)
    peer_id = f"{role}_{secrets.token_hex(6)}"

    # Register
    ws_connections.setdefault(room_id, {})[peer_id] = ws

    # Update room status
    is_spy = role == "spy"
    room = rooms.get(room_id)
    if room:
        active = [p for p in ws_connections[room_id] if not p.startswith("spy")]
        if len(active) >= 2:
            room.status = "active"

    # Send current room state to the new peer
    existing = [p for p in ws_connections[room_id] if p != peer_id]
    try:
        await ws.send_json({
            "type":    "room_state",
            "peer_id": peer_id,
            "role":    role,
            "peers":   existing,
        })
    except Exception:
        pass

    # Notify others that someone joined
    if role != "spy":
        await _broadcast(room_id, {
            "type":    "peer_joined",
            "peer_id": peer_id,
            "role":    role,
        }, exclude=peer_id)
    else:
        # If I am a spy, I only announce myself to OTHER spies
        await _broadcast(room_id, {
            "type":    "peer_joined",
            "peer_id": peer_id,
            "role":    role,
        }, exclude=peer_id, to_spies_only=True)

    try:
        while True:
            data = await ws.receive_json()
            data["from"] = peer_id
            target = data.get("to")
            if target:
                # Direct (offer/answer/candidate)
                tws = ws_connections.get(room_id, {}).get(target)
                if tws:
                    try:
                        await tws.send_json(data)
                    except Exception:
                        pass
            else:
                await _broadcast(room_id, data, exclude=peer_id)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] room={room_id} peer={peer_id} err={e}")
    finally:
        ws_connections.get(room_id, {}).pop(peer_id, None)
        if not ws_connections.get(room_id):
            ws_connections.pop(room_id, None)

        if role != "spy":
            await _broadcast(room_id, {
                "type":    "peer_left",
                "peer_id": peer_id,
                "role":    role,
            })
        else:
            # Tell other spies I left
            await _broadcast(room_id, {
                "type":    "peer_left",
                "peer_id": peer_id,
                "role":    role,
            }, to_spies_only=True)

        if room:
            remaining_non_spy = [
                p for p in ws_connections.get(room_id, {})
                if not p.startswith("spy")
            ]
            # Only end the room if it was active and everyone left, 
            # or if it's been waiting and became empty (timeout cleanup handles this anyway)
            if not remaining_non_spy and room.status == "active":
                # Don't end it immediately so they can refresh!
                asyncio.create_task(_cleanup_room(room_id, delay=60))


async def _broadcast(room_id: str, data: dict, exclude: str = None, to_spies_only: bool = False):
    for pid, peer_ws in dict(ws_connections.get(room_id, {})).items():
        if pid == exclude:
            continue
        if to_spies_only and not pid.startswith("spy"):
            continue
        try:
            await peer_ws.send_json(data)
        except Exception:
            pass


async def _cleanup_room(room_id: str, delay: int = 300):
    """Remove room and its tokens from memory after a delay."""
    await asyncio.sleep(delay)
    room = rooms.get(room_id)
    if room:
        room.status = "ended"
        await _broadcast(room_id, {"type": "room_ended"})
        
    # Remove from memory
    room = rooms.pop(room_id, None)
    if room:
        tokens.pop(room.caller_token, None)
        tokens.pop(room.callee_token, None)
        # Remove any lingering spy tokens for this room
        spy_toks = [t for t, d in list(tokens.items()) if d.get("room_id") == room_id]
        for t in spy_toks:
            tokens.pop(t, None)
    ws_connections.pop(room_id, None)


# ══════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8084, log_level="info")
