"""Prometheus metrics: off by default, and labelled by route template when on (Kubernetes)."""

from app import prom


def _count(method, route, status):
    return prom.REQUESTS.labels(method, route, status)._value.get()


def test_metrics_are_off_without_the_port(client, monkeypatch):
    monkeypatch.delenv("PROMETHEUS_PORT", raising=False)
    assert prom.port() is None
    before = _count("GET", "/health", "200")
    client.get("/health")
    assert _count("GET", "/health", "200") == before


def test_requests_are_counted_by_route_template(make_app, monkeypatch):
    monkeypatch.setattr(prom, "start", lambda: None)          # no real listener in tests
    client = make_app(PROMETHEUS_PORT="9100")
    before = _count("GET", "/health", "200")
    missing = _count("GET", "other", "404")
    client.get("/health")
    client.get("/no-such-page-12345")
    assert _count("GET", "/health", "200") == before + 1
    assert _count("GET", "other", "404") == missing + 1        # unknown paths never become labels
