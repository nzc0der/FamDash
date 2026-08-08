import unittest
from unittest.mock import patch
import json
import settings
from server import create_app


class AIAssistantApiTests(unittest.TestCase):
    def setUp(self):
        settings.init_schema()
        # Ensure user with id=1 exists to satisfy foreign key constraint
        try:
            settings.add_user('admin', 'password', 'admin')
        except ValueError:
            pass

    def test_ai_ask_requires_login(self):
        app = create_app()
        client = app.test_client()
        # No session setup
        response = client.post('/api/ai/ask', json={'question': "What's on today?"})
        # login_required redirects to login, so response code is 302
        self.assertEqual(response.status_code, 302)

    def test_ai_ask_requires_question(self):
        app = create_app()
        client = app.test_client()

        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['role'] = 'admin'
            sess['username'] = 'admin'

        response = client.post('/api/ai/ask', json={})
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertEqual(data['error'], "Question is required.")

    @patch('server.get_ollama_status')
    def test_ai_ask_ollama_unavailable(self, mock_status):
        mock_status.return_value = (False, "Ollama is not running locally. Please make sure Ollama is started on the server.")

        app = create_app()
        client = app.test_client()

        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['role'] = 'admin'
            sess['username'] = 'admin'

        response = client.post('/api/ai/ask', json={'question': "What's on today?"})
        self.assertEqual(response.status_code, 503)
        data = json.loads(response.data)
        self.assertIn("Ollama is not running locally", data['error'])

    @patch('server.get_ollama_status')
    @patch('server.ask_ollama')
    def test_ai_ask_success(self, mock_ask, mock_status):
        mock_status.return_value = (True, "")
        mock_ask.return_value = "You have soccer today at 4 PM."

        app = create_app()
        client = app.test_client()

        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['role'] = 'admin'
            sess['username'] = 'admin'

        response = client.post('/api/ai/ask', json={'question': "What's on today?"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data['answer'], "You have soccer today at 4 PM.")


if __name__ == '__main__':
    unittest.main()
