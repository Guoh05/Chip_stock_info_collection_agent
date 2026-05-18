"""Shared per-run summary.md generator for the chip-availability scrapers.

Each channel scraper imports `write_summary(rec, out_dir, part)` and calls it
after producing the normalized record. The resulting `<MPN>_summary.md` lives in
the per-run folder and is the human-readable view of the scrape.
"""

from __future__ import annotations

from pathlib import Path

# Field display order + Chinese labels for "Key fields" section. Only fields
# that exist (non-None, non-empty) in `extracted` are rendered.
KEY_FIELDS_ORDER = [
    # Identity
    ("lcsc_part_number", "LCSC SKU"),
    ("lcsc_product_id", "LCSC product ID"),
    ("digikey_part_number", "Digikey SKU"),
    ("digikey_product_id", "Digikey product ID"),
    ("mouser_part_number", "Mouser SKU"),
    ("manufacturer_part_number", "Manufacturer P/N (MPN)"),
    ("manufacturer", "Manufacturer"),
    ("manufacturer_cn", "Manufacturer (中文)"),
    # Description
    ("description_en", "Description (EN)"),
    ("description_cn", "Description (中文)"),
    ("detailed_description_cn", "Detailed description (中文)"),
    ("description_intro_cn", "Features (中文)"),
    # Classification
    ("package", "Package"),
    ("shipping_packaging", "Shipping packaging (运输形态)"),
    ("package_qty_line", "Package Qty (per shipping pack)"),
    ("product_arrange", "Packaging form (产品形态)"),
    ("category_name_en", "Category (EN)"),
    ("category_name_cn", "Category (中文)"),
    ("lifecycle_status", "Lifecycle status"),
    ("part_status", "Part status"),
    ("date_code", "Date code (批号)"),
    ("is_normally_stocking", "Normally stocking"),
    ("is_rohs", "RoHS"),
    ("is_hot", "Hot product"),
    ("hts_code", "HTS code"),
    ("eccn", "ECCN"),
    # Order rules
    ("min_order_qty", "Min order qty"),
    ("min_buy_number", "Min buy qty (最小起订)"),
    ("min_whole_number", "Min whole pack qty (整包数)"),
    ("min_packet_unit", "Pack unit (最小包装单位)"),
    ("min_packet_number", "Pack quantity (最小包装数量)"),
    ("min_order_multiplier", "Order multiplier"),
    ("packaging", "Packaging"),
    # Pricing
    ("unit_price_cny", "Unit price (CNY, headline)"),
    ("encap_price", "Encap price 折合1圆盘 (CNY)"),
    # Time
    ("lead_time", "Lead time"),
    ("product_cycle", "Product cycle"),
    ("recently_sales_count", "Recent sales count (30 days)"),
    # Links
    ("datasheet_url", "Datasheet URL"),
    ("image_url", "Image URL"),
    ("weight_kg", "Weight (kg)"),
]


def _row(label: str, value) -> str:
    if value is None or value == "" or value == []:
        return f"- **{label}:** _n/a_"
    return f"- **{label}:** {value}"


def _format_qty(q):
    """Render integer-like qty with commas; leave others untouched."""
    if isinstance(q, int):
        return f"{q:,}"
    try:
        return f"{int(q):,}"
    except (ValueError, TypeError):
        return str(q) if q is not None else ""


STOCK_BREAKDOWN_EXTRA_COLUMNS = [
    # (row_key, header_label) — rendered as additional columns when any
    # breakdown row has the key. Keeps the LCSC/Digikey simple-row case as
    # the default 4-column table while letting HQEW add MPN / MOQ / etc.
    ("mpn", "型号 (MPN)"),
    ("moq", "起订量 (MOQ)"),
    ("batch_code", "批号"),
    ("listing_date", "日期"),
    ("remark", "备注"),
]


def _render_breakdown_table(breakdown: list[dict]) -> list[str]:
    """Render a markdown table for a stock_breakdown row list. Dynamically
    adds extra columns (from STOCK_BREAKDOWN_EXTRA_COLUMNS) for any keys that
    appear on at least one row."""
    if not breakdown:
        return []

    extras = [
        (key, label) for key, label in STOCK_BREAKDOWN_EXTRA_COLUMNS
        if any(r.get(key) not in (None, "") for r in breakdown)
    ]

    headers = ["类型 (label)", "仓库 (warehouse)"]
    for _, label in extras[:3]:  # mpn / moq / batch_code go before quantity
        if label in ("型号 (MPN)", "起订量 (MOQ)", "批号"):
            headers.append(label)
    headers.append("数量 (quantity)")
    for _, label in extras:
        if label in ("日期",):
            headers.append(label)
    headers.append("发货时间 (ship time)")
    for _, label in extras:
        if label in ("备注",):
            headers.append(label)

    # Re-derive the ordered key list to match `headers`
    col_keys: list[tuple[str | None, str]] = []  # (key, kind) where kind=value/label/empty
    col_keys.append((None, "label"))
    col_keys.append((None, "warehouse"))
    for key, label in extras:
        if label in ("型号 (MPN)", "起订量 (MOQ)", "批号"):
            col_keys.append((key, "value"))
    col_keys.append((None, "quantity"))
    for key, label in extras:
        if label == "日期":
            col_keys.append((key, "value"))
    col_keys.append((None, "ship_text"))
    for key, label in extras:
        if label == "备注":
            col_keys.append((key, "value"))

    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for r in breakdown:
        cells = []
        for key, kind in col_keys:
            if kind == "label":
                cells.append(str(r.get("label", "")))
            elif kind == "warehouse":
                cells.append(str(r.get("warehouse", "")))
            elif kind == "quantity":
                q = r.get("quantity")
                cells.append(_format_qty(q) if q is not None else "")
            elif kind == "ship_text":
                cells.append(str(r.get("ship_text", "")))
            else:
                v = r.get(key)
                cells.append(_format_qty(v) if isinstance(v, int) else (str(v) if v else ""))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _render_stock_section(ex: dict) -> list[str]:
    """Render the 现货 / 在途 / SMT breakdown table when the extractor provided it."""
    lines: list[str] = []
    breakdown = ex.get("stock_breakdown") or []
    variants = ex.get("variants") or []

    # Headline totals (channel-agnostic)
    headline_rows = []
    for key, label in [
        ("stock_total", "Stock total (库存总量)"),
        ("stock_text", "Stock total (display)"),
        # Cross-channel canonical fields
        ("stock_now_qty", "现货 quantity"),
        ("stock_now_ship_text", "现货 ship time (发货时间)"),
        ("stock_future_qty", "期货/在途 quantity"),
        ("stock_future_ship_text", "期货/在途 ship time (发货时间)"),
        # LCSC-specific warehouse breakdown
        ("stock_gd_warehouse", "现货 — 广东仓"),
        ("stock_js_warehouse", "现货 — 江苏仓"),
        ("stock_transit", "在途 (in-transit)"),
        ("stock_smt", "SMT扩展库"),
        ("stock_shenzhen", "Stock — Shenzhen"),
        ("stock_domestic_total", "Stock — domestic total"),
        ("stock_overseas_total", "Stock — overseas total"),
    ]:
        if key in ex and ex[key] is not None and ex[key] != "":
            headline_rows.append((label, ex[key]))

    if headline_rows or breakdown:
        lines.append("## Stock")
        lines.append("")
        for label, value in headline_rows:
            lines.append(f"- **{label}:** {_format_qty(value)}")
        lines.append("")

    # Per-variant section — when the channel splits a fuzzy search into
    # multiple distinct MPNs (e.g. HQEW search returning STM... + STM...TR).
    if variants:
        lines.append(f"### Per-variant breakdown ({len(variants)} variants)")
        lines.append("")
        lines.append("| # | MPN | listings | 现货 sum | 包装 | brand |")
        lines.append("|---|---|---|---|---|---|")
        for i, v in enumerate(variants, 1):
            lines.append(
                f"| {i} | {v.get('manufacturer_part_number','?')} | "
                f"{v.get('listing_count','')} | "
                f"{_format_qty(v.get('stock_now_qty'))} | "
                f"{v.get('package','') or ''} | "
                f"{v.get('manufacturer','') or ''} |"
            )
        lines.append("")
        for v in variants:
            mpn = v.get("manufacturer_part_number", "?")
            v_breakdown = v.get("stock_breakdown") or []
            lines.append(
                f"#### {mpn} — {v.get('listing_count',0)} listings "
                f"(top {len(v_breakdown)} shown, sum 现货 = "
                f"{_format_qty(v.get('stock_now_qty'))})"
            )
            lines.append("")
            lines.extend(_render_breakdown_table(v_breakdown))
            lines.append("")
        return lines

    # Channel did not split into variants — render the single combined table.
    if breakdown:
        lines.append("### Stock breakdown (现货 / 在途 / 其他)")
        lines.append("")
        lines.extend(_render_breakdown_table(breakdown))
        lines.append("")

    return lines


def _render_prices(ex: dict) -> list[str]:
    prices = ex.get("prices") or []
    if not prices:
        return []
    lines = [f"## Prices ({len(prices)} tiers)", ""]
    # Detect richest schema
    has_ext = any("ext_price" in t for t in prices)
    has_usd = any("unit_price_usd" in t for t in prices)
    has_cny = any("unit_price_cny" in t for t in prices)
    header = ["min_qty"]
    if has_cny:
        header.append("unit price (CNY)")
    if has_usd:
        header.append("unit price (USD)")
    if not has_cny and not has_usd:
        header.append("unit price")
    if has_ext:
        header.append("ext price")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for t in prices:
        row = [str(t.get("min_qty", ""))]
        if has_cny:
            row.append(str(t.get("unit_price_cny", "")))
        if has_usd:
            row.append(str(t.get("unit_price_usd", "")))
        if not has_cny and not has_usd:
            row.append(str(
                t.get("unit_price")
                or t.get("unit_price_text", "")
            ))
        if has_ext:
            row.append(str(t.get("ext_price", "")))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return lines


def _render_parameters(ex: dict) -> list[str]:
    params = ex.get("parameters") or []
    if not params:
        return []
    lines = [f"## Parameters ({len(params)})", ""]
    # Use whichever set of name keys is populated in the data
    name_keys = ("name", "name_cn", "name_en", "parameterName")
    val_keys = ("value", "value_detail", "value_en", "parameterValue")
    has_en = any(p.get("name_en") for p in params if isinstance(p, dict))
    if has_en:
        lines.append("| 参数 (中文) | Name (EN) | 值 |")
        lines.append("|---|---|---|")
    else:
        lines.append("| 参数 | 值 |")
        lines.append("|---|---|")
    for p in params:
        if not isinstance(p, dict):
            continue
        name = next((p.get(k) for k in name_keys if p.get(k)), "")
        value = next((p.get(k) for k in val_keys if p.get(k)), "")
        if has_en:
            lines.append(f"| {p.get('name_cn') or name} | {p.get('name_en','')} | {value} |")
        else:
            lines.append(f"| {name} | {value} |")
    lines.append("")
    return lines


def _format_attempts(attempts) -> list[str]:
    if not attempts:
        return ["_no attempts recorded_"]
    out = ["| # | Method | Profile | Status | Len | Outcome |",
           "|---|---|---|---|---|---|"]
    for i, a in enumerate(attempts, 1):
        out.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                i,
                a.get("method", ""),
                a.get("profile", ""),
                a.get("status", ""),
                a.get("len", ""),
                a.get("outcome", ""),
            )
        )
    return out


def write_summary(rec: dict, out_dir: Path, part: str) -> Path:
    """Write `<part>_summary.md` and return its path."""
    out_dir = Path(out_dir)
    md: list[str] = []

    channel = rec.get("channel", "?")
    md.append(f"# Scrape summary — {part} ({channel})")
    md.append("")
    md.append(_row("Status", rec.get("status")))
    md.append(_row("Method", rec.get("method")))
    md.append(_row("Data quality", rec.get("data_quality")))
    md.append(_row("Paywall", rec.get("paywall")))
    md.append(_row("Scraped at (UTC)", rec.get("scraped_at_utc")))
    md.append(_row("Source", rec.get("source")))
    if rec.get("resolved_product_url"):
        md.append(_row("Resolved product URL", rec.get("resolved_product_url")))
    if rec.get("item_url"):
        md.append(_row("Item URL", rec.get("item_url")))
    if rec.get("search_url"):
        md.append(_row("Search URL", rec.get("search_url")))
    if rec.get("blocker"):
        md.append(_row("Blocker", rec.get("blocker")))
    md.append("")

    ex = rec.get("extracted") or {}
    if ex:
        # Key identity / classification fields
        md.append("## Key fields")
        md.append("")
        for key, label in KEY_FIELDS_ORDER:
            if key in ex and ex[key] is not None and ex[key] != "":
                md.append(_row(label, ex[key]))
        md.append("")

        # Stock section (totals + breakdown table)
        md.extend(_render_stock_section(ex))

        # Prices
        md.extend(_render_prices(ex))

        # Parameters
        md.extend(_render_parameters(ex))

    md.append("## Attempts")
    md.append("")
    md.extend(_format_attempts(rec.get("attempts")))
    md.append("")

    out = out_dir / f"{part}_summary.md"
    out.write_text("\n".join(md), encoding="utf-8")
    return out
