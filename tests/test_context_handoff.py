"""The hook receives refreshed context before the executor can compose a reply."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bridge.store import EventStore
from bridge.worker import main


class ContextHandoffTests(unittest.TestCase):
    def test_cli_handoff_invokes_context_and_preserves_work_on_success_or_failure(self):
        for membership_ok in (True, False):
            with self.subTest(membership_ok=membership_ok), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / 'config.yaml'
                config.write_text('''deployment: synthetic
ackReaction: null
audiences:
- id: owner
  scope: owner
  userIds: [U_OWNER]
  channelAllowlist: [C_ROOM]
  agentChannels: {C_ROOM: [U_PEER]}
''')
                store = EventStore(root / 'state')
                for event_id, ts, text in [('EvOld', '100.1', 'original unresolved work'),
                                          ('EvCorrection', '101.1', 'synthetic later correction')]:
                    store.append(dict(event_id=event_id, ts=ts, text=text, channel='C_ROOM',
                        user='U_OWNER', scope='owner', audience_id='owner', thread_ts=ts,
                        reply_thread_ts=None, conversation_ts='', bot_user_id='U_BOT', transport='socket'))
                before = store.queue_path.read_bytes()
                web = Mock()
                def correction_during_refresh():
                    # An owner cancellation arrives after claim but before the
                    # executor handoff. It must be visible, never auto-consumed.
                    with sqlite3.connect('file:' + str(store.state_dir / 'worker.sqlite3') + '?mode=ro', uri=True) as db:
                        self.assertEqual(db.execute('SELECT event_id,status FROM jobs').fetchall(), [('EvOld', 'claimed')])
                    store.append(dict(event_id='EvCancel', ts='102.1', text='synthetic owner cancellation',
                        channel='C_ROOM', user='U_OWNER', scope='owner', audience_id='owner', thread_ts='102.1',
                        reply_thread_ts=None, conversation_ts='', bot_user_id='U_BOT', transport='socket'))
                    return 'U_BOT'
                web.auth_test.side_effect = correction_during_refresh
                web.private_room_matches.return_value = membership_ok
                output, errors = io.StringIO(), io.StringIO()
                args = ['worker', '--config', str(config), '--state', str(store.state_dir),
                        'next', '--with-context']
                with patch('sys.argv', args), patch.dict(os.environ, SLACK_BOT_TOKEN='synthetic'), \
                        patch('bridge.webapi.SlackWebClient', return_value=web), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    if membership_ok:
                        main()
                    else:
                        with self.assertRaises(SystemExit) as failure:
                            main()
                        self.assertEqual(failure.exception.code, 2)
                web.auth_test.assert_called_once()
                web.private_room_matches.assert_called_once_with('C_ROOM', {'U_OWNER', 'U_PEER', 'U_BOT'})
                web.post_message.assert_not_called()
                self.assertTrue(store.queue_path.read_bytes().startswith(before))
                self.assertEqual([row['event_id'] for row in store.read_queue()], ['EvOld', 'EvCorrection', 'EvCancel'])
                with sqlite3.connect('file:' + str(store.state_dir / 'worker.sqlite3') + '?mode=ro', uri=True) as db:
                    self.assertEqual(db.execute('SELECT event_id,status FROM jobs').fetchall(), [('EvOld', 'claimed')])
                if membership_ok:
                    packet = json.loads(output.getvalue())
                    self.assertEqual(packet['event']['event_id'], 'EvOld')
                    self.assertEqual(packet['event']['text'], 'original unresolved work')
                    self.assertTrue(packet['current_context']['read_only'])
                    self.assertEqual(packet['current_context']['claimed_event_id'], 'EvOld')
                    self.assertEqual([row['text'] for row in packet['current_context']['events']],
                                     ['synthetic later correction', 'synthetic owner cancellation'])
                else:
                    self.assertEqual(output.getvalue(), '')
                    self.assertEqual(json.loads(errors.getvalue())['claim_preserved'], True)
                    self.assertNotIn('synthetic later correction', errors.getvalue())
