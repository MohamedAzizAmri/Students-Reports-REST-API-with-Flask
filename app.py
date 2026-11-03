import hmac
import itertools
import logging
import os
import uuid
from logging.handlers import RotatingFileHandler
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
import jwt
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.json.sort_keys = False  # keep field order as written below

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-secret-change-me-before-deploying")
ACCESS_TTL = timedelta(minutes=int(os.environ.get("ACCESS_TOKEN_MINUTES", "5")))
REFRESH_TTL = timedelta(hours=int(os.environ.get("REFRESH_TOKEN_HOURS", "24")))

API_USERNAME = os.environ.get("API_USERNAME", "admin")
API_PASSWORD = os.environ.get("API_PASSWORD", "admin123")

HOST = os.environ.get("HOST", "127.0.0.1")        
PORT = int(os.environ.get("PORT", "5000"))
DEBUG = os.environ.get("FLASK_DEBUG", "0").lower() in ("1", "true", "yes")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.environ.get("LOG_FILE", "logs/app.log")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_FILE = BASE_DIR / "data" / "app.db"
DEFAULT_DB_FILE.parent.mkdir(exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DEFAULT_DB_FILE.as_posix()}")

LEVELS = [1, 2, 3, 4]  
REPORT_TYPES = ["Mini senior project", "End-of-studies project"]

# --------------------------------------------------------------------------- #
# Logging: console + rotating file (logs/app.log). Never log passwords/tokens.
# --------------------------------------------------------------------------- #
def configure_logging():
    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
    handlers = [logging.StreamHandler()] 
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        handlers.append(RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError as exc:  
        print(f"WARNING: cannot write log file {LOG_FILE!r}: {exc}")
    app.logger.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        app.logger.addHandler(handler)
    app.logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    app.logger.propagate = False  


configure_logging()


# --------------------------------------------------------------------------- #
# Data: the two lists
# --------------------------------------------------------------------------- #
STUDENTS = []   
REPORTS = []    
REVOKED_JTIS = set()  

_student_ids = itertools.count(1)
_report_ids = itertools.count(1)


def reset_state():
    """Empty everything (used by the tests)."""
    global _student_ids, _report_ids
    STUDENTS.clear()
    REPORTS.clear()
    REVOKED_JTIS.clear()
    _student_ids = itertools.count(1)
    _report_ids = itertools.count(1)


def find_student(student_id):
    return next((s for s in STUDENTS if s["ID_S"] == student_id), None)


def find_report(report_id):
    return next((r for r in REPORTS if r["ID_R"] == report_id), None)


def reports_of(student_id):
    return [r for r in REPORTS if r["ID_S"] == student_id]


def max_reports(student):
    """1 report normally, 2 if the student has a double major."""
    return 2 if student["DoubleMajor"] else 1


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
    code = e.name.lower().replace(" ", "_")
    log_failed_request(e.code, code, e.description)
    return error_response(e.code, code, e.description)


@app.errorhandler(Exception)
def handle_unexpected_error(e):
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
    check_unknown_fields(data, {"FirstName", "LastName", "DoubleMajor", "Level"})
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
    check_unknown_fields(
        data, {"ID_S", "Title", "Type", "K1", "K2", "K3", "ReleaseDate"})
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
            release = date.fromisoformat(str(data["ReleaseDate"])).isoformat()
        except ValueError:
            errors["ReleaseDate"] = "Must be a valid date formatted YYYY-MM-DD."

    if errors:
        raise APIError(400, "Validation failed", "validation_error", errors)
    return {"ID_S": student_id, "Title": title, "Type": report_type,
            "K1": k1, "K2": k2, "K3": k3, "ReleaseDate": release}


def ensure_report_allowed(student_id):
    """The student must exist and must not exceed 1 report (2 if double major)."""
    student = find_student(student_id)
    if student is None:
        raise APIError(422, f"Student {student_id} does not exist.",
                       "invalid_reference")
    if len(reports_of(student_id)) >= max_reports(student):
        limit = max_reports(student)
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
    REVOKED_JTIS.add(payload["jti"])  
    return jsonify(token_pair(payload["sub"]))


# --------------------------------------------------------------------------- #
# Students  
# --------------------------------------------------------------------------- #
@app.post("/students")
def create_student():
    student = {"ID_S": next(_student_ids), **validate_student(json_body())}
    STUDENTS.append(student)
    app.logger.info("Student %s created", student["ID_S"])
    response = jsonify(student)
    response.status_code = 201
    response.headers["Location"] = f"/students/{student['ID_S']}"
    return response


@app.get("/students")
@token_required
def list_students():
    return jsonify(STUDENTS)


@app.get("/students/<int:student_id>")
@token_required
def get_student(student_id):
    student = find_student(student_id)
    if student is None:
        raise APIError(404, f"Student {student_id} not found.", "not_found")
    return jsonify(student)


# --------------------------------------------------------------------------- #
# Reports  
# --------------------------------------------------------------------------- #
@app.post("/reports")
def create_report():
    values = validate_report(json_body())
    ensure_report_allowed(values["ID_S"])
    report = {"ID_R": next(_report_ids), "Title": values["Title"],
              "Type": values["Type"], "K1": values["K1"], "K2": values["K2"],
              "K3": values["K3"], "ReleaseDate": values["ReleaseDate"],
              "ID_S": values["ID_S"]}
    REPORTS.append(report)
    app.logger.info("Report %s created for student %s", report["ID_R"], report["ID_S"])
    response = jsonify(report)
    response.status_code = 201
    response.headers["Location"] = f"/reports/{report['ID_R']}"
    return response


@app.get("/reports")
def list_reports():
    return jsonify(REPORTS)


@app.get("/reports/<int:report_id>")
def get_report(report_id):
    report = find_report(report_id)
    if report is None:
        raise APIError(404, f"Report {report_id} not found.", "not_found")
    return jsonify(report)


def log_startup():
    app.logger.info("API starting | access_ttl=%ss refresh_ttl=%ss log_level=%s log_file=%s",
                    int(ACCESS_TTL.total_seconds()), int(REFRESH_TTL.total_seconds()),
                    LOG_LEVEL, LOG_FILE)
    if SECRET_KEY.startswith("dev-only"):
        app.logger.warning("SECRET_KEY is the built-in development default. "
                           "Set the SECRET_KEY environment variable.")


log_startup()

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=DEBUG)
