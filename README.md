# TeamSpeak 3 → TeamSpeak 6 server migration tool

Unofficial scripts that move a TeamSpeak 3 server (SQLite) onto a
[TeamSpeak 6 beta server](https://github.com/teamspeak/teamspeak6-server).
TeamSpeak documents **no migration path** between the two, and staff have said a TS3
snapshot cannot be restored into TS6. This project does what can be done by editing the
databases directly.

> **Beta software, unsupported method.** It edits the TS6 database by hand and writes to it
> while the server runs. Always work on copies, keep the TS3 server untouched, and read
> "Limitations" first.

> **Built with Claude, tested on one server.** This tool was written with
> [Claude](https://claude.com/claude-code) (Anthropic's AI coding assistant), working
> together with the server's owner. It has been tested on **a single TeamSpeak 3 instance**
> (SQLite, about 2,800 clients, one virtual server) against TS6 `6.0.0-beta13.1`. Other
> setups (older TS3 versions, several virtual servers, MariaDB) have not been tried, so
> expect to adapt it, and check the results yourself.

## What it does

| Part | What | When |
|---|---|---|
| `ts3_to_ts6/migrate.py` | Imports channels, channel properties, server and channel groups, their permissions, server settings, privilege keys (tokens) and IP bans into a **fresh** TS6 database. Keeps TS3 ids, rewrites `server_id`. Writes everything keyed to a client UID into a side database (`legacy.sqlite`). | Once, offline |
| `ts3_to_ts6/remapper.py` | Watches the TS6 server. When an old user connects it restores what was keyed to their old UID: server groups, channel groups, personal permissions, unread offline messages, description, avatar, first-connection date, and connection and traffic totals. It also enforces old UID bans. | Continuous |

### Why a remapper is needed

A TS3 UID is `base64(SHA-1(public key text))` and a TS6 UID is `base64(SHA-256(same text))`.
The same identity therefore gets a different UID, and a hash cannot be converted offline.
The TS6 server reports each connecting client's public key (`client_public_key_raw`), so the
remapper recomputes the old SHA-1 UID at connect time and looks the person up.

## Requirements

* Python 3.10+ (standard library only)
* A copy of the TS3 server folder (`ts3server.sqlitedb`, plus its `-wal` / `-shm` files if present, and `files/`)
* A fresh TS6 beta server with the Web Query enabled (`TSSERVER_QUERY_HTTP_ENABLED=1`)

## Usage

1. **Back up.** Copy the whole TS3 folder (`rsync -a` pulls are read-only for the source).
   Run `PRAGMA integrity_check` on the copy. Never point the scripts at the live TS3 database.
2. **Start a fresh TS6 server once** (see `examples/docker-compose.yml`), let it create its
   database, then **stop it** and keep a copy of that database as your clean starting point.
3. **Get TS6's permission list** (the script drops permission names TS6 does not know):
   ```sh
   curl -s -H "x-api-key: $TS6_API_KEY" http://127.0.0.1:10080/1/permissionlist > permissionlist.json
   ```
4. **Dry run, then run:**
   ```sh
   python3 ts3_to_ts6/migrate.py \
     --ts3-db  backup/TS3/ts3server.sqlitedb \
     --ts6-db  fresh-copy/tsserver.sqlitedb \
     --perm-list permissionlist.json \
     --legacy-out legacy.sqlite \
     --src-server 6 --dst-server 1 --dry-run
   ```
   Drop `--dry-run` to write. The script refuses to run on a TS6 database that is not fresh.
5. **Put the result in place:** copy the migrated `tsserver.sqlitedb` into the TS6 data
   directory and copy `files/virtualserver_<src>/` to `files/virtualserver_<dst>/`.
6. **Start the remapper:**
   ```sh
   export TS6_API_KEY=...            # admin API key from the TS6 first-run output
   export LEGACY_DB=legacy.sqlite
   export TS6_DB=/path/to/data/tsserver.sqlitedb
   export AVATAR_DIR=/path/to/data/files/virtualserver_1/internal
   python3 ts3_to_ts6/remapper.py    # DRY_RUN=1 first to see what it would apply
   ```
   Environment variables: `TS6_QUERY_URL`, `TS6_API_KEY`, `LEGACY_DB`, `STATE_DB`,
   `POLL_SECONDS`, `DRY_RUN`, `TS6_DB`, `AVATAR_DIR`, `DST_SERVER`.

## Gotchas found along the way

* **Web Query silently stops answering** if the client IP is not on `query_ip_allowlist.txt`.
  With Docker the server sees the gateway address (for example `172.20.0.1`); add it.
* **Do not copy** the TS3 server unique identifier, keypair or license token. A modified
  `VIRTUALSERVER_UNIQUE_IDENTIFIER` has stopped TS6 from starting for other people.
* **TS3's "unlimited" quota** is `18446744073709551615`; TS6 cannot load that for quotas, so
  the script keeps the TS6 default.
* **A stale TS3 password hash** with `virtualserver_flag_password = 0` means no password in
  TS3, but TS6 treats any non-empty password as active. The script clears it unless the flag is on.
* **Permissions** are stored by name, so no id mapping is needed. TS6 renamed or dropped a few
  (for example `b_client_issue_client_query_command`); unknown names are skipped and logged.
* **Publish the file-transfer port** (30033/tcp). Icons and avatars are downloaded over it.
* After changing icon permissions, clients may cache failed downloads; clear the client cache.
* Run the remapper as its own process and stop it with `pkill -f '[r]emapper\.py'` carefully:
  a plain `pkill -f remapper.py` also matches the shell that runs it.

## Limitations

* **TS3 clients cannot show avatars on a TS6 server** in the tested beta (TS6 clients can).
  This is a server/client limitation, not something the scripts can fix.
* The avatar flag, first-connection date and traffic totals have no query command in TS6, so
  the remapper **writes them straight into the running server's SQLite database**. It works in
  testing, but it is unsupported and could break with a future TS6 version. Counters are only
  added while a client is offline so a write-back on disconnect cannot undo them.
* Avatars and first-connection dates appear from the user's **next** connection.
* Not migrated: ServerQuery login names of clients, additional virtual servers (the TS6 beta
  license allows one), the TS3 license, and the client list itself (clients re-register on connect).
* The UID-ban and offline-message paths are implemented but were not exercised live in testing.
* The TS6 beta is capped at 32 slots and its license renews every two months.
* Only SQLite is supported as the TS3 source.

## Safety notes

* `legacy.sqlite`, `state.sqlite` and any `*.sqlitedb` contain your users' data. They are
  excluded by `.gitignore`; keep them out of version control.
* The remapper needs the admin API key. Keep it in the environment, never in a file in this repo.

## License

Public domain, under the [Unlicense](LICENSE). Use it however you like, no attribution needed.
