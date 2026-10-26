"""Platform-neutral bridge core.

Shared by every platform adapter (Slack, Telegram, ...):

- ``store`` -- locked, atomic JSON state storage with last-good recovery.
- ``queue`` -- intake queue with dedupe and batch-claim discipline.

Platform-specific fetching, filtering and payload shaping live in the
adapters (e.g. ``bridge_core.telegram``); nothing in ``store`` or
``queue`` knows about a concrete platform's API.
"""
