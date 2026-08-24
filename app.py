#!/usr/bin/env python3
"""ระบบร้านหนังสือมือสอง — เดโมสำหรับนำเสนอลูกค้า
stdlib only: http.server + sqlite3 + urllib (ไม่ต้องลง package)
"""
import base64
import json
import os
import sqlite3
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


def openai_key():
    env = Path("/root/content-engine/.env")
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        if line.startswith("OPENAI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  isbn TEXT, title TEXT NOT NULL, author TEXT, publisher TEXT,
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
            "SELECT * FROM titles WHERE title LIKE ? OR author LIKE ?"
            " OR publisher LIKE ? OR IFNULL(isbn,'') LIKE ? OR IFNULL(synopsis,'') LIKE ?"
            " ORDER BY title LIMIT ?",
            (like, like, like, like, like, limit),
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


AI_PROMPT = (
    "คุณเป็นผู้ช่วยลงข้อมูลหนังสือของร้านหนังสือมือสองไทย "
    "อ่านภาพปกหนังสือนี้แล้วดึงข้อมูลออกมาเป็น JSON เท่านั้น ห้ามมีข้อความอื่น\n"
    'รูปแบบ: {"raw_text":"","title":"","author":"","publisher":"","isbn":"","year":"",'
    '"cover_price":"","synopsis":"","confidence":"high|medium|low"}\n'
    "ขั้นตอน: ให้ทำ raw_text ก่อนเป็นอันดับแรก โดยถอดข้อความ*ทุกบรรทัด*ที่เห็นบนปก "
    "ไล่จากบนลงล่าง รวมบรรทัดเล็ก ตัวเลข ปี และราคา คั่นแต่ละบรรทัดด้วย | "
    "แล้วจึงแยกข้อมูลลงช่องอื่นโดยดูจาก raw_text ที่ถอดไว้\n"
    "อ่านทีละช่องตามนี้:\n"
    "- title: ชื่อหนังสือ มักเป็นตัวใหญ่สุดบนปก\n"
    "- author: ชื่อผู้เขียน มักอยู่ใต้ชื่อเรื่อง\n"
    "- publisher: ชื่อสำนักพิมพ์ มักอยู่ล่างสุดของปก อ่านตัวอักษรให้ครบทุกตัว "
    "ห้ามเดาจากชื่อสำนักพิมพ์ที่คุณรู้จัก ให้อ่านจากตัวอักษรที่เห็นเท่านั้น\n"
    "- year: ปีพิมพ์ มองหาเลข 4 หลักที่มีคำว่า พ.ศ. / ค.ศ. / พิมพ์ครั้งที่ กำกับ "
    "ถ้าเห็นตัวเลขปีบนปกต้องใส่ อย่าปล่อยว่าง\n"
    "- cover_price: ราคาปก มองหาเลขที่มีคำว่า ราคา หรือ บาท กำกับ ใส่เฉพาะตัวเลข\n"
    "- isbn: เลข ISBN ถ้าเห็นบนปก\n"
    "- synopsis: เนื้อเรื่องย่อ 1-2 ประโยค จากข้อความที่เห็นบนปกจริงเท่านั้น\n"
    "กติกาสำคัญ: ถ้าช่องไหนอ่านไม่ออกหรือไม่มีบนปก ให้ใส่ค่าว่าง "
    "ห้ามเดา ห้ามเติมข้อมูลจากความรู้ของคุณเองที่ไม่ปรากฏบนปก\n"
    "confidence: ประเมินจากความชัดของภาพและจำนวนช่องที่อ่านได้ครบ"
)


def api_ai_read(image_b64, media_type):
    """ให้ AI อ่านปกหนังสือจากภาพจริง"""
    key = openai_key()
    if not key:
        return {"error": "ไม่พบ API key บนเครื่อง"}
    body = json.dumps({
        "model": "gpt-4o",
        "max_tokens": 700,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": AI_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
            ],
        }],
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=90))
    except Exception as exc:  # network / quota / auth
        return {"error": f"เรียก AI ไม่สำเร็จ: {exc}"}
    text = resp["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.lower().startswith("json") else text
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        return {"error": "AI ตอบมาไม่ใช่ JSON", "raw": text[:400]}
    # AI อ่านตัวเลขไทยพลาดบ่อย — แปลงเป็นเลขอารบิกแล้วบังคับให้คนตรวจ
    THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
    for k in ("year", "cover_price", "isbn"):
        v = data.get(k)
        if isinstance(v, str):
            data[k] = v.translate(THAI_DIGITS)
    if isinstance(data.get("year"), str):
        data["year"] = data["year"].replace("พ.ศ.", "").replace("ค.ศ.", "").strip()
    if isinstance(data.get("cover_price"), str):
        data["cover_price"] = "".join(c for c in data["cover_price"] if c.isdigit() or c == ".")
    usage = resp.get("usage", {})
    return {
        "extracted": data,
        # ช่องที่วัดแล้วว่า AI พลาดบ่อย ต้องให้คนยืนยันก่อนบันทึก
        "needs_check": [k for k in ("year", "cover_price") if data.get(k)],
        "usage": {"in": usage.get("prompt_tokens"), "out": usage.get("completion_tokens")},
    }


CHAT_MODEL = "gpt-4o-mini"  # ราคาถูก พอสำหรับตอบคำถามลูกค้าจากผลค้นหา ไม่ต้องใช้ vision

CHAT_SYSTEM_PROMPT = (
    "คุณคือพนักงานร้านหนังสือมือสองที่คุยกับลูกค้าทางแชท พูดสั้น กระชับ เป็นกันเอง แบบพนักงานจริง\n"
    "กติกาที่ห้ามฝ่าฝืน:\n"
    "- ก่อนตอบคำถามที่เกี่ยวกับหนังสือ (มีไหม, ราคา, สภาพ, ชั้นวาง, แนวเรื่อง) ต้องเรียก search_books ก่อนเสมอ "
    "ห้ามตอบจากความจำหรือความรู้ทั่วไปเกี่ยวกับหนังสือเล่มนั้น\n"
    "- ห้ามบอกราคา ชั้นวาง หรือจำนวนเล่ม ที่ไม่ได้มาจากผลลัพธ์ของ search_books โดยตรง ห้ามเดาหรือแต่งขึ้นเอง\n"
    "- ถ้าค้นแล้วไม่พบ ให้บอกลูกค้าตรงๆว่าไม่มีหรือหาไม่พบ ไม่ต้องแนะนำเล่มอื่นที่ไม่ได้อยู่ในผลค้นหา\n"
    "- ถ้าลูกค้าถามกว้างๆ เช่นแนวเรื่อง ให้ค้นด้วยคำที่เกี่ยวข้องกับแนวนั้น แล้วสรุปให้จากที่เจอจริง\n"
    "- ตอบเป็นข้อความล้วน ไม่ต้องแสดงรายการหนังสือซ้ำในคำตอบ (ระบบจะโชว์การ์ดหนังสือให้ลูกค้าดูเองแล้ว) "
    "ให้พูดสรุปสั้นๆ พอ เช่น 'เจอ 2 เล่มครับ เช็คราคากับชั้นวางได้จากรายการด้านล่างเลยครับ'"
)

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


def _tool_search_books(query):
    titles = _search_titles(query, limit=6)
    compact = [{
        "title": t["title"], "author": t["author"],
        "in_stock": t["in_stock"],
        "copies": [{"grade": c["grade"], "price": c["price"], "shelf": c["shelf"]}
                   for c in t["copies"] if c["status"] == "in_stock"][:5],
    } for t in titles]
    return titles, {"found": len(compact), "books": compact}


def _openai_chat(messages, tools=None):
    key = openai_key()
    if not key:
        raise RuntimeError("ไม่พบ API key บนเครื่อง")
    body = {"model": CHAT_MODEL, "messages": messages, "temperature": 0.4}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=60))


def api_chat(history):
    """history = [{role:'user'|'assistant', content:str}, ...] จบด้วยข้อความล่าสุดของลูกค้า"""
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}] + history
    found_titles = []
    for _ in range(4):  # กันลูปไม่รู้จบ
        try:
            resp = _openai_chat(messages, tools=[SEARCH_TOOL])
        except Exception as exc:
            return {"reply": f"ขอโทษครับ ระบบขัดข้อง: {exc}", "books": []}
        msg = resp["choices"][0]["message"]
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            seen, deduped = set(), []
            for t in found_titles:
                if t["id"] not in seen:
                    seen.add(t["id"])
                    deduped.append(t)
            return {"reply": msg.get("content", ""), "books": deduped}
        messages.append(msg)
        for tc in tool_calls:
            try:
                args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                args = {}
            titles, tool_result = _tool_search_books(args.get("query", ""))
            found_titles.extend(titles)
            messages.append({
                "role": "tool", "tool_call_id": tc["id"],
                "content": json.dumps(tool_result, ensure_ascii=False),
            })
    return {"reply": "ขอโทษครับ ค้นหาหลายรอบแล้วยังไม่ได้คำตอบ ลองพิมพ์ใหม่อีกครั้งครับ", "books": found_titles}


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
            "INSERT INTO titles(isbn,title,author,publisher,year,synopsis,cover_price,source)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (p.get("isbn") or None, p.get("title", "").strip(), p.get("author"),
             p.get("publisher"), p.get("year"), p.get("synopsis"),
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

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

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
        if path == "/api/grades":
            return self._send(200, {"factors": GRADE_FACTOR, "labels": GRADE_LABEL,
                                    "buyback_rate": BUYBACK_RATE})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        p = self._read_json()
        if path == "/api/ai-read":
            img = p.get("image_b64", "")
            if not img:
                return self._send(400, {"error": "ไม่มีภาพ"})
            return self._send(200, api_ai_read(img, p.get("media_type", "image/jpeg")))
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
            return self._send(200, api_chat(history))
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
