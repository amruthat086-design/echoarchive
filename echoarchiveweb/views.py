import os
import re
import difflib
from datetime import date, datetime
from functools import wraps
from urllib.parse import urlencode

import mysql.connector
from django.conf import settings
from django.contrib import messages
from django.core.files.storage import FileSystemStorage
from django.shortcuts import render, redirect
from django.urls import reverse

# Uploaded photographs and scanned documents are saved in the static images folder.
UPLOAD_DIR = os.path.join(settings.BASE_DIR, 'echoarchiveweb', 'static', 'images', 'uploads')
IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.tiff')
ALLOWED_EXT = IMAGE_EXT + ('.pdf', '.txt')
MAX_BYTES = 15 * 1024 * 1024
MEMORY_TYPES = ['story', 'photograph', 'document', 'recipe', 'festival', 'art', 'institution', 'event', 'other']


# ------------------------------------------------------------------ database helpers

def connect():
    return mysql.connector.connect(host='localhost', user='root', password='', database='echoarchive')


def query(q, p=(), one=False):
    """Run a SELECT and return rows as dictionaries."""
    mydb = connect()
    myc = mydb.cursor(dictionary=True)
    myc.execute(q, p)
    r = myc.fetchone() if one else myc.fetchall()
    mydb.close()
    return r


def run(q, p=()):
    """Run an INSERT, UPDATE or DELETE and return the new row id."""
    mydb = connect()
    myc = mydb.cursor()
    myc.execute(q, p)
    mydb.commit()
    new_id = myc.lastrowid
    mydb.close()
    return new_id


def num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def marks(items):
    return ','.join(['%s'] * len(items))


# ------------------------------------------------------------------ session and page helpers

def is_super(request):
    return request.session.get('urole') == 'superadmin'


def page(request, template, data=None, status=200):
    """Render a template with the details every page needs (who is logged in, menu links)."""
    ctx = {'me': request.session.get('uname'), 'myid': request.session.get('uid'), 'is_super': is_super(request),
           'is_mod': False, 'is_cadmin': False, 'can_add': False, 'unread': 0}
    uid = request.session.get('uid')
    if uid:
        rows = query("SELECT m.role FROM memberships m JOIN communities c ON c.cid=m.cid "
                     "WHERE m.uid=%s AND m.status='Approved' AND c.status='Active'", (uid,))
        roles = [r['role'] for r in rows]
        ctx['is_mod'] = 'moderator' in roles
        ctx['is_cadmin'] = 'communityadmin' in roles
        ctx['can_add'] = 'contributor' in roles
        ctx['unread'] = query("SELECT COUNT(*) AS n FROM notifications WHERE uid=%s AND isread=0", (uid,), one=True)['n']
    if ctx['is_super']:
        ctx['is_mod'] = True
    if data:
        ctx.update(data)
    return render(request, template, ctx, status=status)


def go(name, **q):
    url = reverse(name)
    if q:
        url += '?' + urlencode(q)
    return redirect(url)


def denied(request, text='You do not have access to this page.'):
    return page(request, 'message.html', {'title': 'Not allowed', 'text': text}, 403)


def missing(request):
    return page(request, 'message.html', {'title': 'Not found', 'text': 'That page could not be found.'}, 404)


def need_login(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if 'uid' not in request.session:
            messages.info(request, 'Please log in first.')
            return go('login')
        return view(request, *args, **kwargs)
    return wrapper


def need_super(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not is_super(request):
            return denied(request)
        return view(request, *args, **kwargs)
    return need_login(wrapper)


def role_in(uid, cid):
    r = query("SELECT role FROM memberships WHERE uid=%s AND cid=%s AND status='Approved'", (uid, cid), one=True)
    return r['role'] if r else None


def cids_with_role(uid, role):
    rows = query("SELECT m.cid FROM memberships m JOIN communities c ON c.cid=m.cid "
                 "WHERE m.uid=%s AND m.role=%s AND m.status='Approved' AND c.status='Active'", (uid, role))
    return [r['cid'] for r in rows]


def vis(request):
    """SQL condition (and values) for communities this person may read. Alias c = communities."""
    if is_super(request):
        return '1=1', []
    uid = request.session.get('uid', 0)
    return ("(c.status='Active' AND (c.visibility='public' OR EXISTS "
            "(SELECT 1 FROM memberships vm WHERE vm.cid=c.cid AND vm.uid=%s AND vm.status='Approved')))", [uid])


def log(uid, action, cid=None):
    run("INSERT INTO activity(uid,cid,action,adate) VALUES(%s,%s,%s,%s)", (uid, cid, action, datetime.now()))


def notify(uid, message, memid=None):
    run("INSERT INTO notifications(uid,memid,message,isread,ndate) VALUES(%s,%s,%s,0,%s)",
        (uid, memid, message[:255], datetime.now()))


def notify_role(cid, role, message, memid=None):
    for r in query("SELECT uid FROM memberships WHERE cid=%s AND role=%s AND status='Approved'", (cid, role)):
        notify(r['uid'], message, memid)


def notify_supers(message, memid=None):
    for r in query("SELECT uid FROM users WHERE role='superadmin' AND status='Active'"):
        notify(r['uid'], message, memid)


def snapshot(memid, editor, note):
    """Save the current wording of a memory as the next version."""
    mem = query("SELECT title, description FROM memories WHERE memid=%s", (memid,), one=True)
    last = query("SELECT COALESCE(MAX(verno),0) AS v FROM versions WHERE memid=%s", (memid,), one=True)['v']
    run("INSERT INTO versions(memid,verno,title,description,editorid,note,vdate) VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (memid, last + 1, mem['title'], mem['description'], editor, note[:255], date.today()))
    run("UPDATE memories SET version=%s WHERE memid=%s", (last + 1, memid))


# A memory with its community, category, author and first picture.
MEMSQL = ("SELECT m.*, c.cname, cat.catname, u.name AS author, "
          "(SELECT filename FROM media WHERE memid=m.memid AND isimage=1 AND kind<>'Scanned document' ORDER BY (kind='Memory image') DESC, mediaid LIMIT 1) AS thumb "
          "FROM memories m JOIN communities c ON c.cid=m.cid JOIN users u ON u.uid=m.uid "
          "LEFT JOIN categories cat ON cat.catid=m.catid ")


def with_ocr(memid):
    """Files of a memory, each with the text read from it."""
    files = query("SELECT * FROM media WHERE memid=%s ORDER BY mediaid", (memid,))
    for f in files:
        f['ocr'] = query("SELECT * FROM ocr WHERE mediaid=%s", (f['mediaid'],))
        for o in f['ocr']:
            o['text'] = o['corrected'] or o['extracted']
    return files


def can_read_memory(request, mem):
    """Approved memories follow community visibility. Others only for owner, staff and super admin."""
    uid = request.session.get('uid')
    if mem['status'] == 'Approved':
        v, vp = vis(request)
        if query("SELECT c.cid FROM communities c WHERE c.cid=%s AND " + v, [mem['cid']] + vp, one=True):
            return True
    if not uid:
        return False
    if mem['uid'] == uid or is_super(request):
        return True
    return role_in(uid, mem['cid']) in ('moderator', 'communityadmin')


def ocr_texts(ids):
    """Text read from the scanned documents of each memory, as one lower case line."""
    rows = query("SELECT md.memid, GROUP_CONCAT(COALESCE(NULLIF(o.corrected,''), o.extracted) SEPARATOR ' ') AS t "
                 "FROM media md JOIN ocr o ON o.mediaid=md.mediaid WHERE md.memid IN (" + marks(ids) + ") GROUP BY md.memid", ids)
    return {r['memid']: ' '.join((r['t'] or '').lower().split()) for r in rows}


STOPWORDS = set('a an the and or but of in on at to for from by with as is was were be been it its this that these those i we you he she '
                'they my our their his her him them me us not no so if then than very just also there here when where which who what '
                'while into over under up down out about after before during every each any all some one two only still even now '
                'never always'.split())


def word_set(text):
    return set(w for w in re.findall(r'[a-z0-9]+', (text or '').lower()) if w not in STOPWORDS and len(w) > 2)


def text_similarity(a, b):
    """0 to 1. Compares the important words, so a retelling in different order or length still scores high."""
    A, B = word_set(a), word_set(b)
    if not A or not B:
        return 0
    common = len(A & B)
    return 0.5 * common / len(A | B) + 0.5 * common / min(len(A), len(B))


def find_similar(mem, limit=6):
    """Other memories of the same community that may repeat this one (wording, scanned text, subject, place)."""
    rows = query("SELECT memid, title, description, sid, location, status FROM memories "
                 "WHERE cid=%s AND memid<>%s AND status IN ('Pending','Approved','Needs changes') LIMIT 300",
                 (mem['cid'], mem['memid']))
    scans = ocr_texts([mem['memid']] + [r['memid'] for r in rows])
    mine = (mem['title'] or '') + ' ' + (mem['description'] or '')
    found = []
    for r in rows:
        score = text_similarity(mine, (r['title'] or '') + ' ' + (r['description'] or ''))
        why = []
        if score >= 0.4:
            why.append('the wording is close')
        if scans.get(mem['memid']) and scans.get(r['memid']):
            scan_score = text_similarity(scans[mem['memid']], scans[r['memid']])
            if scan_score >= 0.4:
                why.append('the scanned text matches')
            score = max(score, scan_score)
        if mem['sid'] and r['sid'] == mem['sid']:
            score += 0.2
            why.append('same subject')
        if mem['location'] and r['location'] and mem['location'].lower() == r['location'].lower():
            score += 0.1
            why.append('same place')
        if score >= 0.4:
            r['score'] = round(min(score, 1.0) * 100, 1)
            r['why'] = ', '.join(why)
            found.append(r)
    found.sort(key=lambda x: -x['score'])
    return found[:limit]


def read_text(path, language):
    """Read the printed text in an image with Tesseract. Returns (text, error)."""
    try:
        import pytesseract
        from PIL import Image, ImageOps
        if getattr(settings, 'TESSERACT_CMD', None):
            pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD
        img = Image.open(path)
        img = ImageOps.exif_transpose(img).convert('L')
        text = pytesseract.image_to_string(img, lang=language if language in ('eng', 'mal', 'eng+mal') else 'eng')
        return text.strip(), None
    except Exception as e:
        return '', str(e)


# ------------------------------------------------------------------ public pages

def index(request):
    v, vp = vis(request)
    recent = query(MEMSQL + "WHERE m.status='Approved' AND " + v + " ORDER BY m.memid DESC LIMIT 6", vp)
    comms = query("SELECT c.*, (SELECT COUNT(*) FROM memories x WHERE x.cid=c.cid AND x.status='Approved') AS total "
                  "FROM communities c WHERE " + v + " ORDER BY total DESC LIMIT 6", vp)
    cats = query("SELECT * FROM categories WHERE cid IS NULL ORDER BY catname LIMIT 5")
    total = query("SELECT COUNT(*) AS n FROM memories m JOIN communities c ON c.cid=m.cid "
                  "WHERE m.status='Approved' AND " + v, vp, one=True)['n']
    return page(request, 'index.html', {'recent': recent, 'comms': comms, 'cats': cats, 'total': total})


def archive(request):
    v, vp = vis(request)
    where = ["m.status='Approved'", v]
    params = list(vp)
    q = request.GET.get('q', '').strip()
    if q:
        like = '%' + q + '%'
        where.append("(m.title LIKE %s OR m.description LIKE %s OR m.location LIKE %s OR m.tags LIKE %s OR EXISTS "
                     "(SELECT 1 FROM media md JOIN ocr o ON o.mediaid=md.mediaid WHERE md.memid=m.memid "
                     "AND (o.corrected LIKE %s OR o.extracted LIKE %s)))")
        params += [like] * 6
    if num(request.GET.get('community')):
        where.append("m.cid=%s")
        params.append(num(request.GET.get('community')))
    if num(request.GET.get('category')):
        where.append("m.catid=%s")
        params.append(num(request.GET.get('category')))
    loc = request.GET.get('location', '').strip()
    if loc:
        where.append("m.location LIKE %s")
        params.append('%' + loc + '%')
    if num(request.GET.get('from')) is not None:
        where.append("m.sortyear>=%s")
        params.append(num(request.GET.get('from')))
    if num(request.GET.get('to')) is not None:
        where.append("m.sortyear<=%s")
        params.append(num(request.GET.get('to')))
    cond = ' WHERE ' + ' AND '.join(where)
    total = query("SELECT COUNT(*) AS n FROM memories m JOIN communities c ON c.cid=m.cid" + cond, params, one=True)['n']
    per = 12
    pg = max(num(request.GET.get('page')) or 1, 1)
    order = " ORDER BY (m.sortyear IS NULL), m.sortyear, m.memid DESC" if request.GET.get('sort') == 'year' else " ORDER BY m.memid DESC"
    rows = query(MEMSQL + cond + order + " LIMIT %s OFFSET %s", params + [per, (pg - 1) * per])
    keep = request.GET.copy()
    keep.pop('page', None)
    comms = query("SELECT c.cid, c.cname FROM communities c WHERE " + v + " ORDER BY c.cname", vp)
    cats = query("SELECT cat.catid, cat.catname FROM categories cat LEFT JOIN communities c ON c.cid=cat.cid "
                 "WHERE cat.cid IS NULL OR (" + v + ") ORDER BY cat.catname", vp)
    return page(request, 'archive.html', {
        'rows': rows, 'total': total, 'f': request.GET, 'comms': comms, 'cats': cats,
        'pg': pg, 'pages': (total + per - 1) // per, 'has_prev': pg > 1, 'has_next': pg * per < total,
        'prev': pg - 1, 'next': pg + 1, 'keep': keep.urlencode()})


def memorydetail(request):
    mid = num(request.GET.get('id'))
    mem = query(MEMSQL + "WHERE m.memid=%s", (mid,), one=True)
    if not mem:
        return missing(request)
    if not can_read_memory(request, mem):
        if 'uid' not in request.session:
            messages.info(request, 'Please log in to see this memory.')
            return go('login')
        return denied(request)
    related = []
    links = query("SELECT l.linktype, l.note, m.memid, m.title, m.status, m.cid FROM links l "
                  "JOIN memories m ON m.memid = IF(l.memid=%s, l.otherid, l.memid) WHERE l.memid=%s OR l.otherid=%s",
                  (mid, mid, mid))
    linked_ids = []
    for l in links:
        if can_read_memory(request, dict(l, uid=0)):
            related.append(l)
            linked_ids.append(l['memid'])
    same = []
    if mem['sid']:
        for r in query("SELECT memid, title, cid, status FROM memories WHERE sid=%s AND memid<>%s AND status='Approved'", (mem['sid'], mid)):
            if r['memid'] not in linked_ids and can_read_memory(request, dict(r, uid=0)):
                same.append(r)
    uid = request.session.get('uid')
    mine_role = role_in(uid, mem['cid']) if uid else None
    subject = query("SELECT * FROM subjects WHERE sid=%s", (mem['sid'],), one=True) if mem['sid'] else None
    tags = [t.strip() for t in (mem['tags'] or '').split(',') if t.strip()]
    files = with_ocr(mid)
    return page(request, 'memorydetail.html', {
        'mem': mem, 'photos': [f for f in files if f['kind'] == 'Memory image'],
        'locphotos': [f for f in files if f['kind'] == 'Location image'],
        'doc_texts': [o['text'] for f in files if f['kind'] == 'Scanned document' for o in f['ocr'] if o['text']],
        'related': related, 'same': same, 'subject': subject, 'tags': tags,
        'can_suggest': mem['status'] == 'Approved' and mine_role == 'contributor',
        'can_report': mem['status'] == 'Approved' and mine_role == 'contributor',
        'is_owner': uid == mem['uid'], 'editable': uid == mem['uid'] and mem['status'] in ('Draft', 'Needs changes')})


def timeline(request):
    v, vp = vis(request)
    rows = query(MEMSQL + "WHERE m.status='Approved' AND m.sortyear IS NOT NULL AND " + v + " ORDER BY m.sortyear, m.title", vp)
    groups = []
    for r in rows:
        decade = (r['sortyear'] // 10) * 10
        if not groups or groups[-1]['decade'] != decade:
            groups.append({'decade': decade, 'items': []})
        groups[-1]['items'].append(r)
    undated = query("SELECT COUNT(*) AS n FROM memories m JOIN communities c ON c.cid=m.cid "
                    "WHERE m.status='Approved' AND m.sortyear IS NULL AND " + v, vp, one=True)['n']
    return page(request, 'timeline.html', {'groups': groups, 'undated': undated})


def communities(request):
    v, vp = vis(request)
    rows = query("SELECT c.*, (SELECT COUNT(*) FROM memories x WHERE x.cid=c.cid AND x.status='Approved') AS total, "
                 "(SELECT status FROM memberships WHERE cid=c.cid AND uid=%s) AS mystatus "
                 "FROM communities c WHERE " + v + " ORDER BY c.cname", [request.session.get('uid', 0)] + vp)
    return page(request, 'communities.html', {'rows': rows})


def communitydetail(request):
    cid = num(request.GET.get('id'))
    v, vp = vis(request)
    c = query("SELECT c.* FROM communities c WHERE c.cid=%s AND " + v, [cid] + vp, one=True)
    if not c:
        return missing(request)
    uid = request.session.get('uid', 0)
    mem = query("SELECT * FROM memberships WHERE uid=%s AND cid=%s", (uid, cid), one=True)
    recent = query(MEMSQL + "WHERE m.status='Approved' AND m.cid=%s ORDER BY m.memid DESC LIMIT 6", (cid,))
    total = query("SELECT COUNT(*) AS n FROM memories WHERE cid=%s AND status='Approved'", (cid,), one=True)['n']
    return page(request, 'communitydetail.html', {
        'c': c, 'mem': mem, 'recent': recent, 'total': total,
        'subjects': query("SELECT * FROM subjects WHERE cid=%s ORDER BY sname LIMIT 12", (cid,)),
        'cats': query("SELECT * FROM categories WHERE cid IS NULL OR cid=%s ORDER BY catname", (cid,))})


def subjectdetail(request):
    sid = num(request.GET.get('id'))
    s = query("SELECT * FROM subjects WHERE sid=%s", (sid,), one=True)
    if not s:
        return missing(request)
    v, vp = vis(request)
    if not query("SELECT c.cid FROM communities c WHERE c.cid=%s AND " + v, [s['cid']] + vp, one=True):
        return denied(request)
    rows = query(MEMSQL + "WHERE m.status='Approved' AND m.sid=%s ORDER BY m.sortyear, m.memid", (sid,))
    cname = query("SELECT cname FROM communities WHERE cid=%s", (s['cid'],), one=True)['cname']
    return page(request, 'subjectdetail.html', {'s': s, 'rows': rows, 'cname': cname})


# ------------------------------------------------------------------ register, login, logout

def login(request):
    return page(request, 'login.html')


def register(request):
    return page(request, 'register.html')


def registerhandler(request):
    if request.method != 'POST':
        return go('register')
    name = request.POST.get('name', '').strip()
    uname = request.POST.get('uname', '').strip()
    email = request.POST.get('email', '').strip()
    phone = request.POST.get('phone', '').strip()
    pas = request.POST.get('pas', '')
    pas2 = request.POST.get('pas2', '')
    if not name or not uname or not pas:
        return page(request, 'register.html', {'msg': 'Please fill in your name, username and password.', 'old': request.POST})
    if pas != pas2:
        return page(request, 'register.html', {'msg': 'The two passwords do not match.', 'old': request.POST})
    if query("SELECT uid FROM users WHERE uname=%s", (uname,), one=True):
        return page(request, 'register.html', {'msg': 'That username is taken. Please choose another.', 'old': request.POST})
    run("INSERT INTO users(name,uname,pas,email,phone,role,status,regdate) VALUES(%s,%s,%s,%s,%s,'member','Active',%s)",
        (name, uname, pas, email, phone, date.today()))
    return page(request, 'login.html', {'msg': 'Your account is ready. Log in to join a community.'})


def loginhandler(request):
    if request.method != 'POST':
        return go('login')
    uname = request.POST.get('uname', '').strip()
    pas = request.POST.get('pas', '')
    r = query("SELECT * FROM users WHERE uname=%s AND pas=%s", (uname, pas), one=True)
    if r is None:
        return page(request, 'login.html', {'msg': 'Invalid username or password.'})
    if r['status'] != 'Active':
        return page(request, 'login.html', {'msg': 'This account has been blocked. Please contact the platform admin.'})
    request.session['uid'] = r['uid']
    request.session['uname'] = r['name']
    request.session['urole'] = r['role']
    log(r['uid'], 'Logged in')
    return go('superhome') if r['role'] == 'superadmin' else go('dashboard')


def logout(request):
    request.session.flush()
    return go('index')


# ------------------------------------------------------------------ members: dashboard, joining, notifications

@need_login
def dashboard(request):
    uid = request.session['uid']
    return page(request, 'dashboard.html', {
        'memberships': query("SELECT m.*, c.cname FROM memberships m JOIN communities c ON c.cid=m.cid "
                             "WHERE m.uid=%s ORDER BY m.mid DESC", (uid,)),
        'memories': query("SELECT m.*, c.cname FROM memories m JOIN communities c ON c.cid=m.cid "
                          "WHERE m.uid=%s ORDER BY m.memid DESC", (uid,)),
        'notes': query("SELECT * FROM notifications WHERE uid=%s ORDER BY nid DESC LIMIT 5", (uid,))})


@need_login
def notifications(request):
    rows = query("SELECT * FROM notifications WHERE uid=%s ORDER BY nid DESC LIMIT 60", (request.session['uid'],))
    return page(request, 'notifications.html', {'items': rows, 'unread_count': sum(1 for r in rows if not r['isread'])})


@need_login
def notificationsread(request):
    run("UPDATE notifications SET isread=1 WHERE uid=%s", (request.session['uid'],))
    return go('notifications')


@need_login
def joinhandler(request):
    uid = request.session['uid']
    cid = num(request.POST.get('cid'))
    if is_super(request):
        messages.info(request, 'The super admin can open every community without joining.')
        return go('communitydetail', id=cid or 0)
    v, vp = vis(request)
    c = query("SELECT c.* FROM communities c WHERE c.cid=%s AND c.status='Active' AND " + v, [cid] + vp, one=True)
    if not c:
        return denied(request)
    old = query("SELECT * FROM memberships WHERE uid=%s AND cid=%s", (uid, cid), one=True)
    if old is None:
        run("INSERT INTO memberships(uid,cid,role,status,reqdate) VALUES(%s,%s,'contributor','Pending',%s)", (uid, cid, date.today()))
        notify_role(cid, 'communityadmin', request.session['uname'] + ' asked to join ' + c['cname'] + '.')
        log(uid, 'Asked to join a community', cid)
        messages.success(request, 'Request sent. A community admin will approve it soon.')
    elif old['status'] == 'Pending':
        messages.info(request, 'Your request is still waiting for approval.')
    elif old['status'] == 'Approved':
        messages.info(request, 'You are already a member of this community.')
    else:
        messages.error(request, 'Your earlier request was declined. Contact a community admin to ask again.')
    return go('communitydetail', id=cid)


# ------------------------------------------------------------------ members: adding and editing memories

def form_lists(cids):
    """Categories and subjects a contributor may choose from, for the given communities."""
    if not cids:
        return [], [], []
    comms = query("SELECT cid, cname FROM communities WHERE cid IN (" + marks(cids) + ") ORDER BY cname", cids)
    cats = query("SELECT cat.*, c.cname FROM categories cat LEFT JOIN communities c ON c.cid=cat.cid "
                 "WHERE cat.cid IS NULL OR cat.cid IN (" + marks(cids) + ") ORDER BY cat.catname", cids)
    subs = query("SELECT s.*, c.cname FROM subjects s JOIN communities c ON c.cid=s.cid "
                 "WHERE s.cid IN (" + marks(cids) + ") ORDER BY s.sname", cids)
    return comms, cats, subs


def member_cids(uid):
    rows = query("SELECT m.cid FROM memberships m JOIN communities c ON c.cid=m.cid "
                 "WHERE m.uid=%s AND m.role='contributor' AND m.status='Approved' AND c.status='Active'", (uid,))
    return [r['cid'] for r in rows]


@need_login
def memoryadd(request):
    cids = member_cids(request.session['uid'])
    if not cids:
        messages.info(request, 'Only contributors can add memories. Ask to join a community, and once its admin approves you, you can add memories.')
        return go('communities')
    comms, cats, subs = form_lists(cids)
    return page(request, 'memoryadd.html', {'comms': comms, 'cats': cats, 'subs': subs, 'types': MEMORY_TYPES})


def clean_tags(text):
    names = []
    for t in (text or '').replace('\n', ',').split(','):
        t = t.strip().lower()[:50]
        if t and t not in names:
            names.append(t)
    return ', '.join(names)


def save_scan(request, mem, f, language, caption=''):
    """Save an image of text with the memory and read its text with Tesseract (checked later by the contributor)."""
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in IMAGE_EXT:
        messages.error(request, f.name + ': the image of the text must be JPG, PNG or TIFF.')
        return None
    if f.size > MAX_BYTES:
        messages.error(request, f.name + ': files must be 15 MB or smaller.')
        return None
    fs = FileSystemStorage(location=UPLOAD_DIR)
    saved = fs.save('m%s_%s' % (mem['memid'], re.sub(r'[^A-Za-z0-9._-]', '_', f.name)), f)
    mediaid = run("INSERT INTO media(memid,filename,kind,caption,isimage,mdate) VALUES(%s,%s,'Scanned document',%s,1,%s)",
                  (mem['memid'], saved, caption, date.today()))
    text, err = read_text(os.path.join(UPLOAD_DIR, saved), language)
    run("INSERT INTO ocr(mediaid,language,extracted,corrected,reviewed) VALUES(%s,%s,%s,%s,0)", (mediaid, language, text, text))
    if err:
        messages.warning(request, 'Could not read text from ' + f.name + '. You can type it in below. (' + err[:120] + ')')
    elif not text:
        messages.warning(request, 'No text could be read from ' + f.name + '. You can type it in below.')
    return bool(text)


def save_photo(request, mem, f, caption='', kind='Memory image'):
    """Save a photograph of the memory itself (for example the sadya) or of its place."""
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in IMAGE_EXT or f.size > MAX_BYTES:
        messages.error(request, f.name + ': photos must be JPG, PNG or TIFF and 15 MB or smaller.')
        return False
    saved = FileSystemStorage(location=UPLOAD_DIR).save('m%s_%s' % (mem['memid'], re.sub(r'[^A-Za-z0-9._-]', '_', f.name)), f)
    run("INSERT INTO media(memid,filename,kind,caption,isimage,mdate) VALUES(%s,%s,%s,%s,1,%s)",
        (mem['memid'], saved, kind, caption, date.today()))
    return True


def check_choices(uid, cid, catid, sid):
    """True when the community, category and subject belong together and the user is a member."""
    if cid not in member_cids(uid):
        return False
    if catid and not query("SELECT catid FROM categories WHERE catid=%s AND (cid IS NULL OR cid=%s)", (catid, cid), one=True):
        return False
    if sid and not query("SELECT sid FROM subjects WHERE sid=%s AND cid=%s", (sid, cid), one=True):
        return False
    return True


@need_login
def memoryaddhandler(request):
    if request.method != 'POST':
        return go('memoryadd')
    uid = request.session['uid']
    cid, catid, sid = num(request.POST.get('cid')), num(request.POST.get('catid')), num(request.POST.get('sid'))
    title = request.POST.get('title', '').strip()
    desc = request.POST.get('description', '').strip()
    scans = request.FILES.getlist('textimage')
    photos = request.FILES.getlist('photos')
    language = request.POST.get('language', 'eng+mal')
    if not title or (not desc and not scans) or not catid or not check_choices(uid, cid, catid, sid):
        messages.error(request, 'Please give a title, a category, and either a description or an image of the text, and choose from your own communities.')
        return go('memoryadd')
    memid = run("INSERT INTO memories(cid,uid,catid,sid,title,description,mtype,approxdate,sortyear,location,district,landmark,people,source,tags,status,version,mdate) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Draft',1,%s)",
                (cid, uid, catid, sid, title, desc, request.POST.get('mtype', 'story'), request.POST.get('approxdate', '').strip(),
                 num(request.POST.get('sortyear')), request.POST.get('location', '').strip(), request.POST.get('district', '').strip(),
                 request.POST.get('landmark', '').strip(), request.POST.get('people', '').strip(), request.POST.get('source', '').strip(),
                 clean_tags(request.POST.get('tags')), date.today()))
    log(uid, 'Started a memory', cid)
    newmem = {'memid': memid}
    caption = request.POST.get('photocaption', '').strip()[:255]
    for p in photos:
        save_photo(request, newmem, p, caption)
    read = [save_scan(request, newmem, s, language) for s in scans]
    if any(r is True for r in read):
        messages.success(request, 'Draft saved. Check the text read from your image below, then send it for review.')
    elif any(r is False for r in read):
        messages.success(request, 'Draft saved. The image is kept, but its text must be typed in below before you send it for review.')
    else:
        messages.success(request, 'Draft saved. Add photographs or documents, then send it for review.')
    return go('memoryedit', id=memid)


def own_memory(request, memid):
    return query("SELECT * FROM memories WHERE memid=%s AND uid=%s", (memid, request.session['uid']), one=True)


@need_login
def memoryedit(request):
    memid = num(request.GET.get('id'))
    mem = own_memory(request, memid)
    if not mem:
        return missing(request)
    comms, cats, subs = form_lists([mem['cid']])
    files = with_ocr(memid)
    ocr_rows = [dict(o, filename=f['filename']) for f in files for o in f['ocr']]
    return page(request, 'memoryedit.html', {
        'mem': mem, 'editable': mem['status'] in ('Draft', 'Needs changes'), 'files': files, 'ocr_rows': ocr_rows,
        'unreviewed': sum(1 for o in ocr_rows if not o['reviewed']),
        'requests': query("SELECT * FROM corrections WHERE memid=%s AND ctype='Request' AND status='Open'", (memid,)),
        'cats': cats, 'subs': subs, 'types': MEMORY_TYPES, 'langs': [('eng', 'English'), ('mal', 'Malayalam'), ('eng+mal', 'English and Malayalam')]})


def editable_memory(request):
    mem = own_memory(request, num(request.POST.get('memid')))
    if mem and mem['status'] in ('Draft', 'Needs changes'):
        return mem
    return None


@need_login
def memorysavehandler(request):
    mem = editable_memory(request) if request.method == 'POST' else None
    if not mem:
        return denied(request, 'This memory cannot be edited right now.')
    catid, sid = num(request.POST.get('catid')), num(request.POST.get('sid'))
    desc = request.POST.get('description', '').strip()
    scans = request.FILES.getlist('textimage')
    has_text = query("SELECT o.ocrid FROM ocr o JOIN media md ON md.mediaid=o.mediaid WHERE md.memid=%s", (mem['memid'],))
    if (not desc and not scans and not has_text) or not catid or not check_choices(request.session['uid'], mem['cid'], catid, sid):
        messages.error(request, 'Please give a category, and either a description or an image of the text.')
        return go('memoryedit', id=mem['memid'])
    # The title is never changed here, so the memory keeps its name.
    run("UPDATE memories SET catid=%s,sid=%s,description=%s,mtype=%s,approxdate=%s,sortyear=%s,location=%s,"
        "district=%s,landmark=%s,people=%s,source=%s,tags=%s WHERE memid=%s",
        (catid, sid, desc, request.POST.get('mtype', 'story'), request.POST.get('approxdate', '').strip(),
         num(request.POST.get('sortyear')), request.POST.get('location', '').strip(), request.POST.get('district', '').strip(),
         request.POST.get('landmark', '').strip(), request.POST.get('people', '').strip(), request.POST.get('source', '').strip(),
         clean_tags(request.POST.get('tags')), mem['memid']))
    language = request.POST.get('language', 'eng+mal')
    caption = request.POST.get('photocaption', '').strip()[:255]
    placecaption = request.POST.get('placecaption', '').strip()[:255]
    for p in request.FILES.getlist('photos'):
        save_photo(request, mem, p, caption)
    for p in request.FILES.getlist('placephotos'):
        save_photo(request, mem, p, placecaption, 'Location image')
    read = [save_scan(request, mem, s, language) for s in scans]
    if any(r is True for r in read):
        messages.success(request, 'Saved. Check the text read from your image below.')
    elif any(r is False for r in read):
        messages.success(request, 'Saved. The image is kept, but its text must be typed in below.')
    else:
        messages.success(request, 'Changes saved.')
    return go('memoryedit', id=mem['memid'])


@need_login
def memoryuploadhandler(request):
    mem = editable_memory(request) if request.method == 'POST' else None
    if not mem:
        return denied(request, 'This memory cannot be edited right now.')
    files = request.FILES.getlist('files')
    if not files:
        messages.error(request, 'Choose at least one file to upload.')
        return go('memoryedit', id=mem['memid'])
    kind = request.POST.get('kind')
    if kind not in ('Memory image', 'Location image', 'Scanned document'):
        kind = 'Memory image'
    caption = request.POST.get('caption', '').strip()[:255]
    language = request.POST.get('language', 'eng+mal')
    want_ocr = request.POST.get('runocr') == 'on'
    fs = FileSystemStorage(location=UPLOAD_DIR)
    added = 0
    for f in files:
        ext = os.path.splitext(f.name)[1].lower()
        if ext not in ALLOWED_EXT:
            messages.error(request, f.name + ': this file type is not supported. Use JPG, PNG, TIFF, PDF or TXT.')
            continue
        if f.size > MAX_BYTES:
            messages.error(request, f.name + ': files must be 15 MB or smaller.')
            continue
        safe = re.sub(r'[^A-Za-z0-9._-]', '_', f.name)
        saved = fs.save('m%s_%s' % (mem['memid'], safe), f)
        is_image = 1 if ext in IMAGE_EXT else 0
        this_kind = kind if is_image else 'Scanned document'
        mediaid = run("INSERT INTO media(memid,filename,kind,caption,isimage,mdate) VALUES(%s,%s,%s,%s,%s,%s)",
                      (mem['memid'], saved, this_kind, caption, is_image, date.today()))
        added += 1
        if want_ocr and is_image and this_kind == 'Scanned document':
            text, err = read_text(os.path.join(UPLOAD_DIR, saved), language)
            run("INSERT INTO ocr(mediaid,language,extracted,corrected,reviewed) VALUES(%s,%s,%s,%s,0)", (mediaid, language, text, text))
            if err:
                messages.warning(request, 'Could not read text from ' + f.name + '. You can type it in below. (' + err[:120] + ')')
    if added:
        messages.success(request, 'Files added. Check any text that was read from your images before sending.')
    return go('memoryedit', id=mem['memid'])


@need_login
def ocrsavehandler(request):
    mem = editable_memory(request) if request.method == 'POST' else None
    if not mem:
        return denied(request, 'This memory cannot be edited right now.')
    rows = query("SELECT o.ocrid FROM ocr o JOIN media md ON md.mediaid=o.mediaid WHERE md.memid=%s", (mem['memid'],))
    for r in rows:
        key = 'ocr_%s' % r['ocrid']
        if key in request.POST:
            run("UPDATE ocr SET corrected=%s, reviewed=1 WHERE ocrid=%s", (request.POST.get(key, '').strip(), r['ocrid']))
    messages.success(request, 'Text saved.')
    return go('memoryedit', id=mem['memid'])


@need_login
def mediadeletehandler(request):
    mem = editable_memory(request) if request.method == 'POST' else None
    if not mem:
        return denied(request, 'This memory cannot be edited right now.')
    f = query("SELECT * FROM media WHERE mediaid=%s AND memid=%s", (num(request.POST.get('mediaid')), mem['memid']), one=True)
    if f:
        run("DELETE FROM media WHERE mediaid=%s", (f['mediaid'],))
        try:
            os.remove(os.path.join(UPLOAD_DIR, f['filename']))
        except OSError:
            pass
        messages.success(request, 'File removed.')
    return go('memoryedit', id=mem['memid'])


@need_login
def memorysubmithandler(request):
    mem = editable_memory(request) if request.method == 'POST' else None
    if not mem:
        return denied(request, 'This memory cannot be sent right now.')
    uid = request.session['uid']
    if query("SELECT o.ocrid FROM ocr o JOIN media md ON md.mediaid=o.mediaid WHERE md.memid=%s AND o.reviewed=0", (mem['memid'],)):
        messages.error(request, 'Check the text read from your images and save it before sending.')
        return go('memoryedit', id=mem['memid'])
    resubmit = mem['status'] == 'Needs changes'
    run("UPDATE memories SET status='Pending' WHERE memid=%s", (mem['memid'],))
    snapshot(mem['memid'], uid, 'Resubmitted after changes' if resubmit else 'First submission')
    run("UPDATE corrections SET status='Resolved' WHERE memid=%s AND ctype='Request' AND status='Open'", (mem['memid'],))
    notify_role(mem['cid'], 'moderator', 'New memory to review: "' + mem['title'] + '".', mem['memid'])
    log(uid, 'Sent a memory for review', mem['cid'])
    messages.success(request, 'Sent for review. You will be notified when a moderator decides.')
    return go('dashboard')


# ------------------------------------------------------------------ members: corrections and reports

def readable_approved(request, memid):
    mem = query(MEMSQL + "WHERE m.memid=%s AND m.status='Approved'", (memid,), one=True)
    if mem and can_read_memory(request, mem):
        return mem
    return None


@need_login
def suggest(request):
    mem = readable_approved(request, num(request.GET.get('id')))
    if not mem:
        return missing(request)
    if role_in(request.session['uid'], mem['cid']) != 'contributor':
        messages.info(request, 'Only contributors of this community can suggest corrections.')
        return go('communitydetail', id=mem['cid'])
    return page(request, 'suggest.html', {'mem': mem})


@need_login
def suggesthandler(request):
    mem = readable_approved(request, num(request.POST.get('memid')))
    uid = request.session['uid']
    details = request.POST.get('details', '').strip()
    if not mem or role_in(uid, mem['cid']) != 'contributor' or not details:
        messages.error(request, 'Please describe what should change.')
        return go('memorydetail', id=num(request.POST.get('memid')) or 0)
    run("INSERT INTO corrections(memid,byid,ctype,details,proposed,status,cdate) VALUES(%s,%s,'Suggestion',%s,%s,'Open',%s)",
        (mem['memid'], uid, details, request.POST.get('proposed', '').strip(), date.today()))
    notify_role(mem['cid'], 'moderator', 'Correction suggested for "' + mem['title'] + '".', mem['memid'])
    log(uid, 'Suggested a correction', mem['cid'])
    messages.success(request, 'Thank you. A moderator will review your suggestion.')
    return go('memorydetail', id=mem['memid'])


@need_login
def report(request):
    mem = readable_approved(request, num(request.GET.get('id')))
    if not mem:
        return missing(request)
    if role_in(request.session['uid'], mem['cid']) != 'contributor':
        return denied(request, 'Only contributors of this community can report a memory.')
    return page(request, 'report.html', {'mem': mem})


@need_login
def reporthandler(request):
    mem = readable_approved(request, num(request.POST.get('memid')))
    reason = request.POST.get('reason', '').strip()
    if not mem or not reason or role_in(request.session['uid'], mem['cid']) != 'contributor':
        messages.error(request, 'Please say what the problem is. Only contributors of this community can report.')
        return go('memorydetail', id=num(request.POST.get('memid')) or 0)
    run("INSERT INTO reports(memid,byid,reason,status,rdate) VALUES(%s,%s,%s,'Open',%s)",
        (mem['memid'], request.session['uid'], reason, date.today()))
    notify_supers('A memory was reported: "' + mem['title'] + '".', mem['memid'])
    log(request.session['uid'], 'Reported a memory', mem['cid'])
    messages.success(request, 'Report sent. The platform team will look into it.')
    return go('memorydetail', id=mem['memid'])


# ------------------------------------------------------------------ moderators

def mod_cids(request):
    if is_super(request):
        return [r['cid'] for r in query("SELECT cid FROM communities")]
    return cids_with_role(request.session['uid'], 'moderator')


@need_login
def modqueue(request):
    cids = mod_cids(request)
    if not cids:
        return denied(request)
    pending = query(MEMSQL + "WHERE m.status='Pending' AND m.cid IN (" + marks(cids) + ") ORDER BY m.memid", cids)
    waiting = query(MEMSQL + "WHERE m.status='Needs changes' AND m.cid IN (" + marks(cids) + ") ORDER BY m.memid", cids)
    suggestions = query("SELECT COUNT(*) AS n FROM corrections co JOIN memories m ON m.memid=co.memid "
                        "WHERE co.ctype='Suggestion' AND co.status='Open' AND m.cid IN (" + marks(cids) + ")", cids, one=True)['n']
    comms = query("SELECT cname FROM communities WHERE cid IN (" + marks(cids) + ")", cids)
    return page(request, 'modqueue.html', {'pending': pending, 'waiting': waiting, 'suggestions': suggestions, 'comms': comms})


def mod_memory(request, memid):
    mem = query(MEMSQL + "WHERE m.memid=%s", (memid,), one=True)
    if mem and mem['cid'] in mod_cids(request):
        return mem
    return None


@need_login
def modreview(request):
    memid = num(request.GET.get('id'))
    mem = mod_memory(request, memid)
    if not mem:
        return denied(request)
    links = query("SELECT l.*, m.memid AS othermem, m.title AS othertitle FROM links l "
                  "JOIN memories m ON m.memid = IF(l.memid=%s, l.otherid, l.memid) WHERE l.memid=%s OR l.otherid=%s",
                  (memid, memid, memid))
    subject = query("SELECT sname FROM subjects WHERE sid=%s", (mem['sid'],), one=True) if mem['sid'] else None
    return page(request, 'modreview.html', {
        'mem': mem, 'reviewable': mem['status'] == 'Pending', 'files': with_ocr(memid), 'subject': subject,
        'similar': find_similar(mem), 'links': links,
        'history': query("SELECT v.*, u.name AS modname FROM verifications v JOIN users u ON u.uid=v.modid WHERE v.memid=%s ORDER BY v.vid DESC", (memid,)),
        'others': query("SELECT memid, title FROM memories WHERE cid=%s AND memid<>%s AND status IN ('Pending','Approved','Needs changes') ORDER BY title",
                        (mem['cid'], memid))})


@need_login
def moddecisionhandler(request):
    mem = mod_memory(request, num(request.POST.get('memid'))) if request.method == 'POST' else None
    if not mem or mem['status'] != 'Pending':
        return denied(request)
    decision = request.POST.get('decision')
    reason = request.POST.get('reason', '').strip()
    if decision not in ('Approved', 'Rejected', 'Needs changes', 'Evidence requested'):
        messages.error(request, 'Please choose a decision.')
        return go('modreview', id=mem['memid'])
    if decision != 'Approved' and not reason:
        messages.error(request, 'Please say why, so the contributor knows what to do next.')
        return go('modreview', id=mem['memid'])
    uid = request.session['uid']
    run("INSERT INTO verifications(memid,modid,decision,reason,vdate) VALUES(%s,%s,%s,%s,%s)", (mem['memid'], uid, decision, reason, date.today()))
    if decision == 'Approved':
        run("UPDATE memories SET status='Approved' WHERE memid=%s", (mem['memid'],))
        notify(mem['uid'], '"' + mem['title'] + '" was approved and is now in the archive.', mem['memid'])
    elif decision == 'Rejected':
        run("UPDATE memories SET status='Rejected' WHERE memid=%s", (mem['memid'],))
        notify(mem['uid'], '"' + mem['title'] + '" was not accepted: ' + reason, mem['memid'])
    else:
        run("UPDATE memories SET status='Needs changes' WHERE memid=%s", (mem['memid'],))
        prefix = 'Evidence requested: ' if decision == 'Evidence requested' else ''
        run("INSERT INTO corrections(memid,byid,ctype,details,proposed,status,cdate) VALUES(%s,%s,'Request',%s,'','Open',%s)",
            (mem['memid'], uid, prefix + reason, date.today()))
        notify(mem['uid'], 'Changes requested for "' + mem['title'] + '": ' + reason, mem['memid'])
    log(uid, 'Decision: ' + decision, mem['cid'])
    messages.success(request, 'Decision recorded.')
    return go('modqueue')


@need_login
def modlinkhandler(request):
    mem = mod_memory(request, num(request.POST.get('memid'))) if request.method == 'POST' else None
    if not mem:
        return denied(request)
    other = query("SELECT * FROM memories WHERE memid=%s AND cid=%s AND memid<>%s", (num(request.POST.get('otherid')), mem['cid'], mem['memid']), one=True)
    linktype = request.POST.get('linktype')
    if not other or linktype not in ('Duplicate', 'Related', 'Conflicting'):
        messages.error(request, 'Choose another memory from this community and say how they are connected.')
        return go('modreview', id=mem['memid'])
    try:
        score = float(request.POST.get('score')) if request.POST.get('score') else None
    except ValueError:
        score = None
    run("INSERT INTO links(memid,otherid,linktype,note,score,byid,ldate) VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (mem['memid'], other['memid'], linktype, request.POST.get('note', '').strip(), score, request.session['uid'], date.today()))
    if request.POST.get('samesubject') == 'on':
        sid = mem['sid'] or other['sid']
        if not sid:
            sid = run("INSERT INTO subjects(cid,sname,description) VALUES(%s,%s,'')", (mem['cid'], mem['title'][:150]))
        run("UPDATE memories SET sid=%s WHERE memid IN (%s,%s)", (sid, mem['memid'], other['memid']))
    log(request.session['uid'], 'Linked two memories', mem['cid'])
    messages.success(request, 'Memories linked. Both accounts stay in the archive.')
    return go('modreview', id=mem['memid'])


@need_login
def modduplicatehandler(request):
    """Link the memory to the one it repeats, then reject it as a duplicate."""
    mem = mod_memory(request, num(request.POST.get('memid'))) if request.method == 'POST' else None
    if not mem or mem['status'] != 'Pending':
        return denied(request)
    other = query("SELECT * FROM memories WHERE memid=%s AND cid=%s AND memid<>%s", (num(request.POST.get('otherid')), mem['cid'], mem['memid']), one=True)
    if not other:
        messages.error(request, 'Choose the memory that this one repeats.')
        return go('modreview', id=mem['memid'])
    try:
        score = float(request.POST.get('score')) if request.POST.get('score') else None
    except ValueError:
        score = None
    uid = request.session['uid']
    if other['status'] == 'Approved':
        reason = ('This repeats "' + other['title'] + '", which is already in the archive. If you know something new about it, '
                  'open that memory and suggest a correction.')
    else:
        reason = 'This repeats "' + other['title'] + '", which another contributor added. Only one of the two is kept.'
    run("INSERT INTO links(memid,otherid,linktype,note,score,byid,ldate) VALUES(%s,%s,'Duplicate',%s,%s,%s,%s)",
        (mem['memid'], other['memid'], 'Rejected as a duplicate of "' + other['title'] + '".', score, uid, date.today()))
    run("INSERT INTO verifications(memid,modid,decision,reason,vdate) VALUES(%s,%s,'Rejected',%s,%s)", (mem['memid'], uid, reason, date.today()))
    run("UPDATE memories SET status='Rejected' WHERE memid=%s", (mem['memid'],))
    notify(mem['uid'], '"' + mem['title'] + '" was not accepted: ' + reason, mem['memid'])
    log(uid, 'Rejected a memory as a duplicate', mem['cid'])
    messages.success(request, 'Rejected as a duplicate. The contributor was told which memory it repeats.')
    return go('modqueue')


@need_login
def modunlinkhandler(request):
    mem = mod_memory(request, num(request.POST.get('memid'))) if request.method == 'POST' else None
    if not mem:
        return denied(request)
    run("DELETE FROM links WHERE linkid=%s AND (memid=%s OR otherid=%s)", (num(request.POST.get('linkid')), mem['memid'], mem['memid']))
    messages.success(request, 'Link removed.')
    return go('modreview', id=mem['memid'])


@need_login
def modcorrections(request):
    cids = mod_cids(request)
    if not cids:
        return denied(request)
    items = query("SELECT co.*, m.title, u.name AS byname FROM corrections co JOIN memories m ON m.memid=co.memid "
                  "JOIN users u ON u.uid=co.byid WHERE co.ctype='Suggestion' AND co.status='Open' AND m.cid IN (" + marks(cids) + ") ORDER BY co.corrid", cids)
    return page(request, 'modcorrections.html', {'items': items})


@need_login
def modcorrectionhandler(request):
    cids = mod_cids(request)
    if request.method != 'POST' or not cids:
        return denied(request)
    item = query("SELECT co.*, m.title, m.cid FROM corrections co JOIN memories m ON m.memid=co.memid "
                 "WHERE co.corrid=%s AND co.ctype='Suggestion' AND co.status='Open'", (num(request.POST.get('corrid')),), one=True)
    if not item or item['cid'] not in cids:
        return denied(request)
    uid = request.session['uid']
    if request.POST.get('decision') == 'accept':
        if request.POST.get('apply') == '1' and (item['proposed'] or '').strip():
            run("UPDATE memories SET description=%s WHERE memid=%s", (item['proposed'].strip(), item['memid']))
            snapshot(item['memid'], uid, 'Correction accepted')
        run("UPDATE corrections SET status='Accepted' WHERE corrid=%s", (item['corrid'],))
        notify(item['byid'], 'Your correction to "' + item['title'] + '" was accepted.', item['memid'])
    else:
        run("UPDATE corrections SET status='Declined' WHERE corrid=%s", (item['corrid'],))
        notify(item['byid'], 'Your correction to "' + item['title'] + '" was not applied.', item['memid'])
    log(uid, 'Reviewed a suggested correction', item['cid'])
    messages.success(request, 'Done.')
    return go('modcorrections')


# ------------------------------------------------------------------ community admin

def admin_cids(request):
    return cids_with_role(request.session['uid'], 'communityadmin')


def can_admin(request, cid):
    return is_super(request) or cid in admin_cids(request)


@need_login
def cadmin(request):
    cid = num(request.GET.get('id'))
    mine = admin_cids(request)
    if cid is None:
        if len(mine) == 1:
            return go('cadmin', id=mine[0])
        if not mine:
            return go('supercommunities') if is_super(request) else denied(request)
        rows = query("SELECT cid, cname FROM communities WHERE cid IN (" + marks(mine) + ")", mine)
        return page(request, 'cadminlist.html', {'rows': rows})
    if not can_admin(request, cid):
        return denied(request)
    c = query("SELECT * FROM communities WHERE cid=%s", (cid,), one=True)
    if not c:
        return missing(request)
    status = request.GET.get('status', '')
    sql = "SELECT m.*, u.name AS author FROM memories m JOIN users u ON u.uid=m.uid WHERE m.cid=%s"
    params = [cid]
    if status:
        sql += " AND m.status=%s"
        params.append(status)
    counts = {r['status']: r['n'] for r in query("SELECT status, COUNT(*) AS n FROM memories WHERE cid=%s GROUP BY status", (cid,))}
    members = query("SELECT m.*, u.name, u.uname FROM memberships m JOIN users u ON u.uid=m.uid WHERE m.cid=%s AND m.status='Approved' ORDER BY m.role, u.uname", (cid,))
    return page(request, 'cadmin.html', {
        'c': c, 'status_filter': status, 'counts': counts, 'members': members,
        'requests': query("SELECT m.*, u.name, u.uname FROM memberships m JOIN users u ON u.uid=m.uid WHERE m.cid=%s AND m.status='Pending'", (cid,)),
        'cats': query("SELECT * FROM categories WHERE cid=%s ORDER BY catname", (cid,)),
        'submissions': query(sql + " ORDER BY m.memid DESC LIMIT 50", params),
        'top': query("SELECT u.name, COUNT(*) AS n FROM memories m JOIN users u ON u.uid=m.uid WHERE m.cid=%s GROUP BY u.uid, u.name ORDER BY n DESC LIMIT 5", (cid,)),
        'statuses': ['Draft', 'Pending', 'Needs changes', 'Approved', 'Rejected']})


def admin_post(request):
    """Return the community id for a POST from a community admin, or None if not allowed."""
    cid = num(request.POST.get('cid'))
    if request.method == 'POST' and cid and can_admin(request, cid):
        return cid
    return None


@need_login
def cadmindetailshandler(request):
    cid = admin_post(request)
    if not cid:
        return denied(request)
    name = request.POST.get('cname', '').strip()
    if not name:
        messages.error(request, 'The community needs a name.')
    elif query("SELECT cid FROM communities WHERE cname=%s AND cid<>%s", (name, cid), one=True):
        messages.error(request, 'Another community already uses that name.')
    else:
        vis_value = 'private' if request.POST.get('visibility') == 'private' else 'public'
        run("UPDATE communities SET cname=%s, description=%s, visibility=%s WHERE cid=%s",
            (name, request.POST.get('description', '').strip(), vis_value, cid))
        messages.success(request, 'Community details saved.')
    return go('cadmin', id=cid)


@need_login
def cadminrequesthandler(request):
    cid = admin_post(request)
    if not cid:
        return denied(request)
    m = query("SELECT * FROM memberships WHERE mid=%s AND cid=%s AND status='Pending'", (num(request.POST.get('mid')), cid), one=True)
    if m:
        approve = request.POST.get('decision') == 'approve'
        run("UPDATE memberships SET status=%s WHERE mid=%s", ('Approved' if approve else 'Rejected', m['mid']))
        cname = query("SELECT cname FROM communities WHERE cid=%s", (cid,), one=True)['cname']
        notify(m['uid'], 'Your request to join ' + cname + ' was ' + ('approved.' if approve else 'declined.'))
        log(request.session['uid'], 'Approved a member' if approve else 'Declined a member', cid)
        messages.success(request, 'Done.')
    return go('cadmin', id=cid)


@need_login
def cadminrolehandler(request):
    cid = admin_post(request)
    if not cid:
        return denied(request)
    role = request.POST.get('role')
    m = query("SELECT * FROM memberships WHERE mid=%s AND cid=%s AND status='Approved'", (num(request.POST.get('mid')), cid), one=True)
    if m and m['role'] != 'communityadmin' and role in ('contributor', 'moderator'):
        run("UPDATE memberships SET role=%s WHERE mid=%s", (role, m['mid']))
        notify(m['uid'], 'You are now a ' + role + ' in your community.')
        log(request.session['uid'], 'Changed a role to ' + role, cid)
        messages.success(request, 'Role updated.')
    return go('cadmin', id=cid)


@need_login
def cadminremovehandler(request):
    cid = admin_post(request)
    if not cid:
        return denied(request)
    m = query("SELECT * FROM memberships WHERE mid=%s AND cid=%s", (num(request.POST.get('mid')), cid), one=True)
    if m and m['role'] != 'communityadmin':
        run("DELETE FROM memberships WHERE mid=%s", (m['mid'],))
        log(request.session['uid'], 'Removed a member', cid)
        messages.success(request, 'Member removed.')
    return go('cadmin', id=cid)


@need_login
def cadmincategoryhandler(request):
    cid = admin_post(request)
    if not cid:
        return denied(request)
    name = request.POST.get('catname', '').strip()
    if name:
        run("INSERT INTO categories(catname,description,cid) VALUES(%s,%s,%s)", (name, request.POST.get('description', '').strip(), cid))
        messages.success(request, 'Category added.')
    return go('cadmin', id=cid)


# ------------------------------------------------------------------ super admin

@need_super
def superhome(request):
    def count(sql):
        return query(sql, one=True)['n']
    counts = {r['status']: r['n'] for r in query("SELECT status, COUNT(*) AS n FROM memories GROUP BY status")}
    return page(request, 'superhome.html', {
        'users': count("SELECT COUNT(*) AS n FROM users"),
        'communities_total': count("SELECT COUNT(*) AS n FROM communities"),
        'communities_active': count("SELECT COUNT(*) AS n FROM communities WHERE status='Active'"),
        'approved': counts.get('Approved', 0), 'pending': counts.get('Pending', 0),
        'requests': count("SELECT COUNT(*) AS n FROM memberships WHERE status='Pending'"),
        'reports': count("SELECT COUNT(*) AS n FROM reports WHERE status='Open'"),
        'recent': query("SELECT a.*, u.name, c.cname FROM activity a LEFT JOIN users u ON u.uid=a.uid LEFT JOIN communities c ON c.cid=a.cid ORDER BY a.aid DESC LIMIT 8")})


@need_super
def supercommunities(request):
    rows = query("SELECT c.*, (SELECT COUNT(*) FROM memberships m WHERE m.cid=c.cid AND m.status='Approved') AS members, "
                 "(SELECT COUNT(*) FROM memories x WHERE x.cid=c.cid) AS total FROM communities c ORDER BY c.cname")
    edit = query("SELECT * FROM communities WHERE cid=%s", (num(request.GET.get('id')),), one=True) if request.GET.get('id') else None
    return page(request, 'supercommunities.html', {'rows': rows, 'edit': edit})


@need_super
def supercommunityhandler(request):
    if request.method != 'POST':
        return go('supercommunities')
    cid = num(request.POST.get('cid'))
    name = request.POST.get('cname', '').strip()
    desc = request.POST.get('description', '').strip()
    visibility = 'private' if request.POST.get('visibility') == 'private' else 'public'
    status = 'Inactive' if request.POST.get('status') == 'Inactive' else 'Active'
    if not name:
        messages.error(request, 'The community needs a name.')
    elif query("SELECT cid FROM communities WHERE cname=%s AND cid<>%s", (name, cid or 0), one=True):
        messages.error(request, 'A community with that name already exists.')
    elif cid:
        run("UPDATE communities SET cname=%s, description=%s, visibility=%s, status=%s WHERE cid=%s", (name, desc, visibility, status, cid))
        log(request.session['uid'], 'Edited a community', cid)
        messages.success(request, 'Community saved.')
    else:
        new_id = run("INSERT INTO communities(cname,description,visibility,status,cdate) VALUES(%s,%s,%s,%s,%s)", (name, desc, visibility, status, date.today()))
        log(request.session['uid'], 'Created a community', new_id)
        messages.success(request, name + ' created. Assign a community admin next.')
    return go('supercommunities')


@need_super
def superadmins(request):
    return page(request, 'superadmins.html', {
        'admins': query("SELECT m.*, u.name, u.uname, c.cname FROM memberships m JOIN users u ON u.uid=m.uid JOIN communities c ON c.cid=m.cid "
                        "WHERE m.role='communityadmin' AND m.status='Approved' ORDER BY c.cname"),
        'comms': query("SELECT cid, cname FROM communities ORDER BY cname")})


@need_super
def superadminhandler(request):
    if request.method != 'POST':
        return go('superadmins')
    if request.POST.get('action') == 'remove':
        run("UPDATE memberships SET role='contributor' WHERE mid=%s AND role='communityadmin'", (num(request.POST.get('mid')),))
        messages.success(request, 'Admin rights removed. The person stays on as a contributor.')
        return go('superadmins')
    user = query("SELECT * FROM users WHERE uname=%s", (request.POST.get('uname', '').strip(),), one=True)
    cid = num(request.POST.get('cid'))
    comm = query("SELECT * FROM communities WHERE cid=%s", (cid,), one=True)
    if not user or not comm:
        messages.error(request, 'No user has that username, or the community was not chosen. Ask the person to create an account first.')
        return go('superadmins')
    old = query("SELECT mid FROM memberships WHERE uid=%s AND cid=%s", (user['uid'], cid), one=True)
    if old:
        run("UPDATE memberships SET role='communityadmin', status='Approved' WHERE mid=%s", (old['mid'],))
    else:
        run("INSERT INTO memberships(uid,cid,role,status,reqdate) VALUES(%s,%s,'communityadmin','Approved',%s)", (user['uid'], cid, date.today()))
    notify(user['uid'], 'You are now the community admin of ' + comm['cname'] + '.')
    log(request.session['uid'], 'Assigned a community admin', cid)
    messages.success(request, user['name'] + ' is now an admin of ' + comm['cname'] + '.')
    return go('superadmins')


@need_super
def supercategories(request):
    return page(request, 'supercategories.html', {
        'cats': query("SELECT cat.*, c.cname, (SELECT COUNT(*) FROM memories m WHERE m.catid=cat.catid) AS total "
                      "FROM categories cat LEFT JOIN communities c ON c.cid=cat.cid ORDER BY (cat.cid IS NOT NULL), cat.catname")})


@need_super
def supercategorydetail(request):
    cat = query("SELECT cat.*, c.cname FROM categories cat LEFT JOIN communities c ON c.cid=cat.cid WHERE cat.catid=%s", (num(request.GET.get('id')),), one=True)
    if not cat:
        return missing(request)
    rows = query("SELECT m.*, c.cname, u.name AS author FROM memories m JOIN communities c ON c.cid=m.cid JOIN users u ON u.uid=m.uid "
                 "WHERE m.catid=%s ORDER BY m.memid DESC", (cat['catid'],))
    counts = {}
    for r in rows:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    return page(request, 'supercategorydetail.html', {'cat': cat, 'rows': rows, 'counts': sorted(counts.items())})


@need_super
def supercategoryhandler(request):
    if request.method != 'POST':
        return go('supercategories')
    if request.POST.get('action') == 'delete':
        run("DELETE FROM categories WHERE catid=%s", (num(request.POST.get('catid')),))
        messages.success(request, 'Category removed. Memories that used it keep their other details.')
    elif request.POST.get('catname', '').strip():
        run("INSERT INTO categories(catname,description,cid) VALUES(%s,%s,NULL)", (request.POST.get('catname').strip(), request.POST.get('description', '').strip()))
        messages.success(request, 'Category added.')
    return go('supercategories')


@need_super
def superreports(request):
    return page(request, 'superreports.html', {
        'reports': query("SELECT r.*, m.title, c.cname, u.name AS byname FROM reports r JOIN memories m ON m.memid=r.memid "
                         "JOIN communities c ON c.cid=m.cid JOIN users u ON u.uid=r.byid ORDER BY (r.status='Open') DESC, r.repid DESC LIMIT 100")})


@need_super
def superreporthandler(request):
    if request.method != 'POST':
        return go('superreports')
    rep = query("SELECT r.*, m.title, m.uid AS owner, m.cid FROM reports r JOIN memories m ON m.memid=r.memid WHERE r.repid=%s", (num(request.POST.get('repid')),), one=True)
    if rep:
        decision = request.POST.get('decision')
        if decision == 'dismiss':
            status = 'Dismissed'
        elif decision == 'remove':
            status = 'Removed'
            run("UPDATE memories SET status='Rejected' WHERE memid=%s", (rep['memid'],))
            notify(rep['owner'], '"' + rep['title'] + '" was removed from the archive after a report.', rep['memid'])
        else:
            status = 'Reviewed'
        run("UPDATE reports SET status=%s WHERE repid=%s", (status, rep['repid']))
        log(request.session['uid'], 'Report ' + status.lower(), rep['cid'])
        messages.success(request, 'Report updated.')
    return go('superreports')


USERSQL = ("SELECT u.*, (SELECT GROUP_CONCAT(CONCAT(REPLACE(m.role,'communityadmin','community admin'),' of ',c.cname) SEPARATOR ', ') "
           "FROM memberships m JOIN communities c ON c.cid=m.cid WHERE m.uid=u.uid AND m.status='Approved') AS roles FROM users u")


@need_super
def superusers(request):
    q = request.GET.get('q', '').strip()
    if q:
        like = '%' + q + '%'
        rows = query(USERSQL + " WHERE u.name LIKE %s OR u.uname LIKE %s OR u.email LIKE %s ORDER BY u.uid LIMIT 100", (like, like, like))
    else:
        rows = query(USERSQL + " ORDER BY u.uid LIMIT 100")
    return page(request, 'superusers.html', {'rows': rows, 'q': q})


@need_super
def superuserhandler(request):
    uid = num(request.POST.get('uid')) if request.method == 'POST' else None
    if uid and uid != request.session['uid']:
        u = query("SELECT * FROM users WHERE uid=%s", (uid,), one=True)
        if u:
            new = 'Blocked' if u['status'] == 'Active' else 'Active'
            run("UPDATE users SET status=%s WHERE uid=%s", (new, uid))
            messages.success(request, u['name'] + ' is now ' + new.lower() + '.')
    return go('superusers')


@need_super
def superactivity(request):
    pg = max(num(request.GET.get('page')) or 1, 1)
    per = 40
    rows = query("SELECT a.*, u.name, c.cname FROM activity a LEFT JOIN users u ON u.uid=a.uid LEFT JOIN communities c ON c.cid=a.cid "
                 "ORDER BY a.aid DESC LIMIT %s OFFSET %s", (per + 1, (pg - 1) * per))
    return page(request, 'superactivity.html', {'rows': rows[:per], 'pg': pg, 'has_prev': pg > 1, 'has_next': len(rows) > per, 'prev': pg - 1, 'next': pg + 1})
