"""End-to-end tests against a throwaway SQLite database (never the real annseva.db).

Run with:  pytest -q
"""
import os
import sys
import importlib
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Fresh app + fresh isolated DB for every test."""
    db_file = tmp_path / "test_annseva.db"
    monkeypatch.setenv("ANNSEVA_DB_PATH", str(db_file))
    monkeypatch.setenv("ANNSEVA_ADMIN_PASSWORD", "admin123")
    monkeypatch.setenv("ANNSEVA_FARMER_PASSWORD", "farmer123")

    sys.path.insert(0, str(ROOT))
    if "server" in sys.modules:
        del sys.modules["server"]
    if "recommendation_engine" in sys.modules:
        del sys.modules["recommendation_engine"]
    import server as server_module
    importlib.reload(server_module)

    from fastapi.testclient import TestClient
    with TestClient(server_module.app) as c:
        yield c


def login(client, **payload):
    r = client.post("/api/login", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_unauthenticated_state_is_rejected(client):
    r = client.get("/api/state")
    assert r.status_code == 401


def test_farmer_login_wrong_password_rejected(client):
    r = client.post("/api/login", json={"role": "farmer", "farmer_id": "FMR-1042", "password": "wrong"})
    assert r.status_code == 401


def test_admin_login_unknown_id_rejected(client):
    r = client.post("/api/login", json={"role": "admin", "admin_id": "ADM-NOPE", "password": "admin123"})
    assert r.status_code == 401


def test_farmer_cannot_reach_admin_endpoints(client):
    ftoken = login(client, role="farmer", farmer_id="FMR-1042", password="farmer123")
    r = client.post("/api/recommendations", headers=auth(ftoken), json={"top_k": 5})
    assert r.status_code == 403


def test_farmer_cannot_edit_another_farmers_record(client):
    ftoken = login(client, role="farmer", farmer_id="FMR-1042", password="farmer123")
    r = client.post("/api/applications", headers=auth(ftoken), json={
        "farmer_id": "FMR-1175", "name": "Someone Else", "crop": "Wheat",
        "quantity": 10, "location": "Kurnool",
    })
    assert r.status_code == 403


def test_farmer_only_sees_own_data(client):
    ftoken = login(client, role="farmer", farmer_id="FMR-1042", password="farmer123")
    state = client.get("/api/state", headers=auth(ftoken)).json()
    assert all(a["farmer_id"] == "FMR-1042" for a in state["applications"])
    assert all(f["id"] == "FMR-1042" for f in state["farmers"])


def test_full_workflow_submit_recommend_approve_complete(client):
    ftoken = login(client, role="farmer", farmer_id="FMR-1175", password="farmer123")
    atoken_kurnool = login(client, role="admin", admin_id="ADM-KURNOOL", password="admin123")

    # Farmer already has a seeded Wheat offer in Kurnool; requirement is seeded as Wheat too.
    recs = client.post("/api/recommendations", headers=auth(atoken_kurnool), json={"top_k": 10}).json()
    assert recs["model"] in ("cold_start_baseline", "random_forest")
    rec = next(r for r in recs["recommendations"] if r["farmer_id"] == "FMR-1175")
    app_id = rec["application_id"]
    assigned_admin = rec["assigned_admin_id"]
    # Kurnool is FMR-1175's own jurisdiction, so the non-local rule must route elsewhere.
    assert assigned_admin != "ADM-KURNOOL"

    # The admin who is NOT assigned must be refused.
    r = client.post("/api/approve", headers=auth(atoken_kurnool), json={"application_id": app_id})
    assert r.status_code == 403

    # The assigned admin can approve, which also auto-schedules.
    atoken_assigned = login(client, role="admin", admin_id=assigned_admin, password="admin123")
    r = client.post("/api/approve", headers=auth(atoken_assigned), json={"application_id": app_id})
    assert r.status_code == 200
    approved_app = next(a for a in r.json()["applications"] if a["application_id"] == app_id)
    assert approved_app["status"] == "Scheduled"
    assert approved_app["token"]
    assert approved_app["schedule_date"]

    # Approving twice must fail cleanly instead of double-booking.
    r = client.post("/api/approve", headers=auth(atoken_assigned), json={"application_id": app_id})
    assert r.status_code == 409

    # Completing records the real outcome for ML training.
    r = client.post("/api/complete", headers=auth(atoken_assigned), json={
        "application_id": app_id, "actual_quantity": approved_app["allocation"],
    })
    assert r.status_code == 200
    completed_app = next(a for a in r.json()["applications"] if a["application_id"] == app_id)
    assert completed_app["status"] == "Completed"

    # The farmer can see their own completed application.
    state = client.get("/api/state", headers=auth(ftoken)).json()
    mine = next(a for a in state["applications"] if a["application_id"] == app_id)
    assert mine["status"] == "Completed"


def test_new_requirement_matches_existing_standing_offers(client):
    atoken = login(client, role="admin", admin_id="ADM-ADONI", password="admin123")
    # Seeded data has an FMR-1264 Rice offer with no matching requirement yet.
    r = client.post("/api/requirements", headers=auth(atoken), json={
        "crop": "Rice", "required_quantity": 100, "centre": "Adoni Procurement Centre",
        "centre_capacity": 200, "slot_capacity": 20,
    })
    assert r.status_code == 200
    apps = r.json()["applications"]
    rice_app = next((a for a in apps if a["farmer_id"] == "FMR-1264" and a["crop"] == "Rice"), None)
    assert rice_app is not None
    assert rice_app["status"] == "Submitted"


def test_chat_requires_auth(client):
    r = client.post("/api/chat", json={"message": "hello"})
    assert r.status_code == 401


def test_chat_responds_for_authenticated_farmer(client):
    ftoken = login(client, role="farmer", farmer_id="FMR-1042", password="farmer123")
    r = client.post("/api/chat", headers=auth(ftoken), json={"message": "What is my status?"})
    assert r.status_code == 200
    assert "reply" in r.json()


def test_reset_requires_admin(client):
    ftoken = login(client, role="farmer", farmer_id="FMR-1042", password="farmer123")
    r = client.post("/api/reset", headers=auth(ftoken))
    assert r.status_code == 403
