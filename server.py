#!/usr/bin/env python3
"""
SalesQuest automation backend.

Serves index.html, keeps the ERP data in SQLite instead of the browser, and runs the
automations:
  * WhatsApp sending through the Meta WhatsApp Cloud API
  * email sending through Gmail / Google Workspace SMTP (App Password)
  * drip sequences (Day 0 / Day 3 / Day 7 ...), stopped automatically when a lead's status changes
  * bulk campaign queues, sent one by one with daily caps and a sending window
  * a daily reminder email with due follow-ups and hot leads
Standard library only - nothing to install.

  python server.py              # http://127.0.0.1:8090
  python server.py --open       # also opens the browser
  python server.py --lan        # also reachable from phones on the same Wi-Fi (access key needed)

Listens on this computer only (127.0.0.1) unless --lan is given. With --lan, any device other
than this computer must present the access key in salesquest_data/access_key.txt (opening
http://<this-pc>:8090/?key=<key> once stores it in that browser as a cookie).
Data, credentials and daily backups live in salesquest_data/. Automations only run while this
program is running.

The browser owns the ERP state (leads, notes, templates, sequences) and saves it here
whole; this program only reads it. Everything the backend does is recorded in its own
tables (outbox, enroll) and handed back to the browser through /api/poll as events.
"""
import argparse, copy, datetime as dt, hmac, json, os, re, secrets, shutil, smtplib, socket, sqlite3, ssl, sys, threading, time, traceback
import urllib.error, urllib.request, webbrowser
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

HERE     = os.path.dirname(os.path.abspath(__file__))
DATADIR  = os.path.join(HERE, 'salesquest_data')
DBFILE   = os.path.join(DATADIR, 'salesquest.db')
CFGFILE  = os.path.join(DATADIR, 'config.json')
VCCI_DIR = os.path.normpath(os.path.join(HERE, '..', 'VCCI', 'VCCI_Data'))
MAX_BODY = 30 * 1024 * 1024
DAY      = 86400
CLOSED   = ('won', 'lost', 'invalid')
FINAL    = ('sent', 'failed', 'skipped', 'cancelled')

DEFAULT_CFG = {
    'wa':       {'token': '', 'phoneId': '', 'version': 'v23.0'},
    'email':    {'host': 'smtp.gmail.com', 'port': 465, 'user': '', 'password': '', 'fromName': ''},
    'limits':   {'emailPerDay': 400, 'waPerDay': 200, 'gapSec': 20,
                 'windowStart': '09:30', 'windowEnd': '19:00', 'days': [0, 1, 2, 3, 4, 5]},  # Mon..Sat
    'reminder': {'enabled': False, 'time': '09:00', 'to': ''},
    'optOut':   [],
}
SECRETS = (('wa', 'token'), ('email', 'password'))
MASK = '••••••••'

lock = threading.RLock()
con = None
ALLOWED_HOSTS = set()
KEYFILE = os.path.join(DATADIR, 'access_key.txt')
LAN = {'on': False, 'key': '', 'urls': []}   # phone access (--lan)


def access_key():
    """Random key other devices must present; created once and kept in salesquest_data/."""
    os.makedirs(DATADIR, exist_ok=True)
    try:
        k = open(KEYFILE, encoding='utf-8').read().strip()
        if len(k) >= 8:
            return k
    except OSError:
        pass
    k = secrets.token_urlsafe(9)
    with open(KEYFILE, 'w', encoding='utf-8') as f:
        f.write(k + '\n')
    return k


def lan_ips():
    """This computer's IPv4 addresses on the local network (Wi-Fi / Ethernet)."""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))   # no packet is sent; just picks the outgoing interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(('127.', '169.254.')))


class SendError(Exception):
    pass


# ---- storage --------------------------------------------------------------
def open_db():
    os.makedirs(os.path.join(DATADIR, 'backups'), exist_ok=True)
    if os.path.exists(DBFILE):                      # one snapshot per day, taken at startup
        snap = os.path.join(DATADIR, 'backups', 'salesquest_%s.db' % dt.date.today())
        if not os.path.exists(snap):
            shutil.copy2(DBFILE, snap)
        cutoff = time.time() - 30 * DAY             # keep a month of snapshots
        for f in os.listdir(os.path.join(DATADIR, 'backups')):
            p = os.path.join(DATADIR, 'backups', f)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    c = sqlite3.connect(DBFILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE IF NOT EXISTS outbox(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, due REAL, lead_id TEXT, channel TEXT,
        tpl_id TEXT, source TEXT, enroll_id INTEGER, status TEXT, to_addr TEXT, subject TEXT, body TEXT,
        error TEXT, sent_at REAL, provider_id TEXT, ev INTEGER, updated REAL);
      CREATE TABLE IF NOT EXISTS enroll(
        id INTEGER PRIMARY KEY AUTOINCREMENT, seq_id TEXT, lead_id TEXT, started REAL, step INTEGER,
        next_at REAL, status TEXT, reason TEXT, updated REAL);
      CREATE INDEX IF NOT EXISTS outbox_q  ON outbox(status, channel, due);
      CREATE INDEX IF NOT EXISTS outbox_ev ON outbox(ev);
      CREATE INDEX IF NOT EXISTS enroll_q  ON enroll(status, next_at);
    ''')
    # A message that was mid-send when the program stopped may or may not have gone out.
    # Never resend it blindly; mark it failed so it can be checked and retried by hand.
    for r in c.execute("SELECT id FROM outbox WHERE status='sending'").fetchall():
        c.execute("UPDATE outbox SET status='failed', error=?, ev=?, updated=? WHERE id=?",
                  ('Interrupted while sending (program closed). Check before retrying.', _next_ev(c), time.time(), r['id']))
    c.commit()
    return c


def kv_get(k, default=None, c=None):
    row = (c or con).execute('SELECT v FROM kv WHERE k=?', (k,)).fetchone()
    return row[0] if row else default


def kv_set(k, v, c=None):
    (c or con).execute('INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)', (k, str(v)))


def _next_ev(c=None):
    n = int(kv_get('ev', '0', c)) + 1
    kv_set('ev', n, c)
    return n


# ---- ERP state (written by the browser, read here) --------------------------
_cache = {'rev': None, 'state': None, 'leads': {}}


def state():
    with lock:
        rev = int(kv_get('state_rev', '0'))
        if _cache['rev'] != rev:
            raw = kv_get('state')
            st = json.loads(raw) if raw else None
            _cache.update(rev=rev, state=st, leads={l['id']: l for l in (st or {}).get('leads', [])})
        return _cache['state']


def lead_by_id(lid):
    state()
    return _cache['leads'].get(lid)


def tpl_by_id(st, tid):
    return next((t for t in (st or {}).get('templates', []) if t.get('id') == tid), None)


def seq_by_id(st, sid):
    return next((s for s in (st or {}).get('sequences', []) if s.get('id') == sid), None)


# ---- config ---------------------------------------------------------------
def load_cfg():
    cfg = copy.deepcopy(DEFAULT_CFG)
    try:
        with open(CFGFILE, encoding='utf-8') as f:
            saved = json.load(f)
    except (FileNotFoundError, ValueError):
        saved = {}
    for k, v in saved.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def save_cfg(cfg):
    tmp = CFGFILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CFGFILE)


def masked(cfg):
    c = copy.deepcopy(cfg)
    for sec, key in SECRETS:
        v = c[sec].get(key) or ''
        c[sec][key] = (MASK + v[-4:]) if v else ''
    return c


def merge_cfg(new):
    cur = load_cfg()
    for sec in ('wa', 'email', 'limits', 'reminder'):
        for k, v in (new.get(sec) or {}).items():
            if (sec, k) in SECRETS and isinstance(v, str) and v.startswith(MASK[:2]):
                continue                              # unchanged secret comes back masked
            if isinstance(v, str):
                v = v.strip()
            cur[sec][k] = v
    if isinstance(new.get('optOut'), list):
        cur['optOut'] = [str(x).strip() for x in new['optOut'] if str(x).strip()]
    save_cfg(cur)
    return cur


def wa_ready(cfg):
    return bool(cfg['wa'].get('token') and cfg['wa'].get('phoneId'))


def email_ready(cfg):
    return bool(cfg['email'].get('user') and cfg['email'].get('password'))


# ---- templates (same placeholders as fill() in index.html) ------------------
def digits(s):
    return re.sub(r'\D', '', str(s or ''))


def first_of(s):
    return re.split(r'\s*[,;]\s*', str(s or ''))[0].strip()


def person_first(l):
    """"Mr. Urmil Mehta (OD), ..." -> "Urmil"; "Mr. J. K. Sharma" -> "Mr. Sharma". Mirrors personFirst() in index.html."""
    raw = re.split(r'\s*[,;&]\s*|\s+and\s+', str(l.get('contact') or ''), flags=re.I)[0]
    raw = re.sub(r'\(.*?\)', '', raw).strip()
    m = re.match(r'(mr|mrs|ms|miss|dr|shri|smt|er|prof|capt|col)\b\.?', raw, re.I)
    hon = m.group(0) if m else ''
    w = raw[len(hon):].split()
    if not w:
        if l.get('vcci'):
            return ''
        words = str(l.get('name') or '').split()
        return words[0] if words else ''
    initial = lambda s: re.fullmatch(r'[a-z]\.?', s, re.I)
    if initial(w[0]):
        return '' if initial(w[-1]) else ((hon.rstrip('.') + '. ') if hon else '') + w[-1]
    return w[0]


def fill(txt, l, settings):
    m = {'name': l.get('name') or '', 'firstName': person_first(l) or 'there', 'contact': l.get('contact') or '',
         'company': l.get('company') or 'your business', 'city': l.get('city') or 'your city',
         'industry': l.get('industry') or 'local',
         'myName': settings.get('myName') or '[your name]', 'myCompany': settings.get('myCompany') or '[your company]'}
    extra = l.get('extra') or {}
    return re.sub(r'\{\{\s*(\w+)\s*\}\}', lambda mo: str(m.get(mo.group(1), extra.get(mo.group(1), ''))), str(txt or ''))


def wa_phone(p, cc):
    d = digits(first_of(p))
    if len(d) == 11 and d[0] == '0':
        d = d[1:]
    if len(d) == 10:
        d = digits(cc or '91') + d
    return d if len(d) >= 10 else ''


def opted_out(cfg, email='', phone=''):
    for x in cfg.get('optOut') or []:
        x = x.strip().lower()
        if email and x == email.lower():
            return True
        if phone and len(digits(x)) >= 10 and digits(x)[-10:] == phone[-10:]:
            return True
    return False


# ---- senders --------------------------------------------------------------
def send_email(cfg, to, subject, body):
    e = cfg['email']
    if not email_ready(cfg):
        raise SendError('Email is not set up yet (Settings → Automation backend)')
    msg = EmailMessage()
    msg['From'] = formataddr((e.get('fromName') or e['user'], e['user']))
    msg['To'] = to
    msg['Subject'] = subject or '(no subject)'
    msg['Message-ID'] = make_msgid()
    msg.set_content(body)
    host, port = e.get('host') or 'smtp.gmail.com', int(e.get('port') or 465)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(e['user'], e['password'])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(e['user'], e['password'])
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise SendError('Email login was rejected. For Gmail, use an App Password (not your normal password).')
    except smtplib.SMTPRecipientsRefused:
        raise SendError('The email address was refused: %s' % to)
    except (smtplib.SMTPException, OSError) as ex:
        raise SendError('Email error: %s' % ex)
    return msg['Message-ID']


def send_wa(cfg, to, tpl, text, params):
    w = cfg['wa']
    if not wa_ready(cfg):
        raise SendError('WhatsApp is not set up yet (Settings → Automation backend)')
    if tpl.get('metaName'):
        t = {'name': tpl['metaName'].strip(), 'language': {'code': (tpl.get('metaLang') or 'en').strip()}}
        if params:   # Meta rejects empty values and newlines/tabs inside parameters
            t['components'] = [{'type': 'body', 'parameters': [
                {'type': 'text', 'text': re.sub(r'\s+', ' ', p).strip() or '-'} for p in params]}]
        payload = {'messaging_product': 'whatsapp', 'to': to, 'type': 'template', 'template': t}
    else:
        payload = {'messaging_product': 'whatsapp', 'to': to, 'type': 'text', 'text': {'preview_url': False, 'body': text}}
    url = 'https://graph.facebook.com/%s/%s/messages' % (w.get('version') or 'v23.0', w['phoneId'].strip())
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method='POST', headers={
        'Authorization': 'Bearer ' + w['token'].strip(), 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.load(r)
        return (res.get('messages') or [{}])[0].get('id', '')
    except urllib.error.HTTPError as ex:
        try:
            err = json.loads(ex.read()).get('error', {})
            detail = (err.get('error_data') or {}).get('details') or ''
            msg = '%s (code %s)%s' % (err.get('message', 'WhatsApp error'), err.get('code'), (': ' + detail) if detail else '')
        except ValueError:
            msg = 'WhatsApp HTTP %s' % ex.code
        if '131047' in msg or 're-engagement' in msg.lower():
            msg += '. Free text only works within 24h of the lead messaging you; give this template a Meta-approved template name.'
        raise SendError(msg)
    except (urllib.error.URLError, OSError) as ex:
        raise SendError('Could not reach WhatsApp: %s' % ex)


# ---- outbox worker ----------------------------------------------------------
def hm(s):
    try:
        h, m = str(s).split(':')[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return 0


def in_window(cfg, now=None):
    now = now or dt.datetime.now()
    L = cfg['limits']
    if now.weekday() not in [int(d) for d in L.get('days', [0, 1, 2, 3, 4, 5])]:
        return False
    m = now.hour * 60 + now.minute
    return hm(L.get('windowStart', '09:30')) <= m < hm(L.get('windowEnd', '19:00'))


def day_start():
    return dt.datetime.combine(dt.date.today(), dt.time()).timestamp()


def sent_today(channel):
    return con.execute("SELECT COUNT(*) FROM outbox WHERE channel=? AND status='sent' AND sent_at>=?",
                       (channel, day_start())).fetchone()[0]


def cap(cfg, channel):
    return int(cfg['limits'].get('waPerDay' if channel == 'whatsapp' else 'emailPerDay') or 0)


def finish(oid, status, **cols):
    with lock:
        cols.update(status=status, ev=_next_ev(), updated=time.time())
        con.execute('UPDATE outbox SET %s WHERE id=?' % ', '.join('%s=?' % k for k in cols), (*cols.values(), oid))
        con.commit()


last_sent = {'email': 0.0, 'whatsapp': 0.0}


def process_one(cfg):
    """Send at most one queued message per channel, respecting window, caps and the gap."""
    if not in_window(cfg):
        return
    gap = max(3, int(cfg['limits'].get('gapSec') or 20))
    for ch in ('whatsapp', 'email'):
        now = time.time()
        if now - last_sent[ch] < gap:
            continue
        with lock:
            if sent_today(ch) >= cap(cfg, ch):
                continue
            row = con.execute("SELECT * FROM outbox WHERE status='queued' AND channel=? AND due<=? ORDER BY due, id LIMIT 1",
                              (ch, now)).fetchone()
            if not row:
                continue
            con.execute("UPDATE outbox SET status='sending', updated=? WHERE id=?", (now, row['id']))
            con.commit()
        last_sent[ch] = time.time()
        deliver(dict(row), cfg)


def deliver(row, cfg):
    st = state() or {}
    settings = st.get('settings') or {}
    lead, tpl = lead_by_id(row['lead_id']), tpl_by_id(st, row['tpl_id'])
    if not lead:
        return finish(row['id'], 'skipped', error='Lead was deleted')
    if not tpl:
        return finish(row['id'], 'skipped', error='Template was deleted')
    if row['enroll_id']:
        why = enrollment_stop_reason(row['enroll_id'], lead, st)
        if why:
            return finish(row['id'], 'cancelled', error=why)
    subject, body = fill(tpl.get('subject'), lead, settings), fill(tpl.get('body'), lead, settings)
    to = ''
    try:
        if '[your name]' in body + subject or '[your company]' in body + subject:
            raise SendError('Add your name and company in Settings first (the message still says [your name])')
        if row['channel'] == 'email':
            to = first_of(lead.get('email'))
            if not to:
                return finish(row['id'], 'skipped', error='Lead has no email address')
            if opted_out(cfg, email=to):
                return finish(row['id'], 'skipped', error='On the opt-out list', to_addr=to)
            pid = send_email(cfg, to, subject, body)
        else:
            to = wa_phone(lead.get('phone'), settings.get('cc', '91'))
            if not to:
                return finish(row['id'], 'skipped', error='Lead has no valid phone number')
            if opted_out(cfg, phone=to):
                return finish(row['id'], 'skipped', error='On the opt-out list', to_addr=to)
            params = [fill('{{%s}}' % p.strip(), lead, settings) for p in str(tpl.get('metaParams') or '').split(',') if p.strip()]
            pid = send_wa(cfg, to, tpl, body, params)
        finish(row['id'], 'sent', to_addr=to, subject=subject, body=body, sent_at=time.time(), provider_id=pid, error='')
    except SendError as ex:
        finish(row['id'], 'failed', to_addr=to, subject=subject, body=body, error=str(ex))
    except Exception as ex:  # keep the worker alive whatever happens
        traceback.print_exc()
        finish(row['id'], 'failed', to_addr=to, error='%s: %s' % (type(ex).__name__, ex))


# ---- sequences ------------------------------------------------------------------
def stop_reason(seq, lead):
    if not seq:
        return 'Sequence was deleted'
    if not lead:
        return 'Lead was deleted'
    stop_on = seq.get('stopOn') or list(CLOSED)
    if lead.get('status') in stop_on:
        return 'Lead became "%s"' % lead.get('status')
    return None


def enrollment_stop_reason(eid, lead, st):
    e = con.execute('SELECT * FROM enroll WHERE id=?', (eid,)).fetchone()
    if not e or e['status'] == 'stopped':
        return 'Sequence was stopped for this lead'
    return stop_reason(seq_by_id(st, e['seq_id']), lead)


def stop_enrollments(ids, why):
    with lock:
        for eid in ids:
            con.execute("UPDATE enroll SET status='stopped', reason=?, updated=? WHERE id=? AND status!='stopped'",
                        (why, time.time(), eid))
            for r in con.execute("SELECT id FROM outbox WHERE enroll_id=? AND status='queued'", (eid,)).fetchall():
                con.execute("UPDATE outbox SET status='cancelled', error=?, ev=?, updated=? WHERE id=?",
                            (why, _next_ev(), time.time(), r['id']))
        con.commit()


def run_sequences():
    st = state()
    if not st:
        return
    now = time.time()
    with lock:
        rows = con.execute("SELECT * FROM enroll WHERE status='active' AND next_at<=?", (now,)).fetchall()
    for e in rows:
        seq, lead = seq_by_id(st, e['seq_id']), lead_by_id(e['lead_id'])
        why = stop_reason(seq, lead)
        if why:
            stop_enrollments([e['id']], why)
            continue
        if not seq.get('active', True):
            continue                                  # paused: picked up again when resumed
        steps = [s for s in seq.get('steps') or [] if s.get('tplId')]
        if e['step'] >= len(steps):
            with lock:
                con.execute("UPDATE enroll SET status='done', updated=? WHERE id=?", (now, e['id']))
                con.commit()
            continue
        tpl = tpl_by_id(st, steps[e['step']]['tplId'])
        if not tpl:
            stop_enrollments([e['id']], 'A template in this sequence was deleted')
            continue
        with lock:
            con.execute('INSERT INTO outbox(created, due, lead_id, channel, tpl_id, source, enroll_id, status, updated) '
                        "VALUES(?,?,?,?,?,?,?,'queued',?)",
                        (now, now, e['lead_id'], tpl.get('channel'), tpl['id'], 'seq:' + seq['id'], e['id'], now))
            nxt = e['step'] + 1
            if nxt < len(steps):
                con.execute('UPDATE enroll SET step=?, next_at=?, updated=? WHERE id=?',
                            (nxt, e['started'] + float(steps[nxt].get('day') or 0) * DAY, now, e['id']))
            else:
                con.execute("UPDATE enroll SET step=?, status='done', updated=? WHERE id=?", (nxt, now, e['id']))
            con.commit()


# ---- daily reminder -------------------------------------------------------------
def build_reminder(st):
    today = dt.date.today()
    tk = today.isoformat()
    leads = [l for l in st.get('leads', []) if l.get('status') not in CLOSED]
    due = sorted([l for l in leads if l.get('nextFollowUp') and l['nextFollowUp'] <= tk], key=lambda l: l['nextFollowUp'])
    hot = [l for l in leads if l.get('temp') == 'hot' and l not in due]
    y = today - dt.timedelta(days=1)
    calls = [e for e in st.get('log', []) if e.get('type') == 'call' and dt.date.fromtimestamp(e['ts'] / 1000) == y]

    def line(l):
        last = next((n['text'] for n in reversed(l.get('notes') or []) if n.get('text')), '')
        return '- %s | %s | %s%s' % (l.get('name'), l.get('phone') or l.get('email') or '-',
                                    'due ' + l['nextFollowUp'] if l.get('nextFollowUp') else l.get('temp', ''),
                                    (' | ' + last[:90]) if last else '')
    name = (st.get('settings') or {}).get('myName') or 'there'
    body = ['Good morning %s,' % name, '',
            'Yesterday: %d calls logged. Today\'s target: %s calls.' % (len(calls), (st.get('settings') or {}).get('dailyTarget', 100)), '',
            'FOLLOW-UPS DUE (%d)' % len(due)] + [line(l) for l in due[:60]] + (['  ...and %d more' % (len(due) - 60)] if len(due) > 60 else []) + \
           ['', 'HOT LEADS (%d)' % len(hot)] + [line(l) for l in hot[:30]] + ['', 'Open SalesQuest: http://127.0.0.1:%s/' % PORT]
    return 'SalesQuest: %d follow-ups due, %d hot leads (%s)' % (len(due), len(hot), today.strftime('%d %b')), '\n'.join(body)


def run_reminder(cfg):
    R = cfg['reminder']
    if not R.get('enabled') or not R.get('to'):
        return
    now = dt.datetime.now()
    if kv_get('reminder_last') == now.date().isoformat() or now.hour * 60 + now.minute < hm(R.get('time', '09:00')):
        return
    if time.time() - float(kv_get('reminder_try', '0')) < 3600:   # retry failures hourly, not every tick
        return
    st = state()
    if not st:
        return
    with lock:
        kv_set('reminder_try', time.time())
        con.commit()
    try:
        subject, body = build_reminder(st)
        send_email(cfg, R['to'], subject, body)
        result = {'reminder_last': now.date().isoformat(), 'reminder_error': ''}
    except Exception as ex:
        result = {'reminder_error': str(ex)}
    with lock:
        for k, v in result.items():
            kv_set(k, v)
        con.commit()


def worker():
    last_sched = 0
    while True:
        try:
            cfg = load_cfg()
            if time.time() - last_sched >= 30:
                last_sched = time.time()
                run_sequences()
                run_reminder(cfg)
            process_one(cfg)
        except Exception:
            traceback.print_exc()
        time.sleep(2)


# ---- API --------------------------------------------------------------------------
def q1(q, k, d=None):
    return (q.get(k) or [d])[0]


def api_health(q, b):
    cfg = load_cfg()
    return 200, {'ok': True, 'version': 1, 'wa': wa_ready(cfg), 'email': email_ready(cfg), 'dataDir': DATADIR}


def api_state_get(q, b):
    with lock:
        raw = kv_get('state')
        return 200, {'rev': int(kv_get('state_rev', '0')), 'state': json.loads(raw) if raw else None}


def api_state_put(q, b):
    st = b.get('state')
    if not isinstance(st, dict) or not isinstance(st.get('leads'), list):
        raise ValueError('state must be an object with a leads list')
    with lock:
        cur = int(kv_get('state_rev', '0'))
        if kv_get('state') is not None and int(b.get('rev', -1)) != cur:
            return 409, {'error': 'conflict', 'rev': cur}
        kv_set('state', json.dumps(st, ensure_ascii=False, separators=(',', ':')))
        kv_set('state_rev', cur + 1)
        con.commit()
        return 200, {'rev': cur + 1}


def api_config_get(q, b):
    return 200, masked(load_cfg())


def api_config_put(q, b):
    return 200, masked(merge_cfg(b))


def api_test(q, b):
    cfg, to = load_cfg(), str(b.get('to') or '').strip()
    if not to:
        raise ValueError('Enter where to send the test')
    try:
        if b.get('channel') == 'email':
            pid = send_email(cfg, to, 'SalesQuest test email',
                             'This is a test from your SalesQuest automation backend.\n\nEmail sending works.')
        else:
            cc = ((state() or {}).get('settings') or {}).get('cc', '91')
            num = wa_phone(to, cc)
            if not num:
                raise SendError('That phone number does not look valid')
            pid = send_wa(cfg, num, {'metaName': b.get('metaName') or 'hello_world', 'metaLang': b.get('metaLang') or 'en_US'}, '', [])
    except SendError as ex:
        return 400, {'error': str(ex)}
    return 200, {'ok': True, 'id': pid}


def api_queue(q, b):
    items, source, now = b.get('items') or [], str(b.get('source') or 'manual')[:60], time.time()
    with lock:
        n = 0
        for it in items:
            if it.get('channel') not in ('email', 'whatsapp') or not it.get('leadId') or not it.get('tplId'):
                continue
            con.execute('INSERT INTO outbox(created, due, lead_id, channel, tpl_id, source, status, updated) '
                        "VALUES(?,?,?,?,?,?,'queued',?)", (now, now, it['leadId'], it['channel'], it['tplId'], source, now))
            n += 1
        con.commit()
    return 200, {'queued': n}


def api_enroll(q, b):
    st = state()
    seq = seq_by_id(st, b.get('seqId'))
    steps = [s for s in (seq or {}).get('steps') or [] if s.get('tplId')]
    if not seq or not steps:
        raise ValueError('Sequence not found or has no steps. Save it first.')
    now, added, skipped = time.time(), 0, 0
    with lock:
        active = {r[0] for r in con.execute("SELECT lead_id FROM enroll WHERE seq_id=? AND status='active'", (seq['id'],))}
        for lid in b.get('leadIds') or []:
            lead = lead_by_id(lid)
            if not lead or lid in active or stop_reason(seq, lead):
                skipped += 1
                continue
            con.execute("INSERT INTO enroll(seq_id, lead_id, started, step, next_at, status, updated) VALUES(?,?,?,0,?,'active',?)",
                        (seq['id'], lid, now, now + float(steps[0].get('day') or 0) * DAY, now))
            active.add(lid)
            added += 1
        con.commit()
    return 200, {'enrolled': added, 'skipped': skipped}


def api_enroll_stop(q, b):
    if b.get('ids'):
        ids = [int(i) for i in b['ids']]
    elif b.get('seqId'):
        ids = [r[0] for r in con.execute("SELECT id FROM enroll WHERE seq_id=? AND status='active'", (b['seqId'],))]
    else:
        raise ValueError('ids or seqId required')
    stop_enrollments(ids, 'Stopped by you')
    return 200, {'stopped': len(ids)}


def api_outbox_cancel(q, b):
    with lock:
        if b.get('all'):
            rows = con.execute("SELECT id FROM outbox WHERE status='queued'").fetchall()
        else:
            rows = [{'id': int(i)} for i in b.get('ids') or []]
        for r in rows:
            con.execute("UPDATE outbox SET status='cancelled', error='Cancelled by you', ev=?, updated=? WHERE id=? AND status='queued'",
                        (_next_ev(), time.time(), r['id']))
        con.commit()
    return 200, {'cancelled': len(rows)}


def api_outbox_retry(q, b):
    with lock:
        for i in b.get('ids') or []:
            con.execute("UPDATE outbox SET status='queued', error='', due=?, updated=? WHERE id=? AND status='failed'",
                        (time.time(), time.time(), int(i)))
        con.commit()
    return 200, {'ok': True}


COLS = 'id, created, due, lead_id, channel, tpl_id, source, enroll_id, status, to_addr, error, sent_at, ev'


def api_lan(q, body):
    # Phone address + key for the Settings QR code. Only ever answered to this computer (see Handler.api).
    return 200, {'on': LAN['on'], 'urls': [u + '?key=' + LAN['key'] for u in LAN['urls']] if LAN['on'] else [],
                 'plain': LAN['urls'], 'key': LAN['key'] if LAN['on'] else ''}


def api_poll(q, b):
    since, cfg = int(q1(q, 'since', '0') or 0), load_cfg()
    with lock:
        events = [dict(r) for r in con.execute('SELECT %s FROM outbox WHERE ev>? ORDER BY ev LIMIT 500' % COLS, (since,))]
        ev = int(kv_get('ev', '0'))
        queued = {r[0]: r[1] for r in con.execute("SELECT channel, COUNT(*) FROM outbox WHERE status IN ('queued','sending') GROUP BY channel")}
        failed = con.execute("SELECT COUNT(*) FROM outbox WHERE status='failed' AND updated>=?", (day_start(),)).fetchone()[0]
        recent = [dict(r) for r in con.execute('SELECT %s FROM outbox ORDER BY id DESC LIMIT 60' % COLS)]
        active = [dict(r) for r in con.execute("SELECT id, seq_id, lead_id, step, next_at FROM enroll WHERE status='active'")]
        stats = {}
        for r in con.execute('SELECT seq_id, status, COUNT(*) FROM enroll GROUP BY seq_id, status'):
            stats.setdefault(r[0], {})[r[1]] = r[2]
        res = {
            'ev': ev, 'events': events,
            'wa': wa_ready(cfg), 'email': email_ready(cfg), 'window': in_window(cfg),
            'limits': cfg['limits'], 'queued': queued, 'failedToday': failed,
            'sentToday': {'whatsapp': sent_today('whatsapp'), 'email': sent_today('email')},
            'recent': recent, 'active': active, 'seqStats': stats,
            'reminder': {'enabled': bool(cfg['reminder'].get('enabled')), 'last': kv_get('reminder_last', ''), 'error': kv_get('reminder_error', '')},
        }
    return 200, res


ROUTES = {
    ('GET', '/api/health'): api_health,
    ('GET', '/api/state'): api_state_get,
    ('PUT', '/api/state'): api_state_put,
    ('GET', '/api/config'): api_config_get,
    ('PUT', '/api/config'): api_config_put,
    ('POST', '/api/test'): api_test,
    ('POST', '/api/queue'): api_queue,
    ('POST', '/api/enroll'): api_enroll,
    ('POST', '/api/enroll/stop'): api_enroll_stop,
    ('POST', '/api/outbox/cancel'): api_outbox_cancel,
    ('POST', '/api/outbox/retry'): api_outbox_retry,
    ('GET', '/api/poll'): api_poll,
    ('GET', '/api/lan'): api_lan,
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):
        if '/api/poll' not in self.path and '/api/state' not in self.path:
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def translate_path(self, path):
        # The ERP loads ../VCCI/VCCI_Data/data.js; expose only that file and the ad scans, read-only.
        p = unquote(urlparse(path).path)
        if p.startswith('/VCCI/VCCI_Data/'):
            rel = p[len('/VCCI/VCCI_Data/'):]
            if rel == 'data.js' or re.fullmatch(r'AdsData/[\w.\- ]+\.(?:jpe?g|png|gif|webp)', rel, re.I):
                return os.path.join(VCCI_DIR, *rel.split('/'))
            return os.path.join(HERE, '__not_served__')
        return super().translate_path(path)

    # ---- phone access (--lan): everyone except this computer needs the access key ----
    def is_local(self):
        return self.client_address[0] in ('127.0.0.1', '::1', '::ffff:127.0.0.1')

    def has_key(self):
        if self.is_local():
            return True
        if not LAN['on']:
            return False
        for part in self.headers.get('Cookie', '').split(';'):
            n, _, v = part.strip().partition('=')
            if n == 'sq_key' and hmac.compare_digest(v.encode(), LAN['key'].encode()):
                return True
        return False

    def gate(self):
        """True if the request was answered here (key accepted → redirect, or key needed → sign-in page)."""
        if self.has_key():
            return False
        u = urlparse(self.path)
        k = (parse_qs(u.query).get('key') or [''])[0].strip()
        if LAN['on'] and k and hmac.compare_digest(k.encode(), LAN['key'].encode()):
            self.send_response(302)
            self.send_header('Set-Cookie', 'sq_key=%s; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict' % LAN['key'])
            self.send_header('Location', u.path or '/')
            self.end_headers()
            return True
        if u.path.startswith('/api/'):
            self.reply(401, {'error': 'access key needed'})
            return True
        bad = '<p style="color:#ef4444">That key is not right.</p>' if k else ''
        page = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>SalesQuest</title><body style="font-family:system-ui;background:#0a0c18;color:#eceeff;display:grid;place-items:center;min-height:90vh;margin:0">'
                '<form style="width:min(340px,90vw)"><h2>SalesQuest</h2><p style="color:#8d93c9">Enter the access key shown in Settings on the computer.</p>%s'
                '<input name="key" autocomplete="off" autocapitalize="off" style="width:100%%;box-sizing:border-box;padding:12px;font-size:17px;border-radius:10px;border:1px solid #323a72;background:#141833;color:#eceeff">'
                '<button style="margin-top:12px;width:100%%;padding:12px;font-size:16px;border:0;border-radius:10px;background:#7c5cff;color:#fff">Open</button></form>' % bad).encode()
        self.send_response(401)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(page)))
        self.end_headers()
        self.wfile.write(page)
        return True

    def refuse(self):
        # Only answer requests addressed to this machine by name (blocks DNS-rebinding tricks).
        if self.headers.get('Host', '') not in ALLOWED_HOSTS:
            self.send_error(403, 'Forbidden host')
            return True
        p = unquote(urlparse(self.path).path).lower()
        if p.startswith('/salesquest_data') or '/.' in p or p.endswith(('.py', '.cmd', '.db', '.json', '.tmp')):
            self.send_error(404)
            return True
        return False

    def do_GET(self):
        if self.headers.get('Host', '') in ALLOWED_HOSTS and self.gate():
            return
        if self.path.startswith('/api/'):
            return self.api('GET')
        if not self.refuse():
            super().do_GET()

    def do_HEAD(self):
        if not self.has_key():
            return self.send_error(401)
        if not self.path.startswith('/api/') and not self.refuse():
            super().do_HEAD()

    def do_POST(self):
        if not self.has_key():
            return self.reply(401, {'error': 'access key needed'})
        self.api('POST')

    def do_PUT(self):
        if not self.has_key():
            return self.reply(401, {'error': 'access key needed'})
        self.api('PUT')

    def reply(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def api(self, method):
        # The custom header can't be sent cross-site without a CORS preflight, which we never
        # approve, so other web pages can't drive the API from the browser.
        if self.headers.get('Host', '') not in ALLOWED_HOSTS or self.headers.get('X-SalesQuest') != '1':
            return self.reply(403, {'error': 'forbidden'})
        if not self.has_key():
            return self.reply(401, {'error': 'access key needed'})
        u = urlparse(self.path)
        if u.path == '/api/lan' and not self.is_local():   # the key is only shown on this computer
            return self.reply(403, {'error': 'forbidden'})
        fn = ROUTES.get((method, u.path))
        if not fn:
            return self.reply(404, {'error': 'unknown endpoint'})
        try:
            body = {}
            if method in ('POST', 'PUT'):
                n = int(self.headers.get('Content-Length') or 0)
                if n > MAX_BODY:
                    return self.reply(413, {'error': 'request too large'})
                body = json.loads(self.rfile.read(n) or b'{}')
            code, obj = fn(parse_qs(u.query), body)
            self.reply(code, obj)
        except (ValueError, KeyError, TypeError) as ex:
            self.reply(400, {'error': str(ex)})
        except Exception as ex:
            traceback.print_exc()
            self.reply(500, {'error': '%s: %s' % (type(ex).__name__, ex)})


PORT = 8090


def main():
    global con, PORT
    ap = argparse.ArgumentParser(description='SalesQuest automation backend')
    ap.add_argument('--port', type=int, default=8090)
    ap.add_argument('--open', action='store_true', help='open the ERP in the browser')
    ap.add_argument('--lan', action='store_true', help='also allow phones on the same Wi-Fi (with the access key)')
    a = ap.parse_args()
    PORT = a.port
    ALLOWED_HOSTS.update({'127.0.0.1:%d' % PORT, 'localhost:%d' % PORT})
    if a.lan:
        LAN.update(on=True, key=access_key())
        ips = lan_ips()
        LAN['urls'] = ['http://%s:%d/' % (ip, PORT) for ip in ips]
        ALLOWED_HOSTS.update('%s:%d' % (ip, PORT) for ip in ips)
        try:
            ALLOWED_HOSTS.add('%s:%d' % (socket.gethostname().lower(), PORT))
        except OSError:
            pass
    con = open_db()
    try:
        srv = ThreadingHTTPServer(('0.0.0.0' if a.lan else '127.0.0.1', PORT), Handler)
    except OSError:
        print('Port %d is busy. Is SalesQuest already running? Open http://127.0.0.1:%d/' % (PORT, PORT))
        sys.exit(1)
    threading.Thread(target=worker, daemon=True).start()
    url = 'http://127.0.0.1:%d/' % PORT
    cfg = load_cfg()
    print('SalesQuest backend running at %s' % url)
    print('  WhatsApp: %s   Email: %s' % ('ready' if wa_ready(cfg) else 'not set up', 'ready' if email_ready(cfg) else 'not set up'))
    print('  Data: %s' % DATADIR)
    if LAN['on']:
        if LAN['urls']:
            print('  Phone (same Wi-Fi): %s   access key: %s' % ('  or  '.join(LAN['urls']), LAN['key']))
            print('  Or scan the QR code in SalesQuest > Settings > Open on your phone.')
        else:
            print('  Phone access is on, but no Wi-Fi / network address was found.')
    print('Keep this window open; automations stop when it is closed. Press Ctrl+C to stop.')
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('Stopped.')


if __name__ == '__main__':
    main()
