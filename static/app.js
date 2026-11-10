(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const state = { dashboard: null, llm: null, providerId: '', marketData: null, marketDirty: false, marketSaving: false, watchlistDirty: false, watchlistSaving: false, importSaving: false, scanRunning: false, scanWarnings: [], holdingsFilter: 'all' };
  const markDrafts = new Map();
  const markPending = new Set();
  let session = null;
  let authLost = false;
  function redirectToLogin() {
    if (authLost) return;
    authLost = true;
    session = null;
    state.dashboard = null; state.llm = null; state.marketData = null;
    $('.app-shell').hidden = true;
    $('#session-loading').hidden = false;
    $('#session-loading').textContent = 'Your session has ended. Redirecting to sign in…';
    try { sessionStorage.removeItem('ledger-six-scanner-drafts-v1'); sessionStorage.removeItem('ledger-six-scan-warnings-v1'); } catch (_) {}
    window.location.replace('/login');
  }
  const sessionReady = (async () => {
    let response;
    try { response = await fetch('/api/auth/session', {cache:'no-store'}); }
    catch (_) { throw new Error('Cannot verify your session. Check your connection and reload.'); }
    if (response.status === 401) { redirectToLogin(); throw new Error('Your session has ended.'); }
    if (!response.ok) throw new Error('Cannot verify your session. Please reload and try again.');
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('Cannot verify your session. Please reload and try again.'); }
    if (!data || typeof data.username !== 'string' || !['admin','user'].includes(data.role) || typeof data.csrf !== 'string') throw new Error('Cannot verify your session. Please reload and try again.');
    session = data;
    return data;
  })();
  const isAdmin = () => session?.role === 'admin';
  const isLocalTestMode = () => session?.csrf === '';
  window.addEventListener('pagehide', () => { $('.app-shell').hidden = true; });
  window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
  const settingFields = ['starting_balance', 'risk_percent', 'max_position_percent', 'min_liquidity', 'min_volume', 'slippage_bps', 'fee_bps', 'stop_loss_percent', 'take_profit_percent', 'scan_interval_seconds'];
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const present = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const money = value => present(value) ? new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',maximumFractionDigits:2}).format(Number(value)) : '—';
  const price = value => present(value) ? new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:18}).format(Number(value)) : '—';
  const number = value => present(value) ? new Intl.NumberFormat('en-US', {maximumFractionDigits:18}).format(Number(value)) : '—';
  const date = value => { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.valueOf()) ? String(value) : d.toLocaleString(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}); };
  const shortDate = value => { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.valueOf()) ? String(value) : d.toLocaleDateString(undefined, {month:'short',day:'numeric',year:'numeric'}); };
  function stockQuoteStatus(item) {
    const observed = item.observed_at ? String(item.observed_at) : '';
    if (item.cache_status === 'manual') return `Manual import · ${observed ? `imported at ${observed}` : 'import time unavailable'} · not independently verified`;
    const label = item.cache_status === 'fresh' ? 'Fresh' : item.cache_status === 'cached' ? 'Cached' : item.cache_status === 'stale' ? 'Stale cached quote' : 'Cache status unavailable';
    return `${label} · ${observed ? `fetched at ${observed}` : 'fetch time unavailable'}`;
  }
  function notice(message, error = false, warning = false) { const el = $('#notice'); el.textContent = message; el.classList.toggle('error', error); el.classList.toggle('warning', warning); el.hidden = !message; }
  function errorText(error) { return error instanceof Error ? error.message : String(error); }
  async function api(path, options = {}) {
    await sessionReady;
    if (authLost) throw new Error('Your session has ended.');
    const method = String(options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    if (!['GET','HEAD','OPTIONS'].includes(method)) headers.set('X-CSRF-Token', session.csrf);
    let response;
    try { response = await fetch(path, { ...options, headers }); }
    catch (_) { throw new Error('Cannot reach the application server. Check the connection and try again.'); }
    if (response.status === 401) { redirectToLogin(); throw new Error('Your session has ended.'); }
    if (authLost) throw new Error('Your session has ended.');
    const raw = await response.text(); let data = {};
    if (raw) { try { data = JSON.parse(raw); } catch (_) { if (!response.ok) throw new Error(`Request failed (${response.status}).`); throw new Error('The server returned an invalid response.'); } }
    if (!response.ok) {
      const detail = data.detail ?? data.error ?? data.message;
      const scannerRequest = /^\/api\/(?:scan|cycle)(?:\?|$)/.test(path);
      const message = typeof detail === 'string' ? (scannerRequest ? safeScannerMessage(detail) : detail) : `Request failed (${response.status}).`;
      const error = new Error(message); error.status = response.status; throw error;
    }
    return data;
  }
  const json = (method, data) => ({method, body: JSON.stringify(data)});
  function setConnection(connected) { const el = $('#connection'); el.classList.toggle('connected', connected); el.classList.toggle('disconnected', !connected); $('#connection-text').textContent = connected ? 'Connected' : 'Disconnected'; }
  function setValue(selector, value, color = false) { const el = $(selector); el.textContent = value; if (color) { el.classList.remove('positive', 'negative'); } }
  function setPnl(selector, value) { const el = $(selector); el.textContent = money(value); el.classList.toggle('positive', present(value) && Number(value) > 0); el.classList.toggle('negative', present(value) && Number(value) < 0); }
  function safeUrl(value) { try { const u = new URL(value); return ['http:', 'https:'].includes(u.protocol) ? u.href : ''; } catch (_) { return ''; } }
  function renderChart(points) {
    const svg = $('#equity-chart'), empty = $('#chart-empty');
    const valid = (Array.isArray(points) ? points : []).filter(p => present(p.equity) && p.timestamp && !Number.isNaN(new Date(p.timestamp).valueOf())).slice().sort((a,b) => new Date(a.timestamp) - new Date(b.timestamp));
    const grid = [25, 85, 145, 205].map(y => `<line class="gridline" x1="65" y1="${y}" x2="700" y2="${y}"/>`).join('');
    if (!valid.length) { svg.innerHTML = grid; svg.setAttribute('aria-label','Equity curve has no snapshots yet'); empty.hidden = false; $('#chart-start').textContent = '—'; $('#chart-end').textContent = '—'; return; }
    empty.hidden = true;
    const values = valid.map(p => Number(p.equity)); const min = Math.min(...values), max = Math.max(...values); const spread = max - min || Math.max(Math.abs(max) * .1, 1); const lo = min - spread * .15, hi = max + spread * .15;
    const times = valid.map(p => new Date(p.timestamp).valueOf()); const first = times[0], lastTime = times[times.length - 1];
    const coords = values.map((v,i) => [valid.length === 1 ? 380 : 65 + (lastTime === first ? i / (valid.length - 1) : (times[i] - first) / (lastTime - first)) * 635, 220 - (v - lo) / (hi - lo) * 190]);
    const path = coords.map((p,i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
    const area = valid.length > 1 ? `<path d="${path} L700,235 L65,235 Z" fill="url(#chart-fill)" stroke="none"/>` : '';
    const last = coords[coords.length - 1];
    const yAt = v => (220 - (v - lo) / (hi - lo) * 190).toFixed(1);
    const label = v => `<text class="axis-label" x="0" y="${yAt(v)}" dominant-baseline="middle">${escapeHtml(money(v))}</text>`;
    const labels = min === max ? label(max) : label(max) + label(min);
    svg.innerHTML = `<defs><linearGradient id="chart-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="var(--accent)" stop-opacity=".22"/><stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>${grid}${labels}${area}<path class="chart-line" d="${path}"/><circle class="chart-point" cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="4"/>`;
    svg.setAttribute('aria-label', `Equity curve with ${valid.length} snapshots, from ${money(values[0])} to ${money(values[values.length - 1])}.`);
    $('#chart-start').textContent = shortDate(valid[0].timestamp); $('#chart-end').textContent = shortDate(valid[valid.length-1].timestamp);
  }
  const agentDefs = [
    {key:'scanner', label:'Scanner', number:'01'}, {key:'risk', label:'Risk check', number:'02'}, {key:'sizer', label:'Sizer', number:'03'},
    {key:'executor', label:'Executor', number:'04'}, {key:'logger', label:'Logger', number:'05'}, {key:'equity', label:'Equity curve', number:'06'}
  ];
  function renderAgents(agents) {
    agents = Array.isArray(agents) ? agents : [];
    $('#agents-list').innerHTML = agentDefs.map(def => {
      const a = agents.find(item => `${item.id ?? ''} ${item.name ?? ''}`.toLowerCase().includes(def.key)) || {};
      const status = String(a.status ?? 'Unavailable'); const s = status.toLowerCase();
      const cls = /error|fail|offline|stop/.test(s) ? 'bad' : /warn|wait|idle|paused/.test(s) ? 'warn' : /active|ready|running|online|ok|success/.test(s) ? 'ok' : '';
      return `<article class="agent-row"><div class="agent-icon">${def.number}</div><div><div class="agent-name">${escapeHtml(a.name || def.label)}</div><div class="agent-detail" title="${escapeHtml(a.detail || 'Waiting for engine data')}">${escapeHtml(a.detail || 'Waiting for engine data')}</div></div><div class="agent-status ${cls}">${escapeHtml(status)}</div></article>`;
    }).join('');
  }
  function renderCandidates(items) {
    items = Array.isArray(items) ? items : []; $('#candidates-empty').hidden = items.length > 0;
    const displayOrder = items.map((item, index) => ({item, index})).sort((a, b) => Number(b.item.asset_type === 'stock') - Number(a.item.asset_type === 'stock'));
    $('#candidates-body').innerHTML = displayOrder.map(({item, index}) => {
      const stock = item.asset_type === 'stock';
      const url = safeUrl(item.url), symbol = escapeHtml(item.symbol || '—');
      const source = escapeHtml(item.source || (stock ? 'Source unavailable' : 'DexScreener'));
      const asset = `${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer"><strong>${symbol} ↗</strong></a>` : `<strong>${symbol}</strong>`}<small>${stock ? 'STOCK' : 'CRYPTO'} · Source: ${source}</small>${!stock ? `<small title="${escapeHtml(item.pair_address || '')}">${escapeHtml(item.chain || '—')}${item.pair_address ? ` · ${escapeHtml(item.pair_address)}` : ''}</small>` : ''}`;
      const quote = stock ? `<small class="quote-meta">${item.cache_status === 'manual' ? 'User-supplied date' : 'Trading day'}: ${escapeHtml(item.quote_date || 'Unavailable')}</small><small class="quote-meta ${item.cache_status === 'stale' ? 'quote-stale' : ''}">${escapeHtml(stockQuoteStatus(item))}</small>` : item.observed_at ? `<small>Observed ${escapeHtml(date(item.observed_at))}</small>` : '';
      const change = stock ? `${present(item.stock_change_percent) ? `${number(item.stock_change_percent)}%` : '—'}<small>${item.cache_status === 'manual' ? 'Not provided' : 'Close-to-close'}</small>` : `${present(item.price_change_24h) ? `${number(item.price_change_24h)}%` : '—'}<small>24h</small>`;
      const liquidity = stock ? '—<small>Not assessed</small>' : `${money(item.liquidity_usd)}<small>USD</small>`;
      const volume = stock ? `${number(item.stock_volume_shares)}<small>Shares · ${item.cache_status === 'manual' ? 'user input' : 'quote day'}</small>` : `${money(item.volume_24h)}<small>USD · 24h</small>`;
      const trend = stock ? '—' : `${present(item.volume_spike_ratio) ? `${number(item.volume_spike_ratio)}×` : '—'}<small>1h vs prior 23h avg</small>`;
      const review = stock ? '<span class="risk-pill">Not assessed</span><small>No stock liquidity or rug assessment</small>' : `<span class="risk-pill" title="${escapeHtml(item.reason || '')}">${escapeHtml(item.risk_status || '—')}</span>${item.reason ? `<small title="${escapeHtml(item.reason)}">${escapeHtml(item.reason)}</small>` : ''}`;
      const action = stock && item.symbol && present(item.price_usd) && Number(item.price_usd) > 0 ? `<button class="text-button quote-button" type="button" data-use-quote="${index}" aria-label="Use quote for ${symbol} in the ledger">Use quote <svg><use href="#i-arrow"/></svg></button>` : '';
      return `<tr><td>${asset}${action}</td><td>${price(item.price_usd)}${quote}</td><td>${change}</td><td>${liquidity}</td><td>${volume}</td><td>${trend}</td><td>${review}</td></tr>`;
    }).join('');
  }
  function renderPositions(items) {
    const available = Array.isArray(items);
    // The dashboard positions are the source of truth, not scanner candidates or watchlists.
    const owned = available ? items.filter(item => item && present(item.quantity) && Number(item.quantity) > 0) : [];
    const counts = {all:owned.length, stock:owned.filter(item => item.asset_type === 'stock').length, crypto:owned.filter(item => item.asset_type === 'crypto').length};
    const filtered = state.holdingsFilter === 'all' ? owned : owned.filter(item => item.asset_type === state.holdingsFilter);
    $$('[data-holdings-filter]').forEach(button => {
      const key = button.dataset.holdingsFilter;
      button.setAttribute('aria-pressed', String(key === state.holdingsFilter));
      $(`#holdings-count-${key}`).textContent = available ? String(counts[key]) : '—';
    });
    $('#holdings-count').textContent = available ? `Showing ${filtered.length} of ${owned.length} open holding${owned.length === 1 ? '' : 's'}.` : 'Holdings unavailable; refresh to try again.';
    $('#positions-empty').hidden = available && filtered.length > 0;
    $('#positions-empty-title').textContent = !available ? 'Holdings unavailable' : state.holdingsFilter === 'all' ? 'No open holdings' : `No ${state.holdingsFilter === 'stock' ? 'stock' : 'crypto'} holdings`;
    $('#positions-empty-help').textContent = !available ? 'Refresh to load your paper positions.' : 'Use Add stock or Add crypto to prepare a paper BUY. Scanner results and watchlists do not create holdings.';
    const activeMark = document.activeElement?.matches('.mark-form input') ? document.activeElement.closest('.mark-form').dataset.id : null;
    $('#positions-body').innerHTML = filtered.map(item => {
      const type = item.asset_type === 'stock' ? 'Stock' : item.asset_type === 'crypto' ? 'Crypto' : String(item.asset_type || 'Unknown type');
      const symbol = escapeHtml(item.symbol || '—'), id = String(item.id);
      const sell = ['stock', 'crypto'].includes(item.asset_type) && item.symbol ? `<button class="button button-secondary sell-holding" type="button" data-sell-position="${escapeHtml(id)}" aria-label="Prepare paper sell for ${symbol} (${escapeHtml(type)})">Sell</button>` : '—';
      return `<tr><td><strong>${symbol}</strong><small>${escapeHtml(type)}</small></td><td>${number(item.quantity)}</td><td>${price(item.entry_price)}</td><td>${price(item.mark_price)}<small>${escapeHtml(item.mark_source || 'Unmarked')}${item.mark_timestamp ? ` · ${item.mark_source === 'manual' ? 'Recorded' : 'Observed'}: ${escapeHtml(item.mark_timestamp)}` : ''}</small></td><td class="${present(item.unrealized_pnl) && Number(item.unrealized_pnl) > 0 ? 'positive' : present(item.unrealized_pnl) && Number(item.unrealized_pnl) < 0 ? 'negative' : ''}">${money(item.unrealized_pnl)}</td><td><form class="mark-form" data-id="${escapeHtml(id)}"><input type="number" min="0.000000000000000001" step="any" name="price" required aria-label="New mark price for ${symbol} (${escapeHtml(type)})" placeholder="Price" value="${escapeHtml(markDrafts.get(id) || '')}"><button class="button button-secondary" type="submit"${markPending.has(id) ? ' disabled' : ''}>Update</button></form></td><td>${sell}</td></tr>`;
    }).join('');
    if (activeMark !== null) {
      const restored = $$('.mark-form', $('#positions-body')).find(form => form.dataset.id === activeMark);
      if (restored) restored.elements.price.focus({preventScroll:true});
    }
  }
  function renderTrades(items) {
    items = Array.isArray(items) ? items : []; $('#trades-empty').hidden = items.length > 0;
    $('#trades-body').innerHTML = items.map(item => `<tr><td>${escapeHtml(date(item.timestamp))}</td><td><strong>${escapeHtml(item.symbol || '—')}</strong><small>${escapeHtml(item.asset_type || '')}</small></td><td class="${String(item.side).toUpperCase() === 'BUY' ? 'positive' : ''}">${escapeHtml(item.side || '—')}</td><td>${number(item.quantity)}</td><td>${price(item.reference_price)}<small>Fill ${price(item.fill_price)} · ${number(item.slippage_bps)} bps</small></td><td>${money(item.fee)}</td><td>${money(item.realized_pnl)}</td><td title="${escapeHtml(item.reason || '')}">${escapeHtml(item.reason || '—')}</td></tr>`).join('');
  }
  function renderActivity(items) {
    items = Array.isArray(items) ? items : []; $('#activity-empty').hidden = items.length > 0;
    $('#activity-list').innerHTML = items.slice(0, 8).map(item => `<div class="activity-item"><span class="activity-dot ${/error|fail/i.test(item.level || '') ? 'error' : /warn/i.test(item.level || '') ? 'warning' : ''}"></span><div class="activity-message">${escapeHtml(item.message || '—')}<small>${escapeHtml(item.agent || 'System')}</small></div><time class="activity-time">${escapeHtml(date(item.timestamp))}</time></div>`).join('');
  }
  const marketNames = {stocks:'Stocks', crypto:'Crypto', both:'Both'};
  const scannerDraftKey = 'ledger-six-scanner-drafts-v1';
  function persistScannerDrafts() {
    const draft = {};
    if (state.marketDirty) draft.market = $('#market-form').elements.market_mode.value;
    if (state.watchlistDirty) draft.watchlist = $('#stock-symbols-form').elements.stock_symbols.value;
    const csv = $('#stock-import-form').elements.csv.value;
    if (csv) draft.csv = csv;
    try { if (Object.keys(draft).length) sessionStorage.setItem(scannerDraftKey, JSON.stringify(draft)); else sessionStorage.removeItem(scannerDraftKey); } catch (_) {}
  }
  function restoreScannerDrafts() {
    try {
      const draft = JSON.parse(sessionStorage.getItem(scannerDraftKey) || '{}');
      if (typeof draft.market === 'string' && marketNames[draft.market]) { $('#market-form').elements.market_mode.value = draft.market; state.marketDirty = true; }
      if (typeof draft.watchlist === 'string') { $('#stock-symbols-form').elements.stock_symbols.value = draft.watchlist; state.watchlistDirty = true; }
      if (typeof draft.csv === 'string') $('#stock-import-form').elements.csv.value = draft.csv;
    } catch (_) {}
  }
  function watchlistSymbols(value) { return value.split(',').map(symbol => symbol.trim().toUpperCase()); }
  function validWatchlist(symbols) {
    return symbols.length >= 1 && symbols.length <= 25 && symbols.every(symbol => /^[A-Z]{1,5}(?:[.-][A-Z]{1,4})?$/.test(symbol)) && new Set(symbols).size === symbols.length;
  }
  function renderMarketSelection(settings, available) {
    const form = $('#market-form'), saved = marketNames[settings.market_mode] ? settings.market_mode : '';
    if (available && !state.marketDirty && !state.marketSaving) form.elements.market_mode.value = saved;
    const selected = form.elements.market_mode.value;
    $('#market-options').disabled = !isAdmin() || !available || state.marketSaving || state.scanRunning;
    $('button[type=submit]', form).disabled = !isAdmin() || !available || state.marketSaving || state.scanRunning || !state.marketDirty;
    $('#market-current').textContent = !available || !saved ? 'Saved market selection unavailable.' : !isAdmin() ? `Saved: ${marketNames[saved]} · Only administrators can change this.` : state.marketDirty ? `Saved: ${marketNames[saved]} · ${marketNames[selected] || 'Selection'} not saved` : `Saved: ${marketNames[saved]}`;
  }
  function renderWatchlist(settings, available) {
    const form = $('#stock-symbols-form');
    form.elements.stock_symbols.disabled = !isAdmin() || !available || state.watchlistSaving || state.scanRunning;
    $('button[type=submit]', form).disabled = !isAdmin() || !available || state.watchlistSaving || state.scanRunning || !state.watchlistDirty;
    if (available && !state.watchlistDirty && !state.watchlistSaving) form.elements.stock_symbols.value = (Array.isArray(settings.stock_symbols) ? settings.stock_symbols : []).join(', ');
    $('#stock-settings-status').textContent = !available ? 'Saved watchlist unavailable.' : state.watchlistDirty ? 'Watchlist has unsaved changes. Save or revert before scanning or running a cycle.' : 'Watchlist saved. Scans use this Alpha Vantage list.';
  }
  function renderStockContext() {
    if (!isAdmin()) {
      $('#stock-scan-context').textContent = 'Automated stock source: Alpha Vantage · Provider configuration is managed by an administrator. Yahoo Finance quotes are manual import only.';
      return;
    }
    const alpha = state.marketData;
    const status = !alpha ? 'key status unavailable' : alpha.using_demo ? 'demo mode, IBM only' : alpha.has_api_key ? 'API key saved' : 'no API key saved';
    $('#stock-scan-context').textContent = `Automated stock source: Alpha Vantage · ${status}. Yahoo Finance is manual import only.`;
  }
  function renderMarketData(data) {
    state.marketData = data;
    $('#market-data-status').textContent = data.using_demo ? 'Alpha Vantage · Demo mode: IBM only (no key configured).' : data.has_api_key ? 'Alpha Vantage · API key saved; never displayed.' : 'Alpha Vantage · No API key saved.';
    $('#clear-market-key').disabled = !data.has_api_key;
    renderStockContext();
  }
  // Scanner diagnostics are application-authored messages, never provider payloads.
  // Refuse obvious raw payloads, URLs and credential assignments as a defense in depth.
  function safeScannerMessage(value) {
    const fallback = 'A scanner notice was withheld because it may contain provider data or credentials.';
    if (typeof value !== 'string') return fallback;
    const message = value;
    if (!message.trim() || message.length > 3000 || /[{}<>\r\n]/.test(message) || /https?:\/\/|\b(?:api[_ -]?key|apikey|access[_ -]?token|secret|authorization|password)\s*[:=]/i.test(message)) return fallback;
    return message;
  }
  function warningMessages(data) {
    return Array.isArray(data?.warnings) ? data.warnings.map(item => safeScannerMessage(typeof item === 'string' ? item : item?.message ?? item?.detail)) : [];
  }
  const scanWarningsKey = 'ledger-six-scan-warnings-v1';
  function setScanWarnings(warnings) {
    state.scanWarnings = warnings.map(safeScannerMessage);
    try { if (state.scanWarnings.length) sessionStorage.setItem(scanWarningsKey, JSON.stringify(state.scanWarnings)); else sessionStorage.removeItem(scanWarningsKey); } catch (_) {}
    showScanWarnings(state.scanWarnings);
  }
  function showScanWarnings(warnings) {
    const el = $('#scan-warnings');
    el.textContent = warnings.length ? `Scanner notices — results may be incomplete or stale:\n${warnings.map(message => `• ${message}`).join('\n')}` : '';
    el.hidden = !warnings.length;
  }
  function restoreScanWarnings() {
    try { const saved = JSON.parse(sessionStorage.getItem(scanWarningsKey) || '[]'); if (Array.isArray(saved)) state.scanWarnings = saved.map(safeScannerMessage); } catch (_) {}
    showScanWarnings(state.scanWarnings);
  }
  function renderDashboard(data, preserveSettings = false) {
    state.dashboard = data; const s = data.summary || {};
    setValue('#metric-equity', money(s.equity)); setValue('#metric-cash', money(s.cash)); setPnl('#metric-realized', s.realized_pnl); setValue('#metric-positions', number(s.open_positions));
    $('#metric-unrealized').textContent = `Unrealized P&L: ${money(s.unrealized_pnl)}`;
    $('#last-updated').textContent = data.summary ? `UPDATED ${new Date().toLocaleTimeString(undefined, {hour:'numeric',minute:'2-digit'})}` : 'WAITING FOR DATA';
    renderChart(data.equity_curve); renderAgents(data.agents); renderCandidates(data.candidates); renderPositions(data.positions); renderTrades(data.trades); renderActivity(data.activity);
    const settings = data.settings || {}; const hasAuto = typeof settings.auto_paper === 'boolean';
    const toggle = $('#auto-toggle'); toggle.disabled = !isAdmin() || !hasAuto; toggle.setAttribute('aria-checked', String(hasAuto && settings.auto_paper));
    $('#auto-description').textContent = !hasAuto ? 'Setting unavailable' : `${settings.auto_paper ? 'Enabled · eligible crypto, manual cycles only' : 'Blocked · manual cycles only'}${isAdmin() ? '' : ' · Admin controlled'}`;
    renderMarketSelection(settings, !!data.settings); renderWatchlist(settings, !!data.settings);
    if (!preserveSettings) { const form = $('#settings-form'); settingFields.forEach(key => { form.elements[key].value = settings[key] ?? ''; }); form.elements.auto_paper.checked = !!settings.auto_paper; }
  }
  function renderProviders(data) {
    state.llm = data; const providers = Array.isArray(data.providers) ? data.providers : []; const select = $('#provider-select');
    if (!providers.some(p => String(p.id) === String(state.providerId))) state.providerId = String(data.active_provider ?? providers[0]?.id ?? '');
    if (!providers.some(p => String(p.id) === state.providerId)) state.providerId = String(providers[0]?.id ?? '');
    select.innerHTML = providers.length ? providers.map(p => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name || p.id)}${String(p.id) === String(data.active_provider) ? ' · Active' : ''}</option>`).join('') : '<option value="">No providers available</option>';
    select.value = state.providerId; select.disabled = !providers.length; fillProvider();
  }
  function fillProvider() {
    const p = state.llm?.providers?.find(item => String(item.id) === state.providerId); const form = $('#provider-form');
    $$('input,button', form).forEach(el => { el.disabled = !p; }); $('#test-provider').disabled = !p; $('#activate-provider').disabled = !p;
    if (!p) { $('#provider-status').textContent = 'No provider presets available.'; ['name','kind','base_url','model','api_key'].forEach(key => { form.elements[key].value = ''; }); form.elements.enabled.checked = false; form.elements.clear_key.checked = false; $('#key-status').textContent = 'Saved keys are never displayed.'; return; }
    ['name','kind','base_url','model'].forEach(key => { form.elements[key].value = p[key] ?? ''; });
    form.elements.api_key.value = ''; form.elements.clear_key.checked = false; form.elements.enabled.checked = !!p.enabled;
    $('#key-status').textContent = p.has_key ? 'A key is saved. Leave blank to keep it; saved keys are never displayed.' : 'No key saved. Saved keys are never displayed.';
    $('#provider-status').textContent = `${String(p.id) === String(state.llm.active_provider) ? 'ACTIVE PROVIDER' : 'NOT ACTIVE'} · ${p.enabled ? 'ENABLED' : 'DISABLED'}`;
  }
  let dashboardRequestSequence = 0;
  async function refreshDashboard(preserveSettings = false) { const sequence = ++dashboardRequestSequence; const data = await api('/api/dashboard'); if (sequence === dashboardRequestSequence) { renderDashboard(data, preserveSettings); setConnection(true); } return data; }
  async function refreshProviders() { const data = await api('/api/llm'); renderProviders(data); return data; }
  async function refreshMarketData() { const data = await api('/api/market-data'); renderMarketData(data); return data; }
  async function refreshAfterMutation(fn) { try { await fn(); } catch (e) { throw new Error(`Action completed, but refresh failed. Verify the result before retrying. ${errorText(e)}`); } }
  async function refreshAll(showSuccess = false) {
    await sessionReady;
    const results = await Promise.allSettled([refreshDashboard(), isAdmin() ? refreshProviders() : Promise.resolve(), api('/api/health'), isAdmin() ? refreshMarketData() : Promise.resolve()]);
    if (authLost) return;
    if (results[3].status === 'rejected') { state.marketData = null; $('#market-data-status').textContent = 'Alpha Vantage key status unavailable.'; $('#clear-market-key').disabled = true; renderStockContext(); }
    if (results[0].status === 'rejected') { setConnection(false); notice(`Dashboard unavailable: ${errorText(results[0].reason)}`, true); }
    else if (results[1].status === 'rejected') notice(`Provider settings unavailable: ${errorText(results[1].reason)}`, true);
    else if (results[2].status === 'rejected') { setConnection(false); notice(`Health check unavailable: ${errorText(results[2].reason)}`, true); }
    else if (results[3].status === 'rejected') notice(`Stock provider settings unavailable: ${errorText(results[3].reason)}`, true);
    else if (showSuccess) notice('Data refreshed.');
    else notice('');
  }
  async function runAction(button, task, success) {
    if (button.disabled) return; button.disabled = true;
    try { const result = await task(); const warnings = warningMessages(result); notice(warnings.length ? `${success} Warnings: ${warnings.join(' · ')}` : success, false, warnings.length > 0); } catch (e) { notice(errorText(e), true); } finally { button.disabled = button.id === 'auto-toggle' && !isAdmin(); }
  }
  async function runScanAction(action) {
    if (state.scanRunning) return;
    state.scanRunning = true;
    const buttons = $$('[data-action]'); buttons.forEach(button => { button.disabled = true; });
    renderMarketSelection(state.dashboard?.settings || {}, !!state.dashboard?.settings); renderWatchlist(state.dashboard?.settings || {}, !!state.dashboard?.settings);
    const label = action === 'scan' ? 'Scanner' : 'Paper cycle';
    try {
      const result = await api(`/api/${action}`, {method:'POST'});
      if (result?.success === false || result?.ok === false) throw new Error(safeScannerMessage(result.message ?? result.detail ?? 'No successful scan was reported.'));
      const warnings = warningMessages(result);
      if (Array.isArray(result?.candidates) && result.candidates.some(item => item.asset_type === 'stock' && item.cache_status === 'stale') && !warnings.some(message => /stale|older cached/i.test(message))) {
        warnings.push('Older cached stock quotes are shown and may be stale. Verify the trading day and original fetch time before using them.');
      }
      setScanWarnings(warnings);
      try { await refreshAfterMutation(() => refreshDashboard(true)); }
      catch (_) {
        const message = `${label} returned, but the dashboard could not be refreshed. Displayed results may be stale; refresh to verify before retrying.`;
        setScanWarnings([...state.scanWarnings, message]); notice(message, true); return;
      }
      notice(warnings.length ? `${label} returned with warnings; scanner results may be incomplete or stale.` : `${label} completed.`, false, warnings.length > 0);
    } catch (e) {
      const detail = safeScannerMessage(errorText(e));
      const prefix = /all requested market sources failed/i.test(detail) ? `${label} did not complete.` : `${label} result could not be confirmed.`;
      const message = `${prefix} ${detail} Previously displayed results may still be shown and may be stale.`;
      setScanWarnings([...state.scanWarnings, message]); notice(message, true);
      try { await refreshDashboard(true); } catch (_) { setConnection(false); }
    } finally { state.scanRunning = false; buttons.forEach(button => { button.disabled = false; }); renderMarketSelection(state.dashboard?.settings || {}, !!state.dashboard?.settings); renderWatchlist(state.dashboard?.settings || {}, !!state.dashboard?.settings); }
  }
  $$('.nav-link').forEach(button => button.addEventListener('click', () => { const page = button.dataset.page; $$('.page').forEach(el => { el.hidden = el.id !== `page-${page}`; }); $$('.nav-link').forEach(el => { const active = el === button; el.classList.toggle('active', active); if (active) el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current'); }); $('#breadcrumb-page').textContent = page.toUpperCase(); if (page === 'settings' && isAdmin()) loadUsers(); window.scrollTo(0,0); }));
  function updateThemeButton() { const light = document.documentElement.dataset.theme === 'light'; $('#theme-toggle').setAttribute('aria-label', light ? 'Switch to dark mode' : 'Switch to light mode'); }
  updateThemeButton(); $('#theme-toggle').addEventListener('click', () => { const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = theme; try { localStorage.setItem('ledger-six-theme', theme); } catch (_) {} updateThemeButton(); });
  $('#refresh-button').addEventListener('click', async event => { const button = event.currentTarget; button.disabled = true; try { await refreshAll(true); } finally { button.disabled = false; } });
  $$('[data-action]').forEach(button => button.addEventListener('click', () => {
    if (state.marketDirty || state.watchlistDirty || state.marketSaving || state.watchlistSaving) { notice('Save or revert the market selection and Alpha Vantage watchlist changes before running the scanner or cycle.', true); return; }
    const settings = state.dashboard?.settings;
    if (!settings || !marketNames[settings.market_mode] || !Array.isArray(settings.stock_symbols) || !validWatchlist(settings.stock_symbols)) { notice('Saved market selection or stock watchlist is unavailable. Refresh settings before running the scanner or cycle.', true); return; }
    return runScanAction(button.dataset.action);
  }));
  $('#market-form').addEventListener('change', event => {
    if (event.target.name !== 'market_mode') return;
    const saved = state.dashboard?.settings?.market_mode || '';
    state.marketDirty = event.currentTarget.elements.market_mode.value !== saved;
    persistScannerDrafts();
    renderMarketSelection(state.dashboard?.settings || {}, !!state.dashboard?.settings);
  });
  $('#market-form').addEventListener('submit', async event => {
    event.preventDefault();
    const mode = event.currentTarget.elements.market_mode.value;
    if (!marketNames[mode] || !state.marketDirty || state.marketSaving) return;
    state.marketSaving = true; renderMarketSelection(state.dashboard?.settings || {}, !!state.dashboard?.settings);
    try {
      await api('/api/settings', json('PUT', {market_mode:mode}));
      state.marketDirty = false;
      if (state.dashboard?.settings) state.dashboard.settings.market_mode = mode;
      persistScannerDrafts();
      await refreshAfterMutation(() => refreshDashboard(true));
      notice(`Markets set to ${marketNames[mode]}.`);
    } catch (e) { notice(errorText(e), true); }
    finally { state.marketSaving = false; renderMarketSelection(state.dashboard?.settings || {}, !!state.dashboard?.settings); }
  });
  $('#stock-symbols-form').addEventListener('input', event => {
    const symbols = watchlistSymbols(event.currentTarget.elements.stock_symbols.value), saved = state.dashboard?.settings?.stock_symbols;
    state.watchlistDirty = !Array.isArray(saved) || !validWatchlist(symbols) || JSON.stringify(symbols) !== JSON.stringify(saved);
    persistScannerDrafts(); renderWatchlist(state.dashboard?.settings || {}, !!state.dashboard?.settings);
  });
  $('#stock-symbols-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget, symbols = watchlistSymbols(form.elements.stock_symbols.value);
    if (!state.watchlistDirty || state.watchlistSaving || state.scanRunning) return;
    if (!symbols.length || symbols.length > 25) { notice('The stock watchlist must contain 1 to 25 symbols.', true); return; }
    if (symbols.some(symbol => !/^[A-Z]{1,5}(?:[.-][A-Z]{1,4})?$/.test(symbol))) { notice('Use US ticker syntax, such as IBM or BRK.B.', true); return; }
    if (new Set(symbols).size !== symbols.length) { notice('The stock watchlist cannot contain duplicate symbols.', true); return; }
    state.watchlistSaving = true; renderWatchlist(state.dashboard?.settings || {}, !!state.dashboard?.settings);
    try {
      await api('/api/settings', json('PUT', {stock_symbols:symbols}));
      state.watchlistDirty = false;
      if (state.dashboard?.settings) state.dashboard.settings.stock_symbols = symbols;
      persistScannerDrafts();
      await refreshAfterMutation(() => refreshDashboard(true));
      notice('Alpha Vantage stock watchlist saved.');
    } catch (e) { notice(errorText(e), true); }
    finally { state.watchlistSaving = false; renderWatchlist(state.dashboard?.settings || {}, !!state.dashboard?.settings); }
  });
  $('#stock-import-form').addEventListener('input', persistScannerDrafts);
  $('#stock-import-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget, csv = form.elements.csv.value;
    if (state.importSaving) return;
    const rows = csv.split(/\r?\n/).filter(row => row.trim());
    if (rows.length < 2 || rows.length > 26) { notice('Include a header and 1 to 25 quote rows.', true); return; }
    state.importSaving = true; form.elements.csv.disabled = true; $('button[type=submit]', form).disabled = true;
    try {
      const result = await api('/api/stock-import', json('POST', {csv}));
      if (!Number.isInteger(result.imported) || result.imported < 1 || result.imported > 25) throw new Error('The import result could not be confirmed. Verify before retrying.');
      const warnings = warningMessages(result);
      form.elements.csv.value = ''; persistScannerDrafts();
      const message = `Imported ${result.imported} quote${result.imported === 1 ? '' : 's'}. View them on the dashboard when Stocks or Both is selected. The previous manual import was replaced.`;
      $('#stock-import-status').textContent = warnings.length ? `${message}\n${warnings.join('\n')}` : message;
      try { await refreshDashboard(true); }
      catch (_) { notice(`${message} Dashboard refresh failed; refresh to verify before retrying.`, true); return; }
      notice(warnings.length ? `${message} Warnings: ${warnings.join(' · ')}` : message, false, warnings.length > 0);
    } catch (e) { notice(errorText(e), true); }
    finally { state.importSaving = false; form.elements.csv.disabled = false; $('button[type=submit]', form).disabled = false; }
  });
  $('#market-data-form').addEventListener('submit', event => {
    event.preventDefault();
    const form = event.currentTarget, key = form.elements.api_key.value.trim();
    if (!key) { notice('Enter a new Alpha Vantage API key to save it.', true); return; }
    runAction($('button[type=submit]', form), async () => {
      await api('/api/market-data', json('PUT', {api_key:key}));
      form.elements.api_key.value = '';
      await refreshAfterMutation(refreshMarketData);
    }, 'Stock market data key saved.');
  });
  $('#clear-market-key').addEventListener('click', async event => {
    const button = event.currentTarget;
    await runAction(button, async () => {
      await api('/api/market-data', json('PUT', {clear_key:true}));
      $('#market-data-form').elements.api_key.value = '';
      await refreshAfterMutation(refreshMarketData);
    }, 'Stock market data key cleared.');
    button.disabled = !state.marketData?.has_api_key;
  });
  function replaceTradeDraft() {
    const form = $('#trade-form');
    const hasDraft = ['symbol', 'quantity', 'price', 'reason'].some(name => form.elements[name].value !== '' || form.elements[name].validity.badInput) || form.elements.asset_type.value !== 'stock' || form.elements.side.value !== 'BUY';
    if (hasDraft && !window.confirm('Replace the unsaved paper trade form? Your current entries will be cleared.')) return false;
    form.reset();
    $('#quote-prefill').hidden = true;
    return true;
  }
  function focusTradeForm(name) {
    if ($('#page-ledger').hidden) $('[data-page="ledger"]').click();
    const form = $('#trade-form');
    form.scrollIntoView({block:'start', behavior:'auto'});
    form.elements[name].focus({preventScroll:true});
  }
  $$('[data-holdings-filter]').forEach(button => button.addEventListener('click', () => {
    state.holdingsFilter = button.dataset.holdingsFilter;
    renderPositions(state.dashboard?.positions);
  }));
  $$('[data-add-holding]').forEach(button => button.addEventListener('click', () => {
    if (!replaceTradeDraft()) return;
    const type = button.dataset.addHolding, form = $('#trade-form');
    form.elements.asset_type.value = type;
    form.elements.side.value = 'BUY';
    const prefill = $('#quote-prefill');
    prefill.textContent = `Paper ${type} BUY prepared. Enter a symbol, quantity, quote price and reason; nothing has been submitted.`;
    prefill.hidden = false;
    focusTradeForm('symbol');
  }));
  $('#positions-body').addEventListener('click', event => {
    const button = event.target.closest('[data-sell-position]');
    if (!button || !event.currentTarget.contains(button)) return;
    const position = state.dashboard?.positions?.find(item => String(item.id) === button.dataset.sellPosition);
    if (!position || !['stock', 'crypto'].includes(position.asset_type) || !position.symbol || !present(position.quantity) || Number(position.quantity) <= 0) { notice('That holding is no longer available. Refresh and try again.', true); return; }
    if (!replaceTradeDraft()) return;
    const form = $('#trade-form');
    form.elements.symbol.value = position.symbol;
    form.elements.asset_type.value = position.asset_type;
    form.elements.side.value = 'SELL';
    const prefill = $('#quote-prefill');
    prefill.textContent = `Paper SELL prepared for ${position.symbol} (${position.asset_type}). Enter quantity, quote price and reason; nothing has been submitted.`;
    prefill.hidden = false;
    focusTradeForm('quantity');
  });
  $('#candidates-body').addEventListener('click', event => {
    const button = event.target.closest('[data-use-quote]');
    if (!button || !event.currentTarget.contains(button)) return;
    const candidate = state.dashboard?.candidates?.[Number(button.dataset.useQuote)];
    if (!candidate || candidate.asset_type !== 'stock' || !candidate.symbol || !present(candidate.price_usd) || Number(candidate.price_usd) <= 0) { notice('That stock quote is no longer available. Refresh and try again.', true); return; }
    if (!replaceTradeDraft()) return;
    const form = $('#trade-form');
    form.elements.symbol.value = candidate.symbol; form.elements.asset_type.value = 'stock'; form.elements.price.value = String(candidate.price_usd);
    const prefill = $('#quote-prefill');
    prefill.textContent = `Prefilled ${candidate.symbol} from ${candidate.source || 'an unavailable source'}, ${candidate.cache_status === 'manual' ? 'user-supplied date' : 'trading day'} ${candidate.quote_date || 'unavailable'}. ${stockQuoteStatus(candidate)}. This quote may be delayed or stale${candidate.cache_status === 'manual' ? ' and is not independently verified' : ''}; verify it, then enter quantity and reason and submit manually.`;
    prefill.hidden = false;
    focusTradeForm('quantity');
  });
  $('#trade-form').addEventListener('input', event => { if (['symbol', 'asset_type', 'price'].includes(event.target.name)) $('#quote-prefill').hidden = true; });
  $('#trade-form').addEventListener('change', event => { if (['symbol', 'asset_type', 'price'].includes(event.target.name)) $('#quote-prefill').hidden = true; });
  $('#auto-toggle').addEventListener('click', event => { const button = event.currentTarget; const next = button.getAttribute('aria-checked') !== 'true'; runAction(button, async () => { await api('/api/settings', json('PUT', {auto_paper:next})); await refreshAfterMutation(() => refreshDashboard()); }, next ? 'Automatic paper execution enabled.' : 'Automatic paper execution stopped.'); });
  $('#trade-form').addEventListener('submit', event => {
    event.preventDefault();
    const form = event.currentTarget, submit = $('button[type=submit]', form);
    const draft = Object.fromEntries(new FormData(form));
    const data = {...draft, symbol:draft.symbol.trim().toUpperCase(), reason:draft.reason.trim(), quantity:Number(draft.quantity), price:Number(draft.price)};
    if (!data.symbol || !data.reason) { notice('Symbol and reason are required.', true); return; }
    runAction(submit, async () => {
      await api('/api/trades', json('POST',data));
      // Do not erase a different draft entered while the paper order was in flight.
      if (Object.keys(draft).every(key => form.elements[key].value === draft[key])) { form.reset(); $('#quote-prefill').hidden = true; }
      await refreshAfterMutation(() => refreshDashboard(true));
    }, 'Paper trade recorded.');
  });
  $('#positions-body').addEventListener('input', event => {
    if (!event.target.matches('.mark-form input')) return;
    const id = event.target.closest('.mark-form').dataset.id;
    if (event.target.value) markDrafts.set(id, event.target.value); else markDrafts.delete(id);
  });
  $('#positions-body').addEventListener('submit', event => {
    if (!event.target.matches('.mark-form')) return;
    event.preventDefault();
    const form = event.target, id = form.dataset.id, value = form.elements.price.value;
    if (markPending.has(id)) return;
    markPending.add(id);
    runAction($('button', form), async () => {
      try {
        await api(`/api/positions/${encodeURIComponent(id)}/mark`, json('PATCH',{price:Number(value)}));
        if (markDrafts.get(id) === value) markDrafts.delete(id);
        await refreshAfterMutation(() => refreshDashboard(true));
      } finally {
        markPending.delete(id);
        const current = $$('.mark-form', $('#positions-body')).find(item => item.dataset.id === id);
        if (current) $('button', current).disabled = false;
      }
    }, 'Mark price updated.');
  });
  $('#settings-form').addEventListener('submit', event => { event.preventDefault(); const form = event.currentTarget; const data = {}; settingFields.forEach(key => { data[key] = Number(form.elements[key].value); }); data.auto_paper = form.elements.auto_paper.checked; runAction($('button[type=submit]',form), async () => { await api('/api/settings',json('PUT',data)); await refreshAfterMutation(() => refreshDashboard()); }, 'Trading settings saved.'); });
  $('#provider-select').addEventListener('change', event => { state.providerId = event.target.value; fillProvider(); });
  $('#provider-form').addEventListener('submit', event => { event.preventDefault(); const form = event.currentTarget, id = state.providerId; if (!id) return; const data = {name:form.elements.name.value.trim(),kind:form.elements.kind.value.trim(),base_url:form.elements.base_url.value.trim(),model:form.elements.model.value.trim(),clear_key:form.elements.clear_key.checked,enabled:form.elements.enabled.checked}; const key = form.elements.api_key.value; if (key && data.clear_key) { notice('Choose either a new API key or clear the saved key, not both.', true); return; } if (key) data.api_key = key; runAction($('button[type=submit]',form), async () => { await api(`/api/llm/${encodeURIComponent(id)}`,json('PUT',data)); form.elements.api_key.value = ''; await refreshProviders(); }, 'Provider settings saved.'); });
  $('#activate-provider').addEventListener('click', event => { if (!state.providerId) return; runAction(event.currentTarget, async () => { await api('/api/llm/active/select', json('PUT',{id:state.providerId})); await refreshProviders(); }, 'Active provider updated.'); });
  $('#test-provider').addEventListener('click', event => { if (!state.providerId) return; runAction(event.currentTarget, async () => { const data = await api(`/api/llm/${encodeURIComponent(state.providerId)}/test`,{method:'POST'}); if (data.success === false || data.ok === false) throw new Error(data.message || data.error || 'Provider connection test failed.'); }, 'Provider connection test completed.'); });
  $('#analyst-form').addEventListener('submit', event => { event.preventDefault(); const form = event.currentTarget, id = state.llm?.active_provider; const context = form.elements.context.value.trim(); if (!id) { notice('Select an active provider before requesting analysis.', true); return; } if (!context) { notice('Enter context for the analyst.', true); return; } runAction($('button[type=submit]',form), async () => { const data = await api(`/api/llm/${encodeURIComponent(id)}/analyze`,json('POST',{context})); if (data.success === false || data.ok === false) throw new Error(data.message || data.error || 'Analysis failed.'); const result = typeof data === 'string' ? data : data.analysis ?? data.response ?? data.result ?? data.content ?? data.message ?? JSON.stringify(data,null,2); $('#analyst-result').textContent = typeof result === 'string' ? result : JSON.stringify(result,null,2); $('#analyst-result').hidden = false; }, 'Analysis complete.'); });
  $('#add-provider-form').addEventListener('submit', event => { event.preventDefault(); const form = event.currentTarget, id = form.elements.provider_id.value.trim(); if (!/^[a-z0-9][a-z0-9._-]{0,63}$/.test(id)) { notice('Provider ID must use lowercase letters, digits, hyphens, underscores or dots.', true); return; } if (state.llm?.providers?.some(p => p.id === id)) { notice('That provider ID already exists. Select it above to edit it.', true); return; } runAction($('button[type=submit]', form), async () => { await api(`/api/llm/${encodeURIComponent(id)}`, json('PUT',{name:id,kind:'openai',enabled:true})); state.providerId = id; form.reset(); await refreshProviders(); }, 'Provider added. Configure its model and base URL above.'); });
  let usersBusy = false;
  function userStatus(message, error = false) {
    const el = $('#users-status'); el.textContent = message; el.classList.toggle('error', error);
  }
  function setUsersBusy(busy) {
    usersBusy = busy;
    $('#user-admin-controls').disabled = busy;
    $('#users-refresh').disabled = busy;
  }
  function userError(error) {
    if (error?.status === 403) return 'Administrator access is required to manage users.';
    if (error?.status === 400 || error?.status === 409 || error?.status === 422) return 'The change was not accepted. Check the username and password requirements; the username may already be in use. You cannot disable, delete, or demote yourself or the last active administrator.';
    return 'Unable to complete the request. Check your connection and try again.';
  }
  function element(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function userButton(text, username, extraClass = '') {
    const button = element('button', `button button-secondary ${extraClass}`, text);
    button.type = 'button';
    button.setAttribute('aria-label', `${text} for ${username}`);
    return button;
  }
  function passwordValid(input) {
    const length = new TextEncoder().encode(input.value).length;
    if (length < 12 || length > 128) { userStatus('Passwords must be 12–128 UTF-8 bytes.', true); return false; }
    return true;
  }
  async function fetchUsers() {
    const result = await api('/api/users');
    if (!Array.isArray(result.users)) throw new Error('Invalid user list.');
    renderUsers(result.users);
  }
  async function loadUsers() {
    if (!isAdmin() || isLocalTestMode() || usersBusy || authLost) return;
    setUsersBusy(true); userStatus('Loading users…');
    try { await fetchUsers(); userStatus('User list is up to date.'); }
    catch (e) { if (!authLost) userStatus(userError(e), true); }
    finally { setUsersBusy(false); }
  }
  async function mutateUser(action, message, selfReset = false) {
    if (!isAdmin() || usersBusy || authLost) return;
    setUsersBusy(true); userStatus('Saving changes…');
    try {
      await action();
      if (selfReset) { redirectToLogin(); return; }
      try { await fetchUsers(); userStatus(message); }
      catch (_) { if (!authLost) userStatus('Change completed, but the user list could not be refreshed. Refresh the list before retrying.', true); }
    } catch (e) { if (!authLost) userStatus(userError(e), true); }
    finally { setUsersBusy(false); }
  }
  function renderUsers(users) {
    const list = $('#users-list');
    list.replaceChildren();
    if (!users.length) { list.append(element('p', 'users-status', 'No users found.')); return; }
    const activeAdmins = users.filter(user => user.role === 'admin' && user.enabled === true).length;
    users.forEach(user => {
      const username = String(user.username ?? '');
      const id = encodeURIComponent(String(user.id));
      const self = username === session.username;
      const lastAdmin = user.role === 'admin' && user.enabled === true && activeAdmins <= 1;
      const row = element('article', 'user-row');
      const identity = element('div', 'user-identity');
      identity.append(element('strong', 'user-name', username), element('span', 'user-state', `${user.enabled ? 'Enabled' : 'Disabled'} · ${user.role === 'admin' ? 'Admin' : 'User'}${self ? ' · You' : ''}`));
      row.append(identity);

      const roleForm = element('form', 'user-role-form');
      const roleLabel = element('label', '', 'Role');
      const role = element('select'); role.name = 'role'; role.setAttribute('aria-label', `Role for ${username}`);
      for (const [value, label] of [['user','User'],['admin','Admin']]) { const option = element('option', '', label); option.value = value; role.append(option); }
      role.value = user.role === 'admin' ? 'admin' : 'user';
      role.disabled = self || lastAdmin;
      roleLabel.append(role);
      const roleSubmit = element('button', 'button button-secondary', 'Save role'); roleSubmit.type = 'submit'; roleSubmit.setAttribute('aria-label', `Save role for ${username}`); roleSubmit.disabled = self || lastAdmin;
      roleForm.append(roleLabel, roleSubmit);
      roleForm.addEventListener('submit', event => {
        event.preventDefault();
        if (role.disabled || role.value === user.role) return;
        mutateUser(() => api(`/api/users/${id}`, json('PATCH', {role:role.value})), 'Role updated.');
      });
      row.append(roleForm);

      const actions = element('div', 'user-row-actions');
      const toggle = userButton(user.enabled ? 'Disable' : 'Enable', username);
      toggle.disabled = user.enabled && (self || lastAdmin);
      toggle.addEventListener('click', () => {
        if (user.enabled && !window.confirm(`Disable ${username}? They will no longer be able to sign in.`)) return;
        mutateUser(() => api(`/api/users/${id}`, json('PATCH', {enabled:!user.enabled})), user.enabled ? 'User disabled.' : 'User enabled.');
      });
      const remove = userButton('Delete', username, 'user-danger');
      remove.disabled = self || lastAdmin;
      remove.addEventListener('click', () => {
        if (!window.confirm(`Delete ${username}? This cannot be undone.`)) return;
        mutateUser(() => api(`/api/users/${id}`, {method:'DELETE'}), 'User deleted.');
      });
      actions.append(toggle, remove); row.append(actions);

      const resetForm = element('form', 'reset-password-form');
      const resetLabel = element('label', '', 'New password');
      const password = element('input'); password.type = 'password'; password.name = 'password'; password.required = true; password.maxLength = 128; password.autocomplete = 'new-password'; password.setAttribute('aria-label', `New password for ${username}`);
      resetLabel.append(password);
      const resetButton = element('button', 'button button-secondary', 'Reset password'); resetButton.type = 'submit'; resetButton.setAttribute('aria-label', `Reset password for ${username}`);
      resetForm.append(resetLabel, resetButton);
      resetForm.addEventListener('submit', async event => {
        event.preventDefault();
        if (usersBusy || !resetForm.reportValidity() || !passwordValid(password)) return;
        try { await mutateUser(async () => { await api(`/api/users/${id}/password`, json('POST', {password:password.value})); password.value = ''; }, 'Password reset.', self); }
        finally { password.value = ''; }
      });
      row.append(resetForm);
      if (self || lastAdmin) row.append(element('div', 'user-lock-note', self ? 'Your own account cannot be disabled, deleted, or demoted. Resetting your password signs you out.' : 'The last active administrator cannot be disabled, deleted, or demoted.'));
      list.append(row);
    });
  }
  $('#users-refresh').addEventListener('click', loadUsers);
  $('#create-user-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget, password = form.elements.password;
    if (usersBusy || !form.reportValidity() || !passwordValid(password)) return;
    try {
      await mutateUser(async () => {
        await api('/api/users', json('POST', {username:form.elements.username.value, password:password.value, role:form.elements.role.value}));
        form.reset();
      }, 'User created.');
    } finally { password.value = ''; }
  });

  $('#sign-out').addEventListener('click', async event => {
    const button = event.currentTarget;
    if (button.disabled) return;
    button.disabled = true;
    try { await api('/api/auth/logout', {method:'POST'}); redirectToLogin(); }
    catch (e) { if (!authLost) { notice('Sign out could not be completed. Check your connection and try again.', true); button.disabled = false; } }
  });
  async function start() {
    try {
      const current = await sessionReady;
      if (authLost) return;
      $('#account-username').textContent = current.username;
      $('#account-role').textContent = isLocalTestMode() ? 'admin · local test' : current.role;
      $('#sign-out').hidden = isLocalTestMode();
      $('#local-auth-message').hidden = !isLocalTestMode();
      $('#settings-user-message').hidden = isAdmin();
      $('#admin-settings').hidden = !isAdmin();
      $('#user-admin-panel').hidden = !isAdmin() || isLocalTestMode();
      if (!isAdmin()) $('.market-copy p').textContent = 'Scanner and manual cycles use the administrator’s saved market selection and Alpha Vantage watchlist. Only administrators can change these settings or import Yahoo Finance quotes. Auto-paper cycles buy eligible crypto only; stocks are never bought or marked automatically. No background worker runs.';
      if (isAdmin()) restoreScannerDrafts();
      renderDashboard({}); renderStockContext(); fillProvider(); restoreScanWarnings();
      $('#session-loading').hidden = true;
      $('.app-shell').hidden = false;
      await refreshAll();
    } catch (_) {
      if (!authLost) { $('#session-loading').textContent = 'Unable to verify your session. Check your connection and reload this page.'; }
    }
  }
  start();
  let pollInFlight = false;
  setInterval(async () => { if (!session || authLost || document.hidden || pollInFlight) return; pollInFlight = true; try { await refreshDashboard(true); } catch (e) { if (!authLost) { setConnection(false); notice(`Dashboard refresh failed: ${errorText(e)}`, true); } } finally { pollInFlight = false; } }, 15000);
})();
