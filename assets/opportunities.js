/* Funding Opportunities board.
 * Loads data/grants.json (written nightly by scripts/fetch_grants.py; only
 * OSP-verified calls) and renders a searchable snapshot table.
 * Deadlines are re-checked in the browser, so a round that passes between
 * nightly builds rolls forward to the next round or the call disappears.
 */
(function () {
  'use strict';
  var DAY = 86400000;
  var $ = function (id) { return document.getElementById(id); };
  var ui = {
    rows: $('fo-rows'), table: document.querySelector('.fo-table'), count: $('fo-count'),
    updated: $('fo-updated'), q: $('fo-search'), funder: $('fo-funder'), stage: $('fo-stage'),
    win: $('fo-window'), empty: $('fo-empty'), error: $('fo-error')
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function utc(iso) { var p = String(iso).split('-'); return Date.UTC(+p[0], +p[1] - 1, +p[2]); }
  function fmt(iso) {
    return new Date(utc(iso)).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  }
  function todayUTC() { var n = new Date(); return Date.UTC(n.getFullYear(), n.getMonth(), n.getDate()); }
  function safeUrl(u) { return /^https?:\/\//i.test(String(u || '')) ? u : ''; }
  function uniqSorted(list) {
    return list.filter(function (v, i) { return v && list.indexOf(v) === i; }).sort(function (a, b) { return a.localeCompare(b); });
  }
  function options(select, values) {
    values.forEach(function (v) { var o = document.createElement('option'); o.value = v; o.textContent = v; select.appendChild(o); });
  }

  /* Decide each call's live state for today. Returns null if it has closed. */
  function state(g, today) {
    if (g.rolling) return { rolling: true, days: null, next: null, later: 0 };
    var future = (g.deadlines || []).filter(function (d) { return utc(d) >= today; }).sort();
    if (!future.length) return null;
    return { rolling: false, next: future[0], later: future.length - 1, days: Math.round((utc(future[0]) - today) / DAY) };
  }

  function deadlineCell(g, s, today) {
    var h;
    if (s.rolling) {
      h = '<strong>Rolling</strong><span class="badge rolling">Apply anytime</span>';
    } else {
      var cls = s.days <= 7 ? ' urgent' : s.days <= 30 ? ' soon' : '';
      var txt = s.days === 0 ? 'Closes today' : s.days === 1 ? '1 day left' : s.days + ' days left';
      h = '<time datetime="' + esc(s.next) + '">' + esc(fmt(s.next)) + '</time>' +
          '<span class="badge' + cls + '">' + txt + '</span>';
      if (s.later) h += '<span class="small">+' + s.later + ' later round' + (s.later > 1 ? 's' : '') + '</span>';
      if (g.deadline_note) h += '<span class="small">' + esc(g.deadline_note) + '</span>';
    }
    if (g.opening_date && utc(g.opening_date) > today) h += '<span class="small">Opens ' + esc(fmt(g.opening_date)) + '</span>';
    return h;
  }

  function row(g, s, today) {
    var url = safeUrl(g.application_url);
    var stages = (g.career_stages || []).map(function (x) { return '<span class="chip">' + esc(x) + '</span>'; }).join('');
    var regions = (g.eligible_regions || []).map(function (x) { return '<span class="chip alt">' + esc(x) + '</span>'; }).join('');
    var tz = g.tz_eligibility === 'Eligible'
      ? '<span class="tz ok">Tanzania eligible</span>'
      : '<span class="tz pending">TZ eligibility: ' + esc(g.tz_eligibility || 'Pending assessment') + '</span>';
    var tr = document.createElement('tr');
    tr.innerHTML =
      '<td data-label="Deadline" class="dl">' + deadlineCell(g, s, today) + '</td>' +
      '<td data-label="Opportunity"><span class="funder">' + esc(g.funder_name) + '</span>' +
        '<strong class="title">' + esc(g.grant_title) + '</strong>' +
        (g.call_reference ? '<span class="small">' + esc(g.call_reference) + '</span>' : '') +
        (g.snapshot_summary ? '<p class="summary">' + esc(g.snapshot_summary) + '</p>' : '') +
        (g.research_area ? '<p class="area">' + esc(g.research_area) + '</p>' : '') + '</td>' +
      '<td data-label="Eligibility">' + tz + (stages || regions ? '<div class="chips">' + stages + regions + '</div>' : '') +
        (g.eligible_applicants ? '<details><summary>Who can apply</summary><p>' + esc(g.eligible_applicants) + '</p></details>' : '') + '</td>' +
      '<td data-label="Award" class="award">' + (g.award_budget ? esc(g.award_budget) : '<span class="small">See call</span>') +
        (g.award_period ? '<span class="small">' + esc(g.award_period) + '</span>' : '') + '</td>' +
      '<td class="go">' + (url ? '<a class="btn" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer" aria-label="View call: ' +
        esc(g.grant_title) + ' (opens in new tab)">View call ↗</a>' : '') + '</td>';
    tr.dataset.text = [g.grant_title, g.funder_name, g.call_reference, g.snapshot_summary, g.research_area,
      g.eligible_applicants, (g.career_stages || []).join(' '), (g.eligible_regions || []).join(' ')].join(' ').toLowerCase();
    return tr;
  }

  function render(grants) {
    var today = todayUTC(), live = [];
    grants.forEach(function (g) { var s = state(g, today); if (s) live.push({ g: g, s: s }); });
    live.sort(function (a, b) {
      if (a.s.rolling !== b.s.rolling) return a.s.rolling ? 1 : -1;
      return (a.s.next || '').localeCompare(b.s.next || '') || a.g.funder_name.localeCompare(b.g.funder_name);
    });
    options(ui.funder, uniqSorted(live.map(function (x) { return x.g.funder_name; })));
    options(ui.stage, uniqSorted([].concat.apply([], live.map(function (x) { return x.g.career_stages || []; }))));
    live.forEach(function (x) { x.tr = row(x.g, x.s, today); ui.rows.appendChild(x.tr); });
    ui.table.hidden = !live.length;

    function apply() {
      var term = ui.q.value.trim().toLowerCase(), w = ui.win.value, f = ui.funder.value, st = ui.stage.value, shown = 0;
      live.forEach(function (x) {
        var stages = x.g.career_stages || [];
        var ok = (!term || x.tr.dataset.text.indexOf(term) !== -1) &&
          (!f || x.g.funder_name === f) &&
          (!st || stages.indexOf(st) !== -1 || stages.indexOf('Any stage') !== -1) &&
          (!w || (w === 'rolling' ? x.s.rolling : (!x.s.rolling && x.s.days <= +w)));
        x.tr.hidden = !ok;
        if (ok) shown++;
      });
      ui.count.textContent = shown;
      ui.empty.hidden = shown !== 0;
    }
    [ui.q, ui.funder, ui.stage, ui.win].forEach(function (el) { el.addEventListener('input', apply); });
    apply();
  }

  fetch('../data/grants.json', { cache: 'no-cache' })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var lm = r.headers.get('Last-Modified');
      ui.updated.textContent = lm ? 'updated ' + new Date(lm).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) : 'updated nightly';
      return r.json();
    })
    .then(function (data) {
      if (!Array.isArray(data)) throw new Error('bad data');
      render(data);
    })
    .catch(function () {
      ui.error.hidden = false;
      ui.count.textContent = '0';
      ui.updated.textContent = 'unavailable';
    });
})();
