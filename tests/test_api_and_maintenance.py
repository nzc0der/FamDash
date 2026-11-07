import unittest
import os
import shutil
import json
import sys

# Ensure root repository directory takes precedence over tests directory in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys.path[0] != PROJECT_ROOT:
    sys.path.insert(0, PROJECT_ROOT)

import settings
from server import create_app


class ApiAndMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_db_path = os.path.join(settings.PROJECT_ROOT, "data", "test_family.db")
        os.makedirs(os.path.dirname(self.temp_db_path), exist_ok=True)

        # Test config
        settings.save_config({
            "db_path": self.temp_db_path,
            "backup_dir": os.path.join(settings.PROJECT_ROOT, "backups"),
            "secret_key": "test-super-secret-key-32byteslong!",
            "maintenance_mode": False,
        })
        settings.init_schema()

        # Seed admin and regular user
        with settings._get_db() as conn:
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM todos")
            conn.execute("DELETE FROM notes")
            conn.execute("DELETE FROM announcements")
            conn.execute("DELETE FROM shopping_items")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM bookmarks")

        self.admin_id = settings.add_user("testadmin", "password123", role="admin")
        self.user_id = settings.add_user("testuser", "password123", role="user")
        self.guest_id = settings.add_user("guest", None, role="guest", shopping_permission="read")

        self.admin_token = settings.generate_api_token(self.admin_id)
        self.user_token = settings.generate_api_token(self.user_id)

        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        settings.set_maintenance_mode(False)
        if os.path.isfile(settings.CONFIG_PATH):
            os.remove(settings.CONFIG_PATH)
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except OSError:
                pass

    def test_api_login_success_and_invalid(self):
        # Successful login
        res = self.client.post("/api/auth/login", json={"username": "testuser", "password": "password123"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("token", data)
        self.assertEqual(data["user"]["username"], "testuser")

        # Invalid password
        res_bad = self.client.post("/api/auth/login", json={"username": "testuser", "password": "wrong"})
        self.assertEqual(res_bad.status_code, 401)

        # Guest login
        res_guest = self.client.post("/api/auth/login", json={"username": "guest"})
        self.assertEqual(res_guest.status_code, 200)
        self.assertEqual(res_guest.get_json()["user"]["role"], "guest")

    def test_api_auth_me(self):
        res = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {self.user_token}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["username"], "testuser")

        # Missing token
        res_unauth = self.client.get("/api/auth/me")
        self.assertEqual(res_unauth.status_code, 401)

    def test_cors_preflight_and_headers(self):
        res = self.client.options("/api/todos", headers={"Origin": "https://phone-origin.example.com"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), "https://phone-origin.example.com")
        self.assertIn("POST", res.headers.get("Access-Control-Allow-Methods", ""))

    def test_todos_crud_api(self):
        headers = {"Authorization": f"Bearer {self.user_token}"}

        # Add todo
        res_add = self.client.post("/api/todos", json={"content": "Buy groceries"}, headers=headers)
        self.assertEqual(res_add.status_code, 201)
        todo_id = res_add.get_json()["id"]

        # List todos
        res_list = self.client.get("/api/todos", headers=headers)
        self.assertEqual(res_list.status_code, 200)
        items = res_list.get_json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["content"], "Buy groceries")
        self.assertEqual(items[0]["done"], 0)

        # Toggle todo
        res_toggle = self.client.post(f"/api/todos/{todo_id}/toggle", headers=headers)
        self.assertEqual(res_toggle.status_code, 200)
        res_list_after = self.client.get("/api/todos", headers=headers)
        self.assertEqual(res_list_after.get_json()[0]["done"], 1)

        # Delete todo
        res_del = self.client.delete(f"/api/todos/{todo_id}", headers=headers)
        self.assertEqual(res_del.status_code, 200)
        self.assertEqual(len(self.client.get("/api/todos", headers=headers).get_json()), 0)

    def test_shopping_and_permissions_api(self):
        # User with full permissions adds item
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res_add = self.client.post("/api/shopping", json={"content": "Apples"}, headers=headers)
        self.assertEqual(res_add.status_code, 201)
        item_id = res_add.get_json()["id"]

        # Guest has read-only permission
        guest_token = settings.generate_api_token(self.guest_id)
        guest_headers = {"Authorization": f"Bearer {guest_token}"}
        res_guest_add = self.client.post("/api/shopping", json={"content": "Oranges"}, headers=guest_headers)
        self.assertEqual(res_guest_add.status_code, 403)

        # Admin delete item
        admin_headers = {"Authorization": f"Bearer {self.admin_token}"}
        res_del = self.client.delete(f"/api/shopping/{item_id}", headers=admin_headers)
        self.assertEqual(res_del.status_code, 200)

    def test_notes_and_announcements_api(self):
        headers = {"Authorization": f"Bearer {self.user_token}"}

        # Notes
        res_note = self.client.post("/api/notes", json={"title": "Test Title", "content": "Note body"}, headers=headers)
        self.assertEqual(res_note.status_code, 201)
        note_id = res_note.get_json()["id"]

        notes = self.client.get("/api/notes", headers=headers).get_json()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["title"], "Test Title")

        # Announcements
        res_ann = self.client.post("/api/announcements", json={"title": "Meeting", "body": "Dinner at 7"}, headers=headers)
        self.assertEqual(res_ann.status_code, 201)
        ann_id = res_ann.get_json()["id"]

        anns = self.client.get("/api/announcements", headers=headers).get_json()
        self.assertEqual(len(anns), 1)

        # Non-admin cannot delete announcement
        res_del_unauth = self.client.delete(f"/api/announcements/{ann_id}", headers=headers)
        self.assertEqual(res_del_unauth.status_code, 403)

        # Admin can delete announcement
        res_del_admin = self.client.delete(f"/api/announcements/{ann_id}", headers={"Authorization": f"Bearer {self.admin_token}"})
        self.assertEqual(res_del_admin.status_code, 200)

    def test_dashboard_aggregation_api(self):
        headers = {"Authorization": f"Bearer {self.user_token}"}
        res = self.client.get("/api/dashboard", headers=headers)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        for key in ["todos", "notes", "announcements", "shopping", "bookmarks", "events"]:
            self.assertIn(key, data)

    def test_maintenance_mode_activation_and_blocking(self):
        # 1. Initially maintenance is off
        res_status = self.client.get("/api/maintenance")
        self.assertEqual(res_status.status_code, 200)
        self.assertFalse(res_status.get_json()["maintenance"])

        # 2. Toggle via admin API
        res_toggle = self.client.post(
            "/api/admin/maintenance",
            json={"enabled": True},
            headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(res_toggle.status_code, 200)
        self.assertTrue(res_toggle.get_json()["maintenance"])
        self.assertTrue(settings.is_maintenance_mode())

        # 3. Regular user API request returns 503 during maintenance
        res_user_api = self.client.get("/api/dashboard", headers={"Authorization": f"Bearer {self.user_token}"})
        self.assertEqual(res_user_api.status_code, 503)
        self.assertTrue(res_user_api.get_json()["maintenance"])

        # 4. Admin API request succeeds during maintenance
        res_admin_api = self.client.get("/api/dashboard", headers={"Authorization": f"Bearer {self.admin_token}"})
        self.assertEqual(res_admin_api.status_code, 200)

        # 5. Non-admin web request returns 503 maintenance page
        res_web = self.client.get("/")
        self.assertEqual(res_web.status_code, 503)
        self.assertIn(b"Maintenance Mode", res_web.data)

        # 6. Admin can deactivate maintenance mode via web endpoint
        with self.client.session_transaction() as sess:
            sess["user_id"] = self.admin_id
            sess["role"] = "admin"
            sess["username"] = "testadmin"

        res_web_toggle = self.client.post("/admin/maintenance/toggle")
        self.assertEqual(res_web_toggle.status_code, 302)
        self.assertFalse(settings.is_maintenance_mode())

        # 7. Regular user API works again
        res_after = self.client.get("/api/dashboard", headers={"Authorization": f"Bearer {self.user_token}"})
        self.assertEqual(res_after.status_code, 200)

    def test_phone_client_endpoints(self):
        res_view = self.client.get("/phone")
        self.assertEqual(res_view.status_code, 200)
        self.assertIn(b"Tailscale Funnel API URL", res_view.data)

        res_dl = self.client.get("/phone/download")
        self.assertEqual(res_dl.status_code, 200)
        self.assertIn("attachment", res_dl.headers.get("Content-Disposition", ""))
        self.assertIn("family_dashboard.html", res_dl.headers.get("Content-Disposition", ""))


if __name__ == "__main__":
    unittest.main()
