"""
server.py - Main Flask application entry point.

Responsibilities:
  1. On startup: check initialization state.  If setup has not been run,
     invoke setup.py interactively and exit so systemd can restart.
  2. Start the Flask server on 0.0.0.0:5000.
  3. Register routes for: login/logout, dashboard (to-dos + notes),
     family announcement board, and the admin/update panel.
  4. Run the GitHub auto-update and backup system on a background thread
     at a configurable interval.
"""

import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from functools import wraps

# Ensure project root is on the path for relative imports.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import settings
import push_service

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
    jsonify,
    send_from_directory,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_PATH = os.path.join(PROJECT_ROOT, "logs", "server.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ],
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# First-run check
# ---------------------------------------------------------------------------


def _ensure_initialized() -> None:
    """
    If the system has not been set up yet, launch setup.py interactively and
    then exit so that systemd restarts the service from a clean state.
    """
    if not settings.is_initialized():
        logger.info("System not initialized. Launching setup wizard.")
        result = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "setup.py")],
            check=False,
        )
        if result.returncode != 0:
            logger.error(
                "Setup wizard exited with code %d. Refusing to start server.",
                result.returncode,
            )
            sys.exit(1)
        # Restart via exit so systemd re-launches the service in a clean state.
        logger.info("Setup complete. Exiting so systemd can restart the service.")
        sys.exit(0)


# ---------------------------------------------------------------------------
# Flask application factory
# ---------------------------------------------------------------------------


def create_app() -> Flask:
    cfg = settings.load_config()
    app = Flask(__name__, template_folder=os.path.join(PROJECT_ROOT, "templates"))
    app.secret_key = cfg.get("secret_key", os.urandom(32).hex())
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Keep sessions alive for up to 8 hours of inactivity.
    app.config["PERMANENT_SESSION_LIFETIME"] = 8 * 3600

    _register_routes(app)
    return app


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------


def login_required(f):
    """Redirect unauthenticated requests to the login page."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


def admin_required(f):
    """Abort with 403 unless the logged-in user has the admin role."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "admin":
            abort(403)
        return f(*args, **kwargs)

    return decorated


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def _register_routes(app: Flask) -> None:

    @app.before_request
    def check_ui_mode():
        # Inject ui_mode into session if not present, but don't force it yet.
        # The frontend will show a prompt if 'ui_mode_selected' is False.
        if "ui_mode" not in session:
            ua = request.user_agent.string.lower()
            if any(m in ua for m in ["mobile", "android", "iphone", "ipad"]):
                session["ui_mode"] = "mobile"
            else:
                session["ui_mode"] = "pc"
            session["ui_mode_selected"] = False

    @app.route("/set_ui_mode/<mode>")
    def set_ui_mode(mode):
        if mode in ["pc", "mobile"]:
            session["ui_mode"] = mode
            session["ui_mode_selected"] = True
        return redirect(request.referrer or url_for("index"))

    # ------------------------------------------------------------------
    # Login / Logout
    # ------------------------------------------------------------------

    @app.route("/", methods=["GET"])
    def index():
        if "user_id" in session:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if "user_id" in session:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")

            # Guest login - no password needed.
            if username == "guest":
                guest = settings.get_user_by_username("guest")
                if guest:
                    session.permanent = True
                    session["user_id"] = guest["id"]
                    session["username"] = guest["username"]
                    session["role"] = guest["role"]
                    session["shopping_permission"] = guest.get("shopping_permission", "read")
                    logger.info("Guest login from %s.", request.remote_addr)
                    return redirect(url_for("dashboard"))
                flash("Guest account is not configured.", "danger")
                return render_template("login.html")

            user = settings.verify_user(username, password)
            if user:
                session.permanent = True
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["role"] = user["role"]
                session["shopping_permission"] = user.get("shopping_permission", "full")
                logger.info("User '%s' logged in from %s.", username, request.remote_addr)
                return redirect(url_for("dashboard"))

            flash("Invalid username or password.", "danger")
            logger.warning(
                "Failed login attempt for username '%s' from %s.",
                username,
                request.remote_addr,
            )

        users = settings.get_users()
        users = [u for u in users if u["username"] != "guest"]
        return render_template("login.html", users=users)

    @app.route("/logout")
    def logout():
        username = session.get("username", "unknown")
        session.clear()
        logger.info("User '%s' logged out.", username)
        flash("You have been logged out.", "info")
        return redirect(url_for("login"))

    # ------------------------------------------------------------------
    # Dashboard (to-dos + notes)
    # ------------------------------------------------------------------

    @app.route("/dashboard")
    @login_required
    def dashboard():
        user_id = session["user_id"]
        todos = settings.get_todos(user_id)
        notes = settings.get_notes(user_id)
        announcements = settings.get_announcements()[:3]  # Preview top 3.
        bookmarks = settings.get_bookmarks()
        events = settings.get_events()
        return render_template(
            "dashboard.html",
            todos=todos,
            notes=notes,
            announcements=announcements,
            bookmarks=bookmarks,
            events=events,
        )

    # --- To-dos ---

    @app.route("/todos/add", methods=["POST"])
    @login_required
    def todo_add():
        if session.get("role") == "guest":
            flash("Guests cannot add to-dos.", "warning")
            return redirect(url_for("dashboard"))
        content = request.form.get("content", "").strip()
        if content:
            settings.add_todo(session["user_id"], content)
        return redirect(url_for("dashboard"))

    @app.route("/todos/toggle/<int:todo_id>", methods=["POST"])
    @login_required
    def todo_toggle(todo_id: int):
        settings.toggle_todo(todo_id, session["user_id"])
        return redirect(url_for("dashboard"))

    @app.route("/todos/delete/<int:todo_id>", methods=["POST"])
    @login_required
    def todo_delete(todo_id: int):
        settings.delete_todo(todo_id, session["user_id"])
        return redirect(url_for("dashboard"))

    # --- Notes ---

    @app.route("/notes")
    @login_required
    def notes_list():
        notes = settings.get_notes(session["user_id"])
        return render_template("notes.html", notes=notes)

    @app.route("/notes/save", methods=["POST"])
    @login_required
    def note_save():
        if session.get("role") == "guest":
            flash("Guests cannot save notes.", "warning")
            return redirect(url_for("notes_list"))
        note_id_raw = request.form.get("note_id", "").strip()
        note_id = int(note_id_raw) if note_id_raw and note_id_raw.isdigit() else None
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        if not title and not content:
            flash("Note must have a title or content.", "warning")
        else:
            settings.upsert_note(session["user_id"], note_id, title, content)
        return redirect(url_for("notes_list"))

    @app.route("/notes/delete/<int:note_id>", methods=["POST"])
    @login_required
    def note_delete(note_id: int):
        settings.delete_note(note_id, session["user_id"])
        return redirect(url_for("notes_list"))

    # ------------------------------------------------------------------
    # Family Announcement Board
    # ------------------------------------------------------------------

    @app.route("/board")
    @login_required
    def board():
        announcements = settings.get_announcements()
        return render_template("board.html", announcements=announcements)

    @app.route("/announcements/<int:ann_id>")
    @login_required
    def announcement_detail(ann_id: int):
        announcement = settings.get_announcement(ann_id)
        if not announcement:
            flash("Announcement not found.", "warning")
            return redirect(url_for("board"))
        return redirect(url_for("board") + f"#announcement-{ann_id}")

    @app.route("/board/post", methods=["POST"])
    @login_required
    def board_post():
        if session.get("role") == "guest":
            flash("Guests cannot post announcements.", "warning")
            return redirect(url_for("board"))
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        pinned = bool(request.form.get("pinned"))
        if not title:
            flash("Announcement must have a title.", "warning")
        else:
            # Only admins may pin announcements.
            if pinned and session.get("role") != "admin":
                pinned = False
            announcement_id = settings.add_announcement(session["user_id"], title, body, pinned)
            push_service.send_announcement_push(announcement_id)
        return redirect(url_for("board"))

    @app.route("/api/notifications/vapid-public-key")
    @login_required
    def notification_vapid_public_key():
        return jsonify({"publicKey": push_service.get_vapid_public_key_b64()})

    @app.route("/api/notifications/subscribe", methods=["POST"])
    @login_required
    def notification_subscribe():
        payload = request.get_json(silent=True) or {}
        endpoint = (payload.get("endpoint") or "").strip()
        keys = payload.get("keys") or {}
        p256dh = (keys.get("p256dh") or "").strip()
        auth = (keys.get("auth") or "").strip()

        if not endpoint or not p256dh or not auth:
            return jsonify({"error": "Missing required push subscription fields."}), 400

        settings.save_push_subscription(session["user_id"], endpoint, p256dh, auth)
        return jsonify({"status": "ok"})

    @app.route("/board/delete/<int:ann_id>", methods=["POST"])
    @login_required
    @admin_required
    def board_delete(ann_id: int):
        settings.delete_announcement(ann_id)
        return redirect(url_for("board"))

    @app.route("/board/pin/<int:ann_id>", methods=["POST"])
    @login_required
    @admin_required
    def board_pin(ann_id: int):
        settings.toggle_pin(ann_id)
        return redirect(url_for("board"))

    # ------------------------------------------------------------------
    # Bookmarks
    # ------------------------------------------------------------------

    @app.route("/bookmarks/add", methods=["POST"])
    @login_required
    def bookmark_add():
        title = request.form.get("title", "").strip()
        url = request.form.get("url", "").strip()
        icon = request.form.get("icon", "🌐").strip()
        if title and url:
            if not url.startswith("http"):
                url = "https://" + url
            settings.add_bookmark(title, url, icon)
        else:
            flash("Bookmark must have a title and URL.", "warning")
        return redirect(url_for("dashboard"))

    @app.route("/bookmarks/delete/<int:bm_id>", methods=["POST"])
    @login_required
    @admin_required
    def bookmark_delete(bm_id: int):
        settings.delete_bookmark(bm_id)
        return redirect(url_for("dashboard"))

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @app.route("/calendar")
    @login_required
    def calendar():
        events = settings.get_all_events()
        return render_template("calendar.html", events=events)

    @app.route("/events/add", methods=["POST"])
    @login_required
    def event_add():
        if session.get("role") == "guest":
            flash("Guests cannot add events.", "warning")
            return redirect(url_for("calendar"))
        title = request.form.get("title", "").strip()
        event_date = request.form.get("event_date", "").strip()
        end_date = request.form.get("end_date", "").strip()
        color = request.form.get("color", "").strip()
        if title and event_date:
            settings.add_event(session["user_id"], title, event_date, end_date, color)
            flash("Event added successfully.", "success")
        else:
            flash("Event must have a title and start date.", "warning")
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/events/delete/<int:event_id>", methods=["POST"])
    @login_required
    def event_delete(event_id: int):
        event = settings.get_event(event_id)
        if not event:
            flash("Event not found.", "warning")
        elif session.get("role") != "admin" and session.get("user_id") != event.get("author_id"):
            flash("You can only delete events you created. Only System Admins can delete any event.", "danger")
        else:
            settings.delete_event(event_id)
            flash("Event removed.", "success")
        return redirect(request.referrer or url_for("dashboard"))

    # ------------------------------------------------------------------
    # Shopping List
    # ------------------------------------------------------------------

    @app.route("/shopping")
    @login_required
    def shopping():
        items = settings.get_shopping_items()
        return render_template("shopping.html", items=items)

    @app.route("/shopping/add", methods=["POST"])
    @login_required
    def shopping_add():
        if session.get("shopping_permission") == "read":
            flash("You do not have permission to add shopping items.", "danger")
            return redirect(url_for("shopping"))
        
        content = request.form.get("content", "").strip()
        if content:
            settings.add_shopping_item(content, session["user_id"])
        return redirect(url_for("shopping"))

    @app.route("/shopping/delete/<int:item_id>", methods=["POST"])
    @login_required
    def shopping_delete(item_id: int):
        # Admin can always delete
        if session.get("role") != "admin" and session.get("shopping_permission") in ("read", "add"):
            flash("You do not have permission to remove shopping items.", "danger")
            return redirect(url_for("shopping"))
            
        settings.delete_shopping_item(item_id)
        return redirect(url_for("shopping"))

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    @app.route("/profile/password", methods=["POST"])
    @login_required
    def change_password():
        if session.get("role") == "guest":
            flash("Guest users cannot change passwords.", "danger")
            return redirect(url_for("dashboard"))

        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if len(new_password) < 6:
            flash("Password must be at least 6 characters long.", "warning")
        elif new_password != confirm_password:
            flash("Passwords do not match.", "warning")
        else:
            settings.change_password(session["user_id"], new_password)
            flash("Password successfully updated.", "success")

        return redirect(url_for("dashboard"))

    # ------------------------------------------------------------------
    # Admin panel (update / backup status)
    # ------------------------------------------------------------------

    @app.route("/admin")
    @login_required
    @admin_required
    def admin_panel():
        backup_dir = settings._backup_dir()
        backups: list[dict] = []
        if os.path.isdir(backup_dir):
            for fname in sorted(os.listdir(backup_dir), reverse=True):
                fpath = os.path.join(backup_dir, fname)
                if os.path.isfile(fpath):
                    size_kb = os.path.getsize(fpath) // 1024
                    # Extract date from filename if it follows backup_YYYYMMDD_HHMMSS.db pattern
                    dt_str = "Unknown"
                    if "_" in fname:
                        parts = fname.split("_")
                        if len(parts) >= 2:
                            dt_raw = parts[1].split(".")[0]
                            # Simple formatting
                            if len(dt_raw) >= 8:
                                dt_str = f"{dt_raw[:4]}-{dt_raw[4:6]}-{dt_raw[6:8]}"
                    
                    backups.append({
                        "filename": fname, 
                        "size": f"{size_kb} KB",
                        "date": dt_str
                    })
        users = settings.get_users()
        cfg = settings.load_config()
        return render_template("admin.html", backups=backups[:20], config=cfg, all_users=users)

    @app.route("/admin/users/add", methods=["POST"])
    @login_required
    @admin_required
    def admin_add_user():
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "user")
        shopping_permission = request.form.get("shopping_permission", "full")

        if not username or not password:
            flash("Username and password are required.", "warning")
            return redirect(url_for("admin_panel"))

        try:
            settings.add_user(username, password, role, shopping_permission)
            flash(f"User '{username}' created successfully.", "success")
        except Exception as e:
            flash(f"Error creating user: {e}", "danger")
        return redirect(url_for("admin_panel"))

    @app.route("/admin/update", methods=["POST"])
    @login_required
    @admin_required
    def admin_update():
        """Manually trigger the update cycle from the admin panel."""
        result = run_update_cycle()
        if result["success"]:
            flash(f"Update successful: {result['message']}", "success")
        else:
            flash(f"Update failed: {result['message']}", "danger")
        return redirect(url_for("admin_panel"))

    @app.route("/admin/backup/create", methods=["POST"])
    @login_required
    @admin_required
    def admin_backup_create():
        try:
            db_path = _backup_database()
            _backup_config()
            flash(f"Backup created: {os.path.basename(db_path)}", "success")
        except Exception as e:
            flash(f"Backup failed: {e}", "danger")
        return redirect(url_for("admin_panel"))

    @app.route("/admin/backup/delete/<filename>", methods=["POST"])
    @login_required
    @admin_required
    def admin_backup_delete(filename: str):
        backup_dir = settings._backup_dir()
        fpath = os.path.join(backup_dir, filename)
        if os.path.isfile(fpath):
            os.remove(fpath)
            flash(f"Backup '{filename}' deleted.", "success")
        else:
            flash("Backup file not found.", "danger")
        return redirect(url_for("admin_panel"))

    @app.route("/admin/backups/download/<filename>")
    @login_required
    @admin_required
    def admin_backup_download(filename: str):
        backup_dir = settings._backup_dir()
        return send_from_directory(backup_dir, filename, as_attachment=True)

    @app.route("/admin/restore", methods=["POST"])
    @login_required
    @admin_required
    def admin_restore():
        backup_name = request.form.get("backup_name", "").strip()
        if not backup_name:
            flash("No backup specified.", "warning")
            return redirect(url_for("admin_panel"))
        backup_path = os.path.join(settings._backup_dir(), backup_name)
        if not os.path.isfile(backup_path):
            flash("Backup file not found.", "danger")
            return redirect(url_for("admin_panel"))
        try:
            _restore_database(backup_path)
            flash(f"Database restored from '{backup_name}'.", "success")
        except Exception as exc:
            flash(f"Restore failed: {exc}", "danger")
        return redirect(url_for("admin_panel"))

    @app.route("/admin/reset", methods=["POST"])
    @login_required
    @admin_required
    def admin_reset():
        """CRITICAL: Reset the system by deleting config.json."""
        settings.reset_config()
        session.clear()
        return redirect(url_for("dashboard"))

    @app.route("/admin/users/password", methods=["POST"])
    @login_required
    @admin_required
    def admin_reset_password():
        username = request.form.get("username")
        new_password = request.form.get("new_password", "")
        if not username or len(new_password) < 6:
            flash("Invalid input or password too short.", "warning")
            return redirect(url_for("admin_panel"))
        
        user = settings.get_user_by_username(username)
        if user:
            settings.change_password(user["id"], new_password)
            flash(f"Password updated for {username}.", "success")
        else:
            flash("User not found.", "danger")
        return redirect(url_for("admin_panel"))

    @app.route("/admin/users/delete/<int:user_id>", methods=["POST"])
    @login_required
    @admin_required
    def admin_delete_user(user_id: int):
        try:
            settings.delete_user(user_id)
            flash("User deleted.", "success")
        except ValueError as e:
            flash(str(e), "danger")
        return redirect(url_for("admin_panel"))
    
    @app.route("/sw.js")
    def service_worker():
        return send_from_directory(
        PROJECT_ROOT,
        "static/sw.js"
    )

    # ------------------------------------------------------------------
    # Health check endpoint (used by monitoring / systemd watchdog)
    # ------------------------------------------------------------------

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat()}), 200

    # ------------------------------------------------------------------
    # Error handlers
    # ------------------------------------------------------------------

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403, message="Forbidden"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, message="Page not found"), 404

    @app.errorhandler(500)
    def internal_error(e):
        return render_template("error.html", code=500, message="Internal server error"), 500


# ---------------------------------------------------------------------------
# Auto-update & backup system
# ---------------------------------------------------------------------------

_UPDATE_LOCK = threading.Lock()
_UPDATE_INTERVAL_SECONDS = 3600  # Check for updates once per hour.


def _timestamp() -> str:
    """Return a filesystem-safe timestamp string: YYYY-MM-DD_HHMMSS."""
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _backup_database() -> str:
    """
    Copy the SQLite database to the backups directory.

    Returns the path to the backup file.
    Raises an OSError if the database file cannot be read.
    """
    db_path = settings._db_path()
    backup_dir = settings._backup_dir()
    os.makedirs(backup_dir, exist_ok=True)

    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"Database not found at {db_path}. Nothing to back up.")

    dest = os.path.join(backup_dir, f"backup_{_timestamp()}.db")
    shutil.copy2(db_path, dest)
    logger.info("Database backed up to: %s", dest)
    return dest


def _backup_config() -> str:
    """
    Copy config.json to the backups directory.

    Returns the path to the backup file.
    """
    backup_dir = settings._backup_dir()
    os.makedirs(backup_dir, exist_ok=True)

    if not os.path.isfile(settings.CONFIG_PATH):
        raise FileNotFoundError("config.json not found. Nothing to back up.")

    dest = os.path.join(backup_dir, f"config_{_timestamp()}.json")
    shutil.copy2(settings.CONFIG_PATH, dest)
    logger.info("Config backed up to: %s", dest)
    return dest


def _restore_database(backup_path: str) -> None:
    """
    Overwrite the live database with a backup.

    Uses a two-step copy-then-replace to be atomic on POSIX filesystems.
    """
    db_path = settings._db_path()
    tmp_path = db_path + ".restore_tmp"
    shutil.copy2(backup_path, tmp_path)
    os.replace(tmp_path, db_path)
    logger.info("Database restored from: %s", backup_path)


def _latest_db_backup() -> str | None:
    """Return the path to the most recent database backup, or None."""
    backup_dir = settings._backup_dir()
    if not os.path.isdir(backup_dir):
        return None
    candidates = sorted(
        (
            os.path.join(backup_dir, f)
            for f in os.listdir(backup_dir)
            if f.startswith("backup_") and f.endswith(".db")
        ),
        reverse=True,
    )
    return candidates[0] if candidates else None


def _check_internet(host: str = "8.8.8.8", port: int = 53, timeout: float = 3.0) -> bool:
    """
    Return True if we can open a TCP connection to *host*:*port*.

    Uses a raw socket so we do not depend on DNS resolution of the test host.
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _git_pull() -> tuple[bool, str]:
    """
    Run 'git pull' in the project root directory.

    Returns (success: bool, message: str).
    Never raises; all exceptions are caught and returned as (False, reason).
    """
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            output = result.stdout.strip() or "Already up to date."
            return True, output
        else:
            return False, result.stderr.strip() or "git pull returned non-zero exit code."
    except subprocess.TimeoutExpired:
        return False, "git pull timed out after 60 seconds."
    except FileNotFoundError:
        return False, "git executable not found."
    except Exception as exc:
        return False, str(exc)


def run_update_cycle() -> dict:
    """
    Execute the full update / backup cycle in a thread-safe manner.

    Steps:
      1. Acquire lock to prevent concurrent cycles.
      2. Check internet connectivity.
      3. Back up database and config.json.
      4. Run git pull.
      5. If pull fails, restore from backup.
      6. If pull succeeds, send SIGTERM to self so systemd restarts.

    Returns a dict with keys: success (bool), message (str).
    """
    if not _UPDATE_LOCK.acquire(blocking=False):
        return {"success": False, "message": "Another update cycle is already running."}

    try:
        logger.info("Update cycle started.")

        # Step 2: Internet check.
        if not _check_internet():
            msg = "No internet connection. Update skipped."
            logger.warning(msg)
            return {"success": False, "message": msg}

        # Step 3: Backup.
        try:
            db_backup_path = _backup_database()
            _backup_config()
        except Exception as exc:
            msg = f"Backup failed before update: {exc}. Update aborted."
            logger.error(msg)
            return {"success": False, "message": msg}

        # Step 4: Git pull.
        pull_ok, pull_msg = _git_pull()
        logger.info("git pull result: %s | %s", pull_ok, pull_msg)

        if not pull_ok:
            # Step 5: Rollback.
            logger.error("git pull failed. Attempting rollback.")
            latest_backup = _latest_db_backup()
            if latest_backup:
                try:
                    _restore_database(latest_backup)
                    logger.info("Rollback successful from: %s", latest_backup)
                except Exception as restore_exc:
                    logger.critical("Rollback also failed: %s", restore_exc)
            return {"success": False, "message": f"git pull failed: {pull_msg}"}

        if "Already up to date" in pull_msg:
            logger.info("No new changes from remote.")
            return {"success": True, "message": "Already up to date. No restart needed."}

        # Step 6: Signal systemd to restart.
        logger.info("Update successful. Sending SIGTERM to trigger systemd restart.")
        # Give the current HTTP response a chance to be sent before we die.
        threading.Timer(1.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return {"success": True, "message": pull_msg}

    finally:
        _UPDATE_LOCK.release()


def _background_update_loop() -> None:
    """
    Run the update cycle on a background daemon thread at a fixed interval.

    The first check is delayed by 5 minutes to allow the server to warm up.
    """
    # Initial delay.
    time.sleep(300)
    while True:
        try:
            result = run_update_cycle()
            logger.info("Background update: %s", result)
        except Exception as exc:
            logger.error("Unhandled exception in background update loop: %s", exc)
        time.sleep(_UPDATE_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Application entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # --- Initialization guard ---
    _ensure_initialized()

    # --- Ensure schema is up to date (idempotent) ---
    settings.init_schema()

    # --- Start background update thread ---
    update_thread = threading.Thread(
        target=_background_update_loop,
        name="AutoUpdateThread",
        daemon=True,
    )
    update_thread.start()
    logger.info("Auto-update thread started (interval: %ds).", _UPDATE_INTERVAL_SECONDS)

    # --- Create and run Flask application ---
    app = create_app()
    port = int(os.environ.get("PORT", 80))
    logger.info("Starting Family Dashboard server on 0.0.0.0:%d", port)
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True,
        use_reloader=False,  # We handle restarts via systemd, not Flask's reloader.
    )
