"""WEBAPP_SCHEMA_v1 (decision #33) + fake data for M0 visual demo.

The schema maps merged xlsx column names (decision #27) to tier + display
metadata. Real parsing logic comes in M1.
"""
from __future__ import annotations

# 8 procurement-key columns from merge contract (dark red header in xlsx, decision #14)
HIGHLIGHT_COLUMNS: set[str] = {
    "in_stock",
    "Broker name",
    "Warehouse/vender",
    "Is_orig_manufacture",
    "Is_cheapest",
    "Available Quantity",
    "ship infor after order placed",
    "Unit price w/o VAT (max qty)",
}

# Ordered dict: web rendering preserves this order left-to-right
WEBAPP_SCHEMA_v1: dict[str, dict] = {
    # T1 — always shown, web + email summary (decision #27)
    "Type":                         {"tier": 1, "label": "类型",         "render": "text"},
    "risk":                         {"tier": 1, "label": "风险",         "render": "badge"},
    "MPN_cleaned_byAgent":          {"tier": 1, "label": "MPN（清洗后）",  "render": "text"},
    "Manufacture":                  {"tier": 1, "label": "厂商",         "render": "text"},
    "in_stock":                     {"tier": 1, "label": "现货",         "render": "bool"},
    "Broker name":                  {"tier": 1, "label": "分销商",        "render": "text"},
    "Warehouse/vender":             {"tier": 1, "label": "仓库/供应商",   "render": "text"},
    "Available Quantity":           {"tier": 1, "label": "可用数量",      "render": "qty"},
    "Unit price w/o VAT (max qty)": {"tier": 1, "label": "单价（不含税）",  "render": "price"},
    "Trade Currency":               {"tier": 1, "label": "币种",         "render": "text"},
    "ship infor after order placed": {"tier": 1, "label": "下单后发货",    "render": "text"},
    # T2 — web shows by default, email omits (decision #28)
    "Is_orig_manufacture":          {"tier": 2, "label": "原厂仓?",       "render": "bool"},
    "Is_cheapest":                  {"tier": 2, "label": "最低价?",       "render": "bool"},
    "packaging":                    {"tier": 2, "label": "包装",         "render": "text"},
    "Lead Time (Week)":             {"tier": 2, "label": "Lead Time (周)", "render": "num1"},
}


# ---------- M0 fake data ----------
# Representative rows showing variety: 2 high-risk MCUs, 1 med-risk regulator,
# 1 low-risk passive. All in_stock=True since web filters non-stock out
# (decision #25). Mix of distributors, currencies, prices, packaging.
FAKE_RUN_RESULTS = [
    {
        "Type": "MCU",
        "risk": "high",
        "MPN_cleaned_byAgent": "STM32G030F6P6",
        "Manufacture": "STMicroelectronics",
        "in_stock": True,
        "Broker name": "DigiKey",
        "Warehouse/vender": "DigiKey US",
        "Available Quantity": 12543,
        "Unit price w/o VAT (max qty)": 0.5234,
        "Trade Currency": "USD",
        "ship infor after order placed": "In Stock",
        "Is_orig_manufacture": False,
        "Is_cheapest": True,
        "packaging": "Cut Tape (CT)",
        "Lead Time (Week)": 0.0,
    },
    {
        "Type": "MCU",
        "risk": "high",
        "MPN_cleaned_byAgent": "STM32G030F6P6",
        "Manufacture": "STMicroelectronics",
        "in_stock": True,
        "Broker name": "Mouser",
        "Warehouse/vender": "Mouser US",
        "Available Quantity": 8421,
        "Unit price w/o VAT (max qty)": 0.5789,
        "Trade Currency": "USD",
        "ship infor after order placed": "Same Day",
        "Is_orig_manufacture": False,
        "Is_cheapest": False,
        "packaging": "Cut Tape",
        "Lead Time (Week)": 0.0,
    },
    {
        "Type": "MCU",
        "risk": "high",
        "MPN_cleaned_byAgent": "STM32H743VIT6",
        "Manufacture": "STMicroelectronics",
        "in_stock": True,
        "Broker name": "LCSC",
        "Warehouse/vender": "LCSC 广东仓",
        "Available Quantity": 3420,
        "Unit price w/o VAT (max qty)": 78.21,
        "Trade Currency": "CNY",
        "ship infor after order placed": "次日发货",
        "Is_orig_manufacture": False,
        "Is_cheapest": True,
        "packaging": "编带",
        "Lead Time (Week)": 0.0,
    },
    {
        "Type": "电源 IC",
        "risk": "low",
        "MPN_cleaned_byAgent": "BD18333EUV-ME2",
        "Manufacture": "ROHM Semiconductor",
        "in_stock": True,
        "Broker name": "Element14",
        "Warehouse/vender": "Element14 (US)",
        "Available Quantity": 250,
        "Unit price w/o VAT (max qty)": 2.18,
        "Trade Currency": "USD",
        "ship infor after order placed": "Cut Tape",
        "Is_orig_manufacture": False,
        "Is_cheapest": True,
        "packaging": "Cut Tape",
        "Lead Time (Week)": 4.0,
    },
    {
        "Type": "电源 IC",
        "risk": "low",
        "MPN_cleaned_byAgent": "BD18333EUV-ME2",
        "Manufacture": "ROHM Semiconductor",
        "in_stock": True,
        "Broker name": "Arrow",
        "Warehouse/vender": "Manufacturer warehouse (ROHM)",
        "Available Quantity": 15000,
        "Unit price w/o VAT (max qty)": 1.95,
        "Trade Currency": "USD",
        "ship infor after order placed": "Stocked",
        "Is_orig_manufacture": True,
        "Is_cheapest": False,
        "packaging": "Full Reel",
        "Lead Time (Week)": 2.0,
    },
]


def render_cell(value, render_type: str) -> str:
    """Format a cell value for HTML rendering per render_type hint."""
    if value is None or value == "":
        return "—"
    if render_type == "bool":
        return "✓" if value else "✗"
    if render_type == "qty":
        try:
            return f"{int(value):,}"
        except (ValueError, TypeError):
            return str(value)
    if render_type == "price":
        try:
            return f"{float(value):.4f}"
        except (ValueError, TypeError):
            return str(value)
    if render_type == "num1":
        try:
            return f"{float(value):.1f}"
        except (ValueError, TypeError):
            return str(value)
    if render_type == "badge":
        return str(value)  # rendered as <span class="badge"> in template
    return str(value)
