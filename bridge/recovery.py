"""Metadata-only rescue planning. No apply, claim, token or send capability."""
import argparse
from contextlib import nullcontext
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3


def plan(state, event_id):
    state = Path(state)
    lock_path = state / 'append.lock'
    # Do not create state, lock files or SQLite databases in a dry-run.
    with lock_path.open('r') if lock_path.exists() else nullcontext(None) as lock:
        if lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
        data = (state / 'queue.jsonl').read_bytes()
        if data and not data.endswith(b'\n'):
            raise ValueError('incomplete queue tail; no rescue plan')
        records = [json.loads(line) for line in data.splitlines() if line.strip()]
        matches = [row for row in records if row.get('event_id') == event_id]
        result = dict(event_id=event_id, plan_only=True, apply_supported=False,
                      send_supported=False, coordinated_snapshot=bool(lock),
                      requires_latest_conversation_readback=True,
                      requires_native_runtime_identity_verification=True)
        if len(matches) != 1:
            return dict(result, action='hold_missing_or_ambiguous_identity')
        original = matches[0]
        same_message = [row for row in records if
                        (row.get('channel'), row.get('ts')) ==
                        (original.get('channel'), original.get('ts'))]
        result.update(channel=original.get('channel'), ts=original.get('ts'),
                      original_digest=hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest(),
                      missing_room_metadata=[key for key in ('reply_thread_ts', 'bot_user_id')
                                             if key not in original])
        if len(same_message) != 1:
            return dict(result, action='hold_ambiguous_message_identity')
        db_path = state / 'worker.sqlite3'
        status = None
        if db_path.exists():
            db = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                db.execute('BEGIN')
                row = db.execute('SELECT status, slack_ts FROM jobs WHERE event_id=?', (event_id,)).fetchone()
                if row:
                    status = row[0]
                    result['slack_ts'] = row[1]
            finally:
                db.close()
        actions = {'sending': 'hold_uncertain_send', 'done': 'verify_delivery_do_not_resend',
                   'claimed': 'hold_existing_claim', None: 'review_eligibility_and_latest_conversation'}
        return dict(result, job_state=status, action=actions.get(status, 'hold_unknown_state'),
                    duplicate_acknowledgement_check_required=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('event_id')
    args = parser.parse_args()
    print(json.dumps(plan(args.state, args.event_id)))


if __name__ == '__main__':
    main()
