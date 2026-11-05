/* ============================================================
   charts.js — Chart.js integration for node detail view

   Controller contract (required from A3):
     GET /history/<hostname>?metric=<name>&window=1h
        → 200 application/json
        → {
            "hostname": "RN-02",
            "metric":   "temperature_c",
            "window_seconds": 3600,
            "samples": [{"t": "2026-04-17T10:00:00Z", "v": 58}, ...]
          }

   Metrics we plot:
     - temperature_c     (GPU core temperature, °C)
     - power_draw_w      (instantaneous draw, W)
     - gpu_utilization_pct
     - qsync_synced      (0/1)

   Live updates:
     nvmon.js dispatches `nvmon:detail-opened` when a slide-over
     renders. We wire up one chart per <canvas data-chart=...>.
     Subsequent SSE node_update events push new samples to the
     owning chart and roll the window forward (720 samples = 1h
     at 5s poll interval).

   Charts are disposed on `nvmon:detail-closed`.
   ============================================================ */

(function () {
  'use strict';

  /** @type {Map<string, {chart: any, hostname: string, metric: string}>} */
  var charts = new Map();

  var brand = getCssVar('--brand') || '#76b900';
  var warnC = getCssVar('--warn')  || '#ffaa00';
  var critC = getCssVar('--crit')  || '#ff4444';
  var text  = getCssVar('--text')  || '#e0e0e0';
  var grid  = getCssVar('--border')|| '#2a2a2a';

  window.addEventListener('nvmon:detail-opened', function (ev) {
    var root = ev.detail.root;
    var host = ev.detail.hostname;
    root.querySelectorAll('canvas[data-chart]').forEach(function (canvas) {
      var metric = canvas.getAttribute('data-chart');      // 'temperature' | 'power'
      var apiMetric = metricToApi(metric);
      loadAndRender(canvas, host, apiMetric);
    });
  });

  window.addEventListener('nvmon:detail-closed', function () {
    charts.forEach(function (entry) {
      try { entry.chart.destroy(); } catch (_) {}
    });
    charts.clear();
  });

  // When SSE pushes a new node_update, append it to the open charts.
  document.addEventListener('nvmon:node-update', function (ev) {
    var data = ev.detail;
    charts.forEach(function (entry) {
      if (entry.hostname !== data.hostname) return;
      var sample = extractSample(data, entry.metric);
      if (sample == null) return;
      var chart = entry.chart;
      var labels = chart.data.labels;
      var values = chart.data.datasets[0].data;
      labels.push(formatHM(new Date()));
      values.push(sample);
      if (values.length > 720) { labels.shift(); values.shift(); }
      chart.update('none');
    });
  });

  function metricToApi(m) {
    if (m === 'temperature') return 'temperature_c';
    if (m === 'power')       return 'power_draw_w';
    if (m === 'util')        return 'gpu_utilization_pct';
    return m;
  }

  function extractSample(update, metric) {
    if (metric === 'temperature_c') return update.gpu && update.gpu[0] && update.gpu[0].temperature_c;
    if (metric === 'power_draw_w')  return update.gpu && update.gpu[0] && update.gpu[0].power_draw_w;
    return null;
  }

  function loadAndRender(canvas, hostname, metric) {
    if (typeof Chart === 'undefined') {
      drawFallback(canvas, 'Chart.js not loaded');
      return;
    }
    var url = '/history/' + encodeURIComponent(hostname) +
              '?metric=' + encodeURIComponent(metric) + '&window=1h';
    fetch(url).then(function (r) {
      if (!r.ok) throw new Error('history HTTP ' + r.status);
      return r.json();
    }).then(function (data) {
      render(canvas, hostname, metric, data.samples || []);
    }).catch(function (err) {
      drawFallback(canvas, 'No history (' + err.message + ')');
    });
  }

  function render(canvas, hostname, metric, samples) {
    // Pre-format labels to "HH:mm" strings rather than Date objects.
    // Chart.js v4 with `scales.x.type: 'time'` requires a date adapter
    // library (chartjs-adapter-date-fns / -luxon / -moment) that we do
    // NOT vendor — without one, the Chart constructor throws and the
    // canvas renders blank. Using a category scale with formatted strings
    // avoids the dependency entirely. We still pass through `s.t` for
    // tooltips so the operator can hover and see the full timestamp.
    var labels = samples.map(function (s) { return formatHM(new Date(s.t)); });
    var values = samples.map(function (s) { return s.v; });
    var fullTimes = samples.map(function (s) { return new Date(s.t).toISOString(); });

    if (samples.length === 0) {
      drawFallback(canvas, 'No samples yet (history is still warming up)');
      return;
    }

    var lineColor = brand;
    if (metric === 'power_draw_w') lineColor = warnC;

    // Chart.js refuses to bind a new chart to a canvas that already holds
    // one — "Chart with ID 'N' must be destroyed before the canvas can be
    // reused". This fires every time the slide-over reopens for the same
    // node. Destroy any prior instance first (both via our Map and via
    // Chart.getChart() in case the slide-over was re-rendered without a
    // detail-closed event firing).
    if (typeof Chart.getChart === 'function') {
      var existing = Chart.getChart(canvas);
      if (existing) { try { existing.destroy(); } catch (_) {} }
    }
    var prev = charts.get(canvas.id);
    if (prev) { try { prev.chart.destroy(); } catch (_) {} charts.delete(canvas.id); }

    var chart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          label: metric,
          data: values,
          borderColor: lineColor,
          backgroundColor: alpha(lineColor, 0.15),
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 3,
          tension: 0.2,
          fill: true
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#111',
            borderColor: grid,
            borderWidth: 1,
            titleColor: text,
            bodyColor: text
          }
        },
        scales: {
          x: {
            type: 'category',
            grid: { color: grid, drawTicks: false },
            ticks: { color: '#888', maxRotation: 0, autoSkip: true, maxTicksLimit: 6 }
          },
          y: (function () {
            // Temperature gets a fixed operational range so an integer-
            // quantized signal (NVML reports °C as whole numbers) doesn't
            // look like a 100% spike every time it ticks 32→33→32. The
            // user reported the chart looked dramatically spikier than
            // power because Chart.js was auto-fitting Y to the 32-33
            // band — making 1°C dips fill the whole panel. With a fixed
            // 25-95°C range a 1°C swing is ~1.5% of the axis, matching
            // the visual smoothness of the power chart's 0-100W range.
            // suggestedMin/Max (vs. min/max) lets the axis auto-extend
            // for a thermal runaway above 95°C instead of clipping.
            if (metric === 'temperature_c') {
              return {
                suggestedMin: 25,
                suggestedMax: 95,
                grid: { color: grid },
                ticks: { color: '#888', stepSize: 10 },
              };
            }
            return {
              beginAtZero: metric === 'power_draw_w',
              grid: { color: grid },
              ticks: { color: '#888' },
            };
          })()
        }
      }
    });

    charts.set(canvas.id, {
      chart: chart, hostname: hostname, metric: metric,
      fullTimes: fullTimes,
    });
  }

  function formatHM(d) {
    if (!(d instanceof Date) || isNaN(d.getTime())) return '';
    var hh = String(d.getHours()).padStart(2, '0');
    var mm = String(d.getMinutes()).padStart(2, '0');
    return hh + ':' + mm;
  }

  function drawFallback(canvas, msg) {
    var ctx = canvas.getContext('2d');
    var w = canvas.width  = canvas.offsetWidth;
    var h = canvas.height = canvas.offsetHeight || 120;
    ctx.fillStyle = '#111'; ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = grid;  ctx.strokeRect(0.5, 0.5, w - 1, h - 1);
    ctx.fillStyle = '#888'; ctx.font = '12px Consolas, monospace';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(msg, w / 2, h / 2);
  }

  function alpha(color, a) {
    // Accept #rgb, #rrggbb, or pass-through.
    if (!/^#/.test(color)) return color;
    var c = color.replace('#', '');
    if (c.length === 3) c = c.split('').map(function (x) { return x + x; }).join('');
    var r = parseInt(c.substr(0, 2), 16);
    var g = parseInt(c.substr(2, 2), 16);
    var b = parseInt(c.substr(4, 2), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
  }

  function getCssVar(name) {
    try {
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    } catch (_) { return ''; }
  }
})();
