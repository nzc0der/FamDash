#!/usr/bin/env python3
"""
reset_system.py - Utility to wipe all live user data while preserving backups.

Wipes:
  - Live SQLite database (users, notes, todos, board, etc.)
  - Server logs

Preserves:
  - backups/ directory
  - config.json (system settings & keys)
"""

import os
import sys
import shutil

# Ensure project root is on the path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

import settings

def main():
    print("=== FAMILY DASHBOARD SYSTEM RESET ===")
    print("This will PERMANENTLY DELETE all users, notes, to-dos, and board posts.")
    print("System configuration (config.json) WILL BE DELETED. Backups will NOT be affected.")
    
    confirm = input("\nType 'RESET' to confirm deletion: ").strip()
    if confirm != "RESET":
        print("Reset cancelled.")
        return

    # 1. Reset config (so setup wizard runs next time)
    print("Removing config.json...")
    settings.reset_config()

    # 1. Delete the database
    db_path = settings._db_path()
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
            print(f"Deleted database: {db_path}")
        except Exception as e:
            print(f"Error deleting database: {e}")
    else:
        print("Database file already gone.")

    # 2. Clear logs
    log_dir = os.path.join(PROJECT_ROOT, "logs")
    if os.path.exists(log_dir):
        try:
            shutil.rmtree(log_dir)
            os.makedirs(log_dir)
            print("Cleared all logs.")
        except Exception as e:
            print(f"Error clearing logs: {e}")

    print("\nSystem has been wiped successfully.")
    print("Next time you start the server, you will be prompted to run the setup wizard to create a new admin.")

if __name__ == "__main__":
    main()
