"""Optional real-browser coverage for the holdings UI, using only mocked APIs.

To run: install pytest and playwright in a test environment, then either install
Chromium with ``python -m playwright install chromium`` (and its OS libraries),
install system ``chromium``/``chromium-browser``, or point
``PLAYWRIGHT_CHROMIUM_EXECUTABLE`` at an executable. With Playwright or a
browser absent these tests skip; neither is a runtime dependency of the app.
The HTTP server binds an ephemeral loopback port, and browser requests are
intercepted so external destinations and real API calls are never contacted.
"""

import os
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest


REPO = Path(__file__).resolve().parent.parent


class _StaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def browser():
    # Import inside a fixture rather than at module collection time, so a
    # missing optional package reports ordinary skipped tests even in isolation.
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        configured = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "")
        candidates = [
            configured,
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
        # Do not turn arbitrary launch errors into skips: a broken installed
        # browser or missing OS library should remain visible to the developer.
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


@pytest.fixture(scope="module")
def static_server(browser):
    handler = partial(_StaticHandler, directory=str(REPO))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def ui(browser, static_server):
    stock = {
        "id": 1, "symbol": "AAPL", "asset_type": "stock", "quantity": 2,
        "entry_price": 100, "mark_price": None, "unrealized_pnl": None,
        "mark_source": None, "mark_timestamp": None,
    }
    crypto = {
        "id": 2, "symbol": "BTC", "asset_type": "crypto", "quantity": 0.5,
        "entry_price": 100, "mark_price": 120, "unrealized_pnl": 10,
        "mark_source": "dexscreener", "mark_timestamp": "2026-11-10T00:00:00Z",
    }
    data = {
        "summary": {"cash": 10000, "equity": None, "realized_pnl": 0,
                    "unrealized_pnl": None, "open_positions": 2},
        "positions": [stock, crypto],
        "trades": [
            {"timestamp": "2026-11-10T00:00:00Z", "symbol": "AAPL", "side": "BUY",
             "asset_type": "stock", "quantity": 2, "reference_price": 100,
             "fill_price": 100, "fee": 0, "reason": "stock history"},
            {"timestamp": "2026-11-10T01:00:00Z", "symbol": "BTC", "side": "BUY",
             "asset_type": "crypto", "quantity": 0.5, "reference_price": 100,
             "fill_price": 100, "fee": 0, "reason": "crypto history"},
        ],
        "settings": {"market_mode": "crypto", "stock_symbols": ["AAPL", "MSFT"],
                     "auto_paper": False},
        "candidates": [
            {"symbol": "TSLA", "asset_type": "stock", "price_usd": 200,
             "cache_status": "manual", "source": "user", "quote_date": "2026-11-09"},
            {"symbol": "ZERO", "asset_type": "stock", "price_usd": 0},
            {"symbol": "MISSING", "asset_type": "stock", "price_usd": None},
            {"symbol": "SOL", "asset_type": "crypto", "price_usd": 55},
        ],
        "agents": [], "activity": [], "equity_curve": [],
    }
    writes, blocked, page_errors = [], [], []
    origin = urlsplit(static_server)
    context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                  service_workers="block")
    page = context.new_page()
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.add_init_script("window.__holdingsXss = 0")

    def route_request(route):
        request = route.request
        url = urlsplit(request.url)
        # Only this exact loopback origin may load static resources. API routes
        # are fulfilled here, never passed to the server or the outside world.
        if (url.scheme, url.hostname, url.port) != (origin.scheme, origin.hostname, origin.port):
            blocked.append(request.url)
            route.abort()
            return
        if not url.path.startswith("/api/"):
            if request.method not in ("GET", "HEAD"):
                writes.append((request.method, url.path, request.post_data, None))
                route.abort()
            else:
                route.continue_()
            return
        path = url.path.removeprefix("/api/")
        if request.method not in ("GET", "HEAD"):
            try:
                payload = request.post_data_json
            except Exception:
                payload = request.post_data
            writes.append((request.method, path, payload, request.headers.get("x-csrf-token")))
            if request.method == "PATCH" and path == "positions/1/mark":
                stock.update(mark_price=payload["price"], mark_source="manual",
                             mark_timestamp="2026-11-10T12:00:00Z")
                route.fulfill(status=200, json={"id": 1})
            else:
                route.fulfill(status=409, json={"detail": "Unexpected mutation in browser test"})
            return
        responses = {
            "auth/session": {"username": "viewer", "role": "user", "csrf": "test-token"},
            "dashboard": data,
            "health": {"status": "ok"},
        }
        route.fulfill(status=200 if path in responses else 404, json=responses.get(path, {}))

    context.route("**/*", route_request)
    try:
        page.goto(static_server + "/static/index.html", wait_until="domcontentloaded")
        page.wait_for_function("document.querySelector('#connection-text').textContent === 'Connected'")
        yield SimpleNamespace(page=page, data=data, stock=stock, crypto=crypto, writes=writes)
    finally:
        context.close()
        assert not blocked, f"Blocked non-loopback requests: {blocked}"
        assert not page_errors, f"Browser JavaScript errors: {page_errors}"
        assert not [write for write in writes if write[0] == "POST"], writes


def _ledger(page):
    page.locator('[data-page="ledger"]').click()


def _counts(page):
    return [page.locator(f"#holdings-count-{kind}").inner_text() for kind in ("all", "stock", "crypto")]


def _order(page):
    return {name: page.locator(f'#trade-form [name="{name}"]').input_value()
            for name in ("symbol", "asset_type", "side", "quantity", "price", "reason")}


def _draft(page):
    page.locator('#trade-form [name="symbol"]').fill("DRAFT")
    page.locator('#trade-form [name="quantity"]').fill("7")
    page.locator('#trade-form [name="price"]').fill("42")
    page.locator('#trade-form [name="reason"]').fill("Unfinished")
    return _order(page)


def _confirm_click(page, selector, accept):
    dialogs = []

    def answer(dialog):
        dialogs.append((dialog.type, dialog.message))
        dialog.accept() if accept else dialog.dismiss()

    page.once("dialog", answer)
    try:
        page.locator(selector).click()
    finally:
        page.remove_listener("dialog", answer)
    assert len(dialogs) == 1 and dialogs[0][0] == "confirm", dialogs
    assert "unsaved" in dialogs[0][1].lower()


def test_all_holdings_filters_counts_and_history(ui):
    page = ui.page
    assert page.locator('#market-form [name="market_mode"]:checked').input_value() == "crypto"
    _ledger(page)
    assert page.locator('[data-holdings-filter="all"]').get_attribute("aria-pressed") == "true"
    assert _counts(page) == ["2", "1", "1"]
    assert page.locator("#positions-body tr").count() == 2
    positions = page.locator("#positions-body").inner_text()
    assert "AAPL" in positions and "BTC" in positions
    assert "TSLA" not in positions and "MSFT" not in positions
    assert "dexscreener" in positions and "Observed:" in positions
    history = page.locator("#trades-body").inner_text()
    assert page.locator("#trades-body tr").count() == 2
    assert "stock history" in history and "crypto history" in history
    for kind, included, excluded in (("crypto", "BTC", "AAPL"), ("stock", "AAPL", "BTC")):
        page.locator(f'[data-holdings-filter="{kind}"]').click()
        assert page.locator(f'[data-holdings-filter="{kind}"]').get_attribute("aria-pressed") == "true"
        assert _counts(page) == ["2", "1", "1"]
        assert page.locator("#positions-body tr").count() == 1
        assert included in page.locator("#positions-body").inner_text()
        assert excluded not in page.locator("#positions-body").inner_text()
        assert page.locator("#trades-body").inner_text() == history
    page.locator('[data-holdings-filter="all"]').click()
    assert page.locator("#positions-body tr").count() == 2
    assert page.locator("#trades-body").inner_text() == history
    assert not ui.writes


def test_mark_draft_survives_filters_and_focused_refresh_then_patches(ui):
    page = ui.page
    _ledger(page)
    mark = page.locator('.mark-form[data-id="1"] input')
    mark.fill("123.45")
    page.locator('[data-holdings-filter="crypto"]').click()
    page.locator('[data-holdings-filter="stock"]').click()
    assert mark.input_value() == "123.45"
    mark.focus()
    ui.stock.update(mark_price=111, mark_source="manual", mark_timestamp="2026-11-10T12:00:00Z")
    ui.stock["quantity"] = 3
    # A physical click would itself blur the input; dispatch the same refresh
    # button's click while focus remains in the mark field.
    page.evaluate("document.querySelector('#refresh-button').click()")
    page.wait_for_function("document.querySelector('#positions-body').innerText.includes('$111.00')")
    row = page.locator('#positions-body tr').first
    assert row.locator("td").nth(1).inner_text() == "3"
    assert "Recorded:" in row.inner_text()
    assert mark.input_value() == "123.45"
    assert mark.evaluate("el => document.activeElement === el")
    page.locator('.mark-form[data-id="1"] button').click()
    page.wait_for_function("document.querySelector('#notice').textContent.includes('Mark price updated')")
    assert mark.input_value() == ""
    assert "$123.45" in page.locator("#positions-body").inner_text()
    assert ui.writes == [("PATCH", "positions/1/mark", {"price": 123.45}, "test-token")]


def test_add_sell_and_quote_guard_unsaved_order_without_posting(ui):
    page = ui.page
    _ledger(page)
    page.locator('[data-add-holding="stock"]').click()
    assert _order(page) == {"symbol": "", "asset_type": "stock", "side": "BUY",
                            "quantity": "", "price": "", "reason": ""}
    assert page.evaluate("document.activeElement.name") == "symbol"
    draft = _draft(page)
    _confirm_click(page, '[data-add-holding="crypto"]', accept=False)
    assert _order(page) == draft
    _confirm_click(page, '[data-add-holding="crypto"]', accept=True)
    assert _order(page) == {"symbol": "", "asset_type": "crypto", "side": "BUY",
                            "quantity": "", "price": "", "reason": ""}
    assert page.evaluate("document.activeElement.name") == "symbol"
    draft = _draft(page)
    _confirm_click(page, '[data-add-holding="stock"]', accept=False)
    assert _order(page) == draft
    _confirm_click(page, '[data-add-holding="stock"]', accept=True)
    assert _order(page) == {"symbol": "", "asset_type": "stock", "side": "BUY",
                            "quantity": "", "price": "", "reason": ""}

    draft = _draft(page)
    _confirm_click(page, '[data-sell-position="1"]', accept=False)
    assert _order(page) == draft
    _confirm_click(page, '[data-sell-position="1"]', accept=True)
    assert _order(page) == {"symbol": "AAPL", "asset_type": "stock", "side": "SELL",
                            "quantity": "", "price": "", "reason": ""}
    assert page.evaluate("document.activeElement.name") == "quantity"
    _confirm_click(page, '[data-sell-position="2"]', accept=False)
    assert _order(page)["symbol"] == "AAPL"
    _confirm_click(page, '[data-sell-position="2"]', accept=True)
    assert _order(page) == {"symbol": "BTC", "asset_type": "crypto", "side": "SELL",
                            "quantity": "", "price": "", "reason": ""}

    draft = _draft(page)
    page.locator('[data-page="dashboard"]').click()
    for symbol in ("ZERO", "MISSING", "SOL"):
        row = page.locator("#candidates-body tr").filter(has_text=symbol)
        assert row.count() == 1 and row.locator("[data-use-quote]").count() == 0
    assert page.locator("#candidates-body [data-use-quote]").count() == 1
    _confirm_click(page, "#candidates-body [data-use-quote]", accept=False)
    assert page.locator("#page-dashboard").is_visible()
    assert _order(page) == draft
    _confirm_click(page, "#candidates-body [data-use-quote]", accept=True)
    assert page.locator("#page-ledger").is_visible()
    assert _order(page) == {"symbol": "TSLA", "asset_type": "stock", "side": "BUY",
                            "quantity": "", "price": "200", "reason": ""}
    assert page.evaluate("document.activeElement.name") == "quantity"
    assert "not independently verified" in page.locator("#quote-prefill").inner_text()
    assert not ui.writes


def test_empty_watchlist_escaping_and_mobile_width(ui):
    page = ui.page
    _ledger(page)
    ui.data["positions"] = []
    page.locator("#refresh-button").click()
    page.wait_for_function("document.querySelector('#holdings-count-all').textContent === '0'")
    assert _counts(page) == ["0", "0", "0"]
    assert page.locator("#positions-body tr").count() == 0
    assert page.locator("#positions-empty").is_visible()
    assert "watchlists do not create holdings" in page.locator("#positions-empty").inner_text()
    assert page.locator("#trades-body tr").count() == 2

    hostile = '<img src=x onerror="window.__holdingsXss=1">'
    ui.data["positions"] = [dict(ui.stock, id=9, symbol=hostile)]
    page.locator("#refresh-button").click()
    page.wait_for_function("document.querySelector('#holdings-count-all').textContent === '1'")
    assert hostile in page.locator("#positions-body").inner_text()
    assert page.locator("#positions-body img").count() == 0
    assert page.evaluate("window.__holdingsXss") == 0
    page.set_viewport_size({"width": 390, "height": 844})
    dimensions = page.evaluate("""() => ({viewport: innerWidth,
        document: document.documentElement.scrollWidth, body: document.body.scrollWidth})""")
    assert dimensions["document"] <= dimensions["viewport"], dimensions
    assert dimensions["body"] <= dimensions["viewport"], dimensions
    assert not ui.writes
