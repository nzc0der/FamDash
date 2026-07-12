"""
setup.py - First-run initialization wizard.

This script is invoked automatically by server.py when config.json does not
exist.  It MUST NOT be imported as a library.  Running it again after
initialization has been completed is a safe no-op (it will detect the
existing config.json and exit).

Exit codes:
  0 - Setup completed successfully.
  1 - Setup aborted by user or fatal error.
"""

import getpass
import json
import logging
import os
import sys

# Ensure the project root is on sys.path so that settings.py is importable
# regardless of how this script is executed.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import settings

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

SEPARATOR = "-" * 60


def _print_header() -> None:
    print("\n" + "=" * 60)
    print("  Family Dashboard - First-Run Setup Wizard")
    print("=" * 60)
    print(
        "\nThis wizard runs ONCE.  It will create the database and\n"
        "config.json.  Do not interrupt it mid-way.\n"
    )


def _prompt(prompt_text: str, allow_empty: bool = False) -> str:
    """
    Read a non-empty string from stdin.

    Loops until the user provides a non-blank value (unless allow_empty=True).
    """
    while True:
        value = input(prompt_text).strip()
        if value or allow_empty:
            return value
        print("  [!] Value cannot be empty. Please try again.")


def _prompt_password(username: str) -> str:
    """
    Prompt for a password with confirmation using hidden input.

    Enforces a minimum length of 6 characters.
    """
    min_length = 6
    while True:
        pw1 = getpass.getpass(f"  Password for '{username}' (min {min_length} chars): ")
        if len(pw1) < min_length:
            print(f"  [!] Password too short. Minimum {min_length} characters required.")
            continue
        pw2 = getpass.getpass("  Confirm password: ")
        if pw1 != pw2:
            print("  [!] Passwords do not match. Please try again.")
            continue
        return pw1


def _prompt_int(prompt_text: str, minimum: int = 1, maximum: int = 20) -> int:
    """Prompt for an integer within [minimum, maximum]."""
    while True:
        raw = _prompt(prompt_text)
        try:
            value = int(raw)
            if minimum <= value <= maximum:
                return value
            print(f"  [!] Please enter a number between {minimum} and {maximum}.")
        except ValueError:
            print("  [!] That is not a valid integer.")


# ---------------------------------------------------------------------------
# Directory / path setup
# ---------------------------------------------------------------------------


def _resolve_paths() -> tuple[str, str]:
    """
    Ask the operator where to store the database and backups.

    Returns (db_path, backup_dir).  Both are absolute paths.
    """
    data_dir_default = os.path.join(PROJECT_ROOT, "data")
    backup_dir_default = os.path.join(PROJECT_ROOT, "backups")

    print(SEPARATOR)
    print("Storage Paths")
    print(SEPARATOR)
    print(f"Default database directory : {data_dir_default}")
    print(f"Default backup directory   : {backup_dir_default}")
    print()

    use_defaults = _prompt("Use default paths? [Y/n]: ").lower()
    if use_defaults in ("", "y", "yes"):
        db_path = os.path.join(data_dir_default, "family.db")
        backup_dir = backup_dir_default
    else:
        db_dir_raw = _prompt(
            f"Absolute path for the database DIRECTORY [{data_dir_default}]: ",
            allow_empty=True,
        )
        db_dir = db_dir_raw or data_dir_default
        db_path = os.path.join(os.path.abspath(db_dir), "family.db")

        backup_raw = _prompt(
            f"Absolute path for the backups DIRECTORY [{backup_dir_default}]: ",
            allow_empty=True,
        )
        backup_dir = os.path.abspath(backup_raw or backup_dir_default)

    # Ensure directories exist.
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    os.makedirs(backup_dir, exist_ok=True)

    return db_path, backup_dir


# ---------------------------------------------------------------------------
# Secret key
# ---------------------------------------------------------------------------


def _generate_secret_key() -> str:
    """Generate a cryptographically random Flask secret key (hex, 64 chars)."""
    return os.urandom(32).hex()


# ---------------------------------------------------------------------------
# Main setup routine
# ---------------------------------------------------------------------------


def run_setup() -> None:
    """
    Execute the full first-run setup wizard.

    After completion config.json will exist and settings.is_initialized()
    will return True.
    """
    _print_header()

    # Safety check: do not run if already initialized.
    if settings.is_initialized():
        print(
            "\n[OK] config.json already exists. Setup has already been completed.\n"
            "     If you want to reset the system, delete config.json manually\n"
            "     and restart the server.\n"
        )
        return

    # --- Storage paths ---
    db_path, backup_dir = _resolve_paths()

    # Build and save config first so that settings._db_path() resolves
    # correctly when we call init_schema() and add_user() below.
    secret_key = _generate_secret_key()
    config_data = {
        "db_path": db_path,
        "backup_dir": backup_dir,
        "secret_key": secret_key,
        "project_root": PROJECT_ROOT,
    }
    settings.save_config(config_data)
    logger.info("config.json written.")

    # --- Database schema ---
    settings.init_schema()

    # --- User accounts ---
    print()
    print(SEPARATOR)
    print("User Account Creation")
    print(SEPARATOR)
    num_users = _prompt_int("How many regular family members do you want to create? [1-20]: ")

    created_users: list[str] = []
    for i in range(1, num_users + 1):
        print(f"\n  -- User {i} of {num_users} --")
        while True:
            username = _prompt(f"  Username: ").lower().strip()
            if username == "guest":
                print("  [!] 'guest' is a reserved name. Choose a different username.")
                continue
            if any(u == username for u in created_users):
                print(f"  [!] Username '{username}' was already entered. Choose another.")
                continue
            # Check DB (handles edge case where DB already had entries from a
            # previous partial run that was interrupted before config was written).
            if settings.get_user_by_username(username) is not None:
                print(f"  [!] Username '{username}' already exists in the database.")
                continue
            break

        password = _prompt_password(username)
        
        # Ask for Role
        is_admin = _prompt(f"  Should '{username}' be a System Admin? [y/N]: ").lower() in ("y", "yes")
        role = "admin" if is_admin else "user"
        
        # Ask for Shopping Permissions if not admin (admins get 'full' by default)
        shopping_permission = "full"
        if role == "user":
            print("  Shopping List Permissions:")
            print("    1. Read and Add (Cannot delete others' items)")
            print("    2. Full Access (Can add and delete)")
            print("    3. Read Only")
            perm_choice = _prompt_int("  Select permission [1-3]: ", 1, 3)
            if perm_choice == 1:
                shopping_permission = "add"
            elif perm_choice == 2:
                shopping_permission = "full"
            else:
                shopping_permission = "read"

        try:
            settings.add_user(username, password, role=role, shopping_permission=shopping_permission)
            created_users.append(username)
            label = f" ({role}, shopping:{shopping_permission})"
            print(f"  [+] User '{username}'{label} created.")
        except Exception as exc:
            logger.error("Failed to create user '%s': %s", username, exc)
            # Clean up partial state so the wizard can be re-run.
            settings.reset_config()
            print(
                "\n[FATAL] Failed to create a user account. "
                "config.json has been removed so setup can be re-run.\n"
            )
            sys.exit(1)

    # --- Guest account ---
    try:
        settings.add_user("guest", password=None, role="guest")
        print("\n  [+] Guest account created (no password required).")
    except ValueError:
        # Guest already exists from a previous interrupted setup; that is fine.
        print("\n  [~] Guest account already exists, skipped.")
    except Exception as exc:
        logger.error("Failed to create guest account: %s", exc)
        settings.reset_config()
        print("\n[FATAL] Failed to create guest account. Resetting config.\n")
        sys.exit(1)

    # --- Summary ---
    print()
    print("=" * 60)
    print("  Setup Complete!")
    print("=" * 60)
    print(f"\n  Database : {db_path}")
    print(f"  Backups  : {backup_dir}")
    print(f"  Users    : {', '.join(created_users + ['guest'])}")
    print(
        "\n  The first user listed is the admin.\n"
        "  Start the server with:\n\n"
        "    python server.py\n\n"
        "  or via systemd:\n\n"
        "    sudo systemctl start family_dashboard\n"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        run_setup()
    except KeyboardInterrupt:
        print("\n\n[!] Setup interrupted by user. config.json has NOT been fully written.")
        # Attempt to clean up partial config so next run starts fresh.
        if settings.is_initialized():
            settings.reset_config()
        sys.exit(1)
