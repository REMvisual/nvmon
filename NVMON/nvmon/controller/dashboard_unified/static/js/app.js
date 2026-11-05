/* nvmon app (desktop widget) only — loaded when app_mode is set.
 *
 * The window has no title bar: the dashboard header is the title bar.
 *
 * Pressing empty header space sends "drag" (the window follows the mouse,
 * Snap works), double-clicking it sends "max"; the buttons at the right
 * send opacity / pin / collapse / min / max / close. "collapse" hides the
 * window entirely and leaves the floating sync widget; clicking the
 * widget brings it back.
 *
 * In nvmon-window.exe (WebView2) commands go straight to the host with
 * chrome.webview.postMessage(cmd); it answers {pinned, opacity, maximised}.
 * Fallback (plain Edge --app window): they are POSTs to /app/<cmd> on the
 * app's loopback controller.
 */
(function () {
  'use strict';
  var root = document.documentElement;
  if (!root.classList.contains('app-mode')) return;

  var host = window.chrome && window.chrome.webview;
  if (host) root.classList.add('app-host');

  var INTERACTIVE = 'button, a, input, select, textarea, label, summary, [role="button"], ' +
                    '[contenteditable], .fix-hints-menu, .power-menu';

  function paint(s) {
    if (!s || typeof s !== 'object') return;
    var pin = document.querySelector('.app-ctl [data-app="pin"]');
    if (pin && 'pinned' in s) pin.setAttribute('aria-pressed', s.pinned ? 'true' : 'false');
    var op = document.querySelector('.app-ctl [data-app="opacity"]');
    if (op && 'opacity' in s) op.title = 'Opacity ' + Math.round(s.opacity * 100) + '% (click to change)';
    var mx = document.querySelector('.app-ctl [data-app="max"]');
    if (mx && 'maximised' in s) mx.setAttribute('aria-pressed', s.maximised ? 'true' : 'false');
  }

  function call(cmd) {
    if (host) { host.postMessage(cmd); return; }
    fetch('/app/' + cmd, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    }).then(function (r) { return r.json(); }).then(paint).catch(function () {});
  }

  if (host) host.addEventListener('message', function (ev) { paint(ev.data); });

  document.addEventListener('click', function (ev) {
    var b = ev.target.closest && ev.target.closest('.app-ctl [data-app]');
    if (!b) return;
    ev.preventDefault();
    call(b.dataset.app);
  });

  // Empty header space is the title bar: press = move, double-click = maximise.
  function onEmptyHeader(ev) {
    var hdr = ev.target.closest && ev.target.closest('.hdr');
    return hdr && !ev.target.closest(INTERACTIVE);
  }
  document.addEventListener('mousedown', function (ev) {
    if (ev.button !== 0 || ev.detail > 1 || !onEmptyHeader(ev)) return;
    ev.preventDefault();
    call('drag');
  });
  document.addEventListener('dblclick', function (ev) {
    if (!onEmptyHeader(ev)) return;
    ev.preventDefault();
    call('max');
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { call('state'); });
  } else {
    call('state');
  }
})();
