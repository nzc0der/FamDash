import unittest

import settings
from server import create_app


class PushNotificationsApiTests(unittest.TestCase):
    def setUp(self):
        settings.init_schema()

    def test_subscribe_endpoint_requires_valid_payload(self):
        app = create_app()
        client = app.test_client()

        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['role'] = 'admin'
            sess['username'] = 'admin'

        response = client.post('/api/notifications/subscribe', json={})
        self.assertEqual(response.status_code, 400)

    def test_subscribe_endpoint_accepts_valid_payload(self):
        app = create_app()
        client = app.test_client()

        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['role'] = 'admin'
            sess['username'] = 'admin'

        response = client.post(
            '/api/notifications/subscribe',
            json={
                'endpoint': 'https://example.com/push/endpoint',
                'keys': {
                    'p256dh': 'test-p256dh',
                    'auth': 'test-auth',
                },
            },
        )
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
