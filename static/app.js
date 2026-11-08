(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const state = { dashboard: null, llm: null, providerId: '', marketData: null, marketDirty: false, marketSaving: false, watchlistDirty: false, watchlistSaving: false, busy: new Set() };
  const settingFields = ['starting_balance', 'risk_percent', 'max_position_percent', 'min_liquidity', 'min_volume', 'slippage_bps', 'fee_bps', 'stop_loss_percent', 'take_profit_percent', 'scan_interval_seconds'];
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const present = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
  const money = value => present(value) ? new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',maximumFractionDigits:2}).format(Number(value)) : '—';
  const price = value => present(value) ? new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:18}).format(Number(value)) : '—';
  const number = value => present(value) ? new Intl.NumberFormat('en-US', {maximumFractionDigits:18}).format(Number(value)) : '—';
  const date = value => { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.valueOf()) ? String(value) : d.toLocaleString(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}); };
  const shortDate = value => { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.valueOf()) ? String(value) : d.toLocaleDateString(undefined, {month:'short',day:'numeric',year:'numeric'}); };
  function notice(message, error = false, warning = false) { const el = $('#notice'); el.textContent = message; el.classList.toggle('error', error); el.classList.toggle('warning', warning); el.hidden = !message; }
  function errorText(error) { return error instanceof Error ? error.message : String(error); }
  async function api(path, options = {}) {
    let response;
    try { response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } }); }
    catch (_) { throw new Error('Cannot reach the application server. Check the connection and try again.'); }
    const raw = await response.text(); let data = {};
    if (raw) { try { data = JSON.parse(raw); } catch (_) { if (!response.ok) throw new Error(`Request failed (${response.status}).`); throw new Error('The server returned an invalid response.'); } }
    if (!response.ok) { const detail = data.detail ?? data.error ?? data.message; throw new Error(typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `Request failed (${response.status}).`); }
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
      const source = escapeHtml(item.source || (stock ? 'Alpha Vantage' : 'DexScreener'));
      const asset = `${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer"><strong>${symbol} ↗</strong></a>` : `<strong>${symbol}</strong>`}<small>${stock ? 'STOCK' : 'CRYPTO'} · Source: ${source}</small>${!stock ? `<small title="${escapeHtml(item.pair_address || '')}">${escapeHtml(item.chain || '—')}${item.pair_address ? ` · ${escapeHtml(item.pair_address)}` : ''}</small>` : ''}`;
      const quote = stock ? `<small>Trading day: ${escapeHtml(item.quote_date || 'Unavailable')}</small>` : item.observed_at ? `<small>Observed ${escapeHtml(date(item.observed_at))}</small>` : '';
      const change = stock ? `${present(item.stock_change_percent) ? `${number(item.stock_change_percent)}%` : '—'}<small>Close-to-close</small>` : `${present(item.price_change_24h) ? `${number(item.price_change_24h)}%` : '—'}<small>24h</small>`;
      const liquidity = stock ? '—<small>Not assessed</small>' : `${money(item.liquidity_usd)}<small>USD</small>`;
      const volume = stock ? `${number(item.stock_volume_shares)}<small>Shares · quote day</small>` : `${money(item.volume_24h)}<small>USD · 24h</small>`;
      const trend = stock ? '—' : `${present(item.volume_spike_ratio) ? `${number(item.volume_spike_ratio)}×` : '—'}<small>1h vs prior 23h avg</small>`;
      const review = stock ? '<span class="risk-pill">Not assessed</span><small>No stock liquidity or rug assessment</small>' : `<span class="risk-pill" title="${escapeHtml(item.reason || '')}">${escapeHtml(item.risk_status || '—')}</span>${item.reason ? `<small title="${escapeHtml(item.reason)}">${escapeHtml(item.reason)}</small>` : ''}`;
      const action = stock && item.symbol && present(item.price_usd) && Number(item.price_usd) > 0 ? `<button class="text-button quote-button" type="button" data-use-quote="${index}" aria-label="Use quote for ${symbol} in the ledger">Use quote <svg><use href="#i-arrow"/></svg></button>` : '';
      return `<tr><td>${asset}${action}</td><td>${price(item.price_usd)}${quote}</td><td>${change}</td><td>${liquidity}</td><td>${volume}</td><td>${trend}</td><td>${review}</td></tr>`;
    }).join('');
  }
  function renderPositions(items) {
    if (document.activeElement && document.activeElement.matches('.mark-form input')) return;
    items = Array.isArray(items) ? items : []; $('#positions-empty').hidden = items.length > 0;
    $('#positions-body').innerHTML = items.map(item => `<tr><td><strong>${escapeHtml(item.symbol || '—')}</strong><small>${escapeHtml(item.asset_type || '')}</small></td><td>${number(item.quantity)}</td><td>${price(item.entry_price)}</td><td>${price(item.mark_price)}<small>${escapeHtml(item.mark_source || 'Unmarked')}${item.mark_timestamp ? ` · ${escapeHtml(date(item.mark_timestamp))}` : ''}</small></td><td class="${present(item.unrealized_pnl) && Number(item.unrealized_pnl) > 0 ? 'positive' : present(item.unrealized_pnl) && Number(item.unrealized_pnl) < 0 ? 'negative' : ''}">${money(item.unrealized_pnl)}</td><td><form class="mark-form" data-id="${escapeHtml(item.id)}"><input type="number" min="0.000000000000000001" step="any" name="price" required aria-label="New mark price for ${escapeHtml(item.symbol || 'position')}" placeholder="Price"><button class="button button-secondary" type="submit">Update</button></form></td></tr>`).join('');
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
  function renderMarketSelection(settings, available) {
    const form = $('#market-form'), saved = marketNames[settings.market_mode] ? settings.market_mode : 'crypto';
    if (available && !state.marketDirty && !state.marketSaving) form.elements.market_mode.value = saved;
    const selected = form.elements.market_mode.value;
    $('#market-options').disabled = !available || state.marketSaving;
    $('button[type=submit]', form).disabled = !available || state.marketSaving || !state.marketDirty;
    $('#market-current').textContent = !available ? 'Market selection unavailable.' : state.marketDirty && selected !== saved ? `Current: ${marketNames[saved]} · ${marketNames[selected]} not saved` : `Current: ${marketNames[saved]}`;
  }
  function renderWatchlist(settings, available) {
    const form = $('#stock-symbols-form');
    form.elements.stock_symbols.disabled = !available || state.watchlistSaving;
    $('button[type=submit]', form).disabled = !available || state.watchlistSaving;
    if (available && !state.watchlistDirty && !state.watchlistSaving) form.elements.stock_symbols.value = (Array.isArray(settings.stock_symbols) ? settings.stock_symbols : ['IBM']).join(', ');
  }
  function renderMarketData(data) {
    state.marketData = data;
    const provider = data.stock_provider || 'Alpha Vantage';
    $('#market-data-status').textContent = data.using_demo ? `${provider} · Demo mode: IBM only (no key configured).` : data.has_api_key ? `${provider} · API key saved; never displayed.` : `${provider} · No API key saved.`;
    $('#clear-market-key').disabled = !data.has_api_key;
  }
  function warningMessages(data) {
    return Array.isArray(data?.warnings) ? data.warnings.map(item => typeof item === 'string' ? item : item?.message || item?.detail || JSON.stringify(item)).filter(Boolean) : [];
  }
  function showScanWarnings(warnings) {
    const el = $('#scan-warnings');
    el.textContent = warnings.length ? `Scanner notices — results may be incomplete:\n${warnings.map(message => `• ${message}`).join('\n')}` : '';
    el.hidden = !warnings.length;
  }
  function renderDashboard(data, preserveSettings = false) {
    state.dashboard = data; const s = data.summary || {};
    setValue('#metric-equity', money(s.equity)); setValue('#metric-cash', money(s.cash)); setPnl('#metric-realized', s.realized_pnl); setValue('#metric-positions', number(s.open_positions));
    $('#metric-unrealized').textContent = `Unrealized P&L: ${money(s.unrealized_pnl)}`;
    $('#last-updated').textContent = data.summary ? `UPDATED ${new Date().toLocaleTimeString(undefined, {hour:'numeric',minute:'2-digit'})}` : 'WAITING FOR DATA';
    renderChart(data.equity_curve); renderAgents(data.agents); renderCandidates(data.candidates); renderPositions(data.positions); renderTrades(data.trades); renderActivity(data.activity);
    const settings = data.settings || {}; const hasAuto = typeof settings.auto_paper === 'boolean';
    const toggle = $('#auto-toggle'); toggle.disabled = !hasAuto; toggle.setAttribute('aria-checked', String(hasAuto && settings.auto_paper));
    $('#auto-description').textContent = !hasAuto ? 'Setting unavailable' : settings.auto_paper ? 'Enabled · eligible crypto, manual cycles only' : 'Blocked · manual cycles only';
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
  async function refreshDashboard(preserveSettings = false) { const data = await api('/api/dashboard'); renderDashboard(data, preserveSettings); setConnection(true); return data; }
  async function refreshProviders() { const data = await api('/api/llm'); renderProviders(data); return data; }
  async function refreshMarketData() { const data = await api('/api/market-data'); renderMarketData(data); return data; }
  async function refreshAfterMutation(fn) { try { await fn(); } catch (e) { throw new Error(`Action completed, but refresh failed. Verify the result before retrying. ${errorText(e)}`); } }
  async function refreshAll(showSuccess = false) {
    const results = await Promise.allSettled([refreshDashboard(), refreshProviders(), api('/api/health'), refreshMarketData()]);
    if (results[3].status === 'rejected') { $('#market-data-status').textContent = 'Stock provider settings unavailable.'; $('#clear-market-key').disabled = true; }
    if (results[0].status === 'rejected') { setConnection(false); notice(`Dashboard unavailable: ${errorText(results[0].reason)}`, true); }
    else if (results[1].status === 'rejected') notice(`Provider settings unavailable: ${errorText(results[1].reason)}`, true);
    else if (results[2].status === 'rejected') { setConnection(false); notice(`Health check unavailable: ${errorText(results[2].reason)}`, true); }
    else if (results[3].status === 'rejected') notice(`Stock provider settings unavailable: ${errorText(results[3].reason)}`, true);
    else if (showSuccess) notice('Data refreshed.');
    else notice('');
  }
  async function runAction(button, task, success) {
    if (button.disabled) return; button.disabled = true;
    try { const result = await task(); const warnings = warningMessages(result); notice(warnings.length ? `${success} Warnings: ${warnings.join(' · ')}` : success, false, warnings.length > 0); } catch (e) { notice(errorText(e), true); } finally { button.disabled = false; }
  }
  $$('.nav-link').forEach(button => button.addEventListener('click', () => { const page = button.dataset.page; $$('.page').forEach(el => { el.hidden = el.id !== `page-${page}`; }); $$('.nav-link').forEach(el => { const active = el === button; el.classList.toggle('active', active); if (active) el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current'); }); $('#breadcrumb-page').textContent = page.toUpperCase(); window.scrollTo(0,0); }));
  function updateThemeButton() { const light = document.documentElement.dataset.theme === 'light'; $('#theme-toggle').setAttribute('aria-label', light ? 'Switch to dark mode' : 'Switch to light mode'); }
  updateThemeButton(); $('#theme-toggle').addEventListener('click', () => { const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = theme; try { localStorage.setItem('ledger-six-theme', theme); } catch (_) {} updateThemeButton(); });
  $('#refresh-button').addEventListener('click', async event => { const button = event.currentTarget; button.disabled = true; try { await refreshAll(true); } finally { button.disabled = false; } });
  $$('[data-action]').forEach(button => button.addEventListener('click', () => {
    if (state.marketDirty || state.marketSaving) { notice('Save the market selection before running the scanner or cycle.', true); return; }
    return runAction(button, async () => { const result = await api(`/api/${button.dataset.action}`, {method:'POST'}); showScanWarnings(warningMessages(result)); await refreshAfterMutation(() => refreshDashboard(true)); return result; }, button.dataset.action === 'scan' ? 'Scanner completed.' : 'Paper cycle completed.');
  }));
  $('#market-form').addEventListener('change', event => {
    if (event.target.name !== 'market_mode') return;
    const saved = state.dashboard?.settings?.market_mode || 'crypto';
    state.marketDirty = event.currentTarget.elements.market_mode.value !== saved;
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
      await refreshAfterMutation(() => refreshDashboard(true));
      notice(`Markets set to ${marketNames[mode]}.`);
    } catch (e) { notice(errorText(e), true); }
    finally { state.marketSaving = false; renderMarketSelection(state.dashboard?.settings || {}, !!state.dashboard?.settings); }
  });
  $('#stock-symbols-form').addEventListener('input', () => { state.watchlistDirty = true; });
  $('#stock-symbols-form').addEventListener('submit', event => {
    event.preventDefault();
    const form = event.currentTarget, symbols = form.elements.stock_symbols.value.split(',').map(symbol => symbol.trim().toUpperCase()).filter(Boolean);
    if (!symbols.length || symbols.length > 20) { notice('The stock watchlist must contain 1 to 20 symbols.', true); return; }
    if (symbols.some(symbol => !/^[A-Z]{1,5}(?:[.-][A-Z]{1,4})?$/.test(symbol))) { notice('Use US ticker syntax, such as IBM or BRK.B.', true); return; }
    if (new Set(symbols).size !== symbols.length) { notice('The stock watchlist cannot contain duplicate symbols.', true); return; }
    runAction($('button[type=submit]', form), async () => {
      state.watchlistSaving = true; renderWatchlist(state.dashboard?.settings || {}, !!state.dashboard?.settings);
      try {
        await api('/api/settings', json('PUT', {stock_symbols:symbols}));
        state.watchlistDirty = false;
        if (state.dashboard?.settings) state.dashboard.settings.stock_symbols = symbols;
        await refreshAfterMutation(() => refreshDashboard(true));
      } finally { state.watchlistSaving = false; renderWatchlist(state.dashboard?.settings || {}, !!state.dashboard?.settings); }
    }, 'Stock watchlist saved.');
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
  $('#candidates-body').addEventListener('click', event => {
    const button = event.target.closest('[data-use-quote]');
    if (!button || !event.currentTarget.contains(button)) return;
    const candidate = state.dashboard?.candidates?.[Number(button.dataset.useQuote)];
    if (!candidate || candidate.asset_type !== 'stock' || !candidate.symbol || !present(candidate.price_usd) || Number(candidate.price_usd) <= 0) { notice('That stock quote is no longer available. Refresh and try again.', true); return; }
    const form = $('#trade-form');
    form.reset(); form.elements.symbol.value = candidate.symbol; form.elements.asset_type.value = 'stock'; form.elements.price.value = String(candidate.price_usd);
    const prefill = $('#quote-prefill');
    prefill.textContent = `Prefilled ${candidate.symbol} from trading day ${candidate.quote_date || 'unavailable'}. This quote may be stale; verify it, then enter quantity and reason and submit manually.`;
    prefill.hidden = false;
    $('[data-page="ledger"]').click(); form.elements.quantity.focus();
  });
  $('#trade-form').addEventListener('input', event => { if (['symbol', 'asset_type', 'price'].includes(event.target.name)) $('#quote-prefill').hidden = true; });
  $('#trade-form').addEventListener('change', event => { if (['symbol', 'asset_type', 'price'].includes(event.target.name)) $('#quote-prefill').hidden = true; });
  $('#auto-toggle').addEventListener('click', event => { const button = event.currentTarget; const next = button.getAttribute('aria-checked') !== 'true'; runAction(button, async () => { await api('/api/settings', json('PUT', {auto_paper:next})); await refreshAfterMutation(() => refreshDashboard()); }, next ? 'Automatic paper execution enabled.' : 'Automatic paper execution stopped.'); });
  $('#trade-form').addEventListener('submit', event => { event.preventDefault(); const form = event.currentTarget, submit = $('button[type=submit]', form); const data = Object.fromEntries(new FormData(form)); data.symbol = data.symbol.trim().toUpperCase(); data.reason = data.reason.trim(); data.quantity = Number(data.quantity); data.price = Number(data.price); if (!data.symbol || !data.reason) { notice('Symbol and reason are required.', true); return; } runAction(submit, async () => { await api('/api/trades', json('POST',data)); form.reset(); $('#quote-prefill').hidden = true; await refreshAfterMutation(() => refreshDashboard(true)); }, 'Paper trade recorded.'); });
  $('#positions-body').addEventListener('submit', event => { if (!event.target.matches('.mark-form')) return; event.preventDefault(); const form = event.target; runAction($('button', form), async () => { await api(`/api/positions/${encodeURIComponent(form.dataset.id)}/mark`, json('PATCH',{price:Number(form.elements.price.value)})); if (form.contains(document.activeElement)) document.activeElement.blur(); await refreshAfterMutation(() => refreshDashboard(true)); }, 'Mark price updated.'); });
  $('#settings-form').addEventListener('submit', event => { event.preventDefault(); const form = event.currentTarget; const data = {}; settingFields.forEach(key => { data[key] = Number(form.elements[key].value); }); data.auto_paper = form.elements.auto_paper.checked; runAction($('button[type=submit]',form), async () => { await api('/api/settings',json('PUT',data)); await refreshAfterMutation(() => refreshDashboard()); }, 'Trading settings saved.'); });
  $('#provider-select').addEventListener('change', event => { state.providerId = event.target.value; fillProvider(); });
  $('#provider-form').addEventListener('submit', event => { event.preventDefault(); const form = event.currentTarget, id = state.providerId; if (!id) return; const data = {name:form.elements.name.value.trim(),kind:form.elements.kind.value.trim(),base_url:form.elements.base_url.value.trim(),model:form.elements.model.value.trim(),clear_key:form.elements.clear_key.checked,enabled:form.elements.enabled.checked}; const key = form.elements.api_key.value; if (key && data.clear_key) { notice('Choose either a new API key or clear the saved key, not both.', true); return; } if (key) data.api_key = key; runAction($('button[type=submit]',form), async () => { await api(`/api/llm/${encodeURIComponent(id)}`,json('PUT',data)); form.elements.api_key.value = ''; await refreshProviders(); }, 'Provider settings saved.'); });
  $('#activate-provider').addEventListener('click', event => { if (!state.providerId) return; runAction(event.currentTarget, async () => { await api('/api/llm/active/select', json('PUT',{id:state.providerId})); await refreshProviders(); }, 'Active provider updated.'); });
  $('#test-provider').addEventListener('click', event => { if (!state.providerId) return; runAction(event.currentTarget, async () => { const data = await api(`/api/llm/${encodeURIComponent(state.providerId)}/test`,{method:'POST'}); if (data.success === false || data.ok === false) throw new Error(data.message || data.error || 'Provider connection test failed.'); }, 'Provider connection test completed.'); });
  $('#analyst-form').addEventListener('submit', event => { event.preventDefault(); const form = event.currentTarget, id = state.llm?.active_provider; const context = form.elements.context.value.trim(); if (!id) { notice('Select an active provider before requesting analysis.', true); return; } if (!context) { notice('Enter context for the analyst.', true); return; } runAction($('button[type=submit]',form), async () => { const data = await api(`/api/llm/${encodeURIComponent(id)}/analyze`,json('POST',{context})); if (data.success === false || data.ok === false) throw new Error(data.message || data.error || 'Analysis failed.'); const result = typeof data === 'string' ? data : data.analysis ?? data.response ?? data.result ?? data.content ?? data.message ?? JSON.stringify(data,null,2); $('#analyst-result').textContent = typeof result === 'string' ? result : JSON.stringify(result,null,2); $('#analyst-result').hidden = false; }, 'Analysis complete.'); });
  $('#add-provider-form').addEventListener('submit', event => { event.preventDefault(); const form = event.currentTarget, id = form.elements.provider_id.value.trim(); if (!/^[a-z0-9][a-z0-9._-]{0,63}$/.test(id)) { notice('Provider ID must use lowercase letters, digits, hyphens, underscores or dots.', true); return; } if (state.llm?.providers?.some(p => p.id === id)) { notice('That provider ID already exists. Select it above to edit it.', true); return; } runAction($('button[type=submit]', form), async () => { await api(`/api/llm/${encodeURIComponent(id)}`, json('PUT',{name:id,kind:'openai',enabled:true})); state.providerId = id; form.reset(); await refreshProviders(); }, 'Provider added. Configure its model and base URL above.'); });
  renderDashboard({}); fillProvider(); refreshAll();
  let pollInFlight = false;
  setInterval(async () => { if (document.hidden || pollInFlight) return; pollInFlight = true; try { await refreshDashboard(true); } catch (e) { setConnection(false); notice(`Dashboard refresh failed: ${errorText(e)}`, true); } finally { pollInFlight = false; } }, 15000);
})();
