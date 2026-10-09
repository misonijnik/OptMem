# OptMem

Permanent memory for AI agents. A 426-token prompt, a script, plug and play.

![how OptMem works](anim/optmem.gif)

## OptChat for Codex

OptChat is an installable native Codex plugin for persistent chat memory and
fresh task workers. Hooks record user messages, visible replies, tool calls
and results automatically. A background compactor summarizes the log into a
binary tree; `view`, `zoom`, and `date` let Codex recover earlier decisions
and open the original messages when a summary is too vague.

For each separate user turn, the parent is instructed to start a new native
subagent without inheriting the parent's conversation history. The worker
receives the current request and reads OptChat memory before doing the task;
the parent relays its result. Messages that steer an active task go to its
existing worker. Workers' final reports become part of the permanent memory.

### Install the plugin from source

Use a checkout containing the `codex/` directory. You need Python 3.8+ on
`PATH` and a Codex CLI supporting plugins, hooks, and native subagents; this
installation was tested with Codex CLI 0.161.0. Sign in to Codex normally so
background compaction can use its saved login.

From the checkout root:

```sh
cd /path/to/OptMem
codex plugin marketplace add ./codex
codex plugin add optchat@optmem-local
codex plugin list --json
```

The first command registers this checkout's local marketplace; the second
installs the bundled plugin into Codex's plugin cache. The list should show
`optchat@optmem-local`, version `0.2.0`, with `installed` and `enabled` set
to `true`. The bundle includes its Python scripts, so no build step or uv
installation is required for the plugin.

Start a **new Codex session** in the project you want to work on. Open `/hooks`
and review/trust OptChat's hook definitions: installation alone does not enable
them. Check `/mcp` for the OptChat server. Native subagent tools must also be
enabled. See the official [hook review instructions](https://learn.chatgpt.com/docs/hooks).

### Use and update

After hook review, logging and fresh-worker routing apply automatically.
You can explicitly invoke the skill with `$optchat`, for example:

```text
$optchat Show the current memory status.
```

Memory stays in `~/.optchat` and is shared across root Codex sessions on this
machine. Set `OPTCHAT_DIR` before launching Codex to choose another directory.
The compactor uses your usual Codex model unless you set `OPTCHAT_MODEL`.
Subagents and compactions use your account's normal model allowance.

After changing the sources, refresh the cached plugin:

```sh
codex plugin remove optchat@optmem-local
codex plugin add optchat@optmem-local
```

Then start a new session and review changed hooks through `/hooks`. Keep the
checkout at its registered path for later updates. Removing the plugin leaves
its memory in place. Reinstalling the uv CLI does not refresh the plugin.

Fresh-worker routing is supplied through hook and skill instructions; hooks
cannot enforce a spawn or reset the parent itself. The parent retains Codex's
normal history, and Anthropic-specific cache marks do not apply. Reasoning is
excluded from the log; binary attachments and private subagent steps remain in
their native Codex sessions. Live end-to-end delegation and model compaction
have not been tested here.
See [the plugin guide](codex/README.md) for data layout, settings, and validation.

## Install the `memo` CLI

From a local checkout, with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install .
memo init --instructions ./AGENTS.md
```

Use `./CLAUDE.md` instead for Claude, or pass the path to another instruction
file. OptMem prepends a marked memory block, preserving existing instructions.
Run setup again to update that block without duplicating it. Plain `memo init`
prints the block for manual setup instead. Memories stay at `~/.optmem/memory`.

The generated instructions use the installed launcher's path. With uv this is
usually `~/.local/bin/memo` on macOS and Linux; run
[`uv tool dir --bin`](https://docs.astral.sh/uv/reference/cli/#uv-tool-dir) to
find your executable directory. Use that path in every `memo` command in
`AGENTS.md` (or `CLAUDE.md`).

If you previously pasted the standalone installer's unmarked `## Memory`
block, remove that old block before running `memo init --instructions FILE`.
This installs a marked block with the current executable path. Other
instructions and existing memories are preserved.

If uv reports that its executable directory is missing from `PATH`, run
`uv tool update-shell` and open a new terminal.

After changing or updating the local checkout, reinstall with:

```sh
uv tool install --reinstall .
```

`uv tool uninstall optmem` removes the installed command; memories remain.

### Standalone installer

With Python 3 installed, you can also use the original single-file installer:

```sh
curl -fsSL https://raw.githubusercontent.com/VictorTaelin/OptMem/main/install.sh | sh
```

It prints a `## Memory` block. Paste that at the top of your agent's
`AGENTS.md` (or `CLAUDE.md`), and you are done. Run the same line again to
update.

This installer puts the tool at `~/.optmem/memo`; put `~/.optmem` on `PATH` to
type `memo`, or invoke that path directly.

## Commands

| | |
|---|---|
| `memo init [--instructions FILE]` | create memory; print or install the agent instructions |
| `memo wake` | read the memory — the first command of every session |
| `memo note "..."` | record one memory: one line, up to 280 bytes |
| `memo nap` | answer the merges that came due |
| `memo recall <regex>` | search every memory ever recorded, word for word |
| `memo zoom <lo>-<hi>` | open a tree node into its two halves |
| `memo forget <lo>-<hi>` | drop a bad summary; the next nap rebuilds it |

Merges arrive one at a time, in the output of `note`. Nothing ever runs in the
background.

## Files

```
~/.optmem/
  memo          the tool, if installed with the standalone installer
  memory/
    LOG.txt     every memory, one per line, append-only, never edited
    TREE/       the summaries: a cache, rebuildable from the log alone
    config      the sizes, written by `memo config`
```

With uv, the executable and its Python environment are managed by uv. Both
installation methods use the same memory directory and no runtime dependencies.

```sh
memo config                  # show the sizes
memo config WAKE_LINES=300   # how many lines wake prints (96 ≈ 8k tokens)
memo config WAKE_LINES=      # back to the default
```

`WAKE_LINES` is the only size worth touching, and it is a reading budget, not
a storage budget: change it whenever, in either direction, and nothing is
recomputed.

Records are fixed width, so position *is* identity and every lookup is one
seek. At a million memories (608 MB), `wake` takes 0.03s.

Set `$MEMORY_DIR` to keep `memory/` elsewhere — a synced folder, a git repo.

## The prompt

This example uses the usual uv executable path on macOS and Linux. Prefer
the block generated by `memo init`, which uses your actual executable and
memory paths. For the standalone installer, the executable is
`~/.optmem/memo`; update every command below if copying the block manually.

```markdown
## Memory

Your memory is OptMem:
- The tool is `~/.local/bin/memo`
- Your memories are in `~/.optmem/memory`

OptMem outlives every session, compaction, model and vendor change.
Without it you do not know who you are, or what was decided and tried.

### At startup: activating OptMem (mandatory)

Run `~/.local/bin/memo wake` before any other tool call, in every session, and
then do exactly what it prints, to the end of its output.

### While working: register memories (mandatory)

Call `~/.local/bin/memo note "<1 line, max 280 bytes>"` whenever you learn
something new, or something worth keeping happens. That covers a task
worth real effort, a fact or insight the user teaches you, anything you
learn about their life (even indirectly), any event of lasting effect.

Do not register redundant memories.

If `~/.local/bin/memo note` asks a compression: do it before your next action.

Never edit or delete anything under `~/.optmem/memory`: the tool manages it.

### When you need an old memory: search, or navigate

`~/.local/bin/memo recall <regex>` searches every memory, word for word.

Your memories also form a binary tree: #0-1, #2-3 ... exist as one-line
summaries, pairs of those as #0-3, and so on -- every `#a-b` line wake
prints is one node of it. `~/.local/bin/memo zoom <a-b>` opens a node into its
two halves, down to the raw memories.

### If you're a subagent: skip everything above

Parallel sessions on this machine are all you, and may all write memories.
A subagent is not: it must never run `memo`, because it cannot judge what
is already known, and its notes would arrive duplicated and incorrectly.
When you spawn one, write: `You are a subagent. Don't run memo.`
```
