# codex-conversation-migrator

A local Codex conversation migration tool for exporting, importing, merging, and moving threads between model providers.

This utility works with the local Codex data directory (`CODEX_HOME` or `~/.codex`). It can:

- Export all local Codex conversations from all `model_provider` values.
- Export complete project/workspace directories referenced by conversations by default.
- Import conversations into another machine and rewrite them to a target provider such as `openai`.
- Merge duplicate thread IDs safely when importing the same conversation more than once.
- Repair project history indexes and legacy thread source metadata during import or local migration so projects can show and open their conversations.
- Repair project history indexes and legacy thread source metadata directly without changing providers.
- Rewrite existing local conversations from one provider to another, such as `crs` to `openai`.

> This is an unofficial local data migration helper. Close Codex App before running export, import, migration, or repair commands. Codex keeps some sidebar state in memory while it is running, and may overwrite external repairs when it exits.

## Requirements

- Python 3.10+
- A local Codex installation that has already created `~/.codex`

No third-party Python packages are required.

## Usage

### Export from the source machine

```powershell
python codex_conversation_migrator.py export --output codex-conversations.zip
```

The export package includes local conversation files, metadata from all providers, and complete project/workspace directories referenced by conversations.

Project/workspace directories are copied with no exclusion rules. Review the package for secrets, large files, build outputs, dependencies, databases, and private data before sharing it.

To export conversations only and skip project/workspace directories:

```powershell
python codex_conversation_migrator.py export --output codex-conversations.zip --skip-workspaces
```

### Import into the target machine

Open Codex App once on the target machine, log in, then close Codex App before importing.

```powershell
python codex_conversation_migrator.py import codex-conversations.zip openai
```

All imported conversations are rewritten to the target provider (`openai` in this example), so they can appear under that provider's local conversation list.

During import, the tool also repairs local project history indexes and legacy thread source metadata. It preserves source `projectless-thread-ids` only for threads that were actually projectless, writes `thread-project-assignments` for project threads, updates `sidebar-project-thread-orders` with `local:<thread_id>` keys, backfills `thread-projectless-output-directories` for older projectless threads, and fills missing `thread_source` values with `user`.

If the package includes project/workspace directories, they are restored by default under:

```text
~/.codex/imported_workspaces/<package-name>/
```

Imported thread metadata is updated so `cwd` points to the restored project directory on the target machine.

To choose a restore location:

```powershell
python codex_conversation_migrator.py import codex-conversations.zip openai --restore-workspaces-to D:\CodexImportedProjects
```

### Merge behavior

Import uses merge mode by default:

- Existing thread IDs are not duplicated.
- Existing `.jsonl` event lines are kept.
- Missing event lines from the import package are appended.
- Thread metadata keeps the newer `updated_at` side.
- Related metadata tables are refreshed to avoid duplicate rows.

To replace existing conversations with the imported copy:

```powershell
python codex_conversation_migrator.py import codex-conversations.zip openai --overwrite
```

To skip conversations that already exist on the target:

```powershell
python codex_conversation_migrator.py import codex-conversations.zip openai --skip-existing
```

### Migrate providers on the same machine

```powershell
python codex_conversation_migrator.py migrate crs openai
```

Backward-compatible shorthand:

```powershell
python codex_conversation_migrator.py crs openai
```

This rewrites local conversation metadata from the old provider to the new provider and updates `config.toml` unless `--no-set-config` is provided.

It also repairs project history indexes and fills missing `thread_source` values after the provider rewrite.

### Repair project history indexes only

If provider values are already correct but some project folders show `No conversations` / `暂无对话`, or an older projectless search result appears but will not open even though the thread exists in local history, close Codex App and run:

```powershell
python codex_conversation_migrator.py repair-indexes
```

Short alias:

```powershell
python codex_conversation_migrator.py repair
```

This rebuilds project assignment state and projectless output-directory hints from `state_5.sqlite` and `.codex-global-state.json`, and fills missing `thread_source` values with `user`, without changing providers or session files.

## Custom Codex Home

By default, the script uses `CODEX_HOME` if set, otherwise `~/.codex`.

You can override it:

```powershell
python codex_conversation_migrator.py export --codex-home C:\path\to\.codex --output codex-conversations.zip
python codex_conversation_migrator.py import codex-conversations.zip openai --codex-home C:\path\to\.codex
```

## Backups

Import and local provider migration automatically create a backup under:

```text
~/.codex/migration_backups/
```

The backup includes the local conversation and metadata files the script may modify.

## What It Touches

The script may read or write:

- `sessions/`
- `archived_sessions/`
- `state_5.sqlite`
- `goals_1.sqlite`
- `session_index.jsonl`
- `.codex-global-state.json`
- `config.toml`
- `imported_workspaces/` when importing a package that includes project directories

It does not copy or modify login credential files such as `auth.json`.

## Restore

If something looks wrong after import or migration:

1. Close Codex App.
2. Restore the latest backup from `~/.codex/migration_backups/`.
3. Reopen Codex App.
