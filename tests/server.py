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
    g,
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


def api_auth_required(f):
    """
    Authenticate API requests via Session or Bearer JWT token.
    Populates g.current_user and ensures user_id and role are available.
    """

    @wraps(f)
    def decorated(*args, **kwargs):
        # 1. Check Bearer token in Authorization header
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            user = settings.verify_api_token(token)
            if user:
                g.current_user = user
                return f(*args, **kwargs)
            return jsonify({"error": "Invalid or expired token"}), 401

        # 2. Check Flask session
        if "user_id" in session:
            user = settings.get_user_by_id(session["user_id"])
            if user:
                g.current_user = user
                return f(*args, **kwargs)

        return jsonify({"error": "Authentication required"}), 401

    return decorated


def api_admin_required(f):
    """Require admin role for API endpoints."""

    @wraps(f)
    def decorated(*args, **kwargs):
        user = getattr(g, "current_user", None)
        if not user or user.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
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
    def handle_cors_preflight():
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            res = app.make_default_options_response()
            origin = request.headers.get("Origin") or "*"
            res.headers["Access-Control-Allow-Origin"] = origin
            res.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            res.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
            res.headers["Access-Control-Allow-Credentials"] = "true"
            return res

    @app.after_request
    def apply_cors_headers(response):
        if request.path.startswith("/api/"):
            origin = request.headers.get("Origin") or "*"
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    @app.before_request
    def check_maintenance_mode():
        if not settings.is_maintenance_mode():
            return None

        path = request.path
        if path.startswith("/static/") or path in ("/sw.js", "/healthz"):
            return None

        if path.startswith("/api/"):
            if path in ("/api/maintenance", "/api/auth/login", "/api/auth/logout"):
                return None

            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                user = settings.verify_api_token(auth_header[7:].strip())
                if user and user.get("role") == "admin":
                    return None

            if session.get("role") == "admin":
                return None

            return jsonify({
                "maintenance": True,
                "message": "System is currently in maintenance mode."
            }), 503

        if path in ("/login", "/logout"):
            return None

        if session.get("role") == "admin":
            return None

        return render_template("maintenance.html"), 503

    @app.route("/api/maintenance", methods=["GET"])
    def api_maintenance_status():
        return jsonify({
            "maintenance": settings.is_maintenance_mode()
        })

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
            flash("Announcement posted successfully.", "success")
            # Send push in a background thread so the HTTP response is not delayed.
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

        sub_id = settings.save_push_subscription(session["user_id"], endpoint, p256dh, auth)
        logger.info(
            "Push subscription saved (id=%s) for user %s.",
            sub_id,
            session.get("username"),
        )
        return jsonify({"status": "ok", "id": sub_id})

    @app.route("/api/notifications/unsubscribe", methods=["POST"])
    @login_required
    def notification_unsubscribe():
        """Remove a push subscription by endpoint URL."""
        payload = request.get_json(silent=True) or {}
        endpoint = (payload.get("endpoint") or "").strip()
        if not endpoint:
            return jsonify({"error": "endpoint is required"}), 400
        # Look up the subscription by endpoint.
        with settings._get_db() as conn:
            row = conn.execute(
                "SELECT id FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
                (endpoint, session["user_id"]),
            ).fetchone()
        if row:
            settings.delete_push_subscription(row["id"])
            logger.info(
                "Push subscription %s removed for user %s.",
                row["id"],
                session.get("username"),
            )
        return jsonify({"status": "ok"})

    # ------------------------------------------------------------------
    # REST API endpoints (Tailscale Funnel / Mobile / Phone Client)
    # ------------------------------------------------------------------

    @app.route("/api/auth/login", methods=["POST"])
    def api_login():
        payload = request.get_json(silent=True) or request.form or {}
        username = (payload.get("username") or "").strip().lower()
        password = payload.get("password", "")

        if username == "guest":
            guest = settings.get_user_by_username("guest")
            if guest:
                token = settings.generate_api_token(guest["id"])
                user_info = {
                    "id": guest["id"],
                    "username": guest["username"],
                    "role": guest["role"],
                    "shopping_permission": guest.get("shopping_permission", "read"),
                }
                return jsonify({"token": token, "user": user_info})
            return jsonify({"error": "Guest account not configured"}), 400

        user = settings.verify_user(username, password)
        if user:
            token = settings.generate_api_token(user["id"])
            user_info = {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "shopping_permission": user.get("shopping_permission", "full"),
            }
            return jsonify({"token": token, "user": user_info})

        return jsonify({"error": "Invalid username or password"}), 401

    @app.route("/api/auth/me", methods=["GET"])
    @api_auth_required
    def api_me():
        user = g.current_user
        return jsonify({
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "shopping_permission": user.get("shopping_permission", "full"),
        })

    @app.route("/api/auth/logout", methods=["POST"])
    def api_logout():
        session.clear()
        return jsonify({"status": "ok", "message": "Logged out"})

    @app.route("/api/dashboard", methods=["GET"])
    @api_auth_required
    def api_dashboard():
        user_id = g.current_user["id"]
        todos = settings.get_todos(user_id)
        notes = settings.get_notes(user_id)
        announcements = settings.get_announcements()
        bookmarks = settings.get_bookmarks()
        events = settings.get_events()
        shopping = settings.get_shopping_items()
        return jsonify({
            "todos": todos,
            "notes": notes,
            "announcements": announcements,
            "bookmarks": bookmarks,
            "events": events,
            "shopping": shopping,
        })

    # --- API: To-dos ---
    @app.route("/api/todos", methods=["GET"])
    @api_auth_required
    def api_todos_list():
        todos = settings.get_todos(g.current_user["id"])
        return jsonify(todos)

    @app.route("/api/todos", methods=["POST"])
    @api_auth_required
    def api_todo_add():
        if g.current_user.get("role") == "guest":
            return jsonify({"error": "Guests cannot add to-dos"}), 403
        payload = request.get_json(silent=True) or request.form or {}
        content = (payload.get("content") or "").strip()
        if not content:
            return jsonify({"error": "Content cannot be empty"}), 400
        todo_id = settings.add_todo(g.current_user["id"], content)
        return jsonify({"status": "ok", "id": todo_id}), 201

    @app.route("/api/todos/<int:todo_id>/toggle", methods=["POST"])
    @api_auth_required
    def api_todo_toggle(todo_id: int):
        settings.toggle_todo(todo_id, g.current_user["id"])
        return jsonify({"status": "ok"})

    @app.route("/api/todos/<int:todo_id>", methods=["DELETE"])
    @api_auth_required
    def api_todo_delete(todo_id: int):
        settings.delete_todo(todo_id, g.current_user["id"])
        return jsonify({"status": "ok"})

    # --- API: Notes ---
    @app.route("/api/notes", methods=["GET"])
    @api_auth_required
    def api_notes_list():
        notes = settings.get_notes(g.current_user["id"])
        return jsonify(notes)

    @app.route("/api/notes", methods=["POST"])
    @api_auth_required
    def api_note_save():
        if g.current_user.get("role") == "guest":
            return jsonify({"error": "Guests cannot save notes"}), 403
        payload = request.get_json(silent=True) or request.form or {}
        note_id = payload.get("id") or payload.get("note_id")
        note_id = int(note_id) if note_id and str(note_id).isdigit() else None
        title = (payload.get("title") or "").strip()
        content = (payload.get("content") or "").strip()

        if not title and not content:
            return jsonify({"error": "Title or content required"}), 400

        saved_id = settings.upsert_note(g.current_user["id"], note_id, title, content)
        return jsonify({"status": "ok", "id": saved_id}), 200 if note_id else 201

    @app.route("/api/notes/<int:note_id>", methods=["DELETE"])
    @api_auth_required
    def api_note_delete(note_id: int):
        settings.delete_note(note_id, g.current_user["id"])
        return jsonify({"status": "ok"})

    # --- API: Announcements ---
    @app.route("/api/announcements", methods=["GET"])
    @api_auth_required
    def api_announcements_list():
        announcements = settings.get_announcements()
        return jsonify(announcements)

    @app.route("/api/announcements", methods=["POST"])
    @api_auth_required
    def api_announcement_add():
        if g.current_user.get("role") == "guest":
            return jsonify({"error": "Guests cannot post announcements"}), 403
        payload = request.get_json(silent=True) or request.form or {}
        title = (payload.get("title") or "").strip()
        body = (payload.get("body") or "").strip()
        pinned = bool(payload.get("pinned", False))

        if not title:
            return jsonify({"error": "Title is required"}), 400

        if pinned and g.current_user.get("role") != "admin":
            pinned = False

        ann_id = settings.add_announcement(g.current_user["id"], title, body, pinned)
        try:
            push_service.send_announcement_push(ann_id)
        except Exception as exc:
            logger.warning("Push send failed: %s", exc)
        return jsonify({"status": "ok", "id": ann_id}), 201

    @app.route("/api/announcements/<int:ann_id>", methods=["DELETE"])
    @api_auth_required
    @api_admin_required
    def api_announcement_delete(ann_id: int):
        settings.delete_announcement(ann_id)
        return jsonify({"status": "ok"})

    @app.route("/api/announcements/<int:ann_id>/pin", methods=["POST"])
    @api_auth_required
    @api_admin_required
    def api_announcement_pin(ann_id: int):
        settings.toggle_pin(ann_id)
        return jsonify({"status": "ok"})

    # --- API: Shopping ---
    @app.route("/api/shopping", methods=["GET"])
    @api_auth_required
    def api_shopping_list():
        items = settings.get_shopping_items()
        return jsonify(items)

    @app.route("/api/shopping", methods=["POST"])
    @api_auth_required
    def api_shopping_add():
        if g.current_user.get("shopping_permission") == "read":
            return jsonify({"error": "Permission denied"}), 403
        payload = request.get_json(silent=True) or request.form or {}
        content = (payload.get("content") or payload.get("item_name") or "").strip()
        if not content:
            return jsonify({"error": "Item content is required"}), 400
        item_id = settings.add_shopping_item(content, g.current_user["id"])
        return jsonify({"status": "ok", "id": item_id}), 201

    @app.route("/api/shopping/<int:item_id>", methods=["DELETE"])
    @api_auth_required
    def api_shopping_delete(item_id: int):
        perm = g.current_user.get("shopping_permission")
        if g.current_user.get("role") != "admin" and perm in ("read", "add"):
            return jsonify({"error": "Permission denied"}), 403
        settings.delete_shopping_item(item_id)
        return jsonify({"status": "ok"})

    # --- API: Events ---
    @app.route("/api/events", methods=["GET"])
    @api_auth_required
    def api_events_list():
        events = settings.get_all_events()
        return jsonify(events)

    @app.route("/api/events", methods=["POST"])
    @api_auth_required
    def api_event_add():
        if g.current_user.get("role") == "guest":
            return jsonify({"error": "Guests cannot add events"}), 403
        payload = request.get_json(silent=True) or request.form or {}
        title = (payload.get("title") or "").strip()
        event_date = (payload.get("event_date") or "").strip()
        end_date = (payload.get("end_date") or "").strip() or None
        color = (payload.get("color") or "").strip() or None

        if not title or not event_date:
            return jsonify({"error": "Title and start date are required"}), 400

        event_id = settings.add_event(g.current_user["id"], title, event_date, end_date, color)
        return jsonify({"status": "ok", "id": event_id}), 201

    @app.route("/api/events/<int:event_id>", methods=["DELETE"])
    @api_auth_required
    def api_event_delete(event_id: int):
        event = settings.get_event(event_id)
        if not event:
            return jsonify({"error": "Event not found"}), 404
        if g.current_user.get("role") != "admin" and g.current_user.get("id") != event.get("author_id"):
            return jsonify({"error": "Permission denied"}), 403
        settings.delete_event(event_id)
        return jsonify({"status": "ok"})

    # --- API: Bookmarks ---
    @app.route("/api/bookmarks", methods=["GET"])
    @api_auth_required
    def api_bookmarks_list():
        bookmarks = settings.get_bookmarks()
        return jsonify(bookmarks)

    @app.route("/api/bookmarks", methods=["POST"])
    @api_auth_required
    def api_bookmark_add():
        payload = request.get_json(silent=True) or request.form or {}
        title = (payload.get("title") or "").strip()
        url = (payload.get("url") or "").strip()
        icon = (payload.get("icon") or "🌐").strip()

        if not title or not url:
            return jsonify({"error": "Title and URL are required"}), 400
        if not url.startswith("http"):
            url = "https://" + url

        bm_id = settings.add_bookmark(title, url, icon)
        return jsonify({"status": "ok", "id": bm_id}), 201

    @app.route("/api/bookmarks/<int:bm_id>", methods=["DELETE"])
    @api_auth_required
    @api_admin_required
    def api_bookmark_delete(bm_id: int):
        settings.delete_bookmark(bm_id)
        return jsonify({"status": "ok"})

    @app.route("/api/admin/maintenance", methods=["POST"])
    @api_auth_required
    @api_admin_required
    def api_admin_maintenance_toggle():
        payload = request.get_json(silent=True) or request.form or {}
        if "enabled" in payload:
            enabled = bool(payload.get("enabled"))
        else:
            enabled = not settings.is_maintenance_mode()
        settings.set_maintenance_mode(enabled)
        return jsonify({"status": "ok", "maintenance": enabled})

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

    @app.route("/admin/maintenance/toggle", methods=["POST"])
    @login_required
    @admin_required
    def admin_maintenance_toggle():
        current = settings.is_maintenance_mode()
        new_state = not current
        settings.set_maintenance_mode(new_state)
        state_str = "activated" if new_state else "deactivated"
        flash(f"Maintenance mode {state_str}.", "success" if not new_state else "warning")
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

    @app.route("/phone", methods=["GET"])
    def phone_client():
        """Serve the standalone phone client directly."""
        phone_dir = os.path.join(PROJECT_ROOT, "phone")
        return send_from_directory(phone_dir, "index.html")

    @app.route("/phone/download", methods=["GET"])
    def phone_download():
        """Allow downloading the index.html client file to save locally on a phone."""
        phone_dir = os.path.join(PROJECT_ROOT, "phone")
        return send_from_directory(
            phone_dir,
            "index.html",
            as_attachment=True,
            download_name="family_dashboard.html"
        )
    
    @app.route("/sw.js")
    def service_worker():
        """Serve the service worker from the root scope with no-cache headers.

        Service workers must be served from the root of the origin to control
        the entire site.  The no-cache headers ensure the browser always
        fetches the latest version.
        """
        response = send_from_directory(
            os.path.join(PROJECT_ROOT, "static"),
            "sw.js",
            mimetype="application/javascript",
        )
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

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
