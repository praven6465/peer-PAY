import os
import re
import math
import secrets
import sqlite3

# Load local development secrets from a .env file beside app.py.
# In PythonAnywhere, environment variables set in the WSGI file still take precedence.
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "development-only-change-me"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

PILOT_LOCALITY = os.environ.get("PILOT_LOCALITY", "Nagarbhavi, Bengaluru")
SEARCH_RADIUS_KM = 1.0
PLATFORM_COMMISSION_PCT = 15.0  # Charged only on the host's service component.
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(app.instance_path, "chargenear_v2.sqlite3"))
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
os.makedirs(app.instance_path, exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_session() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('customer','host')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
            area TEXT NOT NULL,
            full_address TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            supported_models TEXT NOT NULL,
            setup_type TEXT NOT NULL,
            meter_type TEXT NOT NULL,
            electricity_rate REAL NOT NULL,
            service_rate REAL NOT NULL,
            outdoor_parking INTEGER NOT NULL DEFAULT 0,
            fixed_charging_point INTEGER NOT NULL DEFAULT 0,
            protection_confirmed INTEGER NOT NULL DEFAULT 0,
            location_clearance_confirmed INTEGER NOT NULL DEFAULT 0,
            permission_confirmed INTEGER NOT NULL DEFAULT 0,
            safety_notes TEXT DEFAULT '',
            available INTEGER NOT NULL DEFAULT 0,
            verification_status TEXT NOT NULL DEFAULT 'pending',
            admin_note TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES users(id),
            host_id INTEGER NOT NULL REFERENCES hosts(id),
            vehicle_model TEXT NOT NULL,
            purpose TEXT NOT NULL CHECK(purpose IN ('emergency','everyday')),
            scheduled_for TEXT NOT NULL,
            target_kwh REAL NOT NULL,
            electricity_rate REAL NOT NULL,
            service_rate REAL NOT NULL,
            commission_pct REAL NOT NULL,
            quote_total REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'requested',
            meter_start REAL,
            meter_end REAL,
            actual_kwh REAL,
            electricity_amount REAL,
            service_gross REAL,
            platform_revenue REAL,
            host_payout REAL,
            final_total REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hosts_discoverable ON hosts(verification_status, available);
        CREATE INDEX IF NOT EXISTS idx_bookings_customer ON bookings(customer_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_bookings_host ON bookings(host_id, created_at);
        """)


init_db()


def now_iso():
    # Store local Bengaluru wall-clock time for datetime-local form compatibility.
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None).isoformat(timespec="minutes")


def money(value):
    return round(float(value) + 1e-9, 2)


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def as_float(value, label, minimum=0.0, maximum=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Enter a valid {label}.")
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        cap = f" and {maximum}" if maximum is not None else ""
        raise ValueError(f"{label.capitalize()} must be between {minimum}{cap}.")
    return result


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped


def role_required(role):
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if session.get("role") != role:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


@app.before_request
def csrf_protect():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    if request.method == "POST":
        supplied = request.form.get("csrf_token", "")
        if not secrets.compare_digest(supplied, session.get("csrf_token", "")):
            abort(400, description="The form expired or could not be verified. Please go back and try again.")


@app.context_processor
def inject_globals():
    return {
        "csrf_token": lambda: session.get("csrf_token", ""),
        "pilot_locality": PILOT_LOCALITY,
        "commission_pct": PLATFORM_COMMISSION_PCT,
        "search_radius_km": SEARCH_RADIUS_KM,
    }


@app.route("/")
def home():
    with db_session() as con:
        approved_count = con.execute("SELECT COUNT(*) FROM hosts WHERE verification_status='approved' AND available=1").fetchone()[0]
    return render_template("home.html", approved_count=approved_count)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = re.sub(r"[\s-]", "", request.form.get("phone", ""))
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        if not name or len(name) > 80:
            flash("Enter your name (up to 80 characters).", "error")
        elif not re.fullmatch(r"\+?[0-9]{10,15}", phone):
            flash("Enter a valid phone number with 10–15 digits.", "error")
        elif len(password) < 8:
            flash("Use a password with at least 8 characters.", "error")
        elif role not in ("customer", "host"):
            flash("Choose Customer or Host.", "error")
        else:
            try:
                with db_session() as con:
                    con.execute("INSERT INTO users(name,phone,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                                (name, phone, generate_password_hash(password), role, now_iso()))
                flash("Account created. Please log in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("That phone number is already registered.", "error")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = re.sub(r"[\s-]", "", request.form.get("phone", ""))
        with db_session() as con:
            user = con.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.update(user_id=user["id"], name=user["name"], role=user["role"])
            return redirect(url_for("dashboard"))
        flash("Invalid phone number or password.", "error")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


@app.route("/dashboard")
@login_required
def dashboard():
    return redirect(url_for("host_dashboard" if session["role"] == "host" else "customer_dashboard"))


@app.route("/customer")
@role_required("customer")
def customer_dashboard():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    if lat is not None and not -90 <= lat <= 90: lat = None
    if lon is not None and not -180 <= lon <= 180: lon = None
    hosts = []
    with db_session() as con:
        rows = con.execute("""SELECT h.*, u.name AS host_name FROM hosts h
            JOIN users u ON u.id=h.user_id
            WHERE h.available=1 AND h.verification_status='approved'""").fetchall()
        booking_rows = con.execute("""SELECT b.*, u.name AS host_name, h.area,
            CASE WHEN b.status IN ('accepted','arrived','charging','completed') THEN h.full_address ELSE NULL END AS shared_address,
            CASE WHEN b.status IN ('accepted','arrived','charging','completed') THEN u.phone ELSE NULL END AS shared_phone
            FROM bookings b JOIN hosts h ON h.id=b.host_id JOIN users u ON u.id=h.user_id
            WHERE b.customer_id=? ORDER BY b.id DESC LIMIT 30""", (session["user_id"],)).fetchall()
    if lat is not None and lon is not None:
        for row in rows:
            item = dict(row)
            item["distance"] = haversine_km(lat, lon, row["latitude"], row["longitude"])
            if item["distance"] <= SEARCH_RADIUS_KM:
                hosts.append(item)
        hosts.sort(key=lambda item: item["distance"])
    return render_template("customer.html", hosts=hosts, bookings=booking_rows, lat=lat, lon=lon,
                           map_hosts=[{"name": h["host_name"], "lat": h["latitude"], "lon": h["longitude"],
                                       "area": h["area"], "distance": round(h["distance"], 2)} for h in hosts])


@app.route("/host", methods=["GET", "POST"])
@role_required("host")
def host_dashboard():
    with db_session() as con:
        existing = con.execute("SELECT * FROM hosts WHERE user_id=?", (session["user_id"],)).fetchone()
        if request.method == "POST":
            try:
                area = request.form.get("area", "").strip()
                address = request.form.get("full_address", "").strip()
                latitude = as_float(request.form.get("latitude"), "latitude", -90, 90)
                longitude = as_float(request.form.get("longitude"), "longitude", -180, 180)
                supported = request.form.get("supported_models", "").strip()
                setup_type = request.form.get("setup_type", "")
                meter_type = request.form.get("meter_type", "")
                electricity_rate = as_float(request.form.get("electricity_rate"), "electricity rate", 0.01, 100)
                service_rate = as_float(request.form.get("service_rate"), "host service rate", 0, 100)
                safety_notes = request.form.get("safety_notes", "").strip()
                outdoor = int(request.form.get("outdoor_parking") == "on")
                fixed_point = int(request.form.get("fixed_charging_point") == "on")
                protection = int(request.form.get("protection_confirmed") == "on")
                location_clearance = int(request.form.get("location_clearance_confirmed") == "on")
                permission = int(request.form.get("permission_confirmed") == "on")
                available = int(request.form.get("available") == "on")
                if not area or len(area) > 100 or not address or len(address) > 300:
                    raise ValueError("Enter the area and full address.")
                pilot_area = PILOT_LOCALITY.split(",")[0].strip().casefold()
                if pilot_area and pilot_area not in area.casefold():
                    raise ValueError(f"The first pilot is limited to {PILOT_LOCALITY}. Please list a host inside {pilot_area.title()}.")
                if not supported or len(supported) > 300:
                    raise ValueError("List the exact two-wheeler models supported (up to 300 characters).")
                if setup_type not in ("Dedicated fixed EV charging point", "Manufacturer-approved charger at fixed protected outlet"):
                    raise ValueError("Choose an approved charging setup type.")
                if meter_type not in ("Dedicated sub-meter", "Smart charger with kWh reading"):
                    raise ValueError("Choose a way to measure delivered energy in kWh.")
                if not (outdoor and fixed_point and protection and location_clearance and permission):
                    raise ValueError("Confirm outdoor parking, fixed charging point, electrical protection, socket/cable clearance, and owner/society permissions before submitting.")
                values = (area, address, latitude, longitude, supported, setup_type, meter_type,
                          electricity_rate, service_rate, outdoor, fixed_point, protection, location_clearance, permission,
                          safety_notes, available, now_iso())
                if existing:
                    # Re-submit safety and location details for review; no unreviewed edits remain bookable.
                    con.execute("""UPDATE hosts SET area=?,full_address=?,latitude=?,longitude=?,supported_models=?,
                        setup_type=?,meter_type=?,electricity_rate=?,service_rate=?,outdoor_parking=?,fixed_charging_point=?,
                        protection_confirmed=?,location_clearance_confirmed=?,permission_confirmed=?,safety_notes=?,available=?,verification_status='pending',
                        admin_note='',updated_at=? WHERE user_id=?""", values + (session["user_id"],))
                else:
                    con.execute("""INSERT INTO hosts(area,full_address,latitude,longitude,supported_models,setup_type,meter_type,
                        electricity_rate,service_rate,outdoor_parking,fixed_charging_point,protection_confirmed,location_clearance_confirmed,permission_confirmed,
                        safety_notes,available,updated_at,user_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        values + (session["user_id"],))
                flash("Host listing submitted for safety review. It will appear to customers after approval.", "success")
                return redirect(url_for("host_dashboard"))
            except ValueError as exc:
                flash(str(exc), "error")
        existing = con.execute("SELECT * FROM hosts WHERE user_id=?", (session["user_id"],)).fetchone()
        requests = con.execute("""SELECT b.*, u.name AS customer_name,
            CASE WHEN b.status IN ('accepted','arrived','charging','completed') THEN u.phone ELSE NULL END AS customer_phone
            FROM bookings b JOIN users u ON u.id=b.customer_id JOIN hosts h ON h.id=b.host_id
            WHERE h.user_id=? ORDER BY b.id DESC LIMIT 40""", (session["user_id"],)).fetchall()
    return render_template("host.html", host=existing, requests=requests)


@app.post("/host/availability")
@role_required("host")
def host_availability():
    available = 1 if request.form.get("available") == "1" else 0
    with db_session() as con:
        con.execute("UPDATE hosts SET available=?,updated_at=? WHERE user_id=? AND verification_status='approved'",
                    (available, now_iso(), session["user_id"]))
    flash("Availability updated. Your safety approval remains unchanged.", "success")
    return redirect(url_for("host_dashboard"))


@app.post("/book/<int:host_id>")
@role_required("customer")
def book(host_id):
    try:
        target_kwh = as_float(request.form.get("target_kwh"), "requested kWh", 0.1, 15)
        vehicle_model = request.form.get("vehicle_model", "").strip()
        purpose = request.form.get("purpose", "")
        scheduled_for = request.form.get("scheduled_for", "").strip()
        if not vehicle_model or len(vehicle_model) > 100:
            raise ValueError("Enter your exact electric two-wheeler model.")
        if purpose not in ("emergency", "everyday"):
            raise ValueError("Choose emergency or everyday charging.")
        if not scheduled_for:
            scheduled_for = now_iso()
        try:
            requested_time = datetime.fromisoformat(scheduled_for)
        except ValueError:
            raise ValueError("Choose a valid preferred date and time.")
        if purpose == "everyday" and requested_time < datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None):
            raise ValueError("Choose a future date and time for everyday charging.")
        with db_session() as con:
            host = con.execute("""SELECT * FROM hosts WHERE id=? AND available=1
                AND verification_status='approved'""", (host_id,)).fetchone()
            if not host:
                abort(404)
            model_list = [x.strip().casefold() for x in host["supported_models"].split(",") if x.strip()]
            if vehicle_model.casefold() not in model_list and "all compatible models" not in model_list:
                raise ValueError("This host has not listed your exact vehicle model as compatible.")
            quote_total = money(target_kwh * (host["electricity_rate"] + host["service_rate"]))
            con.execute("""INSERT INTO bookings(customer_id,host_id,vehicle_model,purpose,scheduled_for,target_kwh,
                electricity_rate,service_rate,commission_pct,quote_total,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?, 'requested',?,?)""",
                (session["user_id"], host_id, vehicle_model, purpose, scheduled_for, target_kwh,
                 host["electricity_rate"], host["service_rate"], PLATFORM_COMMISSION_PCT,
                 quote_total, now_iso(), now_iso()))
        flash("Request sent. Wait for the host to accept before travelling.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("customer_dashboard"))


@app.post("/booking/<int:booking_id>/<action>")
@login_required
def booking_action(booking_id, action):
    role = session.get("role")
    with db_session() as con:
        if role == "host":
            booking = con.execute("""SELECT b.* FROM bookings b JOIN hosts h ON h.id=b.host_id
                WHERE b.id=? AND h.user_id=?""", (booking_id, session["user_id"])).fetchone()
        elif role == "customer":
            booking = con.execute("SELECT * FROM bookings WHERE id=? AND customer_id=?", (booking_id, session["user_id"])).fetchone()
        else:
            abort(403)
        if not booking:
            abort(404)
        status = booking["status"]
        if role == "host" and action == "accepted" and status == "requested":
            active = con.execute("""SELECT COUNT(*) FROM bookings WHERE host_id=? AND id<>?
                AND status IN ('accepted','arrived','charging')""", (booking["host_id"], booking_id)).fetchone()[0]
            if active:
                flash("You already have an active charging session. Complete it before accepting another.", "error")
                return redirect(url_for("host_dashboard"))
            new_status = "accepted"
        elif role == "host" and action == "rejected" and status == "requested":
            new_status = "rejected"
        elif role == "customer" and action == "arrived" and status == "accepted":
            new_status = "arrived"
        elif role == "host" and action == "charging" and status == "arrived":
            new_status = "charging"
        elif role == "host" and action == "completed" and status == "charging":
            try:
                meter_start = as_float(request.form.get("meter_start"), "meter start reading", 0, 1000000)
                meter_end = as_float(request.form.get("meter_end"), "meter end reading", 0, 1000000)
                actual_kwh = round(meter_end - meter_start, 3)
                if actual_kwh <= 0 or actual_kwh > 15:
                    raise ValueError("The end reading must exceed the start reading, with no more than 15 kWh delivered per session.")
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("host_dashboard"))
            electricity_amount = money(actual_kwh * booking["electricity_rate"])
            service_gross = money(actual_kwh * booking["service_rate"])
            platform_revenue = money(service_gross * booking["commission_pct"] / 100)
            host_payout = money(electricity_amount + service_gross - platform_revenue)
            final_total = money(electricity_amount + service_gross)
            con.execute("""UPDATE bookings SET status='completed',meter_start=?,meter_end=?,actual_kwh=?,
                electricity_amount=?,service_gross=?,platform_revenue=?,host_payout=?,final_total=?,updated_at=? WHERE id=?""",
                (meter_start, meter_end, actual_kwh, electricity_amount, service_gross, platform_revenue,
                 host_payout, final_total, now_iso(), booking_id))
            flash("Session marked complete. The final kWh-based bill is now calculated (demo only; no payment collected).", "success")
            return redirect(url_for("host_dashboard"))
        else:
            abort(400, description="That action is not valid for the current booking status.")
        con.execute("UPDATE bookings SET status=?,updated_at=? WHERE id=?", (new_status, now_iso(), booking_id))
    flash(f"Booking status updated to {new_status}.", "success")
    return redirect(url_for("host_dashboard" if role == "host" else "customer_dashboard"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    admin_key = os.environ.get("ADMIN_KEY")
    if not admin_key:
        abort(404)
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("admin_key", ""), admin_key):
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Invalid admin key.", "error")
    return render_template("admin_login.html")


@app.route("/admin")
def admin_dashboard():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))
    with db_session() as con:
        pending = con.execute("""SELECT h.*,u.name AS host_name,u.phone FROM hosts h
            JOIN users u ON u.id=h.user_id WHERE h.verification_status='pending' ORDER BY h.updated_at""").fetchall()
        approved = con.execute("""SELECT h.*,u.name AS host_name FROM hosts h
            JOIN users u ON u.id=h.user_id WHERE h.verification_status='approved' ORDER BY h.updated_at DESC LIMIT 30""").fetchall()
    return render_template("admin.html", pending=pending, approved=approved)


@app.post("/admin/host/<int:host_id>/<decision>")
def admin_decision(host_id, decision):
    if not session.get("is_admin"):
        abort(403)
    if decision not in ("approve", "reject"):
        abort(400)
    note = request.form.get("admin_note", "").strip()[:300]
    status = "approved" if decision == "approve" else "rejected"
    with db_session() as con:
        con.execute("UPDATE hosts SET verification_status=?,admin_note=?,updated_at=? WHERE id=?",
                    (status, note, now_iso(), host_id))
    flash(f"Host listing {status}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/logout")
def admin_logout():
    if not session.get("is_admin"):
        abort(403)
    session.clear()
    return redirect(url_for("home"))


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", code=400, message=getattr(error, "description", "The request could not be processed.")), 400


@app.errorhandler(403)
def forbidden(error):
    return render_template("error.html", code=403, message="You don't have permission to view this page."), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, message="We couldn't find that page or listing."), 404


if __name__ == "__main__":
    app.run(debug=False)
