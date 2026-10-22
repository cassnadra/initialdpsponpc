"""Leaderboard builder - run by a scheduled GitHub Action (.github/workflows/leaderboard.yml), or by hand.

Reads new messages in the submissions channel (the game posts "IDSS-LB 1 {json}" + ghost.idg through a webhook),
checks every ghost, keeps each player's best per board and writes into LB_DIR:
  boards.txt   what the game reads:  B<TAB>board<TAB>course<TAB>label  /  E<TAB>rank<TAB>name<TAB>time_ms<TAB>car<TAB>pid<TAB>ghost<TAB>date
  boards.json  the same for people / web pages
  ghosts/<board>/<pid>.idg
  state.json   what has been read (last message id), every accepted entry, each player id's key + names used
  config.txt   (yours, never touched): submit_webhook=<url> makes every copy of the game send there
Env: DISCORD_TOKEN (a bot that can read the channel), DISCORD_CHANNEL (its id), LB_DIR (default lb),
     DISCORD_API (default https://discord.com/api/v10 - tests point it at fake_discord.py). Python 3 stdlib only.
"""
import hashlib, json, os, re, statistics, struct, sys, time, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ecdsa_p256

API = os.environ.get('DISCORD_API', 'https://discord.com/api/v10').rstrip('/')
TOKEN = os.environ.get('DISCORD_TOKEN', '')
CHANNEL = os.environ.get('DISCORD_CHANNEL', '')
LB = os.environ.get('LB_DIR', 'lb')
STEAM_OPENID = os.environ.get('STEAM_OPENID', 'https://steamcommunity.com/openid/login')   # (tests: fake_discord.py)
TOP = 100                  # entries per board in boards.txt
MAX_GHOST = 4 << 20
HEAD = struct.Struct('<4si16si')              # magic, version, course, n
HEAD2 = struct.Struct('<iI23sb')              # car, paint, model, ai
SAMPLE = struct.Struct('<10f')                # t (vblanks since GO), pos, right, forward
TAIL = struct.Struct('<4sIiI')                # "IDGF", finish (race timer, 1/6000 s), start place, reserved


def get(url, auth=True):
    req = urllib.request.Request(url, headers={'User-Agent': 'idss-leaderboard (github action)', **({'Authorization': 'Bot ' + TOKEN} if auth else {})})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:   # rate limited: wait as told
                time.sleep(float(json.loads(e.read() or b'{}').get('retry_after', 2)) + 0.5); continue
            raise
    raise RuntimeError('rate limited too long: ' + url)


def messages_after(last_id):
    out = []
    while True:
        batch = json.loads(get(f'{API}/channels/{CHANNEL}/messages?after={last_id}&limit=100'))
        if not batch: break
        batch.sort(key=lambda m: int(m['id']))
        out += batch
        last_id = batch[-1]['id']
        if len(batch) < 100: break
    return out


def parse_ghost(data):
    if len(data) < HEAD.size + HEAD2.size + TAIL.size: raise ValueError('too short')
    magic, ver, course, n = HEAD.unpack_from(data, 0)
    if magic != b'IDG1' or ver < 2 or not 2 <= n <= 40000: raise ValueError('not a ghost file')
    car, paint, model, ai = HEAD2.unpack_from(data, HEAD.size)
    if len(data) != HEAD.size + HEAD2.size + n * SAMPLE.size + TAIL.size: raise ValueError('wrong size')
    s = [SAMPLE.unpack_from(data, HEAD.size + HEAD2.size + i * SAMPLE.size) for i in range(n)]
    tmagic, finish, entry, _ = TAIL.unpack_from(data, len(data) - TAIL.size)
    if tmagic != b'IDGF': raise ValueError('no finish record')
    return {'course': course.split(b'\0')[0].decode('ascii', 'replace'), 'car': car, 'ai': ai, 'samples': s, 'finish': finish, 'entry': entry}


def check_signature(sub, data, keys):
    """None if the submission is signed by the key that owns its player id (the first key seen for it), else why not."""
    pid = str(sub.get('pid', ''))
    if not re.fullmatch(r'[0-9a-f]{16}', pid): return 'bad player id'
    if hashlib.sha256(data).hexdigest() != sub.get('ghost_sha256'): return 'ghost file was changed'
    signed = '%s|%d|%d|%s|%s|%d|%s' % (sub['board'], int(sub['time_ms']), int(sub['finish']), pid, sub.get('name', ''), int(sub.get('car', -1)), sub['ghost_sha256'])
    if not ecdsa_p256.verify(str(sub.get('pub', '')), str(sub.get('sig', '')), signed.encode()): return 'bad signature'
    if pid in keys and keys[pid] != sub['pub']: return 'player id already belongs to another key (impersonation?)'
    return None


class NotConfirmed(ValueError):
    """A well-formed sign-in answer for this game that Steam no longer confirms (its answers only check out shortly
    after the sign-in; the board runs every few hours): .steam_id is the account claimed."""
    def __init__(self, steam_id): super().__init__('Steam no longer confirms it'); self.steam_id = steam_id


def verify_steam(query, used_nonces):
    """A "Sign in through Steam" answer (the query string Steam sent back) -> the Steam id it proves, or raises why not.
    Steam itself is asked whether the answer is genuine (OpenID check_authentication: no key needed)."""
    from urllib.parse import parse_qs, urlencode
    q = {k: v[0] for k, v in parse_qs(query.strip(), keep_blank_values=True).items()}
    if q.get('openid.mode') != 'id_res': raise ValueError('not a sign-in answer')
    if q.get('openid.op_endpoint') != 'https://steamcommunity.com/openid/login': raise ValueError('not from Steam')
    if not q.get('openid.return_to', '').startswith('http://127.0.0.1:'): raise ValueError('made for another site')
    m = re.fullmatch(r'https://steamcommunity\.com/openid/id/(\d{17})', q.get('openid.claimed_id', ''))
    if not m or q.get('openid.identity') != q.get('openid.claimed_id'): raise ValueError('no Steam id')
    nonce = q.get('openid.response_nonce', '')
    if not nonce or nonce in used_nonces: raise ValueError('answer already used')
    check = dict(q, **{'openid.mode': 'check_authentication'})
    req = urllib.request.Request(STEAM_OPENID, data=urlencode(check).encode(), headers={'User-Agent': 'idss-leaderboard', 'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=30) as r:
        answer = r.read().decode('utf-8', 'replace')
    used_nonces.append(nonce)
    if 'is_valid:true' not in answer: raise NotConfirmed(m.group(1))
    return m.group(1)


def check(sub, g, board_info):
    """None if the run looks real, else why not."""
    if g['ai']: return 'a CPU run'
    if g['course'] != sub['course'] or g['entry'] != sub['entry'] or g['finish'] != sub['finish']: return 'message and ghost disagree'
    if sub['board'] != f"{g['course']}-{(g['entry'] >> 1) & 1}" or g['entry'] < 0: return 'wrong board'
    if sub['time_ms'] != g['finish'] // 6: return 'time and finish disagree'
    if g['finish'] < 30 * 6000: return 'too short to be a race'
    s = g['samples']
    if s[0][0] > 10: return 'starts late'
    if s[-1][0] / 60 < g['finish'] / 6000 - 1.0: return 'the ghost stops before the finish'
    length = 0.0
    for a, b in zip(s, s[1:]):
        dt = (b[0] - a[0]) / 60
        if dt <= 0: return 'time runs backwards'
        d = ((b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2 + (b[3] - a[3]) ** 2) ** 0.5
        if d / dt > 110: return 'impossible speed (%.0f km/h)' % (d / dt * 3.6)
        length += d
    g['length'] = length
    if board_info:
        st = board_info['start']
        if sum(abs(s[0][1 + k] - st[k]) for k in range(3)) > 30: return 'starts somewhere else'
        if board_info.get('lens') and len(board_info['lens']) >= 3 and length < 0.9 * statistics.median(board_info['lens']): return 'too short a route (cut?)'
    return None


def clean_name(n):
    n = re.sub(r'[^\x20-\x7e]', '', str(n or '')).strip()[:24]
    return n or 'Player'


def main():
    if not TOKEN or not CHANNEL: sys.exit('DISCORD_TOKEN and DISCORD_CHANNEL must be set')
    os.makedirs(os.path.join(LB, 'ghosts'), exist_ok=True)
    sp = os.path.join(LB, 'state.json')
    state = json.load(open(sp)) if os.path.exists(sp) else {'last_id': '0', 'boards': {}}
    keys = state.setdefault('keys', {})      # player id -> its key (the first one seen)
    names = state.setdefault('names', {})    # player id -> every name it has used (moderation)
    steam = state.setdefault('steam', {})    # player id -> the Steam account it proved (signing in through Steam)
    steam_ok = state.setdefault('steam_ok', {})   # player id -> True: Steam confirmed it; else linked by the player (unverified)
    for pid in steam: steam_ok.setdefault(pid, True)   # (links made before this split were all confirmed)
    nonces = state.setdefault('nonces', [])  # Steam answers already used
    msgs = messages_after(state['last_id'])
    if msgs and all(not m.get('content') for m in msgs if m.get('webhook_id')) and any(m.get('webhook_id') for m in msgs):
        sys.exit('::error::The submissions came with empty text: turn on MESSAGE CONTENT INTENT (Developer Portal -> your '
                 'app -> Bot -> Privileged Gateway Intents -> Save), then run again. Nothing is lost.')
    added = rejected = 0
    for m in msgs:
        state['last_id'] = m['id']
        c = m.get('content', '')
        if not c.startswith('IDSS-LB 1 ') or not m.get('attachments'): continue
        try:
            sub = json.loads(c[len('IDSS-LB 1 '):])
            att = m['attachments'][0]
            if att.get('size', 0) > MAX_GHOST: raise ValueError('ghost too big')
            data = get(att['url'], auth=False)
            g = parse_ghost(data)
            b = state['boards'].get(sub['board'])
            why = check_signature(sub, data, keys) or check(sub, g, b)
        except Exception as e:
            why = 'unreadable (%s)' % e
        if why:
            rejected += 1
            print(f"rejected {m['id']}: {why}")
            continue
        s = g['samples']
        if not b:
            label = '%s %s' % (sub['course'].capitalize(), 'downhill' if s[-1][2] < s[0][2] else 'uphill')
            b = state['boards'][sub['board']] = {'course': sub['course'], 'label': label, 'start': list(s[0][1:4]), 'lens': [], 'entries': {}}
        pid = sub['pid']
        keys.setdefault(pid, sub['pub'])
        st = next((a for a in m['attachments'][1:] if a.get('filename') == 'steam.txt'), None)
        if st and not steam_ok.get(pid):   # (the signature above proves this player sent it - now: is the Steam answer genuine?)
            try:
                steam[pid] = verify_steam(get(st['url'], auth=False).decode('utf-8', 'replace'), nonces); steam_ok[pid] = True
                print(f"{pid}: Steam account {steam[pid]} linked (confirmed by Steam)")
            except NotConfirmed as e:   # a real sign-in answer for this game, too old to check: shown as linked by the player
                if pid not in steam:
                    steam[pid] = e.steam_id; steam_ok[pid] = False
                    print(f"{pid}: Steam account {e.steam_id} linked (by the player - Steam no longer confirms the sign-in)")
            except Exception as e:
                print(f"{pid}: Steam link not accepted ({e})")
        nm = clean_name(sub.get('name'))
        if nm not in names.setdefault(pid, []): names[pid].append(nm)
        old = b['entries'].get(pid)
        if old and old['time_ms'] <= sub['time_ms']: continue
        gdir = os.path.join(LB, 'ghosts', sub['board'])
        os.makedirs(gdir, exist_ok=True)
        open(os.path.join(gdir, pid + '.idg'), 'wb').write(data)
        b['entries'][pid] = {'name': clean_name(sub.get('name')), 'time_ms': int(sub['time_ms']), 'car': int(sub.get('car', -1)),
                             'game': str(sub.get('game', ''))[:2], 'date': m['timestamp'][:10], 'msg': m['id'],
                             'ghost': 'ghosts/%s/%s.idg' % (sub['board'], pid)}
        b['lens'] = (b['lens'] + [round(g['length'], 1)])[-50:]
        added += 1
        print(f"added {sub['board']}: {clean_name(sub.get('name'))} {sub['time_ms']} ms")
    # publish (everyone is shown with a tag from their id: "Takumi #3f2a" - names alone can clash or change; a player
    # who linked Steam is that Steam account instead: one entry for all their PCs, the best time)
    def shown(pid, e): return e['name'] + (' (Steam)' if pid in steam else ' #' + pid[:4])
    def who(pid): return 'steam:' + steam[pid] if pid in steam else pid
    lines, js = ['# IDSS leaderboard 1'], {}
    for bid in sorted(state['boards']):
        b = state['boards'][bid]
        best = {}
        for pid, e in sorted(b['entries'].items(), key=lambda kv: (kv[1]['time_ms'], kv[1]['msg'])):
            best.setdefault(who(pid), (pid, e))
        ranked = sorted(best.values(), key=lambda kv: (kv[1]['time_ms'], kv[1]['msg']))[:TOP]
        lines.append('\t'.join(['B', bid, b['course'], b['label']]))
        js[bid] = {'course': b['course'], 'label': b['label'], 'entries': []}
        for r, (pid, e) in enumerate(ranked, 1):
            lines.append('\t'.join(['E', str(r), e['name'].replace('\t', ' '), str(e['time_ms']), str(e['car']), pid, e['ghost'], e['date'], steam.get(pid, ''), ('1' if steam_ok.get(pid) else '0') if pid in steam else '']))
            js[bid]['entries'].append({'rank': r, 'pid': pid, 'shown': shown(pid, e), 'steam': steam.get(pid, ''), 'steam_verified': bool(steam_ok.get(pid)), **{k: e[k] for k in ('name', 'time_ms', 'car', 'game', 'date', 'ghost')}})
    open(os.path.join(LB, 'boards.txt'), 'w', newline='\n').write('\n'.join(lines) + '\n')
    json.dump(js, open(os.path.join(LB, 'boards.json'), 'w'), indent=1)
    json.dump(state, open(sp, 'w'), indent=1)
    print(f'{len(msgs)} new messages: {added} times added, {rejected} rejected')


HELP = {
    401: 'Discord refused the bot token (401). DISCORD_TOKEN must be the bot token (Developer Portal -> your app -> Bot -> '
         'Reset Token), pasted with nothing else - not the webhook URL, not the application id / public key.',
    403: 'The bot can\'t read the channel (403). Server settings -> the leaderboard channel -> Permissions: give the bot '
         'View Channel and Read Message History.',
    404: 'Discord doesn\'t know that channel (404). DISCORD_CHANNEL must be the channel id (Discord settings -> Advanced -> '
         'Developer Mode on, right-click the channel -> Copy Channel ID), and the bot must be in that server.',
}


if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as e:
        sys.exit(f'::error::{HELP.get(e.code, f"Discord answered HTTP {e.code} for {e.url}")}')

