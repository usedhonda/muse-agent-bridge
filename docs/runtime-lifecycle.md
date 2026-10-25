# Runtime lifecycle: intake, checks and execution

This is an integration contract and acceptance guide, not a description of a
verified running VM. A short scheduled check, the resident listener, and a model
turn need separate lifecycle owners. If a platform terminates a check together
with its children, starting the listener or a long model turn inside that check
does not create a durable service.

## Establish the target before changing it

Independently match the current native conversation to the intended target
runtime before installation, recovery or acceptance. Existing SSH trust verifies
an endpoint key, not that it is the active native conversation's deployment.
Repository HEAD, matching logs or a peer report alone do not establish this
mapping. Conflicting environment observations remain unresolved until verified;
keep offline source tests distinct from live deployment evidence. Do not change
keys, trust or connections to manufacture a match.

## Muse public architecture and integration unknowns

The September 8, 2026 [Meta security article](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse)
describes Hatch and agent tools inside a `systemd-nspawn` runtime cell. Runtime
root is not host root. Authd, privileged connector workers and Sentinel sit
outside it. Authd supplies surrogates; Sentinel inserts real credentials after
authorizing egress. Unix-socket peer ACLs and built-in-worker cgroup/credential allowlists
constrain access. Real credentials remain outside the agent/runtime cell, rather
than being absent from the entire VM.

Meta's [Connector help](https://www.meta.com/help/superintelligence/1687253048996149/)
supports Custom Connectors and a Secure Credentials Store. It does not document
this bridge's helper API or an independent service's credential eligibility.

Integration inference: Linux/systemd and product background-agent capabilities
do not establish support for an arbitrary runtime-cell daemon. These sources do
not establish child-process lifetime, any particular check/tool timeout, restart
persistence or credential injection into a separately launched service. Obtain
the target runtime's lifecycle and custom-connector execution contracts before
selecting that adapter. Unknown support is not proof that support is absent.
Public architecture is not an inventory of the currently deployed runtime.

## What this repository supplies

| Component | Existing behavior | Remaining integration |
| --- | --- | --- |
| `bridge.listener` | Resident receive loop; bounded startup-auth retries and in-process reconnect | An approved supervisor that owns its lifetime across check completion and runtime restart |
| `deploy/run-listener.sh` | Obtains runtime-specific credentials, then `exec`s the listener in the same process | Does not daemonize, register a keeper, or establish a new lifecycle boundary |
| `deploy/muse-slack-bridge.service` | systemd **user** unit template with `Restart=on-failure` | Requires a functioning user manager, adapted interpreter/paths and secure credential injection |
| `deploy/muse-slack-bridge-poller.service` | systemd **system** unit template with `Restart=always` | Requires an approved system manager, service ownership and adapted paths; the repository does not install it automatically |
| `bridge.worker` | Short CLI calls: claim, diagnose, submit a reply, skip or recover | Does not run a resident model worker or schedule/model a turn; the external executor must own that work |
| `deploy/run-worker.sh` | Injects a runtime-specific bot credential and runs one worker CLI action | Does not keep a model turn alive when its parent check ends |

The listener's reconnect loop works while its process exists. It cannot restart
itself after the platform kills that process. Process existence alone also does
not establish an active Slack connection or a functioning runtime dispatcher.
The repository does not supply a generic connection-health API or portable
lifecycle keeper for a hosted runtime.

## Existing launcher diagnostic ambiguity

`deploy/run-listener.sh` catches every exception from either credential lookup
and substitutes an empty string. Exit 3 therefore means that a credential is
unavailable to this invocation; it does not distinguish missing registration,
execution-identity refusal, helper failure or a temporary problem. In particular,
the current app-credential message says "not registered yet", but that cause is
not established by this code. Authentication and connection have not succeeded
at that checkpoint.

The helper's documented error contract is not included here. Do not invent
error classes, classify all helper failures as transient, or infer permission
from an empty result. Obtain its supported sanitized error/context categories
from runtime documentation before refining recovery. This documentation update
does not change helper calls, exception handling, credentials or access policy.
Never print returned values, raw exceptions or environment dumps for diagnosis.

## Separate the three owners

1. **Intake supervisor:** start and own one listener through the host's supported
   long-lived service mechanism. Use a consistent checkout, environment and state
   directory. The short scheduled check should inspect that owner's health;
   registration/restart belongs to the supported lifecycle integration, not an
   ad hoc child process created on every check.
2. **Short check/dispatcher:** inspect queue/executor readiness and return within
   its execution budget. `worker status` does not claim a turn. Only call `next`
   when an executor is available to accept the packet. Persist the event ID to
   executor-job mapping and distinguish acceptance from actual execution start.
3. **Runtime executor:** own the model turn independently of check lifetime,
   preserve the claimed packet's context and authorization policy, and record
   start/progress/end. Invoke `reply` or `skip` with the same event ID when ready.
   Creating a separate conversation does not by itself preserve context or
   establish an independent execution lifetime/capacity.

For hooks that turn stdout JSON into executor input, use the bounded
`deploy/run-wake.sh` adapter (or `next --with-context` with secure injection).
It calls current-context retrieval before emitting the packet. On refresh
failure it preserves the claim and emits no packet; a hook must preserve that
error rather than replacing it with null or an old packet. Model-side prose
alone is not evidence that this retrieval ran. This adapter does not start the
model, create a conversation or establish an independent process lifetime.
A queue/dispatch delay after handoff requires another pre-reply refresh.

The bridge has no generic runtime submission API, so these are obligations for
the target runtime's adapter, not commands already implemented by this repo.
A claim proves a packet was handed out; it does not prove executor acceptance
or task start. A successful Slack post does not prove external work succeeded.

If dispatch fails before any runtime turn starts, the dispatcher may use
`release --confirmed-not-started` and let ordinary polling retry. If execution
started or its outcome is unknown, preserve the claim and continue from recorded
progress after establishing the previous executor stopped. `sending` requires
Slack delivery reconciliation. Age alone never releases a claim; do not restore
automatic TTL replay to compensate for a lifecycle failure. See [recovery](worker.md#recovery).

## Hosts without a systemd user bus

Check the target host's supported service/lifecycle facilities before choosing
a template. A missing user bus does not establish permission or support for a
system service. Use a platform-provided long-lived task/daemon facility only if
that environment supports it and deployment is authorized. Record its lifecycle
owner, launch method, stop/rollback method and private state persistence locally.

This repository intentionally supplies no `nohup`, `setsid`, reparenting or
alternate-host recipe as a substitute for a supported host lifecycle. Moving a
process out of a parent's process group does not prove the platform permits it
to survive a turn, runtime suspension or restart.

Until a supported lifecycle owner exists, describe intake as manual-start and
recovery as manual. Preserve `.local/` and inspect unfinished work before using
the existing secure launcher for an authorized manual recovery. Stop older
intake writers during an upgrade; do not reset durable state. A polling-only
profile can cover owner DMs and eligible configured room messages returned by
history, using the shared room gate. This does not establish existing-thread
reply recovery: separate thread retrieval and live lifecycle acceptance remain
unverified. No platform configuration, service registration or restart
is performed by following the repository's offline tests.

## Evidence for authorized live acceptance

Keep evidence in protected local deployment notes, never public conversation
logs, environment dumps or credential files:

- Actual source commit and modified filenames; paths/interpreter used by the
  supervisor, check and executor; persistence across runtime replacement.
- Lifecycle owner and supported platform facility; relevant execution/timeout
  policy; PID/parent/start/end metadata without command arguments or environment.
- Connection establishment/reconnect/disconnect timestamps and a harmless
  authorized round trip. A process listing or historical connected message
  alone does not establish current connection liveness.
- One event ID mapped to queue intake, claim, executor acceptance/start/end and
  actual Slack reply timestamp/location. Diagnose a waiting conversation with
  metadata; do not infer the cause from a late snapshot alone.
- A harmless model turn outliving completion of its triggering check without a
  second claim or repeated external action. Separately test a supported runtime
  restart and listener/executor recovery while preserving unfinished/sending jobs.
- Context/history available to the resumed or separate conversation, tested
  independently from its execution capacity and lifetime.

These are deployment acceptance steps, not results claimed by this branch.
The offline suite verifies queue, routing, privacy and retry contracts; it cannot
establish host lifecycle support or recover messages missed during a Socket outage.
