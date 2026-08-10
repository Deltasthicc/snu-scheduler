"""Delivery headers that keep the generated frontend and live health state fresh."""

from fastapi.testclient import TestClient

from app.main import app


def test_health_readiness_is_never_cached():
    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


def test_frontend_entrypoint_is_never_reused_after_a_deploy():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
