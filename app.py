#!/usr/bin/env python3
"""ระบบร้านหนังสือมือสอง — เดโมสำหรับนำเสนอลูกค้า
stdlib only: http.server + sqlite3 + urllib (ไม่ต้องลง package)
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "shop.db"
PORT = int(os.environ.get("PORT", "5480"))

# ราคามือสองคิดจาก % ของราคาปก ตามเกรดสภาพ
GRADE_FACTOR = {"A": 0.50, "B": 0.35, "C": 0.20}
GRADE_LABEL = {"A": "สภาพดีมาก", "B": "สภาพดี", "C": "พออ่านได้"}
BUYBACK_RATE = 0.40  # รับซื้อที่ 40% ของราคาที่จะขายได้


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key_from(env_path, name):
    env = Path(env_path)
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def typhoon_key():
    return os.environ.get("TYPHOON_API_KEY") or _key_from(BASE / ".env", "TYPHOON_API_KEY")


# ---------------------------------------------------------------- auth
# ล็อกอินง่ายๆ กันคนอื่นเข้ามาใช้ (และกันกิน API key ของร้าน)
# เก็บ session ใน cookie ที่เซ็นด้วย HMAC — ปลอมไม่ได้ ไม่ต้องมีฐานข้อมูล session
COOKIE = "bookstore_session"
SESSION_DAYS = 14


def _cfg(name, default=""):
    return os.environ.get(name) or _key_from(BASE / ".env", name) or default


def _secret():
    return _cfg("SESSION_SECRET", "dev-secret-change-me").encode()


def make_token(user):
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    body = f"{user}|{exp}"
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode()


def check_token(tok):
    """คืนชื่อผู้ใช้ถ้า token ถูกต้องและยังไม่หมดอายุ ไม่งั้นคืน None"""
    try:
        raw = base64.urlsafe_b64decode(tok.encode()).decode()
        user, exp, sig = raw.rsplit("|", 2)
        expect = hmac.new(_secret(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect):
            return None
        if int(exp) < time.time():
            return None
        return user
    except Exception:
        return None


def verify_login(user, password):
    want_u = _cfg("AUTH_USER", "nuun")
    want_p = _cfg("AUTH_PASS")
    if not want_p:          # ยังไม่ตั้งรหัส = ไม่ล็อก (กันล็อกตัวเองออก)
        return True
    return (hmac.compare_digest((user or "").strip(), want_u)
            and hmac.compare_digest(password or "", want_p))


# ปลายทาง API ของ Typhoon (SCB10X) — รูปแบบเดียวกับ OpenAI
TYPHOON_URL = "https://api.opentyphoon.ai/v1/chat/completions"
OCR_MODEL = "typhoon-ocr"                       # Typhoon OCR 1.5 (2B) — อ่านตัวหนังสือจากภาพ
TEXT_MODEL = "typhoon-v2.5-30b-a3b-instruct"    # โมเดลข้อความ — แยกช่องข้อมูล + ตอบแชท


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  isbn TEXT, title TEXT NOT NULL, title_alt TEXT, author TEXT, translator TEXT,
  publisher TEXT, category TEXT, edition TEXT,
  year TEXT, synopsis TEXT, cover_price REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS copies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title_id INTEGER NOT NULL REFERENCES titles(id),
  grade TEXT NOT NULL, price REAL NOT NULL, cost REAL,
  shelf TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'in_stock',
  added_at TEXT NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  copy_id INTEGER NOT NULL REFERENCES copies(id),
  price REAL NOT NULL, sold_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_copies_title ON copies(title_id);
CREATE INDEX IF NOT EXISTS idx_copies_shelf ON copies(shelf);
"""

# ข้อมูลตัวอย่าง (isbn=None คือเล่มเก่าที่ไม่มีบาร์โค้ด ต้องใช้ AI อ่านปก)
SEED = [
    ("9786160000011", "ข้างหลังภาพ", "ศรีบูรพา", "ดอกหญ้า", "2544",
     "เรื่องรักต่างวัยระหว่างนพพรกับคุณหญิงกีรติ ที่เริ่มต้นที่ญี่ปุ่นและจบลงด้วยประโยคที่คนไทยจำได้ทั้งประเทศ", 220,
     [("A", "A-01-2"), ("B", "A-01-2"), ("C", "A-01-3")]),
    ("9786160000028", "สี่แผ่นดิน เล่ม 1", "ม.ร.ว.คึกฤทธิ์ ปราโมช", "ดอกหญ้า", "2545",
     "ชีวิตแม่พลอยที่ผ่านสี่รัชกาล เล่าประวัติศาสตร์ไทยผ่านสายตาผู้หญิงในวัง", 320,
     [("B", "A-02-1"), ("B", "A-02-1")]),
    ("9786160000035", "คู่กรรม", "ทมยันตี", "ณ บ้านวรรณกรรม", "2547",
     "ความรักของโกโบริกับอังศุมาลินท่ามกลางสงครามโลกครั้งที่สอง", 380,
     [("A", "A-02-3"), ("C", "A-02-3")]),
    ("9786160000042", "ปีศาจ", "เสนีย์ เสาวพงศ์", "มติชน", "2549",
     "สาย สีมา ลูกชาวนาที่ยืนหยัดต่อระบบชนชั้น พร้อมประโยค 'ผมเป็นปีศาจที่กาลเวลาได้สร้างขึ้น'", 260,
     [("B", "A-03-1")]),
    ("9786160000059", "เพชรพระอุมา ตอน ไพรมหากาฬ", "พนมเทียน", "ณ บ้านวรรณกรรม", "2540",
     "นวนิยายผจญภัยในป่าลึกที่ยาวที่สุดในวรรณกรรมไทย", 450,
     [("C", "B-01-1"), ("C", "B-01-1"), ("B", "B-01-2")]),
    ("9786160000066", "ผู้ชนะสิบทิศ เล่ม 1", "ยาขอบ", "ผดุงศึกษา", "2538",
     "เรื่องราวของจะเด็ดจากเด็กเลี้ยงช้างสู่ผู้ครองแผ่นดิน", 290,
     [("C", "B-01-3")]),
    ("9786160000073", "เจ้าชายน้อย", "อองตวน เดอ แซงเตกซูเปรี", "ผีเสื้อ", "2553",
     "นักบินตกในทะเลทรายพบเด็กชายจากดาวดวงเล็ก บทสนทนาที่อ่านตอนเด็กกับตอนโตให้ความหมายไม่เหมือนกัน", 195,
     [("A", "C-01-1"), ("A", "C-01-1"), ("B", "C-01-1")]),
    ("9786160000080", "แฮร์รี่ พอตเตอร์ กับศิลาอาถรรพ์", "เจ. เค. โรว์ลิ่ง", "นานมีบุ๊คส์", "2543",
     "เด็กชายใต้บันไดบ้านเลขที่สี่ ซอยพรีเว็ต ได้รับจดหมายจากโรงเรียนพ่อมด", 295,
     [("B", "C-02-1"), ("C", "C-02-1")]),
    ("9786160000097", "โลกของโซฟี", "โยสไตน์ กอร์เดอร์", "คบไฟ", "2548",
     "เด็กหญิงได้รับจดหมายถามว่า 'เธอเป็นใคร' แล้วกลายเป็นบทเรียนปรัชญาตะวันตกทั้งสาย", 420,
     [("B", "C-02-3")]),
    ("9786160000103", "พ่อรวยสอนลูก", "โรเบิร์ต คิโยซากิ", "ซีเอ็ดยูเคชั่น", "2551",
     "ความต่างระหว่างสินทรัพย์กับหนี้สิน เล่าผ่านพ่อสองคนที่สอนเรื่องเงินไม่เหมือนกัน", 250,
     [("B", "D-01-1"), ("B", "D-01-1"), ("C", "D-01-1"), ("A", "D-01-2")]),
    ("9786160000110", "คิดแบบยิว ทำแบบญี่ปุ่น", "ไกรฤกษ์ นานา", "มติชน", "2555",
     "เปรียบวิธีคิดเรื่องเงินและการทำงานของสองชาติที่ต่างกันสุดขั้ว", 210,
     [("B", "D-01-3")]),
    ("9786160000127", "Atomic Habits เพราะชีวิตดีได้กว่าที่เป็น", "เจมส์ เเคลียร์", "อมรินทร์ How to", "2562",
     "เปลี่ยนนิสัยด้วยการแก้ระบบรอบตัวแทนการใช้ใจสู้", 295,
     [("A", "D-02-1"), ("A", "D-02-1"), ("B", "D-02-1")]),
    ("9786160000134", "คู่มือมนุษย์", "พุทธทาสภิกขุ", "ธรรมสภา", "2550",
     "อธิบายแก่นพุทธศาสนาด้วยภาษาที่คนทำงานอ่านรู้เรื่อง", 150,
     [("B", "E-01-1"), ("C", "E-01-1")]),
    ("9786160000141", "โดราเอมอน เล่ม 1", "ฟูจิโกะ เอฟ ฟูจิโอะ", "เนชั่น เอ็ดดูเทนเมนท์", "2545",
     "หุ่นยนต์แมวจากอนาคตมาช่วยโนบิตะ ด้วยของวิเศษที่มักทำให้เรื่องแย่ลงก่อนดีขึ้น", 55,
     [("C", "F-01-1"), ("C", "F-01-1"), ("B", "F-01-1")]),
    ("9786160000158", "วันพีซ เล่ม 1", "เออิจิโร โอดะ", "สยามอินเตอร์คอมิกส์", "2547",
     "ลูฟี่ออกเรือหาวันพีซเพื่อเป็นราชาโจรสลัด", 50,
     [("B", "F-01-3"), ("C", "F-01-3")]),
    ("9786160000165", "แคลคูลัส 1 สำหรับวิศวกร", "รศ.ดร. สมชาย วิริยะ", "จุฬาลงกรณ์มหาวิทยาลัย", "2556",
     "ตำราแคลคูลัสพื้นฐานพร้อมโจทย์แบบฝึกหัดสำหรับนักศึกษาวิศวกรรม", 480,
     [("C", "G-01-1"), ("B", "G-01-1")]),
    ("9786160000172", "ฟิสิกส์ ม.ปลาย เล่มรวม", "ช่วง ทมทิตชงค์", "ไฮเอ็ดพับลิชชิ่ง", "2558",
     "สรุปเนื้อหาฟิสิกส์มัธยมปลายพร้อมโจทย์เตรียมสอบเข้ามหาวิทยาลัย", 420,
     [("C", "G-01-2")]),
    ("9786160000189", "กาแฟดำ", "สุทธิชัย หยุ่น", "เนชั่นบุ๊คส์", "2552",
     "รวมบทวิเคราะห์การเมืองและสื่อจากคอลัมนิสต์รุ่นใหญ่", 240,
     [("B", "H-01-1")]),
    ("9786160000196", "ชีวิตนี้สั้นนัก", "ว.วชิรเมธี", "อมรินทร์ธรรมะ", "2554",
     "ข้อคิดสั้นเรื่องการใช้เวลาที่เหลือให้คุ้มค่า", 180,
     [("A", "E-01-3"), ("B", "E-01-3")]),
    ("9786160000202", "Sapiens ประวัติย่อมนุษยชาติ", "ยูวัล โนอาห์ แฮรารี", "ยิปซี", "2561",
     "เล่าเจ็ดหมื่นปีของมนุษย์ตั้งแต่ยังเป็นสัตว์ธรรมดาจนครองโลก", 395,
     [("A", "C-03-1"), ("B", "C-03-1")]),
    # --- เล่มเก่าไม่มี ISBN ต้องใช้ AI อ่านปก ---
    (None, "ลูกอีสาน", "คำพูน บุญทวี", "โป๊ยเซียน", "2519",
     "ชีวิตเด็กชายในหมู่บ้านอีสานยุคแล้ง หนังสือรางวัลซีไรต์เล่มแรกของไทย", 60,
     [("C", "I-01-1"), ("C", "I-01-1")]),
    (None, "ความรักของวัลยา", "เสนีย์ เสาวพงศ์", "เคล็ดไทย", "2517",
     "นิยายรักที่ผูกกับอุดมการณ์การเมืองในยุคก่อน 14 ตุลา", 45,
     [("C", "I-01-2")]),
    (None, "จดหมายจากเมืองไทย", "โบตั๋น", "ชมรมเด็ก", "2513",
     "จดหมายของชายจีนอพยพที่เขียนกลับบ้านเกิด เล่าสังคมไทยจากสายตาคนนอก", 55,
     [("C", "I-01-3"), ("B", "I-01-3")]),
    (None, "แผ่นดินของเรา", "แม่อนงค์", "บรรณกิจ", "2508",
     "นิยายชีวิตครอบครัวไทยยุคเปลี่ยนผ่านหลังสงคราม", 40,
     [("C", "I-02-1")]),
]


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    fresh = not DB.exists()
    conn = db()
    conn.executescript(SCHEMA)
    # เพิ่มคอลัมน์ใหม่ให้ฐานข้อมูลเดิมที่มีข้อมูลอยู่แล้ว (ไม่ต้องล้างข้อมูล)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(titles)")}
    for col in ("title_alt", "translator", "category", "edition"):
        if col not in have:
            conn.execute(f"ALTER TABLE titles ADD COLUMN {col} TEXT")
    conn.commit()
    if fresh:
        for isbn, title, author, pub, year, syn, cover, copies in SEED:
            cur = conn.execute(
                "INSERT INTO titles(isbn,title,author,publisher,year,synopsis,cover_price,source)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (isbn, title, author, pub, year, syn, cover,
                 "isbn" if isbn else "ai_cover"),
            )
            tid = cur.lastrowid
            for grade, shelf in copies:
                price = round(cover * GRADE_FACTOR[grade] / 5) * 5
                conn.execute(
                    "INSERT INTO copies(title_id,grade,price,cost,shelf,status,added_at)"
                    " VALUES(?,?,?,?,?,'in_stock',?)",
                    (tid, grade, price, round(price * BUYBACK_RATE), shelf, now()),
                )
        conn.commit()
    conn.close()


# ---------------------------------------------------------------- API logic

def _search_titles(q, limit=40):
    """ค้นชื่อ/ผู้เขียน/สนพ./ISBN/เนื้อเรื่องย่อ คืน title+copies พร้อมใช้ทั้ง UI และ AI tool"""
    conn = db()
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        rows = conn.execute(
            "SELECT * FROM titles WHERE title LIKE ? OR IFNULL(title_alt,'') LIKE ?"
            " OR IFNULL(author,'') LIKE ? OR IFNULL(translator,'') LIKE ?"
            " OR IFNULL(publisher,'') LIKE ? OR IFNULL(category,'') LIKE ?"
            " OR IFNULL(isbn,'') LIKE ? OR IFNULL(synopsis,'') LIKE ?"
            " ORDER BY title LIMIT ?",
            (like,) * 8 + (limit,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM titles ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for t in rows:
        copies = conn.execute(
            "SELECT * FROM copies WHERE title_id=? ORDER BY"
            " CASE grade WHEN 'A' THEN 1 WHEN 'B' THEN 2 ELSE 3 END",
            (t["id"],),
        ).fetchall()
        out.append({
            **dict(t),
            "copies": [dict(c) for c in copies],
            "in_stock": sum(1 for c in copies if c["status"] == "in_stock"),
        })
    conn.close()
    return out


def api_search(q):
    return {"results": _search_titles(q, limit=40)}


def api_lookup(isbn):
    """สแกนบาร์โค้ด — หาในฐานข้อมูลก่อน (ของจริงจะต่อ API ฐานข้อมูลหนังสือด้วย)"""
    conn = db()
    row = conn.execute("SELECT * FROM titles WHERE isbn=?", ((isbn or "").strip(),)).fetchone()
    conn.close()
    if row:
        return {"found": True, "title": dict(row)}
    return {"found": False}


# prompt ทางการของ Typhoon OCR 1.5 — โมเดลนี้เป็น OCR ล้วน คืนข้อความทั้งหมดบนภาพ
OCR_PROMPT = """Extract all text from the image.

Instructions:
- Only return the clean Markdown.
- Do not include any explanation or extra text.
- You must include all information on the page.
- This book is from Thailand. Extract EVERY line of text you can see in ANY language
  (Thai, English, or others). Do not skip Thai text, and do not skip English text.
- If the page has text in two languages, extract BOTH, each on its own line,
  keeping each in its original language. Never translate.
- Copy Thai characters exactly as printed, including tone marks
  (่ ้ ๊ ๋) and vowel marks. Do not normalise or guess a similar word.

Formatting Rules:
- Tables: Render tables using <table>...</table> in clean HTML format.
- Page Numbers: Wrap page numbers in <page_number>...</page_number>.
- Checkboxes: Use ☐ for unchecked and ☑ for checked boxes."""

# ขั้นที่ 2 — แยกข้อความที่ OCR ได้ ออกเป็นช่องข้อมูลหนังสือ
FIELD_PROMPT = (
    "จากข้อความที่อ่านได้จากปกหนังสือด้านล่าง ให้แยกข้อมูลเป็น JSON เท่านั้น ห้ามมีข้อความอื่น\n"
    'รูปแบบ: {"title":"","title_alt":"","author":"","translator":"","publisher":"",'
    '"category":"","category_guess":"","edition":"","isbn":"","year":"",'
    '"cover_price":"","synopsis":""}\n\n'
    "กติกาสำคัญที่สุด — **เรื่องภาษา**:\n"
    "- ทุกช่องให้คัดลอกข้อความ **ตามภาษาที่พิมพ์อยู่บนปกจริง** ห้ามแปล ห้ามถอดเสียงเป็นภาษาอื่น\n"
    "- ถ้าชื่อผู้เขียนพิมพ์เป็นภาษาอังกฤษ ให้ใส่เป็นภาษาอังกฤษตามนั้น (เช่น 'Yuval Noah Harari' "
    "ห้ามเขียนเป็น 'ยูวัล โนอาห์ แฮรารี')\n"
    "- ถ้าพิมพ์เป็นภาษาไทย ก็ใส่ภาษาไทยตามนั้น\n"
    "- คัดลอกตัวอักษรไทยเป๊ะๆ รวมวรรณยุกต์ ห้ามเปลี่ยนเป็นคำที่คล้ายกัน\n\n"
    "กติกาแต่ละช่อง:\n"
    "- title: ชื่อหนังสือหลัก **ภาษาเดียวเท่านั้น** ถ้ามีชื่อไทยให้ใช้ชื่อไทย\n"
    "  ห้ามเอาชื่อ 2 ภาษามาต่อกันในช่องนี้ (ผิด: 'ประวัติย่อมนุษยชาติ Sapiens' / ถูก: title='ประวัติย่อมนุษยชาติ' และ title_alt='Sapiens')\n"
    "- title_alt: **ถ้าปกมีชื่อหนังสือ 2 ภาษา ให้ใส่ชื่ออีกภาษาไว้ช่องนี้** (ถ้ามีภาษาเดียวให้เว้นว่าง)\n"
    "- author: ผู้เขียน / translator: ผู้แปล (ถ้ามีคำว่า 'แปลโดย' 'ผู้แปล' 'Translated by')\n"
    "- category: ใส่**เฉพาะหมวดหมู่ที่พิมพ์อยู่บนปกจริง** ถ้าปกไม่ได้พิมพ์ไว้ให้เว้นว่าง ห้ามเดา\n"
    "- category_guess: ถ้า category ว่าง ให้**เลือกหมวดที่ใกล้เคียงที่สุด 1 หมวด** โดยดูจาก\n"
    "  ชื่อเรื่องและเนื้อเรื่องย่อ **ต้องเลือกจากรายการนี้เท่านั้น ห้ามคิดหมวดใหม่**:\n"
    "  {CATEGORY_LIST}\n"
    "  (ถ้า category มีค่าอยู่แล้ว ให้เว้น category_guess ว่าง)\n"
    "- edition: พิมพ์ครั้งที่ ใส่เฉพาะตัวเลข (เช่น 'พิมพ์ครั้งที่ 3' -> '3')\n"
    "- isbn: เลข ISBN ถ้ามีบนปก\n"
    "- year: ปีพิมพ์ ใส่เฉพาะตัวเลข / cover_price: ราคาปก ใส่เฉพาะตัวเลข\n"
    "- synopsis: ย่อ 1-2 ประโยคจากข้อความบนปกเท่านั้น\n\n"
    "ถ้าช่องไหนไม่มีในข้อความ ให้ใส่ค่าว่าง ห้ามเดา ห้ามเติมจากความรู้ของคุณเอง\n\n"
    "ข้อความจากปก:\n"
)

THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

# หมวดหมู่มาตรฐานของร้าน — เก็บไว้ที่เดียว ใช้ทั้งใน prompt ให้ AI เลือก และเป็นปุ่มให้พนักงานแตะ
# ปกหนังสือส่วนใหญ่ไม่พิมพ์หมวดหมู่ไว้ จึงต้องให้ AI เดาจากชื่อเรื่อง+เนื้อเรื่องย่อ
CATEGORIES = [
    "นวนิยาย", "วรรณกรรมแปล", "เรื่องสั้น", "นิยายวาย",
    "จิตวิทยา/พัฒนาตนเอง", "ธรรมะ/ศาสนา", "ปรัชญา",
    "ธุรกิจ/การเงิน", "ประวัติศาสตร์", "ชีวประวัติ",
    "สุขภาพ", "ทำอาหาร", "ท่องเที่ยว", "บ้านและสวน",
    "ศิลปะ/ออกแบบ", "คอมพิวเตอร์/ไอที", "วิทยาศาสตร์",
    "ภาษา/พจนานุกรม", "ตำราเรียน/คู่มือสอบ", "กฎหมาย",
    "การ์ตูน", "เด็ก/เยาวชน", "กีฬา", "อื่นๆ",
]

FIELD_PROMPT = FIELD_PROMPT.replace("{CATEGORY_LIST}", " · ".join(CATEGORIES))


def typhoon_post(body, timeout=120):
    k = typhoon_key()
    if not k:
        raise RuntimeError("ไม่พบ TYPHOON_API_KEY บนเครื่อง")
    req = urllib.request.Request(
        TYPHOON_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + k, "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent?key={key}")
GEMINI_MODEL = "gemini-3-flash-preview"   # ทดสอบแล้วอ่านวรรณยุกต์ไทยครบ 6/6 (Typhoon ได้ 3/6)


def _gemini_read(images, media_type):
    """ทางเลือกที่ 2 — อ่านด้วย Gemini ครั้งเดียวจบ (อ่านภาพ + แยกช่องพร้อมกัน)

    ต่างจาก Typhoon ที่ต้อง 2 ขั้น เพราะ Typhoon OCR ถอดข้อความได้แต่แยกช่องไม่ได้
    ข้อแลกเปลี่ยน: Gemini เป็นบริการบนคลาวด์ ไม่ใช่ Local AI
    """
    key = _cfg("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("ไม่พบ GEMINI_API_KEY บนเครื่อง")

    parts = [{"text": FIELD_PROMPT.replace("ข้อความจากปก:", "ภาพปกหนังสือ:")}]
    for i, im in enumerate(images[:3]):
        b64 = im.get("image_b64") or ""
        if not b64:
            continue
        parts.append({"text": f"[{im.get('label') or f'รูปที่ {i + 1}'}]"})
        parts.append({"inline_data": {"mime_type": im.get("media_type") or media_type,
                                      "data": b64}})

    req = urllib.request.Request(
        GEMINI_URL.format(model=GEMINI_MODEL, key=key),
        data=json.dumps({
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    r = json.load(urllib.request.urlopen(req, timeout=180))
    txt = r["candidates"][0]["content"]["parts"][0]["text"]
    u = r.get("usageMetadata", {})
    return txt, {"prompt_tokens": u.get("promptTokenCount") or 0,
                 "completion_tokens": u.get("candidatesTokenCount") or 0}


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
QWEN_MODEL = "qwen/qwen3.8-27b"   # โมเดลเปิด ดาวน์โหลดมารันบนเครื่องเองได้ = ตัวเลือก Local จริง


def _qwen_read(images, media_type):
    """ทางเลือกที่ 3 — Qwen ผ่าน OpenRouter (อ่าน + แยกช่อง ครั้งเดียวจบ)

    Qwen เป็นโมเดลเปิด (ดาวน์โหลดมารันบน Mac ได้) ต่างจาก Gemini ที่รันเองไม่ได้
    ที่ยิงผ่าน OpenRouter เพราะเครื่องนี้ไม่มีการ์ดจอ — แต่เป็นตัวโมเดลเดียวกัน
    จึงใช้วัดว่า "ถ้าเอาลง Mac จริง จะอ่านแม่นแค่ไหน" ได้ (ความเร็วเชื่อไม่ได้)
    """
    key = _cfg("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("ไม่พบ OPENROUTER_API_KEY บนเครื่อง")

    content = [{"type": "text",
                "text": FIELD_PROMPT.replace("ข้อความจากปก:", "ภาพปกหนังสือ:")}]
    for i, im in enumerate(images[:3]):
        b64 = im.get("image_b64") or ""
        if not b64:
            continue
        content.append({"type": "text", "text": f"[{im.get('label') or f'รูปที่ {i + 1}'}]"})
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{im.get('media_type') or media_type};base64,{b64}"}})

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps({
            "model": QWEN_MODEL, "temperature": 0, "max_tokens": 1500,
            # Qwen รุ่นนี้ "คิดในใจ" ก่อนตอบ ถ้าไม่ปิด มันใช้ token หมดไปกับการคิด
            # แล้วส่งคำตอบว่างเปล่ากลับมา (วัดได้: reasoning 2495 token, content 0 ตัวอักษร)
            # ทดสอบแล้วปิดโหมดคิดได้ผลดีกว่าทุกทาง — ถูกกว่า 4 เท่า และอ่านไทยแม่นกว่า
            "reasoning": {"enabled": False},
            "messages": [{"role": "user", "content": content}],
        }).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    r = json.load(urllib.request.urlopen(req, timeout=240))
    txt = r["choices"][0]["message"].get("content") or ""
    return txt, r.get("usage", {})


def _ocr_one(image_b64, media_type):
    """ขั้นที่ 1 — ให้ Typhoon OCR ถอดข้อความจากภาพเดียว คืน (text, usage)"""
    r = typhoon_post({
        "model": OCR_MODEL,
        "max_tokens": 4096,
        "temperature": 0.1,
        "top_p": 0.6,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
            ],
        }],
    })
    txt = (r["choices"][0]["message"].get("content") or "").strip()
    if txt.startswith("{"):  # เวอร์ชันเก่าคืน JSON ที่มีคีย์ natural_text
        try:
            txt = json.loads(txt).get("natural_text", txt)
        except json.JSONDecodeError:
            pass
    return txt, r.get("usage", {})


def api_ai_read(images, media_type, engine="typhoon"):
    """อ่านหนังสือจากภาพ แล้วแยกเป็นช่องข้อมูล — เลือกเครื่องอ่านได้ 2 แบบ

    images = [{"label": "ปกหน้า", "image_b64": "..."}, ...] รับได้หลายรูป
    (ปกหน้า/ปกหลัง/หน้าแรก) เพราะข้อมูลกระจายกันอยู่ — เนื้อเรื่องย่อมักอยู่ปกหลัง
    ส่วนปีพิมพ์กับราคามักอยู่หน้าแรก

    engine="typhoon" — 2 ขั้น (OCR ทุกรูป -> รวมข้อความ -> แยกช่อง) เพราะ Typhoon OCR
                       เป็น OCR ล้วน แยกช่องเองไม่ได้ / รันบนเครื่องตัวเองได้
    engine="gemini"  — ขั้นเดียว อ่านวรรณยุกต์ไทยแม่นกว่ามาก แต่เป็นคลาวด์ รันเองไม่ได้
    engine="qwen"    — ขั้นเดียว โมเดลเปิด รันบน Mac ได้ (ยิงผ่าน OpenRouter เพราะเครื่องนี้ไม่มีการ์ดจอ)
    """
    if not images:
        return {"error": "ไม่มีภาพ"}

    ONE_SHOT = {"gemini": (_gemini_read, "Gemini"), "qwen": (_qwen_read, "Qwen")}
    if engine in ONE_SHOT:
        fn, name = ONE_SHOT[engine]
        try:
            out, u2 = fn(images, media_type)
        except Exception as exc:
            return {"error": f"{name} อ่านภาพไม่สำเร็จ: {exc}"}
        return _finish_read(out, "", [], 0, 0, u2)

    # ขั้นที่ 1 — OCR ทุกรูป
    raw_by_image, in_tok, out_tok = [], 0, 0
    for i, im in enumerate(images[:3]):  # จำกัด 3 รูปต่อเล่ม
        b64 = im.get("image_b64") or ""
        if not b64:
            continue
        label = im.get("label") or f"รูปที่ {i + 1}"
        try:
            txt, u = _ocr_one(b64, im.get("media_type") or media_type)
        except Exception as exc:
            raw_by_image.append({"label": label, "text": "", "error": str(exc)})
            continue
        in_tok += u.get("prompt_tokens") or 0
        out_tok += u.get("completion_tokens") or 0
        raw_by_image.append({"label": label, "text": txt})
        if i < len(images) - 1:
            time.sleep(0.6)  # เคารพ rate limit ของ Typhoon (2 req/s)

    combined = "\n\n".join(f"[{p['label']}]\n{p['text']}"
                           for p in raw_by_image if p.get("text"))
    if not combined.strip():
        return {"error": "OCR อ่านข้อความจากภาพไม่ได้ — ลองถ่ายใหม่ให้ชัดขึ้น",
                "raw_by_image": raw_by_image}

    # ขั้นที่ 2 — แยกช่องข้อมูลจากข้อความที่รวมได้
    try:
        r2 = typhoon_post({
            "model": TEXT_MODEL,
            "max_tokens": 800,
            "temperature": 0,
            "messages": [{"role": "user", "content": FIELD_PROMPT + combined}],
        })
    except Exception as exc:
        return {"error": f"แยกช่องข้อมูลไม่สำเร็จ: {exc}",
                "raw_text": combined, "raw_by_image": raw_by_image}

    return _finish_read(r2["choices"][0]["message"]["content"], combined,
                        raw_by_image, in_tok, out_tok, r2.get("usage", {}))


READ_LOG = BASE / "reads.jsonl"


def _log_read(engine, n_images, res):
    """เก็บผลอ่านทุกครั้งลงไฟล์ ไว้วิเคราะห์ความแม่นย้อนหลังตอนทดสอบกับปกจริง"""
    try:
        with READ_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "engine": engine,
                "n_images": n_images,
                "error": res.get("error"),
                "raw": res.get("raw"),   # คำตอบดิบตอนแกะ JSON ไม่ได้ — ไว้ไล่หาสาเหตุ
                "extracted": res.get("extracted"),
                "raw_text": (res.get("raw_text") or "")[:1500],
                "usage": res.get("usage"),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass  # เขียน log ไม่ได้ ไม่ควรทำให้การอ่านปกล้มไปด้วย


def _finish_read(out, combined, raw_by_image, in_tok, out_tok, u2):
    """แกะ JSON ที่โมเดลตอบมา + ทำความสะอาดข้อมูล (ใช้ร่วมกันทั้ง Typhoon และ Gemini)"""
    out = (out or "").strip()
    if out.startswith("```"):
        out = out.split("```")[1]
        out = out[4:] if out.lower().startswith("json") else out
    out = out.strip()
    if not out.startswith("{") and "{" in out:
        out = out[out.index("{"): out.rindex("}") + 1]
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"error": "AI ตอบมาไม่ใช่ JSON", "raw": out[:400],
                "raw_text": combined, "raw_by_image": raw_by_image}

    # เผื่อโมเดลคืนเลขไทย
    for k in ("year", "cover_price", "isbn", "edition"):
        v = data.get(k)
        if isinstance(v, str):
            data[k] = v.translate(THAI_DIGITS)
    if isinstance(data.get("year"), str):
        data["year"] = data["year"].replace("พ.ศ.", "").replace("ค.ศ.", "").strip()
    if isinstance(data.get("cover_price"), str):
        data["cover_price"] = "".join(c for c in data["cover_price"] if c.isdigit() or c == ".")
    if isinstance(data.get("edition"), str):
        data["edition"] = "".join(c for c in data["edition"] if c.isdigit())
    # ตัดคำกำกับตำแหน่งที่ติดมากับชื่อคน (เช่น "วิไลรัตน์ เอมเอี่ยม แปล" -> ชื่อล้วน)
    for fld, marks in (("translator", ("แปลโดย", "ผู้แปล", "แปล", "Translated by", "Translator")),
                       ("author", ("เขียนโดย", "ผู้เขียน", "ประพันธ์โดย", "Written by", "Author"))):
        v = (data.get(fld) or "").strip()
        if not v:
            continue
        for m in marks:
            if v.startswith(m):
                v = v[len(m):]
            if v.endswith(m):
                v = v[: -len(m)]
        data[fld] = v.strip(" :·-–—\t")

    # กันเหนียว: บางครั้งโมเดลเอาชื่อ 2 ภาษามาต่อกันในช่อง title — ตัดส่วนที่ซ้ำกับ title_alt ออก
    ti, alt = (data.get("title") or "").strip(), (data.get("title_alt") or "").strip()
    if ti and alt and len(alt) >= 3 and alt in ti and ti != alt:
        trimmed = ti.replace(alt, " ").strip(" -–—:|/\t")
        if len(trimmed) >= 2:
            data["title"] = " ".join(trimmed.split())

    # หมวดหมู่: ถ้าปกไม่ได้พิมพ์ไว้ ใช้ที่ AI เดาจากเนื้อเรื่องย่อแทน
    # แต่รับเฉพาะที่อยู่ในรายการของร้านจริง เพื่อไม่ให้หมวดหมู่งอกใหม่เรื่อยๆ จนค้นหาไม่เจอ
    cat = (data.get("category") or "").strip()
    guess = (data.get("category_guess") or "").strip()
    data["category_from"] = "cover" if cat else ""
    if not cat and guess:
        match = next((c for c in CATEGORIES if c == guess), None) \
            or next((c for c in CATEGORIES if guess in c or c.split("/")[0] == guess), None)
        if match:
            data["category"] = match
            data["category_from"] = "guess"
    data.pop("category_guess", None)

    data.setdefault("confidence", "medium")

    return {
        "extracted": data,
        "raw_text": combined,              # ข้อความรวมทุกรูป
        "raw_by_image": raw_by_image,      # แยกตามรูป ให้พนักงานเทียบกับปกจริงได้
        # ช่องที่วัดแล้วว่า AI พลาดบ่อยสุด = ชื่อคน/สำนักพิมพ์ (วรรณยุกต์ไทยเพี้ยน)
        # ส่วนตัวเลข (ปี/ราคา) ทดสอบแล้วแม่นทุกครั้ง จึงไม่ต้อง flag
        # สะกดเพี้ยนแม้ตัวเดียวทำให้ลูกค้าค้นหาไม่เจอเล่มนั้น จึงต้องให้คนยืนยันก่อนบันทึก
        "needs_check": [k for k in ("author", "translator", "publisher", "category")
                        if data.get(k)],
        "usage": {   # รวมทุกรอบ OCR + รอบแยกช่องข้อมูล
            "in": in_tok + (u2.get("prompt_tokens") or 0),
            "out": out_tok + (u2.get("completion_tokens") or 0),
        },
    }


CHAT_MODEL = TEXT_MODEL  # Typhoon 2.5 — โมเดลไทย รองรับ function calling (ทดสอบแล้วเรียก tool ถูก)

# กติกาที่ใช้ร่วมกันทั้ง 2 โหมด — กันการแต่งข้อมูล ซึ่งเป็นความเสี่ยงใหญ่สุดของงานนี้
_CHAT_RULES_COMMON = (
    "กติกาที่ห้ามฝ่าฝืน:\n"
    "- ตอบได้เฉพาะจากผลค้นฐานข้อมูลที่ระบบแนบมาให้ท้ายบทสนทนา "
    "ห้ามตอบจากความจำหรือความรู้ทั่วไปเกี่ยวกับหนังสือเล่มนั้น\n"
    "- ห้ามบอกราคา สภาพ ปีพิมพ์ หรือจำนวนเล่ม ที่ไม่ได้อยู่ในผลค้น ห้ามเดาหรือแต่งขึ้นเอง\n"
    "- ถ้าไม่พบ ให้บอกตรงๆว่าไม่มีในร้าน ห้ามแนะนำเล่มที่ไม่อยู่ในผลค้น\n"
    "- ถ้าถูกถามหารีวิว/เสียงตอบรับ/คนอ่านว่าอย่างไร: ร้านไม่ได้เก็บรีวิวไว้ "
    "**ห้ามแต่งรีวิวขึ้นเอง และห้ามบอกว่า 'มีรีวิว'** ให้บอกว่าช่วยหาลิงก์รีวิวให้ได้\n"
)

# โหมดลูกค้า — คุยเหมือนคนขายจริง ไม่มีการ์ด ไม่เห็นข้อมูลหลังบ้าน
CHAT_PROMPT_CUSTOMER = (
    "คุณคือพนักงานขายร้านหนังสือมือสองที่กำลังคุยกับลูกค้าทางแชท\n"
    "พูดเหมือนคนจริงคุยกัน อบอุ่น เป็นกันเอง สั้นกระชับ 1-3 ประโยค ลงท้ายด้วยครับ/ค่ะ\n\n"
    + _CHAT_RULES_COMMON +
    "- **ตอบเป็นข้อความสนทนาล้วนเท่านั้น** ห้ามทำเป็นตาราง ห้ามใส่หัวข้อ ห้ามขึ้นบรรทัดเป็นข้อๆ "
    "ห้ามใส่สัญลักษณ์ตกแต่งอย่าง ** หรือ - นำหน้า เขียนเป็นประโยคเล่าให้ฟังธรรมดา\n"
    "- **ตอบเฉพาะสิ่งที่ถูกถาม ห้ามท่องข้อมูลอื่นพ่วงมาด้วย**\n"
    "  ถามสภาพ -> บอกแต่สภาพ ('มีสภาพดีมาก 1 เล่ม กับพออ่านได้ 2 เล่มครับ')\n"
    "  ถามปีพิมพ์ -> บอกแต่ปี ('พิมพ์ปี 2544 ครับ')\n"
    "  ถามราคา -> บอกแต่ราคา / ถามเรื่องย่อ -> เล่าแต่เรื่องย่อ\n"
    "  ห้ามตอบซ้ำเรื่องราคาทุกครั้งถ้าลูกค้าไม่ได้ถามราคา\n"
    "- ถ้าลูกค้าถามต่อเนื่องโดยไม่เอ่ยชื่อเล่ม ให้เข้าใจว่ายังหมายถึงเล่มเดิมที่คุยกันอยู่\n"
    "- **ห้ามพูดถึงข้อมูลหลังร้านเด็ดขาด** ได้แก่ รหัสชั้นวาง ต้นทุนรับซื้อ กำไร รหัสในระบบ "
    "จำนวนสต็อกรวมของร้าน\n"
    "  ถ้าลูกค้าถามเรื่องพวกนี้ ให้ตอบเลี่ยงสุภาพ **ประโยคเดียวแล้วหยุด** "
    "เช่น 'เรื่องนี้เดี๋ยวพนักงานหน้าร้านช่วยดูให้ได้เลยครับ'\n"
    "  **ห้ามบอกตำแหน่งชั้นวางใดๆ ห้ามแต่งชื่อชั้นขึ้นมาเอง** และ **ห้ามพูดว่าร้านเก็บหรือไม่เก็บ"
    "ข้อมูลอะไรไว้ในระบบ** เพราะลูกค้าไม่ต้องรู้เรื่องระบบหลังร้าน\n"
    "- ห้ามพูดว่า 'ดูจากการ์ดด้านล่าง' หรือ 'ดูรายการด้านล่าง' เพราะโหมดนี้ไม่มีรายการให้ดู "
    "ต้องเล่าข้อมูลออกมาในข้อความเลย"
)

# โหมดพนักงาน — เห็นข้อมูลครบ มีการ์ดสรุปให้ ทำงานเร็ว
CHAT_PROMPT_STAFF = (
    "คุณคือผู้ช่วยพนักงานขายในร้านหนังสือมือสอง คุยกับ 'พนักงาน' ไม่ใช่ลูกค้า\n"
    "ตอบสั้น ตรงประเด็น แบบเพื่อนร่วมงานที่รู้งาน\n\n"
    + _CHAT_RULES_COMMON +
    "- โหมดนี้บอกข้อมูลหลังร้านได้: รหัสชั้นวาง จำนวนเล่มคงเหลือ สภาพแต่ละเล่ม ราคาขาย\n"
    "- **ตอบเฉพาะสิ่งที่ถูกถาม ประโยคเดียวจบถ้าได้** ถามชั้นวางก็บอกชั้นวาง ถามปีก็บอกปี\n"
    "- **เจอมากกว่า 1 เล่ม: บอกแค่จำนวน แล้วชี้ไปที่การ์ด** เช่น 'เจอ 3 เล่มครับ "
    "รายละเอียดกับชั้นวางดูจากการ์ดด้านล่างเลย' **ห้ามไล่ชื่อหนังสือทีละเล่มในข้อความ** "
    "เพราะการ์ดแสดงให้ครบแล้ว จะกลายเป็นข้อมูลซ้ำซ้อน\n"
    "- เจอเล่มเดียว: ตอบข้อมูลที่ถูกถามได้เลยในประโยคเดียว\n"
    "- **เขียนเป็นข้อความธรรมดา** ห้ามใช้ * ห้ามทำหัวข้อย่อย ห้ามขึ้นบรรทัดเป็นข้อๆ\n"
    "- **ถ้าไม่เจอ ให้ตอบว่าไม่มีแล้วหยุด** ห้ามต่อท้ายว่าเจอกี่เล่มหรือให้ไปดูการ์ด\n"
    "- ถ้าถามรีวิว ให้ชวนกดลิงก์ 'ดูรีวิวเล่มนี้ใน Google' ที่อยู่ในการ์ดหนังสือ"
)

CHAT_SYSTEM_PROMPT = CHAT_PROMPT_STAFF   # เผื่อโค้ดเก่าที่ยังอ้างชื่อเดิม

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_books",
        "description": "ค้นหนังสือในสต็อกร้านจากชื่อ ผู้เขียน สำนักพิมพ์ ISBN หรือคำที่เกี่ยวกับเนื้อเรื่อง",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "คำค้น เช่น ชื่อหนังสือ ผู้เขียน หรือแนวเรื่อง"}
            },
            "required": ["query"],
        },
    },
}


def _search_contained_in(text, limit=6):
    """หาเล่มที่ 'ชื่อหนังสือ/ผู้เขียน' ปรากฏอยู่ข้างในข้อความที่ส่งมา

    ใช้เป็นชั้นสำรองของการค้น เพราะคำค้นมักมีคำถามติดมาด้วย
    (เช่น 'มีข้างหลังภาพ' ซึ่งค้นแบบปกติจะไม่ตรงกับชื่อ 'ข้างหลังภาพ')
    """
    t = (text or "").strip()
    if len(t) < 3:
        return []
    conn = db()
    rows = conn.execute(
        "SELECT * FROM titles WHERE (length(title) >= 4 AND ? LIKE '%' || title || '%')"
        " OR (author IS NOT NULL AND length(author) >= 4 AND ? LIKE '%' || author || '%')"
        " ORDER BY length(title) DESC LIMIT ?",
        (t, t, limit),
    ).fetchall()
    out = []
    for r in rows:
        copies = conn.execute(
            "SELECT * FROM copies WHERE title_id=? ORDER BY"
            " CASE grade WHEN 'A' THEN 1 WHEN 'B' THEN 2 ELSE 3 END",
            (r["id"],),
        ).fetchall()
        out.append({**dict(r), "copies": [dict(c) for c in copies],
                    "in_stock": sum(1 for c in copies if c["status"] == "in_stock")})
    conn.close()
    return out


def _compact_titles(titles, mode="staff"):
    """ย่อผลค้นให้เหลือเฉพาะที่โมเดลต้องใช้ตอบ (ประหยัด token)

    โหมดลูกค้าตัดข้อมูลหลังร้านออกตั้งแต่ชั้นนี้ — ไม่ส่ง 'ชั้นวาง' กับ 'ต้นทุน' ให้โมเดลเห็นเลย
    ปลอดภัยกว่าการสั่งใน prompt ว่า "ห้ามพูดถึง" เพราะถ้าไม่มีข้อมูล มันหลุดปากไม่ได้
    แต่เพิ่ม ปีพิมพ์/เรื่องย่อ/สำนักพิมพ์ ให้ เพราะเป็นสิ่งที่ลูกค้าถามบ่อย
    """
    if mode == "customer":
        compact = [{
            "title": t["title"], "author": t["author"],
            "publisher": t.get("publisher"), "year": t.get("year"),
            "category": t.get("category"),
            "synopsis": (t.get("synopsis") or "")[:400],
            "copies": [{"สภาพ": GRADE_LABEL.get(c["grade"], c["grade"]), "ราคา": c["price"]}
                       for c in t["copies"] if c["status"] == "in_stock"][:5],
        } for t in titles]
    else:
        compact = [{
            "title": t["title"], "author": t["author"],
            "year": t.get("year"), "in_stock": t["in_stock"],
            "synopsis": (t.get("synopsis") or "")[:400],
            "copies": [{"grade": c["grade"], "price": c["price"],
                        "shelf": c["shelf"], "cost": c.get("cost")}
                       for c in t["copies"] if c["status"] == "in_stock"][:5],
        } for t in titles]
    return {"found": len(compact), "books": compact}


def _tool_search_books(query, mode="staff"):
    titles = _search_titles(query, limit=6)
    return titles, _compact_titles(titles, mode)


def _chat_completion(messages, tools=None, force_tool=None):
    body = {"model": CHAT_MODEL, "messages": messages, "temperature": 0.4}
    if tools:
        body["tools"] = tools
    if force_tool:
        body["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
    return typhoon_post(body, timeout=90)


KEYWORD_PROMPT = (
    "จากบทสนทนาของลูกค้าร้านหนังสือ ให้ตอบกลับมาเฉพาะ 'คำค้น' ที่ควรใช้ค้นฐานข้อมูลหนังสือ\n"
    "ตอบเป็นคำสั้นๆบรรทัดเดียว ห้ามมีคำอธิบาย ห้ามมีเครื่องหมายคำพูด\n"
    "ถ้าเป็นชื่อหนังสือให้ตอบชื่อหนังสือ ถ้าถามแนวเรื่องให้ตอบคำที่บอกแนวนั้น\n"
    "**ถ้าข้อความล่าสุดไม่ได้บอกชื่อหนังสือ (เช่น 'ขอดูรีวิว' 'เล่มนี้ราคาเท่าไหร่' "
    "'มีสภาพอื่นไหม') ให้ย้อนดูว่าลูกค้าถามถึงหนังสือเล่มไหนไว้ก่อนหน้า "
    "แล้วตอบชื่อหนังสือเล่มนั้น**\n"
    "ห้ามตอบคำว่า รีวิว/ราคา/สภาพ เป็นคำค้น เพราะไม่ใช่ชื่อหนังสือ\n\n"
)


def _extract_query(user_msg, history=None):
    """ให้โมเดลสกัดคำค้นจากบทสนทนา — ทำเป็นข้อความล้วนเพราะเสถียรกว่า tool-call arguments มาก
    (วัดแล้ว: ข้อความล้วนถูก 12/12 ครั้ง ส่วน function calling พลาดราว 40%)

    ส่งบทสนทนาก่อนหน้าไปด้วย เพื่อให้คำถามต่อเนื่องอย่าง "ขอดูรีวิวหน่อย"
    รู้ว่าหมายถึงหนังสือเล่มไหนที่กำลังคุยกันอยู่
    """
    # เอาแต่ข้อความที่ "ลูกค้า" พิมพ์ — ไม่เอาคำตอบของพนักงาน/AI เพราะโมเดลจะไปหยิบ
    # ข้อความสรุปอย่าง "เจอ 1 เล่ม" มาเป็นคำค้นแทนชื่อหนังสือ
    prev = [str(m.get("content") or "").strip()
            for m in (history or [])[:-1] if m.get("role") == "user"]
    prev = [x for x in prev if x][-3:]
    convo = "\n".join(f"- {x[:200]}" for x in prev)
    prompt = KEYWORD_PROMPT + (f"ลูกค้าถามอะไรมาก่อนหน้านี้:\n{convo}\n\n" if convo else "") \
        + f"ข้อความล่าสุดของลูกค้า: {user_msg}"
    try:
        r = typhoon_post({
            "model": TEXT_MODEL, "temperature": 0, "max_tokens": 40,
            "messages": [{"role": "user", "content": prompt}],
        }, timeout=60)
        kw = (r["choices"][0]["message"].get("content") or "").strip()
        kw = kw.splitlines()[0].strip(' "\'') if kw else ""
        return kw or user_msg
    except Exception:
        return user_msg  # สกัดไม่ได้ก็ใช้ข้อความลูกค้าไปค้นตรงๆ


REVIEW_WORDS = ("รีวิว", "review", "เสียงตอบรับ", "คนอ่าน", "ความเห็น",
                "วิจารณ์", "น่าอ่าน", "feedback")

# คำที่บอกว่ากำลังถาม "คุณสมบัติของเล่มที่คุยกันอยู่" ไม่ใช่เริ่มหาเล่มใหม่
FOLLOWUP_WORDS = ("สภาพ", "ปีไหน", "ปีพิมพ์", "พิมพ์ปี", "พิมพ์ครั้ง", "ราคา", "กี่บาท",
                  "ชั้นไหน", "ชั้นวาง", "อยู่ไหน", "รีวิว", "เรื่องย่อ", "ย่อ",
                  "กี่เล่ม", "เหลือ", "ผู้เขียน", "ใครเขียน", "สำนักพิมพ์", "ผู้แปล",
                  "เล่มนี้", "เล่มนั้น", "อันนี้", "เล่มเดิม", "ต้นทุน")
# คำที่บอกว่ากำลัง "เริ่มหาเล่มใหม่" — ต้องชนะคำข้างบน ไม่ให้ยึดเล่มเดิมผิด
NEWSEARCH_WORDS = ("มี", "หา", "แนะนำ", "แนว", "หมวด", "อยากได้", "ขอ")


def _match_category(text):
    """เทียบข้อความกับหมวดหมู่ของร้านแบบยืดหยุ่น คืนชื่อหมวดที่ตรง หรือ None

    ลูกค้าพิมพ์ 'หนังสือธุรกิจการเงิน' แต่ในฐานข้อมูลเก็บว่า 'ธุรกิจ/การเงิน'
    ค้นด้วย LIKE ตรงๆ จะไม่เจอเพราะมีขีดคั่น จึงต้องตัดขีดกับช่องว่างออกก่อนเทียบ
    """
    norm = lambda s: re.sub(r"[\s/]", "", s or "")
    t = norm(text)
    if len(t) < 3:
        return None
    for cat in CATEGORIES:
        if cat == "อื่นๆ":
            continue
        if norm(cat) in t:
            return cat
    # เทียบทีละส่วนของหมวดที่มีขีดคั่น เช่น 'ธุรกิจ/การเงิน' -> 'ธุรกิจ' หรือ 'การเงิน'
    for cat in CATEGORIES:
        for part in cat.split("/"):
            if len(part) >= 4 and norm(part) in t:
                return cat
    return None


def _is_followup(msg):
    """คำถามต่อเนื่องถึงเล่มเดิม = มีคำถามคุณสมบัติ แต่ไม่ได้ขอค้นเล่มใหม่"""
    m = (msg or "").strip()
    if len(m) > 40:          # ประโยคยาวมักมีชื่อหนังสือหรือเงื่อนไขใหม่อยู่ในตัว
        return False
    if not any(w in m for w in FOLLOWUP_WORDS):
        return False
    # "มีเล่มนี้ไหม" ยังถือเป็นต่อเนื่อง แต่ "มีหนังสือธุรกิจไหม" ไม่ใช่
    if any(w in m for w in ("เล่มนี้", "เล่มนั้น", "อันนี้", "เล่มเดิม")):
        return True
    return not any(m.startswith(w) or (" " + w) in m for w in NEWSEARCH_WORDS)


def _review_url(t):
    q = " ".join(x for x in (t.get("title"), t.get("title_alt"),
                             t.get("author"), "รีวิว หนังสือ") if x)
    return "https://www.google.com/search?q=" + urllib.parse.quote(q)


def api_chat(history, mode="staff"):
    """history = [{role:'user'|'assistant', content:str}, ...] จบด้วยข้อความล่าสุดของลูกค้า

    ทำ 3 ขั้นแบบกำหนดแน่นอน (ไม่พึ่ง function calling): สกัดคำค้น -> ค้นฐานข้อมูลด้วยโค้ด -> ให้โมเดลเรียบเรียงคำตอบ
    วิธีนี้ "รับประกัน" ว่าค้นฐานข้อมูลก่อนตอบทุกครั้งจริง แข็งแรงกว่าการหวังให้โมเดลเรียก tool เอง

    mode="customer" — คุยแบบพนักงานขาย ตอบเป็นข้อความล้วน ไม่ส่งการ์ด ไม่เห็นข้อมูลหลังร้าน
    mode="staff"    — ผู้ช่วยพนักงาน เห็นข้อมูลครบ ส่งการ์ดหนังสือมาให้ด้วย
    """
    user_msg = next((m.get("content", "") for m in reversed(history)
                     if m.get("role") == "user"), "").strip()
    if not user_msg:
        return {"reply": "พิมพ์คำถามได้เลยครับ", "books": []}

    # คำถามต่อเนื่อง: ถามคุณสมบัติของเล่มเดิมโดยไม่เอ่ยชื่อ ("สภาพเป็นยังไง", "พิมพ์ปีไหน")
    # ต้องยึดเล่มที่คุยกันอยู่ ห้ามค้นใหม่ เพราะตัวสกัดคำค้นชอบแต่งชื่อหนังสือขึ้นมา
    # แล้วบางทีชื่อที่แต่งไปตรงกับเล่มอื่นในร้านพอดี ทำให้ตอบข้ามเล่มโดยไม่มีใครรู้
    titles = compact = None
    if _is_followup(user_msg):
        for old in reversed([str(m.get("content") or "").strip()
                             for m in history[:-1] if m.get("role") == "user"]):
            got = _search_contained_in(old)
            if got:
                titles, compact = got, _compact_titles(got, mode)
                break

    query = user_msg
    if not titles:
        query = _extract_query(user_msg, history)
        titles, compact = _tool_search_books(query, mode)

    # ชั้นสำรอง 1: คำค้นมักมีคำถามติดมา ("มีข้างหลังภาพ") — หาชื่อเล่มที่อยู่ข้างในคำค้น
    if not titles:
        for cand in (query, user_msg):
            got = _search_contained_in(cand)
            if got:
                titles = got
                compact = _compact_titles(titles, mode)
                break

    # ชั้นสำรอง 2: โมเดลบางครั้งส่งคำค้นมาหลายคำ ("ลูกอีสาน, ราคา") ซึ่งค้นรวมกันแล้วไม่เจอ
    # จึงแยกค้นทีละคำแล้วรวมผล
    if not titles:
        terms = [t.strip() for t in re.split(r"[,ฯ/|]+|\s{1,}", query) if len(t.strip()) >= 2]
        merged, seen_ids = [], set()
        for t in terms:
            got, _ = _tool_search_books(t, mode)
            for row in got:
                if row["id"] not in seen_ids:
                    seen_ids.add(row["id"])
                    merged.append(row)
        if merged:
            titles = merged[:6]
            compact = _compact_titles(titles, mode)

    # ยังไม่เจอ ลองค้นด้วยข้อความเดิมของลูกค้าตรงๆ
    if not titles and query != user_msg:
        titles, compact = _tool_search_books(user_msg, mode)

    # ชั้นสำรอง 3: ถามหาตามแนวเรื่อง/หมวดหมู่ ("มีหนังสือธุรกิจการเงินไหม")
    # เทียบกับหมวดของร้านแบบตัดขีดคั่นออก แล้วค้นด้วยชื่อหมวดที่ถูกต้อง
    if not titles:
        cat = _match_category(user_msg) or _match_category(query)
        if cat:
            titles, compact = _tool_search_books(cat, mode)

    # ไม่มีชั้นสำรองย้อนดูข้อความเก่าตรงนี้แล้ว — คำถามต่อเนื่องจัดการไปตอนต้นฟังก์ชันแล้ว
    # ถ้าถามหาเล่มใหม่แล้วไม่เจอ ต้องตอบว่า "ไม่มี" ตรงๆ
    # (บั๊กเดิม: ถาม "มีหนังสือธุรกิจไหม" ไม่เจอ แล้วระบบดึงเล่มที่คุยค้างไว้มาโชว์ ทำให้ตอบขัดกันเอง)

    seen, books = set(), []
    for t in titles:
        if t["id"] not in seen:
            seen.add(t["id"])
            books.append(t)

    is_customer = mode == "customer"
    convo = [{"role": "system",
              "content": CHAT_PROMPT_CUSTOMER if is_customer else CHAT_PROMPT_STAFF}]
    convo += [m for m in history if m.get("role") in ("user", "assistant")]
    convo.append({
        "role": "system",
        "content": ("ผลค้นจากฐานข้อมูลสต็อกจริง (ใช้ข้อมูลนี้เท่านั้นในการตอบ ห้ามเพิ่มเล่มที่ไม่อยู่ในนี้):\n"
                    + json.dumps(compact, ensure_ascii=False)),
    })

    # โหมดลูกค้าไม่มีการ์ด จึงแนบลิงก์รีวิวเป็นลิงก์ในแชท และตัดสินด้วยโค้ด ไม่ให้โมเดลแต่ง URL เอง
    links = []
    if is_customer and any(w in user_msg.lower() for w in REVIEW_WORDS):
        links = [{"label": f"รีวิว “{t['title']}” ใน Google", "url": _review_url(t)}
                 for t in books[:3]]

    try:
        r = _chat_completion(convo)
        reply = (r["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:
        return {"reply": f"ขอโทษครับ ระบบขัดข้อง: {exc}", "books": [] if is_customer else books}

    if not reply:
        reply = ("ไม่เจอเล่มที่ถามในร้านครับ" if not books
                 else f"มีอยู่ {len(books)} เล่มครับ" if is_customer
                 else f"เจอ {len(books)} เล่มครับ ดูราคากับชั้นวางจากการ์ดด้านล่างเลย")
    # กันสัญลักษณ์ markdown หลุดออกจอ — หน้าเว็บแสดงข้อความล้วน ไม่ได้แปลง markdown
    # จึงเห็นเป็น *ชื่อหนังสือ* ติดดาวมาเลย
    reply = re.sub(r"^\s*[-*•]\s+", "", reply, flags=re.M)
    reply = re.sub(r"\*{1,2}(.+?)\*{1,2}", r"\1", reply)
    reply = re.sub(r"\n{3,}", "\n\n", reply).strip()
    return {"reply": reply,
            "books": [] if is_customer else books,   # โหมดลูกค้า = ไม่ส่งการ์ด
            "links": links}


def suggest_price(cover_price, grade):
    if not cover_price:
        return None
    return round(cover_price * GRADE_FACTOR.get(grade, 0.35) / 5) * 5


def api_stockin(p):
    """ลงสต็อกเล่มใหม่ — สร้าง title ถ้ายังไม่มี แล้วเพิ่ม copy"""
    conn = db()
    tid = p.get("title_id")
    if not tid:
        cur = conn.execute(
            "INSERT INTO titles(isbn,title,title_alt,author,translator,publisher,"
            "category,edition,year,synopsis,cover_price,source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.get("isbn") or None, p.get("title", "").strip(), p.get("title_alt"),
             p.get("author"), p.get("translator"), p.get("publisher"),
             p.get("category"), p.get("edition"), p.get("year"), p.get("synopsis"),
             float(p.get("cover_price") or 0) or None, p.get("source") or "manual"),
        )
        tid = cur.lastrowid
    grade = p.get("grade", "B")
    price = float(p.get("price") or 0)
    cost = float(p.get("cost") or 0)
    conn.execute(
        "INSERT INTO copies(title_id,grade,price,cost,shelf,status,added_at,note)"
        " VALUES(?,?,?,?,?,'in_stock',?,?)",
        (tid, grade, price, cost, p.get("shelf", "").strip().upper(), now(), p.get("note")),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM titles WHERE id=?", (tid,)).fetchone()
    conn.close()
    return {"ok": True, "title": dict(row)}


def api_sell(copy_id):
    conn = db()
    c = conn.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
    if not c:
        conn.close()
        return {"error": "ไม่พบเล่มนี้"}
    if c["status"] != "in_stock":
        conn.close()
        return {"error": "เล่มนี้ขายไปแล้ว"}
    conn.execute("UPDATE copies SET status='sold' WHERE id=?", (copy_id,))
    conn.execute("INSERT INTO sales(copy_id,price,sold_at) VALUES(?,?,?)",
                 (copy_id, c["price"], now()))
    conn.commit()
    t = conn.execute("SELECT title FROM titles WHERE id=?", (c["title_id"],)).fetchone()
    conn.close()
    profit = (c["price"] or 0) - (c["cost"] or 0)
    return {"ok": True, "title": t["title"], "price": c["price"],
            "shelf": c["shelf"], "profit": profit}


def api_stats():
    conn = db()
    g = lambda sql: conn.execute(sql).fetchone()[0]
    titles = g("SELECT COUNT(*) FROM titles")
    in_stock = g("SELECT COUNT(*) FROM copies WHERE status='in_stock'")
    sold = g("SELECT COUNT(*) FROM copies WHERE status='sold'")
    value = g("SELECT IFNULL(SUM(price),0) FROM copies WHERE status='in_stock'")
    revenue = g("SELECT IFNULL(SUM(price),0) FROM sales")
    cost_sold = g("SELECT IFNULL(SUM(c.cost),0) FROM sales s JOIN copies c ON c.id=s.copy_id")
    no_isbn = g("SELECT COUNT(*) FROM titles WHERE isbn IS NULL")
    shelves = conn.execute(
        "SELECT shelf, COUNT(*) n FROM copies WHERE status='in_stock'"
        " GROUP BY shelf ORDER BY shelf"
    ).fetchall()
    recent = conn.execute(
        "SELECT t.title, c.grade, c.shelf, s.price, s.sold_at FROM sales s"
        " JOIN copies c ON c.id=s.copy_id JOIN titles t ON t.id=c.title_id"
        " ORDER BY s.id DESC LIMIT 8"
    ).fetchall()
    conn.close()
    return {
        "titles": titles, "in_stock": in_stock, "sold": sold,
        "stock_value": value, "revenue": revenue, "profit": revenue - cost_sold,
        "no_isbn": no_isbn,
        "shelves": [dict(s) for s in shelves],
        "recent_sales": [dict(r) for r in recent],
    }


def api_buyback_quote(p):
    """เสนอราคารับซื้อ — คิดจากราคาปกและเกรดสภาพ"""
    cover = float(p.get("cover_price") or 0)
    grade = p.get("grade", "B")
    sell = suggest_price(cover, grade)
    if not sell:
        return {"error": "ต้องมีราคาปกก่อนจึงคำนวณราคารับซื้อได้"}
    offer = round(sell * BUYBACK_RATE / 5) * 5
    return {"sell_price": sell, "offer": offer, "margin": sell - offer,
            "grade_label": GRADE_LABEL.get(grade, grade)}


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BookshopDemo/1.0"

    def log_message(self, fmt, *args):
        pass  # เงียบไว้ ไม่ต้องรกใน journal

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _user(self):
        """คืนชื่อผู้ใช้จาก cookie ถ้าล็อกอินอยู่ ไม่งั้นคืน None"""
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v:
                return check_token(v)
        return None

    def _set_cookie(self, token, days=SESSION_DAYS):
        # Secure ได้เพราะเสิร์ฟผ่าน https เท่านั้น; HttpOnly กัน JS อ่าน cookie
        age = days * 86400 if token else 0
        val = token or "deleted"
        return [("Set-Cookie",
                 f"{COOKIE}={val}; Path=/; Max-Age={age}; "
                 "HttpOnly; Secure; SameSite=Lax")]

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        query = {}
        if "?" in self.path:
            from urllib.parse import parse_qs, unquote
            query = {k: unquote(v[0]) for k, v in parse_qs(self.path.split("?", 1)[1]).items()}

        # ยังไม่ล็อกอิน: หน้าแรกส่งหน้าล็อกอินให้ (URL เดิม ไม่ต้อง redirect
        # เพราะแอปอยู่ใต้ path /bookstore-demo/ ของ nginx — redirect จะพาไปผิดที่)
        if not self._user():
            if path == "/":
                return self._send(200, (BASE / "login.html").read_text(encoding="utf-8"),
                                  "text/html; charset=utf-8")
            return self._send(401, {"error": "ต้องเข้าสู่ระบบก่อน", "auth": False})

        if path == "/":
            return self._send(200, (BASE / "index.html").read_text(encoding="utf-8"),
                              "text/html; charset=utf-8")
        if path == "/zxing.min.js":
            # ไลบรารีอ่านบาร์โค้ดจากกล้อง — เสิร์ฟจากเครื่องเราเอง ไม่พึ่ง CDN ภายนอก
            return self._send(200, (BASE / "zxing.min.js").read_bytes(),
                              "application/javascript; charset=utf-8")
        if path == "/api/search":
            return self._send(200, api_search(query.get("q")))
        if path == "/api/lookup":
            return self._send(200, api_lookup(query.get("isbn")))
        if path == "/api/stats":
            return self._send(200, api_stats())
        if path == "/api/categories":
            return self._send(200, {"categories": CATEGORIES})
        if path == "/api/grades":
            return self._send(200, {"factors": GRADE_FACTOR, "labels": GRADE_LABEL,
                                    "buyback_rate": BUYBACK_RATE})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        p = self._read_json()

        if path == "/api/login":
            u = str(p.get("username") or "")
            pw = str(p.get("password") or "")
            if verify_login(u, pw):
                return self._send(200, {"ok": True},
                                  extra=self._set_cookie(make_token(u.strip() or "user")))
            time.sleep(0.7)   # หน่วงเล็กน้อย กันลองสุ่มรหัสรัวๆ
            return self._send(401, {"error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"})

        if path == "/api/logout":
            return self._send(200, {"ok": True}, extra=self._set_cookie(None, days=0))

        if not self._user():
            return self._send(401, {"error": "ต้องเข้าสู่ระบบก่อน", "auth": False})
        if path == "/api/ai-read":
            mt = p.get("media_type", "image/jpeg")
            images = p.get("images")
            if not images:
                # รองรับรูปแบบเดิม (รูปเดียว) ไว้ด้วย
                one = p.get("image_b64", "")
                if not one:
                    return self._send(400, {"error": "ไม่มีภาพ"})
                images = [{"label": "ปกหน้า", "image_b64": one}]
            if not isinstance(images, list):
                return self._send(400, {"error": "รูปแบบข้อมูลภาพไม่ถูกต้อง"})
            eng = p.get("engine")
            eng = eng if eng in ("gemini", "qwen") else "typhoon"
            res = api_ai_read(images, mt, eng)
            _log_read(eng, len(images), res)
            return self._send(200, res)
        if path == "/api/stockin":
            if not (p.get("title") or p.get("title_id")):
                return self._send(400, {"error": "ต้องมีชื่อหนังสือ"})
            if not p.get("shelf"):
                return self._send(400, {"error": "ต้องระบุชั้นวาง"})
            return self._send(200, api_stockin(p))
        if path == "/api/sell":
            return self._send(200, api_sell(p.get("copy_id")))
        if path == "/api/chat":
            history = p.get("messages") or []
            if not history:
                return self._send(400, {"error": "ไม่มีข้อความ"})
            mode = "customer" if p.get("mode") == "customer" else "staff"
            return self._send(200, api_chat(history, mode))
        if path == "/api/buyback-quote":
            return self._send(200, api_buyback_quote(p))
        if path == "/api/reset":
            DB.unlink(missing_ok=True)
            init_db()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


if __name__ == "__main__":
    init_db()
    print(f"bookstore demo on :{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
