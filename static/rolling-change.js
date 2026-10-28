/* Standalone Rolling Change Analysis. Calendar math extracted from Risk v1168. */
(function (root) {
  'use strict';
  const WINDOWS = { '1w': [0, 7], '1m': [1, 0], '2m': [2, 0], '3m': [3, 0], '6m': [6, 0], '1y': [12, 0], '2y': [24, 0] };
  function canonical(text) {
    const s = String(text || '').trim().replace(/\s+/g, ' ');
    if (!/^[a-z0-9][a-z0-9 .\/&-]{0,55} (equity|index|comdty|curncy|corp|govt|pfd|m-mkt|mtge|muni)$/i.test(s)) return null;
    const p = s.split(' '), suffix = p.pop().toLowerCase();
    return p.join(' ').toUpperCase() + ' ' + (suffix === 'm-mkt' ? 'M-Mkt' : suffix[0].toUpperCase() + suffix.slice(1));
  }
  function isoDate(value) {
    const m = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!m) return null;
    const date = new Date(value + 'T00:00:00Z');
    return Number.isFinite(+date) && date.toISOString().slice(0, 10) === value ? date : null;
  }
  function shifted(iso, window) {
    const d = isoDate(iso), [months, days] = WINDOWS[window];
    if (months) {
      const day = d.getUTCDate();
      d.setUTCDate(1); d.setUTCMonth(d.getUTCMonth() - months);
      const end = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).getUTCDate();
      d.setUTCDate(Math.min(day, end));
    } else d.setUTCDate(d.getUTCDate() - days);
    return d.toISOString().slice(0, 10);
  }
  function normalize(payload) {
    const rows = new Map();
    (payload.dates || []).forEach((date, i) => {
      const value = (payload.last || payload.values || [])[i];
      if (isoDate(date) && typeof value === 'number' && Number.isFinite(value)) rows.set(date, value);
    });
    return [...rows].sort(([a], [b]) => a.localeCompare(b)).map(([date, value]) => ({ date, value }));
  }
  function rolling(rows, window, mode) {
    // Walk actual observations, never forward-fill the current close across holidays.
    let previous = -1;
    return rows.map((row, i) => {
      const target = shifted(row.date, window);
      while (previous + 1 < i && rows[previous + 1].date <= target) previous++;
      if (previous < 0) return null;
      const base = rows[previous].value;
      if (mode === 'pct' && base === 0) return null;
      const change = mode === 'pct' ? (row.value / base - 1) * 100 : row.value - base;
      return Number.isFinite(change) ? change : null;
    });
  }
  function percentile(value, sample) {
    const valid = sample.filter(v => typeof v === 'number' && Number.isFinite(v));
    if (value == null || !Number.isFinite(value) || valid.length < 2) return null;
    const less = valid.filter(v => v < value).length, equal = valid.filter(v => v === value).length;
    // Average ranks for ties; minimum 0, maximum 100, constant samples 50.
    return 100 * (less + (equal - 1) / 2) / (valid.length - 1);
  }
  function summary(rows, from, to, mode) {
    const visible = rows.map((r, i) => ({ ...r, i })).filter(r => r.date >= from && r.date <= to);
    const last = visible.at(-1);
    return { date: last?.date || null, periods: ['1m', '3m', '6m', '1y'].map(window => {
      const values = rolling(rows, window, mode);
      const sample = visible.map(r => values[r.i]).filter(v => v !== null);
      const latest = last ? values[last.i] : null;
      return { label: window === '1y' ? '12M' : window.toUpperCase(), latest, percentile: percentile(latest, sample), count: sample.length };
    }) };
  }
  function priceOverlay(prices, scale, from, to) {
    const entries = Object.entries(prices);
    const visible = entries.map(([ticker, rows]) => [ticker, rows.filter(r => r.date >= from && r.date <= to)]);
    const series = Object.fromEntries(visible.map(([ticker, rows]) => [ticker, rows.map(r => ({ ...r, raw: r.value }))]));
    if (scale === 'raw') return { series, anchorDate: null, warning: '' };
    if (scale === 'zscore') {
      for (const [ticker, rows] of entries) {
        series[ticker] = rows.map((row, i) => {
          let value = null;
          if (i >= 59) {
            const window = rows.slice(i - 59, i + 1).map(r => r.value);
            const origin = window[0], meanOffset = window.reduce((sum, x) => sum + (x - origin), 0) / 60;
            const variance = window.reduce((sum, x) => sum + ((x - origin) - meanOffset) ** 2, 0) / 60;
            if (variance > 0) value = ((row.value - origin) - meanOffset) / Math.sqrt(variance);
          }
          return { date: row.date, raw: row.value, value: Number.isFinite(value) ? value : null };
        }).filter(r => r.date >= from && r.date <= to);
      }
      return { series, anchorDate: null, warning: '' };
    }

    const eligible = visible.filter(([, rows]) => rows.some(r => r.value > 0));
    const positiveDates = eligible.map(([, rows]) => new Set(rows.filter(r => r.value > 0).map(r => r.date)));
    const anchorDate = eligible.length ? eligible[0][1].find(r => r.value > 0 && positiveDates.every(dates => dates.has(r.date)))?.date || null : null;
    for (const [ticker, rows] of visible) {
      const base = anchorDate ? rows.find(r => r.date === anchorDate && r.value > 0)?.value : null;
      series[ticker] = rows.map(r => ({ date: r.date, raw: r.value, value: base && r.date >= anchorDate ? (r.value / base - 1) * 100 : null }));
    }
    return { series, anchorDate, warning: anchorDate ? '' : 'No common date with positive starting prices. Choose Price to see native values.' };
  }
  // Export pure calculations for deterministic tests without a browser or live feed.
  if (typeof module !== 'undefined' && module.exports) module.exports = { canonical, shifted, normalize, rolling, percentile, summary, priceOverlay };
  if (!root.document) return;
  const $ = id => document.getElementById('rc-' + id);
  let selected = [], history = {}, charts = [], initialized = false, matches = [], active = -1, generation = 0, controller, range = 'all';
  const colors = ['#4e79a7', '#f28e2b', '#59a14f', '#e15759', '#b07aa1', '#76b7b2', '#edc949', '#af7aa1'];
  const text = (tag, content, cls) => { const e = document.createElement(tag); e.textContent = content; if (cls) e.className = cls; return e; };
  const SETTINGS_KEY = 'jcap.rolling-analysis.settings.v1';
  let priceScaleChoice = null;
  const priceScale = () => priceScaleChoice || (selected.length > 1 ? 'pct' : 'raw');
  function syncPriceScale() {
    document.querySelectorAll('[data-rc-price-scale]').forEach(b => {
      const active = b.dataset.rcPriceScale === priceScale();
      b.classList.toggle('active', active); b.setAttribute('aria-pressed', String(active));
    });
  }
  let knownCatalog = new Map(), searchGeneration = 0, searchController, searchTimer;
  function save() {
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify({ selected, range, from: $('from').value, to: $('to').value, first: $('first').value, second: $('second').value, mode: $('mode').value, priceScale: priceScaleChoice })); } catch (_) {}
  }
  function invalidate() {
    generation++; if (controller) controller.abort();
    $('summary-tables').replaceChildren(); $('summary-note').textContent = 'Analyze tickers to see their latest rolling changes and historical percentiles.';
    $('price-note').textContent = '';
    history = {}; destroy(); $('results').classList.add('rc-results-empty'); updateTitles(); $('run').disabled = false; $('cancel').hidden = true;
    $('status').textContent = selected.length ? 'Ready. Select Analyze to use saved prices and fill missing dates.' : 'Select tickers to view rolling change analysis';
  }
  function renderSuggestions(q, items, allowDirect = false) {
    matches = items.filter(o => !selected.includes(o.ticker));
    const full = canonical(q) || (/^[a-z0-9][a-z0-9.\/-]* [a-z]{2}$/i.test(q) ? canonical(q + ' Equity') : null);
    if (allowDirect && !matches.length && full && !selected.includes(full)) matches.unshift({ ticker: full, name: 'Load Bloomberg history' });
    matches = matches.slice(0, 30); active = -1;
    const list = $('suggestions'); list.replaceChildren();
    matches.forEach((item, i) => {
      const b = text('button', ''); b.type = 'button'; b.id = 'rc-option-' + i;
      b.append(text('span', item.name, 'rc-option-name'));
      if (item.name !== item.ticker) b.append(text('span', item.ticker, 'rc-option-ticker'));
      b.setAttribute('role', 'option'); b.setAttribute('aria-selected', 'false');
      b.onmousedown = e => e.preventDefault(); b.onclick = () => add(item.ticker); list.append(b);
    });
    const open = q.length >= 2 && matches.length > 0;
    list.hidden = !open; $('search').setAttribute('aria-expanded', String(open)); $('search').removeAttribute('aria-activedescendant');
  }
  function suggest() {
    clearTimeout(searchTimer); if (searchController) searchController.abort();
    const mine = ++searchGeneration;
    const q = $('search').value.trim().toLowerCase().replace(/\s+/g, ' ');
    if (q.length < 2) { matches = []; $('suggestions').replaceChildren(); closeSuggestions(); return; }
    const cached = () => [...knownCatalog.values()].filter(o => (o.ticker + ' ' + o.name).toLowerCase().includes(q));
    renderSuggestions(q, cached());
    searchTimer = setTimeout(async () => {
      searchController = new AbortController();
      try {
        const response = await fetch('/api/instruments?' + new URLSearchParams({ q }), { credentials: 'same-origin', signal: searchController.signal });
        if (!response.ok) throw new Error('Catalog unavailable');
        const body = await response.json();
        if (mine !== searchGeneration || document.activeElement !== $('search')) return;
        const items = (Array.isArray(body.items) ? body.items : []).map(o => ({ ticker: canonical(o.ticker), name: String(o.name || o.ticker) })).filter(o => o.ticker);
        items.forEach(o => knownCatalog.set(o.ticker, o));
        renderSuggestions(q, items, true);
      } catch (_) {
        if (mine === searchGeneration && document.activeElement === $('search')) renderSuggestions(q, cached(), true);
      }
    }, 200);
  }
  function closeSuggestions() {
    active = -1;
    ++searchGeneration; clearTimeout(searchTimer); if (searchController) searchController.abort();
    $('suggestions').hidden = true; $('search').setAttribute('aria-expanded', 'false'); $('search').removeAttribute('aria-activedescendant');
  }
  function add(ticker) {
    if (!selected.includes(ticker)) { selected.push(ticker); invalidate(); save(); paintChips(); }
    $('search').value = ''; closeSuggestions(); $('search').focus();
  }
  function paintChips() {
    syncPriceScale();
    $('help').hidden = !selected.length;
    $('status').classList.toggle('rc-empty', !selected.length);
    if (!selected.length) $('status').textContent = 'Select tickers to view rolling change analysis';
    $('tickers').replaceChildren();
    selected.forEach(ticker => {
      const b = text('button', ticker + ' ×', 'rc-chip'); b.type = 'button'; b.setAttribute('aria-label', 'Remove ' + ticker);
      b.onclick = () => { selected = selected.filter(t => t !== ticker); invalidate(); save(); paintChips(); };
      $('tickers').append(b);
    });
  }
  function updateTitles() {
    ['first', 'second'].forEach(id => { $('title-' + id).textContent = 'Rolling ' + $(id).selectedOptions[0].textContent.toLowerCase() + ' change'; });
  }
  function destroy() { charts.forEach(c => c.destroy()); charts = []; }
  function drawSummary(from, to, mode) {
    const container = $('summary-tables'); container.replaceChildren();
    for (const ticker of selected) {
      const result = summary(history[ticker] || [], from, to, mode);
      const card = text('section', '', 'rc-summary-card');
      card.append(text('h4', ticker + ' · PRICE'));
      card.append(text('p', result.date ? 'As of ' + result.date : 'No prices in selected range', 'rc-hint'));
      const table = document.createElement('table'); table.className = 'rc-summary-table';
      table.setAttribute('aria-label', ticker + ' rolling price change summary');
      const head = table.createTHead().insertRow();
      ['ROLL', mode === 'pct' ? 'LATEST (%)' : 'LATEST (UNITS)', 'PCTL'].forEach(label => { const th = text('th', label); th.scope = 'col'; head.append(th); });
      const body = table.createTBody();
      for (const period of result.periods) {
        const tr = body.insertRow(); const th = text('th', period.label); th.scope = 'row'; tr.append(th);
        const latest = period.latest;
        const value = latest === null ? '—' : (latest > 0 ? '+' : '') + latest.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: mode === 'pct' ? 1 : 4 }) + (mode === 'pct' ? '%' : '');
        const td = text('td', value); if (latest === null) td.title = 'Insufficient history or a zero percentage base at the latest close.'; tr.append(td);
        const rank = text('td', period.percentile === null ? '—' : String(Math.round(period.percentile)));
        rank.title = period.count + ' valid observations in the selected date range. Percentile uses average ranks for ties; at least two observations are required.';
        if (period.percentile !== null) rank.className = period.percentile <= 10 ? 'rc-percentile-low' : period.percentile >= 90 ? 'rc-percentile-high' : '';
        tr.append(rank);
      }
      card.append(table); container.append(card);
    }
    $('summary-note').textContent = 'Latest = change at each ticker’s last close on or before the end date. PCTL ranks that change against valid daily rolling changes within the selected date range (including the latest; ties use average ranks). Red ≤10th percentile; green ≥90th. Hover over a percentile for its sample count. — means insufficient data. Absolute changes use native price units.';
  }
  function draw() {
    destroy();
    $('results').classList.remove('rc-results-empty');
    const from = $('from').value, to = $('to').value || new Date().toISOString().slice(0, 10), mode = $('mode').value;
    const windows = [$('first').value, $('second').value];
    const scale = priceScale();
    const overlay = priceOverlay(Object.fromEntries(selected.map(t => [t, history[t] || []])), scale, from, to);
    syncPriceScale();
    $('price-note').textContent = overlay.warning || (scale === 'raw' ? 'Prices in native units.' : scale === 'zscore' ? 'Z-score: trailing 60 closes. Gaps mean insufficient history or zero standard deviation.' : overlay.anchorDate + ' = 0% for all compared instruments.');
    updateTitles();
    const labels = [...new Set(Object.values(history).flatMap(rows => rows.filter(r => r.date >= from && r.date <= to).map(r => r.date)))].sort();
    const muted = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim();
    const grid = getComputedStyle(document.documentElement).getPropertyValue('--border').trim();
    ['price', 'first', 'second'].forEach((id, panel) => {
      const datasets = selected.map((ticker, i) => {
        const rows = panel ? (history[ticker] || []) : overlay.series[ticker];
        const values = panel ? rolling(rows, windows[panel - 1], mode) : rows.map(r => r.value);
        const map = new Map(rows.map((r, j) => [r.date, values[j]]));
        const raw = new Map(rows.map(r => [r.date, panel ? r.value : r.raw]));
        return { label: ticker, _raw: labels.map(d => raw.has(d) ? raw.get(d) : null), data: labels.map(d => map.has(d) ? map.get(d) : null), borderColor: colors[i % colors.length], borderDash: i < colors.length ? [] : [6, 3 + Math.floor(i / colors.length)], borderWidth: 1.7, pointRadius: 0, pointHitRadius: 7, spanGaps: false };
      });
      const suffix = panel ? (mode === 'pct' ? '%' : '') : (scale === 'pct' ? '%' : scale === 'zscore' ? ' SD' : '');
      const priceAxis = {raw:'Price (native units)',pct:'Change from start (%)',zscore:'Z-score (SD)'}[scale];
      charts.push(new Chart($('chart-' + id), { type: 'line', data: { labels, datasets }, options: {
        responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: 'index', intersect: false },
        plugins: { legend: { labels: { color: muted, boxWidth: 15 } }, tooltip: { callbacks: {
          label: ctx => {
            const plotted = ctx.dataset.label + ': ' + Number(ctx.parsed.y).toLocaleString(undefined, { maximumFractionDigits: 4 }) + suffix;
            const raw = ctx.dataset._raw[ctx.dataIndex];
            return !panel && scale !== 'raw' && raw !== null ? plotted + ' · Price: ' + raw.toLocaleString(undefined, { maximumFractionDigits: 4 }) : plotted;
          }
        } } },
        scales: { x: { ticks: { color: muted, maxTicksLimit: 9 }, grid: { display: false } }, y: { title: { display: true, text: panel ? (suffix ? 'Change (%)' : 'Change (price units)') : priceAxis, color: muted }, ticks: { color: muted, callback: v => Number(v).toLocaleString() + suffix }, grid: { color: grid } } }
      } }));
    });
    drawSummary(from, to, mode);
  }
  async function analyze() {
    const today = new Date().toISOString().slice(0, 10);
    const from = $('from').value || (range === 'all' ? '0001-01-01' : ''), to = $('to').value || (range === 'all' ? today : '');
    if (!selected.length) { $('status').textContent = 'Add at least one ticker.'; return; }
    if (!isoDate(from) || !isoDate(to) || from > to || to > today) { $('status').textContent = 'Choose a valid date range ending today or earlier.'; return; }
    const earliest = range === 'all'
      ? new Date(+isoDate(today) - 2600 * 86400000).toISOString().slice(0, 10)
      : [shifted(from, $('first').value), shifted(from, $('second').value), shifted(from, '1y')].sort()[0];
    if ((+isoDate(to) - +isoDate(earliest)) / 86400000 > 4000) {
      $('status').textContent = 'This range plus its lookback exceeds 4,000 calendar days.'; return;
    }
    // Keep the displayed cached charts while the server fills gaps.
    generation++; if (controller) controller.abort();
    const mine = generation; controller = new AbortController(); const signal = controller.signal;
    const wanted = selected.slice(), failures = []; $('run').disabled = true; $('cancel').hidden = false;
    save();
    if (Object.values(history).some(rows => rows.length)) draw();
    try {
      // One Analyze action; bounded requests and provider-side batches avoid
      // URL limits and an unbounded burst of Bloomberg requests.
      for (let i = 0; i < wanted.length; i += 100) {
        const batch = wanted.slice(i, i + 100);
        try {
          const params = new URLSearchParams({ tickers: batch.join(','), from: earliest, to, range: '1', predecessor: '1' });
          const started = Date.now();
          while (true) {
            const response = await fetch('/api/history?' + params, { credentials: 'same-origin', signal });
            if (!response.ok) throw new Error('History request failed (' + response.status + ')');
            const body = await response.json();
            if (!body.ok || !body.data) throw new Error(body.error || 'Saved prices unavailable');
            if (mine !== generation) return;
            let changed = false;
            batch.forEach(ticker => {
              const rows = normalize(body.data[ticker] || {});
              if ((rows.length || !history[ticker]) && JSON.stringify(rows) !== JSON.stringify(history[ticker])) { history[ticker] = rows; changed = true; }
            });
            if (changed || !charts.length) draw();
            $('status').textContent = body.status === 'refreshing'
              ? 'Showing saved prices · filling missing dates · ' + Math.min(i + 100, wanted.length) + ' of ' + wanted.length + ' tickers…'
              : 'Saved prices loaded.';
            if (body.status !== 'refreshing') {
              failures.push(...(body.errors || [])); break;
            }
            if (!body.job || Date.now() - started > 600000) throw new Error('Price update is still running; saved prices remain visible. Select Analyze again shortly.');
            params.set('job', body.job);
            await new Promise((resolve, reject) => {
              const abort = () => { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
              const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, 1500);
              signal.addEventListener('abort', abort, { once: true });
              if (signal.aborted) abort();
            });
          }
        } catch (e) {
          if (mine !== generation || e.name === 'AbortError') return;
          failures.push(batch.join(', ') + ': ' + e.message);
        }
      }
      if (mine !== generation) return;
      draw();
      const missing = wanted.filter(t => !(history[t] || []).some(r => r.date >= from && r.date <= to));
      const short = wanted.filter(t => {
        const rows = history[t] || [];
        return rows.some(r => r.date >= from && r.date <= to) && [$('first').value, $('second').value].some(w => {
          const values = rolling(rows, w, $('mode').value);
          return !rows.some((r, i) => r.date >= from && r.date <= to && values[i] !== null);
        });
      });
      $('status').textContent = (missing.length === wanted.length ? 'No prices available for the selected range.' : 'Saved prices loaded. Changes use calendar periods and the previous available close.')
        + (missing.length ? ' No prices: ' + missing.join(', ') + '.' : '')
        + (short.length ? ' Insufficient rolling history: ' + short.join(', ') + '.' : '')
        + (failures.length ? ' ' + failures.join('; ') : '');
    } finally { if (mine === generation) { $('run').disabled = false; $('cancel').hidden = true; } }
  }
  root.rcInit = function () {
    if (initialized) { charts.forEach(c => c.resize()); return; } initialized = true;
    const today = new Date().toISOString().slice(0, 10); $('to').max = today; $('from').max = today;
    try {
      const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || 'null');
      if (saved) {
        range = ['all','ytd','1m','3m','6m'].includes(saved.range) ? saved.range : 'custom';
        selected = [...new Set((Array.isArray(saved.selected) ? saved.selected : []).map(canonical).filter(Boolean))];
        if (isoDate(saved.from)) $('from').value = saved.from;
        if (isoDate(saved.to)) $('to').value = saved.to;
        if (Object.hasOwn(WINDOWS, saved.first)) $('first').value = saved.first;
        if (Object.hasOwn(WINDOWS, saved.second)) $('second').value = saved.second;
        if (['pct', 'abs'].includes(saved.mode)) $('mode').value = saved.mode;
        if (['raw','pct','zscore'].includes(saved.priceScale)) priceScaleChoice = saved.priceScale;
        else if (saved.priceScale === 'rebase') priceScaleChoice = 'pct';
      }
    } catch (_) {}
    const applyRange = () => {
      const now = new Date().toISOString().slice(0, 10);
      if (range === 'custom') return;
      $('to').value = range === 'all' ? '' : now;
      $('from').value = range === 'all' ? '' : range === 'ytd' ? now.slice(0, 4) + '-01-01' : shifted(now, range);
    };
    applyRange();
    paintChips(); updateTitles();
    const markRange = () => document.querySelectorAll('[data-rc-range]').forEach(b => { b.classList.toggle('active', b.dataset.rcRange === range); b.setAttribute('aria-pressed', String(b.dataset.rcRange === range)); });
    markRange();
    document.querySelectorAll('[data-rc-range]').forEach(b => { b.onclick = () => {
      range = b.dataset.rcRange;
      const now = new Date().toISOString().slice(0, 10);
      $('to').value = range === 'all' ? '' : now;
      $('from').value = range === 'all' ? '' : range === 'ytd' ? now.slice(0, 4) + '-01-01' : shifted(now, range);
      markRange(); invalidate(); save(); paintChips();
    }; });
    $('search').onpaste = e => {
      const raw = e.clipboardData.getData('text');
      if (!/[,;\n\t]/.test(raw)) return;
      e.preventDefault();
      const entries = raw.split(/[,;\n\t]+/).map(x => x.trim()).filter(Boolean);
      const tickers = entries.map(canonical);
      if (tickers.some(t => !t)) {
        $('status').textContent = 'Use full Bloomberg tickers for a list, separated by commas or new lines. Nothing added.'; return;
      }
      selected = [...new Set(selected.concat(tickers))];
      invalidate(); save(); paintChips(); $('search').value = ''; closeSuggestions();
    };
    $('search').oninput = suggest; $('search').onfocus = suggest; $('search').onblur = closeSuggestions;
    $('search').onkeydown = e => {
      if (e.key === 'Escape') { closeSuggestions(); return; }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault(); if ($('suggestions').hidden) suggest();
        if (!matches.length) return;
        active = Math.max(0, Math.min(matches.length - 1, active + (e.key === 'ArrowDown' ? 1 : -1)));
        [...$('suggestions').children].forEach((el, i) => el.setAttribute('aria-selected', String(i === active)));
        $('search').setAttribute('aria-activedescendant', 'rc-option-' + active);
        $('suggestions').children[active].scrollIntoView({ block: 'nearest' });
      }
      if (e.key === 'Enter') {
        e.preventDefault(); if ($('suggestions').hidden) suggest();
        if (active >= 0 && matches[active]) add(matches[active].ticker);
        else {
          const query = $('search').value.trim().toLowerCase();
          if (!query) return;
          const match = matches.find(m => m.ticker.toLowerCase() === query || String(m.name).toLowerCase() === query);
          if (match || matches.length) add((match || matches[0]).ticker);
          else $('status').textContent = 'Choose a suggestion to confirm the instrument and market.';
        }
      }
    };
    document.querySelectorAll('[data-rc-price-scale]').forEach(b => { b.onclick = () => {
      priceScaleChoice = b.dataset.rcPriceScale; syncPriceScale(); save();
      if (Object.keys(history).length) draw();
    }; });
    $('run').onclick = analyze;
    ['from', 'to'].forEach(id => { $(id).onchange = () => { range = 'custom'; markRange(); invalidate(); save(); paintChips(); }; });
    ['first', 'second', 'mode'].forEach(id => { $(id).onchange = () => {
      // A changed horizon can require older prices: reload before showing results.
      if (id === 'mode' && Object.keys(history).length && !$('run').disabled) { draw(); save(); }
      else { invalidate(); save(); }
    }; });
    $('cancel').onclick = () => {
      generation++; if (controller) controller.abort();
      $('run').disabled = false; $('cancel').hidden = true;
      $('status').textContent = 'Update cancelled. Saved prices remain visible.';
    };
    $('clear').onclick = () => { selected = []; invalidate(); save(); paintChips(); };
    $('status').textContent = selected.length ? 'Saved tickers restored. Select Analyze to load prices.' : 'Select tickers to view rolling change analysis';
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', root.rcInit, { once: true });
  else root.rcInit();
})(typeof window === 'undefined' ? globalThis : window);
