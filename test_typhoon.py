#!/usr/bin/env python3
"""ทดสอบความแม่นของ Typhoon OCR กับปกหนังสือ เทียบกับ GPT-4o

ใช้ API แบบโฮสต์ของ Typhoon (ไม่ต้องเช่า GPU / ไม่ต้องซื้อเครื่อง)
ความแม่นเท่ากับรันบนเครื่องเอง เพราะเป็น weights ตัวเดียวกัน ต่างแค่ความเร็ว

วิธีใช้:
    export TYPHOON_API_KEY=xxxxx
    python3 test_typhoon.py /path/to/cover1.jpg /path/to/cover2.jpg ...
    python3 test_typhoon.py /path/to/folder/          # ทั้งโฟลเดอร์

stdlib ล้วน ไม่ต้องลง package (pip ถูกบล็อกบนเครื่องนี้)
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TYPHOON_URL = "https://api.opentyphoon.ai/v1/chat/completions"
TYPHOON_MODEL = "typhoon-ocr"  # Typhoon OCR 1.5 (2B) — ตัวล่าสุดที่แนะนำ
# โมเดลแชทของ Typhoon (30B แต่ active 3B แบบ MoE จึงเร็ว) — รองรับ function calling
TYPHOON_CHAT_MODEL = "typhoon-v2.5-30b-a3b-instruct"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# prompt ทางการของ Typhoon OCR 1.5 (คัดจาก repo scb-10x/typhoon-ocr)
# โมเดลนี้เป็น OCR ล้วน — คืน "ข้อความทั้งหมดบนภาพ" ไม่ได้แยกเป็นช่องๆให้
TYPHOON_PROMPT = """Extract all text from the image.

Instructions:
- Only return the clean Markdown.
- Do not include any explanation or extra text.
- You must include all information on the page.

Formatting Rules:
- Tables: Render tables using <table>...</table> in clean HTML format.
- Equations: Render equations using LaTeX syntax with inline ($...$) and block ($$...$$).
- Page Numbers: Wrap page numbers in <page_number>...</page_number>.
- Checkboxes: Use ☐ for unchecked and ☑ for checked boxes."""

# ขั้นที่ 2: แยกข้อความที่ OCR ได้ ออกเป็นช่องข้อมูลหนังสือ
FIELD_PROMPT = """จากข้อความที่อ่านได้จากปกหนังสือด้านล่าง ให้แยกข้อมูลเป็น JSON เท่านั้น
รูปแบบ: {"title":"","author":"","publisher":"","year":"","cover_price":""}
กติกา: ถ้าช่องไหนไม่มีในข้อความ ให้ใส่ค่าว่าง ห้ามเดา ห้ามเติมจากความรู้ของคุณเอง
ปีพิมพ์และราคาให้ใส่เฉพาะตัวเลข

ข้อความจากปก:
"""


def key(env_name, dotenv_path=None, dotenv_key=None):
    v = os.environ.get(env_name)
    if v:
        return v.strip()
    if dotenv_path and Path(dotenv_path).exists():
        for line in Path(dotenv_path).read_text().splitlines():
            if line.startswith((dotenv_key or env_name) + "="):
                return line.split("=", 1)[1].strip().strip('"')
    return None


def post_json(url, api_key, body, timeout=180):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
    )
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout)), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:
        return None, str(e)


def b64(path):
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def media_type(path):
    ext = Path(path).suffix.lower()
    return {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")


def typhoon_ocr(path, api_key):
    """ขั้นที่ 1 — ให้ Typhoon OCR ถอดข้อความทั้งหมดจากภาพ"""
    body = {
        "model": TYPHOON_MODEL,
        "max_tokens": 4096,
        "temperature": 0.1,
        "top_p": 0.6,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": TYPHOON_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:{media_type(path)};base64,{b64(path)}"}},
            ],
        }],
    }
    resp, err = post_json(TYPHOON_URL, api_key, body)
    if err:
        return None, None, err
    text = resp["choices"][0]["message"]["content"].strip()
    # เวอร์ชันเก่าคืน JSON ที่มีคีย์ natural_text — เผื่อไว้
    if text.startswith("{"):
        try:
            text = json.loads(text).get("natural_text", text)
        except json.JSONDecodeError:
            pass
    return text, resp.get("usage", {}), None


def fields_from_text(text, api_key, engine="openai"):
    """ขั้นที่ 2 — แยกข้อความที่ OCR ได้ ออกเป็นช่องข้อมูล (ใช้โมเดลข้อความล้วน ไม่ต้องมองภาพ)

    engine="openai"  → gpt-4o-mini (เทียบเป็นฐาน)
    engine="typhoon" → typhoon chat (ทดสอบท่อ Typhoon ล้วน = แบบที่จะรันบนเครื่องลูกค้าจริง)
    """
    if engine == "typhoon":
        url, key_, model = TYPHOON_URL, api_key, TYPHOON_CHAT_MODEL
    else:
        url, key_, model = OPENAI_URL, api_key, "gpt-4o-mini"
    body = {
        "model": model,
        "max_tokens": 500,
        "temperature": 0,
        "messages": [{"role": "user", "content": FIELD_PROMPT + text}],
    }
    resp, err = post_json(url, key_, body)
    if err:
        return None, err
    out = resp["choices"][0]["message"]["content"].strip()
    # โมเดลบางตัวใส่ reasoning ครอบมา ตัดเอาเฉพาะก้อน JSON
    if out.startswith("```"):
        out = out.split("```")[1]
        out = out[4:] if out.lower().startswith("json") else out
    out = out.strip()
    if not out.startswith("{") and "{" in out:
        out = out[out.index("{"): out.rindex("}") + 1]
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        return None, "ตอบมาไม่ใช่ JSON: " + out[:200]


def gpt4o_direct(path, openai_key):
    """เทียบ: ให้ GPT-4o อ่านปกและแยกช่องในครั้งเดียว (แบบที่เดโมใช้อยู่)"""
    prompt = (
        "อ่านปกหนังสือนี้แล้วตอบเป็น JSON เท่านั้น "
        '{"raw_text":"","title":"","author":"","publisher":"","year":"","cover_price":""} '
        "ให้ทำ raw_text ก่อนโดยถอดข้อความทุกบรรทัดคั่นด้วย | แล้วจึงแยกช่องอื่นจาก raw_text นั้น "
        "ถ้าช่องไหนไม่มีบนปกให้ใส่ค่าว่าง ห้ามเดา ปีและราคาใส่เฉพาะตัวเลข"
    )
    body = {
        "model": OPENAI_MODEL,
        "max_tokens": 900,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{media_type(path)};base64,{b64(path)}"}},
            ],
        }],
    }
    resp, err = post_json(OPENAI_URL, openai_key, body)
    if err:
        return None, err
    out = resp["choices"][0]["message"]["content"].strip()
    if out.startswith("```"):
        out = out.split("```")[1]
        out = out[4:] if out.lower().startswith("json") else out
    try:
        return json.loads(out.strip()), None
    except json.JSONDecodeError:
        return None, "ตอบมาไม่ใช่ JSON: " + out[:200]


def collect_images(args):
    paths = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            paths += sorted(f for f in p.iterdir() if f.suffix.lower() in IMAGE_EXT)
        elif p.suffix.lower() in IMAGE_EXT:
            paths.append(p)
    return paths


def main():
    tk = key("TYPHOON_API_KEY")
    ok = key("OPENAI_API_KEY", "/root/content-engine/.env", "OPENAI_API_KEY")

    if not tk:
        print("ไม่พบ TYPHOON_API_KEY")
        print("ขอ key ได้ที่ https://opentyphoon.ai แล้วรัน:")
        print("  export TYPHOON_API_KEY=xxxxx")
        return 1
    if not ok:
        print("เตือน: ไม่พบ OPENAI_API_KEY — จะข้ามขั้นแยกช่องข้อมูลและการเทียบกับ GPT-4o")

    images = collect_images(sys.argv[1:])
    if not images:
        print(f"ใช้: python3 {Path(sys.argv[0]).name} ปก1.jpg ปก2.jpg  (หรือใส่ชื่อโฟลเดอร์)")
        return 1

    print(f"ทดสอบ {len(images)} ภาพ · Typhoon: {TYPHOON_MODEL} · เทียบกับ: {OPENAI_MODEL}\n")
    results = []

    for i, path in enumerate(images, 1):
        print("=" * 70)
        print(f"[{i}/{len(images)}] {path.name}")
        print("=" * 70)

        t0 = time.time()
        text, usage, err = typhoon_ocr(path, tk)
        dt = time.time() - t0
        if err:
            print(f"  Typhoon ผิดพลาด: {err}\n")
            results.append({"file": path.name, "error": err})
            continue

        print(f"\n-- ข้อความที่ Typhoon OCR อ่านได้ ({dt:.1f}s) --")
        print(text[:800] + ("..." if len(text) > 800 else ""))

        row = {"file": path.name, "typhoon_seconds": round(dt, 1), "typhoon_text": text}

        # ท่อ Typhoon ล้วน (แบบที่จะรันบนเครื่องลูกค้าจริง — ทั้ง OCR และแยกช่องเป็นโมเดลไทย)
        tf, tferr = fields_from_text(text, tk, engine="typhoon")
        if tferr:
            print(f"\n  แยกช่องด้วย Typhoon ไม่สำเร็จ: {tferr}")
        else:
            print(f"\n-- ท่อ Typhoon ล้วน (OCR + {TYPHOON_CHAT_MODEL}) --")
            for k in ("title", "author", "publisher", "year", "cover_price"):
                print(f"  {k:14s}: {tf.get(k)}")
            row["typhoon_only_fields"] = {k: tf.get(k) for k in
                                          ("title", "author", "publisher", "year", "cover_price")}
        time.sleep(0.6)

        if ok:
            fields, ferr = fields_from_text(text, ok, engine="openai")
            if ferr:
                print(f"\n  แยกช่องข้อมูลไม่สำเร็จ: {ferr}")
            else:
                print("\n-- Typhoon OCR + แยกช่องด้วย GPT-4o-mini --")
                for k in ("title", "author", "publisher", "year", "cover_price"):
                    print(f"  {k:14s}: {fields.get(k)}")
                row["typhoon_fields"] = {k: fields.get(k) for k in
                                         ("title", "author", "publisher", "year", "cover_price")}

            g, gerr = gpt4o_direct(path, ok)
            if gerr:
                print(f"\n  GPT-4o ผิดพลาด: {gerr}")
            else:
                print("\n-- GPT-4o อ่านครั้งเดียวจบ (แบบที่เดโมใช้อยู่) --")
                for k in ("title", "author", "publisher", "year", "cover_price"):
                    print(f"  {k:14s}: {g.get(k)}")
                row["gpt4o_fields"] = {k: g.get(k) for k in
                                       ("title", "author", "publisher", "year", "cover_price")}

            if row.get("typhoon_fields") and row.get("gpt4o_fields"):
                diff = [k for k in row["gpt4o_fields"]
                        if str(row["typhoon_fields"].get(k, "")).strip()
                        != str(row["gpt4o_fields"].get(k, "")).strip()]
                print("\n-- ช่องที่ 2 โมเดลตอบไม่ตรงกัน --")
                print("  " + (", ".join(diff) if diff else "ตรงกันทุกช่อง"))
                row["disagree"] = diff

        results.append(row)
        print()
        time.sleep(0.6)  # เคารพ rate limit ของ Typhoon (2 req/s)

    out = Path(__file__).parent / "typhoon_test_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 70)
    print(f"บันทึกผลดิบไว้ที่ {out}")
    print("\nขั้นต่อไป: เปิดไฟล์ผลเทียบกับปกจริงทีละเล่ม นับว่าช่องไหนถูก/ผิดกี่เล่ม")
    print("ช่องที่ต้องดูให้หนักที่สุดคือ ปีพิมพ์ กับ ราคาปก (GPT-4o พลาดบ่อยที่สุด)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
