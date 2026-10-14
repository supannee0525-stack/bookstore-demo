#!/usr/bin/env python3
"""
Automated End-to-End (E2E) User Acceptance Testing (UAT) Suite
Bookstore Demo — Modern Second-hand Bookstore Platform
Tests all core business flows:
1. Catalog search & book inspection
2. Multi-item Cart quoting & Checkout
3. Order tracking by Order Code & Phone
4. Printable Order Receipt & Packing Slip (A4 HTML + Barcode)
5. Order lifecycle updates (Pending -> Paid -> Shipped)
6. Second-hand Book Buyback valuation & request creation
7. Omni-Channel chatbot inquiry simulation
8. Financial & Net Profit analytics dashboard
"""

import unittest
import urllib.request
import urllib.parse
import json
import re
from app import make_token

BASE_URL = "http://127.0.0.1:5480"
STAFF_TOKEN = make_token("nuun", "staff")


class BookstoreE2EUATTestCase(unittest.TestCase):
    test_order_id = None
    test_order_code = None
    test_copy_ids = []

    @classmethod
    def setUpClass(cls):
        cls.headers_staff = {"Cookie": f"bookstore_session={STAFF_TOKEN}", "Content-Type": "application/json"}
        cls.headers_public = {"Content-Type": "application/json"}

    @classmethod
    def tearDownClass(cls):
        # ทำความสะอาดข้อมูลทดสอบ คืนสต็อกหนังสือให้คงสภาพเดิม
        if cls.test_copy_ids:
            try:
                from app import db
                conn = db()
                q_marks = ",".join(["?"] * len(cls.test_copy_ids))
                conn.execute(f"UPDATE copies SET status='in_stock' WHERE id IN ({q_marks})", cls.test_copy_ids)
                conn.execute(f"DELETE FROM sales WHERE copy_id IN ({q_marks})")
                if cls.test_order_id:
                    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (cls.test_order_id,))
                conn.commit()
                conn.close()
            except Exception:
                pass

    def _get(self, path, auth=True):
        url = f"{BASE_URL}{path}"
        headers = self.headers_staff if auth else self.headers_public
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()
            if "application/json" in content_type:
                return resp.status, json.loads(data.decode("utf-8"))
            return resp.status, data.decode("utf-8")

    def _post(self, path, payload, auth=True):
        url = f"{BASE_URL}{path}"
        headers = self.headers_staff if auth else self.headers_public
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()
            if "application/json" in content_type:
                return resp.status, json.loads(data.decode("utf-8"))
            return resp.status, data.decode("utf-8")

    def test_01_catalog_search(self):
        """1. ค้นหาหนังสือในสต็อกและตรวจสอบข้อมูลเล่ม"""
        status, data = self._get("/api/search?q=")
        self.assertEqual(status, 200)
        self.assertIn("results", data)
        self.assertGreater(len(data["results"]), 0, "ต้องมีหนังสือในระบบอย่างน้อย 1 เล่ม")

        first_book = data["results"][0]
        self.assertIn("title", first_book)
        self.assertIn("copies", first_book)
        self.assertGreater(len(first_book["copies"]), 0)

    def test_02_cart_quote_and_checkout(self):
        """2. ระบบตะกร้าสินค้า: คำนวณราคา + ค่าจัดส่ง และ Checkout หลายเล่มพร้อมกัน"""
        # ค้นหาเล่มที่พร้อมขาย (in_stock)
        status, search_data = self._get("/api/search?q=")
        available_copies = []
        for b in search_data["results"]:
            for c in b["copies"]:
                if c.get("status") == "in_stock":
                    available_copies.append(c)
                if len(available_copies) >= 2:
                    break
            if len(available_copies) >= 2:
                break

        self.assertGreaterEqual(len(available_copies), 1, "ต้องมีหนังสือพร้อมขายในสต็อก")
        cids = [c["id"] for c in available_copies]

        # ตรวจสอบการคิดราคาผ่าน /api/cart/quote
        status, quote = self._get(f"/api/cart/quote?copy_ids={','.join(map(str, cids))}", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(quote["item_count"], len(cids))
        self.assertGreater(quote["total_amount"], 0)

        # Checkout ตะกร้าสินค้า
        checkout_payload = {
            "copy_ids": cids,
            "customer_name": "คุณทดสอบ ระบบอัตโนมัติ",
            "phone": "089-999-8888",
            "address": "99/1 อาคารทดสอบ ชั้น 5 แขวงสีลม เขตบางรัก กทม. 10500",
            "payment_method": "promptpay",
            "note": "UAT Automated Test Order"
        }
        status, res = self._post("/api/cart/checkout", checkout_payload, auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(res.get("ok"))
        self.assertIn("order_id", res)
        self.assertIn("order_code", res)

        BookstoreE2EUATTestCase.test_order_id = res["order_id"]
        BookstoreE2EUATTestCase.test_order_code = res["order_code"]
        BookstoreE2EUATTestCase.test_copy_ids = cids

    def test_03_order_tracking(self):
        """3. ติดตามสถานะคำสั่งซื้อผ่าน Order Code และเบอร์โทรศัพท์"""
        order_code = BookstoreE2EUATTestCase.test_order_code
        self.assertIsNotNone(order_code, "ต้องมี order_code จากขั้นตอน checkout")

        # ติดตามด้วย Order Code
        status, res = self._get(f"/api/order/track?query={order_code}", auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(res.get("found"))
        results = res.get("results", [])
        self.assertGreater(len(results), 0)
        order = results[0]
        self.assertEqual(order.get("order_code"), order_code)
        self.assertEqual(order.get("status"), "pending")

        # ติดตามด้วยเบอร์โทรศัพท์
        status, res2 = self._get("/api/order/track?query=089-999-8888", auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(res2.get("found"))

    def test_04_printable_order_receipt_and_slip(self):
        """4. พิมพ์ใบเสร็จรับเงิน / ใบจัดส่งสินค้า (Printable A4 & SVG Barcode)"""
        order_id = BookstoreE2EUATTestCase.test_order_id
        order_code = BookstoreE2EUATTestCase.test_order_code
        self.assertIsNotNone(order_id)

        # ทดสอบการเรียก HTML ใบเสร็จ
        status, html = self._get(f"/order-receipt?id={order_id}", auth=False)
        self.assertEqual(status, 200)
        self.assertIn("ใบเสร็จรับเงิน / ใบจัดส่งสินค้า", html)
        self.assertIn(order_code, html)
        self.assertIn("คุณทดสอบ ระบบอัตโนมัติ", html)
        self.assertIn("<svg", html, "ต้องมี SVG Code128 Barcode สำหรับสแกน")
        self.assertIn("เช็กลิสต์การตรวจสภาพและการแพ็กพัสดุ", html)

    def test_05_order_lifecycle_management(self):
        """5. หลังบ้านเปลี่ยนสถานะคำสั่งซื้อ (ยืนยันรับเงิน -> บันทึกจัดส่งพัสดุ)"""
        order_id = BookstoreE2EUATTestCase.test_order_id
        order_code = BookstoreE2EUATTestCase.test_order_code
        self.assertIsNotNone(order_id)

        # ยืนยันชำระเงิน
        status, res = self._post("/api/order-update", {"order_id": order_id, "action": "pay"})
        self.assertEqual(status, 200)
        self.assertTrue(res.get("ok"))

        # บันทึกการจัดส่งพร้อมเลขพัสดุ
        status, res2 = self._post("/api/order-update", {
            "order_id": order_id,
            "action": "ship",
            "tracking": "TH123456789K"
        })
        self.assertEqual(status, 200)
        self.assertTrue(res2.get("ok"))

        # ตรวจสอบสถานะการติดตามหลังส่ง
        status, track_res = self._get(f"/api/order/track?query={order_code}", auth=False)
        results = track_res.get("results", [])
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0].get("status"), "shipped")
        self.assertEqual(results[0].get("tracking"), "TH123456789K")

    def test_06_buyback_valuation_and_flow(self):
        """6. ระบบประเมินราคารับซื้อคืน (Buy Back 30%) และบันทึกคำขอ"""
        # ทดสอบประเมินราคาผ่าน /api/buyback/estimate
        quote_payload = {
            "items": [
                {"title": "หนังสือทดสอบการประเมินสภาพ A", "cover_price": 300},
                {"title": "หนังสือทดสอบการประเมินสภาพ B", "cover_price": 250}
            ]
        }
        status, quote = self._post("/api/buyback/estimate", quote_payload, auth=False)
        self.assertEqual(status, 200)
        self.assertIn("total_ref_price", quote)
        self.assertGreater(quote.get("total_ref_price", 0), 0)
        self.assertGreater(quote.get("total_cash_offer", 0), 0)
        self.assertGreater(quote.get("total_credit_offer", 0), 0)

        # ส่งคำขอรับซื้อคืนผ่าน /api/buyback/create
        submit_payload = {
            "sender_name": "คุณลูกค้า ขายหนังสือคืน",
            "sender_phone": "081-222-3333",
            "sender_address": "88/2 ถนนพหลโยธิน แขวงสามเสนใน เขตพญาไท กทม 10400",
            "payout_method": "store_credit",
            "items": [
                {"title": "หนังสือทดสอบการประเมินสภาพ A", "cover_price": 300}
            ]
        }
        status, sub_res = self._post("/api/buyback/create", submit_payload, auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(sub_res.get("ok"))
        self.assertIn("tracking_code", sub_res)
        self.assertIn("label_url", sub_res)

    def test_07_omni_channel_simulation(self):
        """7. จำลองการแชท Omni-Channel (Facebook/LINE/Web) สอบถามและแนะนำหนังสือ"""
        payload = {
            "channel": "facebook",
            "user_name": "คุณทดสอบ Facebook UAT",
            "text": "มีหนังสือแนะนำเกี่ยวกับการบริหารเวลาไหมคะ"
        }
        status, sim_res = self._post("/api/omni/simulate", payload, auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(sim_res.get("ok"))
        self.assertIn("result", sim_res)
        result = sim_res["result"]
        self.assertIn("reply", result)
        self.assertGreater(len(result.get("reply", "")), 0)

    def test_08_analytics_dashboard_metrics(self):
        """8. สถิติรายงานวิเคราะห์กำไรสุทธิ (Net Profit) และภาพรวมร้าน"""
        status, stats = self._get("/api/stats")
        self.assertEqual(status, 200)
        self.assertIn("titles", stats)
        self.assertIn("in_stock", stats)
        self.assertIn("gross_profit", stats)
        self.assertIn("net_profit", stats)
        self.assertIn("buyback", stats)
        self.assertIn("orders", stats)
        self.assertIn("channels", stats)

        # ยืนยันโครงสร้าง Buyback & Orders metrics
        bb = stats["buyback"]
        self.assertIn("payout_cash", bb)
        self.assertIn("payout_credit", bb)
        self.assertIn("approved", bb)

        ord_stats = stats["orders"]
        self.assertIn("total", ord_stats)
        self.assertIn("paid", ord_stats)
        self.assertIn("shipped", ord_stats)


if __name__ == "__main__":
    unittest.main(verbosity=2)
