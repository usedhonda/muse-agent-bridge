"""Offline startup, disconnect and non-secret intake diagnosis contracts."""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
import websocket

from bridge.config import Audience, BridgeConfig
from bridge.listener import Listener, authenticate, serve
from bridge.store import EventStore
from bridge.transport import SocketModeTransport
from bridge.webapi import SlackWebClient


class ListenerRecoveryTests(unittest.TestCase):
    def test_startup_network_failure_retries_before_authentication(self):
        web, sleep = Mock(), Mock()
        web.auth_test.side_effect = [ConnectionError('private detail'), OSError(), 'U_BOT']
        self.assertEqual(authenticate(web, sleep=sleep), 'U_BOT')
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_transient_slack_auth_response_retries_but_invalid_credentials_fail(self):
        web = SlackWebClient('synthetic')
        web._post = Mock(side_effect=[dict(ok=False, error='internal_error'),
                                     dict(ok=True, bot_id='B_TEST', user_id='U_BOT')])
        self.assertEqual(authenticate(web, sleep=Mock()), 'U_BOT')
        web._post = Mock(return_value=dict(ok=False, error='invalid_auth'))
        with self.assertRaisesRegex(RuntimeError, 'did not verify'):
            authenticate(web, sleep=Mock())
        web._post.assert_called_once()

    def test_invalid_identity_does_not_retry_and_exhaustion_is_sanitized(self):
        web, sleep = Mock(), Mock()
        web.auth_test.side_effect = RuntimeError('auth.test did not verify a bot identity')
        with self.assertRaises(RuntimeError):
            authenticate(web, sleep=sleep)
        sleep.assert_not_called()
        web.auth_test.side_effect = OSError('private credential detail')
        with self.assertRaisesRegex(RuntimeError, '^bot authentication unavailable after startup retries$'):
            authenticate(web, sleep=sleep, attempts=2)

    def test_websocket_failure_reconnects_and_closes_transport(self):
        transport = SocketModeTransport('synthetic')
        transport._ws = Mock()
        transport._ws.recv.side_effect = websocket.WebSocketConnectionClosedException('private url')
        with self.assertRaisesRegex(ConnectionError, '^Socket Mode connection interrupted$'):
            transport.receive()
        factory = Mock(return_value=transport)
        transport.connect = Mock()
        transport.close = Mock()
        listener = Mock()
        listener.run.side_effect = ConnectionError('private url')
        # End the otherwise resident loop only after its reconnect wait.
        with self.assertRaises(InterruptedError):
            serve(listener, 'synthetic', factory, sleep=Mock(side_effect=InterruptedError))
        transport.close.assert_called_once()

    def test_websocket_connect_send_and_close_exceptions_are_normalized(self):
        transport = SocketModeTransport('synthetic')
        error = websocket.WebSocketException('private connection detail')
        with patch('bridge.transport.open_connection_url', return_value='wss://invalid.example'), \
                patch('websocket.create_connection', side_effect=error):
            with self.assertRaises(ConnectionError):
                transport.connect()
        transport._ws = Mock()
        transport._ws.send.side_effect = error
        transport._ws.close.side_effect = error
        with self.assertRaises(ConnectionError):
            transport.send_ack('synthetic')
        with self.assertRaises(ConnectionError):
            transport.close()
        self.assertIsNone(transport._ws)

    def test_drop_diagnostics_and_durable_intake_do_not_retain_private_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            reports = []
            store = EventStore(directory)
            cfg = BridgeConfig('test', None, [Audience('owner', 'owner', {'U_OWNER'}, {'C_TEST'}, True)])
            listener = Listener(cfg, store, Mock(), 'U_BOT', diagnostic=reports.append)
            event = dict(type='message', user='U_OWNER', channel='C_TEST', ts='100.1',
                         text='private body and credential')
            envelope = dict(type='events_api', envelope_id='e1',
                            payload=dict(event_id='EvTest', event=event))
            listener.handle_envelope(envelope, Mock())
            self.assertEqual(reports[0]['reason'], 'no-mention')
            self.assertEqual(reports[0]['event_id'], 'EvTest')
            self.assertEqual(store.read_queue(), [])
            self.assertEqual(list(store.state_dir.iterdir()), [])
            event.update(channel='D_TEST')
            listener.handle_envelope(envelope, Mock())
            listener.handle_envelope(envelope, Mock())
            self.assertEqual([row['stage'] for row in reports], ['dropped', 'queued', 'duplicate'])
            serialized = json.dumps(reports)
            for private in ('private body', 'credential', 'U_OWNER', 'C_TEST', 'D_TEST'):
                self.assertNotIn(private, serialized)
            envelope['payload']['event_id'] = 'untrusted raw secret'
            event.update(user='U_UNKNOWN', ts='untrusted raw secret')
            listener.handle_envelope(envelope, Mock())
            self.assertNotIn('event_id', reports[-1])
            self.assertNotIn('message_ts', reports[-1])
