import os
import sqlite3
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
from patent_data import PATENTS
import csv
import io
from flask import Response

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, "startupverse.db")

app = Flask(__name__)
app.secret_key = os.environ.get("STARTUPVERSE_SECRET", "change-this-development-secret-key")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('investor', 'finder')),
        activity_points INTEGER NOT NULL DEFAULT 0,
        trust_score INTEGER NOT NULL DEFAULT 50,
        last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS investor_profiles (
        user_id INTEGER PRIMARY KEY,
        organization TEXT DEFAULT '',
        industries TEXT DEFAULT '',
        funding_min REAL DEFAULT 0,
        funding_max REAL DEFAULT 0,
        preferred_stage TEXT DEFAULT 'Any',
        location TEXT DEFAULT '',
        bio TEXT DEFAULT '',
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS startup_profiles (
        user_id INTEGER PRIMARY KEY,
        startup_name TEXT DEFAULT '',
        industry TEXT DEFAULT '',
        funding_needed REAL DEFAULT 0,
        stage TEXT DEFAULT 'Idea',
        location TEXT DEFAULT '',
        description TEXT DEFAULT '',
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS compliance_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        due_date TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending', 'In Progress', 'Completed')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS meetings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        investor_user_id INTEGER NOT NULL,
        finder_user_id INTEGER NOT NULL,
        requested_by INTEGER NOT NULL,
        proposed_at TEXT NOT NULL DEFAULT '',
        message TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'Requested'
          CHECK(status IN ('Requested', 'Accepted', 'Declined', 'Completed', 'Cancelled')),
        investor_confirmed INTEGER NOT NULL DEFAULT 0,
        finder_confirmed INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(investor_user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(finder_user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(requested_by) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "activity_points" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN activity_points INTEGER NOT NULL DEFAULT 0")
    if "trust_score" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN trust_score INTEGER NOT NULL DEFAULT 50")
    if "last_active" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    db.commit()
    db.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def role_required(role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                flash("Please log in to continue.", "warning")
                return redirect(url_for("login"))
            if session.get("role") != role:
                flash("That page is not available for your account type.", "warning")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def get_matches(startup):
    db = get_db()
    investors = db.execute("""
        SELECT u.id, u.name, u.email, u.last_active, p.organization, p.industries,
               p.funding_min, p.funding_max, p.preferred_stage, p.location, p.bio
        FROM users u JOIN investor_profiles p ON p.user_id = u.id
        WHERE u.role = 'investor'
        ORDER BY u.name
    """).fetchall()
    matches = []
    for investor in investors:
        industries = [x.strip().lower() for x in (investor["industries"] or "").split(",") if x.strip()]
        startup_industry = (startup["industry"] or "").strip().lower()
        industry_score = 100 if startup_industry and startup_industry in industries else (50 if "any" in industries else 0)

        need = float(startup["funding_needed"] or 0)
        low, high = float(investor["funding_min"] or 0), float(investor["funding_max"] or 0)
        if low <= need <= high and high > 0:
            funding_score = 100
        elif high > 0 and need > 0:
            funding_score = max(0, int(100 - min(abs(need - high) / max(high, 1) * 100, 100)))
        else:
            funding_score = 50

        inv_stage = (investor["preferred_stage"] or "Any").lower()
        stage_score = 100 if inv_stage == "any" or inv_stage == (startup["stage"] or "").lower() else 0
        inv_location = (investor["location"] or "").strip().lower()
        startup_location = (startup["location"] or "").strip().lower()
        location_score = 100 if inv_location and startup_location and inv_location == startup_location else (50 if not inv_location else 0)
        score = round(industry_score * .4 + funding_score * .3 + stage_score * .2 + location_score * .1)
        matches.append({
            "id": investor["id"], "name": investor["name"], "email": investor["email"], "last_active": investor["last_active"],
            "organization": investor["organization"], "industries": investor["industries"],
            "funding_min": investor["funding_min"], "funding_max": investor["funding_max"],
            "preferred_stage": investor["preferred_stage"], "location": investor["location"],
            "bio": investor["bio"], "score": score
        })
    return sorted(matches, key=lambda item: item["score"], reverse=True)



def award_points(user_id, points, trust_delta=0):
    db = get_db()
    db.execute(
        "UPDATE users SET activity_points = activity_points + ?, trust_score = MIN(100, MAX(0, trust_score + ?)) WHERE id = ?",
        (points, trust_delta, user_id)
    )
    db.commit()


@app.before_request
def update_last_active():
    user_id = session.get("user_id")
    if user_id:
        try:
            db = get_db()
            db.execute("UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
            db.commit()
        except sqlite3.Error:
            pass


@app.context_processor
def inject_presence_helpers():
    from datetime import datetime, timezone
    def presence(last_active):
        if not last_active:
            return {"label": "Offline", "online": False}
        try:
            stamp = datetime.fromisoformat(str(last_active).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            seconds = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
            if seconds <= 300:
                return {"label": "Active recently", "online": True}
            if seconds <= 86400:
                return {"label": "Active today", "online": False}
            return {"label": "Last active " + stamp.strftime("%d %b"), "online": False}
        except (ValueError, TypeError):
            return {"label": "Status unavailable", "online": False}
    return {"presence": presence}


def get_meeting_rows(user_id):
    db = get_db()
    return db.execute("""
        SELECT m.*, inv.name AS investor_name, inv.last_active AS investor_last_active,
               fin.name AS finder_name, fin.last_active AS finder_last_active,
               inv.email AS investor_email, fin.email AS finder_email
        FROM meetings m
        JOIN users inv ON inv.id = m.investor_user_id
        JOIN users fin ON fin.id = m.finder_user_id
        WHERE m.investor_user_id = ? OR m.finder_user_id = ?
        ORDER BY CASE m.status WHEN 'Requested' THEN 0 WHEN 'Accepted' THEN 1 ELSE 2 END,
                 m.proposed_at DESC, m.created_at DESC
    """, (user_id, user_id)).fetchall()

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    role = request.args.get("role", "finder")
    if role not in ("investor", "finder"):
        role = "finder"
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "finder")
        if role not in ("investor", "finder"):
            role = "finder"
        if not name or not email or len(password) < 8:
            flash("Enter your name and email. Password must contain at least 8 characters.", "danger")
            return render_template("register.html", selected_role=role)
        db = get_db()
        try:
            cur = db.execute(
                "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
                (name, email, generate_password_hash(password), role)
            )
            user_id = cur.lastrowid
            if role == "investor":
                db.execute("INSERT INTO investor_profiles (user_id) VALUES (?)", (user_id,))
            else:
                db.execute("INSERT INTO startup_profiles (user_id) VALUES (?)", (user_id,))
            db.commit()
        except sqlite3.IntegrityError:
            flash("That email already has an account. Please log in.", "warning")
            return render_template("register.html", selected_role=role)
        session.clear()
        session["user_id"], session["name"], session["role"] = user_id, name, role
        award_points(user_id, 10, 0)
        flash("Account created. Complete your profile to get started.", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html", selected_role=role)


@app.route("/login", methods=["GET", "POST"])
def login():
    selected_role = request.args.get("role", "")
    if selected_role not in ("investor", "finder"):
        selected_role = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        selected_role = request.form.get("role", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password) and (not selected_role or selected_role == user["role"]):
            session.clear()
            session["user_id"], session["name"], session["role"] = user["id"], user["name"], user["role"]
            return redirect(url_for("dashboard"))
        flash("Invalid email/password, or choose the correct account type.", "danger")
    return render_template("login.html", selected_role=selected_role)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    user_id, role = session["user_id"], session["role"]
    account = db.execute("SELECT activity_points, trust_score, last_active FROM users WHERE id = ?", (user_id,)).fetchone()
    meetings = get_meeting_rows(user_id)
    if role == "investor":
        profile = db.execute("SELECT * FROM investor_profiles WHERE user_id = ?", (user_id,)).fetchone()
        startup_count = db.execute("SELECT COUNT(*) FROM startup_profiles").fetchone()[0]
        return render_template("investor_dashboard.html", profile=profile, startup_count=startup_count,
                               account=account, meetings=meetings)
    profile = db.execute("SELECT * FROM startup_profiles WHERE user_id = ?", (user_id,)).fetchone()
    tasks = db.execute(
            "SELECT * FROM compliance_tasks WHERE user_id = ? ORDER BY CASE status WHEN 'Pending' THEN 0 WHEN 'In Progress' THEN 1 ELSE 2 END, due_date",
            (user_id,)
        ).fetchall()
    matches = get_matches(profile)
    return render_template("finder_dashboard.html", profile=profile, tasks=tasks, matches=matches,
                           account=account, meetings=meetings)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    db = get_db()
    user_id, role = session["user_id"], session["role"]
    if request.method == "POST":
        if role == "investor":
            prior = db.execute("SELECT organization, industries FROM investor_profiles WHERE user_id=?", (user_id,)).fetchone()
            was_complete = bool(prior and prior["industries"] and prior["organization"])
            organization = request.form.get("organization", "").strip()
            industries = request.form.get("industries", "").strip()
            db.execute("""UPDATE investor_profiles SET organization=?, industries=?, funding_min=?,
                       funding_max=?, preferred_stage=?, location=?, bio=? WHERE user_id=?""",
                       (organization, industries, safe_float(request.form.get("funding_min")),
                        safe_float(request.form.get("funding_max")), request.form.get("preferred_stage", "Any"),
                        request.form.get("location", "").strip(), request.form.get("bio", "").strip(), user_id))
            now_complete = bool(industries and organization)
        else:
            prior = db.execute("SELECT startup_name, industry FROM startup_profiles WHERE user_id=?", (user_id,)).fetchone()
            was_complete = bool(prior and prior["startup_name"] and prior["industry"])
            startup_name = request.form.get("startup_name", "").strip()
            industry = request.form.get("industry", "").strip()
            db.execute("""UPDATE startup_profiles SET startup_name=?, industry=?, funding_needed=?,
                       stage=?, location=?, description=? WHERE user_id=?""",
                       (startup_name, industry, safe_float(request.form.get("funding_needed")),
                        request.form.get("stage", "Idea"), request.form.get("location", "").strip(),
                        request.form.get("description", "").strip(), user_id))
            now_complete = bool(startup_name and industry)
        db.commit()
        if now_complete and not was_complete:
            award_points(user_id, 20, 5)
            flash("Profile completed: +20 activity points and +5 reputation.", "success")
        else:
            flash("Profile saved successfully.", "success")
        return redirect(url_for("dashboard"))
    if role == "investor":
        profile = db.execute("SELECT * FROM investor_profiles WHERE user_id=?", (user_id,)).fetchone()
    else:
        profile = db.execute("SELECT * FROM startup_profiles WHERE user_id=?", (user_id,)).fetchone()
    return render_template("profile.html", profile=profile, role=role)


def safe_float(value):
    try:
        number = float(value or 0)
        return max(0, number)
    except (ValueError, TypeError):
        return 0


@app.route("/investors")
@login_required
@role_required("finder")
def investors():
    db = get_db()
    profile = db.execute("SELECT * FROM startup_profiles WHERE user_id=?", (session["user_id"],)).fetchone()
    matches = get_matches(profile)
    query = request.args.get("q", "").strip().lower()
    if query:
        matches = [m for m in matches if query in (m["name"] or "").lower()
                   or query in (m["organization"] or "").lower()
                   or query in (m["industries"] or "").lower()]
    return render_template("investors.html", matches=matches, query=query)


@app.route("/startups")
@login_required
@role_required("investor")
def startups():
    db = get_db()
    rows = db.execute("""
      SELECT u.name as founder, u.last_active as founder_last_active, s.* FROM users u
      JOIN startup_profiles s ON s.user_id = u.id
      WHERE u.id != ? ORDER BY s.startup_name
    """, (session["user_id"],)).fetchall()
    query = request.args.get("q", "").strip().lower()
    if query:
        rows = [r for r in rows if query in (r["startup_name"] or "").lower()
                or query in (r["industry"] or "").lower()]
    return render_template("startups.html", startups=rows, query=query)


@app.route("/compliance/add", methods=["POST"])
@login_required
@role_required("finder")
def add_task():
    title = request.form.get("title", "").strip()
    due_date = request.form.get("due_date", "").strip()
    if title:
        db = get_db()
        db.execute("INSERT INTO compliance_tasks (user_id, title, due_date) VALUES (?, ?, ?)",
                   (session["user_id"], title, due_date))
        db.commit()
        flash("Compliance task added.", "success")
    return redirect(url_for("dashboard"))


@app.route("/compliance/<int:task_id>/status", methods=["POST"])
@login_required
@role_required("finder")
def update_task(task_id):
    status = request.form.get("status", "Pending")
    if status not in ("Pending", "In Progress", "Completed"):
        status = "Pending"
    db = get_db()
    db.execute("UPDATE compliance_tasks SET status=? WHERE id=? AND user_id=?",
               (status, task_id, session["user_id"]))
    db.commit()
    return redirect(url_for("dashboard"))


@app.route("/compliance/<int:task_id>/delete", methods=["POST"])
@login_required
@role_required("finder")
def delete_task(task_id):
    db = get_db()
    db.execute("DELETE FROM compliance_tasks WHERE id=? AND user_id=?",
               (task_id, session["user_id"]))
    db.commit()
    flash("Task deleted.", "success")
    return redirect(url_for("dashboard"))



@app.route("/patents")
def patent_library():
    q = request.args.get("q", "").strip().lower()
    category = request.args.get("category", "").strip()
    jurisdiction = request.args.get("jurisdiction", "").strip()
    doc_type = request.args.get("doc_type", "").strip()
    status = request.args.get("status", "").strip()
    year = request.args.get("year", "").strip()

    rows = PATENTS
    if q:
        rows = [p for p in rows if q in " ".join(str(p.get(k, "")) for k in
                ("id", "title", "assignee", "inventors", "category", "abstract", "ipc_cpc")).lower()]
    if category:
        rows = [p for p in rows if p["category"] == category]
    if jurisdiction:
        rows = [p for p in rows if p["jurisdiction"] == jurisdiction]
    if doc_type:
        rows = [p for p in rows if p["type_group"] == doc_type]
    if status:
        rows = [p for p in rows if status.lower() in p["status"].lower()]
    if year:
        rows = [p for p in rows if p["publication_date"].startswith(year)]
    rows = sorted(rows, key=lambda p: p["publication_date"], reverse=True)
    categories = sorted(set(p["category"] for p in PATENTS))
    jurisdictions = sorted(set(p["jurisdiction"] for p in PATENTS))
    years = sorted(set(p["publication_date"][:4] for p in PATENTS), reverse=True)
    return render_template("patents.html", patents=rows, q=q, category=category,
                           jurisdiction=jurisdiction, doc_type=doc_type, status=status,
                           year=year, categories=categories, jurisdictions=jurisdictions,
                           years=years, total=len(PATENTS))


@app.route("/patents/<patent_id>")
def patent_detail(patent_id):
    patent = next((p for p in PATENTS if p["id"].lower() == patent_id.lower()), None)
    if patent is None:
        return render_template("404.html"), 404
    return render_template("patent_detail.html", patent=patent)


@app.route("/patents/export.csv")
def patent_export():
    output = io.StringIO()
    fields = ["id", "title", "jurisdiction", "document_type", "application_number",
              "publication_date", "filing_date", "priority_date", "grant_date",
              "inventors", "assignee", "status", "ipc_cpc", "category", "abstract",
              "claim_focus", "startupverse_relevance", "family_notes", "source"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(PATENTS)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=startupverse_patent_landscape.csv"})



@app.route("/meetings/request/<int:other_user_id>", methods=["POST"])
@login_required
def request_meeting(other_user_id):
    user_id, role = session["user_id"], session["role"]
    db = get_db()
    other = db.execute("SELECT id, role FROM users WHERE id=?", (other_user_id,)).fetchone()
    expected = "finder" if role == "investor" else "investor"
    if not other or other["role"] != expected or other_user_id == user_id:
        flash("That account cannot receive a meeting request from your account type.", "warning")
        return redirect(url_for("dashboard"))
    proposed_at = request.form.get("proposed_at", "").strip()
    message = request.form.get("message", "").strip()[:1000]
    investor_id = user_id if role == "investor" else other_user_id
    finder_id = other_user_id if role == "investor" else user_id
    existing = db.execute("""SELECT id FROM meetings WHERE investor_user_id=? AND finder_user_id=?
        AND status IN ('Requested','Accepted')""", (investor_id, finder_id)).fetchone()
    if existing:
        flash("There is already an open meeting request for this pair.", "warning")
        return redirect(url_for("dashboard"))
    db.execute("""INSERT INTO meetings (investor_user_id, finder_user_id, requested_by, proposed_at, message, status)
        VALUES (?, ?, ?, ?, ?, 'Requested')""", (investor_id, finder_id, user_id, proposed_at, message))
    db.commit()
    award_points(user_id, 2, 0)
    flash("Meeting request sent: +2 activity points.", "success")
    return redirect(url_for("dashboard"))


@app.route("/meetings/<int:meeting_id>/respond", methods=["POST"])
@login_required
def respond_meeting(meeting_id):
    db = get_db()
    meeting = db.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    user_id = session["user_id"]
    if not meeting or user_id not in (meeting["investor_user_id"], meeting["finder_user_id"]):
        flash("Meeting not found.", "danger")
        return redirect(url_for("dashboard"))
    if meeting["requested_by"] == user_id:
        flash("You cannot respond to your own meeting request.", "warning")
        return redirect(url_for("dashboard"))
    action = request.form.get("action", "")
    if meeting["status"] != "Requested" or action not in ("Accepted", "Declined"):
        flash("This request can no longer be changed.", "warning")
        return redirect(url_for("dashboard"))
    db.execute("UPDATE meetings SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (action, meeting_id))
    db.commit()
    if action == "Accepted":
        award_points(user_id, 3, 2)
        flash("Meeting accepted: +3 activity points and +2 reputation.", "success")
    else:
        flash("Meeting declined. No points were deducted.", "success")
    return redirect(url_for("dashboard"))


@app.route("/meetings/<int:meeting_id>/confirm", methods=["POST"])
@login_required
def confirm_meeting(meeting_id):
    db = get_db()
    meeting = db.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    user_id = session["user_id"]
    if not meeting or user_id not in (meeting["investor_user_id"], meeting["finder_user_id"]):
        flash("Meeting not found.", "danger")
        return redirect(url_for("dashboard"))
    if meeting["status"] != "Accepted":
        flash("Only accepted meetings can be confirmed.", "warning")
        return redirect(url_for("dashboard"))
    if user_id == meeting["investor_user_id"]:
        db.execute("UPDATE meetings SET investor_confirmed=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (meeting_id,))
    else:
        db.execute("UPDATE meetings SET finder_confirmed=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (meeting_id,))
    db.commit()
    latest = db.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if latest["investor_confirmed"] and latest["finder_confirmed"]:
        db.execute("UPDATE meetings SET status='Completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (meeting_id,))
        db.commit()
        award_points(latest["investor_user_id"], 10, 5)
        award_points(latest["finder_user_id"], 10, 5)
        flash("Both participants confirmed. Each earned +10 XP and +5 reputation.", "success")
    else:
        flash("Confirmation saved. The meeting completes after both participants confirm.", "success")
    return redirect(url_for("dashboard"))


@app.route("/leaderboard")
def leaderboard():
    db = get_db()
    leaders = db.execute("""
      SELECT u.id, u.name, u.role, u.activity_points, u.trust_score, u.last_active,
             COALESCE(i.organization, s.startup_name, '') AS display_name
      FROM users u
      LEFT JOIN investor_profiles i ON i.user_id=u.id
      LEFT JOIN startup_profiles s ON s.user_id=u.id
      ORDER BY u.activity_points DESC, u.trust_score DESC, u.name ASC LIMIT 50
    """).fetchall()
    return render_template("leaderboard.html", leaders=leaders)

@app.context_processor
def inject_user():
    return {"current_user": session.get("name"), "current_role": session.get("role")}


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="127.0.0.1", port=5000)
