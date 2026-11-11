"""Optional Chromium coverage of the real stock-source UI and local backend.

Run with pytest after installing Playwright and Chromium. Set
PLAYWRIGHT_CHROMIUM_EXECUTABLE to use a specific browser. No provider, quote,
key-status, dashboard, or simulation endpoint is mocked. A single synthetic
quota warning is added to one otherwise real scan result solely to exercise the
warning lifecycle without making an external request.
"""

import http.client
import ipaddress
import os
import shutil
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest


@pytest.fixture(scope="module")
def browser():
    # Keep Playwright optional, including when this file is run on its own.
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        candidates = [
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", ""),
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            driver.chromium.executable_path,
        ]
        executable = next(
            (
                str(path)
                for candidate in candidates
                if candidate
                for path in [Path(shutil.which(candidate) or candidate).expanduser()]
                if path.is_file() and os.access(path, os.X_OK)
            ),
            None,
        )
        if executable is None:
            pytest.skip("No Chromium executable: install one or set PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        # As in test_holdings_browser, don't hide genuine browser launch errors.
        instance = driver.chromium.launch(
            executable_path=executable,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-background-networking",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            ],
        )
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def live_server(browser, tmp_path, monkeypatch):
    import uvicorn

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_AUTH_ENABLED", "0")
    monkeypatch.setenv("APP_BIND_ADDR", "127.0.0.1")
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)

    from app import db, trading
    from app.main import app

    forbidden_calls = []

    async def forbidden_scan(*args, **kwargs):
        forbidden_calls.append("provider scanner")
        raise AssertionError("A provider scanner was called in the offline browser test")

    real_finnhub_scan = trading._scan_finnhub
    for name in ("_scan_crypto", "_scan_stocks", "_scan_finnhub"):
        monkeypatch.setattr(trading, name, forbidden_scan)

    # Also deny accidental non-loopback Python sockets, independent of browser
    # routing and even if a future backend path bypasses the scanner functions.
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect

    def loopback_host(host):
        try:
            return ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError):
            return host == "localhost"

    def local_getaddrinfo(host, *args, **kwargs):
        if not loopback_host(host):
            forbidden_calls.append(f"DNS: {host}")
            raise AssertionError(f"Non-loopback DNS forbidden: {host}")
        return original_getaddrinfo(host, *args, **kwargs)

    def local_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not loopback_host(address[0]):
            forbidden_calls.append(f"socket: {address}")
            raise AssertionError(f"Non-loopback connection forbidden: {address}")
        return original_connect(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", local_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", local_connect)

    # Use a real scan and its real response schema. Inject a warning only when
    # explicitly armed to simulate an old quota diagnostic; no provider is run.
    real_scan = trading.scan_market
    inject_quota_warning = threading.Event()
    quota_warning = "Test provider quota notice from an earlier scan."

    async def scan_with_test_warning(*args, **kwargs):
        result = await real_scan(*args, **kwargs)
        if inject_quota_warning.is_set():
            inject_quota_warning.clear()
            result["warnings"].append(quota_warning)
        return result

    monkeypatch.setattr(trading, "scan_market", scan_with_test_warning)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", access_log=False,
        lifespan="on", timeout_graceful_shutdown=2,
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not thread.is_alive():
                pytest.fail("Uvicorn stopped before becoming healthy")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            try:
                conn.request("GET", "/api/health")
                response = conn.getresponse()
                if response.status == 200 and server.started:
                    response.read()
                    break
            except OSError:
                pass
            finally:
                conn.close()
            time.sleep(0.05)
        else:
            pytest.fail("Uvicorn did not become healthy on its loopback socket")
        assert db.data_path() == tmp_path / "tradeledger.sqlite3"
        yield SimpleNamespace(
            origin=f"http://127.0.0.1:{port}",
            inject_quota_warning=inject_quota_warning,
            quota_warning=quota_warning,
            real_finnhub_scan=real_finnhub_scan,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "Uvicorn failed to stop"
        assert not forbidden_calls, f"Forbidden network/provider calls: {forbidden_calls}"


@pytest.fixture
def ui(browser, live_server):
    blocked, page_errors = [], []
    origin = urlsplit(live_server.origin)
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")

    def route_request(route):
        url = urlsplit(route.request.url)
        # No API responses are fulfilled or modified by this route: the exact
        # local server must supply the real static files and backend responses.
        if (url.scheme, url.hostname, url.port) != (origin.scheme, origin.hostname, origin.port):
            blocked.append(route.request.url)
            route.abort()
        else:
            route.continue_()

    context.route("**/*", route_request)
    page = context.new_page()
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        yield SimpleNamespace(page=page, server=live_server)
    finally:
        context.close()
        assert not blocked, f"Blocked non-loopback browser requests: {blocked}"
        assert not page_errors, f"Browser JavaScript errors: {page_errors}"


def _response_for(path, method="GET"):
    return lambda response: urlsplit(response.url).path == path and response.request.method == method


def _read_json(page, path):
    # A browser fetch is covered by the same non-loopback route as the UI.
    return page.evaluate("""async path => {
        const response = await fetch(path, {cache: 'no-store'});
        if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
        return await response.json();
    }""", path)


def _ledger_snapshot(page):
    dashboard = _read_json(page, "/api/dashboard")
    return {key: dashboard[key] for key in ("summary", "positions", "trades")}


def test_stock_sources_demo_csv_and_warning_lifecycle(ui):
    from playwright.sync_api import expect

    page, server = ui.page, ui.server
    with page.expect_response(_response_for("/api/market-data/finnhub")) as finn_request:
        page.goto(server.origin + "/", wait_until="domcontentloaded")
    finn_response = finn_request.value
    assert finn_response.status == 200
    # This is the actual backend response; do not add an Alpha-only using_demo.
    assert finn_response.json() == {"has_api_key": False}
    expect(page.locator("#connection-text")).to_have_text("Connected")

    old_warning = "OLD SESSION provider quota notice must not reappear"
    page.evaluate("""text => sessionStorage.setItem(
        'ledger-six-scan-warnings-v1', JSON.stringify([text]))""", old_warning)
    assert page.evaluate("sessionStorage.getItem('ledger-six-scan-warnings-v1')") is not None
    with page.expect_response(_response_for("/api/market-data/finnhub")) as after_reload:
        page.reload(wait_until="domcontentloaded")
    assert after_reload.value.status == 200
    assert after_reload.value.json() == {"has_api_key": False}
    expect(page.locator("#connection-text")).to_have_text("Connected")
    expect(page.locator("#scan-warnings")).to_be_hidden()
    assert old_warning not in page.locator("#scan-warnings").text_content()
    assert page.evaluate("sessionStorage.getItem('ledger-six-scan-warnings-v1')") is None

    page.locator('[data-page="settings"]').click()
    expect(page.locator("#finnhub-data-status")).to_contain_text("No API key saved")
    expect(page.locator('#finnhub-data-form [name="api_key"]')).to_be_enabled()
    expect(page.locator('#finnhub-data-form button[type="submit"]')).to_be_enabled()
    expect(page.locator('#stock-provider-select option[value="finnhub"]')).to_be_enabled()
    assert page.locator("#stock-source-mode option").evaluate_all(
        "options => options.map(option => option.value)"
    ) == ["api", "csv", "both", "simulation"]

    # Select Finnhub without saving a key: simulation and CSV need no key.
    page.locator("#stock-source-mode").select_option("simulation")
    page.locator("#stock-provider-select").select_option("finnhub")
    with page.expect_response(_response_for("/api/settings", "PUT")) as source_request:
        page.locator('#stock-provider-form button[type="submit"]').click()
    assert source_request.value.status == 200
    assert source_request.value.json()["stock_source_mode"] == "simulation"
    expect(page.locator("#notice")).to_contain_text("Stock source and API provider saved")

    # Exercise the full supported watchlist size in the real UI and API.
    symbols = [chr(ord("A") + index) for index in range(25)]
    page.locator('#stock-symbols-form [name="stock_symbols"]').fill(", ".join(symbols))
    with page.expect_response(_response_for("/api/settings", "PUT")) as watchlist_request:
        page.locator('#stock-symbols-form button[type="submit"]').click()
    assert watchlist_request.value.status == 200
    assert watchlist_request.value.json()["stock_symbols"] == symbols
    expect(page.locator("#notice")).to_have_text("Stock watchlist saved.")

    page.locator('[data-page="dashboard"]').click()
    page.locator('#market-form [name="market_mode"][value="stocks"]').check()
    with page.expect_response(_response_for("/api/settings", "PUT")) as market_request:
        page.locator('#market-form button[type="submit"]').click()
    assert market_request.value.status == 200
    assert market_request.value.json()["market_mode"] == "stocks"
    expect(page.locator("#notice")).to_have_text("Markets set to Stocks.")
    expect(page.locator("#stock-scan-context")).to_contain_text("not market data")

    # Put a real, marked paper holding into the shared ledger so isolation is
    # checked against a meaningful nonempty baseline, not just empty lists.
    page.locator('[data-page="ledger"]').click()
    form = page.locator("#trade-form")
    form.locator('[name="symbol"]').fill("SEED")
    form.locator('[name="asset_type"]').select_option("stock")
    form.locator('[name="quantity"]').fill("2")
    form.locator('[name="price"]').fill("50")
    form.locator('[name="reason"]').fill("Existing shared paper holding")
    with page.expect_response(_response_for("/api/trades", "POST")) as trade_request:
        form.locator('button[type="submit"]').click()
    assert trade_request.value.status == 200
    expect(page.locator("#notice")).to_have_text("Paper trade recorded.")
    page.locator('.mark-form input[name="price"]').fill("55")
    with page.expect_response(lambda r: r.request.method == "PATCH" and "/api/positions/" in r.url) as mark_request:
        page.locator('.mark-form button[type="submit"]').click()
    assert mark_request.value.status == 200
    expect(page.locator("#notice")).to_have_text("Mark price updated.")
    before = _ledger_snapshot(page)
    assert before["summary"]["open_positions"] == 1
    assert before["positions"][0]["symbol"] == "SEED"
    assert before["positions"][0]["mark_price"] == 55
    assert len(before["trades"]) == 1
    positions_text = page.locator("#positions-body").inner_text()
    trades_text = page.locator("#trades-body").inner_text()

    page.locator('[data-page="dashboard"]').click()
    cash_text = page.locator("#metric-cash").inner_text()
    server.inject_quota_warning.set()
    with page.expect_response(_response_for("/api/scan", "POST")) as scan_request:
        page.locator('#page-dashboard [data-action="scan"]').first.click()
    assert scan_request.value.status == 200
    scan = scan_request.value.json()
    assert len(scan["candidates"]) == 25
    assert {candidate["symbol"] for candidate in scan["candidates"]} == set(symbols)
    assert all(candidate["source"] == "Simulation" and candidate["cache_status"] == "simulated"
               for candidate in scan["candidates"])
    assert server.quota_warning in scan["warnings"]
    expect(page.locator("#candidates-body tr")).to_have_count(25)
    expect(page.locator("#candidates-body")).to_contain_text("not market data")
    expect(page.locator("#candidates-body [data-view-demo]")).to_have_count(25)
    expect(page.locator("#candidates-body [data-use-quote]")).to_have_count(0)
    expect(page.locator("#scan-warnings")).to_contain_text(server.quota_warning)
    assert _ledger_snapshot(page) == before

    # Run two explicit demo cycles, inspect their real summary, and verify the
    # demo tables and cash are separate from the shared dashboard ledger.
    page.locator('[data-page="ledger"]').click()
    expect(page.locator("#simulation-cycle")).to_be_enabled()
    expect(page.locator("#simulation-step")).to_have_text("0")
    cycle_results = []
    for step in (1, 2):
        with page.expect_response(_response_for("/api/simulation/stocks/cycle", "POST")) as cycle_request:
            page.locator("#simulation-cycle").click()
        assert cycle_request.value.status == 200
        result = cycle_request.value.json()
        cycle_results.append(result)
        assert result["enabled"] is True and result["step"] == step
        assert result["summary"]["step"] == step and result["summary"]["scanned"] == 25
        expect(page.locator("#simulation-step")).to_have_text(str(step))
        expect(page.locator("#simulation-cycle")).to_be_enabled()
    demo = cycle_results[-1]
    assert demo["trades"] and demo["positions"]
    assert demo["initial_cash"] == 100000 and demo["cash"] < demo["initial_cash"]
    assert all("synthetic" in trade["reason"].lower() for trade in demo["trades"])
    assert len(demo["equity_curve"]) == 3  # initial state and two cycles
    chart = page.locator("#simulation-chart")
    expect(chart).to_be_visible()
    expect(chart.locator("path")).to_have_count(1)
    assert "L" in chart.locator("path").get_attribute("d")
    assert "Synthetic demo equity curve" in chart.get_attribute("aria-label")
    expect(page.locator("#simulation-chart-empty")).to_be_hidden()
    expect(page.locator("#simulation-trades tr")).to_have_count(len(demo["trades"]))
    expect(page.locator("#simulation-positions tr")).to_have_count(len(demo["positions"]))
    expect(page.locator("#simulation-trades")).to_contain_text("Synthetic")
    assert page.locator("#positions-body").inner_text() == positions_text
    assert page.locator("#trades-body").inner_text() == trades_text
    assert _ledger_snapshot(page) == before

    page.locator('[data-page="settings"]').click()
    page.locator('#stock-import-form [name="csv"]').fill(
        "symbol,price,date,volume\nA,912.75,2020-02-03,123\n"
    )
    with page.expect_response(_response_for("/api/stock-import", "POST")) as import_request:
        page.locator('#stock-import-form button[type="submit"]').click()
    assert import_request.value.status == 200
    assert import_request.value.json()["imported"] == 1
    expect(page.locator("#stock-import-status")).to_contain_text("Imported 1 quote")
    # Importing while still in simulation must not itself clear the notice;
    # switching the saved source below must clear it.
    page.locator('[data-page="dashboard"]').click()
    expect(page.locator("#scan-warnings")).to_contain_text(server.quota_warning)
    page.locator('[data-page="settings"]').click()
    page.locator("#stock-source-mode").select_option("csv")
    with page.expect_response(_response_for("/api/settings", "PUT")) as csv_source:
        page.locator('#stock-provider-form button[type="submit"]').click()
    assert csv_source.value.status == 200
    assert csv_source.value.json()["stock_source_mode"] == "csv"
    assert csv_source.value.json()["stock_provider"] == "finnhub"
    expect(page.locator("#notice")).to_contain_text("Stock source and API provider saved")

    page.locator('[data-page="dashboard"]').click()
    expect(page.locator("#scan-warnings")).to_be_hidden()
    assert server.quota_warning not in page.locator("#scan-warnings").text_content()
    expect(page.locator("#stock-scan-context")).to_contain_text("Saved manual imports only")
    with page.expect_response(_response_for("/api/scan", "POST")) as csv_scan:
        page.locator('#page-dashboard [data-action="scan"]').first.click()
    assert csv_scan.value.status == 200
    imported = csv_scan.value.json()["candidates"]
    assert len(imported) == 1
    assert (imported[0]["symbol"], imported[0]["source"], imported[0]["price_usd"],
            imported[0]["cache_status"]) == ("A", "User CSV import", 912.75, "manual")
    expect(page.locator("#candidates-body tr")).to_have_count(1)
    expect(page.locator("#candidates-body")).to_contain_text("User CSV import")
    expect(page.locator("#candidates-body")).to_contain_text("not independently verified")
    expect(page.locator("#candidates-body [data-use-quote]")).to_have_count(1)
    expect(page.locator("#scan-warnings")).to_be_hidden()
    assert page.locator("#metric-cash").inner_text() == cash_text
    assert _ledger_snapshot(page) == before

    # Using the manual CSV quote only prepares a form; no shared trade is made.
    page.locator("#candidates-body [data-use-quote]").click()
    expect(page.locator("#page-ledger")).to_be_visible()
    expect(form.locator('[name="symbol"]')).to_have_value("A")
    expect(form.locator('[name="price"]')).to_have_value("912.75")
    expect(page.locator("#quote-prefill")).to_contain_text("not independently verified")
    expect(page.locator("#simulation-cycle")).to_be_disabled()
    expect(page.locator("#simulation-status")).to_contain_text("Demo is viewable")
    expect(page.locator("#simulation-step")).to_have_text("2")
    expect(page.locator("#simulation-trades tr")).to_have_count(len(demo["trades"]))
    disabled_demo = _read_json(page, "/api/simulation/stocks")
    assert disabled_demo["enabled"] is False
    for key in ("step", "initial_cash", "cash", "equity", "positions", "trades", "equity_curve"):
        assert disabled_demo[key] == demo[key]
    expect(chart).to_be_visible()
    expect(chart.locator("path")).to_have_count(1)
    assert page.locator("#positions-body").inner_text() == positions_text
    assert page.locator("#trades-body").inner_text() == trades_text
    assert _ledger_snapshot(page) == before


def test_both_mode_finnhub_without_key_keeps_csv_available(ui, monkeypatch):
    """The real no-key Finnhub branch returns warnings without making HTTP calls.

    Only this extra fallback test restores the Finnhub adapter; the main
    simulation/CSV test above keeps all three provider scanners forbidden.
    Non-loopback sockets and DNS remain blocked for both tests.
    """
    from playwright.sync_api import expect
    from app import trading

    page, server = ui.page, ui.server
    finnhub_calls = []

    async def tracked_finnhub(symbols):
        finnhub_calls.append(list(symbols))
        return await server.real_finnhub_scan(symbols)

    monkeypatch.setattr(trading, "_scan_finnhub", tracked_finnhub)
    with page.expect_response(_response_for("/api/market-data/finnhub")) as status:
        page.goto(server.origin + "/", wait_until="domcontentloaded")
    assert status.value.status == 200
    assert status.value.json() == {"has_api_key": False}
    expect(page.locator("#connection-text")).to_have_text("Connected")
    page.locator('[data-page="settings"]').click()
    expect(page.locator('#finnhub-data-form [name="api_key"]')).to_be_enabled()
    page.locator('#stock-import-form [name="csv"]').fill("symbol,price,date\nIBM,123.45,2020-02-03\n")
    with page.expect_response(_response_for("/api/stock-import", "POST")) as imported:
        page.locator('#stock-import-form button[type="submit"]').click()
    assert imported.value.status == 200 and imported.value.json()["imported"] == 1
    expect(page.locator("#stock-import-status")).to_contain_text("Imported 1 quote")
    page.locator("#stock-source-mode").select_option("both")
    page.locator("#stock-provider-select").select_option("finnhub")
    with page.expect_response(_response_for("/api/settings", "PUT")) as source:
        page.locator('#stock-provider-form button[type="submit"]').click()
    assert source.value.status == 200
    assert source.value.json()["stock_source_mode"] == "both"
    assert source.value.json()["stock_provider"] == "finnhub"
    expect(page.locator("#notice")).to_contain_text("Stock source and API provider saved")
    page.locator('[data-page="dashboard"]').click()
    page.locator('#market-form [name="market_mode"][value="stocks"]').check()
    with page.expect_response(_response_for("/api/settings", "PUT")) as market:
        page.locator('#market-form button[type="submit"]').click()
    assert market.value.status == 200 and market.value.json()["market_mode"] == "stocks"
    expect(page.locator("#notice")).to_have_text("Markets set to Stocks.")
    before = _ledger_snapshot(page)

    # In Both mode a missing Finnhub key must not block the saved CSV results.
    with page.expect_response(_response_for("/api/scan", "POST")) as scan:
        page.locator('#page-dashboard [data-action="scan"]').first.click()
    assert scan.value.status == 200
    result = scan.value.json()
    assert finnhub_calls == [["IBM"]]
    assert any("requires its own saved API key" in warning for warning in result["warnings"])
    assert any("no Finnhub requests were made" in warning for warning in result["warnings"])
    assert [(item["symbol"], item["source"], item["price_usd"]) for item in result["candidates"]] == [
        ("IBM", "User CSV import", 123.45)
    ]
    expect(page.locator("#scan-warnings")).to_contain_text("Finnhub requires its own saved API key")
    expect(page.locator("#scan-warnings")).to_contain_text("no Finnhub requests were made")
    expect(page.locator("#candidates-body tr")).to_have_count(1)
    expect(page.locator("#candidates-body")).to_contain_text("User CSV import")
    expect(page.locator("#candidates-body [data-use-quote]")).to_have_count(1)
    assert _ledger_snapshot(page) == before
