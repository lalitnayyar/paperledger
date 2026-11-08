from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=1)
    errors=[]
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto('http://127.0.0.1:8765/', wait_until='networkidle')
    page.wait_for_function("document.querySelector('#connection-text').textContent === 'Connected'")
    assert page.locator('#metric-equity').inner_text() == '$100,000.00'
    assert page.locator('.agent-row').count()==6
    page.screenshot(path='/home/ubuntu/ledger-six-dashboard.png',full_page=True)
    page.locator('#theme-toggle').click()
    assert page.locator('html').get_attribute('data-theme') == 'light'
    page.screenshot(path='/home/ubuntu/ledger-six-light.png',full_page=True)
    page.locator('[data-page="ledger"]').click()
    page.locator('#trade-form [name=symbol]').fill('AAPL')
    page.locator('#trade-form [name=quantity]').fill('2')
    page.locator('#trade-form [name=price]').fill('100')
    page.locator('#trade-form [name=reason]').fill('Paper test entry')
    page.locator('#trade-form button[type=submit]').click()
    page.wait_for_function("document.querySelector('#notice').textContent.includes('Paper trade recorded')")
    assert 'AAPL' in page.locator('#positions-body').inner_text()
    assert 'Unmarked' in page.locator('#positions-body').inner_text()
    page.locator('.mark-form input').fill('110')
    page.locator('.mark-form button').click()
    page.wait_for_function("document.querySelector('#notice').textContent.includes('Mark price updated')")
    assert '$110.00' in page.locator('#positions-body').inner_text()
    page.locator('[data-page="settings"]').click()
    assert page.locator('#provider-select option').count()>=12
    page.locator('#add-provider-form [name=provider_id]').fill('test-local-model')
    page.locator('#add-provider-form button').click()
    page.wait_for_function("document.querySelector('#notice').textContent.includes('Provider added')")
    assert page.locator('#provider-select').input_value() == 'test-local-model'
    page.locator('#provider-form [name=model]').fill('test-model')
    page.locator('#provider-form [name=base_url]').fill('http://host.docker.internal:11434/v1')
    page.locator('#provider-form [name=api_key]').fill('private-test-key')
    page.locator('#provider-form button[type=submit]').click()
    page.wait_for_function("document.querySelector('#notice').textContent.includes('Provider settings saved')")
    assert page.locator('#provider-form [name=api_key]').input_value()==''
    assert 'A key is saved' in page.locator('#key-status').inner_text()
    page.locator('#activate-provider').click()
    page.wait_for_function("document.querySelector('#notice').textContent.includes('Active provider updated')")
    assert 'ACTIVE PROVIDER' in page.locator('#provider-status').inner_text()
    page.locator('[data-page="dashboard"]').click()
    assert page.locator('#metric-equity').inner_text() != '—'
    mobile=browser.new_page(viewport={'width':390,'height':844},device_scale_factor=1)
    mobile.goto('http://127.0.0.1:8765/',wait_until='networkidle')
    assert mobile.locator('body').evaluate('(e) => e.scrollWidth') <= 390
    mobile.screenshot(path='/home/ubuntu/ledger-six-mobile.png',full_page=True)
    print('Browser flows passed: dashboard, themes, six stages, buy, mark, providers, activation, mobile.')
    if errors: raise AssertionError(f'Browser errors: {errors}')
    browser.close()
