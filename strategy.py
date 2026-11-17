"""Decide this round's offers — กลยุทธ์ "ตามราคาตลาดรายผู้ซื้อ" (buyer-tracking) v2

ภาพรวม (ทุกรอบ):
  1. learn()        ไล่ผลทุกรอบจาก memory (เรียงตามเลขรอบ) แล้วคำนวณใหม่ทั้งหมด -> รีสตาร์ทไม่ลืม
                      - k ของผู้ซื้อแต่ละราย (ไม่ใช่แค่ประเภท)
                      - ส่วนลดเฉพาะล็อต (ล็อตที่แพ้ตลอดทั้งที่ล็อตอื่นราคาเดียวกันชนะ = TV เราประเมินสูงไป)
                      - อัตราขายได้ของเรา (fill rate)
  2. pressure()     ของที่เหลือ เทียบกับที่ยังขายได้จริงภายใต้เพดานกระสอบต่อรอบ -> ตัดสินว่าถือของได้ไหม
  3. price_rfqs()   k ของแต่ละ RFQ = k ผู้ซื้อ − ความเร่ง (+ ซองหยั่งราคาเมื่อขายทันแผน)
  4. build_plan()   แจกกระสอบทีละ 1 ไปยัง RFQ ที่จ่ายดีสุดแบบวนรอบ จนเต็ม capacity
  5. llm_adjust()   (ไม่บังคับ) LLM ขยับ k รายผู้ซื้อได้ไม่เกิน ±0.02
  6. guard()        ตรวจทุกซองก่อนส่ง

price_per_kg = k × TV ที่เราประเมิน · ผู้ซื้อเรียงซองด้วย ราคา ÷ TV จริง (ถูกสุดได้ก่อน) แล้วจ่ายตามซอง
"""
import json
import math
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field

BAG = 60

# ---------------- ตัวเลขที่จูนได้ ----------------
START = {"SPECIALTY": 1.10, "COMMERCIAL": 0.99, "INSTANT": 0.93}   # จากรอบเก็บคะแนนที่ 1
MIN_MARKUP, MAX_MARKUP = 0.85, 1.40     # guard: ห้ามออกนอกช่วงนี้ไม่ว่ากรณีใด
FLOOR_EARLY = 0.88                      # ขายทันแผน: ไม่ขายต่ำกว่านี้ (ถือไว้ขายรอบหลังได้)
FLOOR_LATE = 0.85                       # ขายไม่ทันแผน หรือ 2 รอบสุดท้าย: ยอมลงถึงนี้ (ยังดีกว่า 0.80)

UP = 0.01            # ขายหมด -> ขึ้น
UP_FAST = 0.02       # ขายหมด และผู้ซื้อยังซื้อไม่ครบ -> ขึ้นเร็ว
DOWN = 0.01          # แพ้หมด -> ลง
MAX_DROP = 0.03      # ลงได้มากสุดต่อรอบ
BELOW_CEIL = 0.02    # ABOVE_LIMIT ทุกซอง -> ต่ำกว่าเพดานที่เจอเท่านี้
CEIL_MEMORY = 3      # จำเพดานไว้กี่รอบ
MKT_ADJ = 0.01       # price_max ÷ TV ประมาณต่ำกว่าจุดตัดจริง -> บวกชดเชยเท่านี้
DRIFT_SEEN = 0.3     # รอบที่ไม่ได้ยื่นให้ผู้ซื้อที่เคยยื่น: ขยับเข้าหาราคาตลาดเท่านี้
DRIFT_NEW = 0.5      # ผู้ซื้อที่ไม่เคยยื่นเลย: ขยับเข้าหาราคาตลาดเท่านี้

LOT_STEP = 0.01      # ล็อตแพ้ทั้งที่ล็อตอื่นราคาเดียวกันชนะ -> ลดราคาล็อตนี้ลง
LOT_MAX_OFF = 0.04
BOUNDARY_DEFECT = 0.30   # เมล็ดเสียห่างขอบเกรดไม่ถึงเท่านี้ = TV อาจกระโดด 4% -> ไม่ใช้สอน k ผู้ซื้อ
BOUNDARY_MOIST = 0.15    # ความชื้นห่าง hard_limit ไม่ถึงเท่านี้

PROBE = 0.01         # ซองหยั่งราคา: สูงกว่าราคาหลักเท่านี้ · 1 กระสอบ · เฉพาะตอนขายทันแผน
PROBE_MAX_PRESSURE = 0.8
SWITCH_GAP = 0.03    # ล็อตไปขายผู้ซื้อที่จ่ายต่ำกว่าทางที่ดีสุดของมันได้ไม่เกินเท่านี้
FILL_DEFAULT = 0.7   # อัตราขายได้ตั้งต้น (ยังไม่มีข้อมูล)
TIGHT = 1.0          # pressure เกินนี้ = ขายไม่ทัน -> ใช้ FLOOR_LATE
URGENCY_SLOPE = 0.05 # pressure เกิน 1 ทุก 0.1 -> ลด k 0.005
URGENCY_MAX = 0.04
FESTIVAL_BONUS = 0.01
PRACTICE_LADDER = (-0.03, 0.0, 0.03)    # รอบซ้อม: ยื่นหลายระดับเพื่อหยั่งตลาด (ถ้ารอบซ้อมตัดสต็อกจริง ให้ตั้ง (0, 0, 0))
PACE_NO_CAP = 2.0

MARGIN_DEFECT = 0.30     # เผื่อ noise ตอนเช็กสเปก
MARGIN_MOIST = 0.15

LLM_MAX_DELTA = 0.02
LLM_MAX_SECONDS = 20
# -------------------------------------------------


@dataclass
class Context:
    rules: dict
    calendar: list
    tv: dict
    rng: random.Random
    memory: object = None
    llm: object = None
    seconds_left: float = 999
    notes: list = field(default_factory=list)


# ---------------- ตัวช่วย ----------------
def spec_ok(sample: dict, spec: dict, margin: bool = False) -> bool:
    md, mm = (MARGIN_DEFECT, MARGIN_MOIST) if margin else (0.0, 0.0)
    return (sample["defect_pct"] <= spec["max_defect_pct"] - md
            and sample["moisture_pct"] <= spec["max_moisture_pct"] - mm
            and (spec["min_screen"] is None or sample["screen"] >= spec["min_screen"]))


def rounds_left(state: dict, ctx: Context) -> int:
    rnd, total = state["round"], ctx.rules["timeline"]["rounds"]
    return total if rnd["kind"] == "practice" else max(1, total - rnd["number"] + 1)


def near_boundary(sample: dict, rules: dict) -> bool:
    """ผลวัดอยู่ใกล้จุดที่ TV กระโดด (ขอบเกรด หรือขอบความชื้นหนัก) -> TV ที่ประเมินอาจผิดหลาย %"""
    d, m = sample["defect_pct"], sample["moisture_pct"]
    if any(abs(d - s["max_defect_pct"]) < BOUNDARY_DEFECT for s in rules["defect_steps"]):
        return True
    return abs(m - rules["moisture"]["hard_limit"]) < BOUNDARY_MOIST


def round_order(rnd: dict) -> tuple:
    digits = re.findall(r"\d+", str(rnd.get("round_id", "")))
    return (0 if rnd.get("kind") == "practice" else 1, int(digits[0]) if digits else 0)


def tv_reference(state: dict, ctx: Context) -> dict:
    ref = {}
    for r in state["rfqs"]:
        tvs = [ctx.tv[l["lot_id"]] for l in state["lots"] if spec_ok(l["sample"], r["spec"])]
        if tvs:
            ref[r["buyer_type"]] = max(ref.get(r["buyer_type"], 0), max(tvs))
    best = max(ctx.tv.values()) if ctx.tv else 100.0
    return defaultdict(lambda: best, ref)


# ---------------- 1. เรียนรู้ ----------------
def learn(state: dict, ctx: Context) -> dict:
    """คืน {"buyers": {...}, "lot_off": {lot: offset}, "fill": อัตราขายได้}"""
    mem = ctx.memory
    tv_ref = tv_reference(state, ctx)
    boundary = {l["lot_id"]: near_boundary(l["sample"], ctx.rules) for l in state["lots"]}
    buyers, lot_off = {}, defaultdict(float)
    offered = filled = 0
    rounds = sorted(mem.rounds, key=round_order) if mem else []

    for i, rnd in enumerate(rounds):
        sent = {(o["rfq_id"], o["lot_id"]): o for o in mem.sent.get(rnd["round_id"], [])}
        summ = {s["rfq_id"]: s for s in rnd.get("market_summary") or []}

        # ผลของซองเรา: base_k = k ที่ส่ง − ส่วนลดเฉพาะล็อต ณ ตอนนั้น (replay จึงได้ค่าเดียวกับตอนส่ง)
        entries = defaultdict(list)                   # buyer -> [(lot, status, base_k)]
        for o in rnd.get("my_offers") or []:
            s, m = sent.get((o["rfq_id"], o["lot_id"])), summ.get(o["rfq_id"])
            if not s or not m or o["status"] in ("SPEC_MISMATCH", "CANCELLED"):
                continue
            entries[m["buyer"]].append((o["lot_id"], o["status"], s["markup"] - lot_off[o["lot_id"]]))
            if rnd.get("kind") != "practice":
                offered += o["kg"]
                filled += o.get("filled_kg", 0)

        # ส่วนลดเฉพาะล็อต: แพ้ ทั้งที่ล็อตอื่นของเรา k เท่ากันหรือสูงกว่าขายได้ที่ผู้ซื้อรายเดียวกัน
        for res in entries.values():
            won = [k for _, st, k in res if st in ("ACCEPTED", "PARTIAL")]
            for lot, st, k in res:
                if st in ("OUTBID", "ABOVE_LIMIT") and won and max(won) >= k - 1e-9:
                    lot_off[lot] = max(-LOT_MAX_OFF, lot_off[lot] - LOT_STEP)
                elif st == "ACCEPTED" and lot_off[lot] < 0:
                    lot_off[lot] = min(0.0, lot_off[lot] + LOT_STEP / 2)

        rows_by_buyer = defaultdict(list)
        for m in summ.values():
            rows_by_buyer[m["buyer"]].append(m)

        for buyer, rows in rows_by_buyer.items():
            t = rows[0]["buyer_type"]
            b = buyers.setdefault(buyer, {"type": t, "k": START.get(t, 1.0), "ceil": [], "own": False, "last": ""})
            sold = [r for r in rows if r["bought_kg"] > 0 and r.get("price_max")]
            k_mkt = max(MIN_MARKUP, max(r["price_max"] for r in sold) / tv_ref[t] + MKT_ADJ) if sold else None
            short = any(r["bought_kg"] < r["wanted_kg"] for r in rows)
            res = entries.get(buyer, [])
            clean = [(st, k) for lot, st, k in res if not boundary.get(lot)] or [(st, k) for _, st, k in res]
            st_all = [st for st, _ in clean]
            k_old = b["k"]

            if clean:
                b["own"] = True
                sold_k = [k for st, k in clean if st in ("ACCEPTED", "PARTIAL")]
                if "ABOVE_LIMIT" in st_all:
                    c = min(k for st, k in clean if st == "ABOVE_LIMIT")
                    b["ceil"].append((i, c))
                    new = max(sold_k) if sold_k else c - BELOW_CEIL
                    why = "เกินเพดานบางซอง คงราคาที่ขายได้" if sold_k else f"เกินเพดาน {c:.3f}"
                elif all(st == "ACCEPTED" for st in st_all):
                    new = max(k for _, k in clean) + (UP_FAST if short else UP)
                    why = "ขายหมด" + (" + ผู้ซื้อยังขาดของ" if short else "")
                elif all(st == "OUTBID" for st in st_all):
                    lo = min(k for _, k in clean)
                    new = lo - DOWN
                    if k_mkt:
                        new = max(lo - MAX_DROP, min(new, k_mkt - 0.005))
                    why = "แพ้หมด" + (f" (ตลาด≈{k_mkt:.3f})" if k_mkt else "")
                else:
                    new = max(sold_k) if sold_k else k_old
                    why = "ผลปนกัน คงราคา"
            elif k_mkt:
                w = DRIFT_SEEN if b["own"] else DRIFT_NEW
                new = k_old + w * ((k_mkt - 0.005) - k_old)
                why = f"ไม่ได้ยื่น · ขยับหาตลาด≈{k_mkt:.3f}"
            else:
                new, why = k_old, "ไม่มีข้อมูลใหม่"

            recent = [c for j, c in b["ceil"] if i - j < CEIL_MEMORY]
            if recent and new > min(recent) - 0.01:
                new, why = min(recent) - 0.01, why + " · ติดเพดานล่าสุด"
            b["k"] = round(min(max(new, MIN_MARKUP), MAX_MARKUP), 3)
            b["last"] = f"{rnd['round_id']}: {'/'.join(st_all) or '-'} -> {b['k']:.3f} ({why})"

    fill = min(1.0, max(0.3, filled / offered)) if offered >= 3 * BAG else FILL_DEFAULT
    return {"buyers": buyers, "lot_off": {k: round(v, 3) for k, v in lot_off.items() if v}, "fill": fill}


# ---------------- 2. ขายทันไหม ----------------
def pressure(state: dict, ctx: Context, fill: float) -> float:
    """ของที่เหลือ ÷ ที่น่าจะยังขายได้ (เพดานกระสอบต่อรอบ × รอบที่เหลือ × อัตราขายได้)
    > 1 = ถ้าขายแบบนี้ต่อจะมีของเหลือไปขายที่ 0.80 แน่ๆ"""
    left_bags = sum(l["kg_remaining"] for l in state["lots"]) / BAG
    cap = state.get("round_capacity_kg")
    if cap is None:
        cap = state.get("capacity_left_kg")
    if cap is None:            # ไม่มีเพดาน: เทียบกับแผนขายเท่าๆ กันทุกรอบ
        total = sum(l["kg_total"] for l in state["lots"]) / BAG or 1
        expected = total * rounds_left(state, ctx) / ctx.rules["timeline"]["rounds"]
        return left_bags / max(expected, 1)
    return left_bags / max(cap / BAG * rounds_left(state, ctx) * fill, 1)


# ---------------- 3. ตั้งราคาแต่ละ RFQ ----------------
def price_rfqs(state: dict, ctx: Context, buyers: dict, pres: float) -> tuple[dict, set]:
    rnd = state["round"]
    practice = rnd["kind"] == "practice"
    left = rounds_left(state, ctx)
    tight = not practice and (pres > TIGHT or left <= 2)
    floor = FLOOR_LATE if tight else FLOOR_EARLY
    cut = 0.0 if practice else min(URGENCY_MAX, max(0.0, (pres - 1.0) * URGENCY_SLOPE))
    bonus = FESTIVAL_BONUS if "เทศกาล" in str(rnd.get("calendar", "")) else 0.0
    allow_probe = not practice and pres < PROBE_MAX_PRESSURE and left > 1

    by_type = defaultdict(list)
    for b in buyers.values():
        by_type[b["type"]].append(b["k"])

    k_rfq, probes, seen = {}, set(), defaultdict(int)
    for r in sorted(state["rfqs"], key=lambda r: (r["buyer"], -r["bags"])):
        t = r["buyer_type"]
        base = buyers[r["buyer"]]["k"] if r["buyer"] in buyers else (
            sum(by_type[t]) / len(by_type[t]) if by_type[t] else START.get(t, 1.0))
        n = seen[r["buyer"]]
        seen[r["buyer"]] += 1
        if practice:
            k = base + PRACTICE_LADDER[n % len(PRACTICE_LADDER)]
        else:
            k = base + bonus - cut
            if allow_probe and n == 1:
                k += PROBE
                probes.add(r["rfq_id"])
        k_rfq[r["rfq_id"]] = round(min(max(k, floor), MAX_MARKUP), 3)
    ctx.notes.append(f"pressure {pres:.2f} · urgency -{cut:.3f} · floor {floor} · probe {sorted(probes) or '-'} · k: "
                     + ", ".join(f"{r['rfq_id']}={k_rfq[r['rfq_id']]}" for r in state["rfqs"]))
    return k_rfq, probes


# ---------------- 4. แจกกระสอบ ----------------
def candidates(state: dict, ctx: Context) -> list[dict]:
    out = []
    for lot in state["lots"]:
        if lot["kg_available"] < BAG:
            continue
        for r in state["rfqs"]:
            if spec_ok(lot["sample"], r["spec"]):
                out.append({"lot_id": lot["lot_id"], "rfq_id": r["rfq_id"], "buyer": r["buyer"],
                            "buyer_type": r["buyer_type"], "safe": spec_ok(lot["sample"], r["spec"], margin=True),
                            "tv": ctx.tv[lot["lot_id"]]})
    return out


def round_budget_bags(state: dict, ctx: Context) -> int:
    avail = sum(l["kg_available"] // BAG for l in state["lots"])
    cap = state.get("capacity_left_kg")
    if cap is not None:
        return min(avail, cap // BAG)
    left = rounds_left(state, ctx)
    return avail if left <= 1 else min(avail, math.ceil(avail / left * PACE_NO_CAP))


def build_plan(state: dict, ctx: Context, cands: list[dict], k_rfq: dict, probes: set,
               lot_off: dict | None = None) -> list[dict]:
    lot_off = lot_off or {}
    last = rounds_left(state, ctx) <= 1
    rfqs = {r["rfq_id"]: r for r in state["rfqs"]}
    has_safe = {c["lot_id"] for c in cands if c["safe"]}
    pairs = [c for c in cands if c["safe"] or last or c["lot_id"] not in has_safe]
    best_k = defaultdict(float)
    for c in pairs:
        best_k[c["lot_id"]] = max(best_k[c["lot_id"]], k_rfq[c["rfq_id"]])

    stock = {l["lot_id"]: l["kg_available"] // BAG for l in state["lots"]}
    rfq_room = {i: (min(1, r["bags"]) if i in probes else r["bags"]) for i, r in rfqs.items()}
    budget = round_budget_bags(state, ctx)
    # ราคาหลักก่อน (ผู้ซื้อจ่ายดีก่อน) ซองหยั่งราคาไปท้ายสุด -> ซองหยั่งไม่แย่งโควตา
    order = sorted(rfqs, key=lambda i: (i in probes, -k_rfq[i], -rfqs[i]["bags"]))
    alloc = defaultdict(int)

    while budget > 0:
        progressed = False
        for rid in order:
            if budget <= 0 or rfq_room[rid] <= 0:
                continue
            opts = [c for c in pairs if c["rfq_id"] == rid and stock[c["lot_id"]] > 0
                    and (last or k_rfq[rid] >= best_k[c["lot_id"]] - SWITCH_GAP)]
            if not opts:
                continue
            c = min(opts, key=lambda c: (best_k[c["lot_id"]] - k_rfq[rid], not c["safe"], -c["tv"]))
            alloc[(c["lot_id"], rid)] += 1
            stock[c["lot_id"]] -= 1
            rfq_room[rid] -= 1
            budget -= 1
            progressed = True
        if not progressed:
            break
    return [{"lot_id": lid, "rfq_id": rid, "bags": n, "markup": k_rfq[rid] + lot_off.get(lid, 0.0)}
            for (lid, rid), n in alloc.items()]


def rule_plan(state: dict, ctx: Context, cands: list[dict]) -> list[dict]:
    """ใช้ได้ทั้งตอนปกติและตอน main.py เรียกเป็นทางสำรองเมื่อ decide() พัง"""
    L = learn(state, ctx)
    k_rfq, probes = price_rfqs(state, ctx, L["buyers"], pressure(state, ctx, L["fill"]))
    return build_plan(state, ctx, cands, k_rfq, probes, L["lot_off"])


# ---------------- 5. LLM (ไม่บังคับ) ----------------
SYSTEM = """You advise a coffee-bean seller in a sealed-offer market with many competing sellers.
price = markup x lot value. Buyers take the cheapest offers (price/value) first, reject offers above a hidden
ceiling, and pay the offered price. Unsold beans are worth only 0.80 at the end, and each round we may offer
only a limited number of bags, so falling behind cannot be fixed later.
For each buyer you get our current markup and what happened last round, plus our sales pressure
(above 1 = we are behind and will have leftovers). Return a small delta per buyer in [-0.02, 0.02]:
positive if the buyer looks undersupplied or we sold everything easily, negative if we keep losing.
Use 0 when unsure. Answer only JSON matching the schema."""

SCHEMA = {"type": "object", "required": ["deltas", "reason"], "properties": {
    "deltas": {"type": "array", "items": {"type": "object", "required": ["buyer", "delta"], "properties": {
        "buyer": {"type": "string"}, "delta": {"type": "number"}}}},
    "reason": {"type": "string", "maxLength": 200}}}


def llm_adjust(state: dict, ctx: Context, buyers: dict, pres: float) -> bool:
    timeout = min(LLM_MAX_SECONDS, ctx.seconds_left - 10)
    if not ctx.llm or not buyers or timeout < 3:
        return False
    rnd = state["round"]
    table = [{"buyer": n, "type": b["type"], "markup": b["k"], "last": b["last"]} for n, b in buyers.items()]
    user = (f"Round {rnd['number']} of {rnd['of']} ({rnd['kind']}, calendar {rnd.get('calendar')}). "
            f"Rounds left: {rounds_left(state, ctx)}. Sales pressure: {pres:.2f}.\n"
            f"Buyers:\n{json.dumps(table, ensure_ascii=False)}")
    answer = ctx.llm.ask_json(SYSTEM, user, SCHEMA, timeout=timeout)
    if not answer or not isinstance(answer.get("deltas"), list):
        ctx.notes.append(f"llm skipped: {getattr(ctx.llm, 'last_error', None)}")
        return False
    for d in answer["deltas"]:
        try:
            b = buyers[d["buyer"]]
            delta = min(max(float(d["delta"]), -LLM_MAX_DELTA), LLM_MAX_DELTA)
        except (KeyError, TypeError, ValueError):
            continue
        b["k"] = round(min(max(b["k"] + delta, MIN_MARKUP), MAX_MARKUP), 3)
    ctx.notes.append(f"llm: {str(answer.get('reason'))[:200]}")
    return True


# ---------------- 6. guard ----------------
def guard(state: dict, ctx: Context, plan: list[dict]) -> list[dict]:
    """แปลงแผนเป็นซองที่ตลาดรับแน่นอน · ไม่เชื่อตัวเลขจาก LLM หรือโค้ดส่วนอื่นตรงๆ"""
    lots = {l["lot_id"]: l for l in state["lots"]}
    rfqs = {r["rfq_id"]: r for r in state["rfqs"]}
    room = state["capacity_left_kg"] if state.get("capacity_left_kg") is not None else 10**9
    lot_left = {i: l["kg_available"] for i, l in lots.items()}
    rfq_left = {i: r["kg"] for i, r in rfqs.items()}
    merged = {}
    for p in plan:
        key = (p["lot_id"], p["rfq_id"])
        if key in merged:
            merged[key]["bags"] += p["bags"]
        else:
            merged[key] = dict(p)
    offers = []
    for p in merged.values():
        lot, rfq = lots.get(p["lot_id"]), rfqs.get(p["rfq_id"])
        if lot is None or rfq is None or not spec_ok(lot["sample"], rfq["spec"]):
            ctx.notes.append(f"guard dropped {p}")
            continue
        kg = min(int(p["bags"]) * BAG, lot_left[lot["lot_id"]], rfq_left[rfq["rfq_id"]], room) // BAG * BAG
        if kg < BAG:
            continue
        markup = min(max(float(p["markup"]), MIN_MARKUP), MAX_MARKUP)
        offers.append({"rfq_id": rfq["rfq_id"], "lot_id": lot["lot_id"], "kg": kg,
                       "price_per_kg": round(markup * ctx.tv[lot["lot_id"]], 2)})
        lot_left[lot["lot_id"]] -= kg
        rfq_left[rfq["rfq_id"]] -= kg
        room -= kg
    return offers


# ---------------- รวมทุกขั้น ----------------
def decide(state: dict, ctx: Context) -> tuple[list[dict], str]:
    cands = candidates(state, ctx)
    if not cands:
        return [], "none"
    L = learn(state, ctx)
    pres = pressure(state, ctx, L["fill"])
    used_llm = llm_adjust(state, ctx, L["buyers"], pres)
    for n, b in sorted(L["buyers"].items()):
        ctx.notes.append(f"{n} [{b['type']}] k={b['k']:.3f} · {b['last']}")
    ctx.notes.append(f"fill {L['fill']:.2f} · lot offsets {L['lot_off'] or '-'}")
    k_rfq, probes = price_rfqs(state, ctx, L["buyers"], pres)
    offers = guard(state, ctx, build_plan(state, ctx, cands, k_rfq, probes, L["lot_off"]))
    return offers, "rules+llm" if used_llm else "rules"
