/* AXIOM :: Mission Control
 *
 * Vanilla ES2020. No build step, no framework, no network except this origin's /api.
 *
 * Two things here are load-bearing and worth reading before changing anything:
 *
 * 1. The task grid does KEYED DOM updates, not innerHTML replacement. Re-rendering the
 *    grid wholesale would work, but it would destroy the element mid-animation on every
 *    poll — and the animations ARE the product here. A cell keeps its identity for the
 *    life of the task so a state change or a fence advance can be flashed on it.
 *
 * 2. lease_epoch is diffed client-side against the previously observed value. When a
 *    worker is SIGKILLed and another takes the task over, the epoch increments; that
 *    increment is the proof the old worker can no longer settle. It gets a flash, a
 *    persistent corner mark, and a counter — because it is the single most important
 *    invisible thing this system does.
 *
 * 3. POLLING IS METERED. The hosted demo runs on Lambda inside the always-free 1,000,000
 *    requests/month, and this page is the only client. A fixed 1s poll issuing five GETs
 *    per tick is ~15,000,000 requests a month from ONE tab left open — fifteen times the
 *    entire free allowance, from a browser nobody is looking at. So the poll here is a
 *    ladder (1s → 60s) that backs off when nothing changes, pins itself to 1s while work
 *    is actually in flight, and stops dead — no timer at all — while the tab is hidden.
 *    The current interval and the session's request count are printed in the header,
 *    because a cost control you cannot see is a cost control you will not trust.
 */
'use strict';

// ─────────────────────────────────────────────────────────────────────── util ──

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

/** Escape for interpolation into innerHTML. Every value below comes from the database
 *  and at least one of them (memory content) is attacker-influenced by construction —
 *  a poisoned memory is literally a demo beat. */
function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

const money = (cents) => {
  const n = Number(cents || 0) / 100;
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
const moneyShort = (cents) => {
  const n = Number(cents || 0) / 100;
  return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });
};

const hhmmss = (iso) => {
  if (!iso) return '--:--:--';
  const d = new Date(iso);
  if (isNaN(d)) return '--:--:--';
  return d.toTimeString().slice(0, 8);
};
/** Compact age: 4s / 2m14 / 1h03. Fixed-width-ish so the column does not jitter. */
function age(secs) {
  const s = Math.max(0, Math.floor(secs));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0');
  return Math.floor(s / 3600) + 'h' + String(Math.floor((s % 3600) / 60)).padStart(2, '0');
}

const shortId = (id) => (id ? String(id).slice(0, 8) : '—');

const TASK_STATES = ['PENDING', 'READY', 'LEASED', 'AWAITING_APPROVAL',
  'ACTION_PREPARED', 'SUCCEEDED', 'FAILED', 'DEAD_LETTER', 'CANCELLED'];

const SHARD_COUNT = 16;

// ──────────────────────────────────────────────────────────────────────── api ──

let apiFailures = 0;

async function api(path, opts) {
  // Every request this tab makes is counted here and nowhere else, so the number in the
  // header is the real one — including the ones a panel swallows via get().
  P.reqs++;
  const res = await fetch(path, Object.assign({ headers: { 'accept': 'application/json' } }, opts || {}));
  if (!res.ok) {
    let detail = res.status + ' ' + res.statusText;
    // Two error shapes exist: the API's own handlers emit {error, detail}, while a bare
    // FastAPI HTTPException (404 no mission, 409 approval not PENDING, 422 validation)
    // emits only {detail}. Reading just `error` turned every one of those into the
    // useless toast "409 Conflict".
    try { const j = await res.json(); if (j && (j.error || j.detail)) detail = j.error || j.detail; } catch (e) { /* body not json */ }
    throw new Error(detail);
  }
  return res.json();
}

/** GET that never throws: a panel whose endpoint is down should go quiet, not take the
 *  whole console with it. Returns `fallback` and records the failure for the POLL lamp. */
async function get(path, fallback) {
  try {
    const v = await api(path);
    return v;
  } catch (e) {
    apiFailures++;
    return fallback;
  }
}

async function post(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'accept': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

// ────────────────────────────────────────────────────────────────────── state ──

const S = {
  mission: null,
  tasks: [],
  epochs: new Map(),        // task_id -> last observed lease_epoch
  states: new Map(),        // task_id -> last observed state
  fenced: new Set(),        // task_ids that have been fenced since page load
  fenceFrom: new Map(),     // task_id -> {from, to, at} for the recent-fence callout
  fenceCount: 0,
  changed: false,           // did this poll cycle observe anything at all? drives backoff
  chaosAt: 0,               // when KILL A WORKER was last fired; arms the recovery banner
  missionSig: '',
  seenEvents: new Set(),    // "subject_id:seq"
  agents: [],
  agentsFetchedAt: 0,
  pstats: null,
  view: 'ops',
  lastRecall: null,         // {params, ids:Set, count}
  drawerTask: null,
  crashWindows: null,
  booted: false,
};

// ────────────────────────────────────────────────────────────────── poll meter ──

/** The rungs. 1s is the demo cadence — a state change has to appear inside one beat of
 *  the operator's attention. 60s is what an abandoned tab settles to: 8 requests a minute
 *  is 345,000 a month, which fits inside Lambda's free 1,000,000 with room for a second
 *  viewer. Nothing in between is arbitrary either: 5s is "a human is reading", 15s and
 *  30s are "the page is open on a spare monitor". */
const LADDER = [1000, 2000, 5000, 15000, 30000, 60000];

/** Cycles of stillness before stepping out one rung. Three, not one: a task can sit in
 *  LEASED for two seconds mid-provider-call and that is not idleness. */
const IDLE_BEFORE_BACKOFF = 3;

/** The same thing, but for a board that still has a lease outstanding. A held lease is
 *  weak evidence that something is about to happen, so it buys patience rather than
 *  immunity: 25 cycles at 1s outlasts the 20s lease TTL, which means a takeover after a
 *  worker dies is still caught on the fast rung — and a board left LEASED forever by a
 *  crashed worker nobody came back for still backs off instead of polling until the
 *  free tier runs out. Nothing on this page is allowed to pin the poll open. */
const IDLE_BEFORE_BACKOFF_LIVE = 25;

/** The slow set (health, workers, provider stats, receipts, approvals) never runs faster
 *  than this no matter how fast the core loop is. It is 5 of the 8 requests per cycle. */
const AUX_MIN_MS = 4000;

/** States that mean a worker is holding this task RIGHT NOW, so the next second is worth
 *  paying for. Only the two lease-holding states qualify.
 *
 *  READY and PENDING are deliberately absent, and that omission is the single largest
 *  cost decision in this file. A freshly seeded board is 30 READY tasks that will not
 *  move until a human presses RUN MISSION — it is the demo's RESTING state, not its
 *  live one — and counting READY as live pinned the poll at 1s through it forever:
 *  255 requests/minute, 11,000,000 a month, eleven times the entire free tier, from a
 *  tab sitting on an idle board. Measured, not theorised. When a task does leave READY
 *  the renderers set S.changed and the ladder snaps back to 1s within one cycle.
 *
 *  AWAITING_APPROVAL is absent for the same reason: it can sit for an hour waiting on a
 *  human, and the human's click resets the ladder anyway. */
const LIVE_STATES = new Set(['LEASED', 'ACTION_PREPARED']);

const P = {
  rung: 0,
  idle: 0,
  timer: null,
  lastAux: 0,
  lastTick: 0,
  reqs: 0,
  busy: false,
};

// ─────────────────────────────────────────────────────────────────────── toast ──

let toastTimer = null;
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.toggle('is-bad', !!bad);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3600);
}

// ───────────────────────────────────────────────────────────────────── header ──

function renderHealth(h) {
  const set = (k, ok) => {
    const el = $(`.hx[data-k="${k}"]`);
    if (!el) return;
    el.classList.toggle('is-ok', !!ok);
    el.classList.toggle('is-bad', !ok);
  };
  set('db', h && h.db);
  set('provider', h && h.provider);
  $('#hver').textContent = h && h.version ? h.version : '—';
}

function renderMission(m) {
  // The backoff decision is made from what the DATA says, not from what the DOM did, so
  // every renderer that can observe a change reports it here.
  const sig = m ? [m.state, m.spent_cents, JSON.stringify(m.by_state || {})].join('|') : '';
  if (sig !== S.missionSig) { S.missionSig = sig; S.changed = true; }

  S.mission = m && m.id ? m : null;
  if (!S.mission) {
    $('#m-title').textContent = 'no mission';
    $('#m-goal').textContent = 'seed the demo to create one';
    return;
  }
  $('#m-title').textContent = m.title || '—';
  $('#m-goal').textContent = m.goal || '';
  $('#m-state').textContent = m.state || '—';
  $('#m-uuid').textContent = shortId(m.id);
  $('#m-created').textContent = hhmmss(m.created_at);

  const spent = Number(m.spent_cents || 0);
  const budget = Number(m.budget_cents || 0);
  const pct = budget > 0 ? Math.min(100, (spent / budget) * 100) : 0;
  const fill = $('#b-fill');
  fill.style.width = pct.toFixed(2) + '%';
  fill.classList.toggle('is-warn', pct >= 80 && pct < 100);
  fill.classList.toggle('is-alarm', pct >= 100);
  $('#b-nums').innerHTML =
    `<span class="${pct >= 100 ? 'over' : ''}">${esc(money(spent))}</span>` +
    `<span style="color:var(--faint)"> / ${esc(money(budget))}</span>` +
    `<span style="color:var(--ghost)"> · ${pct.toFixed(0)}%</span>`;

  const by = m.by_state || {};
  $('#statestrip').innerHTML = TASK_STATES
    .filter((s) => by[s])
    .map((s) => `<span class="sc"><i class="mark m-${s}"></i>${esc(s)} <b>${by[s]}</b></span>`)
    .join('');
}

function renderProviderStats(p) {
  if (p && S.pstats && (p.refunds !== S.pstats.refunds || p.replays !== S.pstats.replays)) {
    S.changed = true;   // the provider acted: never let the poll be backing off through that
  }
  S.pstats = p;
  if (!p) return;
  const dupes = Number(p.duplicate_orders || 0);
  $('#dupes').textContent = String(dupes);
  $('#moneyshot').classList.toggle('is-alarm', dupes > 0);
  $('#p-refunds').textContent = String(p.refunds != null ? p.refunds : '—');
  $('#p-replays').textContent = String(p.replays != null ? p.replays : '—');
  $('#p-total').textContent = moneyShort(p.total_cents);
}

// ────────────────────────────────────────────────────────────────── task grid ──

/** `fenced` marks a task that has been taken over at least once since this tab loaded.
 *  The corner tick and the highlighted epoch persist after the flash decays, so an
 *  operator who arrives mid-incident can still see which tasks changed hands. */
const FENCE_CALLOUT_MS = 20000;

function cellHTML(t, fenced) {
  const key = String(t.dedupe_key || '').replace(/^order:/, '').replace(/:refund$/, '');

  // For the first 20 seconds after a takeover the epoch chip shows the TRANSITION —
  // "e1 → e2" — not just the new value. On a 720p screen recording a single digit
  // changing from 1 to 2 in a 9px chip is invisible; an arrow appearing is not. After
  // the callout expires it collapses back to the plain chip and the corner mark stays.
  const f = S.fenceFrom.get(t.id);
  const recent = f && (Date.now() - f.at) < FENCE_CALLOUT_MS && Number(f.to) === Number(t.lease_epoch);
  const epoch = recent
    ? `<span class="ep bumped is-callout">e${esc(f.from)}<b>&rarr;</b>e${esc(f.to)}</span>`
    : `<span class="ep${fenced ? ' bumped' : ''}">e${esc(t.lease_epoch)}</span>`;

  return (
    `<div class="cell-key">${esc(key)}</div>` +
    `<div class="cell-state">${esc(t.state)}</div>` +
    `<div class="cell-meta">` +
      `<span class="sh">s${String(t.shard).padStart(2, '0')}</span>` +
      `<span class="at${Number(t.attempt) > 1 ? ' retry' : ''}">a${esc(t.attempt)}</span>` +
      epoch +
    `</div>` +
    (fenced ? '<i class="fencemark"></i>' : '')
  );
}

/** How long the ARMED banner is allowed to keep promising a crash before it admits none
 *  arrived. A takeover needs the chaos worker to die, the 20s lease to lapse, and another
 *  worker to claim — so this has to be comfortably longer than one lease TTL, and 90s is.
 *  Measured against the real failure it exists for: pressing KILL A WORKER on a board that
 *  has already drained dispatches a worker with nothing to claim, so it exits cleanly and
 *  no fence ever lands. The banner used to sit there narrating an event that was never
 *  coming, for the rest of the session — on a screen recording that is a caption claiming
 *  something the grid underneath it plainly is not doing. */
const CHAOS_ARM_TIMEOUT_MS = 90000;

/** The banner above the grid. It is the only thing on this page allowed to narrate:
 *  ARMED after KILL A WORKER is pressed, FENCED when the takeover it predicted actually
 *  lands, EXPIRED when it does not, and hidden otherwise. It exists because the proof —
 *  an integer incrementing inside one cell of a 30-cell grid — is true but unfilmable.
 *
 *  Every visible state carries its own dismissal, so the bar cannot outlive the thing it
 *  is describing. That is the property that makes it safe to point a camera at. */
function chaosBar(kind, label, text) {
  const bar = $('#chaosbar');
  clearTimeout(chaosBar.t);
  if (!kind) { bar.hidden = true; bar.className = 'chaosbar'; return; }
  bar.hidden = false;
  bar.className = 'chaosbar is-' + kind;
  $('#cb-lbl').textContent = label;
  $('#cb-txt').textContent = text;

  if (kind === 'armed') {
    // Not silence, and not an error either: a chaos worker that found an empty queue and
    // exited is correct behaviour, and the honest report is that no takeover was observed.
    chaosBar.t = setTimeout(() => chaosBar('expired', 'NO TAKEOVER OBSERVED',
      'the chaos worker found nothing to claim · SEED, then KILL A WORKER'), CHAOS_ARM_TIMEOUT_MS);
  } else {
    chaosBar.t = setTimeout(() => chaosBar(null), FENCE_CALLOUT_MS);
  }
}

function renderTasks(tasks) {
  S.tasks = tasks;
  const grid = $('#taskgrid');
  const seen = new Set();
  let newFences = 0;
  let firstFenced = null;      // the cell to scroll to; a fence off-screen proves nothing

  for (const t of tasks) {
    seen.add(t.id);
    let cell = grid.querySelector(`[data-id="${t.id}"]`);
    const prevState = S.states.get(t.id);
    const prevEpoch = S.epochs.get(t.id);

    if (!cell) {
      cell = document.createElement('div');
      cell.className = 'cell';
      cell.dataset.id = t.id;
      cell.tabIndex = 0;
      // Drop the flash class once it has played. Without this the class accumulates on
      // the element forever and a later re-flash has nothing to re-trigger.
      cell.addEventListener('animationend', (ev) => {
        cell.classList.remove(ev.animationName === 'flash-fence' ? 'did-fence' : 'did-change');
      });
      grid.appendChild(cell);
    }

    // Swap only the state class; never rebuild className, which would clobber whichever
    // flash animation is mid-flight.
    if (cell.dataset.state !== t.state) {
      if (cell.dataset.state) cell.classList.remove('s-' + cell.dataset.state);
      cell.classList.add('s-' + t.state);
      cell.dataset.state = t.state;
    }

    // The fence advanced: another worker took this task over and the previous lease
    // holder's settle is now rejected on sight. This is the money moment of a crash demo.
    // A FENCE is a takeover, not a claim. epoch 0 -> 1 is simply the first worker picking
    // the task up, and flashing that as a fence made the banner announce "FENCE ADVANCED"
    // thirty times on a fresh seed while the counter underneath it still read 0 — the
    // counter has always defined a fence as (lease_epoch - 1), and the colour has to
    // agree with the number or neither is believable.
    const fenced = prevEpoch !== undefined
      && Number(t.lease_epoch) > Number(prevEpoch)
      && Number(t.lease_epoch) >= 2;
    if (fenced) {
      newFences++;
      S.fenced.add(t.id);
      S.fenceFrom.set(t.id, { from: prevEpoch, to: t.lease_epoch, at: Date.now() });
      if (!firstFenced) firstFenced = { cell: cell, task: t, from: prevEpoch };
      cell.classList.remove('did-fence');
      void cell.offsetWidth;           // restart the animation on a re-fence
      cell.classList.add('did-fence');
    } else if (prevState !== undefined && prevState !== t.state) {
      cell.classList.remove('did-change');
      void cell.offsetWidth;
      cell.classList.add('did-change');
    }
    if (fenced || (prevState !== undefined && prevState !== t.state) || prevState === undefined) {
      S.changed = true;
    }

    const html = cellHTML(t, S.fenced.has(t.id));
    if (cell.dataset.sig !== html) { cell.innerHTML = html; cell.dataset.sig = html; }

    S.states.set(t.id, t.state);
    S.epochs.set(t.id, t.lease_epoch);
  }

  // Tasks that vanished (a reset) lose their cells.
  for (const cell of Array.from(grid.children)) {
    if (!seen.has(cell.dataset.id)) cell.remove();
  }

  // The TOTAL number of fence advances is derivable from the data, not just from what
  // this browser tab happened to witness: a task claimed once sits at epoch 1, so every
  // epoch beyond that is one takeover from a worker that can no longer settle. Counting
  // only live increments would show 0 to an operator who loaded the page after the
  // incident — which is precisely when someone loads an ops console.
  const total = tasks.reduce((n, t) => n + Math.max(0, Number(t.lease_epoch || 0) - 1), 0);
  S.fenceCount = total;
  $('#fence-count').textContent = String(total);

  if (newFences) {
    const fm = $('.fence-meter');
    fm.classList.add('is-live');
    $('#fence-delta').textContent = '+' + newFences;
    // 6s, not 2.4s. This has to survive being scrubbed past in a screen recording.
    setTimeout(() => { fm.classList.remove('is-live'); $('#fence-delta').textContent = ''; }, 6000);

    if (firstFenced) {
      const k = String(firstFenced.task.dedupe_key || '').replace(/^order:/, '').replace(/:refund$/, '');
      // Kept under ~70 characters on purpose: the bar is one line and at 1280px the
      // sentence that was here before was ellipsised exactly where the meaning was.
      chaosBar('fenced', 'FENCE ADVANCED',
        `${k} · lease_epoch e${firstFenced.from} → e${firstFenced.task.lease_epoch}` +
        (newFences > 1 ? ` · +${newFences - 1} more` : '') +
        ' · the old lease can no longer settle');
      // Bring the proof on screen. `nearest` so a fence in the visible rows does not
      // yank the grid around for no reason.
      firstFenced.cell.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      // The dismissal timer belongs to chaosBar itself now, so arriving here also cancels
      // the ARMED timeout — a fence that lands is the answer the banner was waiting for.
    }
  }
  $('#grid-note').textContent = tasks.length + ' tasks';
}

function renderLegend() {
  $('#legend').innerHTML = TASK_STATES
    .map((s) => `<span class="lg"><i class="mark m-${s}"></i>${esc(s)}</span>`).join('');
}

// ─────────────────────────────────────────────────────────────────────── events ──

/** `agent:ab77ef12-f521-…` is 44 characters of noise in a 300px column. Keep the prefix
 *  and the first octet — enough to tell two workers apart, which is all it is for. */
function shortActor(a) {
  const s = String(a || '');
  const m = s.match(/^([a-z]+):([0-9a-f]{8})[0-9a-f-]*$/i);
  return m ? m[1] + ':' + m[2] : s;
}

/** The one line of an event that an operator actually reads.
 *
 *  task.recovered.detail.rationale is the most valuable string this system produces —
 *  it is the recovery decision explaining itself in terms of the receipt it found and
 *  the memories it recalled. Surfacing it here is the difference between the timeline
 *  showing that recovery happened and showing WHY it was safe. */
function eventDetail(e) {
  const d = e.detail || {};
  if (e.event_type === 'task.recovered') {
    return `<span class="ev-act">${esc(d.action || 'RECOVER')}</span>` +
           (d.rationale ? `<span class="ev-why">${esc(d.rationale)}</span>` : '');
  }
  if (e.event_type === 'approval.requested' && d.reason) return `<span class="ev-why">${esc(d.reason)}</span>`;
  if (e.event_type === 'approval.decided') {
    return `<span class="ev-act">${d.approved ? 'APPROVED' : 'REJECTED'}</span>`;
  }
  if (e.event_type === 'task.dead_lettered' && d.reason) return `<span class="ev-why">${esc(d.reason)}</span>`;
  if (e.event_type === 'memory.quarantined' && d.reason) return `<span class="ev-why">${esc(d.reason)}</span>`;
  if (e.event_type === 'attempt.settled') {
    const bits = [];
    if (d.outcome) bits.push(d.outcome);
    if (d.replayed) bits.push('PROVIDER REPLAY');
    if (d.provider_ref) bits.push(d.provider_ref);
    return bits.length ? `<span class="ev-why">${esc(bits.join(' · '))}</span>` : '';
  }
  return '';
}

/** Which events deserve a coloured rule. Everything else stays grey on purpose:
 *  if every line is highlighted, no line is. */
function eventClass(type) {
  if (type === 'task.recovered') return 'e-recovered';
  if (type === 'task.dead_lettered') return 'e-dead';
  if (type === 'memory.quarantined') return 'e-quarantine';
  if (type === 'approval.requested' || type === 'approval.decided') return 'e-approval';
  if (type === 'attempt.settled') return 'e-settled';
  return '';
}

function renderEvents(events) {
  if (!events || !events.length) return;
  const box = $('#events');
  const frag = document.createDocumentFragment();

  // The API returns newest-first; walk oldest-first so prepending preserves order.
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    const key = e.subject_id + ':' + e.seq;
    if (S.seenEvents.has(key)) continue;
    S.seenEvents.add(key);

    const row = document.createElement('div');
    row.className = 'ev ' + eventClass(e.event_type) + (S.booted ? ' is-new' : '');
    const trans = (e.from_state || e.to_state)
      ? `<div class="ev-trans">${esc(e.from_state || '·')} <b>&rarr;</b> ${esc(e.to_state || '·')}</div>` : '';
    row.innerHTML =
      `<div class="ev-time">${esc(hhmmss(e.occurred_at))}</div>` +
      `<div class="ev-main">` +
        `<div class="ev-type">${esc(e.event_type)}</div>` +
        trans +
        eventDetail(e) +
        `<div class="ev-actor">${esc(shortActor(e.actor))}</div>` +
      `</div>`;
    frag.insertBefore(row, frag.firstChild);
  }
  if (frag.childNodes.length) { box.insertBefore(frag, box.firstChild); S.changed = true; }

  while (box.children.length > 260) box.removeChild(box.lastChild);
  $('#ev-note').textContent = box.children.length + ' shown';
}

// ────────────────────────────────────────────────────────────────────── agents ──

function renderAgents(agents) {
  S.agents = agents || [];
  S.agentsFetchedAt = Date.now();
  paintAgents();
}

/** Painted on its own 1s timer as well as on fetch, so the age counts up smoothly
 *  between polls. A worker that has stopped heartbeating must LOOK like it is drifting
 *  away, not tick over in silent 1s steps. */
function paintAgents() {
  const drift = (Date.now() - S.agentsFetchedAt) / 1000;
  const box = $('#workers');
  if (!S.agents.length) {
    box.innerHTML = '<span class="empty">no workers registered</span>';
    $('#workers-note').textContent = '0';
    return;
  }
  let live = 0;
  box.innerHTML = S.agents.map((a) => {
    const secs = Number(a.seconds_since_heartbeat || 0) + drift;
    // Lease is 20s; a worker silent for a quarter of it is suspicious, and past the
    // lease its tasks are claimable by anyone, which is functionally dead.
    let cls = '', status = a.status;
    if (a.status === 'DEAD' || secs > 20) { cls = 'is-dead'; status = 'DEAD'; }
    else if (secs > 6) { cls = 'is-stale'; status = 'STALE'; }
    else { live++; }

    // An empty shard list means no affinity — the worker claims from every shard.
    // See worker.py, which logs `shards=ALL` for exactly this case.
    const owned = (a.shards || []).map(Number);
    const all = owned.length === 0;
    const shards = new Set(owned);
    let map = '';
    for (let i = 0; i < SHARD_COUNT; i++) map += `<i class="${all || shards.has(i) ? 'on' : ''}"></i>`;

    return (
      `<div class="worker ${cls}">` +
        `<div class="worker-ref">${esc(a.worker_ref)}</div>` +
        `<div class="worker-age">${esc(age(secs))}</div>` +
        `<div class="worker-sub">` +
          `<span class="worker-status">${esc(status)}</span>` +
          `<span class="worker-shards">${all ? 'ALL' : owned.length + '/' + SHARD_COUNT}</span>` +
          `<span class="shardmap" title="${all ? 'no shard affinity' : 'shards ' + owned.join(',')}">${map}</span>` +
        `</div>` +
      `</div>`
    );
  }).join('');
  $('#workers-note').textContent = live + '/' + S.agents.length + ' live';
}

// ──────────────────────────────────────────────────────────── unsettled receipts ──

function renderUnsettled(rows) {
  const box = $('#unsettled');
  $('#unsettled-note').textContent = String((rows || []).length);
  if (!rows || !rows.length) {
    box.innerHTML = '<span class="empty">none — nothing is in flight</span>';
    return;
  }
  box.innerHTML = rows.map((r) => (
    `<div class="receipt">` +
      `<div class="r-top">` +
        `<span class="r-key">${esc(r.step_name || r.operation)}</span>` +
        `<span class="r-amt">${esc(money(r.amount_cents))}</span>` +
      `</div>` +
      `<div class="r-sub">${esc(r.attempt_state)} · ${esc(shortId(r.idempotency_key))} · ${esc(hhmmss(r.prepared_at))}</div>` +
    `</div>`
  )).join('');
}

// ────────────────────────────────────────────────────────────────────── rewind ──

async function doRewind(secs) {
  const out = $('#rewind-out');
  out.innerHTML = '<span class="empty">reading…</span>';
  const r = await get('/api/rewind?seconds_ago=' + encodeURIComponent(secs), null);
  if (!r || r.error) {
    out.innerHTML = '<span class="empty">no snapshot that far back (GC window)</span>';
    return;
  }
  const then = r.tasks_by_state || {};
  const now = (S.mission && S.mission.by_state) || {};
  const keys = TASK_STATES.filter((k) => then[k] || now[k]);
  const rows = keys.map((k) => {
    const a = Number(then[k] || 0), b = Number(now[k] || 0);
    const d = b - a;
    const dtxt = d === 0 ? '' : `<span class="delta${d < 0 ? ' neg' : ''}">${d > 0 ? '+' : ''}${d}</span>`;
    return `<div class="rw-row"><span><i class="mark m-${k}"></i> ${esc(k)}</span><b>${a} &rarr; ${b} ${dtxt}</b></div>`;
  }).join('');
  out.innerHTML =
    `<div class="rw-at">AS OF ${esc(hhmmss(r.at))} &nbsp;(-${esc(age(secs))})</div>` + rows +
    `<div class="rw-row"><span>MEMORIES</span><b>${esc(r.memory_count)}</b></div>`;
}

// ────────────────────────────────────────────────────────────────────── ledger ──

function renderLedger(rows) {
  const t = $('#ledger-tbl');
  if (!rows || !rows.length) {
    t.innerHTML = '<tbody><tr><td class="mut">the provider has issued no refunds</td></tr></tbody>';
    return;
  }
  // Count per order so a duplicate is flagged on the row, not only in the headline.
  const perOrder = {};
  for (const r of rows) perOrder[r.order_ref] = (perOrder[r.order_ref] || 0) + 1;

  t.innerHTML =
    '<thead><tr>' +
      '<th>PROVIDER REF</th><th>ORDER</th><th class="num">AMOUNT</th>' +
      '<th>STATUS</th><th class="num">REPLAYS</th><th class="c-key">IDEMPOTENCY KEY</th>' +
      '<th>CREATED</th>' +
    '</tr></thead><tbody>' +
    rows.map((r) => {
      const dup = perOrder[r.order_ref] > 1;
      return '<tr>' +
        `<td class="strong">${esc(r.provider_ref)}</td>` +
        `<td class="${dup ? 'alarm' : ''}">${esc(r.order_ref)}${dup ? ' ×' + perOrder[r.order_ref] : ''}</td>` +
        `<td class="num">${esc(money(r.amount_cents))}</td>` +
        `<td class="${r.status === 'succeeded' ? 'ok' : ''}">${esc(r.status)}</td>` +
        // A replay is the idempotency guarantee doing its job: the provider recognised a
        // key it had already acted on and did NOT act again. Worth colouring.
        `<td class="num ${Number(r.replay_count) > 0 ? 'warn' : 'mut'}">${esc(r.replay_count)}</td>` +
        `<td class="mut c-key" title="${esc(r.idempotency_key)}">${esc(r.idempotency_key)}</td>` +
        `<td class="mut">${esc(hhmmss(r.created_at))}</td>` +
      '</tr>';
    }).join('') +
    '</tbody>';
}

// ────────────────────────────────────────────────────────────────────── memory ──

function trustMeter(level) {
  const n = Number(level || 0);
  let s = `<span class="trust t${n}">`;
  for (let i = 0; i < 4; i++) s += `<i class="${i <= n ? 'on' : ''}"></i>`;
  return s + '</span>';
}

function renderMemories(rows) {
  const t = $('#mem-tbl');
  if (!rows || !rows.length) {
    t.innerHTML = '<tbody><tr><td class="mut">no memories</td></tr></tbody>';
    return;
  }
  // Narrow columns are pinned with width:1% + nowrap so CONTENT absorbs all remaining
  // width. Without this the content column collapses to ~90px and every row becomes a
  // ten-line paragraph, which is what the first build of this table did.
  t.innerHTML =
    '<thead><tr>' +
      '<th class="c-ctx">CONTEXT</th><th>CONTENT</th>' +
      '<th class="c-adm">ADMISSIBILITY</th><th class="c-act"></th>' +
    '</tr></thead><tbody>' +
    rows.map((m) => {
      const q = !!m.quarantined;
      const dup = m.outcome === 'DUPLICATE_EFFECT';
      return `<tr class="${q ? 'is-q' : ''}">` +
        `<td class="c-ctx"><div class="m-key">${esc(m.context_key)}</div>` +
          `<div class="m-cls">${esc(m.memory_class)}</div></td>` +
        `<td class="m-content wrap">${esc(m.content)}` +
          `<div class="m-out"><span class="${dup ? 'alarm' : 'mut'}">${esc(m.outcome)}</span></div>` +
          (m.quarantine_reason ? `<div class="m-qr">held: ${esc(m.quarantine_reason)}</div>` : '') +
        `</td>` +
        `<td class="c-adm"><span class="rc rc-${esc(m.retrieval_class)}">${esc(m.retrieval_class)}</span>` +
          `<div class="m-prov">${esc(m.source)}</div>${trustMeter(m.trust_level)}</td>` +
        `<td class="c-act">${q ? '<span class="mut">held</span>'
                 : `<button class="btn btn-xs btn-reject" data-quarantine="${esc(m.id)}">QUARANTINE</button>`}</td>` +
      '</tr>';
    }).join('') +
    '</tbody>';
}

function renderHits(hits, goneIds) {
  const box = $('#recall-hits');
  if (!hits || !hits.length) {
    box.innerHTML = '<div class="hit"><div class="hit-body"><span class="empty">no admissible memory matched</span></div></div>';
    return;
  }
  const gone = goneIds || new Set();
  box.innerHTML = hits.map((h) => {
    const sim = Number(h.similarity || 0);
    return (
      `<div class="hit">` +
        `<div><div class="hit-sim">${sim.toFixed(3)}</div>` +
          `<div class="simbar"><i style="width:${Math.max(0, Math.min(100, sim * 100)).toFixed(1)}%"></i></div></div>` +
        `<div class="hit-body">` +
          `<div class="hit-content">${esc(h.content)}</div>` +
          `<div class="hit-meta">` +
            `<span>${esc(h.outcome)}</span><span>${esc(h.source)}</span>` +
            `<span>trust ${esc(h.trust_level)}</span><span>${esc(shortId(h.id))}</span>` +
          `</div>` +
        `</div>` +
        `<div>${trustMeter(h.trust_level)}</div>` +
      `</div>`
    );
  }).join('') +
  (gone.size ? Array.from(gone).map((g) => (
    `<div class="hit is-gone"><div><div class="hit-sim">—</div></div>` +
    `<div class="hit-body"><div class="hit-content">${esc(g.content)}</div>` +
    `<div class="hit-meta"><span>dropped out of recall · quarantined</span></div></div><div></div></div>`
  )).join('') : '');
}

async function runRecall(isRerun) {
  const params = {
    query: $('#r-query').value,
    memory_class: $('#r-class').value,
    context_key: $('#r-ctx').value.trim() || null,
    k: Number($('#r-k').value) || 5,
  };
  let res;
  try {
    res = await post('/api/memories/recall', params);
  } catch (e) {
    toast('recall failed: ' + e.message, true);
    return;
  }
  const hits = res.hits || [];
  const lamp = $('#idx-lamp');
  const ok = !!res.plan_uses_vector_index;
  lamp.classList.toggle('is-ok', ok);
  lamp.classList.toggle('is-bad', !ok);
  $('#idx-text').textContent = 'plan_uses_vector_index ' + (ok ? 'TRUE' : 'FALSE');

  const ids = new Set(hits.map((h) => h.id));
  let gone = new Set();
  if (isRerun && S.lastRecall) {
    for (const prev of S.lastRecall.hits) if (!ids.has(prev.id)) gone.add(prev);
    $('#recall-delta').textContent = gone.size
      ? `${S.lastRecall.hits.length} → ${hits.length} hits · ${gone.size} no longer admissible`
      : `${S.lastRecall.hits.length} → ${hits.length} hits`;
  } else {
    $('#recall-delta').textContent = `${hits.length} hit${hits.length === 1 ? '' : 's'}`;
  }

  renderHits(hits, gone);
  S.lastRecall = { params, hits };
}

// ─────────────────────────────────────────────────────────────────── approvals ──

function renderApprovals(rows) {
  const box = $('#approvals');
  const badge = $('#appr-badge');
  const n = (rows || []).length;
  badge.hidden = n === 0;
  badge.textContent = String(n);

  if (!n) {
    box.dataset.sig = '';
    box.innerHTML = '<span class="empty">nothing is waiting on a human</span>';
    return;
  }

  // These cards contain a text input the operator types into. Re-rendering the list on
  // every 4s poll would wipe a half-typed name out from under them, so redraw only when
  // the actual set of pending approvals changes.
  const sig = rows.map((a) => a.id).join(',');
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.innerHTML = rows.map((a) => {
    const ev = a.evidence_memory_ids || [];
    const expSecs = a.expires_at ? (new Date(a.expires_at) - Date.now()) / 1000 : null;
    return (
      `<div class="appr" data-appr="${esc(a.id)}">` +
        `<div class="appr-top">` +
          `<div class="appr-action">${esc(a.proposed_action || a.step_name)}</div>` +
          `<div class="appr-amt">${esc(money(a.proposed_amount_cents))}</div>` +
        `</div>` +
        `<div class="appr-reason">${esc(a.reason)}</div>` +
        `<div class="appr-grid">` +
          `<div class="appr-f"><span>POLICY</span><b>${esc(a.policy_id || '—')} v${esc(a.policy_version != null ? a.policy_version : '—')}</b></div>` +
          `<div class="appr-f"><span>STEP</span><b>${esc(a.step_name || '—')}</b></div>` +
          `<div class="appr-f"><span>TASK</span><b>${esc(shortId(a.task_id))}</b></div>` +
          `<div class="appr-f"><span>EVIDENCE MEMORIES (${ev.length})</span><b>${ev.length ? ev.map(shortId).map(esc).join(' · ') : 'none'}</b></div>` +
        `</div>` +
        `<div class="appr-acts">` +
          `<input class="who" value="human:ops@acme.example" data-who>` +
          `<button class="btn btn-approve" data-decide="1">APPROVE</button>` +
          `<button class="btn btn-reject" data-decide="0">REJECT</button>` +
          (expSecs !== null
            ? `<span class="appr-expiry${expSecs < 120 ? ' is-soon' : ''}">expires in ${esc(age(expSecs))}</span>` : '') +
        `</div>` +
      `</div>`
    );
  }).join('');
}

async function decide(card, approved) {
  const id = card.dataset.appr;
  const who = $('[data-who]', card).value || 'human:operator';
  const btns = $$('button', card);
  btns.forEach((b) => (b.disabled = true));
  try {
    await post(`/api/approvals/${encodeURIComponent(id)}/decide`,
      { approved: approved, decided_by: who, note: approved ? 'approved in Mission Control' : 'rejected in Mission Control' });
    toast((approved ? 'approved' : 'rejected') + ' · ' + shortId(id));
    card.remove();
    // A decision unblocks a task: the next second is interesting, so poll it now.
    P.rung = 0; P.idle = 0; run();
  } catch (e) {
    toast('decision failed: ' + e.message, true);
    btns.forEach((b) => (b.disabled = false));
  }
}

// ───────────────────────────────────────────────────────────── crash windows ──

/** Rendered as definition blocks rather than a table.
 *
 *  This is a specification an operator READS, not a grid they scan, and six columns of
 *  prose in a 730px column produced a table where "No effect can exist" wrapped over six
 *  lines and the test path was clipped. One block per window uses the full width and
 *  keeps every field intact. */
function renderCrashWindows(rows) {
  const t = $('#spec-tbl');
  if (!rows || !rows.length) {
    t.innerHTML = '<span class="empty">/api/crash-windows returned nothing</span>';
    return;
  }
  t.innerHTML = rows.map((w) => {
    const eff = String(w.effect_possible).toUpperCase();
    const key = (eff === 'YES' || eff === 'TRUE') ? 'YES'
      : ((eff === 'NO' || eff === 'FALSE') ? 'NO' : 'UNKNOWN');
    return (
      `<article class="cw cw-${key}">` +
        `<div class="cw-id">${esc(w.id)}</div>` +
        `<div class="cw-body">` +
          `<div class="cw-top">` +
            `<h4 class="cw-when">${esc(w.when)}</h4>` +
            `<span class="eff eff-${key}">EFFECT POSSIBLE: ${esc(key)}</span>` +
          `</div>` +
          `<div class="cw-row"><span>RECOVERY</span><p>${esc(w.recovery)}</p></div>` +
          `<div class="cw-row"><span>GUARANTEE</span><p class="cw-guar">${esc(w.guarantee)}</p></div>` +
          `<div class="cw-row"><span>TEST</span><p class="testref">${esc(w.covered_by_test)}</p></div>` +
        `</div>` +
      `</article>`
    );
  }).join('');
}

// ────────────────────────────────────────────────────────────────────── drawer ──

async function openTask(id) {
  S.drawerTask = id;
  const d = $('#drawer');
  d.hidden = false;
  $('#d-body').innerHTML = '<div class="d-sec"><span class="empty">loading…</span></div>';
  const r = await get('/api/tasks/' + encodeURIComponent(id), null);
  if (!r || !r.task) { $('#d-body').innerHTML = '<div class="d-sec"><span class="empty">not found</span></div>'; return; }
  const t = r.task;
  $('#d-key').textContent = t.dedupe_key || '—';
  $('#d-id').textContent = t.id;

  const f = (k, v, cls) => `<div class="d-f"><span>${esc(k)}</span><b class="${cls || ''}">${esc(v)}</b></div>`;

  const attempts = (r.attempts || []).map((a) => (
    `<div class="att a-${esc(a.attempt_state)}">` +
      `<div class="att-top"><b>${esc(a.step_name)} · ${esc(a.operation)}</b>` +
      `<span class="att-state">${esc(a.attempt_state)}</span></div>` +
      `<div class="att-kv">` +
        `<span>AMOUNT</span><b>${esc(money(a.amount_cents))} ${esc(a.currency || '')}</b>` +
        `<span>IDEMPOTENCY</span><b class="key">${esc(a.idempotency_key)}</b>` +
        `<span>FINGERPRINT</span><b>${esc(String(a.request_fingerprint || '').slice(0, 32))}…</b>` +
        `<span>PROVIDER REF</span><b>${esc(a.provider_ref || '—')}</b>` +
        `<span>EPOCH</span><b>${esc(a.lease_epoch)}</b>` +
        `<span>PREPARED</span><b>${esc(hhmmss(a.prepared_at))}</b>` +
        `<span>SETTLED</span><b>${esc(a.settled_at ? hhmmss(a.settled_at) : '—')}</b>` +
      `</div>` +
    `</div>`
  )).join('') || '<span class="empty">no receipts — nothing irreversible was ever authorized</span>';

  const evs = (r.events || []).slice().reverse().map((e) => (
    `<div class="d-ev">` +
      `<span class="sq">${esc(e.seq)}</span>` +
      `<span><span class="ty">${esc(e.event_type)}</span>` +
        (e.from_state || e.to_state ? `<span class="tr"> ${esc(e.from_state || '·')} &rarr; ${esc(e.to_state || '·')}</span>` : '') +
      `</span>` +
      `<span class="ep">${e.lease_epoch != null ? 'e' + esc(e.lease_epoch) : ''}</span>` +
    `</div>`
  )).join('');

  $('#d-body').innerHTML =
    `<div class="d-sec"><h3>EXECUTION STATE</h3><div class="d-fields">` +
      f('STATE', t.state) + f('SHARD', t.shard) + f('ATTEMPT', t.attempt + ' / ' + t.max_attempts) +
      f('LEASE EPOCH', t.lease_epoch) + f('LEASE OWNER', shortId(t.lease_owner)) +
      f('POLICY', (t.policy_id || '—') + ' v' + (t.policy_version != null ? t.policy_version : '—')) +
      f('UPDATED', hhmmss(t.updated_at)) + f('AVAILABLE AT', hhmmss(t.available_at)) +
    `</div>${t.last_error ? `<pre class="json" style="margin-top:8px;color:var(--alarm)">${esc(t.last_error)}</pre>` : ''}</div>` +
    `<div class="d-sec"><h3>RECEIPT CHAIN</h3>${attempts}</div>` +
    `<div class="d-sec"><h3>PAYLOAD</h3><pre class="json">${esc(JSON.stringify(t.payload, null, 2))}</pre></div>` +
    (t.result ? `<div class="d-sec"><h3>RESULT</h3><pre class="json">${esc(JSON.stringify(t.result, null, 2))}</pre></div>` : '') +
    `<div class="d-sec"><h3>EVENT REPLAY (${(r.events || []).length})</h3>${evs}</div>`;
}

function closeDrawer(fromHash) {
  $('#drawer').hidden = true;
  S.drawerTask = null;
  if (!fromHash && location.hash.slice(1).startsWith('task/')) location.hash = S.view;
}

// ─────────────────────────────────────────────────────────────────────── views ──

const VIEWS = ['ops', 'ledger', 'memory', 'approvals', 'spec'];

/** The tab lives in the hash so a reload keeps its place and a demo can deep-link
 *  straight to, say, #spec without four seconds of clicking on camera. `#task/<uuid>`
 *  opens the operations view with that task's receipt chain already open, which is the
 *  link you paste into an incident channel. */
function applyHash(h) {
  const raw = (h || '').replace(/^#/, '');
  if (raw.startsWith('task/')) {
    const id = raw.slice(5);
    setView('ops', true);
    if (id) openTask(id);
    return;
  }
  closeDrawer(true);
  setView(raw || 'ops', true);
}

function setView(v, fromHash) {
  if (!VIEWS.includes(v)) v = 'ops';
  S.view = v;
  $$('.tab').forEach((t) => t.classList.toggle('is-on', t.dataset.view === v));
  $$('.view').forEach((s) => s.classList.toggle('is-on', s.dataset.view === v));
  if (!fromHash && location.hash.slice(1) !== v) location.hash = v;
  tickView();
}

/** Only the visible tab's data is polled. The always-on panels (grid, journal, rail,
 *  money shot) are never gated on it. */
async function tickView() {
  const v = S.view;
  if (v === 'ledger') renderLedger(await get('/api/provider/ledger?limit=100', []));
  else if (v === 'memory') {
    const inc = $('#mem-inadmissible').checked;
    renderMemories(await get('/api/memories?limit=100&include_inadmissible=' + inc, []));
    // Land on this tab with a live ANN result already on screen. An empty recall panel
    // with an unlit index lamp says nothing; the populated one is the argument.
    if (!S.lastRecall) runRecall(false);
  } else if (v === 'approvals') renderApprovals(await get('/api/approvals', []));
  else if (v === 'spec') {
    if (!S.crashWindows) S.crashWindows = await get('/api/crash-windows', []);
    renderCrashWindows(S.crashWindows);
  }
}

// ──────────────────────────────────────────────────────────────────── polling ──
//
// One loop, not three timers. Every cycle issues three GETs (mission, tasks, events) and,
// at most every AUX_MIN_MS, five more. The interval between cycles is chosen by adapt()
// after each pass. On a hidden tab there is no timer scheduled at all — the old build
// kept firing setInterval and returned early on document.hidden, which costs nothing in
// requests but leaves a page that has to be told to stop, twice, in two places.

/** The five slower reads. Split out because they are 5 of the 8 requests per cycle and
 *  none of them changes at 1Hz: a heartbeat is 5s, a health check is a database ping,
 *  and approvals wait on a human. */
async function aux() {
  const [health, agents, pstats, unsettled, approvals] = await Promise.all([
    get('/api/health', null),
    get('/api/agents', null),
    get('/api/provider/stats', null),
    get('/api/receipts/unsettled', []),
    get('/api/approvals', []),
  ]);
  renderHealth(health);
  if (agents) renderAgents(agents);
  if (pstats) renderProviderStats(pstats);
  renderUnsettled(unsettled);

  const badge = $('#appr-badge');
  badge.hidden = !approvals.length;
  badge.textContent = String(approvals.length);
  // The approvals view is fed from this same response rather than fetching it again,
  // which is what the previous build did — two identical GETs every four seconds.
  if (S.view === 'approvals') renderApprovals(approvals);
  else if (S.view !== 'ops') tickView();
}

async function tick() {
  if (P.busy || document.hidden) return;
  P.busy = true;
  P.lastTick = Date.now();
  const before = apiFailures;
  try {
    S.changed = false;
    const [mission, tasks, events] = await Promise.all([
      get('/api/mission', null),
      get('/api/tasks?limit=300', null),
      get('/api/events?limit=60', null),
    ]);
    if (mission) renderMission(mission);
    if (tasks) renderTasks(tasks);
    if (events) renderEvents(events);

    if (Date.now() - P.lastAux >= AUX_MIN_MS) {
      P.lastAux = Date.now();
      await aux();
    }

    const poll = $('.hx[data-k="poll"]');
    const ok = apiFailures === before;
    poll.classList.toggle('is-ok', ok);
    poll.classList.toggle('is-bad', !ok);
    S.booted = true;
    adapt();
  } finally {
    P.busy = false;
  }
}

/** Pick the next interval. Two inputs only: did anything change, and is a lease held.
 *  A change resets to 1s outright. A held lease does not reset anything — it only widens
 *  the stillness the ladder will tolerate before stepping out, because a worker mid-refund
 *  goes quiet for a second or two and the next second is when the crash lands. Every path
 *  through here still terminates in backoff; there is no input that pins the fast rung
 *  open indefinitely, which is the property that keeps an abandoned tab inside the free
 *  tier. */
function adapt() {
  const live = S.tasks.some((t) => LIVE_STATES.has(t.state));
  if (S.changed) {
    P.rung = 0;
    P.idle = 0;
  } else if (++P.idle >= (live ? IDLE_BEFORE_BACKOFF_LIVE : IDLE_BEFORE_BACKOFF)) {
    P.idle = 0;
    P.rung = Math.min(P.rung + 1, LADDER.length - 1);
  }
  paintPoll();
}

function paintPoll() {
  const rate = $('#poll-rate');
  const ms = LADDER[P.rung];
  rate.textContent = document.hidden ? 'PAUSED' : (ms >= 1000 ? (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's' : ms + 'ms');
  rate.classList.toggle('is-paused', document.hidden);
  $('#req-count').textContent = P.reqs.toLocaleString('en-US') + ' REQ';
}

function schedule() {
  clearTimeout(P.timer);
  P.timer = null;
  if (document.hidden) return;
  P.timer = setTimeout(run, LADDER[P.rung]);
}

async function run() {
  await tick();
  schedule();
}

/** Something happened that means a human is present: a click, a keystroke, the tab coming
 *  back to the front. Drop to the fast rung, and if the loop is parked on a long timer,
 *  do not make them wait out the remainder of a minute. */
function nudge() {
  if (document.hidden) return;
  const wasSlow = P.rung > 1;
  P.rung = 0;
  P.idle = 0;
  if (wasSlow && Date.now() - P.lastTick > 900) run();
  else { paintPoll(); schedule(); }
}

// ──────────────────────────────────────────────────────────────────── wiring ──

function wire() {
  $('#tabs').addEventListener('click', (e) => {
    const t = e.target.closest('.tab');
    if (t) setView(t.dataset.view);
  });

  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, select, textarea')) return;
    const map = { '1': 'ops', '2': 'ledger', '3': 'memory', '4': 'approvals', '5': 'spec' };
    if (map[e.key]) setView(map[e.key]);
    if (e.key === 'Escape') closeDrawer();
  });

  $('#taskgrid').addEventListener('click', (e) => {
    const c = e.target.closest('.cell');
    if (c) location.hash = 'task/' + c.dataset.id;
  });
  $('#d-close').addEventListener('click', () => closeDrawer());

  $('#rewind-ctl').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-secs]');
    if (!b) return;
    $$('#rewind-ctl .btn').forEach((x) => x.classList.toggle('is-on', x === b));
    doRewind(Number(b.dataset.secs));
  });

  $('#recall-form').addEventListener('submit', (e) => { e.preventDefault(); runRecall(false); });

  $('#mem-inadmissible').addEventListener('change', tickView);

  // Quarantine, then immediately re-run the last recall. Watching a poisoned memory
  // fall out of the candidate set in the same breath is the whole point of folding
  // admissibility into a vector-index prefix column — so do not make the operator
  // press RECALL again to see it.
  $('#mem-tbl').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-quarantine]');
    if (!b) return;
    b.disabled = true;
    try {
      await post(`/api/memories/${encodeURIComponent(b.dataset.quarantine)}/quarantine`,
        { reason: 'quarantined by operator in Mission Control', by: 'human:ops@acme.example' });
      toast('quarantined · ' + shortId(b.dataset.quarantine) + ' — re-running recall');
      await tickView();
      if (S.lastRecall) await runRecall(true);
    } catch (err) {
      toast('quarantine failed: ' + err.message, true);
      b.disabled = false;
    }
  });

  $('#approvals').addEventListener('click', (e) => {
    const b = e.target.closest('[data-decide]');
    if (!b) return;
    decide(b.closest('.appr'), b.dataset.decide === '1');
  });

  $('#btn-seed').addEventListener('click', async () => {
    const b = $('#btn-seed'); b.disabled = true;
    try { const r = await post('/api/demo/seed', { tasks: 30, reset: true }); toast('seeded ' + r.tasks + ' tasks'); resetClientState(); }
    catch (e) { toast('seed failed: ' + e.message, true); }
    finally { b.disabled = false; nudge(); run(); }
  });

  $('#btn-reset').addEventListener('click', async () => {
    const b = $('#btn-reset'); b.disabled = true;
    try { await post('/api/demo/reset', {}); toast('demo state cleared'); resetClientState(); }
    catch (e) { toast('reset failed: ' + e.message, true); }
    finally { b.disabled = false; nudge(); run(); }
  });

  $('#btn-run').addEventListener('click', () => runWorker('drain'));
  $('#btn-kill').addEventListener('click', () => runWorker('chaos'));
}

/** POST /api/demo/run-worker.
 *
 *  `drain` starts a worker that claims and settles until the queue is empty. `chaos`
 *  starts one that dies AFTER the provider has committed a refund and BEFORE AXIOM has
 *  recorded it — crash window W4, the only window where an effect can exist that the
 *  ledger does not know about. The button says KILL A WORKER because that is what it
 *  looks like from the grid; what the API actually does is start a worker that will kill
 *  itself at the worst possible instant, which is a smaller and more honest power than
 *  reaching out and SIGKILLing an arbitrary process.
 *
 *  Both are one request. The recovery that follows is watched, not driven — nothing in
 *  this page tells the surviving worker to take the lease over. */
async function runWorker(mode) {
  const b = $(mode === 'chaos' ? '#btn-kill' : '#btn-run');
  b.disabled = true;
  try {
    const r = await post('/api/demo/run-worker', { mode: mode, seconds: 60 });
    const where = r.backend === 'lambda' ? 'lambda ' + (r.function || '') : 'local pid ' + r.pid;
    if (mode === 'chaos') {
      S.chaosAt = Date.now();
      chaosBar('armed', 'CHAOS WORKER DISPATCHED',
        'it dies mid-refund (W4) · watch for lease_epoch to advance');
      toast('chaos worker dispatched · ' + where);
    } else {
      toast('worker dispatched · ' + where);
    }
    // Whatever rung the ladder had backed off to, a mission is now running.
    P.rung = 0; P.idle = 0;
    run();
  } catch (e) {
    toast((mode === 'chaos' ? 'chaos' : 'run') + ' failed: ' + e.message, true);
  } finally {
    // Long enough that a double-click cannot start two workers by accident, short enough
    // that a second crash is one press away when the first one is being explained.
    setTimeout(() => { b.disabled = false; }, 2500);
  }
}

function resetClientState() {
  S.epochs.clear(); S.states.clear(); S.fenced.clear(); S.fenceFrom.clear(); S.fenceCount = 0;
  S.seenEvents.clear(); S.lastRecall = null; S.chaosAt = 0; S.missionSig = '';
  $('#taskgrid').innerHTML = ''; $('#events').innerHTML = '';
  $('#fence-count').textContent = '0';
  chaosBar(null);
}

function boot() {
  renderLegend();
  wire();
  window.addEventListener('hashchange', () => applyHash(location.hash));
  applyHash(location.hash);
  setInterval(() => { $('#clock').textContent = new Date().toTimeString().slice(0, 8); }, 1000);
  $('#clock').textContent = new Date().toTimeString().slice(0, 8);

  // Local-only timers. paintAgents recomputes heartbeat ages from the last fetch, so the
  // worker panel keeps counting up between polls without asking the server anything —
  // which is what lets the poll back off to 60s without the pool looking frozen.
  setInterval(paintAgents, 1000);

  // The whole point: no timer exists while the tab is hidden. A background tab makes
  // exactly zero requests, and coming back to the front starts at the fast rung.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearTimeout(P.timer); P.timer = null; paintPoll(); }
    else { P.rung = 0; P.idle = 0; run(); }
  });
  // Any evidence of a human resets the ladder. Deliberately not mousemove: a still cursor
  // over a page nobody is reading would pin the poll at 1s forever.
  document.addEventListener('click', nudge, true);
  document.addEventListener('keydown', nudge, true);

  paintPoll();
  tickView();
  run();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
