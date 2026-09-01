/* Luuv Fryxo — shared front-end error reporter.
 *
 * Loaded as the FIRST script on each staff PWA (pos2, waiter, purchases, manager, admin) so it is
 * already listening when the page's own code runs:
 *
 *     <script src="/lferr.js?v=1" data-app="pos2"></script>
 *
 * Everything here is fire-and-forget and wrapped in try/catch — a reporting bug must never take
 * down the app it is watching. Nothing is logged when there is no CSRF token (logged-out page).
 *
 * What it catches:
 *   js       — uncaught runtime errors (window 'error' with an Error attached)
 *   promise  — unhandled promise rejections
 *   resource — a script/img/css that failed to load (this is why the icons vanish when the
 *              lucide CDN is blocked on a restaurant wifi)
 *   console  — anything the app itself passes to console.error
 *   api      — a fetch() that returned 5xx or died at the network layer. The apps swallow these
 *              (`catch(e){}` → a generic toast), so without this a broken backend looks to the
 *              staffer like "the app is slow" and never reaches anyone who can fix it.
 *
 * Bump the ?v= when this file changes — the PWA service workers cache same-origin GETs.
 */
(function () {
  'use strict';
  if (window.__lfErrLoaded) return;
  window.__lfErrLoaded = true;

  var ENDPOINT = '/api/method/luuvcrm.api.log_client_error';
  var QKEY = 'lf_err_queue';
  var MAX_PER_LOAD = 25;      // a render-loop error must not become 10k rows
  var MAX_QUEUE = 25;         // offline backlog cap (pos2 runs on flaky wifi)
  var MAX_TEXT = 4000;

  var APP = 'unknown';
  try {
    var me = document.currentScript;
    APP = (me && me.getAttribute('data-app')) ||
          (location.pathname.replace(/^\/+|\/+$/g, '').split('/')[0]) || 'unknown';
  } catch (e) {}

  var sentCount = 0;
  var seen = {};              // fingerprint → true, so one bug reports once per page load
  var flushing = false;
  // Grabbed before we wrap window.fetch, so our own reporting call can't be observed
  // (and re-reported) by the wrapper below.
  var rawFetch = window.fetch;

  function token() {
    try {
      var m = document.querySelector('meta[name="csrf-token"]');
      return (m && m.getAttribute('content')) || window.csrf_token || '';
    } catch (e) { return ''; }
  }

  function clip(v, n) {
    if (v === null || v === undefined) return '';
    var s = typeof v === 'string' ? v : String(v);
    return s.length > (n || MAX_TEXT) ? s.slice(0, n || MAX_TEXT) + ' …[cut]' : s;
  }

  function readQueue() {
    try { var a = JSON.parse(localStorage.getItem(QKEY) || '[]'); return a.length ? a : []; }
    catch (e) { return []; }
  }
  function writeQueue(a) {
    try {
      if (!a.length) localStorage.removeItem(QKEY);
      else localStorage.setItem(QKEY, JSON.stringify(a.slice(-MAX_QUEUE)));
    } catch (e) {}
  }

  // Returns a promise for "did the server take it?" — never rejects.
  function send(rec, keepalive) {
    var t = token();
    if (!t) return Promise.resolve(false);
    var body = new URLSearchParams();
    body.set('csrf_token', t);
    for (var k in rec) {
      if (Object.prototype.hasOwnProperty.call(rec, k) && rec[k] !== null && rec[k] !== undefined) {
        body.set(k, String(rec[k]));
      }
    }
    if (typeof rawFetch !== 'function') return Promise.resolve(false);
    try {
      return rawFetch.call(window, ENDPOINT, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'X-Frappe-CSRF-Token': t,
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
        },
        body: body.toString(),
        keepalive: !!keepalive
      }).then(function (r) { return !!(r && r.ok); }, function () { return false; });
    } catch (e) { return Promise.resolve(false); }
  }

  function flush() {
    if (flushing) return;
    var q = readQueue();
    if (!q.length) return;
    flushing = true;
    var rec = q[0];
    send(rec).then(function (ok) {
      flushing = false;
      if (!ok) return;                 // still offline — leave the backlog alone
      var cur = readQueue();
      cur.shift();
      writeQueue(cur);
      if (cur.length) setTimeout(flush, 400);
    });
  }

  function report(rec) {
    try {
      rec.app = APP;
      rec.page = clip(location.pathname + location.search, 300);
      rec.ua = clip(navigator.userAgent, 300);
      rec.screen = (window.innerWidth || 0) + 'x' + (window.innerHeight || 0);
      rec.at = new Date().toISOString();
      rec.message = clip(rec.message, 900);
      rec.stack = clip(rec.stack);
      rec.source = clip(rec.source, 300);

      var fp = [rec.app, rec.kind, rec.message, rec.source, rec.line].join('|');
      if (seen[fp]) return;            // same bug, same page load — already reported
      seen[fp] = true;
      if (sentCount >= MAX_PER_LOAD) return;
      sentCount++;

      send(rec).then(function (ok) {
        if (ok) return;
        var q = readQueue();
        q.push(rec);
        writeQueue(q);                 // retried on next load / when the network returns
      });
    } catch (e) {}
  }

  function stackOf(err) {
    if (!err) return '';
    if (err.stack) return err.stack;
    try { return (err.name || 'Error') + ': ' + (err.message || String(err)); } catch (e) { return ''; }
  }

  // ── uncaught errors + failed resource loads ──
  // Capture phase, because resource load failures do not bubble.
  window.addEventListener('error', function (ev) {
    try {
      var el = ev.target;
      if (el && el !== window && (el.src || el.href)) {
        report({ kind: 'resource', message: 'Failed to load ' + (el.tagName || '?').toLowerCase(),
                 source: el.src || el.href, line: 0, col: 0, stack: '' });
        return;
      }
      report({ kind: 'js', message: ev.message || 'Unknown error', source: ev.filename || '',
               line: ev.lineno || 0, col: ev.colno || 0, stack: stackOf(ev.error) });
    } catch (e) {}
  }, true);

  window.addEventListener('unhandledrejection', function (ev) {
    try {
      var r = ev.reason;
      var msg = (r && (r.message || r.statusText)) || (typeof r === 'string' ? r : '') ||
                'Unhandled promise rejection';
      report({ kind: 'promise', message: msg, source: '', line: 0, col: 0, stack: stackOf(r) });
    } catch (e) {}
  });

  // ── console.error passthrough ──
  var rawConsoleError = window.console && window.console.error;
  if (rawConsoleError) {
    window.console.error = function () {
      try {
        var parts = [];
        for (var i = 0; i < arguments.length; i++) {
          var a = arguments[i];
          parts.push(a instanceof Error ? stackOf(a)
                     : (typeof a === 'object' ? (function () {
                         try { return JSON.stringify(a); } catch (e) { return String(a); } })()
                       : String(a)));
        }
        report({ kind: 'console', message: parts.join(' '), source: '', line: 0, col: 0, stack: '' });
      } catch (e) {}
      try { return rawConsoleError.apply(window.console, arguments); } catch (e) {}
    };
  }

  // ── fetch: report server faults the apps otherwise swallow ──
  if (typeof rawFetch === 'function') {
    window.fetch = function (input, init) {
      var url = '';
      try { url = typeof input === 'string' ? input : (input && input.url) || ''; } catch (e) {}
      var p;
      try { p = rawFetch.apply(this, arguments); } catch (e) { throw e; }
      try {
        p.then(function (r) {
          try {
            // 4xx is normal traffic here (403 → the login redirect in every app's _api()).
            if (r && r.status >= 500 && url.indexOf(ENDPOINT) === -1) {
              report({ kind: 'api', message: 'HTTP ' + r.status + ' from ' + apiName(url),
                       source: clip(url, 300), line: 0, col: 0, stack: '' });
            }
          } catch (e) {}
        }, function (err) {
          try {
            if (url.indexOf(ENDPOINT) === -1) {
              report({ kind: 'api', message: 'Network failure calling ' + apiName(url),
                       source: clip(url, 300), line: 0, col: 0, stack: stackOf(err) });
            }
          } catch (e) {}
        });
      } catch (e) {}
      return p;
    };
  }

  function apiName(url) {
    try {
      var m = String(url).match(/\/api\/method\/([\w.]+)/);
      return m ? m[1] : String(url).split('?')[0];
    } catch (e) { return 'request'; }
  }

  // ── manual hook for app code: lfErr.log('charge failed', {order: name}) ──
  window.lfErr = {
    log: function (message, extra) {
      report({ kind: 'manual', message: message, source: '', line: 0, col: 0,
               stack: extra ? (function () {
                 try { return JSON.stringify(extra); } catch (e) { return String(extra); } })() : '' });
    },
    app: APP
  };

  window.addEventListener('online', flush);
  if (document.readyState === 'complete') setTimeout(flush, 1500);
  else window.addEventListener('load', function () { setTimeout(flush, 1500); });
})();
