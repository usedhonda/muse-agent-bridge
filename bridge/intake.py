"""Shared Socket/history admission and routing; rejected bodies are not retained."""
from datetime import datetime, timezone
from .audience import resolve_event


def normalize_event(cfg, event, event_id, transport, web, bot_user_id):
    decision = resolve_event(cfg, event, bot_user_id)
    if not decision.accepted:
        return None, decision.reason
    agents = decision.audience.agent_channels.get(event.get('channel'))
    if agents and (bot_user_id in agents or not web.private_room_matches(
            event['channel'], decision.audience.user_ids | agents | {bot_user_id})):
        return None, 'private-room-mismatch'
    record = dict(event_id=event_id, transport=transport,
                  audience_id=decision.audience.id, scope=decision.audience.scope,
                  channel=event.get('channel'), user=event.get('user'),
                  text=event.get('text', ''), ts=event.get('ts'),
                  thread_ts=event.get('thread_ts') or event.get('ts'),
                  received_at=datetime.now(timezone.utc).isoformat())
    if agents:
        thread = event.get('thread_ts')
        if thread == event.get('ts'):
            thread = None  # a root may name itself as thread_ts in history
        if not thread and event.get('reply_count', 0):
            thread = event.get('ts')
        record.update(reply_thread_ts=thread, conversation_ts=thread or '',
                      bot_user_id=bot_user_id)
    return record, 'accepted'
