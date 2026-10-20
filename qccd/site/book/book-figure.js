// The book's figures: a certificate as Lean replays it, drawn on its device.
//
// Every number drawn here comes from Lean -- QCCDBook.figure (engine/figure.lean) runs the
// checker's own posAt / look / ionOf / check and prints the result -- so a figure shows what is
// being proved, never a second implementation of it.  The only thing computed here is the
// drawing: where a trap sits on the page, and the rail an ion slides along between two cycles.
(function (global) {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var QCOL = ['#1f5bb5', '#d1631a', '#2f8f5b', '#7b2fb5', '#b5341f', '#0f8a8a', '#8a6d1f', '#5a5a66'];
  var OK = '#2f8f5b', BAD = '#c0392b';

  function el(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function h(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function label(name) { return { cx: 'CX', cz: 'CZ', h: 'H', x: 'X', y: 'Y', z: 'Z', s: 'S', t: 'T', tdg: 'T†', sdg: 'S†', swap: 'SWAP', measure: 'M', reset: '|0⟩', rx: 'RX', ry: 'RY', rz: 'RZ' }[name] || name.toUpperCase(); }

  // ------------------------------------------------------------------ the device on the page
  function layout(dev, width) {
    var xs = dev.nodes.map(function (n) { return n[1]; }), ys = dev.nodes.map(function (n) { return n[2]; });
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    var pad = 46, dx = Math.max(x1 - x0, 1e-9), dy = Math.max(y1 - y0, 1e-9);
    var unit = Math.min(150, (width - 2 * pad) / dx, y1 > y0 ? 230 / dy : 1e9);
    var at = {}, kind = {}, gate = {};
    dev.nodes.forEach(function (n) {
      at[n[0]] = [pad + (n[1] - x0) * unit, pad + (n[2] - y0) * unit];
      kind[n[0]] = n[3]; gate[n[0]] = n[4];
    });
    var adj = {};
    dev.segs.forEach(function (s) { (adj[s[0]] = adj[s[0]] || []).push(s[1]); (adj[s[1]] = adj[s[1]] || []).push(s[0]); });
    return { at: at, kind: kind, gate: gate, adj: adj, w: Math.max(width, pad * 2 + dx * unit), h: pad * 2 + (y1 > y0 ? dy * unit : 0), unit: unit };
  }
  // the rails an ion takes from a to b: the shortest path through the device's segments
  function route(L, a, b) {
    if (a === b || !L.at[a] || !L.at[b]) return [a, b];
    var prev = {}, q = [a], seen = {}; seen[a] = 1;
    while (q.length) {
      var u = q.shift();
      if (u === b) break;
      (L.adj[u] || []).forEach(function (v) { if (!seen[v]) { seen[v] = 1; prev[v] = u; q.push(v); } });
    }
    if (!seen[b]) return [a, b];
    var path = [b];
    while (path[0] !== a) path.unshift(prev[path[0]]);
    return path;
  }
  function along(L, path, f) {
    var pts = path.map(function (n) { return L.at[n]; }), lens = [], tot = 0;
    for (var i = 1; i < pts.length; i++) { var d = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]); lens.push(d); tot += d; }
    if (!tot) return pts[0];
    var want = f * tot;
    for (i = 1; i < pts.length; i++) {
      if (want <= lens[i - 1] || i === pts.length - 1) {
        var g = lens[i - 1] ? Math.min(1, want / lens[i - 1]) : 1;
        return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * g, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * g];
      }
      want -= lens[i - 1];
    }
    return pts[pts.length - 1];
  }

  // ------------------------------------------------------------------ one figure
  var uid = 0;
  function Figure(root, opts) {
    this.root = root; this.opts = opts || {};
    this.arrow = 'figarrow' + (++uid);
    this.t = +(root.getAttribute('data-at') || 0);
    this.playing = false;
    root.innerHTML = '';
    this.head = h('div', 'fighead'); root.appendChild(this.head);
    var body = h('div', 'figbody'); root.appendChild(body);
    this.dev = el('svg', { class: 'figdev', role: 'img' }, body);
    this.circ = el('svg', { class: 'figcirc', role: 'img' }, body);
    var ctl = h('div', 'figctl'); root.appendChild(ctl);
    var self = this;
    function btn(txt, title, fn) { var b = h('button', '', txt); b.type = 'button'; b.title = title; b.addEventListener('click', fn); ctl.appendChild(b); return b; }
    btn('⏮', 'Back to cycle 0', function () { self.stop(); self.seek(0); });
    btn('◀', 'One cycle back', function () { self.stop(); self.seek(Math.max(0, Math.ceil(self.t) - 1)); });
    this.playBtn = btn('▶', 'Play', function () { self.playing ? self.stop() : self.play(); });
    btn('▶|', 'One cycle on', function () { self.stop(); self.seek(Math.min(self.fig.T, Math.floor(self.t) + 1)); });
    this.slider = h('input'); this.slider.type = 'range'; this.slider.min = 0; this.slider.step = 'any';
    this.slider.setAttribute('aria-label', 'cycle');
    this.slider.addEventListener('input', function () { self.stop(); self.seek(+self.slider.value); });
    ctl.appendChild(this.slider);
    this.cyc = h('span', 'figcyc'); ctl.appendChild(this.cyc);
    this.list = h('div', 'figchecks'); root.appendChild(this.list);
  }
  Figure.prototype.update = function (fig, device) {
    this.fig = fig; this.device = device;
    // the circuit keeps its natural width beside the device; the device takes what is left
    var circuitW = 40 + 42 * fig.ops.length + 24;
    var width = Math.max(260, Math.min(560, this.root.clientWidth - circuitW - 50));
    this.L = layout(device, width);
    this.slider.max = fig.T;
    this.ionQ = {};
    fig.qmap.forEach(function (p) { this.ionQ[p[1]] = p[0]; }, this);
    this.t = Math.min(this.t, fig.T);
    this.drawHead(); this.drawChecks(); this.seek(this.t);
  };
  Figure.prototype.drawHead = function () {
    var f = this.fig;
    this.head.innerHTML = '';
    this.head.appendChild(h('b', '', this.root.getAttribute('data-name')));
    this.head.appendChild(document.createTextNode(' on ' + f.arch + ', replayed by Lean. '));
    var v = h('span', 'figverdict ' + (f.check ? 'ok' : 'bad'), f.check ? 'check = true: the certificate holds' : 'check = false: the checker refuses it');
    this.head.appendChild(v);
  };
  Figure.prototype.drawChecks = function () {
    var f = this.fig, self = this, rows = [];
    f.moves.forEach(function (m) {
      rows.push({ t: m[0], text: 'cycle ' + m[0] + ': ' + m[1] + ' moves ' + m[2] + ' → ' + m[3],
        chips: [[m[4], m[4] ? 'a hop of the device' : 'NOT a hop of the device'],
                [m[5], m[5] ? 'starts where it is' : m[1] + ' is not in ' + m[2]]] });
    });
    f.gates.forEach(function (g) {
      var together = g[4].every(function (s) { return s === g[2]; });
      var where = g[3].map(function (ion, i) { return ion + ' is in ' + g[4][i]; }).join(', ');
      rows.push({ t: g[1], text: 'cycle ' + g[1] + ': ' + label(g[7]) + ' (op ' + g[0] + ') in ' + g[2] + ' on ' + g[3].join(', '),
        chips: [[together, together ? 'ions together in ' + g[2] : where],
                [g[5], g[5] ? 'the trap can gate' : g[2] + ' cannot gate'],
                [g[6], g[6] ? "the op's own ions" : "not the op's ions"]] });
    });
    (f.rots || []).forEach(function (r) {
      rows.push({ t: r[0], text: 'cycle ' + r[0] + ': loop ' + r[1] + ' turns ' + (r[2] > 0 ? '+' : '') + r[2]
        + ' (every ion on it moves ' + Math.abs(r[2]) + ' slot' + (Math.abs(r[2]) === 1 ? '' : 's') + ')', chips: [] });
    });
    f.ops.forEach(function (o) {
      var needs = ['measure', 'reset', 'barrier'].indexOf(o[1]) < 0;
      if (needs && o[3] !== 1) rows.push({ t: 0, text: 'op ' + o[0] + ' (' + label(o[1]) + ')', chips: [[false, o[3] + ' witnesses, not 1']] });
    });
    rows.sort(function (a, b) { return a.t - b.t; });
    this.list.innerHTML = '';
    this.rows = rows.map(function (r) {
      var d = h('div', 'figrow');
      d.appendChild(h('span', 'figrowt', r.text));
      r.chips.forEach(function (c) { d.appendChild(h('span', 'chip ' + (c[0] ? 'ok' : 'bad'), (c[0] ? '✓ ' : '✗ ') + c[1])); });
      d.addEventListener('click', function () { self.stop(); self.seek(r.t); });
      d.title = 'Show cycle ' + r.t;
      self.list.appendChild(d);
      return { t: r.t, el: d };
    });
  };
  Figure.prototype.play = function () {
    var self = this;
    if (this.t >= this.fig.T) this.t = 0;
    this.playing = true; this.playBtn.textContent = '❚❚'; this.playBtn.title = 'Pause';
    var last = performance.now();
    function tick(now) {
      if (!self.playing) return;
      self.seek(Math.min(self.fig.T, self.t + (now - last) / 650));
      last = now;
      if (self.t >= self.fig.T) { self.stop(); return; }
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  };
  Figure.prototype.stop = function () { this.playing = false; this.playBtn.textContent = '▶'; this.playBtn.title = 'Play'; };
  Figure.prototype.seek = function (t) {
    this.t = t; this.slider.value = t;
    var c = Math.floor(t + 1e-9);
    this.cyc.textContent = 'cycle ' + c + ' of ' + this.fig.T;
    this.rows.forEach(function (r) { r.el.classList.toggle('now', r.t === c); });
    this.drawDevice(t); this.drawCircuit(c);
  };

  // where every ion is at time t (a cycle and a fraction of the next), as page coordinates
  Figure.prototype.ionPoints = function (t) {
    var f = this.fig, L = this.L, c = Math.min(Math.floor(t), f.T), frac = t - c;
    var now = f.pos[c] || [], next = f.pos[Math.min(c + 1, f.T)] || now;
    var nextAt = {}; next.forEach(function (p) { nextAt[p[0]] = p[1]; });
    function slots(state) {
      var by = {}, out = {};
      state.forEach(function (p) { (by[p[1]] = by[p[1]] || []).push(p[0]); });
      Object.keys(by).forEach(function (n) { by[n].sort().forEach(function (ion, i, a) { out[ion] = (i - (a.length - 1) / 2) * 22; }); });
      return out;
    }
    var s0 = slots(now), s1 = slots(next), out = [];
    now.forEach(function (p) {
      var ion = p[0], a = p[1], b = nextAt[ion] || a, xy;
      // an ion slides along the rails only in the second half of a cycle that moves it
      var g = frac < 0.5 ? 0 : (frac - 0.5) * 2;
      xy = a === b ? L.at[a] : along(L, route(L, a, b), g);
      if (!xy) return;
      var off = (s0[ion] || 0) * (1 - g) + (s1[ion] || 0) * g;
      out.push({ ion: ion, x: xy[0] + off, y: xy[1], moving: a !== b && frac >= 0.5, at: a });
    });
    return out;
  };
  Figure.prototype.drawDevice = function (t) {
    var svg = this.dev, L = this.L, f = this.fig, dev = this.device, c = Math.floor(t + 1e-9), arrow = this.arrow;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.setAttribute('viewBox', '0 0 ' + L.w + ' ' + (L.h + 30));
    svg.setAttribute('width', L.w); svg.setAttribute('height', L.h + 30);
    dev.segs.forEach(function (s) {
      var a = L.at[s[0]], b = L.at[s[1]];
      el('line', { x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: '#cfccc2', 'stroke-width': 5, 'stroke-linecap': 'round' }, svg);
    });
    dev.nodes.forEach(function (n) {
      var p = L.at[n[0]];
      if (n[3] === 'junction') { el('circle', { cx: p[0], cy: p[1], r: 6, fill: '#efeee9', stroke: '#b9b5a9' }, svg); return; }
      el('rect', { x: p[0] - 30, y: p[1] - 17, width: 60, height: 34, rx: 7, fill: n[4] ? '#f4f8fe' : '#f6f5f1',
        stroke: n[4] ? '#1f5bb5' : '#b9b5a9', 'stroke-width': n[4] ? 1.6 : 1 }, svg);
      var tx = el('text', { x: p[0], y: p[1] + 31, 'text-anchor': 'middle', 'font-size': 11, fill: '#56545e', 'font-family': 'ui-monospace,monospace' }, svg);
      tx.textContent = n[0];
    });
    var pts = this.ionPoints(t), byIon = {};
    pts.forEach(function (p) { byIon[p.ion] = p; });
    // a move claimed this cycle that is not a hop: the claim drawn as the certificate states it
    f.moves.forEach(function (m) {
      if (m[0] !== c) return;
      var a = L.at[m[2]], b = L.at[m[3]];
      if (!a || !b) return;
      var bad = !m[4] || !m[5];
      el('line', { x1: a[0], y1: a[1] - 24, x2: b[0], y2: b[1] - 24, stroke: bad ? BAD : '#56545e', 'stroke-width': 1.6,
        'stroke-dasharray': bad ? '5 4' : '0', 'marker-end': 'url(#' + arrow + ')' }, svg);
      if (bad) {
        var tx = el('text', { x: (a[0] + b[0]) / 2, y: Math.min(a[1], b[1]) - 30, 'text-anchor': 'middle', 'font-size': 11.5, fill: BAD, 'font-weight': 600 }, svg);
        tx.textContent = !m[4] ? m[2] + ' → ' + m[3] + ' is not a hop' : m[1] + ' is not in ' + m[2];
      }
    });
    // a rotation this cycle: every ion on the loop advances together, named at the loop's middle
    (f.rots || []).forEach(function (r) {
      if (r[0] !== c) return;
      var lp = (dev.loops || []).filter(function (l) { return l.id === r[1]; })[0];
      if (!lp) return;
      var cx = 0, cy = 0;
      lp.nodes.forEach(function (n) { cx += L.at[n][0]; cy += L.at[n][1]; });
      cx /= lp.nodes.length; cy /= lp.nodes.length;
      var tx = el('text', { x: cx, y: cy + 4, 'text-anchor': 'middle', 'font-size': 13, 'font-weight': 700, fill: '#1f5bb5' }, svg);
      tx.textContent = '⟳ ' + r[1] + ' turns ' + (r[2] > 0 ? '+' : '') + r[2];
    });
    var defs = el('defs', {}, svg), mk = el('marker', { id: arrow, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, defs);
    el('path', { d: 'M0,0 L10,5 L0,10 z', fill: '#56545e' }, mk);
    // the gates witnessed at this cycle, and whether their ions are really there
    f.gates.forEach(function (g) {
      if (g[1] !== c) return;
      var p = L.at[g[2]];
      if (!p) return;
      var ok = g[4].every(function (s) { return s === g[2]; }) && g[5] && g[6];
      el('rect', { x: p[0] - 38, y: p[1] - 25, width: 76, height: 50, rx: 11, fill: 'none', stroke: ok ? OK : BAD, 'stroke-width': 3, opacity: 0.9 }, svg);
      var tx = el('text', { x: p[0], y: p[1] - 31, 'text-anchor': 'middle', 'font-size': 12.5, 'font-weight': 700, fill: ok ? OK : BAD }, svg);
      tx.textContent = label(g[7]);
      g[3].forEach(function (ion, i) {
        if (g[4][i] === g[2]) return;
        var q = byIon[ion];
        if (!q) return;
        el('line', { x1: p[0], y1: p[1], x2: q.x, y2: q.y, stroke: BAD, 'stroke-width': 1.5, 'stroke-dasharray': '4 3' }, svg);
        var w = el('text', { x: (p[0] + q.x) / 2, y: Math.max(p[1], q.y) + 48, 'text-anchor': 'middle', 'font-size': 11.5, fill: BAD, 'font-weight': 600 }, svg);
        w.textContent = ion + ' is in ' + g[4][i] + ', not ' + g[2];
      });
    });
    var ionQ = this.ionQ;
    pts.forEach(function (p) {
      var q = ionQ[p.ion], col = QCOL[(q == null ? 7 : q) % QCOL.length];
      el('circle', { cx: p.x, cy: p.y, r: 10, fill: col, stroke: '#fff', 'stroke-width': 2 }, svg);
      var tx = el('text', { x: p.x, y: p.y + 4, 'text-anchor': 'middle', 'font-size': 10, fill: '#fff', 'font-weight': 700, 'font-family': 'ui-monospace,monospace' }, svg);
      tx.textContent = q == null ? '?' : String(q);
    });
    svg.setAttribute('aria-label', 'The device ' + f.arch + ' at cycle ' + c + ': ' + pts.map(function (p) { return p.ion + ' at ' + p.at; }).join(', '));
  };
  Figure.prototype.drawCircuit = function (c) {
    var svg = this.circ, f = this.fig;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var nq = f.qmap.length;
    f.ops.forEach(function (o) { o[2].forEach(function (q) { nq = Math.max(nq, q + 1); }); });
    var col = 42, row = 34, x0 = 40, y0 = 26, w = x0 + col * f.ops.length + 24, ht = y0 + row * nq + 6;
    svg.setAttribute('viewBox', '0 0 ' + w + ' ' + ht); svg.setAttribute('width', w); svg.setAttribute('height', ht);
    var hd = el('text', { x: 4, y: 13, 'font-size': 11, fill: '#8a8892' }, svg);
    hd.textContent = 'the circuit (lit: the op witnessed at cycle ' + c + ')';
    for (var q = 0; q < nq; q++) {
      var y = y0 + row * q + row / 2;
      el('line', { x1: x0 - 6, y1: y, x2: w - 8, y2: y, stroke: '#b9b5a9' }, svg);
      var tq = el('text', { x: 4, y: y + 4, 'font-size': 11, fill: QCOL[q % QCOL.length], 'font-weight': 700, 'font-family': 'ui-monospace,monospace' }, svg);
      tq.textContent = 'q' + q;
    }
    var witnessT = {};
    f.gates.forEach(function (g) { (witnessT[g[0]] = witnessT[g[0]] || []).push(g[1]); });
    f.ops.forEach(function (o, i) {
      var x = x0 + col * i + col / 2, ts = witnessT[o[0]] || [];
      var state = ts.indexOf(c) >= 0 ? 'now' : ts.some(function (t) { return t < c; }) ? 'done' : (ts.length ? 'later' : 'none');
      var stroke = state === 'now' ? '#1f5bb5' : state === 'done' ? '#15141a' : '#b9b5a9';
      var fill = state === 'now' ? '#eaf0fa' : '#fff';
      var ys = o[2].map(function (q) { return y0 + row * q + row / 2; });
      if (o[1] === 'cx' && ys.length === 2) {
        el('line', { x1: x, y1: ys[0], x2: x, y2: ys[1], stroke: stroke, 'stroke-width': 1.6 }, svg);
        el('circle', { cx: x, cy: ys[0], r: 4.5, fill: stroke }, svg);
        el('circle', { cx: x, cy: ys[1], r: 9, fill: fill, stroke: stroke, 'stroke-width': 1.6 }, svg);
        el('line', { x1: x - 9, y1: ys[1], x2: x + 9, y2: ys[1], stroke: stroke, 'stroke-width': 1.6 }, svg);
        el('line', { x1: x, y1: ys[1] - 9, x2: x, y2: ys[1] + 9, stroke: stroke, 'stroke-width': 1.6 }, svg);
      } else {
        if (ys.length > 1) el('line', { x1: x, y1: Math.min.apply(null, ys), x2: x, y2: Math.max.apply(null, ys), stroke: stroke, 'stroke-width': 1.6 }, svg);
        ys.forEach(function (y) {
          el('rect', { x: x - 15, y: y - 11, width: 30, height: 22, rx: 4, fill: fill, stroke: stroke, 'stroke-width': state === 'now' ? 2.2 : 1.4 }, svg);
          var t = el('text', { x: x, y: y + 4, 'text-anchor': 'middle', 'font-size': 10.5, fill: stroke, 'font-weight': 700 }, svg);
          t.textContent = label(o[1]);
        });
      }
    });
    svg.setAttribute('aria-label', 'The circuit: ' + f.ops.map(function (o) { return label(o[1]) + ' ' + o[2].join(','); }).join('; '));
  };

  global.BookFigure = {
    create: function (root) { return new Figure(root); },
  };
})(window);
