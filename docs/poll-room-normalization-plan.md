# Room polling normalization: review plan, not a transport switch

The current branch excludes configured agent rooms from polling because its
poll records do not meet the worker's room admission/routing contract. That is a
containment measure pending shared normalization, not a permanent requirement
that every deployment use Socket Mode. No poller code is changed by this plan.
Parallel poller work must first be identified by file/commit and reviewed;
worker context receipts and metadata-only rescue planning are separate changes.

A minimal future alternative should reuse a single admission/normalization
boundary for poll and Socket input, with transport provenance kept explicit:

- Resolve the authenticated bot identity from Slack, and verify private/unshared
  room properties plus the exact configured membership before retaining bodies.
  Validate again before contextual reads and sends. Membership/API failure must
  not save the room body; do not trust an agent's identity assertion or widen
  the configured room/sender set to make a payload eligible.
- Use Slack-returned author, bot/subtype, channel and exact message/thread
  timestamps. Preserve top-level versus existing-thread routing and bot-self
  rejection; do not invent missing bot/room/thread metadata from conversation.
  Share the resulting audience/scope, bot identity, reply location and conversation
  fields with the Socket path, instead of two divergent payload schemas.
- Fetch every available page before advancing a durable high watermark over
  that range. Compare timestamps numerically without binary-float rounding;
  process inclusive boundary overlap through dedupe. A partial page/API failure
  must not advance past unseen records. State update and concurrent poll starts
  require serialization or a recoverable checkpoint protocol; append locking
  alone does not protect independent watermark writes.
- Establish the supported Slack thread-retrieval capability separately; root
  history alone must not be called existing-thread recovery. Verify required API
  access with already authorized credentials/scopes before promising coverage.
  Keep a bounded, documented backfill window and report gaps rather than silently
  marking history caught up.
- Retain the original durable ID for an already accepted `(channel, ts)`;
  incoming envelope IDs remain provenance, not permission to create another job.
  Both transport arrival orders, same-transport retries, concurrent processes,
  page boundaries and crash-before-checkpoint must yield one accepted identity.
  Legacy incomplete rows require a separately reviewed rescue plan; normalizing
  new intake alone cannot repair an existing dedupe shadow.

Offline acceptance needs both arrival orders for root and existing-thread
messages, rejected membership/API failures with no body retention, author/bot
validation, full/partial pagination, exact timestamp boundaries, watermark crash
recovery and concurrent starts. Reply acceptance must still require the current
worker context receipt, preserve originals, and hold uncertain sends. None of
this plan switches live intake, starts a daemon, requests new credentials/scopes,
repairs existing queues or establishes native/target runtime identity.
