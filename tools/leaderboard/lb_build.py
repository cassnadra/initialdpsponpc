"""Leaderboard builder - run by a scheduled GitHub Action (.github/workflows/leaderboard.yml), or by hand.

Reads new messages in the submissions channel (the game posts "IDSS-LB 1 {json}" + ghost.idg through a webhook),
checks every ghost, keeps each player's best per board and writes into LB_DIR:
  boards.txt   what the game reads:  B<TAB>board<TAB>course<TAB>label  /  E<TAB>rank<TAB>name<TAB>time_ms<TAB>car<TAB>pid<TAB>ghost<TAB>date
  boards.json  the same for people / web pages
  ghosts/<board>/<pid>.idg
  state.json   what has been read (last message id), every accepted entry, each player id's key + names used
  config.txt   (yours, never touched): submit_webhook=<url> makes every copy of the game send there
A board is "<course>-<direction>" (0 / 1), plus "-wet" for a run on the course's rain variant (a wet lap is slower,
so dry and wet have their own boards; the ghost's tail records which).
Env: DISCORD_TOKEN (a bot that can read the channel), DISCORD_CHANNEL (its id), LB_DIR (default lb),
     DISCORD_API (default https://discord.com/api/v10 - tests point it at fake_discord.py). Python 3 stdlib only.
"""
import hashlib, json, os, re, shutil, statistics, struct, sys, time, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ecdsa_p256

API = os.environ.get('DISCORD_API', 'https://discord.com/api/v10').rstrip('/')
TOKEN = os.environ.get('DISCORD_TOKEN', '')
CHANNEL = os.environ.get('DISCORD_CHANNEL', '')
LB = os.environ.get('LB_DIR', 'lb')
STEAM_OPENID = os.environ.get('STEAM_OPENID', 'https://steamcommunity.com/openid/login')   # (tests: fake_discord.py)
# The only account that may moderate (delete scores, ban players): the admin's Steam id (17 digits).
# A moderation command is accepted only when it is signed with the admin's signing key (a key only their PC has) and
# that key's player id has been Steam-verified to this account. A Steam id isn't a secret, so it is safe to keep here.
ADMIN_STEAM_ID = os.environ.get('ADMIN_STEAM_ID', '76561198289153961')
TOP = 100                  # entries per board in boards.txt
MAX_GHOST = 4 << 20
HEAD = struct.Struct('<4si16si')              # magic, version, course, n
HEAD2 = struct.Struct('<iI23sb')              # car, paint, model, ai
SAMPLE = struct.Struct('<10f')                # t (vblanks since GO), pos, right, forward
TAIL = struct.Struct('<4sIiI')                # "IDGF", finish (race timer, 1/6000 s), start place, reserved
                                             # (reserved bit 0: the run had another player in it - versus / party;
                                             #  bit 1: the run was on the course's wet variant)


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
    tmagic, finish, entry, reserved = TAIL.unpack_from(data, len(data) - TAIL.size)
    if tmagic != b'IDGF': raise ValueError('no finish record')
    return {'course': course.split(b'\0')[0].decode('ascii', 'replace'), 'car': car, 'ai': ai, 'samples': s, 'finish': finish, 'entry': entry, 'reserved': reserved}


def link_steam(pid, proof, steam, steam_ok, nonces):
    """A Steam sign-in answer from player `pid`: confirmed by Steam -> verified; too old to confirm -> linked by the player."""
    try:
        steam[pid] = verify_steam(proof, nonces); steam_ok[pid] = True
        print(f"{pid}: Steam account {steam[pid]} linked (confirmed by Steam)")
    except NotConfirmed as e:   # a real sign-in answer for this game, too old to check: shown as linked by the player
        if pid not in steam:
            steam[pid] = e.steam_id; steam_ok[pid] = False
            print(f"{pid}: Steam account {e.steam_id} linked (by the player - Steam no longer confirms the sign-in)")
    except Exception as e:
        print(f"{pid}: Steam link not accepted ({e})")


def check_link(link, proof, keys):
    """None if a link message ("link|pid|sha256(proof)" signed) comes from the key that owns its player id, else why not."""
    pid = str(link.get('pid', ''))
    if not re.fullmatch(r'[0-9a-f]{16}', pid): return 'bad player id'
    if hashlib.sha256(proof).hexdigest() != link.get('proof_sha256'): return 'proof was changed'
    if not ecdsa_p256.verify(str(link.get('pub', '')), str(link.get('sig', '')), ('link|%s|%s' % (pid, link['proof_sha256'])).encode()): return 'bad signature'
    if pid in keys and keys[pid] != link['pub']: return 'player id already belongs to another key (impersonation?)'
    return None


def clean_text(t, n):
    t = re.sub(r'[\x00-\x1f\x7f]', ' ', str(t or ''))
    return t.replace('\t', ' ').strip()[:n]


def check_profile(pr, keys):
    """None if a profile message is signed by the key that owns its player id, else why not."""
    pid = str(pr.get('pid', ''))
    if not re.fullmatch(r'[0-9a-f]{16}', pid): return 'bad player id'
    signed = 'profile|%s|%s|%s|%d|%d|%d|%d|%s|%s' % (pid, pr.get('name', ''), pr.get('bio', ''), int(pr.get('pic', 1)), int(pr.get('car', -1)), int(pr.get('cards', -1)), int(pr.get('play_h', 0)), pr.get('car_img', ''), pr.get('discord', ''))
    if not ecdsa_p256.verify(str(pr.get('pub', '')), str(pr.get('sig', '')), signed.encode()): return 'bad signature'
    if pid in keys and keys[pid] != pr['pub']: return 'player id already belongs to another key (impersonation?)'
    return None


def check_admin(sub, keys, steam, steam_ok, signed):
    """None if a moderation command is from the admin, else why not. The command must be signed with the key that
    owns `sub['pid']`, and that player id must be Steam-verified to ADMIN_STEAM_ID - so only the admin's PC
    (the only place with the key) that has signed in through the admin's Steam account can moderate."""
    if not ADMIN_STEAM_ID: return 'no admin configured on the board'
    pid = str(sub.get('pid', ''))
    if not re.fullmatch(r'[0-9a-f]{16}', pid): return 'bad admin player id'
    if not ecdsa_p256.verify(str(sub.get('pub', '')), str(sub.get('sig', '')), signed.encode()): return 'bad signature'
    if pid in keys and keys[pid] != sub['pub']: return 'player id already belongs to another key (impersonation?)'
    if not steam_ok.get(pid) or steam.get(pid) != ADMIN_STEAM_ID: return 'not the admin (Steam not confirmed)'
    return None


def check_signature(sub, data, keys):
    """None if the submission is signed by the key that owns its player id (the first key seen for it), else why not."""
    pid = str(sub.get('pid', ''))
    if not re.fullmatch(r'[0-9a-f]{16}', pid): return 'bad player id'
    if hashlib.sha256(data).hexdigest() != sub.get('ghost_sha256'): return 'ghost file was changed'
    base = '%s|%d|%d|%s|%s|%d|%s' % (sub['board'], int(sub['time_ms']), int(sub['finish']), pid, sub.get('name', ''), int(sub.get('car', -1)), sub['ghost_sha256'])
    solo = 0 if (sub.get('solo') is False or sub.get('solo') == 0) else 1
    texts = [base + '|%d' % solo]
    if 'solo' not in sub: texts.append(base)   # (a build from before the versus check signed without it)
    if not any(ecdsa_p256.verify(str(sub.get('pub', '')), str(sub.get('sig', '')), t.encode()) for t in texts): return 'bad signature'
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


# Usui starts both directions on the same spot, facing opposite ways: the game (before 0.97.5) told them apart by the
# start place alone and sent every Usui run as direction 0. The ghost's heading at the start says which it was:
# anti-clockwise (direction 0) leaves facing -z, clockwise (1) +z.
SAME_START = {'usui'}


def run_dir(course, s, entry):
    """the run's direction (0 / 1): the start place's, or for SAME_START courses the heading at the start"""
    if course in SAME_START and len(s[0]) >= 10:
        return 0 if s[0][9] < 0 else 1
    return (entry >> 1) & 1


def run_board(course, s, entry, reserved):
    return f"{course}-{run_dir(course, s, entry)}" + ('-wet' if reserved & 2 else '')


def check(sub, g, board_info):
    """None if the run looks real, else why not."""
    if g['ai']: return 'a CPU run'
    if g.get('reserved', 0) & 1: return 'a versus race (not a solo run)'
    if sub.get('solo') is False or sub.get('solo') == 0: return 'a versus race (not a solo run)'
    if g['course'] != sub['course'] or g['entry'] != sub['entry'] or g['finish'] != sub['finish']: return 'message and ghost disagree'
    want = run_board(g['course'], g['samples'], g['entry'], g.get('reserved', 0))
    if sub['board'] != want or g['entry'] < 0: return 'wrong board (dry/wet or direction)'
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


def count_laps(samples, start, threshold=30.0):
    """Circuit laps the ghost completes: how many times it leaves the start area and comes back.
    Only used for circuits (finish near start); a point-to-point course counts as one 'lap'."""
    laps = 0
    away = False
    for s in samples:
        d = sum(abs(s[1 + k] - start[k]) for k in range(3))
        if d > threshold:
            away = True
        elif away:
            laps += 1
            away = False
    return max(1, laps)


# a circuit's finish line is a little past its grid (Myougi ~70 m, Usui ~40-60 m); a point-to-point run ends kilometres
# from where it started
CIRCUIT_END = 300.0
# the laps of a solo (time attack) race on each circuit: 3, but Usui's longer lap is raced twice
COURSE_LAPS = {'usui': 2}


def run_shape(s):
    """(is_circuit, laps) of a run's samples: a point-to-point run is one 'lap'."""
    start = s[0][1:4]
    is_circuit = sum(abs(s[-1][1 + k] - start[k]) for k in range(3)) <= CIRCUIT_END
    return is_circuit, (count_laps(s, start) if is_circuit else 1)


def board_label(sub, s, is_circuit, laps):
    """The board's label: downhill/uphill for point-to-point, or a direction/lap note for circuits.
    A wet run gets "(wet)": its board is separate, since the rain variant is slower."""
    bid = str(sub.get('board', ''))
    wet = bid.endswith('-wet')
    if wet:
        bid = bid[:-4]
    name = sub['course'].capitalize()
    if is_circuit:
        label = name + (' (reverse)' if bid.endswith('-1') else '')
        if laps != COURSE_LAPS.get(sub['course'], 3):
            label += ' (%d laps)' % laps
    else:
        label = name + (' downhill' if s[-1][2] < s[0][2] else ' uphill')
    return label + (' (wet)' if wet else '')


# ---- challenges: one a day (UTC) and one a week (from Monday 00:00 UTC). Everyone races the same course, direction and
# conditions; a run sent while it is on counts there (the same checks as any run - signed, ghost checked). Nothing to
# set up: the picks come from the date (a hash of the challenge's id), so every run of this job agrees on them.
# Published as C (challenge) + R (its ranking) lines in boards.txt, and challenges.json (with past winners) for the site.
CH_COURSES = ['myougi', 'usui', 'akagi', 'akina', 'happou', 'iroha', 'syoumaru', 'tuchisaka']   # the quick race's order (racelobby.c)
CH_NAMES = ['Myogi', 'Usui', 'Akagi', 'Akina', 'Happogahara', 'Irohazaka', 'Shomaru', 'Tsuchisaka']
CH_CIRCUITS = {'myougi', 'usui'}
CH_CONDS = [('day', 5), ('night', 3), ('wet', 2)]   # (weights) wet: either time of day - a wet run doesn't record which
CH_EPOCH = '2026-11-02'          # (a Monday) the first challenges; each one avoids the last few picks before it
CH_TOP = 20                      # entries per challenge in boards.txt
CH_KEEP_GHOSTS_DAYS = 14         # a finished challenge's ghosts are removed after this (its results stay)


def _day(s):
    import datetime
    return datetime.date.fromisoformat(s)


def ch_pick(cid, avoid):
    """(course index, direction, condition) for challenge `cid`, different from the (course, direction) pairs in avoid"""
    import random
    rng = random.Random(int.from_bytes(hashlib.sha256(('idss-challenge|' + cid).encode()).digest()[:8], 'big'))
    while True:
        c, d = rng.randrange(len(CH_COURSES)), rng.randrange(2)
        if (c, d) not in avoid: break
    conds = [k for k, w in CH_CONDS for _ in range(w) if not (CH_COURSES[c] == 'happou' and k == 'day')]   # (Happogahara: night only)
    return c, d, rng.choice(conds)


_ch_cache = {}
def ch_weekly(monday):
    """the weekly challenge starting on that Monday (a date)"""
    import datetime
    if ('w', monday) in _ch_cache: return _ch_cache['w', monday]
    first = _day(CH_EPOCH)
    avoid = set()
    if monday > first:   # (the last 3 weeks' courses, any direction)
        for k in range(1, 4):
            m = monday - datetime.timedelta(weeks=k)
            if m >= first: avoid |= {(ch_weekly(m)['ci'], d) for d in (0, 1)}
    iso = monday.isocalendar()
    ch = ch_make('w-%d-W%02d' % (iso[0], iso[1]), 'weekly', monday, monday + datetime.timedelta(days=7), avoid)
    _ch_cache['w', monday] = ch
    return ch


def ch_daily(day):
    """the daily challenge of that date: not the week's course, nor the last 6 days' (course, direction)"""
    import datetime
    if ('d', day) in _ch_cache: return _ch_cache['d', day]
    first = _day(CH_EPOCH)
    wk = ch_weekly(day - datetime.timedelta(days=day.weekday()))
    avoid = {(wk['ci'], 0), (wk['ci'], 1)}
    for k in range(1, 7):
        p = day - datetime.timedelta(days=k)
        if p >= first: avoid.add((ch_daily(p)['ci'], ch_daily(p)['dir']))
    ch = ch_make('d-' + day.isoformat(), 'daily', day, day + datetime.timedelta(days=1), avoid)
    _ch_cache['d', day] = ch
    return ch


def ch_make(cid, kind, start, end, avoid):
    import calendar
    c, d, cond = ch_pick(cid, avoid)
    course = CH_COURSES[c]
    return {'id': cid, 'kind': kind, 'ci': c, 'course': course, 'dir': d, 'cond': cond,
            'board': '%s-%d%s' % (course, d, '-wet' if cond == 'wet' else ''),
            'start': calendar.timegm(start.timetuple()), 'end': calendar.timegm(end.timetuple())}


def ch_label(ch, boards):
    """"Akina downhill, night": the direction's name from that board's label when it has times, else a guess"""
    b = boards.get('%s-%d' % (ch['course'], ch['dir'])) or boards.get('%s-%d-wet' % (ch['course'], ch['dir']))
    if b: base = re.sub(r' \((wet|\d+ laps)\)', '', b['label'])
    else:
        name = CH_NAMES[ch['ci']]
        base = (name + (' (reverse)' if ch['dir'] else '')) if ch['course'] in CH_CIRCUITS else name + (' uphill' if ch['dir'] else ' downhill')
    base = base.replace('Myougi', 'Myogi')
    return base + {'day': ', day', 'night': ', night', 'wet': ', wet'}[ch['cond']]


def ch_at(when):
    """the challenges on at that unix time: [daily, weekly] (none before the first)"""
    import datetime
    day = datetime.datetime.fromtimestamp(when, datetime.timezone.utc).date()
    if day < _day(CH_EPOCH): return []
    return [ch_daily(day), ch_weekly(day - datetime.timedelta(days=day.weekday()))]


def ch_matches(ch, board, course, reserved):
    """the run (its board + ghost tail) was raced the way the challenge says"""
    if board != ch['board']: return False
    if ch['cond'] == 'wet' or course == 'happou': return True   # (board has it; Happogahara is always night)
    return bool(reserved & 4) == (ch['cond'] == 'night')


def msg_time(m):
    import datetime
    return datetime.datetime.fromisoformat(m['timestamp'].replace('Z', '+00:00')).timestamp()


def ch_file(state, m, sub, g, data):
    """a checked run: onto each challenge it fits (sent while that challenge was on), if it beats the player's time there"""
    when = msg_time(m)
    out = []
    for ch in ch_at(when):
        if not (ch['start'] <= when < ch['end']) or not ch_matches(ch, sub['board'], g['course'], g.get('reserved', 0)): continue
        c = state.setdefault('challenges', {}).setdefault(ch['id'], {k: ch[k] for k in ('kind', 'course', 'dir', 'cond', 'board', 'start', 'end')} | {'entries': {}})
        pid = sub['pid']
        old = c['entries'].get(pid)
        if old and old['time_ms'] <= sub['time_ms']: continue
        gp = 'ghosts/ch/%s/%s.idg' % (ch['id'], pid)
        os.makedirs(os.path.dirname(os.path.join(LB, gp)), exist_ok=True)
        open(os.path.join(LB, gp), 'wb').write(data)
        c['entries'][pid] = {'name': clean_name(sub.get('name')), 'time_ms': int(sub['time_ms']), 'car': int(sub.get('car', -1)),
                             'date': m['timestamp'][:10], 'msg': m['id'], 'ghost': gp}
        out.append(ch['id'])
    return out


def ch_publish(state, now, lines, rank_of, steam, steam_ok, shown):
    """C + R lines for the challenges on now and the last finished daily / weekly; challenges.json (+ past winners)"""
    import datetime
    chs = state.setdefault('challenges', {})
    for cid, c in chs.items():   # (old ghosts go; the results stay)
        if c['end'] < now - CH_KEEP_GHOSTS_DAYS * 86400 and not c.get('ghosts_gone'):
            shutil.rmtree(os.path.join(LB, 'ghosts', 'ch', cid), ignore_errors=True); c['ghosts_gone'] = True
    cur = ch_at(now)
    prev = []
    if cur:
        day = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
        first = _day(CH_EPOCH)
        if day - datetime.timedelta(days=1) >= first: prev.append(ch_daily(day - datetime.timedelta(days=1)))
        mon = day - datetime.timedelta(days=day.weekday())
        if mon - datetime.timedelta(weeks=1) >= first: prev.append(ch_weekly(mon - datetime.timedelta(weeks=1)))
    js = {'now': int(now), 'current': [], 'recent': [], 'past': []}
    for ch, state_name in [(c, 'on') for c in cur] + [(c, 'over') for c in prev]:
        ch = dict(ch, label=ch_label(ch, state['boards']))
        ranked = rank_of(chs.get(ch['id'], {}).get('entries', {}))
        # C<TAB>id<TAB>kind<TAB>label<TAB>board<TAB>cond<TAB>course index<TAB>direction<TAB>start<TAB>end<TAB>on/over<TAB>drivers
        lines.append('\t'.join(['C', ch['id'], ch['kind'], ch['label'], ch['board'], ch['cond'], str(ch['ci']), str(ch['dir']), str(ch['start']), str(ch['end']), state_name, str(len(ranked))]))
        for r, (pid, e) in enumerate(ranked[:CH_TOP], 1):   # R<TAB>rank<TAB>name<TAB>time_ms<TAB>car<TAB>pid<TAB>ghost<TAB>date<TAB>steam<TAB>steam_ok
            lines.append('\t'.join(['R', str(r), e['name'].replace('\t', ' '), str(e['time_ms']), str(e['car']), pid, e['ghost'], e['date'], steam.get(pid, ''), ('1' if steam_ok.get(pid) else '0') if pid in steam else '']))
        (js['current'] if state_name == 'on' else js['recent']).append({**{k: ch[k] for k in ('id', 'kind', 'label', 'board', 'cond', 'course', 'dir', 'start', 'end')}, 'drivers': len(ranked),
            'entries': [{'rank': r, 'pid': pid, 'shown': shown(pid, e), 'steam': steam.get(pid, ''), **{k: e[k] for k in ('name', 'time_ms', 'car', 'date', 'ghost')}} for r, (pid, e) in enumerate(ranked[:CH_TOP], 1)]})
    for cid, c in sorted(chs.items(), key=lambda kv: -kv[1]['end']):   # every finished challenge's podium
        if c['end'] > now: continue
        ranked = rank_of(c['entries'])
        if not ranked: continue
        ch = dict(c, id=cid, ci=CH_COURSES.index(c['course']) if c['course'] in CH_COURSES else 0)
        js['past'].append({'id': cid, 'kind': c['kind'], 'label': ch_label(ch, state['boards']), 'board': c['board'], 'start': c['start'], 'end': c['end'], 'drivers': len(ranked),
                           'podium': [{'rank': r, 'pid': pid, 'shown': shown(pid, e), 'name': e['name'], 'time_ms': e['time_ms'], 'car': e['car']} for r, (pid, e) in enumerate(ranked[:3], 1)]})
    json.dump(js, open(os.path.join(LB, 'challenges.json'), 'w'), indent=1)


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
    profiles = state.setdefault('profiles', {})   # player id -> {pic, car, cards, play_h, name, bio} (their profile card)
    bans = state.setdefault('bans', {})           # "steam:<id>" or "<pid>" -> {name, reason, when} (moderation)
    for bid in [b for b in state['boards'] if re.search(r'-x\d+$', b)]:   # (versus races used to get lap-split boards)
        del state['boards'][bid]
        shutil.rmtree(os.path.join(LB, 'ghosts', bid), ignore_errors=True)
        print(f'removed board {bid}: versus races no longer count')
    for bid, b in state['boards'].items():   # (circuits finishing past their grid were labelled as point-to-point: two
        for e in b['entries'].values():      #  "Myougi downhill" boards. A board's label comes from one of its ghosts)
            try: s = parse_ghost(open(os.path.join(LB, e['ghost']), 'rb').read())['samples']
            except Exception: continue
            label = board_label({'course': b['course'], 'board': bid}, s, *run_shape(s))
            if label != b.get('label'): print(f"board {bid}: label '{b.get('label')}' -> '{label}'"); b['label'] = label
            break
    for bid in list(state['boards']):   # (Usui runs filed by the start place alone: clockwise ones were on the anti-clockwise board)
        b = state['boards'][bid]
        if b['course'] not in SAME_START: continue
        for pid, e in list(b['entries'].items()):
            try: g = parse_ghost(open(os.path.join(LB, e['ghost']), 'rb').read())
            except Exception: continue
            to = run_board(b['course'], g['samples'], g['entry'], g.get('reserved', 0))
            if to == bid: continue
            nb = state['boards'].get(to)
            if not nb:
                nb = state['boards'][to] = {'course': b['course'], 'label': board_label({'course': b['course'], 'board': to}, g['samples'], *run_shape(g['samples'])),
                                            'start': list(g['samples'][0][1:4]), 'lens': [], 'entries': {}}
            del b['entries'][pid]
            old_e = nb['entries'].get(pid)
            if old_e and old_e['time_ms'] <= e['time_ms']: print(f"{pid} ({e['name']}): {bid} -> {to}, slower than their time there"); continue
            os.makedirs(os.path.join(LB, 'ghosts', to), exist_ok=True)
            ng = f"ghosts/{to}/{pid}.idg"
            shutil.move(os.path.join(LB, e['ghost']), os.path.join(LB, ng)); e['ghost'] = ng
            nb['entries'][pid] = e
            print(f"{pid} ({e['name']}): {bid} -> {to} (its heading at the start)")
    msgs =messages_after(state['last_id'])
    if msgs and all(not m.get('content') for m in msgs if m.get('webhook_id')) and any(m.get('webhook_id') for m in msgs):
        sys.exit('::error::The submissions came with empty text: turn on MESSAGE CONTENT INTENT (Developer Portal -> your '
                 'app -> Bot -> Privileged Gateway Intents -> Save), then run again. Nothing is lost.')
    added = rejected = 0
    for m in msgs:
        state['last_id'] = m['id']
        c = m.get('content', '')
        if c.startswith('IDSS-PROFILE 1 '):   # a player's profile card (name, portrait, a line about them, stats, car)
            try:
                pr = json.loads(c[len('IDSS-PROFILE 1 '):])
                why = check_profile(pr, keys)
            except Exception as e:
                why = 'unreadable (%s)' % e
            if why: print(f"profile {m['id']} rejected: {why}"); continue
            keys.setdefault(pr['pid'], pr['pub'])
            img = str(pr.get('car_img', ''))
            if re.fullmatch(r'[0-9a-f]{32}', img):   # their car's turntable picture: a PNG whose fingerprint (sha256, first 16 bytes) was signed
                att = next((a for a in m.get('attachments', []) if a.get('filename') == 'car.png'), None)
                ok_img = False
                if att and att.get('size', 0) <= 4 << 20:   # (24-angle 384 x 216 pictures since 0.97.2: ~1 MB; the old 12-angle ones ~300 KB)
                    data = get(att['url'], auth=False)
                    if data[:8] == b'\x89PNG\r\n\x1a\n' and hashlib.sha256(data).hexdigest()[:32] == img:
                        os.makedirs(os.path.join(LB, 'cars'), exist_ok=True)
                        open(os.path.join(LB, 'cars', img + '.png'), 'wb').write(data); ok_img = True
                if not ok_img: img = ''
            else: img = ''
            pic = int(pr.get('pic', 1)); pic = pic if pic == -1 or 1 <= pic <= 173 else 1   # (any collection card)
            profiles[pr['pid']] = {'pic': pic, 'car': max(-1, min(34, int(pr.get('car', -1)))), 'cards': max(-1, min(173, int(pr.get('cards', -1)))),
                                   'play_h': max(0, min(100000, int(pr.get('play_h', 0)))), 'name': clean_text(pr.get('name'), 24), 'bio': clean_text(pr.get('bio'), 120),
                                   'car_img': img, 'discord': clean_text(pr.get('discord'), 37)}
            print(f"{pr['pid']}: profile updated")
            continue
        if c.startswith('IDSS-LINK 1 ') and m.get('attachments'):   # sent when a player signs in through Steam (+ the bot woken up):
            try:                                                     # checked while Steam still confirms it - then every run is verified
                link = json.loads(c[len('IDSS-LINK 1 '):])
                proof = get(m['attachments'][0]['url'], auth=False)
                why = check_link(link, proof, keys)
            except Exception as e:
                why = 'unreadable (%s)' % e
            if why: print(f"link {m['id']} rejected: {why}"); continue
            keys.setdefault(link['pid'], link['pub'])
            if not steam_ok.get(link['pid']): link_steam(link['pid'], proof.decode('utf-8', 'replace'), steam, steam_ok, nonces)
            continue
        if c.startswith('IDSS-DELETE 1 '):   # the admin removes a score from a board (the game only offers this in an admin build)
            try:
                d = json.loads(c[len('IDSS-DELETE 1 '):])
                board, target = str(d.get('board', '')), str(d.get('target', ''))
                why = check_admin(d, keys, steam, steam_ok, 'delete|%s|%s' % (board, target))
            except Exception as e:
                why = 'unreadable (%s)' % e
            if why: print(f"delete {m['id']} rejected: {why}"); continue
            keys.setdefault(d['pid'], d['pub'])
            b = state['boards'].get(board)
            if b and target in b.get('entries', {}):
                del b['entries'][target]
                g = os.path.join(LB, 'ghosts', board, target + '.idg')
                if os.path.exists(g): os.remove(g)
                print(f"delete: removed {target} from {board}")
            else:
                print(f"delete: {target} not on {board}")
            continue
        if c.startswith('IDSS-BAN 1 '):   # the admin mutes (no chat) or bans (no chat + no scores) a player
            try:
                d = json.loads(c[len('IDSS-BAN 1 '):])
                tsteam, tpid = str(d.get('steam', '')), str(d.get('target', ''))
                kind = 'mute' if str(d.get('kind', '')) == 'mute' else 'ban'
                why = check_admin(d, keys, steam, steam_ok, 'ban|%s|%s|%s' % (tsteam, tpid, kind))
            except Exception as e:
                why = 'unreadable (%s)' % e
            if why: print(f"ban {m['id']} rejected: {why}"); continue
            keys.setdefault(d['pid'], d['pub'])
            key = ('steam:' + tsteam) if tsteam else tpid
            if key:
                bans[key] = {'name': clean_name(d.get('name')), 'reason': clean_text(d.get('reason'), 120), 'when': m['timestamp'][:10], 'kind': kind}
                print(f"ban: {key} ({bans[key]['name']}) {kind}")
            continue
        if not c.startswith('IDSS-LB 1 ') or not m.get('attachments'): continue
        try:
            sub = json.loads(c[len('IDSS-LB 1 '):])
            att = m['attachments'][0]
            if att.get('size', 0) > MAX_GHOST: raise ValueError('ghost too big')
            data = get(att['url'], auth=False)
            g = parse_ghost(data)
            s = g['samples']
            # a circuit ends near where it started. Only the time attack lap count is a solo time; a shorter/longer
            # run is a versus race (or a modified build) and must not beat the real times.
            is_circuit, laps = run_shape(s)
            want_laps = COURSE_LAPS.get(sub['course'], 3)
            why = check_signature(sub, data, keys)   # (signed with the board the game sent)
            if not why and g['course'] in SAME_START and g['course'] == sub['course']:
                fixed = run_board(g['course'], s, g['entry'], g.get('reserved', 0))
                if fixed != sub['board']: print(f"{m['id']}: {sub['board']} -> {fixed} (its heading at the start)"); sub['board'] = fixed
            board_key = sub['board']
            b = state['boards'].get(board_key)
            why = (why
                   or ('a %d-lap circuit race (only %d-lap races count)' % (laps, want_laps) if is_circuit and laps != want_laps else None)
                   or check(sub, g, b))
        except Exception as e:
            why = 'unreadable (%s)' % e
        if why:
            rejected += 1
            print(f"rejected {m['id']}: {why}")
            continue
        if not b:
            b = state['boards'][board_key] = {'course': sub['course'], 'label': board_label(sub, s, is_circuit, laps), 'start': list(s[0][1:4]), 'lens': [], 'entries': {}}
        pid = sub['pid']
        keys.setdefault(pid, sub['pub'])
        st = next((a for a in m['attachments'][1:] if a.get('filename') == 'steam.txt'), None)
        if st and not steam_ok.get(pid):   # (the signature above proves this player sent it - now: is the Steam answer genuine?)
            try: link_steam(pid, get(st['url'], auth=False).decode('utf-8', 'replace'), steam, steam_ok, nonces)
            except Exception as e: print(f"{pid}: Steam link not accepted ({e})")
        if (pid in bans and bans[pid].get('kind') != 'mute') or (steam.get(pid) and ('steam:' + steam[pid]) in bans and bans['steam:' + steam[pid]].get('kind') != 'mute'):
            rejected += 1   # a full ban drops new submissions (a mute only silences chat)
            print(f"rejected {m['id']}: banned")
            continue
        nm = clean_name(sub.get('name'))
        if nm not in names.setdefault(pid, []): names[pid].append(nm)
        for cid in ch_file(state, m, sub, g, data): print(f"challenge {cid}: {nm} {sub['time_ms']} ms")   # (also a run slower than their best on the board)
        old = b['entries'].get(pid)
        if old and old['time_ms'] <= sub['time_ms']: continue
        gdir = os.path.join(LB, 'ghosts', board_key)
        os.makedirs(gdir, exist_ok=True)
        open(os.path.join(gdir, pid + '.idg'), 'wb').write(data)
        b['entries'][pid] = {'name': clean_name(sub.get('name')), 'time_ms': int(sub['time_ms']), 'car': int(sub.get('car', -1)),
                             'game': str(sub.get('game', ''))[:2], 'date': m['timestamp'][:10], 'msg': m['id'],
                             'ghost': 'ghosts/%s/%s.idg' % (board_key, pid)}
        b['lens'] = (b['lens'] + [round(g['length'], 1)])[-50:]
        added += 1
        print(f"added {board_key}: {clean_name(sub.get('name'))} {sub['time_ms']} ms")
    # publish (everyone is shown with a tag from their id: "Takumi #3f2a" - names alone can clash or change; a player
    # who linked Steam is that Steam account instead: one entry for all their PCs, the best time)
    def shown(pid, e): return e['name'] + (' (Steam)' if pid in steam else ' #' + pid[:4])
    def who(pid): return 'steam:' + steam[pid] if pid in steam else pid
    lines, js = ['# IDSS leaderboard 1'], {}
    def rank_of(entries):   # [(pid, entry)] fastest first, one per player (a Steam account's PCs count as one)
        best = {}
        for pid, e in sorted(entries.items(), key=lambda kv: (kv[1]['time_ms'], kv[1]['msg'])):
            best.setdefault(who(pid), (pid, e))
        return sorted(best.values(), key=lambda kv: (kv[1]['time_ms'], kv[1]['msg']))
    for bid in sorted(state['boards']):
        b = state['boards'][bid]
        ranked = rank_of(b['entries'])[:TOP]
        lines.append('\t'.join(['B', bid, b['course'], b['label']]))
        js[bid] = {'course': b['course'], 'label': b['label'], 'entries': []}
        for r, (pid, e) in enumerate(ranked, 1):
            lines.append('\t'.join(['E', str(r), e['name'].replace('\t', ' '), str(e['time_ms']), str(e['car']), pid, e['ghost'], e['date'], steam.get(pid, ''), ('1' if steam_ok.get(pid) else '0') if pid in steam else '']))
            js[bid]['entries'].append({'rank': r, 'pid': pid, 'shown': shown(pid, e), 'steam': steam.get(pid, ''), 'steam_verified': bool(steam_ok.get(pid)), **{k: e[k] for k in ('name', 'time_ms', 'car', 'game', 'date', 'ghost')}})
    for pid, pr in sorted(profiles.items()):   # profile cards: P<TAB>pid<TAB>pic<TAB>car<TAB>cards<TAB>play seconds<TAB>name<TAB>bio<TAB>car_img<TAB>discord<TAB>steam<TAB>steam_ok
        lines.append('\t'.join(['P', pid, str(pr['pic']), str(pr['car']), str(pr['cards']), str(pr['play_h'] * 3600), pr['name'], pr['bio'], pr.get('car_img', ''), pr.get('discord', ''),
                                steam.get(pid, ''), ('1' if steam_ok.get(pid) else '') if pid in steam else '']))
    ch_publish(state, float(os.environ.get('LB_NOW') or time.time()), lines, rank_of, steam, steam_ok, shown)   # (LB_NOW: tests)
    open(os.path.join(LB, 'boards.txt'), 'w', newline='\n').write('\n'.join(lines) + '\n')
    json.dump(js, open(os.path.join(LB, 'boards.json'), 'w'), indent=1)
    open(os.path.join(LB, 'bans.txt'), 'w', newline='\n').write(   # X<TAB>key<TAB>kind<TAB>name<TAB>reason<TAB>when  (key = steam:<id> or <pid>, kind = mute/ban)
        '\n'.join(['# IDSS bans 1'] + ['\t'.join(['X', k, v.get('kind', 'ban'), v.get('name', ''), v.get('reason', ''), v.get('when', '')]) for k, v in sorted(bans.items())]) + '\n')
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

