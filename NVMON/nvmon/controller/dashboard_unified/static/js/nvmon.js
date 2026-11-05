/* ============================================================
   nvmon.js — core client logic for the unified dashboard.

   Responsibilities:
     1. Open and manage the SSE /stream connection.
     2. Provide an Alpine.js `$store.nvmon` global state object
        (group filter, view mode, slide-over/menu state, alerts).
     3. Render sparklines inline into <svg data-sparkline>.
     4. Drive the fan-out flow via a short-lived WebSocket.
     5. Keyboard shortcuts: A S R G Esc / and arrow nav.

   This file ships as static; no build step.
   ============================================================ */

(function () {
  'use strict';

  /* -----------------------------------------------------------
   *  SSE client (single persistent connection)
   * --------------------------------------------------------- */
  function SSEClient(url) {
    this.url = url;
    this.es = null;
    this.handlers = Object.create(null);
    this.status = 'connecting';
    this.retryMs = 1000;
  }
  SSEClient.prototype.on = function (event, fn) {
    (this.handlers[event] = this.handlers[event] || []).push(fn);
  };
  SSEClient.prototype.emit = function (event, data) {
    (this.handlers[event] || []).forEach(function (fn) { fn(data); });
  };
  SSEClient.prototype.connect = function () {
    var self = this;
    if (this.es) { try { this.es.close(); } catch (_) {} }

    this.setStatus('connecting');
    this.es = new EventSource(this.url);

    this.es.addEventListener('open', function () {
      self.retryMs = 1000;
      self.setStatus('live');
    });

    [
      'node_update', 'alert', 'heartbeat', 'node_reachable_change',
      'node_appeared', 'node_disappeared',
    ].forEach(function (type) {
      self.es.addEventListener(type, function (ev) {
        var payload;
        try { payload = JSON.parse(ev.data); } catch (e) { return; }
        self.emit(type, payload);
      });
    });

    this.es.addEventListener('error', function () {
      self.setStatus('reconnecting');
      try { self.es.close(); } catch (_) {}
      // EventSource auto-reconnects, but we nudge to show UI state.
      setTimeout(function () { self.connect(); }, Math.min(self.retryMs, 15000));
      self.retryMs = Math.min(self.retryMs * 2, 15000);
    });
  };
  SSEClient.prototype.setStatus = function (s) {
    this.status = s;
    var el = document.querySelector('.ftr .stream-state');
    if (el) {
      el.setAttribute('data-state', s);
      var label = el.querySelector('[data-stream-state]');
      if (label) label.textContent = s;
    }
  };

  /* -----------------------------------------------------------
   *  Sparkline renderer (inline SVG polyline)
   * --------------------------------------------------------- */
  function renderSparklines(root) {
    (root || document).querySelectorAll('svg[data-sparkline]').forEach(function (svg) {
      var raw = svg.getAttribute('data-sparkline') || '';
      var pts = raw.split(',').map(Number).filter(function (n) { return !isNaN(n); });
      if (pts.length < 2) return;
      var min = +svg.getAttribute('data-min') || Math.min.apply(null, pts);
      var max = +svg.getAttribute('data-max') || Math.max.apply(null, pts);
      var w = +svg.getAttribute('viewBox').split(' ')[2] || 120;
      var h = +svg.getAttribute('viewBox').split(' ')[3] || 24;
      var range = (max - min) || 1;
      var step = w / (pts.length - 1);
      var coords = pts.map(function (v, i) {
        var y = h - ((v - min) / range) * h;
        return (i * step).toFixed(1) + ',' + y.toFixed(1);
      }).join(' ');
      var poly = svg.querySelector('polyline');
      if (poly) poly.setAttribute('points', coords);

      // Color by latest sample
      var last = pts[pts.length - 1];
      var color = 'var(--ok)';
      if (last > 85) color = 'var(--crit)';
      else if (last > 75) color = 'var(--warn)';
      svg.style.color = color;
    });
  }

  /* -----------------------------------------------------------
   *  Fan-out WebSocket client
   * --------------------------------------------------------- */
  function FanoutWS(jobId, wsUrl, onMessage, onClose) {
    this.jobId = jobId;
    this.ws = null;
    this.url = wsUrl;
    this.onMessage = onMessage;
    this.onClose = onClose;
  }
  FanoutWS.prototype.open = function () {
    var self = this;
    var u = this.url;
    if (!/^wss?:/.test(u)) {
      var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      u = proto + '//' + location.host + u;
    }
    this.ws = new WebSocket(u);
    this.ws.addEventListener('message', function (ev) {
      try { self.onMessage(JSON.parse(ev.data)); } catch (_) {}
    });
    this.ws.addEventListener('close', function () { self.onClose && self.onClose(); });
    this.ws.addEventListener('error', function () { self.onClose && self.onClose(); });
  };
  FanoutWS.prototype.cancel = function () {
    if (this.ws && this.ws.readyState === 1) {
      try { this.ws.send(JSON.stringify({ action: 'cancel' })); } catch (_) {}
    }
  };
  FanoutWS.prototype.close = function () {
    if (this.ws) { try { this.ws.close(); } catch (_) {} }
  };

  /* -----------------------------------------------------------
   *  Alpine store registration
   * --------------------------------------------------------- */
  document.addEventListener('alpine:init', function () {
    /* global Alpine */
    Alpine.store('nvmon', {
      // State
      group: 'all',
      view: storedView(),
      filter: '',
      slideoverHost: null,
      fanoutOpen: false,
      groupsData: readJSON('groups-data') || [],
      topAlert: null,

      // Scope helpers -------------------------------------------------
      scopeTargets() {
        var me = this;
        if (me.group === 'all') {
          return Array.from(document.querySelectorAll('.node[data-online="true"]'))
            .map(function (el) { return el.dataset.hostname; });
        }
        return Array.from(document.querySelectorAll('.node[data-group="' + cssEscape(me.group) + '"][data-online="true"]'))
          .map(function (el) { return el.dataset.hostname; });
      },
      // Like scopeTargets() but ignores data-online — returns every known
      // host in the current group whether the controller currently sees
      // it as ONLINE or OFFLINE. Used by Restart Listener: a node that
      // appears offline in the dashboard is the exact case where you
      // want to bounce its listener, not skip it.
      scopeAllKnown() {
        var me = this;
        var q = me.group === 'all'
          ? '.node[data-hostname]'
          : '.node[data-group="' + cssEscape(me.group) + '"]';
        return Array.from(document.querySelectorAll(q))
          .map(function (el) { return el.dataset.hostname; });
      },
      scopeSkipped() {
        var me = this;
        var q = '.node[data-online="false"]';
        if (me.group !== 'all') q = '.node[data-group="' + cssEscape(me.group) + '"][data-online="false"]';
        return Array.from(document.querySelectorAll(q)).map(function (el) {
          return { hostname: el.dataset.hostname, reason: 'offline' };
        });
      },
      scopeLabel() {
        var t = this.scopeTargets().length;
        if (this.group === 'all') return t + ' node' + (t === 1 ? '' : 's');
        return this.group + ' (' + t + ' node' + (t === 1 ? '' : 's') + ')';
      },

      // Actions -------------------------------------------------------
      setGroup(g)  { this.group = g; applyFilter(); },
      setView(v) {
        this.view = v;
        localStorage.setItem('nvmon-view', v);
        // The dashboard.css uses `body.view-list` to flatten the grid into
        // a stack of full-width rows. Without this branch the toggle was
        // a pure no-op (the user reported clicking List did nothing).
        applyView(v);
      },
      setFilter(f) { this.filter = (f || '').toLowerCase(); applyFilter(); },

      openFanout(action, opts) {
        opts = opts || {};
        var scope = opts.scope || this.scopeTargets();
        if (!scope || scope.length === 0) {
          window.alert('No online targets to act on. Pick a group with online nodes first.');
          return;
        }

        // Sync and Restore don't need a profile picker -- skip the
        // (Alpine-fragile) modal and just fire the fanout job directly,
        // confirming through the browser's native dialogs. This is the
        // bulletproof path: any of the modal's stages can fail to bind
        // and the user is left with a grey box; with this path every
        // click definitively does something.
        if (action === 'sync' || action === 'restore') {
          var actionLabel = action === 'sync' ? 'Sync (QSync enable)' : 'Restore to desktop';
          var hostsLabel = scope.length === 1 ? scope[0] : (scope.length + ' nodes');
          if (!window.confirm(actionLabel + ' on ' + hostsLabel + '?')) return;
          fireFanoutDirect(action, scope, null);
          return;
        }

        // Re-apply All path: opts.reapplyActive=true bypasses the profile
        // picker entirely. The fanout job dispatches /apply with no body
        // profile; the controller looks up each node's status.active_profile
        // and fans out per-node. Lets the fleet hold mixed profiles and
        // still be re-applied with one click.
        if (action === 'apply' && opts.reapplyActive) {
          var hostsLabel = scope.length === 1 ? scope[0] : (scope.length + ' nodes');
          if (!window.confirm('Re-apply each node\'s active profile on ' + hostsLabel + '?\n\n'
                              + 'Nodes with no active profile will be skipped.')) return;
          fireFanoutDirect('apply', scope, null);
          return;
        }

        // Template: switch the fleet to a profile template — every node
        // gets ITS role's profile for the picked template (host nodes get
        // "<T> - SyncHost", clients "<T> - SyncClient", per the {template}
        // patterns in [controller.node_profiles]). Same per-node fan-out
        // as Re-apply All; the controller remembers the pick so Re-apply
        // All keeps using it. Also addressable: POST /all/template.
        if (action === 'template') {
          fetch('/templates').then(function (r) { return r.ok ? r.json() : { templates: [] }; })
            .then(function (j) {
              var templates = (j && j.templates) || [];
              if (templates.length === 0) {
                window.alert('No templates found.\n\n'
                  + 'A template needs (1) {template} in the [controller.node_profiles] values in nvmon.toml, '
                  + 'and (2) a matching profile for EVERY pattern, e.g. "30 - SyncHost" AND "30 - SyncClient".\n\n'
                  + ((j && j.missing) ? 'Partial sets ignored: ' + j.missing.join(', ') : ''));
                return;
              }
              showProfilePicker(scope, templates, function (pick) {
                if (!window.confirm('Switch ' + (scope.length === 1 ? scope[0] : scope.length + ' nodes')
                                    + ' to template "' + pick + '"?\n\n'
                                    + 'Each node gets its own role\'s profile for that template.')) return;
                fireFanoutDirect('template', scope, pick);
              }, { title: 'Apply template to', preferred: (j && (j.active || j.default)) || '' });
            })
            .catch(function (e) {
              window.alert('Could not fetch template list: ' + e);
            });
          return;
        }

        // Apply: fetch the list, show the picker overlay, fire on click.
        // No Alpine, no prompt(); buildable from any state.
        if (action === 'apply') {
          fetch('/profiles').then(function (r) { return r.ok ? r.json() : { profiles: [] }; })
            .then(function (j) {
              var profiles = (j && j.profiles) || (readJSON('profiles-data') || []);
              if (profiles.length === 0) {
                window.alert('No profiles found across the fleet. Run `python -m nvmon save-profile <name>` on a node first.');
                return;
              }
              showProfilePicker(scope, profiles, function (pick) {
                fireFanoutDirect('apply', scope, pick);
              });
            })
            .catch(function (e) {
              window.alert('Could not fetch profile list: ' + e);
            });
          return;
        }

        window.alert('Unknown action: ' + action);
      },
      closeFanout() {
        this.fanoutOpen = false;
        var portal = document.getElementById('modal-portal');
        if (portal) portal.innerHTML = '';
      },

      /* restartMonitor — bounce the controller process itself (REM-gated
         power-menu item, v0.9.2). POST /system/restart_monitor spawns a
         detached helper on the monitor box that runs kill_monitor.bat and
         then run_monitor.bat (minimized, no browser — this tab is the
         browser). The tab stays open: once /health has gone away and come
         back, reload. If the controller never dropped (very fast bounce)
         reload after 20 s anyway; give up after 2 min with a pointer to
         the helper log. */
      restartMonitor() {
        var ok = window.confirm(
          'Restart the monitor (controller) on this machine?\n\n' +
          'The controller process stops and run_monitor.bat relaunches it ' +
          'minimized — no OS reboot, the render nodes are not touched.\n\n' +
          'This page goes dark for ~5–10 s and reloads by itself when the ' +
          'controller answers again. Use it after new code has been mirrored ' +
          'into NVMON\\ on the monitor box.'
        );
        if (!ok) return;
        announce('RESTART MONITOR requested');
        showToast('Restarting controller — this page reloads when it is back…', 'pending');
        fetch('/system/restart_monitor', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ delay_seconds: 2, open_browser: false }),
        }).then(function (r) {
          return r.json().catch(function () { return {}; })
            .then(function (j) { return { ok: r.ok, status: r.status, data: j }; });
        }).then(function (resp) {
          if (!resp.ok || !(resp.data && resp.data.ok)) {
            showToast('Restart monitor failed: HTTP ' + resp.status + ' ' +
                      ((resp.data && resp.data.error) || ''), 'crit');
            return;
          }
          var delayMs = ((resp.data && resp.data.delay_seconds) || 2) * 1000;
          var t0 = Date.now();
          var sawDown = false;
          function probe() {
            var elapsed = Date.now() - t0;
            if (elapsed > 120000) {
              showToast('Controller did not come back within 2 min — check the "nvmon - monitor" ' +
                        'window on the monitor box and logs\\restart_monitor-*.log', 'crit');
              return;
            }
            fetch('/health', { cache: 'no-store' }).then(function (r) {
              if (r.ok && (sawDown || elapsed > 20000)) { window.location.reload(); return; }
              setTimeout(probe, 1500);
            }).catch(function () { sawDown = true; setTimeout(probe, 1500); });
          }
          setTimeout(probe, delayMs + 1500);
        }).catch(function (e) {
          showToast('Restart monitor request failed: ' + e, 'crit');
        });
      },

      /* openPower — restart / shutdown / abort flow for one node or the
         whole fleet.  Two-step window.confirm chain so a misclick can't
         take down the renderfarm.  Goes through the same /fanout/job
         path as apply/sync/restore so the result toast shows the per-
         node outcome (succeeded / failed / unreachable).

         The second confirm spells out the recovery asymmetry:
           - restart  : auto-recovers via "nvmon Listener" Scheduled Task
           - shutdown : stays off until physical power-on / Wake-on-LAN
         so the operator can't claim they weren't warned. */
      openPower(action, opts) {
        opts = opts || {};
        if (action !== 'restart' && action !== 'shutdown' && action !== 'restart_listener') {
          window.alert('Unsupported power action: ' + action);
          return;
        }
        // restart_listener targets every KNOWN host (online + offline) —
        // a stuck/disconnected listener is exactly the case where you
        // want to bounce it, not skip it. restart/shutdown still default
        // to online-only since you can't reboot an unreachable box.
        var defaultScope = action === 'restart_listener'
          ? this.scopeAllKnown()
          : this.scopeTargets();
        var scope = opts.scope || defaultScope;

        // Strip the controller's own host from OS-level actions. The fanout
        // layer also refuses self-host (defense in depth), but filtering
        // here means the confirmation dialog tells the truth about what
        // will actually happen instead of saying "5 nodes" then quietly
        // skipping one. restart_listener is safe against self because it
        // bounces a different process — keep self in scope for it.
        var droppedSelf = [];
        if (action !== 'restart_listener') {
          scope = (scope || []).filter(function (h) {
            var card = document.querySelector('.node[data-hostname="' + cssEscape(h) + '"]');
            if (card && card.dataset.self === 'true') {
              droppedSelf.push(h);
              return false;
            }
            return true;
          });
          if (droppedSelf.length) {
            announce('Skipping monitor host: ' + droppedSelf.join(', '));
          }
        }

        if (!scope || scope.length === 0) {
          window.alert(
            'No targets for ' + action + '.\n\n' +
            (droppedSelf.length
              ? 'The only candidate was the monitor (controller) machine, which is intentionally excluded from power actions.'
              : 'Pick a group with online nodes, or open a single node and use the per-node Power section.')
          );
          return;
        }

        var verb = action === 'restart_listener' ? 'RESTART LISTENER' : action.toUpperCase();
        var hostsLabel = scope.length === 1
          ? scope[0]
          : scope.length + ' nodes (' + scope.slice(0, 4).join(', ') +
            (scope.length > 4 ? ', …' : '') + ')';

        // restart_listener doesn't take down the OS — single confirmation
        // is enough. OS-level restart/shutdown still gets the two-step.
        if (action === 'restart_listener') {
          var ok = window.confirm(
            'Restart listener on ' + hostsLabel + '?\n\n' +
            'The listener process will stop and re-launch via the ' +
            '"nvmon Listener" Scheduled Task — no OS reboot. ' +
            'Each node briefly goes OFFLINE for ~5–10s while it reboots ' +
            'the python process; the dashboard will recover automatically.\n\n' +
            'Use this after FreeFileSyncing new code into NVVMON\\.'
          );
          if (!ok) return;
          announce('RESTART LISTENER fanout to ' + scope.length + ' node(s)');
          fireFanoutDirect('restart_listener', scope, null);
          return;
        }

        // First confirmation — straightforward "you really meant to click this?"
        var step1 = window.confirm(
          verb + ' ' + hostsLabel + '?\n\n' +
          'Render machines will go offline immediately. ' +
          (action === 'restart'
            ? 'They will reboot in ~8 seconds.'
            : 'They will power off in ~8 seconds.')
        );
        if (!step1) return;

        // Second confirmation — last chance, with recovery semantics spelled out
        // and the host list repeated so you can't slip on the wrong scope.
        var note = action === 'restart'
          ? 'Recovery: the listener auto-restarts on next logon (nvmon Listener Scheduled Task, /SC ONLOGON /RL HIGHEST).'
          : 'Recovery: the machine stays OFF until physically powered on or via Wake-on-LAN. nvmon does NOT auto-revive shutdown nodes.';
        var step2 = window.confirm(
          'ARE YOU SURE?\n\n' +
          verb + ' on ' + hostsLabel + '\n\n' +
          note + '\n\n' +
          'Click OK to proceed. Cancel to back out.'
        );
        if (!step2) return;

        announce(verb + ' fanout to ' + scope.length + ' node(s)');
        fireFanoutDirect(action, scope, null);
      },

      openNodeMenu(host, ev) {
        // Slide-over already shows the per-node Actions section; this just
        // jumps the operator's focus there in case the panel is long.
        if (this.slideoverHost === host) {
          var heading = document.getElementById('h-actions');
          if (heading) heading.scrollIntoView({ behavior: 'smooth', block: 'start' });
        } else {
          this.gotoNode(host);
        }
      },
      gotoNode(host) {
        this.slideoverHost = host;
        fetch('/node/' + encodeURIComponent(host) + '/detail')
          .then(function (r) { return r.text(); })
          .then(function (html) {
            mountInjected(document.getElementById('slideover-portal'), html);
          });
      },
      closeSlideover() {
        this.slideoverHost = null;
        var portal = document.getElementById('slideover-portal');
        if (portal) portal.innerHTML = '';
      },

      forceRefresh() {
        // The user asked for the dashboard ⟳ button to act as a manual
        // ping/poll, not just an SSE reconnect. We POST /poll which tells
        // the aggregator to probe every node right now (skipping the 5s
        // tick) and we reload the page once the result returns so the
        // grid reflects fresh online/offline state immediately.
        announce('Polling all nodes…');
        fetch('/poll', { method: 'POST' })
          .then(function (r) { return r.json().catch(function () { return {}; }); })
          .then(function (data) {
            announce('Polled ' + (data.polled || '?') + ' node(s) in ' + (data.duration_ms || '?') + 'ms');
            if (window.__nvmonSSE) window.__nvmonSSE.connect();
            // Soft-reload the dashboard markup so node cards re-render
            // with the post-poll registry state.
            setTimeout(function () { window.location.reload(); }, 400);
          })
          .catch(function () {
            if (window.__nvmonSSE) window.__nvmonSSE.connect();
            announce('Poll request failed; reconnecting stream.');
          });
      },

      saveProfileFrom(host) {
        var name = window.prompt(
          'Save ' + host + "'s current state as profile:",
          'default'
        );
        if (!name) return;
        name = String(name).trim();
        var description = window.prompt(
          'Description (optional)',
          'Captured from ' + host + ' on ' + new Date().toISOString().slice(0,10)
        ) || '';
        fetch('/node/' + encodeURIComponent(host) + '/profile/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name, description: description })
        }).then(function (r) {
          return r.json().then(function (j) { return { ok: r.ok, status: r.status, data: j }; });
        }).then(function (resp) {
          // Controller wraps the agent reply in a fanout-style result.
          var inner = (resp.data && Array.isArray(resp.data.results) && resp.data.results[0]) || null;
          var nodeReply = inner && inner.body;
          if (resp.ok && inner && inner.status === 'accepted' && nodeReply && nodeReply.saved) {
            announce('Saved profile "' + name + '" → ' + nodeReply.saved);
            window.alert('Saved profile "' + name + '" on ' + host + '.\n\nFile: ' + nodeReply.saved);
          } else {
            var err = (inner && (inner.error || (nodeReply && nodeReply.error))) ||
                      ('HTTP ' + resp.status);
            announce('Save failed: ' + err);
            window.alert('Save failed on ' + host + ': ' + err);
          }
        }).catch(function (e) {
          announce('Save failed: ' + e);
          window.alert('Save failed: ' + e);
        });
      }
    });

    /* Node grid component ---------------------------------------- */
    Alpine.data('nodeGrid', function () {
      return {
        init() {
          renderSparklines(this.$el);
          bindArrowNav(this.$el);
        }
      };
    });

    /* Node detail component -------------------------------------- */
    Alpine.data('nodeDetail', function (hostname) {
      return {
        hostname: hostname,
        init() {
          // charts.js listens for this and wires up Chart.js instances.
          window.dispatchEvent(new CustomEvent('nvmon:detail-opened', {
            detail: { hostname: hostname, root: this.$el }
          }));
        }
      };
    });

    /* Per-node Ping — forces the controller to probe the agent's /health
       right now (bypasses the aggregator cache) and shows the result
       inline. On success we also refresh the slide-over so the freshly-
       polled subsystems re-render.

       The user asked for the old ⟳ refresh button to become a manual
       ping, so OFFLINE nodes can be re-tested without waiting for the
       next 5s aggregator tick. */
    Alpine.data('pingButton', function (hostname) {
      return {
        hostname: hostname,
        pinging: false,
        result: '',
        resultClass: '',
        ping() {
          var me = this;
          me.pinging = true;
          me.result = '';
          var t0 = Date.now();
          fetch('/node/' + encodeURIComponent(hostname) + '/ping', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
          })
            .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, status: r.status, data: j }; }); })
            .then(function (resp) {
              me.pinging = false;
              var dt = Date.now() - t0;
              if (resp.ok && resp.data && resp.data.ok) {
                me.result = '✓ ' + (resp.data.duration_ms || dt) + 'ms';
                me.resultClass = 'ok';
                // ALSO update the matching card on the main dashboard so
                // it flips OFFLINE→ONLINE without waiting for a refresh.
                var card = document.querySelector('.node[data-hostname="' + cssEscape(hostname) + '"]');
                if (card) {
                  card.setAttribute('data-online', 'true');
                  var status = card.querySelector('.node-status');
                  if (status) {
                    status.textContent = 'ONLINE';
                    status.classList.remove('offline');
                  }
                }
                // Force a slide-over refresh so OFFLINE → ONLINE flips visibly.
                fetch('/node/' + encodeURIComponent(hostname) + '/detail')
                  .then(function (r) { return r.text(); })
                  .then(function (html) {
                    mountInjected(document.getElementById('slideover-portal'), html);
                  });
              } else {
                var err = (resp.data && (resp.data.error || resp.data.detail)) || ('HTTP ' + resp.status);
                me.result = '✗ ' + err;
                me.resultClass = 'crit';
              }
            })
            .catch(function (e) {
              me.pinging = false;
              me.result = '✗ ' + e;
              me.resultClass = 'crit';
            });
        }
      };
    });

    /* Raw NVIDIA CLI dump panel — used by the Diagnose section to fetch
       /node/<host>/<subsys>/raw and show stdout + parsed view. */
    Alpine.data('rawDump', function (hostname) {
      return {
        hostname: hostname,
        loading: false,
        error: '',
        data: null,
        show(subsystem) {
          var me = this;
          me.loading = true;
          me.error = '';
          me.data = null;
          fetch('/node/' + encodeURIComponent(hostname) + '/' + subsystem + '/raw')
            .then(function (r) {
              return r.json().then(function (j) { return { ok: r.ok, status: r.status, data: j }; });
            })
            .then(function (resp) {
              me.loading = false;
              if (!resp.ok) {
                me.error = (resp.data && resp.data.error) || ('HTTP ' + resp.status);
                return;
              }
              me.data = resp.data;
            })
            .catch(function (e) {
              me.loading = false;
              me.error = String(e);
            });
        }
      };
    });

    /* Fan-out modal component ------------------------------------ */
    Alpine.data('fanoutModal', function (cfg) {
      var profiles = cfg.profiles || [];
      // Prefer the GT profile name the user agreed to (default) — it's the
      // one captured via save-profile on node1 and the most likely choice
      // when sync'ing the fleet.
      var pre = profiles.indexOf('default') >= 0 ? 'default' : (profiles[0] || null);
      return {
        action: cfg.action,
        scope: cfg.scope || [],
        skipped: cfg.skipped || [],
        profiles: profiles,
        stage: 'confirm',
        selectedProfile: pre,
        progress: [],
        summary: { succeeded: 0, failed: 0, skipped: 0, failures: [] },
        ws: null,

        title() {
          if (this.action === 'apply')   return 'Apply profile';
          if (this.action === 'sync')    return 'Sync All';
          if (this.action === 'restore') return 'Restore All';
          return this.action;
        },
        subtitle() {
          return this.scope.length + ' target node' + (this.scope.length === 1 ? '' : 's') +
                 (this.skipped.length ? ' · ' + this.skipped.length + ' skipped' : '');
        },
        doneCount() {
          return this.progress.filter(function (r) {
            return r.state === 'done' || r.state === 'failed' || r.state === 'skipped';
          }).length;
        },

        start() {
          var me = this;
          me.stage = 'progress';
          me.progress = me.scope.map(function (h) {
            return { hostname: h, state: 'pending', stateLabel: 'queued', durationText: '' };
          });
          fetch('/fanout/job', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              action: me.action,
              scope: me.scope,
              profile: me.selectedProfile
            })
          }).then(function (r) {
            return r.json().then(function (data) { return { ok: r.ok, status: r.status, data: data }; });
          }).then(function (resp) {
            if (!resp.ok) {
              me.stage = 'summary';
              me.summary = {
                succeeded: 0, failed: me.scope.length, skipped: 0,
                failures: me.scope.map(function (h) {
                  return { hostname: h, error: (resp.data && resp.data.error) || ('HTTP ' + resp.status) };
                })
              };
              return;
            }
            var data = resp.data || {};
            // Server-side fanout — controller returns a synchronous summary
            // with empty ws_url.  Apply progress + summary directly.
            if (!data.ws_url) {
              if (Array.isArray(data.progress)) {
                data.progress.forEach(function (p) { me.handleMessage({ type: 'progress', hostname: p.hostname, state: p.state, state_label: p.state_label }); });
              }
              if (data.summary) {
                me.handleMessage({
                  type: 'summary',
                  succeeded: data.summary.succeeded,
                  failed: data.summary.failed,
                  skipped: data.summary.skipped,
                  failures: data.summary.failures
                });
              }
              return;
            }
            me.ws = new FanoutWS(data.job_id, data.ws_url,
              function (msg) { me.handleMessage(msg); },
              function ()    { /* socket closed */ });
            me.ws.open();
          }).catch(function (e) {
            me.stage = 'summary';
            me.summary = {
              succeeded: 0, failed: me.scope.length, skipped: 0,
              failures: [{ hostname: 'controller', error: String(e) }]
            };
          });
        },
        handleMessage(msg) {
          if (msg.type === 'progress') {
            var row = this.progress.find(function (r) { return r.hostname === msg.hostname; });
            if (row) {
              row.state = msg.state;                       // running | done | failed | skipped
              row.stateLabel = msg.state_label || msg.state;
              if (msg.duration_ms != null) row.durationText = '(' + msg.duration_ms + ' ms)';
            }
          } else if (msg.type === 'summary') {
            this.summary = {
              succeeded: msg.succeeded || 0,
              failed: msg.failed || 0,
              skipped: msg.skipped || 0,
              failures: msg.failures || []
            };
            this.stage = 'summary';
          }
        },
        cancel() {
          if (this.ws) this.ws.cancel();
          announce('Cancel requested — commands already accepted will complete.');
        },
        close() {
          if (this.ws) this.ws.close();
          Alpine.store('nvmon').closeFanout();
        }
      };
    });
  });

  /* -----------------------------------------------------------
   *  Filter + group visibility
   * --------------------------------------------------------- */
  /* -----------------------------------------------------------
   *  View mode: grid | list | auto
   *  auto = cards while every visible card fits in the window height,
   *  the compact list as soon as they don't (resize the window shorter
   *  and it flips to the list, taller and the cards come back). The grid
   *  is laid out and measured in the same frame, so nothing flickers.
   * --------------------------------------------------------- */
  function storedView() {
    var v = localStorage.getItem('nvmon-view');
    return (v === 'grid' || v === 'list' || v === 'auto') ? v : 'auto';
  }
  function applyView(v) {
    var b = document.body;
    var list = v === 'list';
    if (v === 'auto') {
      b.classList.remove('view-list');
      b.classList.add('view-grid');
      var grid = document.getElementById('nodes');
      if (grid) {
        var ftr = document.querySelector('.ftr');
        var bottom = grid.getBoundingClientRect().bottom + window.scrollY;
        list = bottom + (ftr ? ftr.offsetHeight : 0) > window.innerHeight + 2;
      }
    }
    b.classList.toggle('view-list', list);
    b.classList.toggle('view-grid', !list);
    b.classList.toggle('view-auto', v === 'auto');
  }
  var _autoViewQueued = false;
  function reapplyAutoView() {
    if (_autoViewQueued || storedView() !== 'auto') return;
    _autoViewQueued = true;
    window.requestAnimationFrame(function () {
      _autoViewQueued = false;
      applyView('auto');
    });
  }
  window.addEventListener('resize', reapplyAutoView);
  window.nvmonApplyView = reapplyAutoView;

  function applyFilter() {
    var s = Alpine.store('nvmon');
    // Cards and list rows both carry data-group / data-hostname (v0.9.8:
    // the list view used to ignore the Wall / Ceiling filter).
    document.querySelectorAll('.node, .node-row').forEach(function (el) {
      var matchesGroup = (s.group === 'all') || (el.dataset.group === s.group);
      var host = (el.dataset.hostname || '').toLowerCase();
      var matchesFilter = !s.filter || host.indexOf(s.filter) !== -1;
      el.classList.toggle('hidden', !(matchesGroup && matchesFilter));
    });
    reapplyAutoView();  // fewer/more cards may now fit
  }

  /* -----------------------------------------------------------
   *  Arrow-key node card navigation
   * --------------------------------------------------------- */
  function bindArrowNav(grid) {
    grid.addEventListener('keydown', function (ev) {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(ev.key)) return;
      ev.preventDefault();
      var cards = Array.from(grid.querySelectorAll('.node:not(.hidden)'));
      var current = document.activeElement;
      var idx = cards.indexOf(current);
      if (idx < 0) { cards[0] && cards[0].focus(); return; }
      var cols = computeGridCols(grid);
      var next = idx;
      if (ev.key === 'ArrowRight') next = Math.min(cards.length - 1, idx + 1);
      if (ev.key === 'ArrowLeft')  next = Math.max(0, idx - 1);
      if (ev.key === 'ArrowDown')  next = Math.min(cards.length - 1, idx + cols);
      if (ev.key === 'ArrowUp')    next = Math.max(0, idx - cols);
      cards[next] && cards[next].focus();
    });
  }
  function computeGridCols(grid) {
    var style = window.getComputedStyle(grid);
    return (style.getPropertyValue('grid-template-columns') || '').split(' ').length || 1;
  }

  /* -----------------------------------------------------------
   *  Global keyboard shortcuts
   * --------------------------------------------------------- */
  function bindKeybindings() {
    document.addEventListener('keydown', function (ev) {
      // Skip when typing in form fields
      var tag = (ev.target && ev.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') {
        if (ev.key === 'Escape') ev.target.blur();
        return;
      }
      var store = (window.Alpine && Alpine.store) ? Alpine.store('nvmon') : null;
      if (store) {
        if (ev.key === 'a' || ev.key === 'A') { store.openFanout('apply', {reapplyActive: true});  ev.preventDefault(); }
        if (ev.key === 't' || ev.key === 'T') { store.openFanout('template'); ev.preventDefault(); }
        if (ev.key === 's' || ev.key === 'S') { store.openFanout('sync');   ev.preventDefault(); }
        if (ev.key === 'r' || ev.key === 'R') { store.forceRefresh();       ev.preventDefault(); }
        if (ev.key === 'g' || ev.key === 'G') {
          var first = document.querySelector('.tabbar .tab');
          first && first.focus(); ev.preventDefault();
        }
        if (ev.key === '/') {
          var input = document.getElementById('node-filter');
          input && input.focus(); ev.preventDefault();
        }
      }
      // Escape ALWAYS clears any open modal/slideover via plain JS,
      // so the user is never stuck on a misbehaving Alpine binding.
      if (ev.key === 'Escape') {
        var modalPortal = document.getElementById('modal-portal');
        var slidePortal = document.getElementById('slideover-portal');
        if (modalPortal && modalPortal.firstElementChild) {
          modalPortal.innerHTML = '';
          if (store) store.fanoutOpen = false;
        } else if (slidePortal && slidePortal.firstElementChild) {
          slidePortal.innerHTML = '';
          if (store) store.slideoverHost = null;
        }
      }
    });
  }

  /* -----------------------------------------------------------
   *  SSE-driven DOM updates
   * --------------------------------------------------------- */
  function bindSSE() {
    var sse = new SSEClient('/stream');
    window.__nvmonSSE = sse;

    // v0.8.2: periodic soft refresh of the #fleet block (cards + list) so
    // server-rendered conditionals catch up without a manual reload. The
    // helper skips itself while a modal / slide-over is open or the grid
    // has keyboard focus.
    setInterval(function () { softRefreshFleet(''); }, 45000);
    document.body.addEventListener('htmx:afterSwap', function (e) {
      var t = e && e.detail && e.detail.target;
      if (t && t.id === 'fleet') {
        renderSparklines(document);
        // The swapped-in block is unfiltered server HTML; re-apply the
        // Wall / Ceiling / text filter so cards and list rows stay scoped.
        try { applyFilter(); } catch (_) { /* store not ready */ }
        reapplyAutoView();
        // v0.9.10: the header's Fix hints dropdown lives outside #fleet;
        // copy the fresh list + count from the hidden #fix-hints-src.
        try {
          var src = document.getElementById('fix-hints-src');
          var body = document.getElementById('fix-hints-body');
          var btn = document.getElementById('fix-hints-btn');
          if (src && body) body.innerHTML = src.innerHTML;
          if (src && btn) {
            var n = parseInt(src.getAttribute('data-count') || '0', 10) || 0;
            btn.classList.toggle('has', n > 0);
            var c = btn.querySelector('[data-hints-count]');
            if (c) c.textContent = n ? ' (' + n + ')' : '';
          }
        } catch (_) { /* best effort */ }
      }
    });

    sse.on('node_update', function (data) {
      // Swap the card's counters/dots in-place. HTMX could do this too;
      // we keep it in JS so a single SSE connection serves all tabs.
      var card = document.querySelector('.node[data-hostname="' + cssEscape(data.hostname) + '"]');
      if (!card) return;

      // Update text fields the user expects to reflect live state.
      // Previously the card kept its server-rendered active_profile /
      // refresh_hz / displays_count until a full page reload — apply a
      // profile that drops the rate to 24Hz and the slide-over showed
      // 24Hz immediately while the tile still showed 60Hz. The
      // `data-field` selectors mirror the spans tagged in dashboard.html.
      function setField(name, value) {
        var el = card.querySelector('[data-field="' + name + '"]');
        if (el && el.textContent !== value) el.textContent = value;
      }
      if (data.active_profile != null) {
        setField('active_profile', data.active_profile || 'no profile');
      }
      if (data.refresh_hz != null) {
        setField('refresh_hz', Number(data.refresh_hz).toFixed(3));
      }
      if (data.displays_count != null) {
        setField('displays_count', String(data.displays_count));
      }
      // v0.9.3: live colour depth / encoding ("10-bit Ycc422"); hidden
      // when Windows does not report it for this node.
      if (data.color_text != null) {
        var ctEl = card.querySelector('[data-field="color_text"]');
        if (ctEl) {
          if (ctEl.textContent !== data.color_text) ctEl.textContent = data.color_text;
          ctEl.style.display = data.color_text ? '' : 'none';
        }
      }
      // v0.8.0: stale / applying marker in the card foot. The agent pauses
      // its snapshot while an apply owns the NVIDIA tools; show that
      // instead of a frozen reading that looks live.
      var ageEl = card.querySelector('[data-field="status_age"]');
      if (ageEl && data.status_age_s != null) {
        var age = Number(data.status_age_s) || 0;
        if (data.job_active) {
          ageEl.textContent = '⟳ applying';
          ageEl.style.display = '';
          ageEl.style.color = '#38bdf8';
        } else if (age > 15) {
          ageEl.textContent = 'stale ' + Math.round(age) + 's';
          ageEl.style.display = '';
          ageEl.style.color = 'var(--warn)';
        } else {
          ageEl.style.display = 'none';
        }
      }
      // Temperature label next to the sparkline. Server-rendered as
      // "GPU {N}°C"; we rewrite the whole string so the °C glyph and
      // formatting stay consistent.
      if (data.gpu && data.gpu[0] && data.gpu[0].temperature_c != null) {
        var t = Number(data.gpu[0].temperature_c);
        setField('temperature_c', 'GPU ' + Math.round(t) + '°C');
        // Update the temp class so it recolors the same way the
        // server-rendered branch does (>85 crit, >75 warn, else ok).
        var tempEl = card.querySelector('[data-field="temperature_c"]');
        if (tempEl) {
          tempEl.classList.remove('ok', 'warn', 'crit');
          tempEl.classList.add(t > 85 ? 'crit' : (t > 75 ? 'warn' : 'ok'));
        }
      }

      // Update dot levels
      ['dd', 'mosaic', 'qsync'].forEach(function (key) {
        var lvl = data.subsystems && data.subsystems[key] && data.subsystems[key].level;
        if (!lvl) return;
        var sel = '.dot[title^="' + (key === 'dd' ? 'DirectDisplay' : key === 'mosaic' ? 'Mosaic' : 'QSync') + '"]';
        var dot = card.querySelector(sel);
        if (dot) dot.setAttribute('data-level', lvl);
      });

      // v0.8.2: job badge (apply/sync/restore outcome), chain line, QSync
      // dot tooltip and the list-view row. All of these were server-
      // rendered only, so a finished apply or a chain mismatch needed a
      // manual reload before the tile showed it.
      var jobEl = card.querySelector('[data-field="job"]');
      if (jobEl) renderJobBadge(jobEl, data.job, data.job_active);
      var chainEl = card.querySelector('[data-field="chain"]');
      if (chainEl && data.chain) renderChain(chainEl, data.chain);
      var qdot = card.querySelector('.dot[title^="QSync"]');
      if (qdot && data.subsystems && data.subsystems.qsync) {
        qdot.title = 'QSync: ' + (data.subsystems.qsync.status || '');
      }
      updateListRow(data);

      // Update card-level worst status. Match the server-side rollup in
      // dashboard.html: QSync + alerts + profile validation contribute. DD
      // reports level=warn for "inactive" on a healthy non-DD rig; rolling
      // that up made every node yellow forever (and the SSE was overwriting
      // the server-rendered green every 5s).
      var qLevel = (data.subsystems && data.subsystems.qsync && data.subsystems.qsync.level) || 'off';
      var alertsCount = data.alerts_count || 0;
      var worst = 'ok';
      if (qLevel === 'crit') worst = 'crit';
      else if (qLevel === 'warn' || alertsCount > 0 || data.validation_ok === false) worst = 'warn';
      card.setAttribute('data-level', worst);

      // Update sparkline
      if (data.gpu && data.gpu[0] && data.gpu[0].sparkline) {
        var svg = card.querySelector('svg[data-sparkline]');
        if (svg) {
          svg.setAttribute('data-sparkline', data.gpu[0].sparkline.join(','));
          renderSparklines(card);
        }
      }
    });

    sse.on('alert', function (alert) {
      var el = document.getElementById('live-region');
      if (el) el.textContent = '[' + alert.severity + '] ' + alert.hostname + ': ' + alert.type;
    });

    sse.on('heartbeat', function (hb) {
      var online = document.querySelector('[data-counter="online"]');
      var total  = document.querySelector('[data-counter="total"]');
      if (online && hb.nodes_online != null) online.textContent = hb.nodes_online;
      if (total  && hb.nodes_online != null && hb.nodes_offline != null)
        total.textContent = hb.nodes_online + hb.nodes_offline;
    });

    sse.on('node_reachable_change', function (data) {
      // Previously this handler only flipped the `data-online` attribute,
      // which was used by CSS for the card border but did NOT update the
      // visible "ONLINE / OFFLINE" pill text or its `.offline` class. So
      // when a node came back online the card border lit up correctly but
      // the pill kept saying OFFLINE until a full page reload — exactly
      // the symptom the user reported (card shows OFFLINE, slideover for
      // the same node shows ONLINE because the slideover is a fresh
      // server-rendered HTMX fetch).
      //
      // Fix: when reachability flips, update the pill text + class. Also
      // fire an HTMX request to refresh the *card row* so the conditional
      // server-rendered blocks (active profile, subsystem dots) appear
      // when a node comes back online.
      // v0.9.8: the list-view row too — it used to keep the status it had
      // at page load, so an offline node still read ONLINE in list mode.
      var row = document.querySelector('.node-row[data-hostname="' + cssEscape(data.hostname) + '"]');
      if (row) {
        row.setAttribute('data-online', data.reachable ? 'true' : 'false');
        var cell = row.querySelector('[data-field="status"]');
        if (cell) {
          cell.classList.toggle('ok', !!data.reachable);
          cell.classList.toggle('crit', !data.reachable);
          cell.setAttribute('data-sort', data.reachable ? '0_online' : '1_offline');
          var txt = cell.querySelector('[data-field="status_text"]');
          if (txt) txt.textContent = data.reachable ? 'ONLINE' : 'OFFLINE';
        }
      }
      var card = document.querySelector('.node[data-hostname="' + cssEscape(data.hostname) + '"]');
      if (!card) return;
      card.setAttribute('data-online', data.reachable ? 'true' : 'false');

      var pill = card.querySelector('.node-status');
      if (pill) {
        pill.textContent = data.reachable ? 'ONLINE' : 'OFFLINE';
        pill.classList.toggle('offline', !data.reachable);
      }

      // The conditional sub-block (`{% if n.online %}` — active profile,
      // dots) was server-rendered with the initial page load and won't
      // appear retroactively when the node comes back online. The next
      // `node_update` SSE will populate metrics if the card has the
      // matching DOM nodes; for a node that was offline at first paint
      // they're missing entirely, and a page reload is the cleanest way
      // to recover. We don't auto-reload here because that would yank the
      // operator out of any open slideover. The pill flip alone fixes
      // the "card says offline / slideover says online" inconsistency
      // the user reported, which is the actual confusing part.
    });

    // node_appeared: a listener that wasn't on the page when it loaded
    // has shown up. We want the new card visible without a manual click.
    //
    // Two reload tracks, because they have opposing failure modes:
    //   - First-ever appearance of a hostname this session → reload
    //     immediately (debounced 1.5s for burst collapse). The user
    //     just stood up a new node and they're waiting to see it.
    //   - Re-appearance of a hostname we've already seen → reload but
    //     throttled to once every 15s. This is the flap case: a flaky
    //     listener TTL-expires + re-announces every few seconds; without
    //     a throttle the page loops.
    //
    // Seed the seenHosts set from the cards already in the DOM so a
    // node that was rendered server-side and is announced again (zeroconf
    // refresh) doesn't trigger a useless reload.
    var seenHosts = new Set();
    document.querySelectorAll('.node[data-hostname], .node-row[data-hostname]').forEach(function (el) {
      var h = el.getAttribute('data-hostname');
      if (h) seenHosts.add(h);
    });
    var reloadTimer = null;
    var lastReloadAt = 0;
    var REAPPEAR_FLOOR_MS = 15000;
    var FIRST_APPEAR_DEBOUNCE_MS = 1500;
    var REAPPEAR_DEBOUNCE_MS = 3000;
    function scheduleReload(reason, throttle) {
      if (reloadTimer) return;
      if (throttle) {
        var sinceLast = Date.now() - lastReloadAt;
        if (sinceLast < REAPPEAR_FLOOR_MS) {
          announce(reason + ' (recent refresh — skipped to avoid flap loop)');
          return;
        }
      }
      announce(reason);
      var delay = throttle ? REAPPEAR_DEBOUNCE_MS : FIRST_APPEAR_DEBOUNCE_MS;
      reloadTimer = setTimeout(function () {
        lastReloadAt = Date.now();
        window.location.reload();
      }, delay);
    }
    sse.on('node_appeared', function (data) {
      var host = data.hostname || '';
      var firstTime = !seenHosts.has(host);
      seenHosts.add(host);
      scheduleReload(
        'New listener detected: ' + (host || '?') + ' — refreshing…',
        !firstTime,  // throttle only when the host is reappearing
      );
    });
    // node_disappeared: do NOT reload. The card already has node_reachable_change
    // wired to flip its OFFLINE/ONLINE state, so the existing card just goes red.
    // Reloading on every disappear was the root cause of "I keep losing nodes,
    // saw 10 now I see 2" — when mDNS is flaky, a single listener flickering
    // every few seconds caused cascading reloads that briefly hid every other
    // node card mid-render. Quietly mark the disappeared card as stale instead.
    sse.on('node_disappeared', function (data) {
      var card = document.querySelector('.node[data-hostname="' + cssEscape(data.hostname || '') + '"]');
      if (card) {
        card.setAttribute('data-online', 'false');
        card.setAttribute('data-stale', 'true');
      }
    });

    sse.connect();
  }

  /* -----------------------------------------------------------
   *  showProfilePicker — overlay menu listing available profiles.
   *  Replaces the window.prompt() that used to gate Apply. Pure DOM,
   *  no Alpine, dismiss on Escape / outside click / Cancel. The
   *  callback fires with the picked profile name.
   * --------------------------------------------------------- */
  function showProfilePicker(scope, profiles, onPick, opts) {
    opts = opts || {};
    var existing = document.getElementById('nvmon-profile-picker');
    if (existing) existing.remove();

    var preferred = (opts.preferred && profiles.indexOf(opts.preferred) >= 0)
      ? opts.preferred
      : (profiles.indexOf('default') >= 0 ? 'default' : profiles[0]);

    var backdrop = document.createElement('div');
    backdrop.id = 'nvmon-profile-picker';
    backdrop.className = 'nvmon-picker-backdrop';

    var card = document.createElement('div');
    card.className = 'nvmon-picker-card';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-label', 'Apply profile');

    var title = document.createElement('div');
    title.className = 'nvmon-picker-title';
    var hostsLabel = scope.length === 1 ? scope[0] : (scope.length + ' nodes');
    title.textContent = (opts.title || 'Apply profile to') + ' ' + hostsLabel;
    card.appendChild(title);

    var list = document.createElement('div');
    list.className = 'nvmon-picker-list';
    profiles.forEach(function (name) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'nvmon-picker-item';
      if (name === preferred) btn.classList.add('preferred');
      btn.textContent = name;
      btn.addEventListener('click', function () {
        cleanup();
        onPick(name);
      });
      list.appendChild(btn);
    });
    card.appendChild(list);

    var footer = document.createElement('div');
    footer.className = 'nvmon-picker-footer';
    var cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'nvmon-picker-cancel';
    cancel.textContent = 'Cancel (Esc)';
    cancel.addEventListener('click', cleanup);
    footer.appendChild(cancel);
    card.appendChild(footer);

    backdrop.appendChild(card);
    backdrop.addEventListener('click', function (ev) {
      if (ev.target === backdrop) cleanup();
    });

    function onKey(ev) {
      if (ev.key === 'Escape') { cleanup(); ev.preventDefault(); }
    }
    document.addEventListener('keydown', onKey);
    function cleanup() {
      document.removeEventListener('keydown', onKey);
      backdrop.remove();
    }

    document.body.appendChild(backdrop);
    // Focus the preferred button so Enter applies it directly.
    var preferredBtn = list.querySelector('.preferred') || list.querySelector('.nvmon-picker-item');
    if (preferredBtn) preferredBtn.focus();
  }

  /* -----------------------------------------------------------
   *  fireFanoutDirect — POST /fanout/job and render a plain-JS toast
   *  with the per-node result.  Bypasses Alpine entirely so it works
   *  even when the modal HTML failed to bind.  Used by openFanout()
   *  for sync/restore (no modal) and apply (after the picker chooses
   *  the profile).  The toast is dismiss-on-click and falls off after 8s.
   * --------------------------------------------------------- */
  function fireFanoutDirect(action, scope, profile) {
    showToast('Running ' + action + ' on ' + scope.length + ' node(s)...', 'pending');
    var body = { action: action, scope: scope };
    if (action === 'template') body.template = profile;   // picker value is the template name
    else if (profile) body.profile = profile;
    fetch('/fanout/job', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, status: r.status, data: j }; });
    }).then(function (resp) {
      if (!resp.ok) {
        showToast(action + ' failed: HTTP ' + resp.status + ' ' + ((resp.data && resp.data.error) || ''), 'crit');
        return;
      }
      var s = (resp.data && resp.data.summary) || {};
      var lines = [
        action.toUpperCase() + (profile ? ' (' + profile + ')' : '') + ' dispatched.',
        '  ✓ ' + (s.succeeded || 0) + ' accepted by agent',
      ];
      if (s.failed)  lines.push('  ✗ ' + s.failed  + ' failed at dispatch');
      if (s.skipped) lines.push('  - ' + s.skipped + ' skipped');
      (s.failures || []).forEach(function (f) {
        lines.push('     ' + (f.hostname || '?') + ': ' + (f.error || ''));
      });
      var level = s.failed ? 'crit' : (s.succeeded ? 'ok' : 'warn');
      showToast(lines.join('\n'), level);

      // Restore / system actions finish on the agent within a few
      // seconds; re-render the fleet block once so role, lock state and
      // the job badge reflect the outcome without a manual reload.
      if (action !== 'apply' && action !== 'template' && action !== 'sync') {
        setTimeout(function () { softRefreshFleet(action + ' finished — view refreshed'); }, 6000);
      }

      // Apply, template and (v0.8.2) sync run async on the agent and the
      // 2xx we just got is dispatch-only. Poll each accepted target's
      // /apply/status to surface the TRUTHFUL outcome (success / "QSync
      // not locked" / "Mosaic configure failed" / …). restart_*/shutdown
      // finish synchronously so the dispatch result is the truth.
      if (action === 'apply' || action === 'template' || action === 'sync') {
        var accepted = ((resp.data && resp.data.summary && resp.data.summary.accepted_hosts) || [])
          .concat([]);
        // Fall back to scope when summary doesn't carry per-host accept
        // info — better to over-poll than to miss the truth.
        if (!accepted.length) accepted = scope.slice();
        if (accepted.length) pollApplyOutcomes(accepted, profile, action);
      }
    }).catch(function (e) {
      showToast(action + ' request failed: ' + e, 'crit');
    });
  }

  /* pollApplyOutcomes — for each host in the apply scope, poll
   *   GET /node/<host>/apply/status
   * every 2s until either:
   *   - in_progress=false → record final success/message,
   *   - 30s elapsed → record "still in progress / unknown",
   *   - HTTP failure → record the transport error.
   * Then surface a single follow-up toast summarizing the truth so the
   * operator can stop trusting the dispatch-only "Applied" message. */
  function pollApplyOutcomes(hosts, profile, action) {
    action = action || 'apply';
    // A sync is two enable cycles with settles (~30 s worst case); an
    // apply can run a full Mosaic rebuild. Poll a bit longer than either.
    var DEADLINE_MS = action === 'sync' ? 45000 : 60000;
    var INTERVAL_MS = 2000;
    var start = Date.now();
    var pending = new Set(hosts);
    var outcomes = {};  // host -> { success, message, http?, transport_error? }

    function recordAndMaybeFinish() {
      if (pending.size === 0 || (Date.now() - start) >= DEADLINE_MS) {
        renderApplyOutcomeToast(hosts, outcomes, profile, action);
        return true;
      }
      return false;
    }

    function tick() {
      if (recordAndMaybeFinish()) return;
      var batch = Array.from(pending);
      Promise.all(batch.map(function (h) {
        return fetch('/node/' + encodeURIComponent(h) + '/apply/status', {
          headers: { 'Accept': 'application/json' },
        }).then(function (r) {
          return r.json().then(function (j) { return { host: h, ok: r.ok, status: r.status, data: j }; })
                          .catch(function () { return { host: h, ok: r.ok, status: r.status, data: null }; });
        }).catch(function (e) {
          return { host: h, ok: false, status: 0, transport_error: String(e) };
        });
      })).then(function (rows) {
        rows.forEach(function (row) {
          if (row.transport_error) {
            // Network glitch — keep polling, only record on deadline.
            return;
          }
          var d = row.data || {};
          // Only accept a FINISHED record of the action we dispatched —
          // a stale "apply ok" from an hour ago must not be read as the
          // outcome of the sync we just fired.
          var startedAt = d.started_at ? Date.parse(d.started_at) : NaN;
          var fresh = isNaN(startedAt) || startedAt >= (start - 15000);
          if (d.in_progress === false && d.success != null && fresh
              && (!d.action || d.action === action || (action === 'template' && d.action === 'apply'))) {
            outcomes[row.host] = {
              success: d.success === true,
              message: String(d.message || ''),
              finished_at: d.finished_at || null,
            };
            pending.delete(row.host);
          }
        });
        if (!recordAndMaybeFinish()) {
          setTimeout(tick, INTERVAL_MS);
        }
      });
    }
    // First poll fires after a short delay so the agent has a moment to
    // call _set_in_progress() before we read.
    setTimeout(tick, 1000);
  }

  function renderApplyOutcomeToast(hosts, outcomes, profile, action) {
    var ok = 0, fail = 0, unknown = 0;
    var label = String(action || 'apply').toUpperCase();
    var lines = [label + (profile ? ' (' + profile + ')' : '') + ' result:'];
    hosts.forEach(function (h) {
      var o = outcomes[h];
      if (!o) {
        lines.push('  ? ' + h + ': still running — check the card badge');
        unknown++;
      } else if (o.success) {
        lines.push('  ✓ ' + h + ': ' + (o.message || 'applied'));
        ok++;
      } else {
        lines.push('  ✗ ' + h + ': ' + (o.message || 'failed'));
        fail++;
      }
    });
    var level = fail ? 'crit' : (unknown ? 'warn' : 'ok');
    showToast(lines.join('\n'), level);
    setTimeout(function () { softRefreshFleet('Apply finished — view refreshed'); }, 1500);
  }

  /* -----------------------------------------------------------
   *  v0.8.2: live job badge, chain line, list-row updates and the
   *  soft fleet refresh. Keep the DOM writes idempotent — the SSE
   *  frame arrives every 5 s.
   * --------------------------------------------------------- */
  function jobIsRecent(iso, ms) {
    if (!iso) return false;
    var t = Date.parse(iso);
    return !isNaN(t) && (Date.now() - t) < ms;
  }

  function renderJobBadge(el, job, active) {
    job = job || {};
    var state = '', text = '', title = '';
    if (job.in_progress || active) {
      state = 'running';
      text = '⟳ ' + (job.action || 'working') + (job.step ? ' · ' + job.step : '');
      title = job.profile || '';
    } else if (job.known && job.success === false) {
      state = 'fail';
      text = '✖ ' + (job.action || 'job') + ' failed';
      title = job.message || '';
    } else if (job.known && job.success === true && jobIsRecent(job.finished_at, 120000)) {
      state = 'ok';
      text = '✔ ' + (job.action || 'job') + ' ok';
      title = job.message || '';
    }
    if (el.getAttribute('data-state') !== state) el.setAttribute('data-state', state);
    if (el.textContent !== text) el.textContent = text;
    if (el.title !== title) el.title = title;
    el.style.display = state ? '' : 'none';
  }

  function renderChain(el, chain) {
    var text = '';
    if (chain.upstream) text = '↑ ' + chain.upstream;
    if (chain.detail) text = (text ? text + ' · ' : '') + chain.detail;
    el.setAttribute('data-level', chain.level || 'ok');
    if (el.textContent !== text) el.textContent = text;
    el.style.display = text ? '' : 'none';
  }

  function updateListRow(data) {
    var row = document.querySelector('.node-row[data-hostname="' + cssEscape(data.hostname) + '"]');
    if (!row) return;
    function cell(name) { return row.querySelector('td[data-field="' + name + '"]'); }
    var c;
    if ((c = cell('profile')) && data.active_profile != null) {
      var p = data.active_profile || '—';
      if (c.textContent.trim() !== p) c.textContent = p;
      c.setAttribute('data-sort', (data.active_profile || '~zzz').toLowerCase());
    }
    var q = data.subsystems && data.subsystems.qsync;
    if ((c = cell('qsync')) && q && q.level) {
      c.setAttribute('data-level', q.level);
      c.setAttribute('data-sort', String({ crit: 3, warn: 2, ok: 1, off: 0 }[q.level] || 0));
      var qt = q.level.toUpperCase();
      if (c.textContent.trim() !== qt) c.textContent = qt;
      c.title = 'QSync: ' + (q.status || '');
    }
    if ((c = cell('role')) && data.qsync_role != null) {
      var r = data.qsync_role ? String(data.qsync_role).toUpperCase() : '—';
      if (c.textContent.trim() !== r) c.textContent = r;
    }
    if ((c = cell('refresh')) && data.refresh_hz != null) {
      var hz = Number(data.refresh_hz);
      var ht = hz ? hz.toFixed(3) + ' Hz' : '—';
      if (c.textContent.trim() !== ht) c.textContent = ht;
      c.setAttribute('data-sort', String(hz || 0));
    }
    if ((c = cell('displays')) && data.displays_count != null) {
      var d = String(data.displays_count);
      if (c.textContent.trim() !== d) c.textContent = d;
      c.setAttribute('data-sort', d);
    }
    if ((c = cell('color')) && data.color_text != null) {
      var ct = data.color_text || '—';
      if (c.textContent.trim() !== ct) c.textContent = ct;
      c.setAttribute('data-sort', (data.color_text || '~').toLowerCase());
    }
    var jb = row.querySelector('[data-field="job"]');
    if (jb) renderJobBadge(jb, data.job, data.job_active);
  }

  /* softRefreshFleet — re-fetch the dashboard and swap only the #fleet
     block (cards + list) so server-rendered conditionals (validation,
     role, chain, job banner) catch up without a full page reload. Skipped
     while a modal / slide-over is open, while the grid has keyboard
     focus, when the tab is hidden, and at most once every 3 s. */
  var _lastFleetRefresh = 0;
  function softRefreshFleet(reason) {
    if (!window.htmx || document.hidden) return;
    var fleet = document.getElementById('fleet');
    if (!fleet) return;
    var modal = document.getElementById('modal-portal');
    var slide = document.getElementById('slideover-portal');
    if ((modal && modal.firstElementChild) || (slide && slide.firstElementChild)) return;
    if (document.activeElement && fleet.contains(document.activeElement)) return;
    if (Date.now() - _lastFleetRefresh < 3000) return;
    _lastFleetRefresh = Date.now();
    try {
      window.htmx.ajax('GET', location.pathname + location.search, {
        target: '#fleet', select: '#fleet', swap: 'outerHTML',
      });
      if (reason) announce(reason);
    } catch (e) { /* best effort */ }
  }
  window.nvmonSoftRefresh = softRefreshFleet;

  /* showToast — minimal corner toast. Three levels (ok/warn/crit) map
     to existing CSS color tokens. Click to dismiss; auto-fades at 8s. */
  function showToast(text, level) {
    var t = document.createElement('div');
    t.className = 'nvmon-toast ' + (level || 'ok');
    t.textContent = text;
    t.title = 'click to dismiss';
    t.onclick = function () { t.remove(); };
    document.body.appendChild(t);
    setTimeout(function () { try { t.remove(); } catch (_) {} }, 8000);
  }
  window.nvmonToast = showToast;  // expose for other scripts

  /* nvmonCancelApply — POST /node/<host>/apply/cancel. Stops the running
     apply / restore / boot-apply on that node at its next step. Plain JS
     (no Alpine) like the other slide-over actions. The slide-over is
     re-fetched after a moment so the job banner reflects the new state. */
  function nvmonCancelApply(host) {
    if (!host) return;
    fetch('/node/' + encodeURIComponent(host) + '/apply/cancel', {
      method: 'POST',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
      body: '{}',
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        // The controller passthrough answers with the fan-out tally:
        // {results:[{hostname, status, http_code, body:{cancelling,...}, error?}]}
        var res = (j && j.results && j.results[0]) || j || {};
        var body = res.body || {};
        var cancelling = body.cancelling === true;
        var why = body.error || res.error || '';
        showToast(cancelling ? 'CANCEL sent to ' + host + ' — the job stops at its next step'
                             : 'Nothing running on ' + host + (why ? ' (' + why + ')' : ''),
                  cancelling ? 'warn' : 'ok');
        setTimeout(function () {
          var portal = document.getElementById('slideover-portal');
          if (portal && portal.innerHTML) {
            fetch('/node/' + encodeURIComponent(host) + '/detail').then(function (r) { return r.text(); })
              .then(function (html) { if (window.mountInjected) window.mountInjected(portal, html); else portal.innerHTML = html; })
              .catch(function () {});
          }
        }, 2500);
      });
    }).catch(function (e) { showToast('Cancel failed: ' + e, 'crit'); });
  }
  window.nvmonCancelApply = nvmonCancelApply;

  /* nvmonRemoveFromRegistry — DELETE /register/<host> for an operator-
     visible "remove this push-registered node from the dashboard" action.
     The button only renders in the slide-over when node.source ==
     "registered" (see node_detail.html). Auth piggybacks on the existing
     nvmon_session cookie that the controller's login flow set; the cookie
     is HttpOnly so JS can't read it but the browser will attach it
     automatically. If the controller is in no-auth mode the cookie is
     irrelevant and the request still succeeds. Plain JS + window.confirm
     mirroring the sync/restore pattern — no Alpine, no modal-stage
     transitions, click definitively does something. */
  function nvmonRemoveFromRegistry(hostname) {
    if (!hostname) return;
    var msg = 'Remove ' + hostname + ' from registry?\n\n' +
              'It will reappear automatically if its listener heartbeats ' +
              'this controller again. Use this to drop nodes that are ' +
              'permanently retired or that re-registered from a stale URL.';
    if (!window.confirm(msg)) return;
    fetch('/register/' + encodeURIComponent(hostname), { method: 'DELETE' })
      .then(function (r) {
        return r.json().catch(function () { return {}; })
          .then(function (j) { return { ok: r.ok, status: r.status, data: j }; });
      })
      .then(function (resp) {
        if (resp.ok) {
          showToast('Removed ' + hostname + ' from registry.', 'ok');
          // Soft reload so the card disappears from the grid + the
          // slide-over closes. setTimeout gives the toast a moment to
          // paint before the navigation tears the DOM down.
          setTimeout(function () { window.location.reload(); }, 600);
          return;
        }
        var err = (resp.data && resp.data.error) || ('HTTP ' + resp.status);
        showToast('Remove failed: ' + err, 'crit');
      })
      .catch(function (e) {
        showToast('Remove request failed: ' + e, 'crit');
      });
  }
  window.nvmonRemoveFromRegistry = nvmonRemoveFromRegistry;

  /* -----------------------------------------------------------
   *  mountInjected — wire up dynamically-injected HTML so HTMX
   *  rescans hx-* attributes AND Alpine binds the new x-data
   *  trees. Without the Alpine.initTree call, the fanout modal
   *  rendered as two empty grey bars (x-data set but never bound)
   *  and the slide-over's Diagnose buttons no-op'd.
   * --------------------------------------------------------- */
  function mountInjected(portal, html) {
    if (!portal) return;
    portal.innerHTML = html;
    if (window.htmx && typeof window.htmx.process === 'function') {
      try { window.htmx.process(portal); } catch (_) {}
    }
    if (window.Alpine && typeof window.Alpine.initTree === 'function') {
      try { window.Alpine.initTree(portal); } catch (_) {}
    }
  }

  /* -----------------------------------------------------------
   *  Helpers
   * --------------------------------------------------------- */
  function readJSON(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }
  function cssEscape(s) {
    return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/["\\]/g, '\\$&');
  }
  function announce(msg) {
    var lr = document.getElementById('live-region');
    if (lr) lr.textContent = msg;
  }

  // Expose a minimal hook for charts.js and test harnesses
  window.nvmon = {
    renderSparklines: renderSparklines,
    SSEClient: SSEClient,
    FanoutWS: FanoutWS
  };

  /* -----------------------------------------------------------
   *  Boot
   * --------------------------------------------------------- */
  function boot() {
    // One-shot reset of the visibility toggles. The previous boot logic
    // auto-migrated the legacy `nvmon-show-optional` key into both
    // `nvmon-show-dd` and `nvmon-show-mosaic` set to '1', which left
    // operators staring at idle DD/Mosaic cards they never asked for.
    // We can't tell apart "user explicitly ticked the new toggle" from
    // "old migration left it on", so we wipe both keys ONCE on first
    // boot of this client, keyed by a schema-version sentinel. After
    // the wipe runs, the user's manual toggle state is honored
    // normally on every subsequent load.
    var TOGGLE_SCHEMA = 'v2';
    if (localStorage.getItem('nvmon-toggle-schema') !== TOGGLE_SCHEMA) {
      localStorage.removeItem('nvmon-show-optional');  // legacy
      localStorage.removeItem('nvmon-show-dd');
      localStorage.removeItem('nvmon-show-mosaic');
      localStorage.removeItem('nvmon-show-edid');
      localStorage.setItem('nvmon-toggle-schema', TOGGLE_SCHEMA);
    }

    // Apply the persisted per-subsystem visibility flags before paint so
    // toggle states match the rendered cards. Cards force-show on a
    // given node when the subsystem is actually doing work — the
    // template omits the `optional` class in that case — so these flags
    // only affect idle cards.
    if (localStorage.getItem('nvmon-show-dd')     === '1') document.body.classList.add('show-dd');
    if (localStorage.getItem('nvmon-show-mosaic') === '1') document.body.classList.add('show-mosaic');
    if (localStorage.getItem('nvmon-show-edid')   === '1') document.body.classList.add('show-edid');
    // Apply the persisted view mode (grid|list|auto; auto when never
    // picked). Without this restoration the toggle would visually un-stick
    // on every reload.
    applyView(storedView());
    renderSparklines(document);
    bindKeybindings();
    bindListSort();
    if (document.querySelector('main') && !window.__nvmonNoSSE) {
      bindSSE();
    }
  }

  /* -----------------------------------------------------------
   *  bindListSort — column-click sort for the list-view table.
   *  Each <th> has data-col + data-sort-type (text|num|level).
   *  Each <td> in the matching column has data-sort with the
   *  raw value (numeric where it makes sense). Clicking a header
   *  reorders <tr>s in place — first click = asc, second = desc,
   *  third = original DOM order. Sort state lives in localStorage
   *  so it survives reloads.
   * --------------------------------------------------------- */
  function bindListSort() {
    var table = document.getElementById('node-list');
    if (!table) return;
    var thead = table.tHead;
    var tbody = table.tBodies[0];
    if (!thead || !tbody) return;

    // Snapshot the original DOM order so a third click restores it.
    var originalOrder = Array.prototype.slice.call(tbody.children);

    // Restore persisted sort if any.
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem('nvmon-list-sort') || 'null'); } catch (_) {}
    if (saved && saved.col && saved.dir) {
      applySort(saved.col, saved.dir);
    } else {
      // Default: hostname asc, matches the natural-sorted server order.
      applySort('hostname', 'asc');
    }

    Array.prototype.forEach.call(thead.querySelectorAll('th[data-col]'), function (th) {
      th.addEventListener('click', function () {
        var col = th.getAttribute('data-col');
        var current = th.getAttribute('data-sort-dir');
        var nextDir = current === 'asc' ? 'desc' : (current === 'desc' ? 'reset' : 'asc');
        applySort(col, nextDir);
        if (nextDir === 'reset') {
          localStorage.removeItem('nvmon-list-sort');
        } else {
          localStorage.setItem('nvmon-list-sort', JSON.stringify({col: col, dir: nextDir}));
        }
      });
    });

    function applySort(col, dir) {
      // Clear all indicators first.
      Array.prototype.forEach.call(thead.querySelectorAll('th[data-col]'), function (th) {
        th.removeAttribute('data-sort-dir');
      });
      if (dir === 'reset') {
        originalOrder.forEach(function (row) { tbody.appendChild(row); });
        return;
      }
      var th = thead.querySelector('th[data-col="' + col + '"]');
      if (!th) return;
      th.setAttribute('data-sort-dir', dir);
      var idx = Array.prototype.indexOf.call(thead.rows[0].cells, th);
      var sortType = th.getAttribute('data-sort-type') || 'text';
      var rows = Array.prototype.slice.call(tbody.children);
      rows.sort(function (a, b) {
        var av = a.cells[idx].getAttribute('data-sort');
        var bv = b.cells[idx].getAttribute('data-sort');
        var cmp;
        if (sortType === 'num' || sortType === 'level') {
          cmp = parseFloat(av || '0') - parseFloat(bv || '0');
        } else {
          // Text: stable lexical compare on lowercased value.
          if (av === bv) cmp = 0;
          else cmp = (av < bv) ? -1 : 1;
        }
        return dir === 'asc' ? cmp : -cmp;
      });
      rows.forEach(function (row) { tbody.appendChild(row); });
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
