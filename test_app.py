
import os
import pytest

# Force the in-memory database before app is imported so it doesn't touch data/app.db
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import app, db

@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with app.test_client() as client:
        with app.app_context():
            db.drop_all()
            db.create_all()
            yield client
            db.session.remove()
            db.drop_all()


@pytest.fixture
def auth_headers(client):
    response = client.post("/auth/login", json={
        "username": "admin",
        "password": "admin123"
    })
    token = response.get_json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --- Authentication Tests ---

def test_login_success(client):
    response = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    assert response.status_code == 200
    assert "access_token" in response.get_json()


def test_login_failure(client):
    response = client.post("/auth/login", json={"username": "wrong", "password": "123"})
    assert response.status_code == 401


# --- Student CRUD Tests ---

def test_create_student(client):
    response = client.post("/students", json={
        "FirstName": "John", "LastName": "Doe", "DoubleMajor": False, "Level": 2
    })
    assert response.status_code == 201
    data = response.get_json()
    assert data["ID_S"] == 1
    assert data["FirstName"] == "John"


def test_get_students_requires_token(client):
    response = client.get("/students")
    assert response.status_code == 401


def test_get_students_with_token(client, auth_headers):
    client.post("/students", json={
        "FirstName": "John", "LastName": "Doe", "DoubleMajor": False, "Level": 2
    })
    response = client.get("/students", headers=auth_headers)
    assert response.status_code == 200
    assert len(response.get_json()) == 1


def test_update_student(client, auth_headers):
    client.post("/students", json={
        "FirstName": "John", "LastName": "Doe", "DoubleMajor": False, "Level": 2
    })
    # PUT replaces all fields
    response = client.put("/students/1", json={
        "FirstName": "Johnny", "LastName": "Doe", "DoubleMajor": True, "Level": 3
    })
    assert response.status_code == 200
    data = client.get("/students/1", headers=auth_headers).get_json()
    assert data["FirstName"] == "Johnny"
    assert data["Level"] == 3


def test_patch_student(client, auth_headers):
    client.post("/students", json={
        "FirstName": "John", "LastName": "Doe", "DoubleMajor": False, "Level": 2
    })
    # PATCH updates only provided fields
    response = client.patch("/students/1", json={"Level": 4})
    assert response.status_code == 200
    data = client.get("/students/1", headers=auth_headers).get_json()
    assert data["Level"] == 4
    assert data["FirstName"] == "John"  # Remains unchanged


# --- Report CRUD & Limit Tests ---

def test_create_report(client):
    client.post("/students", json={
        "FirstName": "John", "LastName": "Doe", "DoubleMajor": False, "Level": 2
    })
    response = client.post("/reports", json={
        "ID_S": 1, "Title": "My Report", "Type": "Mini senior project",
        "K1": "A", "K2": "B", "K3": "C", "ReleaseDate": "2027-01-01"
    })
    assert response.status_code == 201


def test_single_major_limit(client):
    client.post("/students", json={
        "FirstName": "John", "LastName": "Doe", "DoubleMajor": False, "Level": 2
    })
    report_data = {
        "ID_S": 1, "Title": "Report 1", "Type": "Mini senior project",
        "K1": "A", "K2": "B", "K3": "C", "ReleaseDate": "2027-01-01"
    }
    client.post("/reports", json=report_data)
    response = client.post("/reports", json=report_data)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "report_limit_reached"


def test_double_major_limit(client):
    client.post("/students", json={
        "FirstName": "Jane", "LastName": "Doe", "DoubleMajor": True, "Level": 4
    })
    report_data = {
        "ID_S": 1, "Title": "Report", "Type": "End-of-studies project",
        "K1": "A", "K2": "B", "K3": "C", "ReleaseDate": "2027-01-01"
    }
    assert client.post("/reports", json=report_data).status_code == 201
    assert client.post("/reports", json=report_data).status_code == 201
    assert client.post("/reports", json=report_data).status_code == 409


def test_cannot_remove_double_major_if_reports_exceed_limit(client, auth_headers):
    client.post("/students", json={
        "FirstName": "Jane", "LastName": "Doe", "DoubleMajor": True, "Level": 4
    })
    report_data = {
        "ID_S": 1, "Title": "Report", "Type": "End-of-studies project",
        "K1": "A", "K2": "B", "K3": "C", "ReleaseDate": "2027-01-01"
    }
    client.post("/reports", json=report_data)
    client.post("/reports", json=report_data)
    
    response = client.patch("/students/1", json={"DoubleMajor": False})
    assert response.status_code == 409
    assert "Delete one report first" in response.get_json()["error"]["message"]


# --- Relationships & Cascade Tests ---

def test_student_includes_reports(client, auth_headers):
    client.post("/students", json={
        "FirstName": "John", "LastName": "Doe", "DoubleMajor": False, "Level": 2
    })
    client.post("/reports", json={
        "ID_S": 1, "Title": "My Report", "Type": "Mini senior project",
        "K1": "A", "K2": "B", "K3": "C", "ReleaseDate": "2027-01-01"
    })
    response = client.get("/students/1", headers=auth_headers)
    data = response.get_json()
    assert "reports" in data
    assert len(data["reports"]) == 1
    assert data["reports"][0]["Title"] == "My Report"


def test_report_includes_student(client):
    client.post("/students", json={
        "FirstName": "John", "LastName": "Doe", "DoubleMajor": False, "Level": 2
    })
    client.post("/reports", json={
        "ID_S": 1, "Title": "My Report", "Type": "Mini senior project",
        "K1": "A", "K2": "B", "K3": "C", "ReleaseDate": "2027-01-01"
    })
    response = client.get("/reports/1")
    data = response.get_json()
    assert "student" in data
    assert data["student"]["FirstName"] == "John"


def test_cascade_delete_student_deletes_reports(client, auth_headers):
    client.post("/students", json={
        "FirstName": "John", "LastName": "Doe", "DoubleMajor": False, "Level": 2
    })
    client.post("/reports", json={
        "ID_S": 1, "Title": "My Report", "Type": "Mini senior project",
        "K1": "A", "K2": "B", "K3": "C", "ReleaseDate": "2027-01-01"
    })
    
    # Verify report exists
    assert client.get("/reports/1").status_code == 200
    
    # Delete student
    response = client.delete("/students/1")
    assert response.status_code == 200
    
    # Verify cascade delete removed the report from the DB
    assert client.get("/reports/1").status_code == 404
