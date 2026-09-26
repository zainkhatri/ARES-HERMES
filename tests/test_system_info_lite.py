import importlib


def _client(monkeypatch):
    monkeypatch.setenv("ARES_PASSWORD", "test-password-not-default")
    monkeypatch.setenv("ARES_API_TOKEN", "test-token")
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    monkeypatch.setattr(app_mod, "get_system_info",
                        lambda: {"hostname": "ARES", "cpu_percent": 1.0, "autofix_incidents": [{"x": 1}]})
    return app_mod.app.test_client()


def test_lite_drops_autofix(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/system-info?lite=1", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    body = r.get_json()
    assert "autofix_incidents" not in body and body["hostname"] == "ARES"


def test_default_keeps_autofix(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/system-info", headers={"Authorization": "Bearer test-token"})
    assert r.get_json()["autofix_incidents"] == [{"x": 1}]
