# Review backlog: source and deployment acceptance

Baseline: `f1065b9d58c1ff7c2834686c32b620dcb41af406`.
This inventory separates the baseline findings from changes in this branch.
Offline evidence is not live acceptance; compare deployed source separately.

| Area | Finding / uncertainty | Required reflection and verification | Status in this branch |
| --- | --- | --- | --- |
| Setup path | README lacks an end-to-end beginner path | Link getting-started; test on a clean supported VM | Documented; clean live setup pending |
| Runtime support | Launchers depend on a Muse-specific credential module | Declare supported runtime and show a clear prerequisite failure | Documented; poller adapter now lazy with clear error |
| Python service | Listener unit invokes system Python while setup uses a venv | Choose one supported interpreter path and verify supervised startup | Documented template/user-bus/manual-start caveats; lifecycle keeper remains external |
| Credentials | App and bot registration are separate steps | Distinguish missing app credential from bot failure without exposing values | Documented; actual registrations pending operator verification |
| Config examples | YAML/legacy JSON have different reaction defaults | Retire JSON or make its obsolete status unambiguous | YAML documented as current; JSON retirement remains |
| Hook scheduling | `hookPollIntervalSec` is not consumed by the loader | Document real scheduler setup; do not imply config alone registers it | Documented; runtime scheduling remains external |
| Deployment docs | Standby/polling descriptions conflict with later handoff notes | Separate architecture from dated local deployment receipts | Generic profile guide replaces current-host assertions |
| Source/runtime drift | VM-local hook/session/keeper changes may live outside Git | Compare commit, modified filenames, supervisor and persistence inventory | Pending operator/runtime acceptance |
| VM replacement | Files/processes may disappear independently of code | Test restart and replacement separately; document external recovery dependencies | Pending operator/runtime acceptance |
| Bot eligibility | History access and new-event wakeup differ | Test natural agent events, room membership and self-event suppression | Pending operator/runtime acceptance |
| Owner natural call | In baseline `audience.resolve_event`, a configured nonempty `agentChannels` room bypasses mention gating for owner messages too | Compare VM commit/config and any upstream hook gate; test owner plain text, name-only and explicit mention separately; identify the actual rejecting layer | Offline owner plain/name/mention intake regression passes; live comparison pending |
| Reply location | Defaults, room metadata and local overrides interact | Test actual top-level and existing-thread paths, including queued events | Pending operator/runtime acceptance |
| Runtime queue | A dedicated context may still share an execution queue | Measure concurrent owner-DM/room dispatch under load | Pending operator/runtime acceptance |
| Claim expiry | `claim()` deletes aged claimed rows after 900 seconds | Prove old turn termination or use lease ownership/fencing; test a live long turn | Fixed: diagnostic age only, explicit resume; long-turn regression passes |
| Reconciliation | Reply status and actual Slack arrival can diverge | Correlate event ID/turn/post before replay; preserve uncertain deliveries | Pending operator/runtime acceptance |
| Intake dedupe | `EventStore` caches seen keys per process without interprocess locking | Reproduce listener/poller overlap; use atomic durable uniqueness | Fixed: append lock plus persisted refresh; two-process regression passes |
| ACK timing | Membership API and fsync precede protocol ACK | Measure three-second deadline; do not assume Slack replay guarantees | Pending operator/runtime acceptance |
| Room replies | Blank acknowledgements can trigger pointless bot exchanges | Preserve skip; test substantive collaboration without empty response loops | Skip preserved; live conversational acceptance pending |
| Diagnostic visibility | Connected/queued/running/sent are different states | Expose metadata-only stage status and waiting owner/reason | Added worker status and opt-in intake drop reasons; runtime start/end instrumentation remains |
| Validation | Source tests cannot establish installed runtime acceptance | Record offline, deployed and end-to-end results separately | 77 offline tests pass; deployment/live acceptance not performed |

[Getting started](getting-started.md) and the [worker contract](worker.md) cover
setup and diagnosis. Draft PRs #2 (secondary seen-ID persistence) and #3 (queued
bot identity binding) remain independent and unmerged. This branch does not
claim to incorporate them or validate any currently running deployment.
