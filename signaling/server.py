# signaling/server.py
import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Optional, Dict, List

import jwt as pyjwt
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# ── Config ────────────────────────────────────────────────────────────────────
APP_JWT_SECRET   = os.environ.get("APP_JWT_SECRET",   "change-me")
APP_JWT_ISSUER   = os.environ.get("APP_JWT_ISSUER",   "my-thin-bridge")
APP_JWT_AUDIENCE = os.environ.get("APP_JWT_AUDIENCE", "my-attached-app")

DEFAULT_SPEAKER_TIME   = 60    # seconds
MOTION_PENDING_TIMEOUT = 30    # seconds
SECONDED_TIMEOUT       = 300   # 5 minutes
VOTE_CLOSED_DISPLAY    = 5     # seconds

# ── Dataclasses ───────────────────────────────────────────────────────────────
@dataclass
class Member:
    id: str
    name: str
    is_chair: bool = False
    hand_raised: bool = False

@dataclass
class Motion:
    text: str
    moved_by: str
    seconded_by: Optional[str] = None
    votes: Dict = field(default_factory=lambda: {"yea": 0, "nay": 0, "abstain": 0})
    member_votes: Dict = field(default_factory=dict)
    result: Optional[str] = None

@dataclass
class Room:
    room_id: str
    phase: str = "open"
    members: List = field(default_factory=list)
    speaker_queue: List = field(default_factory=list)
    current_speaker: Optional[str] = None
    timer_remaining: int = DEFAULT_SPEAKER_TIME
    speaker_time: int = DEFAULT_SPEAKER_TIME
    motion: Optional[Motion] = None
    # Saved state for restoring after motion_pending
    _prev_phase: Optional[str]   = field(default=None, repr=False)
    _prev_speaker: Optional[str] = field(default=None, repr=False)
    _prev_timer: int              = field(default=0,    repr=False)

# ── Global state (asyncio single-threaded — no locks needed) ─────────────────
rooms:         Dict[str, Room]                   = {}
connections:   Dict[str, Dict[str, WebSocket]]   = {}
_timer_tasks:  Dict[str, asyncio.Task]           = {}
_motion_tasks: Dict[str, asyncio.Task]           = {}

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ── Auth ──────────────────────────────────────────────────────────────────────
def _validate_jwt(token: str) -> dict:
    return pyjwt.decode(
        token, APP_JWT_SECRET,
        algorithms=["HS256"],
        issuer=APP_JWT_ISSUER,
        audience=APP_JWT_AUDIENCE,
    )

# ── Helpers ───────────────────────────────────────────────────────────────────
def _room_to_dict(room: Room) -> dict:
    return {
        "room_id": room.room_id,
        "phase": room.phase,
        "members": [
            {"id": m.id, "name": m.name,
             "is_chair": m.is_chair, "hand_raised": m.hand_raised}
            for m in room.members
        ],
        "speaker_queue": list(room.speaker_queue),
        "current_speaker": room.current_speaker,
        "timer_remaining": room.timer_remaining,
        "speaker_time": room.speaker_time,
        "motion": {
            "text": room.motion.text,
            "moved_by": room.motion.moved_by,
            "seconded_by": room.motion.seconded_by,
            "votes": dict(room.motion.votes),
            "member_votes": dict(room.motion.member_votes),
            "result": room.motion.result,
        } if room.motion else None,
    }

def _get_member(room: Room, member_id: str) -> Optional[Member]:
    return next((m for m in room.members if m.id == member_id), None)

def _is_chair(room: Room, member_id: str) -> bool:
    m = _get_member(room, member_id)
    return m is not None and m.is_chair

def _cancel_task(task_dict: dict, key: str) -> None:
    task = task_dict.pop(key, None)
    if task and not task.done():
        task.cancel()

async def _broadcast(room_id: str, msg: dict) -> None:
    text = json.dumps(msg)
    for ws in list(connections.get(room_id, {}).values()):
        try:
            await ws.send_text(text)
        except Exception:
            pass

async def _broadcast_state(room_id: str) -> None:
    room = rooms.get(room_id)
    if room:
        await _broadcast(room_id, {"type": "state", "state": _room_to_dict(room)})

async def _send_error(ws: WebSocket, message: str) -> None:
    try:
        await ws.send_text(json.dumps({"type": "error", "message": message}))
    except Exception:
        pass

# ── Timer & background tasks ──────────────────────────────────────────────────
async def _run_speaker_timer(room_id: str) -> None:
    """Ticks speaker timer every second; auto-advances at 0."""
    try:
        while True:
            await asyncio.sleep(1)
            room = rooms.get(room_id)
            if not room or room.current_speaker is None:
                return
            room.timer_remaining = max(0, room.timer_remaining - 1)
            await _broadcast_state(room_id)
            if room.timer_remaining == 0:
                await _advance_speaker(room_id)
                return
    except asyncio.CancelledError:
        pass

async def _advance_speaker(room_id: str) -> None:
    """Grant floor to next speaker. Guards against double-call via current_speaker check."""
    room = rooms.get(room_id)
    if not room or room.current_speaker is None:
        return  # already advanced — guard

    _cancel_task(_timer_tasks, room_id)

    prev = _get_member(room, room.current_speaker)
    if prev:
        prev.hand_raised = False
    room.current_speaker = None

    if room.speaker_queue:
        next_id = room.speaker_queue.pop(0)
        room.current_speaker = next_id
        room.timer_remaining = room.speaker_time
        room.phase = "floor_held"
        _timer_tasks[room_id] = asyncio.create_task(_run_speaker_timer(room_id))
    else:
        room.phase = "open"

    await _broadcast_state(room_id)

async def _motion_pending_timeout(room_id: str) -> None:
    try:
        await asyncio.sleep(MOTION_PENDING_TIMEOUT)
        room = rooms.get(room_id)
        if room and room.phase == "motion_pending":
            await _restore_prev_phase(room_id)
    except asyncio.CancelledError:
        pass

async def _seconded_timeout(room_id: str) -> None:
    try:
        await asyncio.sleep(SECONDED_TIMEOUT)
        room = rooms.get(room_id)
        if room and room.phase == "seconded":
            room.phase = "open"
            room.motion = None
            # Clear saved prev-state to prevent stale data
            room._prev_phase = room._prev_speaker = None
            room._prev_timer = 0
            await _broadcast_state(room_id)
    except asyncio.CancelledError:
        pass

async def _restore_prev_phase(room_id: str) -> None:
    room = rooms.get(room_id)
    if not room:
        return
    _cancel_task(_motion_tasks, room_id)
    room.motion   = None
    room.phase    = room._prev_phase or "open"
    room.current_speaker = room._prev_speaker
    room.timer_remaining = room._prev_timer
    room._prev_phase = room._prev_speaker = None
    room._prev_timer = 0
    if room.current_speaker and room.phase == "floor_held":
        _timer_tasks[room_id] = asyncio.create_task(_run_speaker_timer(room_id))
    await _broadcast_state(room_id)

async def _close_vote(room_id: str) -> None:
    room = rooms.get(room_id)
    if not room or not room.motion:
        return
    yea = room.motion.votes["yea"]
    nay = room.motion.votes["nay"]
    room.motion.result = "passed" if yea > nay else "failed"
    room.phase = "vote_closed"
    await _broadcast_state(room_id)
    await asyncio.sleep(VOTE_CLOSED_DISPLAY)
    room = rooms.get(room_id)
    if room:
        room.phase  = "open"
        room.motion = None
        await _broadcast_state(room_id)

# ── Leave ─────────────────────────────────────────────────────────────────────
async def _handle_leave(room_id: str, member_id: str) -> None:
    room = rooms.get(room_id)
    if not room:
        return
    connections.get(room_id, {}).pop(member_id, None)
    was_chair   = _is_chair(room, member_id)
    was_speaker = room.current_speaker == member_id
    room.members       = [m for m in room.members       if m.id != member_id]
    room.speaker_queue = [x for x in room.speaker_queue if x != member_id]

    if not room.members:
        _cancel_task(_timer_tasks,  room_id)
        _cancel_task(_motion_tasks, room_id)
        rooms.pop(room_id, None)
        connections.pop(room_id, None)
        return

    if was_chair:
        room.members[0].is_chair = True

    if was_speaker:
        room.current_speaker = None  # clear before advance to prevent double-call
        _cancel_task(_timer_tasks, room_id)
        if room.speaker_queue:
            nxt = room.speaker_queue.pop(0)
            room.current_speaker  = nxt
            room.timer_remaining  = room.speaker_time
            room.phase            = "floor_held"
            _timer_tasks[room_id] = asyncio.create_task(_run_speaker_timer(room_id))
        elif room.phase == "floor_held":
            room.phase = "open"

    if room.phase == "voting" and was_chair:
        await _broadcast_state(room_id)
        asyncio.create_task(_close_vote(room_id))
        return

    await _broadcast_state(room_id)

# ── Message handler ───────────────────────────────────────────────────────────
async def _handle_message(room_id: str, member_id: str,
                           ws: WebSocket, msg: dict) -> None:
    room = rooms.get(room_id)
    if not room:
        return
    mtype = msg.get("type")

    # WebRTC relay
    if mtype in ("offer", "answer", "ice"):
        to = msg.get("to")
        target = connections.get(room_id, {}).get(to)
        if target:
            await target.send_text(json.dumps({
                "type": "signal", "from": member_id, "signal_type": mtype,
                **{k: v for k, v in msg.items() if k not in ("type", "to")},
            }))
        return

    if mtype == "raise_hand":
        if room.phase not in ("open", "floor_held", "seconded"):
            return await _send_error(ws, "cannot raise hand in current phase")
        m = _get_member(room, member_id)
        if m and not m.hand_raised and member_id not in room.speaker_queue:
            m.hand_raised = True
            room.speaker_queue.append(member_id)
            if room.phase == "open" and room.current_speaker is None:
                nxt = room.speaker_queue.pop(0)
                room.current_speaker  = nxt
                room.timer_remaining  = room.speaker_time
                room.phase            = "floor_held"
                _timer_tasks[room_id] = asyncio.create_task(_run_speaker_timer(room_id))
            await _broadcast_state(room_id)
        return

    if mtype == "lower_hand":
        if room.phase not in ("open", "floor_held", "seconded"):
            return await _send_error(ws, "cannot lower hand in current phase")
        m = _get_member(room, member_id)
        if m:
            m.hand_raised = False
            room.speaker_queue = [x for x in room.speaker_queue if x != member_id]
            await _broadcast_state(room_id)
        return

    if mtype == "yield_floor":
        if room.phase != "floor_held" or room.current_speaker != member_id:
            return await _send_error(ws, "not your floor to yield")
        await _advance_speaker(room_id)
        return

    if mtype == "make_motion":
        if room.phase not in ("open", "floor_held", "seconded"):
            return await _send_error(ws, "cannot make motion in current phase")
        # Block making a new motion while a motion is already seconded and under debate
        if room.phase == "seconded":
            return await _send_error(ws, "a motion is already under debate; withdraw it first")
        text = (msg.get("text") or "").strip()
        if not text:
            return await _send_error(ws, "motion text required")
        _cancel_task(_timer_tasks, room_id)
        # Also cancel any pending motion timeout (e.g., lingering _motion_pending_timeout)
        _cancel_task(_motion_tasks, room_id)
        room._prev_phase   = room.phase
        room._prev_speaker = room.current_speaker
        room._prev_timer   = room.timer_remaining
        room.phase  = "motion_pending"
        room.motion = Motion(text=text, moved_by=member_id)
        _motion_tasks[room_id] = asyncio.create_task(_motion_pending_timeout(room_id))
        await _broadcast_state(room_id)
        return

    if mtype == "second_motion":
        if room.phase != "motion_pending":
            return await _send_error(ws, "no motion pending")
        if not room.motion or room.motion.moved_by == member_id:
            return await _send_error(ws, "mover cannot second their own motion")
        _cancel_task(_motion_tasks, room_id)
        room.motion.seconded_by = member_id
        room.phase = "seconded"
        _motion_tasks[room_id] = asyncio.create_task(_seconded_timeout(room_id))
        await _broadcast_state(room_id)
        return

    if mtype == "withdraw_motion":
        if room.phase not in ("motion_pending", "seconded"):
            return await _send_error(ws, "no active motion to withdraw")
        if not room.motion or room.motion.moved_by != member_id:
            return await _send_error(ws, "only the mover can withdraw")
        await _restore_prev_phase(room_id)
        return

    if mtype == "call_vote":
        if not _is_chair(room, member_id):
            return await _send_error(ws, "only the chair can call a vote")
        if room.phase != "seconded":
            return await _send_error(ws, "can only call vote in seconded phase")
        _cancel_task(_motion_tasks, room_id)
        room.phase = "voting"
        await _broadcast_state(room_id)
        return

    if mtype == "cast_vote":
        if room.phase != "voting" or not room.motion:
            return await _send_error(ws, "not in voting phase")
        if member_id in room.motion.member_votes:
            return await _send_error(ws, "already voted")
        vote = msg.get("vote")
        if vote not in ("yea", "nay", "abstain"):
            return await _send_error(ws, "vote must be yea, nay, or abstain")
        room.motion.member_votes[member_id] = vote
        room.motion.votes[vote] += 1
        if len(room.motion.member_votes) >= len(room.members):
            asyncio.create_task(_close_vote(room_id))
        else:
            await _broadcast_state(room_id)
        return

    if mtype == "set_speaker_time":
        if not _is_chair(room, member_id):
            return await _send_error(ws, "only the chair can set speaker time")
        if room.phase not in ("open", "floor_held"):
            return await _send_error(ws, "can only set speaker time when no business is pending")
        if room.current_speaker is not None:
            return await _send_error(ws, "cannot change speaker time while someone has the floor")
        secs = msg.get("seconds")
        if not isinstance(secs, int) or not (10 <= secs <= 600):
            return await _send_error(ws, "speaker_time must be 10–600 seconds")
        room.speaker_time = secs
        await _broadcast_state(room_id)
        return

    if mtype == "leave":
        await _handle_leave(room_id, member_id)
        await ws.close()
        return

# ── WebSocket endpoint ────────────────────────────────────────────────────────
@app.websocket("/ws/{room_id}")
async def ws_endpoint(websocket: WebSocket, room_id: str) -> None:
    await websocket.accept()
    member_id: Optional[str] = None
    try:
        raw = await websocket.receive_text()
        msg = json.loads(raw)
        if msg.get("type") != "join":
            await _send_error(websocket, "first message must be join")
            return await websocket.close()
        try:
            claims = _validate_jwt(msg["token"])
        except Exception:
            await _send_error(websocket, "unauthorized")
            return await websocket.close()

        member_id = claims["sub"]
        name      = claims.get("name") or member_id

        if room_id not in rooms:
            rooms[room_id]       = Room(room_id=room_id)
            connections[room_id] = {}

        room     = rooms[room_id]
        is_chair = len(room.members) == 0
        room.members.append(Member(id=member_id, name=name, is_chair=is_chair))
        connections[room_id][member_id] = websocket

        await websocket.send_text(json.dumps({"type": "welcome", "self_id": member_id}))
        await _broadcast_state(room_id)

        async for raw in websocket.iter_text():
            try:
                await _handle_message(room_id, member_id, websocket, json.loads(raw))
            except Exception as e:
                await _send_error(websocket, str(e))

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if member_id:
            await _handle_leave(room_id, member_id)
