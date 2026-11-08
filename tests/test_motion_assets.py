"""Basic offline checks for the decorative dashboard animation assets."""
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def test_animation_assets_are_served_and_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.delenv('APP_ENCRYPTION_KEY', raising=False)
    with TestClient(app) as client:
        html = client.get('/')
        css = client.get('/static/motion.css')
        script = client.get('/static/motion.js')
        assert html.status_code == css.status_code == script.status_code == 200
        assert 'Illustrative animation · not live execution' in html.text
        assert 'id="flow-motion-toggle"' in html.text
        assert 'aria-pressed="true"' in html.text
        assert html.text.count('class="flow-step"') == 6
        assert 'prefers-reduced-motion:reduce' in css.text
        assert 'flow-halo' in css.text and 'flow-travel' in css.text
        assert 'visibilitychange' in script.text and 'prefers-reduced-motion: reduce' in script.text
        assert 'fetch(' not in script.text  # Purely decorative: no API actions.


def test_animation_preview_is_packaged():
    root = Path(__file__).resolve().parent.parent
    assert (root / 'docs/screenshots/agent-flow.gif').stat().st_size > 0
