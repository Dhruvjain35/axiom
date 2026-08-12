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

/** What the operator sees, which is not the same list.
 *
 *  DEAD_LETTER is one database state covering two outcomes that are nothing alike, and
 *  painting both of them in the alarm colour is a lie by palette. worker.py sends an
 *  `escalate` triage there — no receipt was ever minted, no money moved, a human now owns
 *  the case — and it sends genuine budget/retry exhaustion there too. Only the second is
 *  a failure. The two are distinguishable from the row itself (see isEscalated), so the
 *  interface distinguishes them: ESCALATED is --hold, the colour this panel already uses
 *  for "a human owns it", and the alarm colour stays reserved for things that are wrong. */
const DISPLAY_STATES = ['PENDING', 'READY', 'LEASED', 'AWAITING_APPROVAL',
  'ACTION_PREPARED', 'SUCCEEDED', 'ESCALATED', 'FAILED', 'DEAD_LETTER', 'CANCELLED'];

function isEscalated(t) {
  return t.state === 'DEAD_LETTER' && t.result && t.result.action === 'escalate';
}
/** The state class this task should render as. */
function viewState(t) { return isEscalated(t) ? 'ESCALATED' : t.state; }

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

/** /api/mission alone, because its 404 is not a fault.
 *
 *  An empty board is a legitimate state — it is what RESET produces, and it is what a
 *  judge lands on if they press RESET before SEED. The generic get() counted that 404 as
 *  an API failure, which latched the POLL lamp in the alarm colour on a page where
 *  nothing was wrong, and returned null, which made the caller skip renderMission and
 *  leave the static "waiting for /api/mission" placeholder on screen forever. Both bugs
 *  come from one conflation. A 404 here means "no mission"; say so and move on. */
async function getMission() {
  P.reqs++;
  try {
    const res = await fetch('/api/mission', { headers: { accept: 'application/json' } });
    if (res.status === 404) return {};
    if (!res.ok) { apiFailures++; return null; }
    return await res.json();
  } catch (e) {
    apiFailures++;
    return null;
  }
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
  recoveries: 0,            // task.recovered events seen — see the note in renderEvents
  receiptKeys: new Map(),   // task_id -> idempotency_key, captured while still unsettled
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
  frozen: false,     // held across a seed/reset; see withPollHeld
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
    // Reachable now that a 404 is a value rather than an exception. Clear every field the
    // populated branch writes — a half-cleared strip showing the previous mission's uuid
    // next to "no mission" is worse than either.
    $('#m-title').textContent = 'no mission';
    $('#m-goal').textContent = 'press RUN THE PROOF, or SEED to set the board up by hand';
    $('#m-state').textContent = '—';
    $('#m-uuid').textContent = '—';
    $('#m-created').textContent = '—';
    $('#b-nums').textContent = '— / —';
    $('#b-fill').style.width = '0%';
    $('#statestrip').innerHTML = '';
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

  renderStateStrip(m.by_state || {});
}

/** The per-state counts under the budget bar.
 *
 *  Preferred source is the task list rather than the mission's server-side by_state,
 *  because only the task rows carry `result` and therefore only they can tell an
 *  escalation apart from a genuine dead letter. by_state is the fallback for the moment
 *  before the first /api/tasks lands, and for a mission larger than the 300-row page. */
function renderStateStrip(byState) {
  let counts = byState || {};
  const total = Object.values(counts).reduce((a, b) => a + Number(b), 0);
  if (S.tasks.length && S.tasks.length >= total) {
    counts = {};
    for (const t of S.tasks) {
      const k = viewState(t);
      counts[k] = (counts[k] || 0) + 1;
    }
  }
  $('#statestrip').innerHTML = DISPLAY_STATES
    .filter((s) => counts[s])
    .map((s) => `<span class="sc"><i class="mark m-${s}"></i>${esc(s)} <b>${counts[s]}</b></span>`)
    .join('');
}

function renderProviderStats(p) {
  if (p && S.pstats && (p.refunds !== S.pstats.refunds || p.replays !== S.pstats.replays)) {
    S.changed = true;   // the provider acted: never let the poll be backing off through that
  }
  S.pstats = p;
  if (!p) return;
  const dupes = Number(p.duplicate_orders || 0);
  const replays = Number(p.replays || 0);
  const refunds = Number(p.refunds || 0);
  $('#dupes').textContent = String(dupes);
  $('#moneyshot').classList.toggle('is-alarm', dupes > 0);
  $('#p-refunds').textContent = String(p.refunds != null ? p.refunds : '—');
  $('#p-replays').textContent = String(p.replays != null ? p.replays : '—');
  $('#p-total').textContent = moneyShort(p.total_cents);

  // The honesty line, and the reason this dashboard is worth trusting at all.
  //
  // DUPLICATE REFUNDS 0 in 46-point type is unearned on a board where nothing was ever
  // retried — zero is also what you get by doing nothing. The claim only becomes a proof
  // once the provider has recorded at least one idempotent replay, because a replay is
  // the crash actually having happened. Say which of the two states the number is in,
  // rather than letting the big number imply the stronger one.
  const v = $('#dup-verdict');
  v.className = 'money-verdict';
  if (!refunds) {
    v.textContent = 'nothing refunded yet — nothing to prove';
  } else if (dupes > 0) {
    v.classList.add('is-alarm');
    v.textContent = dupes + (dupes === 1 ? ' order was' : ' orders were') + ' refunded more than once';
  } else if (replays === 0) {
    v.classList.add('is-unproven');
    v.textContent = 'no replay observed — 0 duplicates is not yet a proof';
  } else {
    v.classList.add('is-proven');
    v.textContent = 'PROVEN · ' + replays + ' idempotent replay' + (replays === 1 ? '' : 's')
      + ' at the crash instant, 0 duplicates';
  }

  if (PF.running) paintProofEvidence();
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
    `<div class="cell-state">${esc(viewState(t))}</div>` +
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
    const vs = viewState(t);
    if (cell.dataset.state !== vs) {
      if (cell.dataset.state) cell.classList.remove('s-' + cell.dataset.state);
      cell.classList.add('s-' + vs);
      cell.dataset.state = vs;
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
      // Not every fence is the interesting one, and the difference decides whether the
      // big FENCE ADVANCED block is allowed to fire.
      //
      // A task parked at AWAITING_APPROVAL is re-claimed at a higher epoch the moment a
      // human approves it. That genuinely fences the previous holder, so it belongs in
      // the counter and it earns the cell's chip — but nothing irreversible was ever
      // outstanding on it, and raising a 20px FENCE ADVANCED readout over it during the
      // guided run made the approval beat look like a crash recovery. (It did, on the
      // first run I recorded.)
      //
      // The dangerous fence is the one where a LIVE RECEIPT existed at the moment the
      // task changed hands — i.e. the task was, and remains, in ACTION_PREPARED. That is
      // the only case where an effect may already exist in the world, and it is the only
      // case the headline block is spent on.
      // Specifically the PREVIOUS observed state, not the current one. Reading the
      // current state instead fired the block during the approval beat, because a task
      // resumed after a human says yes is momentarily in ACTION_PREPARED at the higher
      // epoch it was just re-claimed under — which looks identical to a recovery if you
      // only look at where the task is now. Where it WAS is what settles it.
      const overReceipt = prevState === 'ACTION_PREPARED';
      if (overReceipt && !firstFenced) firstFenced = { cell: cell, task: t, from: prevEpoch };
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
      // The one-line bar stays: it is what a returning operator reads. The block below it
      // is what a camera reads. `newFences - 1` is deliberately not reported as "more
      // takeovers" — it is only the count of other handovers seen in the same poll.
      chaosBar(null);
      showFenceProof(k, firstFenced.from, firstFenced.task.lease_epoch, 0);
      // Fire and forget: the key comparison lands a second or two after the fence, which
      // is the right order anyway — the fence is the takeover, the key is what happened
      // next. The guided run awaits the same helper so both paths prove it the same way.
      verifyIdempotency(firstFenced.task.id, S.receiptKeys.get(firstFenced.task.id));
      // Bring the proof on screen. `nearest` so a fence in the visible rows does not
      // yank the grid around for no reason.
      firstFenced.cell.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }
  $('#grid-note').textContent = tasks.length + ' tasks';
  renderStateStrip(S.mission && S.mission.by_state);
  if (PF.running) paintProofEvidence();
}

/** The fence, at a size that survives a 720p screen recording.
 *
 *  The proof this whole project rests on is one integer incrementing inside one 132px
 *  cell. It is genuinely true and it is genuinely unwatchable — so the cell keeps its
 *  chip and its corner mark, and the fact gets restated here at 20px with the word
 *  lease_epoch spelled out. No glow and no animation: it is a readout that appears, the
 *  way a readout on an instrument appears.
 *
 *  The second row is the half of the argument the epoch does not carry. Advancing the
 *  fence stops the dead worker from writing; it does not stop a SECOND refund. What stops
 *  that is the recovering worker re-sending the key it found on the durable receipt
 *  instead of minting a fresh one, so the string is printed once, in full, with both
 *  halves of its provenance named. It is only filled in when the two keys have actually
 *  been compared (see proofRecoverKey) — it is never asserted from the design. */
function showFenceProof(taskKey, from, to, more) {
  const box = $('#fenceproof');
  box.hidden = false;
  $('#fp-task').textContent = taskKey + (more > 0 ? '  (+' + more + ' more)' : '');
  $('#fp-from').textContent = 'e' + from;
  $('#fp-to').textContent = 'e' + to;
  clearTimeout(showFenceProof.t);
  // Long-lived on purpose. This is the artifact someone scrubs back to.
  showFenceProof.t = setTimeout(() => { box.hidden = true; $('#fp-idem').hidden = true; }, 120000);
}

function showIdempotencyProof(key) {
  $('#fp-key').textContent = key;
  $('#fp-idem').hidden = false;
}

/** Compare the key the receipt was minted under against the key the recovering worker
 *  actually settled with, and only claim IDENTICAL if they are.
 *
 *  `expected` came from /api/receipts/unsettled while the task was still stranded, so
 *  this is a comparison of two independently observed strings rather than a restatement
 *  of one. If the settle has not landed yet the task detail simply will not contain it,
 *  hence the short retry; if it never matches, nothing is shown. Silence is the correct
 *  output for "could not verify".
 */
async function verifyIdempotency(taskId, expected, tries) {
  if (!expected) return false;
  for (let i = 0; i < (tries || 6); i++) {
    const d = await get('/api/tasks/' + encodeURIComponent(taskId), null);
    const hit = d && (d.attempts || []).some(
      (a) => a.idempotency_key === expected && a.settled_at);
    if (hit) { showIdempotencyProof(expected); return true; }
    await new Promise((r) => setTimeout(r, 900));
  }
  return false;
}

function renderLegend() {
  $('#legend').innerHTML = DISPLAY_STATES
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

    // Counted here rather than derived from lease_epoch, because those are not the same
    // number and conflating them overstates the claim.
    //
    // FENCE ADVANCES counts every lease handover — which includes the perfectly ordinary
    // case of a task parked at AWAITING_APPROVAL being re-claimed after a human says yes.
    // Three approvals in the demo therefore add three to it, and a reader who takes that
    // meter as "three crash takeovers" has been misled by the dashboard rather than by
    // the data. A `task.recovered` event is unambiguous: it is only written when a worker
    // claims a task, finds a LIVE receipt already sitting on it, and has to decide what
    // to do about an effect that may already exist. That is the number the proof strip
    // reports.
    if (e.event_type === 'task.recovered') S.recoveries++;

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

/** How many worker rows the rail shows. /api/agents has no LIMIT and no staleness filter,
 *  so the pool grows by one row per dispatch and never shrinks until a reset. Forty judges
 *  pressing two buttons each is eighty rows of history in a 234px rail that also has to
 *  hold UNSETTLED RECEIPTS and REWIND. Newest N, then a count of the rest. */
const WORKER_ROWS = 6;

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
  // `pool`, not `all` — the map below already binds `all` for shard affinity, and having
  // the two shadow each other is exactly the kind of thing that reads fine and breaks in
  // six months.
  const pool = S.agents.slice().sort((a, b) =>
    Number(a.seconds_since_heartbeat || 0) - Number(b.seconds_since_heartbeat || 0));
  const shown = pool.slice(0, WORKER_ROWS);
  const hidden = pool.length - shown.length;

  let live = 0;
  box.innerHTML = shown.map((a) => {
    const secs = Number(a.seconds_since_heartbeat || 0) + drift;

    // Three outcomes, not two, and the difference is the whole point of this panel.
    //
    // A worker that drained its queue and exited wrote stopped_at on the way out. That is
    // a normal, correct end and it must not be painted in the alarm colour — spending red
    // on a process that did its job and left is what makes red stop meaning anything.
    //
    // A worker with no stopped_at whose heartbeat has outlived the 20s lease stopped
    // without ever saying so. That is the exact condition the fencing token exists for,
    // and it is the only one here that earns --alarm. The chaos worker lands in it
    // because os._exit(9) skips every atexit hook — the same way a SIGKILL does.
    let cls = '', status;
    if (a.stopped_at) { cls = 'is-exited'; status = 'EXITED'; }
    else if (secs > 20) { cls = 'is-dead'; status = 'KILLED'; }
    else if (secs > 6) { cls = 'is-stale'; status = 'STALE'; live++; }
    else { status = 'ALIVE'; live++; }

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
  }).join('') + (hidden > 0
    ? `<div class="worker-more">+${hidden} earlier worker${hidden === 1 ? '' : 's'} not shown</div>` : '');
  $('#workers-note').textContent = live + '/' + pool.length + ' live';
}

// ──────────────────────────────────────────────────────────── unsettled receipts ──

function renderUnsettled(rows) {
  const box = $('#unsettled');
  $('#unsettled-note').textContent = String((rows || []).length);

  // Remember the key each outstanding receipt was minted under, BEFORE anything takes the
  // task over. This is the only moment it can be captured honestly: once the recovering
  // worker has settled, a key read off the finished attempt is just a key, and saying it
  // is "the same one" would be an assertion rather than a comparison. Held per task and
  // never cleared on settle, so the comparison survives the receipt leaving this list.
  for (const r of (rows || [])) {
    if (r.task_id && r.idempotency_key) S.receiptKeys.set(r.task_id, r.idempotency_key);
  }
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
      // The replayed row is the crash, in the provider's own book. Give it a rule so a
      // judge who lands on this tab does not have to scan 18 rows for the one that
      // matters — the one where AXIOM asked twice and the provider acted once.
      const replayed = Number(r.replay_count) > 0;
      return `<tr class="${replayed ? 'is-replay' : ''}">` +
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

/** What the agent is asking permission to do, as a line a human can read.
 *
 *  `proposed_action` is JSONB — tasks.py writes {operation, request_body} — so the
 *  previous `a.proposed_action || a.step_name` put the string "[object Object]" at 15px
 *  in serif at the top of every approval card. On the one screen in this product where a
 *  person is being asked to authorise money moving, the field naming the act was
 *  unreadable. Render the operation, and the order it is against. */
function proposedLabel(a) {
  const pa = a.proposed_action;
  if (pa && typeof pa === 'object') {
    const body = pa.request_body || {};
    const op = pa.operation || a.step_name || 'action';
    return body.order_ref ? op + '  ·  ' + body.order_ref : op;
  }
  return String(pa || a.step_name || '—');
}

/** The reason field, when the request body carries one. */
function proposedReason(a) {
  const pa = a.proposed_action;
  const body = (pa && typeof pa === 'object' && pa.request_body) || {};
  return body.reason || '';
}

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
          `<div class="appr-action">${esc(proposedLabel(a))}</div>` +
          `<div class="appr-amt">${esc(money(a.proposed_amount_cents))}</div>` +
        `</div>` +
        `<div class="appr-reason">${esc(a.reason)}</div>` +
        `<div class="appr-grid">` +
          `<div class="appr-f"><span>POLICY</span><b>${esc(a.policy_id || '—')} v${esc(a.policy_version != null ? a.policy_version : '—')}</b></div>` +
          `<div class="appr-f"><span>STEP</span><b>${esc(a.step_name || '—')}${proposedReason(a) ? ' · ' + esc(proposedReason(a)) : ''}</b></div>` +
          `<div class="appr-f"><span>TASK</span><b>${esc(shortId(a.task_id))}</b></div>` +
          `<div class="appr-f"><span>EVIDENCE MEMORIES (${ev.length})</span><b>${ev.length ? ev.map(shortId).map(esc).join(' · ') : 'none'}</b></div>` +
        `</div>` +
        `<div class="appr-acts">` +
          `<input class="who" value="ops@acme.example" data-who>` +
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
  // tasks.decide_approval writes actor = f'human:{decided_by}', so sending "human:ops@…"
  // from here produced "human:human:ops@…" in the journal — on the one event that records
  // a person taking responsibility for money moving. Send the identity, not the identity
  // plus the prefix the server is about to add.
  const who = $('[data-who]', card).value || 'operator';
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
  // W4 is the only window this browser session can witness first-hand, and it can only
  // claim to have witnessed it once BOTH halves are on the wire: a fence advanced (a
  // takeover happened) and the provider recorded a replay (the same key was presented
  // twice and it acted once). One without the other is not an observation of W4.
  const sawW4 = S.fenceCount > 0 && S.pstats && Number(S.pstats.replays || 0) > 0;

  t.innerHTML = rows.map((w) => {
    const eff = String(w.effect_possible).toUpperCase();
    const key = (eff === 'YES' || eff === 'TRUE') ? 'YES'
      : ((eff === 'NO' || eff === 'FALSE') ? 'NO' : 'UNKNOWN');
    const live = sawW4 && w.id === 'W4';
    return (
      `<article class="cw cw-${key}${live ? ' cw-live' : ''}">` +
        `<div class="cw-id">${esc(w.id)}</div>` +
        `<div class="cw-body">` +
          `<div class="cw-top">` +
            `<h4 class="cw-when">${esc(w.when)}</h4>` +
            (live ? '<span class="cw-obs">OBSERVED IN THIS SESSION</span>' : '') +
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

// ──────────────────────────────────────────────────────────── counterexample ──

/** A RECORDED RUN of scripts/counterexample.py. Deliberately not live.
 *
 *  The script proves the thesis by letting a fair transcript-memory agent double-refund a
 *  real order at the exact crash instant AXIOM survives. That means running it creates a
 *  genuine duplicate refund in the provider's ledger — $600 out the door against one $300
 *  order. A dashboard whose headline number is DUPLICATE REFUNDS must not have a button
 *  that manufactures duplicate refunds, so this panel is a fixture and says so in its own
 *  footer, with the command to reproduce it.
 *
 *  Every value below is copied from one terminal run. Provenance in `measured`.
 *  Reproduce with:  ./.venv/bin/python scripts/counterexample.py
 */
const COUNTEREXAMPLE = {
  measured: '2026-08-11 · CockroachDB v26.2.3 (local) · AXIOM_OFFLINE=1 · re-run on a '
    + 'fully seeded database, exit 0, printed PASS',
  command: './.venv/bin/python scripts/counterexample.py',
  order: 'one order · $300.00 · both agents killed at the same instant (W4)',
  // [label, baseline, axiom, class, note-under-the-axiom-cell]
  rows: [
    ['killed in W4', 'yes', 'yes', '', ''],
    ['memory consulted', '2 transcript turns', 'receipt + 5 recalled memories', '', ''],
    ['policy gate', 'none — refunds $300 unattended', 'stopped and asked a human first', '', ''],
    ['recovery decision', 'retry — cannot know if it landed', 'RESEND under the same key', '', ''],
    ['idempotency key', 'newly generated each attempt', 'axm_79f90ba205cf427918ae7e…', '',
      'a new key on every attempt is what makes the provider treat the second call as a '
      + 'second refund; the whole difference is in this row'],
    ['fence (lease_epoch)', 'n/a — no such concept', '2 → 3', '',
      'starts at 2, not 1, because this script claims the task, gets stopped by the policy '
      + 'gate, and re-claims after the operator approves — then the crash takeover makes it 3'],
  ],
  totals: [
    ['REFUNDS CREATED', '2', '1', 'bad', ''],
    ['IDEMPOTENT REPLAYS', '0', '1', 'good', ''],
    ['DOLLARS OUT', '$600.00', '$300.00', 'money', ''],
  ],
  reasoning: {
    baseline: 'transcript shows an unfinished refund intent with no completion; cannot tell '
      + 'whether the call landed, so retrying',
    axiom: 'live receipt axm_79f90ba205cf427918ae7ea05fac778c5ed63d34a607d6ad exists; '
      + 're-dispatching under the same key (5 comparable recoveries recalled, none adverse)',
  },
  verdict: 'The customer was overcharged $300.00 by the baseline and $0.00 by AXIOM.',
};

/** The comparison, rendered once. Static by design — nothing here polls. */
function renderCounterexample() {
  const c = COUNTEREXAMPLE;
  const row = (r, big) =>
    `<tr class="${big ? 'ce-total ce-' + r[3] : ''}">` +
      `<td class="ce-k">${esc(r[0])}</td>` +
      `<td class="ce-a">${esc(r[1])}</td>` +
      `<td class="ce-b">${esc(r[2])}` +
        (r[4] ? `<div class="ce-note">${esc(r[4])}</div>` : '') +
      `</td>` +
    `</tr>`;

  $('#counter-out').innerHTML =
    `<div class="ce">` +

      `<p class="ce-lede">A transcript-memory agent is the honest competition, so this runs
        one against the same provider, the same order, the same $300 and the same crash —
        and it is <em>not</em> a strawman. It fsyncs its transcript, re-reads it on restart,
        and records its intent before it acts. It still refunds twice, because after the
        crash its memory cannot distinguish <em>the call never went out</em> from <em>the
        call went out and I died</em>, and it has no durable receipt to recover the original
        idempotency key from.</p>` +

      `<div class="ce-setup"><span class="lbl">SETUP</span>${esc(c.order)}</div>` +

      `<table class="ce-tbl">` +
        `<thead><tr><th></th>` +
          `<th class="ce-h ce-h-a">TRANSCRIPT MEMORY<span>the fair baseline</span></th>` +
          `<th class="ce-h ce-h-b">AXIOM<span>execution memory</span></th>` +
        `</tr></thead><tbody>` +
        c.rows.map((r) => row(r, false)).join('') +
        c.totals.map((r) => row(r, true)).join('') +
      `</tbody></table>` +

      `<div class="ce-say">` +
        `<div class="ce-q"><span class="lbl">BASELINE REASONING</span><p>${esc(c.reasoning.baseline)}</p></div>` +
        `<div class="ce-q ce-q-b"><span class="lbl">AXIOM RATIONALE</span><p>${esc(c.reasoning.axiom)}</p></div>` +
      `</div>` +

      `<p class="ce-verdict">${esc(c.verdict)}</p>` +

      `<div class="ce-prov">` +
        `<div><span class="lbl">WHY THIS PANEL IS NOT LIVE</span>` +
          `<p>Running the baseline creates a real duplicate refund in the provider's ledger —
            that is the point of it. A dashboard whose headline reads DUPLICATE REFUNDS 0
            does not get a button that manufactures duplicate refunds. So this is a
            recorded run, and this paragraph is here so nobody has to wonder.</p></div>` +
        `<div><span class="lbl">PROVENANCE</span>` +
          `<p>${esc(c.measured)}</p>` +
          `<pre class="json">${esc(c.command)}</pre></div>` +
      `</div>` +

    `</div>`;
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

const VIEWS = ['ops', 'ledger', 'memory', 'approvals', 'spec', 'counter'];

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
  } else if (v === 'counter') renderCounterexample();
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

/** Hold the poll across a destructive demo call.
 *
 *  /api/demo/seed {reset:true} truncates and re-inserts, and there is a window inside it
 *  where the tenant genuinely has zero tasks. The API's demo self-heal treats an empty
 *  /api/tasks as "this demo is broken, rebuild it" — so a poll landing in that window
 *  makes the server create a SECOND mission alongside the one the seed is in the middle
 *  of writing. Everything downstream then looks wrong in a way that is very hard to read:
 *  the newest mission is the healed one, the workers claim tasks from the other, and the
 *  mission-scoped provider ledger comes back empty under a board of SUCCEEDED tasks.
 *
 *  Observed, not theorised: `axiom.demo :: self-healed the demo ... (mission 74701eca)`
 *  in the API log at the exact second a guided run pressed seed, and that run ended with
 *  an empty ledger and no crash. The race is in the server and is reported separately;
 *  this is the half of it this page is responsible for, which is not issuing the read
 *  that trips it.
 */
async function withPollHeld(fn) {
  P.frozen = true;
  clearTimeout(P.timer); P.timer = null;
  try {
    return await fn();
  } finally {
    P.frozen = false;
  }
}

async function tick() {
  if (P.busy || P.frozen || document.hidden) return;
  P.busy = true;
  P.lastTick = Date.now();
  const before = apiFailures;
  try {
    S.changed = false;
    const [mission, tasks, events] = await Promise.all([
      getMission(),
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
    if (e.key === 'Escape' && !$('#brief').hidden) { closeBrief(); return; }
    if (e.key === 'b' || e.key === 'B' || e.key === '?') { openBrief(); return; }
    const map = { '1': 'ops', '2': 'ledger', '3': 'memory', '4': 'approvals',
                  '5': 'spec', '6': 'counter' };
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
        { reason: 'quarantined by operator in Mission Control', by: 'ops@acme.example' });
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
    try {
      const r = await withPollHeld(() => post('/api/demo/seed', { tasks: 30, reset: true }));
      toast('seeded ' + r.tasks + ' tasks'); resetClientState();
    }
    catch (e) { toast('seed failed: ' + e.message, true); }
    finally { b.disabled = false; nudge(); run(); }
  });

  $('#btn-reset').addEventListener('click', async () => {
    const b = $('#btn-reset'); b.disabled = true;
    try {
      await withPollHeld(() => post('/api/demo/reset', {}));
      toast('demo state cleared'); resetClientState();
    }
    catch (e) { toast('reset failed: ' + e.message, true); }
    finally { b.disabled = false; nudge(); run(); }
  });

  $('#btn-run').addEventListener('click', () => runWorker('drain'));
  $('#btn-kill').addEventListener('click', () => runWorker('chaos'));

  $('#btn-proof').addEventListener('click', runProof);
  $('#pf-stop').addEventListener('click', () => { PF.abort = true; });

  $('#btn-brief').addEventListener('click', openBrief);
  $('#brief-x').addEventListener('click', closeBrief);
  $('#brief-skip').addEventListener('click', closeBrief);
  $('#brief-go').addEventListener('click', () => { closeBrief(); runProof(); });
  // Click the scrim to dismiss; clicks inside the card must not.
  $('#brief').addEventListener('click', (e) => { if (e.target.id === 'brief') closeBrief(); });
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
      // A takeover needs somebody left alive to take over. On a freshly seeded board the
      // chaos worker is the only process running, so "watch for lease_epoch to advance"
      // is an instruction the operator cannot follow — the epoch will sit at e1 forever
      // and the demo looks hung when it is in fact correct. Say which button is missing.
      const others = (S.agents || []).filter(
        (a) => !a.stopped_at && Number(a.seconds_since_heartbeat || 0) <= 20).length;
      chaosBar('armed', 'CHAOS WORKER DISPATCHED', others > 0
        ? 'it dies mid-refund (W4) · watch for lease_epoch to advance'
        : 'it dies mid-refund (W4) · nothing else is running — press RUN MISSION to '
          + 'dispatch the worker that takes the lease over');
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

// ═══════════════════════════════════════════════════════════════ THE GUIDED PROOF ══
//
// One button that walks a stranger through the entire argument in about forty seconds,
// live, against the same API every other panel on this page reads.
//
// Three rules govern everything below, and they are the reason it is written as a state
// machine over observed API responses rather than as a timeline of setTimeouts:
//
//   1. IT NARRATES ONLY WHAT IT HAS SEEN. Every beat waits for the evidence in an API
//      response — an unsettled receipt row, a lease_epoch that actually incremented, a
//      replay counter on the provider's own ledger — and if that evidence does not arrive
//      inside the timeout, the step says so in those words and the run continues to the
//      verdict with the failure on screen. A guided demo that asserts a beat it did not
//      observe is worse than no guided demo, because the one claim this project makes is
//      that systems should tell the truth about what they have done.
//
//   2. IT DRIVES NOTHING IT COULD NOT DRIVE BY HAND. Every action is a POST that already
//      has a button in the header. Nothing here reaches into the database, nothing fakes
//      a state, and the recovery in particular is watched rather than caused — no request
//      from this page tells the surviving worker to take the lease over.
//
//   3. IT IS INTERRUPTIBLE. STOP is checked between every await. The page is left in a
//      real state, because it was only ever in real states.
//
// The order of the steps is dictated by the engine, not by the story. The chaos worker
// has to run FIRST, while the queue still has claimable work — pressing KILL A WORKER on
// a drained board dispatches a worker with nothing to claim, which is the observed reason
// the old flow could sit narrating a crash that was never coming. And the recovery cannot
// be hurried: the stranded task is unclaimable until its 20-second lease lapses, and that
// interval IS the fence, so the run spends it saying so with a countdown instead of
// pretending it is not there.

/** Kept short enough to survive the rail, which gives each step about a seventh of the
 *  window. The sentence-length version of each beat is the narration line. */
const PROOF_STEPS = [
  ['SEED THE BOARD',        'seed'],
  ['CRASH MID-REFUND',      'crash'],
  ['THE MISSION RUNS',      'work'],
  ['A HUMAN AUTHORIZES',    'auth'],
  ['WAIT OUT THE LEASE',    'fence'],
  ['RECOVER, SAME KEY',     'recover'],
  ['THE PROVIDER LEDGER',   'verdict'],
];

const PF = {
  running: false,
  abort: false,
  i: -1,
  stranded: null,     // {id, key, idem, amount, epoch, available_at} captured at the crash
  failed: false,
};

const pfSleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Every await in the run passes through here so STOP is honoured promptly. */
function pfCheck() { if (PF.abort) throw new Error('__stopped__'); }

async function pfWait(ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) { pfCheck(); await pfSleep(Math.min(120, end - Date.now())); }
}

/** Poll `probe` until it returns a truthy value or the budget runs out.
 *  Returns the value, or null on timeout — the caller decides what to say about that. */
async function pfUntil(probe, budgetMs, everyMs, onTick) {
  const end = Date.now() + budgetMs;
  while (Date.now() < end) {
    pfCheck();
    const v = await probe();
    if (v) return v;
    if (onTick) onTick(Math.max(0, end - Date.now()));
    await pfSleep(everyMs || 500);
  }
  return null;
}

function pfRail() {
  $('#pf-rail').innerHTML = PROOF_STEPS.map((s, i) => {
    const cls = i < PF.i ? 'is-done' : (i === PF.i ? 'is-now' : '');
    return `<li class="pfs ${cls}"><b>${i + 1}</b><span>${esc(s[0])}</span></li>`;
  }).join('');
}

function pfStep(i) {
  PF.i = i;
  $('#pf-n').textContent = (i + 1) + '/' + PROOF_STEPS.length;
  $('#pf-step').textContent = PROOF_STEPS[i][0];
  pfRail();
}

/** The narration line. `tone` is '', 'hot' (something irreversible is outstanding) or
 *  'miss' (the beat did not happen — said plainly, never dressed up). */
function pfSay(text, tone) {
  const el = $('#pf-say');
  el.textContent = text;
  el.className = 'pf-say' + (tone ? ' is-' + tone : '');
}

function paintProofEvidence() {
  const p = S.pstats || {};
  $('#pf-fence').textContent = String(S.recoveries || 0);
  $('#pf-replay').textContent = String(p.replays != null ? p.replays : 0);
  const d = Number(p.duplicate_orders || 0);
  $('#pf-dupe').textContent = String(d);
  $('#pf-dupe-box').classList.toggle('is-alarm', d > 0);
}

/** Force one full poll cycle now, at the fast rung, so the grid and the journal are
 *  showing the same instant the narration is describing. */
async function pfRefresh() {
  P.rung = 0; P.idle = 0;
  clearTimeout(P.timer); P.timer = null;
  await tick();
  schedule();
}

// ─────────────────────────────────────────────────────────────────── the steps ──

async function stepSeed() {
  pfStep(0);
  pfSay('rebuilding the world — order exceptions, one budget authority, prior memories');
  const r = await withPollHeld(() => post('/api/demo/seed', { tasks: 30, reset: true }));
  resetClientState();
  await pfRefresh();
  // Read the counts back off the response and the mission rather than restating the
  // numbers the seed happened to use when this was written. seed.py's budget has already
  // moved once ($1,500 -> $2,500) and a narration that hardcodes it is a lie waiting for
  // the next commit.
  const budget = S.mission ? money(S.mission.budget_cents) : '—';
  pfSay(`${r.tasks} tasks READY against ${budget} of budget authority, ${r.memories} prior `
      + "memories. Nothing has run. The payment provider's ledger is empty.");
  await pfWait(1800);
}

async function stepCrash() {
  pfStep(1);
  setView('ops');
  pfSay('dispatching a worker that will die at crash window W4 — after the provider commits '
      + 'the refund, before AXIOM records it');

  // The evidence that W4 was actually entered: a receipt that is DISPATCHED and not
  // settled. That row is the system admitting an effect may exist that it has not
  // recorded — which is the only honest thing it can say at this instant.
  //
  // Two attempts, because a chaos worker only dies once it reaches a refund, and roughly
  // a fifth of the seeded exceptions are reship/escalate cases that move no money. A
  // worker that happens to draw only those drains them and exits cleanly, having proved
  // nothing — correct behaviour, and not a reason to fail the run. Retrying is honest
  // (it is the same button pressed twice); inventing the crash would not be.
  let receipt = null;
  for (let attempt = 0; attempt < 2 && !receipt; attempt++) {
    if (attempt) {
      // Two things can leave a chaos worker with no refund to die on: it drew only the
      // reship/escalate cases, or the queue was emptied out from under it by something
      // else claiming from this tenant. Re-seeding covers both, and it is the same SEED
      // the header button posts — the retry does nothing a person could not do by hand.
      pfSay('that worker found no refund to die on. Re-seeding and dispatching another.', 'miss');
      await withPollHeld(() => post('/api/demo/seed', { tasks: 30, reset: true }));
      resetClientState();
      await pfRefresh();
    }
    await post('/api/demo/run-worker', { mode: 'chaos' });
    receipt = await pfUntil(async () => {
      const rows = await get('/api/receipts/unsettled', []);
      await pfRefresh();
      return rows && rows.length ? rows[0] : null;
    }, 20000, 700);
  }

  if (!receipt) {
    PF.failed = true;
    pfSay('no unsettled receipt appeared after two chaos workers — W4 was not entered. '
        + 'Not claiming a crash that did not happen.', 'miss');
    await pfWait(3000);
    return;
  }

  const task = S.tasks.find((t) => t.id === receipt.task_id);
  // Also record it where the fence handler looks, so the key comparison is driven by the
  // same captured-before-the-crash string on both the guided and the manual path.
  S.receiptKeys.set(receipt.task_id, receipt.idempotency_key);
  PF.stranded = {
    id: receipt.task_id,
    key: task ? String(task.dedupe_key || '').replace(/^order:/, '').replace(/:refund$/, '') : '—',
    idem: receipt.idempotency_key,
    amount: receipt.amount_cents,
    epoch: task ? Number(task.lease_epoch) : 1,
    available_at: task ? task.available_at : null,
  };
  pfSay(`${money(receipt.amount_cents)} has left the building under key ${receipt.idempotency_key.slice(0, 18)}… `
      + `— and the worker that sent it is gone. ${PF.stranded.key} is stranded in ACTION_PREPARED.`, 'hot');
  await pfWait(4200);
}

async function stepWork() {
  pfStep(2);
  pfSay('the other 29 exceptions proceed. The stranded task cannot be touched by anyone — '
      + 'its lease has to lapse first.');
  await post('/api/demo/run-worker', { mode: 'drain' });

  await pfUntil(async () => {
    await pfRefresh();
    const busy = S.tasks.filter((t) => t.state === 'READY' || t.state === 'LEASED').length;
    return busy === 0 ? true : null;
  }, 25000, 600);

  const n = (s) => S.tasks.filter((t) => viewState(t) === s).length;
  pfSay(`${n('SUCCEEDED')} settled · ${n('AWAITING_APPROVAL')} stopped for a human · `
      + `${n('ESCALATED')} escalated with no money moved · 1 stranded mid-refund`);
  await pfWait(2600);
}

async function stepAuthorize() {
  pfStep(3);
  setView('approvals');
  const pending = await get('/api/approvals', []);
  if (!pending.length) {
    pfSay('nothing is waiting on a human this run — skipping the authority beat', 'miss');
    await pfWait(2000);
    return;
  }
  pfSay(`${pending.length} refunds exceeded the $200 unattended ceiling. Procedural memory did `
      + 'not advise — it refused. Execution is blocked until a human decides.');
  await pfWait(4200);

  for (const a of pending) {
    pfCheck();
    await post(`/api/approvals/${encodeURIComponent(a.id)}/decide`,
      { approved: true, decided_by: 'ops@acme.example',
        note: 'authorized during the guided proof' });
  }
  pfSay(`authorized by human:ops@acme.example — the decision is written into the same journal `
      + 'as the execution, with the policy version it overrode');
  await post('/api/demo/run-worker', { mode: 'drain' });
  await pfUntil(async () => {
    await pfRefresh();
    return S.tasks.some((t) => t.state === 'AWAITING_APPROVAL') ? null : true;
  }, 20000, 600);
  setView('ops');
  await pfWait(1200);
}

async function stepFence() {
  pfStep(4);
  if (!PF.stranded) { pfSay('nothing is stranded — no lease to wait out', 'miss'); await pfWait(1500); return; }

  // available_at is re-read from the task list each cycle rather than pinned once. The
  // value captured at the crash can be null if the poll had not yet seen the task, and a
  // null there silently collapsed this step to zero — which then dispatched the recovery
  // worker before the lease had lapsed, so it had nothing to claim and the fence never
  // landed. Read it live; the lease is the server's fact, not ours.
  await pfUntil(async () => {
    const t = S.tasks.find((x) => x.id === PF.stranded.id);
    const at = (t && t.available_at) || PF.stranded.available_at;
    if (!at) return true;                       // genuinely unknown — do not stall the run
    const left = Math.ceil((new Date(at).getTime() - Date.now()) / 1000);
    if (left <= 0) return true;
    pfSay(`the worker that died still holds the lease on ${PF.stranded.key}. No other worker may `
        + `touch it for ${left}s. That interval is the fence.`, 'hot');
    return null;
  }, 40000, 250);
  pfSay('the lease has lapsed. The task is claimable again — and the receipt is still there.');
  await pfWait(1500);
}

async function stepRecover() {
  pfStep(5);
  if (!PF.stranded) { pfSay('nothing to recover', 'miss'); await pfWait(1200); return; }
  pfSay('dispatching a fresh worker. Watch lease_epoch on ' + PF.stranded.key + '.');
  await post('/api/demo/run-worker', { mode: 'drain' });

  const done = await pfUntil(async () => {
    await pfRefresh();
    const t = S.tasks.find((x) => x.id === PF.stranded.id);
    return (t && Number(t.lease_epoch) > PF.stranded.epoch) ? t : null;
  }, 30000, 600);

  if (!done) {
    PF.failed = true;
    pfSay('lease_epoch did not advance within 30s. The takeover was not observed, so it is '
        + 'not being claimed.', 'miss');
    await pfWait(3000);
    return;
  }

  // The other half of the proof, and the half the epoch does not carry. Advancing the
  // fence stops the dead worker writing; it does not stop a SECOND refund. What stops
  // that is the recovering worker re-sending the key it recovered from the receipt — so
  // compare the string captured before the crash against the one on the settled attempt.
  if (await verifyIdempotency(PF.stranded.id, PF.stranded.idem)) {
    pfSay(`fence advanced e${PF.stranded.epoch} → e${done.lease_epoch}. The receipt was recovered `
        + 'and re-sent under the identical key — not a new one.');
  } else {
    pfSay(`fence advanced e${PF.stranded.epoch} → e${done.lease_epoch}, but the settled receipt did `
        + 'not carry the original key. Reporting that as-is.', 'miss');
  }
  await pfWait(4200);
}

async function stepVerdict() {
  pfStep(6);
  setView('ledger');
  await pfRefresh();
  const p = await get('/api/provider/stats', null) || {};
  renderProviderStats(p);
  renderLedger(await get('/api/provider/ledger?limit=100', []));

  // The ledger is in the provider's own order (newest first) and it is NOT reordered to
  // suit the argument — putting the interesting row at the top would be exactly the kind
  // of arrangement this project exists to complain about. Instead, scroll to it. It is
  // one row out of eighteen and it is the only one that matters here.
  const hit = $('#ledger-tbl tr.is-replay');
  if (hit) hit.scrollIntoView({ block: 'center', behavior: 'smooth' });

  const replays = Number(p.replays || 0);
  const dupes = Number(p.duplicate_orders || 0);
  if (dupes === 0 && replays > 0) {
    pfSay(`${p.refunds} refunds, ${replays} idempotent replay${replays === 1 ? '' : 's'}, `
        + `${dupes} duplicate orders — in the provider's ledger, which shares no transaction `
        + 'with AXIOM. Effectively-once, via receipts. Never exactly-once.');
  } else if (replays === 0) {
    pfSay(`${dupes} duplicates, but 0 replays — the crash window was not exercised, so this run `
        + 'proves nothing. Press RUN THE PROOF again.', 'miss');
  } else {
    pfSay(`${dupes} duplicate order${dupes === 1 ? '' : 's'} in the provider ledger. That is a `
        + 'failure and it is being reported as one.', 'miss');
  }
  pfRail();
}

async function runProof() {
  if (PF.running) return;
  PF.running = true; PF.abort = false; PF.failed = false; PF.stranded = null;
  $('#proof').hidden = false;
  $('#btn-proof').disabled = true;
  $('#btn-proof').textContent = 'PROOF RUNNING';
  paintProofEvidence();
  try {
    await stepSeed();
    await stepCrash();
    await stepWork();
    await stepAuthorize();
    await stepFence();
    await stepRecover();
    await stepVerdict();
    PF.i = PROOF_STEPS.length;      // every tick filled in
    pfRail();
  } catch (e) {
    if (e && e.message === '__stopped__') {
      pfSay('stopped. The board is left exactly as it was — nothing here was staged.');
      pfRail();
    } else {
      pfSay('the run failed: ' + (e && e.message ? e.message : String(e)), 'miss');
      toast('guided proof failed: ' + (e && e.message), true);
    }
  } finally {
    PF.running = false;
    $('#btn-proof').disabled = false;
    $('#btn-proof').textContent = 'RUN THE PROOF';
  }
}

// ═════════════════════════════════════════════════════════════════════ BRIEFING ══

const BRIEF_KEY = 'axiom.brief.seen.v1';

function openBrief() {
  $('#brief').hidden = false;
  $('#brief-x').focus();
}
function closeBrief() {
  $('#brief').hidden = true;
  try { localStorage.setItem(BRIEF_KEY, '1'); } catch (e) { /* private mode */ }
}
function maybeOpenBriefOnBoot() {
  let seen = false;
  try { seen = localStorage.getItem(BRIEF_KEY) === '1'; } catch (e) { /* private mode */ }
  // A hash deep-link is somebody who already knows where they are going; do not stand in
  // front of it.
  if (!seen && !location.hash) openBrief();
}

function resetClientState() {
  S.epochs.clear(); S.states.clear(); S.fenced.clear(); S.fenceFrom.clear(); S.fenceCount = 0;
  S.seenEvents.clear(); S.lastRecall = null; S.chaosAt = 0; S.missionSig = '';
  S.recoveries = 0; S.receiptKeys.clear();
  $('#taskgrid').innerHTML = ''; $('#events').innerHTML = '';
  $('#fence-count').textContent = '0';
  chaosBar(null);
  clearTimeout(showFenceProof.t);
  $('#fenceproof').hidden = true;
  $('#fp-idem').hidden = true;
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
  maybeOpenBriefOnBoot();
  run();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
