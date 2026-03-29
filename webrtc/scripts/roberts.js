// webrtc/scripts/roberts.js
// Renders Roberts Rules UI from server state snapshots.
// All display logic lives here; no state is derived outside this module.

export class RobertsUI {
    #signaling;
    #selfId;
    #state = null;

    constructor(signaling, selfId) {
        this.#signaling = signaling;
        this.#selfId    = selfId;
    }

    // Call once after DOM is ready.
    bindButtons() {
        this.#on('btn-raise-hand', 'click', () => {
            const inQueue = this.#state?.speaker_queue.includes(this.#selfId);
            this.#signaling.send({ type: inQueue ? 'lower_hand' : 'raise_hand' });
        });
        // Yield Floor: only shown to the current speaker during floor_held
        this.#on('btn-yield-floor', 'click', () =>
            this.#signaling.send({ type: 'yield_floor' }));
        this.#on('btn-second',    'click', () => this.#signaling.send({ type: 'second_motion' }));
        this.#on('btn-withdraw',  'click', () => this.#signaling.send({ type: 'withdraw_motion' }));
        this.#on('btn-call-vote', 'click', () => this.#signaling.send({ type: 'call_vote' }));
        ['yea', 'nay', 'abstain'].forEach(v =>
            this.#on(`btn-${v}`, 'click', () =>
                this.#signaling.send({ type: 'cast_vote', vote: v })));
        document.getElementById('motion-form')?.addEventListener('submit', e => {
            e.preventDefault();
            const inp = document.getElementById('motion-input');
            const txt = inp?.value.trim();
            if (txt) {
                this.#signaling.send({ type: 'make_motion', text: txt });
                inp.value = '';
            }
        });
    }

    // Apply a full state snapshot from the server.
    applyState(state) {
        this.#state = state;
        const me      = state.members.find(m => m.id === this.#selfId);
        const isChair = me?.is_chair ?? false;
        const inQueue = state.speaker_queue.includes(this.#selfId);
        const phase   = state.phase;
        const motion  = state.motion;

        // Header
        this.#text('phase-badge',    phase.replace(/_/g, ' '));
        this.#text('member-count',   `${state.members.length} member${state.members.length !== 1 ? 's' : ''}`);

        // Speaker + timer
        const speaker = state.members.find(m => m.id === state.current_speaker);
        this.#text('current-speaker', speaker ? `${speaker.name} has the floor` : '');
        this.#text('timer-display',   state.current_speaker ? this.#fmt(state.timer_remaining) : '');

        // Queue
        const queueEl = document.getElementById('speaker-queue');
        if (queueEl) {
            queueEl.innerHTML = state.speaker_queue.map((id, i) => {
                const m = state.members.find(m => m.id === id);
                return `<li>${i + 1}. ${m?.name ?? id}</li>`;
            }).join('');
        }

        // Raise/lower hand button
        const canHand = ['open', 'floor_held', 'seconded'].includes(phase);
        this.#show('btn-raise-hand',  canHand);
        this.#text('btn-raise-hand',  inQueue ? '✋ Lower Hand' : '✋ Raise Hand');

        // Yield floor button — only shown to the current speaker in floor_held
        const isSpeaker = state.current_speaker === this.#selfId;
        this.#show('btn-yield-floor', phase === 'floor_held' && isSpeaker);

        // Motion display
        this.#show('motion-section', !!motion);
        if (motion) {
            this.#text('motion-text',  `"${motion.text}"`);
            const mover = state.members.find(m => m.id === motion.moved_by);
            this.#text('motion-meta', `Moved by ${mover?.name ?? motion.moved_by}`);
        }

        // Motion action buttons
        this.#show('btn-second',    phase === 'motion_pending' && motion?.moved_by !== this.#selfId);
        this.#show('btn-withdraw',  ['motion_pending', 'seconded'].includes(phase) && motion?.moved_by === this.#selfId);
        this.#show('btn-call-vote', phase === 'seconded' && isChair);

        // Voting section — guests can see tallies but not vote
        const amGuest = me?.is_guest ?? false;
        this.#show('voting-section', phase === 'voting');
        if (phase === 'voting' && motion) {
            this.#text('vote-yea-count',     String(motion.votes.yea));
            this.#text('vote-nay-count',     String(motion.votes.nay));
            this.#text('vote-abstain-count', String(motion.votes.abstain));
            const hasVoted = motion.member_votes[this.#selfId] !== undefined;
            ['btn-yea', 'btn-nay', 'btn-abstain'].forEach(id => {
                const btn = document.getElementById(id);
                if (btn) btn.disabled = hasVoted || amGuest;
            });
        }

        // Motion form (not shown in seconded — server rejects make_motion when debate is active)
        this.#show('motion-form', ['open', 'floor_held'].includes(phase));

        // Vote result
        const showResult = phase === 'vote_closed' && motion?.result;
        this.#show('vote-result', showResult);
        if (showResult) {
            this.#text('vote-result', `Motion ${motion.result.toUpperCase()}`);
        }

        // Chair controls
        this.#show('chair-controls', isChair);
    }

    #fmt(s) {
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }

    #on(id, event, fn) {
        document.getElementById(id)?.addEventListener(event, fn);
    }

    #text(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    #show(id, visible) {
        const el = document.getElementById(id);
        if (el) el.style.display = visible ? '' : 'none';
    }
}
