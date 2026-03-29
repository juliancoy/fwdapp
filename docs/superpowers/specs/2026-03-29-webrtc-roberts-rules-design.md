# WebRTC Roberts Rules Meeting Room — Design Spec

**Date:** 2026-03-29

---

## Overview

A real-time meeting room implementing Roberts Rules of Order, integrated into the fwdapp. Members join via app-minted HS256 JWT (from `wix/main.py`), speak in a FIFO queue with an automated 60-second timer, and vote on motions. Speaker progression is fully automated — no chair action needed to advance the queue.

---

## Architecture

```
Cloudflare Pages (static)          julian-latitude (or any Python host)
  webrtc/index.html           ←→    signaling/server.py  (FastAPI WebSocket)
  webrtc/scripts/
    signaling.js   ──WebSocket──→   • WebRTC offer/answer/ICE relay
    meeting.js     ──WebRTC P2P─→   • Roberts Rules state machine
    roberts.js                      • JWT validation on join
  webrtc/styles.css                 • Timer broadcast (1s intervals)
```

**Topology:** Mesh — each peer connects directly to every other peer via WebRTC. Signaling server relays only SDP offer/answer and ICE candidates; actual audio/video never touches the server. Suitable for ≤15 participants.

---

## New Files

| File | Responsibility |
|------|---------------|
| `signaling/server.py` | FastAPI WebSocket server: WebRTC signaling + Roberts Rules state machine + timer |
| `signaling/requirements.txt` | Python dependencies for signaling server |
| `webrtc/index.html` | Meeting room page — requires valid app JWT to enter |
| `webrtc/scripts/signaling.js` | WebSocket connection, reconnect logic, message dispatch |
| `webrtc/scripts/meeting.js` | WebRTC peer connections, media streams, video grid management |
| `webrtc/scripts/roberts.js` | Roberts Rules UI: raise-hand button, speaker queue display, countdown timer, motion form, vote buttons |
| `webrtc/styles.css` | Meeting room layout and UI styles |

---

## Roberts Rules State Machine

Server-enforced. All transitions broadcast a full `state` snapshot to every connected client.

```
idle
 └─ join (first member) ──────────────────────────→ open

open
 ├─ raise_hand + queue non-empty ─────────────────→ floor_held  (auto, no chair action)
 ├─ make_motion ──────────────────────────────────→ motion_pending (saves prev_phase=open)
 └─ all members leave ────────────────────────────→ idle (room destroyed)

floor_held
 ├─ timer hits 0 OR yield_floor:
 │    queue non-empty ────────────────────────────→ floor_held  (next speaker, auto)
 │    queue empty ────────────────────────────────→ open
 ├─ make_motion ──────────────────────────────────→ motion_pending (saves prev_phase=floor_held)
 └─ all members leave ────────────────────────────→ idle

motion_pending  (30s server timeout → restores prev_phase)
 ├─ second_motion ────────────────────────────────→ seconded
 ├─ withdraw_motion (mover only) ─────────────────→ restores prev_phase
 └─ 30s timeout ──────────────────────────────────→ restores prev_phase

seconded  (5-min server timeout → open)
 ├─ raise_hand resumes (debate queue)
 ├─ call_vote (chair only) ───────────────────────→ voting
 ├─ withdraw_motion (mover only) ─────────────────→ open
 └─ 5-min timeout ────────────────────────────────→ open

voting
 ├─ all members voted OR chair closes ────────────→ vote_closed
 └─ chair disconnects ────────────────────────────→ vote_closed (partial result)

vote_closed  (5s display) ───────────────────────→ open
```

**prev_phase restoration:** Server stores `prev_phase` and `prev_speaker` (the `current_speaker` at motion time) on entering `motion_pending`. On restore, `current_speaker` is reset to `prev_speaker` and the timer resumes with its pre-motion `timer_remaining`. If `prev_phase` was `open`, both are null.

---

## WebSocket Message Protocol

All messages are JSON. Server broadcasts full room `state` after every state-changing action.

### Client → Server

| Message | Payload | Who / When |
|---------|---------|-----------|
| `join` | `{ token, room_id }` | anyone; must be first message |
| `offer` | `{ to: member_id, sdp }` | any member, any phase |
| `answer` | `{ to: member_id, sdp }` | any member, any phase |
| `ice` | `{ to: member_id, candidate }` | any member, any phase |
| `raise_hand` | — | any member; `open`, `floor_held`, `seconded` only |
| `lower_hand` | — | any member; `open`, `floor_held`, `seconded` only |
| `yield_floor` | — | `current_speaker` only; `floor_held` only |
| `make_motion` | `{ text }` | any member; `open`, `floor_held`, `seconded` |
| `second_motion` | — | any member except mover; `motion_pending` only |
| `withdraw_motion` | — | mover only; `motion_pending` or `seconded` |
| `call_vote` | — | chair only; `seconded` only |
| `cast_vote` | `{ vote: "yea"\|"nay"\|"abstain" }` | any member; `voting` only; once per member (final) |
| `set_speaker_time` | `{ seconds: int (10–600) }` | chair only; `open` or between speakers |
| `leave` | — | any member; graceful disconnect |

Server rejects messages sent in wrong phase or by wrong role with an `error` response. Ungraceful disconnects (WebSocket close) are treated identically to `leave`.

### Server → Client

| Message | Payload | When |
|---------|---------|------|
| `welcome` | `{ self_id: member_id }` | immediately after successful `join`, before first `state` |
| `state` | full room state (see below) | after every state-changing action |
| `signal` | `{ from: member_id, type: "offer"\|"answer"\|"ice", sdp\|candidate }` | WebRTC relay |
| `error` | `{ message }` | invalid action or auth failure |

`welcome` is sent once per session. The client stores `self_id` locally to determine its own role when rendering the `state` broadcast.

---

## Room State Shape

```json
{
  "room_id": "string",
  "phase": "idle | open | floor_held | motion_pending | seconded | voting | vote_closed",
  "members": [
    {
      "id": "member_id (sub claim from JWT)",
      "name": "Jane Smith",
      "is_chair": true,
      "hand_raised": false
    }
  ],
  "speaker_queue": ["member_id_1", "member_id_2"],
  "current_speaker": "member_id | null",
  "timer_remaining": 47,
  "speaker_time": 60,
  "motion": {
    "text": "I move to table the amendment",
    "moved_by": "member_id",
    "seconded_by": "member_id | null",
    "votes": { "yea": 3, "nay": 1, "abstain": 0 },
    "member_votes": { "member_id": "yea" },
    "result": "passed | failed | null"
  }
}
```

`motion` is `null` when no motion is active. `phase: "idle"` rooms are never broadcast — the room only exists in memory from first `join` to last `leave`.

**Vote result rule:** Simple majority of votes cast. Ties → "failed". No quorum enforcement (out of scope).

---

## Auth

- `webrtc/index.html` reads the app-minted HS256 token from `localStorage` at key `wix_member_token` (set by `scripts/auth.js`)
- This is the token minted by `wix/main.py` `mint_app_token()`, **not** the raw Wix OAuth token
- On WebSocket `join`, server validates the token with PyJWT:
  - Algorithm: `HS256`
  - Secret: `APP_JWT_SECRET` env var (same secret used in `wix/main.py`)
  - Claims checked: `exp` (must not be expired), `iss` (`"my-thin-bridge"`), `aud` (`"my-attached-app"`)
  - `sub` claim → used as `member_id` throughout the room session
  - `name` claim (if present) or `sub` → display name
- Invalid/expired token → server sends `error { message: "unauthorized" }` and closes connection

**Chair assignment:** First `join` to create a new room sets `is_chair: true` atomically (Python asyncio is single-threaded; `rooms` dict is checked-and-set in a single coroutine before any `await`, preventing the race). Subsequent joins to an existing room never receive `is_chair`.

**Chair disconnect:** If the chair leaves, `is_chair` is promoted to the member who joined earliest (lowest index in `members` list). Meeting continues uninterrupted.

---

## Timer Behaviour

- Default: **60 seconds**
- Chair can set `speaker_time` (10–600s) only in `open` or between speakers; change takes effect for the next speaker (does not alter the current speaker's remaining time)
- Server runs one `asyncio` background task per active speaker that decrements `timer_remaining` every second and broadcasts `state`
- At `timer_remaining === 0`: task calls `_advance_speaker()` which checks `current_speaker is not None` before acting (guards against the `yield_floor` / timer-tick race — both paths check-and-clear `current_speaker` atomically in the asyncio event loop before awaiting)
- `yield_floor` cancels the timer task and calls `_advance_speaker()` directly

---

## WebRTC Peer Identity

- **Signaling identity = `member_id` (`sub` claim)**
- `offer`/`answer`/`ice` messages use `to: member_id`; server looks up the target's WebSocket connection in `connections: dict[member_id, WebSocket]` (populated on successful `join`)
- `meeting.js` keys its `RTCPeerConnection` map by `member_id`
- `roberts.js` can therefore look up "the video element for `current_speaker`" via `member_id`

---

## Frontend Layout (webrtc/index.html)

```
┌─────────────────────────────────────────────────────┐
│  header: room name · phase badge · member count     │
├──────────────────────────┬──────────────────────────┤
│                          │  Speaker Queue           │
│   Video Grid             │  ┌─ [Jane] ──────────── │
│   (current speaker       │  │  Speaking: 0:47 left  │
│    large, others small)  │  └────────────────────── │
│                          │  1. Bob                  │
│                          │  2. Carol                │
│                          │                          │
│                          │  [✋ Raise Hand]          │
├──────────────────────────┴──────────────────────────┤
│  Motion: "Table amendment"  [Second]  [Withdraw]    │
│  [Call Vote - chair]                                │
│  Voting: [Yea] [Nay] [Abstain]   Yea:3 Nay:1 Abs:0 │
├─────────────────────────────────────────────────────┤
│  [Make Motion: ________________] [Submit]           │
└─────────────────────────────────────────────────────┘
```

Controls shown/hidden by phase × role (using `self_id` from `welcome` message):
- `raise_hand`: visible in `open`, `floor_held`, `seconded`; hidden in `voting`
- `make_motion` form: visible in `open`, `floor_held`, `seconded`
- `second_motion`: visible in `motion_pending` for non-mover
- `withdraw_motion`: visible in `motion_pending`/`seconded` for mover only
- `call_vote`: visible in `seconded` for chair only
- vote buttons: visible in `voting` only; disabled after casting (votes are final)

---

## Deployment

Signaling server runs separately from Cloudflare Pages.

```bash
# Local dev
cd signaling && pip install -r requirements.txt
uvicorn server:app --port 8765 --reload
```

Frontend connects to `WS_URL` imported from `webrtc/scripts/config.js`:
```js
export const WS_URL = 'ws://localhost:8765';  // override for production
```

---

## Success Criteria

- Member can join room with app JWT and see/hear other members via WebRTC
- Raising hand adds to queue; queue auto-advances when 60s expires or speaker yields
- Motion workflow: make → second → vote → result displayed to all
- Chair promotes automatically on chair disconnect
- All state changes appear on all clients within ~100ms
- Invalid/expired JWT rejected at join; connection closed
