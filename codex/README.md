# OptChat: native Codex plugin

OptChat keeps visible Codex conversations as permanent memory and routes user
turns to fresh native subagents. It is a local plugin with hooks, MCP tools,
and a skill; no separate chat interface or model API key is needed.
Python 3.8+ and a Codex version supporting plugin lifecycle
hooks are required. Local installation was tested with Codex CLI 0.161.0.

## Install from source

Use a checkout that contains `codex/.agents/plugins/marketplace.json` and
`codex/plugins/optchat/`. You need `python3` and `codex` on `PATH`, normal
Codex authentication, and native subagents enabled. The plugin bundles its
Python scripts; it does not require a build, a uv environment, or an extra
model API key.

From the repository root:

```sh
cd /path/to/OptMem
codex plugin marketplace add ./codex
codex plugin add optchat@optmem-local
codex plugin list --json
```

`./codex` is the marketplace root, not the plugin directory. To register it
from another directory, pass `/absolute/path/to/OptMem/codex` instead. Codex
registers that local source and copies the plugin into its installed cache.
`plugin list` should show `optchat@optmem-local`, version `0.2.0`, installed
and enabled. Keep the checkout at its registered location for source updates.

Start a new session in the project where you want to work. Open `/hooks` and
review/trust the OptChat definitions, then check `/mcp` for the OptChat server.
Codex requires this review for plugin hooks; installation does not grant hook
trust. See [Codex plugin packaging](https://developers.openai.com/plugins/build/plugins)
and [Codex hooks](https://learn.chatgpt.com/docs/hooks).

The MCP tools are `view(page)`, `zoom(id, n, page)`, `date(id)`, and `status()`.
The skill is `$optchat` (namespaced as `optchat:optchat` in plugin catalogs).
Routing instructions are supplied automatically at startup, resume, native
compaction, and each root user prompt. Logging does not require invoking the
skill. Try `$optchat Show the current memory status.` to exercise the worker
and the memory tools.

To refresh a local installation after editing the plugin, remove it and add it
again, then start a new session and review changed hooks:

```sh
codex plugin remove optchat@optmem-local
codex plugin add optchat@optmem-local
```

Removing the plugin does not remove its memory.
Updating the uv package only updates the optional CLI; it does not refresh
Codex's cached plugin.

## Fresh workers

The main Codex thread acts as a dispatcher. Each separate user turn starts
one new worker with no inherited conversation history. On Codex versions
exposing `spawn_agent`, routing requests `fork_context=false`; versions with
`collaboration.spawn_agent` use `fork_turns="none"`. Both history-isolation
parameters are present in the locally tested Codex CLI 0.161.0 tool schemas.

The worker gets the user's current request verbatim, the current workspace
and applicable constraints, and a stable bootstrap at the start of its task,
marked `OPTCHAT_WORKER_V1`. It reads the saved view through MCP, follows all
pages, and zooms relevant original
messages before doing the work. The parent relays its report. Completed workers
are not reused; steering during an active task goes to that task's worker.
Workers do not recursively spawn turn workers. Further delegation still needs
the user's request.

The parent receives routing instructions rather than a copy of the full view.
MCP view pages stay within 20,000 UTF-8 bytes, avoiding dependence on very large
tool results. Workers' final reports are captured as `work` messages; their
private tool steps remain in their native Codex threads. Reports should include
lasting findings, changes, verification, and unresolved blockers.

This is a model-directed native workflow. Hooks can supply instructions but
cannot directly invoke the parent's spawn tool or guarantee that the model
obeys them. If native agents or history isolation are unavailable, the plugin
instructs the parent to explain that limitation rather than quietly work with
its accumulated history. See [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents).

## Memory and compaction

By default, all root sessions on this machine share `~/.optchat`. Set
`OPTCHAT_DIR` before starting Codex to choose a different chat directory.
The existing OptMem data at `~/.optmem/memory` is not changed.

```text
~/.optchat/
  main/YYYY-MM-DD.jsonl   permanent messages: i, kind, text, size, date, source
  tree/YYYY-MM-DD.jsonl   immutable nodes: l, i, text, size
  view.json              saved view as [l, i] pairs
  view-state.json        saved batch state and smaller compaction view
  index.sqlite3          rebuildable lookups, source deduplication, ready queues
  .lock                  exclusive writer lock
  .compactor.lock        one background compactor for the shared chat
```

Only one writer holds the store lock at a time. Model requests run outside
that lock so hooks can continue recording messages. Every JSONL append is
flushed and synced. Existing JSONL lines and completed nodes are never edited.
A missing saved view is an error; OptChat does not refit it from the log.
An incomplete JSONL write is reported instead of silently deleting data.

Kinds are `user`, `unii` (Codex's visible replies), `tool`, `echo`, `work`, and
`note`. Reasoning, developer/system messages, and compaction calls are excluded.
Subagents contribute their final `[Name]` report, not their private steps.
Tool output is clipped to its first and last characters, 30,000 characters in
all. Other text is split at UTF-8 boundaries without dropping any content.
`source` records allow repeated or concurrent hook delivery without duplicates.

Messages that fit in 512 bytes including their kind become exact leaf nodes.
Two children that fit together are joined by a newline without a model call.
Other ready nodes are compacted through `codex exec`, using saved Codex login.
Set `OPTCHAT_MODEL` to an available inexpensive model to select the compactor;
otherwise it uses the default from the user's Codex configuration. This uses
the account's normal model allowance. Up to eight compactions run together.
Ready work is queued; historical nodes are not scanned for work on each message.
Hooks are disabled in compactor calls to prevent recursive memory ingestion.
Shell, subagents, apps, plugins, and web search are disabled for these calls,
which run in a read-only sandbox. Any response containing a tool call is rejected.

Compaction prompts include the smaller saved 16–32 KB view, bounded by the
node being built and stopping at the first unbuilt line. They use a 512-dash
ruler and up to five attempts, retaining the shortest result without cutting
its words. Failed work stays queued for a later turn. Oversized summaries are
measured at their actual UTF-8 sizes.

The main view appends leaf references until it exceeds 128,000 bytes, then
merges built siblings to at most 64,000 bytes. It keeps trying at later writes
if parents are not ready. Merge priority measures age from the pair's last
message, with the oldest pair breaking ties. No dates are included in view
lines. Pending leaves use an explicit placeholder; `zoom` still retrieves the
complete original message. Views larger than a tool/hook page are paginated,
never returned as silently clipped message fragments.

## Codex integration boundaries

This is a native plugin, not a replacement model harness. Codex retains the
parent's conversation and compaction behavior. Fresh workers isolate task
execution from that history, but the parent still reads its own context to
route messages. Guaranteed per-message resets of the root itself would require
an app-server client or a replacement harness. Hooks cannot guarantee the source
design's prompt-cache ratios: workers load memory through MCP, and Anthropic
cache marks do not apply.
Compactor retry requests are fresh Codex calls with the previous answer and
correction included, rather than a resumed Anthropic conversation.

Stable hook fields capture new prompts, tool inputs/results, and final replies.
An incremental rollout adapter also captures intermediate visible replies and
backfills resumed chats. Codex does not guarantee the rollout format; the
adapter is isolated and tested against the currently installed format. The
adapter accepts both legacy message events and current `response_item` messages,
excluding environment/context injections. Synchronous hooks spend at most about
two seconds replaying a resumed transcript, checkpointing every 64 processed
records and at the budget boundary. Later hooks resume from the saved byte
offset; duplicate records do not rewrite the views. Large first-time backfills
may take several hook deliveries to finish, while direct hook fields still
capture the active prompt, tool result, and final reply.
The plugin archives text, not binary image/audio attachments or full subagent
conversations. Those remain in their original Codex sessions.

## CLI and legacy notes

The uv package also exposes `optchat` for diagnostics and explicit imports:

```sh
uv tool install --reinstall .
optchat status
optchat view
optchat compact
optchat import-optmem ~/.optmem/memory
```

The importer copies legacy 320-byte records as `note` messages, retaining ids
inside the source log and dates in the copied records. It does not edit source
files. Repeating it is safe; imported messages get their own permanent OptChat
ids. Use `--chat-dir PATH` before the subcommand to inspect a different store.

The plugin bundles its own Python scripts; installing it does not require uv
or the diagnostic CLI. The original `memo` tool and installer still work.

## Verify locally

```sh
python3 test.py
python3 -m unittest test_optchat
```

The OptChat suite uses fake model calls and temporary directories. It tests
rollback equivalence for 20,001 pushes, the T=10 age regression, append-only
logs, short-node passthrough, Unicode splitting, batching, restart/cache
recovery, pending dependencies, retries, hook deduplication, reasoning
exclusion, subagent reports, MCP navigation, and legacy imports. Live model
calls require a working authenticated Codex connection. Hook routing tests
verify context delivery and exclusion from private subagents; they do not
prove a live model will follow delegation instructions.
