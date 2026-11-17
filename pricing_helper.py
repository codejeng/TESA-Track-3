#!/usr/bin/env python3
"""
ผู้ช่วยคิดราคายื่นซอง ตลาดเมล็ดกาแฟ (รอบ manual)

โปรแกรมนี้แค่ "คำนวณ" ไม่ติดต่อตลาดเลย (ไม่มี HTTP / API ใดๆ)
คนต้องเอาราคาที่ได้ไปกรอกและกดยื่นในหน้าเว็บเอง

วิธีใช้:
    python pricing_helper.py            # เริ่มหรือเล่นต่อจากไฟล์ pricing_state.json
    python pricing_helper.py --reset    # ล้างข้อมูล เริ่มเกมใหม่

ทุกรอบ:
    1) กรอกผลของซองรอบที่แล้ว (a/p/o/l/s)  -> โปรแกรมปรับตัวคูณ k ให้เอง
    2) กรอก RFQ ของรอบนี้                   -> โปรแกรมบอกว่ายื่น RFQ ไหน ล็อตไหน กี่กระสอบ ราคาเท่าไร
"""
import json
import math
import sys
from pathlib import Path

# ---------------- ปรับค่าได้ ----------------
STATE_FILE = Path("pricing_state.json")
TOTAL_ROUNDS = 6
BAG_KG = 60
SALVAGE = 0.80

# สูตร TV (ตามสไลด์วันที่ 17)
P0 = 120.0
DEFECT_STEPS = [(1, "PREMIUM", 1.08), (3, "GRADE_1", 1.04), (5, "GRADE_2", 1.00),
                (7, "GRADE_3", 0.96), (15, "BELOW_STANDARD", 0.85), (float("inf"), "REJECT", 0.60)]
SCREEN = {12: 0.92, 13: 0.97, 14: 1.00, 15: 1.00, 16: 1.03, 17: 1.03, 18: 1.06}
MOIST_REF, MOIST_HARD, MOIST_PEN, MOIST_HARD_F = 12.5, 15.0, 0.02, 0.60

# ตัวคูณเริ่มต้นต่อประเภทผู้ซื้อ  (S=Specialty, C=Commercial, I=Instant)
START_MARKUP = {"S": 1.15, "C": 1.02, "I": 0.94}
TYPE_NAME = {"S": "SPECIALTY", "C": "COMMERCIAL", "I": "INSTANT"}
# สเปกมาตรฐาน (max_defect, max_moisture, min_screen) · Specialty ต้องกรอกเอง
DEFAULT_SPEC = {"C": (7.0, 12.5, None), "I": (15.0, 14.0, None)}
PRIORITY = {"S": 0, "C": 1, "I": 2}       # ผู้ซื้อที่จ่ายแพงได้ของก่อน

MIN_K, MAX_K = 0.82, 1.60   # ห้ามต่ำกว่า 0.80 เด็ดขาด (ของเหลือขายได้ 0.80 อยู่แล้ว)
STEP_UP = 0.03              # ขายได้หมด -> ขึ้น
STEP_DOWN = 0.02            # OUTBID -> ลง
BELOW_CEIL = 0.03           # โดน ABOVE_LIMIT -> ตั้งต่ำกว่าเพดานที่เจอเท่านี้
PACE = 2.5                  # ยื่นต่อรอบ = ที่เหลือ / รอบที่เหลือ × PACE (รอบสุดท้ายยื่นหมด)
LAST_ROUND_CUT = 0.04       # รอบสุดท้ายลด k ลงอีกเพื่อระบายของ
SPLIT = 2                   # กระจายแต่ละล็อตไปอย่างน้อยกี่ RFQ (เพดานลับเป็นของผู้ซื้อแต่ละราย ไม่ใช่ทั้งประเภท)
SPEC_MARGIN_DEFECT = 0.0    # รอบหลังที่ตัวอย่างคลาดเคลื่อน ลองตั้ง 0.3
SPEC_MARGIN_MOIST = 0.0     # รอบหลังลองตั้ง 0.15
# --------------------------------------------


def grade(d):
    for limit, name, step in DEFECT_STEPS:
        if d <= limit:
            return name, step


def true_value(moisture, defect, screen):
    _, step = grade(defect)
    g = (1 - defect / 100) * step
    if moisture <= MOIST_REF:
        m = 1.0
    elif moisture <= MOIST_HARD:
        m = (1 - moisture / 100) / (1 - MOIST_REF / 100) - MOIST_PEN * (moisture - MOIST_REF)
    else:
        m = MOIST_HARD_F
    s = SCREEN[min(max(int(screen), 12), 18)]
    return P0 * g * m * s


def passes(lot, spec):
    max_d, max_m, min_s = spec
    return (lot["defect"] <= max_d - SPEC_MARGIN_DEFECT
            and lot["moisture"] <= max_m - SPEC_MARGIN_MOIST
            and (min_s is None or lot["screen"] >= min_s))


# ---------------- state ----------------
def save(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def load():
    if STATE_FILE.exists() and "--reset" not in sys.argv:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        sys.exit(0)


# ---------------- ขั้นที่ 0: ล็อต ----------------
def enter_lots():
    print("\nกรอกล็อตทีละบรรทัด:  LOT-ID ความชื้น เมล็ดเสีย ขนาด กระสอบ")
    print("ตัวอย่าง:  LOT-T23-1 12.29 3.85 17 7      (บรรทัดว่าง = จบ)")
    lots = {}
    while True:
        line = ask("  ล็อต> ")
        if not line:
            if lots:
                break
            continue
        try:
            lid, m, d, s, bags = line.split()
            m, d, s, bags = float(m), float(d), int(s), int(bags)
        except ValueError:
            print("  รูปแบบผิด ลองใหม่")
            continue
        tv = round(true_value(m, d, s), 2)
        lots[lid] = {"moisture": m, "defect": d, "screen": s, "bags_total": bags, "bags_left": bags, "tv": tv}
        ok = [TYPE_NAME[t] for t, sp in DEFAULT_SPEC.items() if passes(lots[lid], sp)]
        print(f"    {lid}: {grade(d)[0]} · TV = {tv:.2f} บาท/กก. · ผ่านสเปก: {', '.join(ok) or 'ไม่มี (ยกเว้น Specialty ที่สเปกต่างไป)'}")
    return lots


# ---------------- ขั้นที่ 1: ผลรอบก่อน ----------------
def enter_results(st):
    offers = st.get("last_offers") or []
    if not offers:
        return
    print(f"\n── ผลรอบ {st['round'] - 1} · กรอกผลแต่ละซอง")
    print("   a=ACCEPTED  p N=PARTIAL ขายได้ N กระสอบ  o=OUTBID  l=ABOVE_LIMIT  s=SPEC_MISMATCH  x=ไม่ได้ยื่น")
    print("   ถ้ากรอกราคาในเว็บต่างจากที่แนะนำ ต่อท้ายด้วยราคาจริง เช่น  a 121.5")
    results = []
    for i, o in enumerate(offers):
        while True:
            raw = ask(f"   [{i + 1}] {o['rfq']} {TYPE_NAME[o['type']]:<10} {o['lot']} {o['bags']} กระสอบ @ {o['price']:.2f} (k {o['k']:.3f}) > ").lower().split()
            if not raw or raw[0] not in "apolsx":
                print("   ใส่ a / p N / o / l / s / x")
                continue
            code, rest = raw[0], raw[1:]
            filled = o["bags"] if code == "a" else 0
            try:
                if code == "p":
                    filled = int(rest.pop(0))
                if rest:
                    o["price"] = float(rest[0])
                    o["k"] = o["price"] / st["lots"][o["lot"]]["tv"]
            except (ValueError, IndexError):
                print("   p ต้องตามด้วยจำนวนกระสอบที่ขายได้ เช่น  p 3")
                continue
            break
        if code == "x":
            continue
        lot = st["lots"][o["lot"]]
        lot["bags_left"] -= filled
        st["revenue"] += filled * BAG_KG * o["price"]
        results.append((o["type"], o["k"], code.upper()))
    update_markups(st, results)
    st["last_offers"] = []


def update_markups(st, results):
    by = {}
    for t, k, code in results:
        if code != "S":                      # SPEC_MISMATCH ไม่เกี่ยวกับราคา
            by.setdefault(t, []).append((code, k))
    for t, rs in by.items():
        codes = [c for c, _ in rs]
        old = st["markup"][t]
        if "L" in codes and ("A" in codes or "P" in codes):
            new, why = max(k for c, k in rs if c in "AP"), "บางรายเกินเพดาน บางรายรับ คงราคา (เพดานต่างกันแต่ละผู้ซื้อ)"
        elif "L" in codes:
            ceil = min(k for c, k in rs if c == "L")
            st["ceiling"][t] = min(st["ceiling"].get(t, 99), ceil)
            new, why = ceil - BELOW_CEIL, f"เกินเพดานที่ k {ceil:.3f}"
        elif all(c == "A" for c in codes):
            new, why = max(k for _, k in rs) + STEP_UP, "ขายได้หมด ลองขึ้นราคา"
        elif "O" in codes and "A" not in codes and "P" not in codes:
            new, why = min(k for c, k in rs if c == "O") - STEP_DOWN, "โดน OUTBID ลดราคา"
        else:
            new, why = old, "ใกล้จุดพอดี คงราคา"
        if t in st["ceiling"] and not all(c == "L" for c in codes) and new > st["ceiling"][t] - 0.01:
            new, why = st["ceiling"][t] - 0.01, why + " · ติดเพดานที่เคยเจอ"
        st["markup"][t] = round(min(max(new, MIN_K), MAX_K), 3)
        print(f"   {TYPE_NAME[t]:<10} k {old:.3f} → {st['markup'][t]:.3f}  ({why})")


# ---------------- ขั้นที่ 2: RFQ และแผนยื่น ----------------
def enter_rfqs(st):
    print(f"\n── RFQ รอบ {st['round']}/{st['total_rounds']} · กรอกทีละบรรทัด:  ประเภท กระสอบ [เสียสูงสุด ชื้นสูงสุด ขนาดต่ำสุด]")
    print("   ประเภท: c=Commercial  i=Instant  s=Specialty (ต้องใส่สเปก)   เช่น  c 10   ·   s 4 3 12 16   (บรรทัดว่าง = จบ)")
    rfqs = []
    while True:
        line = ask("   RFQ> ").split()
        if not line:
            return rfqs
        t = line[0].upper()
        if t not in TYPE_NAME:
            print("   ประเภทต้องเป็น c / i / s")
            continue
        try:
            bags = int(line[1])
            if len(line) >= 4:
                spec = (float(line[2]), float(line[3]), int(line[4]) if len(line) > 4 and line[4] != "-" else None)
            else:
                spec = DEFAULT_SPEC[t]
        except (KeyError, ValueError, IndexError):
            print("   รูปแบบผิด (Specialty ต้องใส่สเปก)")
            continue
        rfqs.append({"id": f"#{len(rfqs) + 1}", "type": t, "bags": bags, "spec": spec})


def plan(st, rfqs):
    rounds_left = st["total_rounds"] - st["round"] + 1
    last = rounds_left <= 1
    budget = {}
    for lid, lot in st["lots"].items():
        left = lot["bags_left"]
        budget[lid] = left if last else min(left, math.ceil(left / rounds_left * PACE))
    per_rfq = {lid: max(1, math.ceil(b / SPLIT)) for lid, b in budget.items()}
    k_now = {t: (max(MIN_K, k - LAST_ROUND_CUT) if last else k) for t, k in st["markup"].items()}

    def n_options(lid):     # ล็อตที่มีทางไปน้อย ให้ได้คิวก่อนใน RFQ ที่มันผ่าน
        return sum(passes(st["lots"][lid], r["spec"]) for r in rfqs)

    offers = []
    for r in sorted(rfqs, key=lambda r: (PRIORITY[r["type"]], -r["bags"])):
        need = r["bags"]
        elig = [lid for lid, lot in st["lots"].items() if passes(lot, r["spec"]) and budget[lid] > 0]
        elig.sort(key=lambda lid: (n_options(lid), -st["lots"][lid]["tv"]))
        for lid in elig:
            take = min(need, budget[lid], per_rfq[lid])
            if take <= 0:
                continue
            k = k_now[r["type"]]
            offers.append({"rfq": r["id"], "type": r["type"], "lot": lid, "bags": take,
                           "k": k, "price": round(k * st["lots"][lid]["tv"], 2)})
            budget[lid] -= take
            need -= take
            if need == 0:
                break
    return offers, budget, last


def show_plan(st, offers, budget, last):
    print(f"\n══ แผนยื่นรอบ {st['round']}" + ("  (รอบสุดท้าย: ระบายของ)" if last else ""))
    if not offers:
        print("   ไม่มีล็อตไหนผ่านสเปก RFQ รอบนี้")
    print(f"   {'RFQ':<5}{'ประเภท':<12}{'ล็อต':<13}{'กระสอบ':>7}{'k':>8}{'ราคา บาท/กก.':>15}")
    for o in offers:
        print(f"   {o['rfq']:<5}{TYPE_NAME[o['type']]:<12}{o['lot']:<13}{o['bags']:>7}{o['k']:>8.3f}{o['price']:>15.2f}")
    held = {lid: b for lid, b in budget.items() if b > 0}
    if held:
        print("   งบรอบนี้ที่ไม่มี RFQ รับ: " + ", ".join(f"{lid} {b} กระสอบ" for lid, b in held.items()))


def show_status(st):
    total_tv = sum(l["tv"] * l["bags_total"] * BAG_KG for l in st["lots"].values())
    left_tv = sum(l["tv"] * l["bags_left"] * BAG_KG for l in st["lots"].values())
    pr = (st["revenue"] + SALVAGE * left_tv) / total_tv
    print("\n── สถานะ")
    for lid, l in st["lots"].items():
        print(f"   {lid:<13} TV {l['tv']:>7.2f} · เหลือ {l['bags_left']}/{l['bags_total']} กระสอบ")
    ks = " · ".join(f"{TYPE_NAME[t]} {k:.3f}" + (f" (เพดาน≈{st['ceiling'][t]:.3f})" if t in st["ceiling"] else "")
                    for t, k in st["markup"].items())
    print(f"   ตัวคูณ: {ks}")
    print(f"   รายได้ {st['revenue']:,.0f} บาท · PR ถ้าจบตอนนี้ ≈ {pr:.3f}")


def commands(st):
    print("\n   Enter = ไปต่อ · k c 1.10 = ตั้งตัวคูณเอง · t LOT-ID 118.84 = แก้ TV · r 3 = ตั้งเลขรอบ · q = ออก")
    while True:
        c = ask("   คำสั่ง> ").split()
        if not c:
            return
        try:
            if c[0] == "q":
                save(st)
                sys.exit(0)
            elif c[0] == "k":
                st["markup"][c[1].upper()] = float(c[2])
            elif c[0] == "t":
                st["lots"][c[1]]["tv"] = float(c[2])
            elif c[0] == "r":
                st["round"] = int(c[1])
            else:
                raise ValueError
            save(st)
            print("   ok")
        except (ValueError, IndexError, KeyError):
            print("   คำสั่งไม่ถูก")


def main():
    st = load()
    if st is None:
        st = {"lots": enter_lots(), "markup": dict(START_MARKUP), "ceiling": {}, "revenue": 0.0,
              "round": 1, "total_rounds": TOTAL_ROUNDS, "last_offers": []}
        save(st)
    while st["round"] <= st["total_rounds"]:
        enter_results(st)
        save(st)
        show_status(st)
        commands(st)
        rfqs = enter_rfqs(st)
        offers, budget, last = plan(st, rfqs)
        show_plan(st, offers, budget, last)
        st["last_offers"] = offers
        st["round"] += 1
        save(st)
    enter_results(st)        # ผลรอบสุดท้าย
    save(st)
    show_status(st)
    print("\nจบ 6 รอบ · ของที่เหลือระบบขายให้ที่ 0.80 × TV")


if __name__ == "__main__":
    main()
