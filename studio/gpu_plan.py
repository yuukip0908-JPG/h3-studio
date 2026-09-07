"""Which GPU the Studio is allowed to start, and what it costs to say yes.

START GPU used to mean "restart whatever Pod id is in runpod_state.json". That
is how a session ended up on an A100 at $1.59/hr: the acquisition loop took the
first card RunPod would give it, and nothing in the path had an opinion about
price. This module is that opinion, kept separate from the RunPod client so it
can be tested with no network and no GPU.

Two rules decide everything here:

  * a card may start automatically only if its price is *known* and within
    budget, and only if the baseline has actually been run on it. "Probably
    fine" is not a tier -- an unpriced card is treated as expensive, because
    the alternative is discovering the price on the invoice;
  * anything else is offered to the operator with the number attached, and
    waits for an explicit yes.

Prices come from Pods this account has really run (rest.runpod.io reports
costPerHr per Pod). RunPod's REST v1 has no GPU-type catalogue -- /gputypes is
not in its OpenAPI spec -- and the GraphQL endpoint that does rejects this
account's key, so an unlisted card genuinely has no price until one is created.
"""

# The baseline's measured cache-MISS peak is 47.3 GiB, so 48 GB is the floor.
MIN_VRAM_GB = 48

# A card at or below this may start without asking. The baseline runs at
# $0.84/hr; the cap leaves room for a small price move without opening the door
# to the $1.59 tier.
AUTO_START_MAX_USD_PER_HOUR = 1.00

PREFERRED = "preferred"      # start automatically
ALLOWED = "allowed"          # priced in budget, but the baseline has not run on it
EXPENSIVE = "expensive"      # over budget -- always ask
UNKNOWN = "unknown"          # no price on file -- always ask

# Ordered by preference. usd_per_hour is None when this account has never run
# the card and therefore has no measured price.
CATALOG = [
    {"id": "NVIDIA RTX 6000 Ada Generation", "label": "RTX 6000 Ada 48GB",
     "vram_gb": 48, "usd_per_hour": 0.84, "baseline_verified": True,
     "note": "baseline を実測したカード"},
    {"id": "NVIDIA L40S", "label": "L40S 48GB", "vram_gb": 48,
     "usd_per_hour": None, "baseline_verified": False, "note": "未実行・価格不明"},
    {"id": "NVIDIA L40", "label": "L40 48GB", "vram_gb": 48,
     "usd_per_hour": None, "baseline_verified": False, "note": "未実行・価格不明"},
    {"id": "NVIDIA RTX A6000", "label": "RTX A6000 48GB", "vram_gb": 48,
     "usd_per_hour": None, "baseline_verified": False, "note": "未実行・価格不明"},
    {"id": "NVIDIA A40", "label": "A40 48GB", "vram_gb": 48,
     "usd_per_hour": None, "baseline_verified": False, "note": "未実行・価格不明"},
    {"id": "NVIDIA RTX PRO 5000 Blackwell", "label": "RTX PRO 5000 Blackwell 48GB",
     "vram_gb": 48, "usd_per_hour": None, "baseline_verified": False, "note": "未実行・価格不明"},
    {"id": "NVIDIA A100-SXM4-80GB", "label": "A100 80GB SXM4", "vram_gb": 80,
     "usd_per_hour": 1.59, "baseline_verified": True,
     "note": "2026-09-07 に実際に走らせた。動くが約2倍の価格"},
    {"id": "NVIDIA A100 80GB PCIe", "label": "A100 80GB PCIe", "vram_gb": 80,
     "usd_per_hour": None, "baseline_verified": False, "note": "未実行・価格不明"},
    {"id": "NVIDIA H100 PCIe", "label": "H100 80GB PCIe", "vram_gb": 80,
     "usd_per_hour": None, "baseline_verified": False, "note": "未実行・価格不明"},
    {"id": "NVIDIA H100 80GB HBM3", "label": "H100 80GB HBM3", "vram_gb": 80,
     "usd_per_hour": None, "baseline_verified": False, "note": "未実行・価格不明"},
    {"id": "NVIDIA H100 NVL", "label": "H100 NVL 94GB", "vram_gb": 94,
     "usd_per_hour": None, "baseline_verified": False, "note": "未実行・価格不明"},
    {"id": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
     "label": "RTX PRO 6000 Blackwell 96GB", "vram_gb": 96,
     "usd_per_hour": None, "baseline_verified": False, "note": "未実行・価格不明"},
]

BY_ID = {entry["id"]: entry for entry in CATALOG}


def find(gpu_id):
    return BY_ID.get((gpu_id or "").strip())


def tier(gpu_id, usd_per_hour=None):
    """Classify a card. A live price from the API wins over the catalogue's:
    it is what the account will actually be charged."""
    entry = find(gpu_id)
    price = usd_per_hour if usd_per_hour is not None else (entry or {}).get("usd_per_hour")
    if price is None:
        return UNKNOWN
    if price > AUTO_START_MAX_USD_PER_HOUR:
        return EXPENSIVE
    if not entry or not entry.get("baseline_verified"):
        return ALLOWED
    if entry.get("vram_gb", 0) < MIN_VRAM_GB:
        return EXPENSIVE          # too small to run the baseline at all
    return PREFERRED


def may_auto_start(gpu_id, usd_per_hour=None):
    """Only a priced, baseline-verified, in-budget card starts without asking."""
    return tier(gpu_id, usd_per_hour) == PREFERRED


def auto_start_gpu_ids():
    """The ids a Pod may be created with when nobody is watching."""
    return [e["id"] for e in CATALOG if may_auto_start(e["id"])]


def offer_gpu_ids():
    """Everything else, in preference order, for the confirmation dialog."""
    return [e["id"] for e in CATALOG if not may_auto_start(e["id"])]


def price_card(gpu_id, usd_per_hour=None):
    """What the operator has to see before anything starts billing."""
    entry = find(gpu_id) or {}
    price = usd_per_hour if usd_per_hour is not None else entry.get("usd_per_hour")
    t = tier(gpu_id, usd_per_hour)
    card = {
        "gpu": gpu_id,
        "label": entry.get("label") or gpu_id or "不明",
        "vram_gb": entry.get("vram_gb"),
        "tier": t,
        "baseline_verified": bool(entry.get("baseline_verified")),
        "note": entry.get("note"),
        "usd_per_hour": price,
        "usd_30min": None if price is None else round(price * 0.5, 2),
        "usd_60min": None if price is None else round(price, 2),
        "auto_start": t == PREFERRED,
    }
    catalogue_price = entry.get("usd_per_hour")
    if (usd_per_hour is not None and catalogue_price is not None
            and abs(usd_per_hour - catalogue_price) > 0.01):
        # describe() has no GPU name for a RUNNING Pod and falls back to what was
        # recorded at creation, which after a migration can name a card the Pod
        # does not have. The price is what the account is charged, so the price
        # wins and the name is marked as unverified rather than shown as fact.
        card["name_matches_price"] = False
        card["label"] = "%s?（記録名。実価格 $%.2f/時と一致しません）" % (card["label"], usd_per_hour)
        card["warning"] = ("記録された GPU 名と実際の価格が一致しません。"
                           "実体は nvidia-smi でしか確認できません。")
        return card
    if t == EXPENSIVE:
        card["warning"] = "高額です。確認なしでは起動しません。"
    elif t == UNKNOWN:
        card["warning"] = "このアカウントで実行実績が無く、価格が取得できません。確認なしでは起動しません。"
    elif t == ALLOWED:
        card["warning"] = "価格は許容範囲ですが baseline 未検証です。確認なしでは起動しません。"
    return card


def recommended():
    """The card the UI should show next to START GPU."""
    for entry in CATALOG:
        if may_auto_start(entry["id"]):
            return price_card(entry["id"])
    return None


def estimate_usd(seconds, usd_per_hour):
    if not usd_per_hour or seconds is None or seconds < 0:
        return None
    return round(usd_per_hour * (seconds / 3600.0), 4)
