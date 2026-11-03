import hmac
import logging
import os
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path

import jwt
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.json.sort_keys = False  # keep field order as written below

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-change-me-before-deploying")
ACCESS_TTL = timedelta(minutes=int(os.environ.get("ACCESS_TOKEN_MINUTES", "5")))
REFRESH_TTL = timedelta(hours=int(os.environ.get("REFRESH_TOKEN_HOURS", "24")))

# A single demo account is enough for the assignment (override with env vars).
API_USERNAME = os.environ.get("API_USERNAME", "admin")
API_PASSWORD = os.environ.get("API_PASSWORD", "admin123")

# Server + logging settings (all overridable with environment variables).
HOST = os.environ.get("HOST", "127.0.0.1")        # used by `python app.py` only
PORT = int(os.environ.get("PORT", "5000"))
DEBUG = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.environ.get("LOG_FILE", "logs/app.log")

# Database: a SQLite file in ./data/app.db unless DATABASE_URL says otherwise.
# The default path is built from this file's location, so it is the same wherever
# you run from. Use an ABSOLUTE path in DATABASE_URL (Flask-SQLAlchemy would put a
# relative SQLite path inside a hidden "instance" folder).
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_FILE = BASE_DIR / "data" / "app.db"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DEFAULT_DB_FILE.as_posix()}")

_db_url = make_url(DATABASE_URL)
if _db_url.get_backend_name() == "sqlite" and _db_url.database not in (None, "", ":memory:"):
    Path(_db_url.database).parent.mkdir(parents=True, exist_ok=True)

LEVELS = [1, 2, 3, 4]  # study year: 1 = Freshman ... 4 = Senior
REPORT_TYPES = ["Mini senior project", "End-of-studies project"]
STUDENT_FIELDS = ("FirstName", "LastName", "DoubleMajor", "Level")
REPORT_FIELDS = ("Title", "Type", "K1", "K2", "K3", "ReleaseDate", "ID_S")

# --------------------------------------------------------------------------- #
# Logging: console + rotating file (logs/app.log). Never log passwords/tokens.
# --------------------------------------------------------------------------- #
def configure_logging():
    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
    handlers = [logging.StreamHandler()]  # console: visible with `docker logs`
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        handlers.append(RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError as exc:  # e.g. read-only folder: keep running, console only
        print(f"WARNING: cannot write log file {LOG_FILE!r}: {exc}")
    app.logger.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        app.logger.addHandler(handler)
    app.logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    app.logger.propagate = False  # avoid duplicate lines under gunicorn


configure_logging()


# --------------------------------------------------------------------------- #
# Database: connection + the two tables (ORM models)
# --------------------------------------------------------------------------- #
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
db = SQLAlchemy(app)


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite ignores foreign keys unless asked: switch them on for every connection."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Student(db.Model):
    __tablename__ = "student"
    ID_S = db.Column(db.Integer, primary_key=True)
    FirstName = db.Column(db.String(100), nullable=False)
    LastName = db.Column(db.String(100), nullable=False)
    DoubleMajor = db.Column(db.Boolean, nullable=False)
    Level = db.Column(db.Integer, nullable=False)
    # all reports of this student; deleting the student deletes their reports too
    reports = db.relationship("Report", back_populates="student",
                              cascade="all, delete-orphan")


class Report(db.Model):
    __tablename__ = "report"
    ID_R = db.Column(db.Integer, primary_key=True)
    Title = db.Column(db.String(200), nullable=False)
    Type = db.Column(db.String(50), nullable=False)
    K1 = db.Column(db.String(100), nullable=False)
    K2 = db.Column(db.String(100), nullable=False)
    K3 = db.Column(db.String(100), nullable=False)
    ReleaseDate = db.Column(db.Date, nullable=False)
    # the link to the student, stored (and enforced) in the database itself
    ID_S = db.Column(db.Integer, db.ForeignKey("student.ID_S", ondelete="CASCADE"),
                     nullable=False)
    student = db.relationship("Student", back_populates="reports")


# Create the tables if they do not exist yet (this also creates data/app.db)
with app.app_context():
    db.create_all()

REVOKED_JTIS = set()  # refresh tokens that were already used (rotation)


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def find_student(student_id):
    return db.session.get(Student, student_id)


def find_report(report_id):
    return db.session.get(Report, report_id)


def get_student_or_404(student_id):
    student = find_student(student_id)
    if student is None:
        raise APIError(404, f"Student {student_id} not found.", "not_found")
    return student


def get_report_or_404(report_id):
    report = find_report(report_id)
    if report is None:
        raise APIError(404, f"Report {report_id} not found.", "not_found")
    return report


def count_reports(student_id, ignore_report_id=None):
    query = select(func.count()).select_from(Report).where(Report.ID_S == student_id)
    if ignore_report_id is not None:
        query = query.where(Report.ID_R != ignore_report_id)
    return db.session.scalar(query)


def max_reports(double_major):
    """1 report normally, 2 if the student has a double major."""
    return 2 if double_major else 1


def commit():
    """Save the changes; on a database error undo them and let the error handler log it."""
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        raise


def apply_values(row, values):
    for field, value in values.items():
        setattr(row, field, value)


def student_to_dict(student, include_reports=True):
    data = {"ID_S": student.ID_S, "FirstName": student.FirstName,
            "LastName": student.LastName, "DoubleMajor": student.DoubleMajor,
            "Level": student.Level}
    if include_reports:
        data["reports"] = [report_to_dict(r, include_student=False) for r in student.reports]
    return data


def report_to_dict(report, include_student=True):
    data = {"ID_R": report.ID_R, "Title": report.Title, "Type": report.Type,
            "K1": report.K1, "K2": report.K2, "K3": report.K3,
            "ReleaseDate": report.ReleaseDate.isoformat(), "ID_S": report.ID_S}
    if include_student and report.student:
        data["student"] = student_to_dict(report.student, include_reports=False)
    return data


# --------------------------------------------------------------------------- #
# Errors: every error leaves the API as JSON with the same shape
# --------------------------------------------------------------------------- #
class APIError(Exception):
    def __init__(self, status, message, code, details=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.details = details


def error_response(status, code, message, details=None):
    body = {"error": {"status": status, "code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    response = jsonify(body)
    response.status_code = status
    if status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


def log_failed_request(status, code, message, details=None):
    """One line per failed request: easy to read, easy to paste into an LLM."""
    line = f"{request.method} {request.path} -> {status} {code}: {message}"
    if details:
        line += f" | details={details}"
    app.logger.warning(line)


@app.errorhandler(APIError)
def handle_api_error(e):
    log_failed_request(e.status, e.code, e.message, e.details)
    return error_response(e.status, e.code, e.message, e.details)


@app.errorhandler(HTTPException)
def handle_http_error(e):
    # 404 unknown URL, 405 wrong method, etc.
    code = e.name.lower().replace(" ", "_")
    log_failed_request(e.code, code, e.description)
    return error_response(e.code, code, e.description)


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    # logger.exception writes the full traceback to the log (never to the client)
    app.logger.exception("%s %s -> 500 unhandled %s: %s",
                         request.method, request.path, type(e).__name__, e)
    return error_response(500, "internal_server_error",
                          "Something went wrong on the server.")


# --------------------------------------------------------------------------- #
# Request parsing + validation helpers
# --------------------------------------------------------------------------- #
def json_body():
    if not request.is_json:
        raise APIError(415, "Content-Type must be application/json",
                       "unsupported_media_type")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError(400, "Request body must be a valid JSON object",
                       "invalid_json")
    return data


def check_unknown_fields(data, allowed):
    unknown = [k for k in data if k not in allowed]
    if unknown:
        raise APIError(400, "Unknown field(s) in request body", "unknown_fields",
                       {"unknown": unknown, "allowed": sorted(allowed)})


def partial_body(current_values, allowed):
    """PATCH: the fields sent replace the current ones, the others stay as they are."""
    data = json_body()
    check_unknown_fields(data, allowed)
    if not data:
        raise APIError(400, "Send at least one field to update.", "validation_error",
                       {"body": f"Allowed fields: {', '.join(sorted(allowed))}."})
    return {**current_values, **data}


def clean_string(data, field, errors):
    """Required, non-empty string. On failure the problem is stored in `errors`."""
    value = data.get(field)
    if value is None:
        errors[field] = "This field is required."
        return None
    if not isinstance(value, str) or not value.strip():
        errors[field] = "Must be a non-empty string."
        return None
    return value.strip()


def match_choice(value, choices):
    """Case-insensitive match against a list of allowed values."""
    if isinstance(value, str):
        for choice in choices:
            if value.strip().lower() == choice.lower():
                return choice
    return None


def validate_student(data):
    check_unknown_fields(data, set(STUDENT_FIELDS))
    errors = {}
    first = clean_string(data, "FirstName", errors)
    last = clean_string(data, "LastName", errors)

    double_major = data.get("DoubleMajor")
    if "DoubleMajor" not in data:
        errors["DoubleMajor"] = "This field is required."
    elif not isinstance(double_major, bool):
        errors["DoubleMajor"] = "Must be a boolean (true or false)."

    level = data.get("Level")
    if "Level" not in data:
        errors["Level"] = "This field is required."
    elif not isinstance(level, int) or isinstance(level, bool) or level not in LEVELS:
        errors["Level"] = "Must be an integer: 1, 2, 3 or 4."

    if errors:
        raise APIError(400, "Validation failed", "validation_error", errors)
    return {"FirstName": first, "LastName": last,
            "DoubleMajor": double_major, "Level": level}


def validate_report(data):
    check_unknown_fields(data, set(REPORT_FIELDS))
    errors = {}
    title = clean_string(data, "Title", errors)
    k1 = clean_string(data, "K1", errors)
    k2 = clean_string(data, "K2", errors)
    k3 = clean_string(data, "K3", errors)

    student_id = data.get("ID_S")
    if "ID_S" not in data:
        errors["ID_S"] = "This field is required."
    elif not isinstance(student_id, int) or isinstance(student_id, bool):
        errors["ID_S"] = "Must be an integer student ID."

    report_type = None
    if "Type" not in data:
        errors["Type"] = "This field is required."
    else:
        report_type = match_choice(data["Type"], REPORT_TYPES)
        if report_type is None:
            errors["Type"] = f"Must be one of: {', '.join(REPORT_TYPES)}."

    release = None
    if "ReleaseDate" not in data:
        errors["ReleaseDate"] = "This field is required."
    else:
        try:
            value = data["ReleaseDate"]
            if not isinstance(value, str):
                raise ValueError
            release = date.fromisoformat(value)
            if release.isoformat() != value:  # only the strict YYYY-MM-DD form
                raise ValueError
        except ValueError:
            release = None
            errors["ReleaseDate"] = "Must be a valid date formatted YYYY-MM-DD."

    if errors:
        raise APIError(400, "Validation failed", "validation_error", errors)
    return {"ID_S": student_id, "Title": title, "Type": report_type,
            "K1": k1, "K2": k2, "K3": k3, "ReleaseDate": release}


def ensure_report_allowed(student_id, ignore_report_id=None):
    """The student must exist and must not exceed 1 report (2 if double major)."""
    student = find_student(student_id)
    if student is None:
        raise APIError(422, f"Student {student_id} does not exist.",
                       "invalid_reference")
    limit = max_reports(student.DoubleMajor)
    if count_reports(student_id, ignore_report_id) >= limit:
        raise APIError(
            409,
            f"Student {student_id} already has {limit} report(s), which is the "
            f"maximum ({'double major' if limit == 2 else 'single major'}).",
            "report_limit_reached")


# --------------------------------------------------------------------------- #
# Authentication: access token + refresh token (JWT)
# --------------------------------------------------------------------------- #
def create_token(subject, kind):
    now = datetime.now(timezone.utc)
    ttl = ACCESS_TTL if kind == "access" else REFRESH_TTL
    payload = {"sub": subject, "type": kind, "jti": uuid.uuid4().hex,
               "iat": now, "exp": now + ttl}
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token, expected_type):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise APIError(401, "The token has expired.", "token_expired")
    except jwt.InvalidTokenError:
        raise APIError(401, "The token is invalid.", "token_invalid")
    if payload.get("type") != expected_type:
        raise APIError(401, f"A {expected_type} token is required here.",
                       "wrong_token_type")
    if payload.get("jti") in REVOKED_JTIS:
        raise APIError(401, "The token has already been used or revoked.",
                       "token_revoked")
    return payload


def token_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise APIError(401, "Missing token. Send 'Authorization: Bearer <token>'.",
                           "token_missing")
        decode_token(token.strip(), "access")
        return view(*args, **kwargs)
    return wrapper


def token_pair(subject):
    return {"access_token": create_token(subject, "access"),
            "refresh_token": create_token(subject, "refresh"),
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TTL.total_seconds())}


@app.post("/auth/login")
def login():
    data = json_body()
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))
    ok_user = hmac.compare_digest(username.encode(), API_USERNAME.encode())
    ok_pass = hmac.compare_digest(password.encode(), API_PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise APIError(401, "Wrong username or password.", "invalid_credentials")
    app.logger.info("Login OK for user %r", username)
    return jsonify(token_pair(username))


@app.post("/auth/refresh")
def refresh():
    data = json_body()
    token = data.get("refresh_token")
    if not isinstance(token, str) or not token:
        raise APIError(400, "refresh_token is required.", "validation_error",
                       {"refresh_token": "This field is required."})
    payload = decode_token(token, "refresh")
    REVOKED_JTIS.add(payload["jti"])  # rotation: a refresh token works only once
    return jsonify(token_pair(payload["sub"]))


# --------------------------------------------------------------------------- #
# Students  (only the GET endpoints require a token)
# --------------------------------------------------------------------------- #
def save_student_changes(student, values):
    """Shared by PUT and PATCH."""
    if count_reports(student.ID_S) > max_reports(values["DoubleMajor"]):
        raise APIError(
            409,
            "Cannot remove the double major while the student has 2 reports. "
            "Delete one report first.", "report_limit_reached")
    apply_values(student, values)
    commit()
    app.logger.info("Student %s updated", student.ID_S)
    return jsonify(student_to_dict(student))


@app.post("/students")
def create_student():
    student = Student(**validate_student(json_body()))
    db.session.add(student)
    commit()
    app.logger.info("Student %s created", student.ID_S)
    response = jsonify(student_to_dict(student))
    response.status_code = 201
    response.headers["Location"] = f"/students/{student.ID_S}"
    return response


@app.get("/students")
@token_required
def list_students():
    students = db.session.scalars(select(Student).order_by(Student.ID_S)).all()
    return jsonify([student_to_dict(s) for s in students])


@app.get("/students/<int:student_id>")
@token_required
def get_student(student_id):
    return jsonify(student_to_dict(get_student_or_404(student_id)))


@app.put("/students/<int:student_id>")
def update_student(student_id):
    student = get_student_or_404(student_id)
    return save_student_changes(student, validate_student(json_body()))


@app.patch("/students/<int:student_id>")
def patch_student(student_id):
    student = get_student_or_404(student_id)
    current = {field: getattr(student, field) for field in STUDENT_FIELDS}
    merged = partial_body(current, set(STUDENT_FIELDS))
    return save_student_changes(student, validate_student(merged))


@app.delete("/students/<int:student_id>")
def delete_student(student_id):
    student = get_student_or_404(student_id)
    deleted_reports = len(student.reports)
    db.session.delete(student)  # their reports are deleted with them (cascade)
    commit()
    app.logger.info("Student %s deleted (with %s report(s))", student_id, deleted_reports)
    return jsonify({"message": f"Student {student_id} deleted.",
                    "deleted_reports": deleted_reports})


# --------------------------------------------------------------------------- #
# Reports  (free access)
# --------------------------------------------------------------------------- #
def save_report_changes(report, values):
    """Shared by PUT and PATCH."""
    ensure_report_allowed(values["ID_S"], ignore_report_id=report.ID_R)
    apply_values(report, values)
    commit()
    app.logger.info("Report %s updated", report.ID_R)
    return jsonify(report_to_dict(report))


@app.post("/reports")
def create_report():
    values = validate_report(json_body())
    ensure_report_allowed(values["ID_S"])
    report = Report(**values)
    db.session.add(report)
    commit()
    app.logger.info("Report %s created for student %s", report.ID_R, report.ID_S)
    response = jsonify(report_to_dict(report))
    response.status_code = 201
    response.headers["Location"] = f"/reports/{report.ID_R}"
    return response


@app.get("/reports")
def list_reports():
    reports = db.session.scalars(select(Report).order_by(Report.ID_R)).all()
    return jsonify([report_to_dict(r) for r in reports])


@app.get("/reports/<int:report_id>")
def get_report(report_id):
    return jsonify(report_to_dict(get_report_or_404(report_id)))


@app.put("/reports/<int:report_id>")
def update_report(report_id):
    report = get_report_or_404(report_id)
    return save_report_changes(report, validate_report(json_body()))


@app.patch("/reports/<int:report_id>")
def patch_report(report_id):
    report = get_report_or_404(report_id)
    current = {field: report_to_dict(report)[field] for field in REPORT_FIELDS}
    merged = partial_body(current, set(REPORT_FIELDS))
    return save_report_changes(report, validate_report(merged))


@app.delete("/reports/<int:report_id>")
def delete_report(report_id):
    report = get_report_or_404(report_id)
    db.session.delete(report)
    commit()
    app.logger.info("Report %s deleted", report_id)
    return jsonify({"message": f"Report {report_id} deleted."})


def log_startup():
    app.logger.info(
        "API starting | access_ttl=%ss refresh_ttl=%ss log_level=%s log_file=%s database=%s",
        int(ACCESS_TTL.total_seconds()), int(REFRESH_TTL.total_seconds()),
        LOG_LEVEL, LOG_FILE, _db_url.get_backend_name())  # never log the full URL
    if SECRET_KEY.startswith("dev-only"):
        app.logger.warning("SECRET_KEY is the built-in development default. "
                           "Set the SECRET_KEY environment variable.")


log_startup()

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=DEBUG)
