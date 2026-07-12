# Family Dashboard - Operations Guide

A local Flask web application for Raspberry Pi 400 providing per-user
to-do lists, shared notes, a family announcement board, and an automatic
GitHub-based update system with pre-update database backups.

---

## Table of Contents

1. [System Requirements](#1-system-requirements)
2. [Installation](#2-installation)
3. [First-Run Setup](#3-first-run-setup)
4. [Configuring the systemd Service](#4-configuring-the-systemd-service)
5. [Accessing the Dashboard](#5-accessing-the-dashboard)
6. [Auto-Update and Backup System](#6-auto-update-and-backup-system)
7. [Manual Backup and Restore](#7-manual-backup-and-restore)
8. [Safe Manual Reset](#8-safe-manual-reset)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. System Requirements

| Requirement         | Version / Notes                               |
|---------------------|-----------------------------------------------|
| Hardware            | Raspberry Pi 400 (or any Raspberry Pi model)  |
| Operating System    | Raspberry Pi OS (Debian Bookworm / Bullseye)  |
| Python              | 3.11 or later                                 |
| Git                 | Any recent version (for auto-update)          |
| Network             | Connected to local Wi-Fi or Ethernet          |

**No OS modifications or additional hardware is required.**

Install system dependencies (if not already present):

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

---

## 2. Installation

### 2a. Clone the repository

```bash
cd /home/pi
git clone https://github.com/nzc0der/FamilySystem.git
cd FamilySystem
```

### 2b. Create a Python virtual environment

```bash
python3 -m venv venv
```

### 2c. Install Python dependencies

```bash
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

> **Note:** `bcrypt` requires a C compiler on some platforms. If installation
> fails, run `sudo apt install -y build-essential libffi-dev` first.

---

## 3. First-Run Setup

The setup wizard runs **exactly once** — when `config.json` does not yet exist.

Run it manually before enabling the service:

```bash
cd /home/pi/FamilySystem
venv/bin/python server.py
```

The wizard will:

1. Ask where to store the SQLite database and backup files (or use sensible
   defaults inside the project directory).
2. Prompt for the number of family members and their usernames and passwords.
   Passwords are entered via hidden input (no echo) and must be confirmed.
   **The first user created becomes the admin.**
3. Automatically create a `guest` account with no password for read-only
   browsing.
4. Write `config.json` (contains paths and the Flask secret key).
5. Create the SQLite database and all required tables.

After the wizard completes the server exits cleanly so the systemd service
can be started fresh.

> **Important:** `config.json` and the `data/` directory are excluded from
> git via `.gitignore`. They will never be committed or overwritten by a
> `git pull`.

---

## 4. Configuring the systemd Service

### 4a. Copy the service file

```bash
sudo cp /home/pi/FamilySystem/family_dashboard.service \
        /etc/systemd/system/family_dashboard.service
```

### 4b. Edit the service file if needed

Open it to verify paths match your setup:

```bash
sudo nano /etc/systemd/system/family_dashboard.service
```

Key fields to check:

| Field              | Default value                              |
|--------------------|--------------------------------------------|
| `User`             | `pi`                                       |
| `WorkingDirectory` | `/home/pi/FamilySystem`                   |
| `ExecStart`        | `/home/pi/FamilySystem/venv/bin/python /home/pi/FamilySystem/server.py` |

If your username is not `pi`, update `User`, `WorkingDirectory`, and
`ExecStart` accordingly.

### 4c. Enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable family_dashboard
sudo systemctl start family_dashboard
```

### 4d. Verify it is running

```bash
sudo systemctl status family_dashboard
```

### 4e. View live logs

```bash
journalctl -u family_dashboard -f
```

---

## 5. Accessing the Dashboard

On any device connected to the **same local Wi-Fi network** as the Pi:

1. Find the Pi's local IP address:
   ```bash
   hostname -I
   ```
   It will look something like `192.168.1.42`.

2. Open a browser and navigate to:
   ```
   http://192.168.1.42:8000
   ```
   Replace `192.168.1.42` with your Pi's actual address.

3. Log in with any of the usernames and passwords created during setup,
   or click **Continue as Guest** for read-only access.

> **Tip:** Assign a static DHCP lease to your Pi in your router settings
> so its IP address never changes. Then bookmark the URL on all family devices.

---

## 6. Auto-Update and Backup System

### How it works

The auto-update system runs on a background daemon thread and checks for
updates **once per hour** automatically. It can also be triggered manually
from the **Admin** page.

Each update cycle follows these steps:

```
Step 1: Connectivity check
         Is 8.8.8.8:53 reachable via TCP?
         No  -->  Log warning and abort (no changes made to system)
         Yes -->  Continue

Step 2: Backup (before any code change)
         Copy data/family.db  -->  backups/backup_YYYY-MM-DD_HHMMSS.db
         Copy config.json     -->  backups/config_YYYY-MM-DD_HHMMSS.json

Step 3: git pull
         Run `git pull` in the project root
         Success + new commits  -->  Step 4
         Success + already up to date  -->  Stop (no restart needed)
         Failure                -->  Rollback (Step 5)

Step 4: Restart
         Send SIGTERM to the current process
         systemd sees the non-zero exit and restarts the service
         The new process loads the updated code from disk

Step 5: Rollback (only on git pull failure)
         Restore the most recent backup_*.db file over data/family.db
         Log the error
         Return failure status (server continues running unchanged)
```

### What is safe from git pull

The following are **never touched** by `git pull` because they are listed
in `.gitignore` and therefore untracked by git:

- `config.json`
- `data/family.db` (and WAL sidecar files)
- `backups/`
- `logs/`

### Database schema safety

All `CREATE TABLE` statements use `CREATE TABLE IF NOT EXISTS`. The update
flow **never** executes `DROP TABLE`. Schema additions in new code versions
are always backward compatible.

---

## 7. Manual Backup and Restore

### Create a manual backup

```bash
cd /home/pi/FamilySystem
cp data/family.db backups/manual_backup_$(date +%Y-%m-%d_%H%M%S).db
cp config.json    backups/manual_config_$(date +%Y-%m-%d_%H%M%S).json
```

### Restore from a backup via the Admin panel

1. Log in as the admin user.
2. Navigate to **Admin** in the top navigation bar.
3. In the **Recent Backups** list, find the `.db` backup file you want.
4. Click **Restore** next to it and confirm the dialog.

The running server replaces the live database file atomically using a
temporary file and `os.replace()` so the restore is crash-safe.

### Restore manually from the command line

```bash
sudo systemctl stop family_dashboard
cp backups/backup_YYYY-MM-DD_HHMMSS.db data/family.db
sudo systemctl start family_dashboard
```

---

## 8. Safe Manual Reset

> **Warning:** This erases all user accounts, to-dos, notes, and
> announcements. Back up first.

```bash
# 1. Stop the service
sudo systemctl stop family_dashboard

# 2. Back up data
cp data/family.db backups/pre_reset_$(date +%Y-%m-%d_%H%M%S).db
cp config.json    backups/pre_reset_config_$(date +%Y-%m-%d_%H%M%S).json

# 3. Remove config and database
rm config.json
rm data/family.db

# 4. Restart - the server will detect the missing config.json
#    and launch the setup wizard automatically
sudo systemctl start family_dashboard

# 5. Run the setup wizard (follow prompts)
journalctl -u family_dashboard -f
```

---

## 9. Troubleshooting

### Service fails to start

```bash
sudo systemctl status family_dashboard
journalctl -u family_dashboard -n 50
```

Common causes:

| Symptom                          | Fix                                                       |
|----------------------------------|-----------------------------------------------------------|
| `ModuleNotFoundError`            | Run `venv/bin/pip install -r requirements.txt` again      |
| `Permission denied` on data/     | Run `chown -R pi:pi /home/pi/FamilySystem`                |
| `Address already in use :8000`   | Another process is on port 8000; kill it or change port   |
| `config.json: malformed JSON`    | Restore from the latest `config_*.json` backup            |

### Forgot admin password

```bash
# 1. Stop the service
sudo systemctl stop family_dashboard

# 2. Open a Python shell in the project venv
cd /home/pi/FamilySystem
venv/bin/python - <<'EOF'
import settings, bcrypt
new_pw = input("New admin password: ")
hashed = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt(12)).decode()
with settings._get_db() as conn:
    conn.execute("UPDATE users SET password_hash=? WHERE role='admin'", (hashed,))
print("Password updated.")
EOF

# 3. Restart
sudo systemctl start family_dashboard
```

### Check the Pi's local IP at any time

```bash
hostname -I
# or
ip -4 addr show wlan0
```

### Force an immediate update check

Log in as admin, go to **Admin**, and click **Run Update Now**.
