/* Luuv Fryxo — shared staff activity (tap) tracker.
 *
 * Sibling of lferr.js: that one reports what BREAKS, this one reports what the
 * staff DO. Loaded right after it on the staff PWAs:
 *
 *     <script src="/lfact.js?v=1" data-app="pos"></script>
 *
 * Why a client-side tracker at all, when api.py already logs every write:
 * the server only ever sees the RESULT. When a cashier's finger lands twice on
 * the same item inside two seconds, the server receives one autosave with qty 2
 * and cannot tell it from a deliberate "two kottu". The browser can, and that is
 * exactly the "the bill was Rs 2,000 more than it should be" complaint.
 *
 * Design notes:
 *   - Events are queued and flushed in batches (the POS taps fast; one POST per
 *     tap would put the log in the critical path of the cart).
 *   - The queue survives a reload and a dead wifi in localStorage — the POS runs
 *     offline regularly, and a trail with a hole in it answers nothing.
 *   - Every tap carries the bill total BEFORE and AFTER it, so the timeline
 *     reads like a bank statement and a wrong tap is visible without maths.
 *   - Everything is wrapped in try/catch and nothing here may throw into the
 *     app: wrapCart() calls the original function first and logs afterwards.
 *
 * Bump the ?v= when this file changes — the PWA service workers cache same-origin GETs.
 */
(function () {
  'use strict';
  if (window.LFAct) return;

  var ENDPOINT = '/api/method/luuvcrm.activity.log_ui_activity';
  var QKEY = 'lf_act_queue';
  var FLUSH_MS = 4000;        // batch window
  var FLUSH_AT = 15;          // ...or this many events, whichever comes first
  var MAX_BATCH = 60;         // server caps at 60 too
  var MAX_QUEUE = 300;        // offline backlog cap
  var DOUBLE_TAP_MS = 3000;   // same item twice inside this = flagged

  var APP = 'unknown';
  try {
    var me = document.currentScript;
    APP = (me && me.getAttribute('data-app')) ||
          (location.pathname.replace(/^\/+|\/+$/g, '').split('/')[0]) || 'unknown';
  } catch (e) {}

  var SESSION = 's' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  var seq = 0;
  var queue = [];
  var timer = null;
  var flushing = false;
  var lastTap = {};           // "action|item" → epoch ms, for double-tap detection
  // Grabbed before any app code can wrap it, and used for our own POST so the
  // lferr.js fetch wrapper never reports our reporting call.
  var rawFetch = window.fetch;

  var ctx = { order: null, bill: null, table: null };

  function token() {
    try {
      var m = document.querySelector('meta[name="csrf-token"]');
      return (m && m.getAttribute('content')) || window.csrf_token || '';
    } catch (e) { return ''; }
  }

  function num(v) {
    var n = typeof v === 'number' ? v : parseFloat(v);
    return isFinite(n) ? Math.round(n * 100) / 100 : null;
  }

  function clip(v, n) {
    if (v === null || v === undefined) return '';
    var s = typeof v === 'string' ? v : String(v);
    return s.length > n ? s.slice(0, n) : s;
  }

  function load() {
    try {
      var raw = localStorage.getItem(QKEY);
      return raw ? (JSON.parse(raw) || []) : [];
    } catch (e) { return []; }
  }

  function save(q) {
    try { localStorage.setItem(QKEY, JSON.stringify(q.slice(-MAX_QUEUE))); } catch (e) {}
  }

  /* ── the one public write ──────────────────────────────────────────── */
  function tap(action, o) {
    try {
      if (!action) return;
      o = o || {};
      var now = Date.now();
      var ev = {
        action: clip(action, 60),
        label: clip(o.label || '', 140),
        order: clip(o.order !== undefined ? o.order : ctx.order, 140),
        bill: clip(o.bill !== undefined ? o.bill : ctx.bill, 60),
        table: clip(o.table !== undefined ? o.table : ctx.table, 60),
        item: clip(o.item, 140),
        item_name: clip(o.item_name, 140),
        qty: o.qty === undefined ? null : num(o.qty),
        amount: o.amount === undefined ? null : num(o.amount),
        before: o.before === undefined ? null : num(o.before),
        after: o.after === undefined ? null : num(o.after),
        detail: o.detail || null,
        page: clip(location.pathname + location.search, 300),
        seq: ++seq,
        ts: now
      };

      // Double tap: the same item, the same action, inside three seconds. This is
      // the single most common way a bill silently grows.
      if (ev.item && /^cart\.(add|inc)$/.test(ev.action)) {
        var k = ev.action + '|' + ev.item;
        var prev = lastTap[k];
        if (prev && (now - prev) < DOUBLE_TAP_MS) {
          ev.flag = 'double-tap';
          ev.flag_note = ((now - prev) / 1000).toFixed(1) + 's after the previous tap';
        }
        lastTap[k] = now;
      }

      queue.push(ev);
      if (queue.length >= FLUSH_AT) flush();
      else if (!timer) timer = setTimeout(flush, FLUSH_MS);
    } catch (e) {}
  }

  function flush(useBeacon) {
    try {
      if (timer) { clearTimeout(timer); timer = null; }
      if (flushing && !useBeacon) return;

      var pending = load().concat(queue);
      queue = [];
      if (!pending.length) { save([]); return; }
      var batch = pending.slice(0, MAX_BATCH);
      var rest = pending.slice(MAX_BATCH);
      save(pending);                      // keep everything until the POST lands

      var t = token();
      if (!t) return;                     // logged out — hold it, do not drop it

      var body = new URLSearchParams();
      body.set('app', APP);
      body.set('session_id', SESSION);
      body.set('ua', clip(navigator.userAgent, 300));
      body.set('events', JSON.stringify(batch));

      // Page is going away: sendBeacon is the only thing that reliably survives.
      if (useBeacon && navigator.sendBeacon) {
        body.set('csrf_token', t);
        var ok = navigator.sendBeacon(ENDPOINT, body);
        if (ok) save(rest);
        return;
      }

      flushing = true;
      rawFetch.call(window, ENDPOINT, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-Frappe-CSRF-Token': t
        },
        body: body.toString(),
        credentials: 'same-origin'
      }).then(function (r) {
        flushing = false;
        if (r && r.ok) {
          save(rest);
          if (rest.length) setTimeout(flush, 200);
        }
        // Not ok → the batch stays in localStorage and rides the next flush.
      }).catch(function () { flushing = false; });
    } catch (e) { flushing = false; }
  }

  /* ── auto-wrap an app's existing cart functions ────────────────────────
   * pos/waiter declare their cart handlers as top-level `function foo(){}` in a
   * classic inline <script>, so they live on `window` and can be wrapped without
   * touching a single line inside them. The original runs FIRST and its return
   * value is passed straight back; logging happens after and can never change
   * what the app does.
   *
   *   LFAct.wrapCart({ total: fn, order: fn, table: fn, name: fn, rate: fn },
   *                  { addToCart: 'cart.add', setQty: '@qty', ... })
   */
  function wrapCart(hooks, map) {
    hooks = hooks || {};
    var call = function (fn, a) {
      try { return typeof fn === 'function' ? fn(a) : null; } catch (e) { return null; }
    };
    Object.keys(map || {}).forEach(function (fname) {
      var orig = window[fname];
      if (typeof orig !== 'function' || orig._lfWrapped) return;
      var spec = map[fname];
      var wrapped = function () {
        var args = Array.prototype.slice.call(arguments);
        var before = call(hooks.total);
        var out;
        try {
          out = orig.apply(this, args);
        } finally {
          try {
            var code = typeof args[0] === 'string' ? args[0] : '';
            var action = spec, label = '';
            var qty = null;
            if (typeof spec === 'function') {
              // Escape hatch for handlers keyed by something other than an item
              // code (waiter's previewQty takes a line id): the app resolves it.
              var r = spec(args) || {};
              action = r.action || 'cart.change';
              label = r.label || '';
              code = r.item || code;
              if (r.qty !== undefined) qty = r.qty;
            } else if (spec === '@qty') {     // setQty(code, delta)
              var d = Number(args[1]) || 0;
              action = d < 0 ? 'cart.dec' : 'cart.inc';
              label = (d < 0 ? 'Qty −' : 'Qty +') + (code ? ' ' + call(hooks.name, code) : '');
              qty = d;
            }
            tap(action, {
              label: label || undefined,
              item: code || undefined,
              item_name: code ? call(hooks.name, code) : undefined,
              qty: qty,
              amount: code ? call(hooks.rate, code) : undefined,
              before: before,
              after: call(hooks.total),
              order: call(hooks.order),
              table: call(hooks.table)
            });
          } catch (e) {}
        }
        return out;
      };
      wrapped._lfWrapped = true;
      window[fname] = wrapped;
    });
  }

  function setCtx(o) {
    try {
      if (!o) return;
      if ('order' in o) ctx.order = o.order || null;
      if ('bill' in o) ctx.bill = o.bill || null;
      if ('table' in o) ctx.table = o.table || null;
    } catch (e) {}
  }

  window.LFAct = { tap: tap, flush: flush, wrapCart: wrapCart, setCtx: setCtx,
                   session: SESSION, app: APP };

  try {
    // A cashier closing the till app must not take the last taps with them.
    window.addEventListener('pagehide', function () { flush(true); });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') flush(true);
    });
    // Anything stranded by an earlier offline stretch goes out on the next load.
    window.addEventListener('load', function () { setTimeout(flush, 1500); });
  } catch (e) {}
})();
