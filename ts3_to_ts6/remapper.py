#!/usr/bin/env python3
"""Reconnect remapper: restores TS3-era client data on a migrated TS6 server.

TS3 UID = base64(SHA-1(public key text)), TS6 UID = base64(SHA-256(same text)).
When a client connects, TS6 reports its public key, so we recompute the legacy
UID, look it up in legacy.sqlite (written by migrate.py) and re-apply what was
keyed to it: server groups, channel groups, client permissions, unread offline
messages, and UID bans. Each TS6 client database id is processed once.
"""
import base64, hashlib, json, os, sqlite3, sys, time, urllib.parse, urllib.request

BASE = os.environ.get('TS6_QUERY_URL', 'http://127.0.0.1:10080/1')
KEY = os.environ['TS6_API_KEY']
HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.environ.get('LEGACY_DB', os.path.join(HERE, 'legacy.sqlite'))
STATE = os.environ.get('STATE_DB', os.path.join(HERE, 'state.sqlite'))
INTERVAL = float(os.environ.get('POLL_SECONDS', '3'))
DRY = os.environ.get('DRY_RUN') == '1'
TS6_DB = os.environ.get('TS6_DB', '/opt/teamspeak6/data/tsserver.sqlitedb')
AVATAR_DIR = os.environ.get('AVATAR_DIR', '/opt/teamspeak6/data/files/virtualserver_1/internal')
DST_SERVER = int(os.environ.get('DST_SERVER', '1'))
DUPLICATE_OK = {2561}  # "duplicate entry": already a member / already set


def log(*a):
    print(time.strftime('%F %T'), *a, flush=True)


def api(cmd, **params):
    url = f'{BASE}/{cmd}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'x-api-key': KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {'status': {'code': e.code, 'message': str(e)}}


def call(cmd, **params):
    """Run a write command; returns (ok, message). Duplicates count as ok."""
    if DRY:
        log('   [dry]', cmd, params)
        return True, 'dry'
    d = api(cmd, **params)
    st = d.get('status', {})
    code = st.get('code', -1)
    return (code == 0 or code in DUPLICATE_OK), f"{code} {st.get('message', '')}"


def legacy_uid(pubkey):
    return base64.b64encode(hashlib.sha1(pubkey.encode()).digest()).decode()


def new_uid(pubkey):
    return base64.b64encode(hashlib.sha256(pubkey.encode()).digest()).decode()


def mark(state, cldbid, sha1uid, sha256uid, summary):
    if DRY:  # a dry run must leave no trace, so the real run still processes this client
        return
    state.execute('insert or replace into applied values (?,?,?,?,?)', (cldbid, sha1uid, sha256uid, time.time(), summary))
    state.commit()


def avatar_name(digest):
    """TS avatar filename: 'avatar_' + each byte as two letters a-p (high nibble first)."""
    return 'avatar_' + ''.join(chr(97 + (b >> 4)) + chr(97 + (b & 15)) for b in digest)


def set_created(cldbid, ts):
    """Write the TS3 first-connection time (the query cannot edit it). Returns True if changed."""
    db = sqlite3.connect(TS6_DB, timeout=15)
    try:
        cur = db.execute("select value from client_properties where server_id=? and id=? and ident='client_created'", (DST_SERVER, cldbid)).fetchone()
        if not cur or cur[0] == str(ts):
            return False
        db.execute("update client_properties set value=? where server_id=? and id=? and ident='client_created'", (str(ts), DST_SERVER, cldbid))
        db.commit()
        return True
    finally:
        db.close()


def reconcile_created(state, lg, online):
    """Re-check first-connection dates once a client is offline, in case the server
    wrote its own value back on disconnect."""
    if DRY:
        return
    for cldbid, sha1 in state.execute('select cldbid,sha1_uid from applied where cldbid not in (select cldbid from created_ok)').fetchall():
        if cldbid in online:
            continue
        row = lg.execute("select p.value from legacy_clients c join legacy_client_props p on p.old_client_id=c.old_client_id and p.ident='client_created' where c.sha1_uid=?", (sha1,)).fetchone()
        if row and set_created(cldbid, row['value']):
            log(f'     first-connection date re-applied for cldbid {cldbid}')
        state.execute('insert or ignore into created_ok values (?)', (cldbid,))
        state.commit()


def reconcile_stats(state, lg, online):
    """Add the TS3 connection counter and traffic totals to the TS6 totals, once per client,
    while the client is offline (so a server write-back on disconnect cannot undo it).
    last-connected is left alone: TS6 stamps it with the current connection, which is newer."""
    if DRY:
        return
    for cldbid, sha1 in state.execute('select cldbid,sha1_uid from applied where cldbid not in (select cldbid from stats_ok)').fetchall():
        if cldbid in online:
            continue
        row = lg.execute('select totalconnections,total_upload,total_download from legacy_clients where sha1_uid=?', (sha1,)).fetchone()
        if row:
            db = sqlite3.connect(TS6_DB, timeout=15)
            try:
                db.execute('update clients set client_totalconnections=coalesce(client_totalconnections,0)+?, client_total_upload=coalesce(client_total_upload,0)+?, client_total_download=coalesce(client_total_download,0)+? where server_id=? and client_id=?',
                           (row['totalconnections'] or 0, row['total_upload'] or 0, row['total_download'] or 0, DST_SERVER, cldbid))
                db.commit()
            finally:
                db.close()
            log(f"     stats added for cldbid {cldbid}: +{row['totalconnections']} connections, +{row['total_upload']} up, +{row['total_download']} down")
        state.execute('insert or ignore into stats_ok values (?)', (cldbid,))
        state.commit()


def restore_profile(lg, old, cldbid, sha1uid, sha256uid, nick):
    """Restore description and avatar. TS6 cannot set the avatar flag through the query,
    so the file is copied to the new SHA-256 name and the flag is written to the database
    (the server reads it when the client next connects). Never overwrites a TS6 avatar."""
    props = {r['ident']: r['value'] for r in lg.execute('select ident,value from legacy_client_props where old_client_id=?', (old,))}
    out = []
    if props.get('client_created') and not DRY and set_created(cldbid, props['client_created']):
        out.append('first-connection date')
    if props.get('client_description'):
        ok, msg = call('clientdbedit', cldbid=cldbid, client_description=props['client_description'][:200])
        out.append('description' if ok else f'description FAILED ({msg})')
    flag = props.get('client_flag_avatar')
    if flag:
        import hashlib, shutil
        src = os.path.join(AVATAR_DIR, avatar_name(base64.b64decode(sha1uid)))
        dst = os.path.join(AVATAR_DIR, avatar_name(base64.b64decode(sha256uid)))
        if not os.path.exists(src):
            out.append('avatar file missing')
        elif hashlib.md5(open(src, 'rb').read()).hexdigest() != flag:
            out.append('avatar file does not match its stored hash, skipped')
        elif DRY:
            out.append('avatar [dry]')
        else:
            db = sqlite3.connect(TS6_DB, timeout=15)
            try:
                cur = db.execute("select value from client_properties where server_id=? and id=? and ident='client_flag_avatar'", (DST_SERVER, cldbid)).fetchone()
                if cur and cur[0]:
                    out.append('kept existing TS6 avatar')
                elif not cur:
                    out.append('avatar skipped (no property row yet)')
                else:
                    if not os.path.exists(dst):
                        shutil.copyfile(src, dst)
                    db.execute("update client_properties set value=? where server_id=? and id=? and ident='client_flag_avatar'", (flag, DST_SERVER, cldbid))
                    db.commit()
                    out.append('avatar (visible after next connect)')
            finally:
                db.close()
    if out:
        log(f'     profile {nick}: ' + ', '.join(out))


def process(clid, nick, cldbid, state, lg):
    info = api('clientinfo', clid=clid).get('body', [{}])[0]
    pk = info.get('client_public_key_raw')
    if not pk:
        log(f'? {nick} (clid {clid}): no public key reported, skipping')
        return False
    sha1uid, sha256uid = legacy_uid(pk), new_uid(pk)

    row = lg.execute('select old_client_id,nickname from legacy_clients where sha1_uid=?', (sha1uid,)).fetchone()
    ban = lg.execute('select ban_reason,ban_timestamp,ban_length from legacy_uid_bans where ban_uid=?', (sha1uid,)).fetchone()

    if ban and not (ban['ban_length'] and ban['ban_timestamp'] + ban['ban_length'] < time.time()):
        ok, msg = call('banclient', clid=clid, time=0, banreason=(ban['ban_reason'] or 'migrated TS3 ban')[:80])
        log(f'BAN  {nick} (cldbid {cldbid}) matched a TS3 UID ban -> {msg}')
        mark(state, cldbid, sha1uid, sha256uid, 'banned')
        return True

    if not row:
        log(f'NEW  {nick} (cldbid {cldbid}): no TS3 history, nothing to restore')
        mark(state, cldbid, sha1uid, sha256uid, 'new')
        return True

    old = row['old_client_id']
    done = {'sg': 0, 'cg': 0, 'perm': 0, 'msg': 0, 'fail': 0}

    def track(key, res, what):
        ok, msg = res
        if ok:
            done[key] += 1
        else:
            done['fail'] += 1
            log(f'   ! {what} failed: {msg}')

    for (gid,) in lg.execute('select group_id from legacy_server_groups where old_client_id=?', (old,)):
        track('sg', call('servergroupaddclient', sgid=gid, cldbid=cldbid), f'servergroupaddclient sgid={gid}')
    for r in lg.execute('select channel_id,group_id from legacy_channel_groups where old_client_id=?', (old,)):
        track('cg', call('setclientchannelgroup', cgid=r['group_id'], cid=r['channel_id'], cldbid=cldbid),
              f"setclientchannelgroup cgid={r['group_id']} cid={r['channel_id']}")
    for r in lg.execute('select perm_id,perm_value,perm_negated,perm_skip from legacy_client_perms where old_client_id=?', (old,)):
        track('perm', call('clientaddperm', cldbid=cldbid, permsid=r['perm_id'], permvalue=r['perm_value'],
                           permskip=r['perm_skip'] or 0, permnegated=r['perm_negated'] or 0), f"clientaddperm {r['perm_id']}")
    for r in lg.execute('select channel_id,perm_id,perm_value,perm_negated,perm_skip from legacy_channel_client_perms where old_client_id=?', (old,)):
        track('perm', call('channelclientaddperm', cid=r['channel_id'], cldbid=cldbid, permsid=r['perm_id'],
                           permvalue=r['perm_value']), f"channelclientaddperm {r['perm_id']}")
    for m in lg.execute('select from_sha1_uid,subject,msg,ts from legacy_messages where to_old_client_id=? and flag_read=0', (old,)).fetchall():
        sender = lg.execute('select nickname from legacy_clients where sha1_uid=?', (m['from_sha1_uid'],)).fetchone()
        when = time.strftime('%F %H:%M', time.gmtime(m['ts'] or 0))
        body = f"[Migrated from TS3 - from {sender['nickname'] if sender else 'unknown'}, {when} UTC]\n{m['msg'] or ''}"
        track('msg', call('messageadd', cluid=sha256uid, subject=(m['subject'] or '')[:200], message=body), 'messageadd')

    summary = json.dumps(done)
    log(f"OK   {nick} (cldbid {cldbid}) <- TS3 '{row['nickname']}' (id {old}): {summary}")
    try:
        restore_profile(lg, old, cldbid, sha1uid, sha256uid, nick)
    except Exception as e:  # profile restore must never block group restore
        log(f'   ! profile restore failed for {nick}: {e!r}')
    mark(state, cldbid, sha1uid, sha256uid, summary)
    return True


def main():
    state = sqlite3.connect(STATE)
    state.execute('create table if not exists applied (cldbid integer primary key, sha1_uid text, sha256_uid text, at real, summary text)')
    state.execute('create table if not exists created_ok (cldbid integer primary key)')
    state.execute('create table if not exists stats_ok (cldbid integer primary key)')
    lg = sqlite3.connect(f'file:{LEGACY}?mode=ro', uri=True)
    lg.row_factory = sqlite3.Row
    log(f'remapper up: {lg.execute("select count(*) from legacy_clients").fetchone()[0]} legacy clients, dry_run={DRY}')
    while True:
        try:
            online = set()
            for c in api('clientlist').get('body', []):
                if c.get('client_type') != '0':
                    continue
                cldbid = int(c['client_database_id'])
                online.add(cldbid)
                if state.execute('select 1 from applied where cldbid=?', (cldbid,)).fetchone():
                    continue
                process(c['clid'], c['client_nickname'], cldbid, state, lg)
            reconcile_created(state, lg, online)
            reconcile_stats(state, lg, online)
        except Exception as e:  # keep running; the next poll retries
            log('error:', repr(e))
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
