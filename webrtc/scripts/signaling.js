// webrtc/scripts/signaling.js
// Manages the WebSocket connection to the signaling server.
// Dispatches CustomEvents: 'welcome', 'state', 'signal', 'error'
import { WS_URL } from './config.js';

export class SignalingClient extends EventTarget {
    #ws = null;
    #token = null;
    #roomId = null;
    #guestOpts = null;
    #reconnectDelay = 2000;

    connect(token, roomId, guestOpts = {}) {
        this.#token     = token || '__guest__';  // truthy so reconnect works for guests
        this.#roomId    = roomId;
        this.#guestOpts = guestOpts;
        this.#open();
    }

    send(msg) {
        if (this.#ws?.readyState === WebSocket.OPEN) {
            this.#ws.send(JSON.stringify(msg));
        }
    }

    close() {
        this.#token = null;  // prevent reconnect loop before closing
        this.#ws?.close();
        this.#ws = null;
    }

    #open() {
        this.#ws = new WebSocket(`${WS_URL}/ws/${this.#roomId}`);

        this.#ws.onopen = () => {
            const joinMsg = this.#guestOpts?.guest
                ? { type: 'join', guest: true, name: this.#guestOpts.name, room_id: this.#roomId }
                : { type: 'join', token: this.#token, room_id: this.#roomId };
            this.#ws.send(JSON.stringify(joinMsg));
        };

        this.#ws.onmessage = ({ data }) => {
            const msg = JSON.parse(data);
            this.dispatchEvent(new CustomEvent(msg.type, { detail: msg }));
        };

        this.#ws.onclose = () => {
            // Reconnect unless explicitly closed
            if (this.#token) {
                setTimeout(() => this.#open(), this.#reconnectDelay);
            }
        };

        this.#ws.onerror = () => {
            this.dispatchEvent(new CustomEvent('error', {
                detail: { message: 'WebSocket error' },
            }));
        };
    }
}
