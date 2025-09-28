# -*- coding: utf-8 -*-
"""
telegram_watcher.py — نسخة ذكية ومشدّدة
- 100 حرف كحد أقصى، 20 كلمة، سطران
- نية + خدمة (مطابقة مرنة)
- منع الإنجليزي الصِرف
- منع كامل للروابط حتى لو كانت مجزأة على أسطر/بمسافات (t.me / wa.me / http / https / www)
- حجب قروبات الأخبار/السياسة/الترفيه/الدردشة
- فلتر لعبارة: "وين طامس يحلو : A L I" بمرونة عالية
- تكرار 5 دقائق، مانع محتالين (>6 جروبات/24 ساعة)
- فلتر المشرفين
- أول سطر: 👤 + رابط المرسل + bio
"""

import os, re, json, time, sqlite3, threading, asyncio, hashlib, requests, html
from typing import List
from telethon.sync import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
from telethon.tl.functions.users import GetFullUserRequest

# ===== إعداداتك =====
API_ID = 27365071
API_HASH = '4ab2f70c153a54c1738ba2e81e9ea822'
BOT_TOKEN = "7991348516:AAG2-wBullJmGz4h1Ob2ii5djb8bQFLjm4w"

DEFAULT_ALLOWED_RECIPIENTS = [698815776, 7052552394]
ALLOWED_IDS_FILE = "allowed_ids.json"

def _dedup_preserve_order(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            out.append(x); seen.add(x)
    return out

def _load_allowed_ids_from_file(path: str):
    try:
        if not os.path.exists(path): return []
        with open(path, "r", encoding="utf-8") as f: data = json.load(f)
        ids=[]
        if isinstance(data, list):
            for v in data:
                s=str(v)
                if s.isdigit(): ids.append(int(s))
        return _dedup_preserve_order(ids)
    except: return []

ALLOWED_RECIPIENTS = _dedup_preserve_order(
    DEFAULT_ALLOWED_RECIPIENTS + _load_allowed_ids_from_file(ALLOWED_IDS_FILE)
)

SESS_FILE, DB_PATH = "sessions.json", "seen.db"

# ===== فلاتر عامة =====
MAX_AD_LENGTH = 100
MAX_LINES = 2
MAX_WORDS = 20
DUP_WINDOW_SECONDS = 5 * 60  # 5 دقائق
ALLOWED_CHAT_USERNAMES: List[str] = []  # فارغة = راقب الكل

# ===== التطبيع العربي =====
_ARABIC_NORM_MAP = str.maketrans({"أ":"ا","إ":"ا","آ":"ا","ى":"ي","ئ":"ي","ؤ":"و","ة":"ه","ـ":""})
_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
def normalize_ar(t:str)->str:
    t=t.strip().lower()
    t=_DIACRITICS.sub("",t)
    t=t.translate(_ARABIC_NORM_MAP)
    t=re.sub(r"[^\w\s؟?]", " ", t)
    t=re.sub(r"\s+"," ",t)
    return t

def squash_repeats(t: str)->str:
    return re.sub(r"(.)\1{2,}", r"\1\1", t)

# ===== كشف لغة =====
AR_LETTER_RE = re.compile(r"[\u0600-\u06FF]")
EN_LETTER_RE = re.compile(r"[A-Za-z]")
def has_arabic(text:str)->bool: return bool(AR_LETTER_RE.search(text))
def has_english(text:str)->bool: return bool(EN_LETTER_RE.search(text))

# ===== حجب الروابط/الأرقام (مرن جداً) =====
ANY_LINK_RE   = re.compile(r"(?:https?://|www\.)\S+", re.I)
LINK_FRAG_RE1 = re.compile(r"https?://\S+(?:\s*/\S+)+", re.I)
LINK_FRAG_RE2 = re.compile(r"(?:t|wa)\s*[\.\-]\s*me\s*/\s*\S+", re.I)
HAS_T_OR_WA   = re.compile(r"(?:t|wa)\s*[\.\-]\s*me", re.I)
PHONE_RE      = re.compile(r"(?:\+?9665\d{8}|05\d{8})")

def has_any_link(txt:str)->bool:
    return bool(ANY_LINK_RE.search(txt) or LINK_FRAG_RE1.search(txt) or LINK_FRAG_RE2.search(txt) or HAS_T_OR_WA.search(txt))

# ===== حجب قروبات الأخبار/السياسة/الدردشة =====
CHAT_BLACKLIST_KEYWORDS = [
    "اخبار","أخبار","سياسه","سياسة","ترند","قناة اخبار","سياسي","اقتصاد","عاجل",
    "شات","دردشه","دردشة","سوالف","فرفشة","ضحك","ترفيه","نكت","مسابقات",
    "news","breaking","politic","politics","chat","fun","jokes","entertainment","trend",
]
def is_blacklisted_chat(chat) -> bool:
    try:
        name = (getattr(chat, "title", "") or "") + " " + (getattr(chat, "username", "") or "")
        name = name.lower()
        return any(k.lower() in name for k in CHAT_BLACKLIST_KEYWORDS)
    except:
        return False

# ===== مفردات الخدمة/النية =====
SERVICE_KEYWORDS = [
    "حل واجب","حل واجبات","واجب","واجبات","يسوي","يسوي لي","يحل","يحل لي",
    "اختبار","امتحان","كويز","كويل",
    "تقرير","تقرير تدريب","تقرير ميداني","عرض","عرض تقديمي","برزنتيشن",
    "مشروع","مشاريع","مشروع تخرج","تدريب","تدريب ميداني",
    "رياضيات","ماث","فيزياء","كيمياء","احياء","لغة عربية","لغة انجليزية",
    "برمجه","برمجة","جافا","بايثون","وورد","اكسل","باوربوينت","سيرة ذاتية","cv",
    "سكليف","سكاليف","سكلائف","عذر طبي","اعذار طبية","في صحتي","بصحتي","صحتي",
]
SERVICE_KEYWORDS = _dedup_preserve_order(SERVICE_KEYWORDS)
SERVICE_KEYWORDS_NORM = [normalize_ar(w) for w in SERVICE_KEYWORDS]

REQUEST_PATTERNS_RAW = [
    r"\b(ابغى|أبغى|ابي|أحتاج|احتاج|محتاج|ودي|بغيت)\b",
    r"\b(ممكن|لو\s*سمحت|ياليت|تكفى+|تكف+ون|فضلاً)\b",
    r"\b(مين|من)(?:\s+\S+){0,1}\s+(يعرف|فاهم|يسوي|يحل|يكتب|يساعد|يفهم|يشرح)\b",
    r"\b(اللي|الي)(?:\s+\S+){0,1}\s+(يفهم|يحل|يسوي|يشرح|يلخص)\b",
    r"\b(فيه|هل\s*في|احد|أحد|حد)(?:\s+\S+){0,1}\s+(يسوي|يحل|يكتب|يفهم|فاهم|يشرح)\b",
    r"\bعندي(?:\s+\S+){0,3}\s+(واجب|اختبار|كويز|كويل|تقرير|بحث)\b",
    r"[؟?]",
    r"\b(يحل|يسوي|يكتب|يلخص|يشرح|يفهم)\b",
]
REQUEST_REGEXES = [re.compile(p, re.IGNORECASE) for p in REQUEST_PATTERNS_RAW]
SERVICE_ACTION_VERBS_NORM = [normalize_ar(w) for w in ["يحل","يسوي","يكتب","يلخص","يشرح","يفهم"]]

_PREFIX_RE = re.compile(r"\b[وفبكل]?ال")
_DASH_RE = re.compile(r"[-_]+")

def _soft_normalize_for_service(s: str) -> str:
    s = normalize_ar(s)
    s = _DASH_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _strip_common_prefixes(token: str) -> str:
    return _PREFIX_RE.sub("", token)

def has_service_term(text_norm: str) -> bool:
    base = _soft_normalize_for_service(text_norm)
    if any(kw in base for kw in SERVICE_KEYWORDS_NORM):
        return True
    tokens = base.split()
    stripped = [_strip_common_prefixes(t) for t in tokens]
    rebuilt = " ".join(stripped)
    if any(kw in rebuilt for kw in SERVICE_KEYWORDS_NORM):
        return True
    for n in (2,3):
        for i in range(0, max(0, len(stripped)-n+1)):
            if any(" ".join(stripped[i:i+n]) in kw or kw in " ".join(stripped[i:i+n]) for kw in SERVICE_KEYWORDS_NORM):
                return True
    return False

def is_student_service_inquiry(txt: str, norm: str) -> bool:
    intent = any(rx.search(txt) for rx in REQUEST_REGEXES) or ("?" in txt) or ("؟" in txt)
    if not intent:
        intent = any(v in norm for v in SERVICE_ACTION_VERBS_NORM)
    service = has_service_term(norm)
    return intent and service

def is_meaningless_english(text: str) -> bool:
    letters = re.sub(r'[^a-zA-Z]', '', text)
    non_space = len(text.replace(" ", ""))
    if non_space == 0: return False
    ratio = len(letters) / non_space
    return ratio > 0.8 and len(text.split()) < 6

# ===== قاعدة بيانات =====
def init_db():
    c=sqlite3.connect(DB_PATH,check_same_thread=False); cur=c.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS seen_messages(chat_id INTEGER,msg_id INTEGER,PRIMARY KEY(chat_id,msg_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS seen_text_user(sender_id INTEGER,text_hash TEXT,last_ts INTEGER,PRIMARY KEY(sender_id,text_hash))")
    cur.execute("CREATE TABLE IF NOT EXISTS seen_text_global(text_hash TEXT,last_ts INTEGER,PRIMARY KEY(text_hash))")
    cur.execute("CREATE TABLE IF NOT EXISTS sender_group_spread(sender_id INTEGER,chat_id INTEGER,last_ts INTEGER,PRIMARY KEY(sender_id,chat_id))")
    c.commit(); c.close()
init_db()

def get_db_connection():
    return sqlite3.connect(DB_PATH,check_same_thread=False)

# ===== تكرار =====
def _hash_text(t): return hashlib.sha1(t.encode()).hexdigest()
def is_seen(conn,cid,mid):
    cur=conn.cursor(); cur.execute("INSERT OR IGNORE INTO seen_messages VALUES(?,?)",(cid,mid)); conn.commit()
    cur.execute("SELECT changes()"); return cur.fetchone()[0]==0
def is_duplicate_for_user(conn,sid,norm,ts):
    h=_hash_text(norm); cur=conn.cursor(); cur.execute("SELECT last_ts FROM seen_text_user WHERE sender_id=? AND text_hash=?",(sid,h))
    row=cur.fetchone()
    if row and (ts-row[0])<DUP_WINDOW_SECONDS: return True
    cur.execute("INSERT INTO seen_text_user VALUES(?,?,?) ON CONFLICT(sender_id,text_hash) DO UPDATE SET last_ts=excluded.last_ts",(sid,h,ts))
    conn.commit(); return False
def is_duplicate_global(conn,norm,ts):
    h=_hash_text(norm); cur=conn.cursor(); cur.execute("SELECT last_ts FROM seen_text_global WHERE text_hash=?",(h,))
    row=cur.fetchone()
    if row and (ts-row[0])<DUP_WINDOW_SECONDS: return True
    cur.execute("INSERT INTO seen_text_global VALUES(?,?) ON CONFLICT(text_hash) DO UPDATE SET last_ts=excluded.last_ts",(h,ts))
    conn.commit(); return False

# ===== مانع محتالين =====
SPREAD_WINDOW_SECONDS=24*60*60
def update_and_check_sender_spread(conn,sid,cid,ts,window_seconds=SPREAD_WINDOW_SECONDS,threshold=6):
    try:
        cur=conn.cursor()
        cur.execute("DELETE FROM sender_group_spread WHERE last_ts<?",(ts-window_seconds,))
        cur.execute("INSERT INTO sender_group_spread VALUES(?,?,?) ON CONFLICT(sender_id,chat_id) DO UPDATE SET last_ts=excluded.last_ts",(sid,cid,ts))
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM sender_group_spread WHERE sender_id=? AND last_ts>=?",(sid,ts-window_seconds))
        return (cur.fetchone()[0] or 0)>threshold
    except: return False

# ===== فلتر المشرف =====
async def is_sender_admin(event):
    try:
        chat=event.chat
        if getattr(chat,"megagroup",None) or getattr(chat,"broadcast",None) or getattr(chat,"gigagroup",None) or getattr(chat,"username",None):
            res=await event.client(GetParticipantRequest(chat,event.sender_id))
            part=getattr(res,"participant",None)
            return isinstance(part,(ChannelParticipantAdmin,ChannelParticipantCreator))
        perms=await event.client.get_permissions(chat, event.sender_id)
        return bool(getattr(perms,"is_admin",False) or getattr(perms,"is_creator",False) or getattr(perms,"admin_rights",None))
    except: return False

# ===== إرسال =====
def send_alert_http(text):
    if not ALLOWED_RECIPIENTS: return
    url=f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"; CHUNK=3900
    parts=[text[i:i+CHUNK] for i in range(0,len(text),CHUNK)] or [text]
    for rid in ALLOWED_RECIPIENTS:
        for p in parts: requests.post(url,data={"chat_id":rid,"text":p,"parse_mode":"HTML"})

# ===== اسم المرسل + bio =====
async def _build_sender_anchor_html(event,sender):
    import html as _html
    first,last=(getattr(sender,"first_name","") or ""), (getattr(sender,"last_name","") or "")
    fullname=(f"{first} {last}".strip()) or "مستخدم بدون اسم"; fullname_esc=_html.escape(fullname)
    username=getattr(sender,"username",None); uid=getattr(sender,"id",0)
    if username: return f'<a href="https://t.me/{username}">@{_html.escape(username)}</a>',""
    anchor=f'<a href="tg://openmessage?user_id={uid}">{fullname_esc}</a>'; bio_html=""
    try:
        full=await event.client(GetFullUserRequest(sender)); about=getattr(full.full_user,"about",None)
        if about:
            about = squash_repeats(about)
            if len(about)>300: about=about[:300]+"…"
            bio_html=f'<b>النبذة :</b> {_html.escape(about)}'
    except: pass
    return anchor,bio_html

# ===== فلتر العبارة المزعجة (مرن) =====
ALI_SPLIT_RE = r"(?:a\s*[^a-zA-Z0-9]?\s*l\s*[^a-zA-Z0-9]?\s*i)"
BAD_PHRASE_RE = re.compile(
    r"وين+\s+طام[سص]\s+يحل[وه]?\s*[:\-]?\s*" + ALI_SPLIT_RE,
    re.IGNORECASE
)
def matches_bad_phrase(text_raw: str, text_norm: str) -> bool:
    return bool(BAD_PHRASE_RE.search(text_raw)) or ("وين طامس يحلو" in text_norm)

# ===== أدوات مساعدة =====
def within_word_limit(text_raw:str)->bool:
    words = re.findall(r"\S+", text_raw.strip())
    return len(words) <= MAX_WORDS

# ===== معالج الرسائل =====
def get_message_handler(conn):
    @events.register(events.NewMessage())
    async def handle_message(event):
        try:
            if not event.message or not event.message.message: return

            chat = event.chat

            # 0) استثناء قروبات الأخبار/السياسة/الدردشة/الترفيه
            if is_blacklisted_chat(chat):
                return

            if ALLOWED_CHAT_USERNAMES:
                uname = getattr(chat,"username",None)
                if not uname or uname.lower() not in [u.lower() for u in ALLOWED_CHAT_USERNAMES]:
                    return

            text_raw = event.message.message

            # 0.1) أي رابط (حتى المجزأ) أو t.me/wa.me أو أرقام جوال ⇒ تجاهل
            if has_any_link(text_raw) or PHONE_RE.search(text_raw):
                return

            # 0.2) رسائل إنجليزية قصيرة/عديمة المعنى
            if is_meaningless_english(text_raw):
                return

            # 1) حدود الطول/الكلمات/الأسطر
            if len(text_raw) > MAX_AD_LENGTH: return
            if not within_word_limit(text_raw): return
            if len(text_raw.splitlines()) > MAX_LINES: return

            # 2) منع الإنجليزية الصِرف
            if has_english(text_raw) and not has_arabic(text_raw):
                return

            # 3) التطبيع وإزالة التكرارات
            text_norm = normalize_ar(squash_repeats(text_raw))

            # 3.1) فلتر العبارة المزعجة
            if matches_bad_phrase(text_raw, text_norm):
                return

            # 4) نية + خدمة
            if not is_student_service_inquiry(text_raw, text_norm):
                return

            # 5) فلتر المشرفين
            if await is_sender_admin(event):
                return

            cid, mid = event.chat_id, event.message.id
            if is_seen(conn, cid, mid): return

            ts = int(event.message.date.timestamp())
            sender = await event.get_sender(); sid = getattr(sender, "id", 0)

            # 6) تكرار 5 دقائق لنفس المستخدم
            if is_duplicate_for_user(conn, sid, text_norm, ts): return

            # 7) مانع محتالين (>6 مجموعات/24 ساعة)
            if update_and_check_sender_spread(conn, sid, cid, ts): return

            # ===== تحضير الإرسال =====
            msg_link = "غير متاح"
            if getattr(chat, "username", None):
                msg_link = f"https://t.me/{chat.username}/{mid}"

            safe_text = html.escape(text_raw)
            sender_anchor, bio_html = await _build_sender_anchor_html(event, sender)

            message_text = (f"👤 {sender_anchor}\n"
                            f"<b>ID المرسل :</b> {sid}\n"
                            f"<b>نص الرساله :</b>\n<code>{safe_text}</code>\n"
                            f"<b>رابط الرساله :</b> {msg_link}\n")
            if bio_html: message_text += f"{bio_html}\n"

            send_alert_http(message_text)
            print(f"🚨 تنبيه (ID:{sid})")

        except Exception as e:
            print(f"⚠️ خطأ: {e}")
    return handle_message

# ===== تشغيل العملاء =====
def client_runner(s,i):
    name=f"client-{i}"; db=get_db_connection()
    while True:
        try:
            loop=asyncio.new_event_loop(); asyncio.set_event_loop(loop)
            c=TelegramClient(StringSession(s),API_ID,API_HASH); c.start()
            c.add_event_handler(get_message_handler(db),events.NewMessage())
            # 👇 يطبع في الكونسول فقط — لا يرسل للبوت
            print(f"✅ [{name}] جاهز — يبدأ الاستماع")
            # (تم حذف send_alert_http هنا حتى لا تُرسل رسالة 'متصل')
            c.run_until_disconnected()
        except Exception as e:
            print(f"⛔ [{name}] خطأ: {e} — إعادة المحاولة بعد 5 ثوانٍ")
            time.sleep(5)

# ===== main (مع إشعار Online وقائمة الجلسات) =====
def main():
    print("🚀 بدء البوت…"); print(f"📤 المرسَل لهم: {ALLOWED_RECIPIENTS}")
    if not os.path.exists(SESS_FILE):
        print("❌ لا يوجد sessions.json"); return

    with open(SESS_FILE,"r",encoding="utf-8") as f:
        sessions=json.load(f)

    if not sessions:
        print("❌ لا جلسات"); return

    # 🔔 إشعار Online مع أرقام الجلسات (1,2,3,...)
    indices = ",".join(str(i) for i in range(1, len(sessions) + 1))
    msg = f"<b>Online</b> {indices}"
    print(msg)
    send_alert_http(msg)

    # تشغيل كل جلسة في ثريد
    for i,s in enumerate(sessions,1):
        threading.Thread(target=client_runner,args=(s,i),daemon=True).start()

    while True:
        time.sleep(3600)

if __name__=="__main__":
    main()