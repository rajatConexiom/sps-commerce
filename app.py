"""Web app to compare SPS Commerce .xerp files with Conexiom Document Extraction API results."""

import json
import os
import re
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template

load_dotenv()

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DOCS = r"C:\Users\rkadian\Downloads\SPS commerce"
DOCS_DIR = os.getenv("SPS_DOCS_DIR") or (_DEFAULT_DOCS if os.path.isdir(_DEFAULT_DOCS) else os.path.join(BASE_DIR, "data"))
CACHE_DIR = os.path.join(BASE_DIR, "cache")

CONEXIOM_TOKEN = os.getenv("CONEXIOM_TOKEN")
CONEXIOM_PROJECT_UUID = os.getenv("CONEXIOM_PROJECT_UUID")
CONEXIOM_API_URL = os.getenv(
    "CONEXIOM_API_URL",
    "https://api.conexiom.com/api/v1/projects/{uuid}/documents/extraction",
)
if not CONEXIOM_TOKEN or not CONEXIOM_PROJECT_UUID:
    raise RuntimeError(
        "CONEXIOM_TOKEN and CONEXIOM_PROJECT_UUID must be set (see .env / .env.example)."
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def list_orders():
    """Find subfolders of DOCS_DIR containing a .xerp file and a PDF."""
    orders = []
    if not os.path.isdir(DOCS_DIR):
        return orders
    for name in sorted(os.listdir(DOCS_DIR)):
        folder = os.path.join(DOCS_DIR, name)
        if not os.path.isdir(folder):
            continue
        xerp = pdf = None
        for f in os.listdir(folder):
            lower = f.lower()
            if lower.endswith(".xerp"):
                xerp = os.path.join(folder, f)
            elif lower.endswith(".pdf"):
                pdf = os.path.join(folder, f)
        if xerp:
            orders.append({"id": name, "xerp": xerp, "pdf": pdf})
    return orders


def get_order(order_id):
    for order in list_orders():
        if order["id"] == order_id:
            return order
    return None


# ---------------------------------------------------------------------------
# Extraction API (mirrors conexiom_sdk_demo ConexiomClient)
# ---------------------------------------------------------------------------

def extract_document(pdf_path, order_id, force=False):
    """Call the Conexiom extraction API, caching results per order."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"{order_id}.json")
    if not force and os.path.isfile(cache_file):
        with open(cache_file, encoding="utf-8-sig") as fh:
            return json.load(fh), True

    if not pdf_path or not os.path.isfile(pdf_path):
        raise FileNotFoundError(
            "Source PDF is missing and no cached extraction exists for this order."
        )
    url = CONEXIOM_API_URL.format(uuid=CONEXIOM_PROJECT_UUID)
    headers = {
        "Authorization": f"Bearer {CONEXIOM_TOKEN}",
        "Accept": "application/json",
        "User-Agent": "PostmanRuntime/7.36.3",
    }
    with open(pdf_path, "rb") as fh:
        files = {"file": (os.path.basename(pdf_path), fh, "application/pdf")}
        response = requests.post(url, headers=headers, files=files, timeout=300)
    if not response.ok:
        raise RuntimeError(f"API error {response.status_code}: {response.text[:500]}")
    result = response.json()
    with open(cache_file, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    return result, False


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def norm_date(value):
    """Normalize dates like '7/15/2026' or '2026-07-15T00:00:00' to YYYY-MM-DD."""
    if value in (None, ""):
        return None
    value = str(value).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return value


def norm_number(value):
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).replace(",", "").replace("$", "")), 2)
    except ValueError:
        return str(value)


def norm_text(value):
    if value in (None, ""):
        return None
    return re.sub(r"\s+", " ", str(value)).strip()


def values_match(a, b):
    if a is None and b is None:
        return None  # both missing -> not applicable
    if a is None or b is None:
        return False
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) < 0.005
    return str(a).strip().lower() == str(b).strip().lower()


# ---------------------------------------------------------------------------
# Field mapping: normalize both documents to a common shape
# ---------------------------------------------------------------------------

ADDR_TYPES = [("ST", "shipTo", "Ship To"), ("BT", "billTo", "Bill To"), ("VN", "shipFrom", "Vendor / Ship From")]

ADDR_FIELDS = [
    ("AddressName", "company", "Name", norm_text),
    ("Address1", "street", "Street", norm_text),
    ("City", "city", "City", norm_text),
    ("State", "state", "State", norm_text),
    ("PostalCode", "zipCode", "Postal Code", norm_text),
    ("Country", "country", "Country", norm_text),
]


def normalize_xerp(xerp):
    header = xerp.get("Header", {})
    order_header = header.get("OrderHeader", {})
    dates = header.get("Dates", []) or []
    req_date = next((d.get("Date") for d in dates if d.get("DateTimeQualifier") == "010"), None)

    addresses = {}
    for addr in header.get("Address", []) or []:
        addresses[addr.get("AddressTypeCode")] = addr

    items = []
    for li in xerp.get("LineItem", []) or []:
        line = li.get("OrderLine", {})
        descs = li.get("ProductOrItemDescription", []) or []
        items.append({
            "sku": norm_text(line.get("BuyerPartNumber")),
            "qty": norm_number(line.get("OrderQty")),
            "unit_price": norm_number(line.get("PurchasePrice")),
            "line_total": norm_number(line.get("ExtendedItemTotal")),
            "description": norm_text(descs[0].get("ProductDescription")) if descs else None,
        })

    return {
        "po_number": norm_text(order_header.get("PurchaseOrderNumber")),
        "po_date": norm_date(order_header.get("PurchaseOrderDate")),
        "requested_delivery": norm_date(req_date),
        "total": norm_number((xerp.get("Summary") or {}).get("TotalAmount")),
        "addresses": addresses,
        "items": items,
    }


def normalize_extraction(result):
    docs = result.get("documents", []) or []
    doc = docs[0] if docs else {}
    document = doc.get("document", {}) or {}
    custom = doc.get("customFields", {}) or {}

    items = []
    for it in doc.get("items", []) or []:
        product = it.get("product", {}) or {}
        items.append({
            "sku": norm_text(product.get("sku") or product.get("externalSku")),
            "qty": norm_number(it.get("quantity")),
            "unit_price": norm_number(it.get("unitPrice")),
            "line_total": norm_number(it.get("extendedLineTotal")),
            "description": norm_text(product.get("description")),
        })

    return {
        "po_number": norm_text(document.get("documentNumber")),
        "po_date": norm_date(document.get("orderDate")),
        "requested_delivery": norm_date(custom.get("requestedDeliveryDate")),
        "total": norm_number((doc.get("totals") or {}).get("total")),
        "addresses": doc.get("addresses", {}) or {},
        "items": items,
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def row(field, xerp_val, api_val):
    match = values_match(xerp_val, api_val)
    return {
        "field": field,
        "xerp": xerp_val,
        "api": api_val,
        "status": "n/a" if match is None else ("match" if match else "diff"),
    }


def compare(xerp_norm, api_norm):
    sections = []

    # Header
    header_rows = [
        row("PO Number", xerp_norm["po_number"], api_norm["po_number"]),
        row("PO Date", xerp_norm["po_date"], api_norm["po_date"]),
        row("Requested Delivery Date", xerp_norm["requested_delivery"], api_norm["requested_delivery"]),
        row("Total Amount", xerp_norm["total"], api_norm["total"]),
    ]
    sections.append({"title": "Order Header", "rows": header_rows})

    # Addresses
    for code, api_key, label in ADDR_TYPES:
        xa = xerp_norm["addresses"].get(code, {})
        aa = api_norm["addresses"].get(api_key, {})
        if not xa and not aa:
            continue
        rows = [
            row(label_f, xerp_f and norm_text(xa.get(xerp_f)), api_f and norm_text(aa.get(api_f)))
            for xerp_f, api_f, label_f, _ in ADDR_FIELDS
        ]
        sections.append({"title": f"Address — {label}", "rows": rows})

    # Line items: match by SKU, fall back to index
    xerp_items = list(xerp_norm["items"])
    api_items = list(api_norm["items"])
    pairs = []
    used_api = set()
    for xi in xerp_items:
        match_idx = next(
            (i for i, ai in enumerate(api_items)
             if i not in used_api and ai["sku"] and xi["sku"]
             and ai["sku"].lower() == xi["sku"].lower()),
            None,
        )
        if match_idx is None:
            match_idx = next((i for i in range(len(api_items)) if i not in used_api), None)
        if match_idx is not None:
            used_api.add(match_idx)
            pairs.append((xi, api_items[match_idx]))
        else:
            pairs.append((xi, None))
    for i, ai in enumerate(api_items):
        if i not in used_api:
            pairs.append((None, ai))

    line_fields = [
        ("sku", "Part Number / SKU"),
        ("qty", "Quantity"),
        ("unit_price", "Unit Price"),
        ("line_total", "Line Total"),
        ("description", "Description"),
    ]
    for idx, (xi, ai) in enumerate(pairs, start=1):
        xi = xi or {}
        ai = ai or {}
        rows = [row(label, xi.get(key), ai.get(key)) for key, label in line_fields]
        sections.append({"title": f"Line Item {idx}", "rows": rows})

    total_rows = sum(len(s["rows"]) for s in sections)
    diffs = sum(1 for s in sections for r in s["rows"] if r["status"] == "diff")
    matches = sum(1 for s in sections for r in s["rows"] if r["status"] == "match")
    return {
        "sections": sections,
        "summary": {"total": total_rows, "matches": matches, "diffs": diffs},
    }


# ---------------------------------------------------------------------------
# Schema comparison
# ---------------------------------------------------------------------------

def schema_paths(obj, prefix=""):
    """Flatten a JSON object into {path: type} (arrays collapsed to [])."""
    paths = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            paths.update(schema_paths(value, f"{prefix}.{key}" if prefix else key))
    elif isinstance(obj, list):
        merged = {}
        for element in obj:
            for p, t in schema_paths(element, f"{prefix}[]").items():
                merged.setdefault(p, set()).add(t)
        if not obj:
            paths[f"{prefix}[]"] = "array (empty)"
        for p, types in merged.items():
            paths[p] = " | ".join(sorted(types))
    else:
        if isinstance(obj, bool):
            t = "boolean"
        elif isinstance(obj, int):
            t = "integer"
        elif isinstance(obj, float):
            t = "number"
        elif obj is None:
            t = "null"
        else:
            t = "string"
        paths[prefix] = t
    return paths


# xerp path -> API path equivalence (None = no counterpart)
SCHEMA_MAPPING = [
    ("Header.OrderHeader.PurchaseOrderNumber", "documents[].document.documentNumber", "PO number"),
    ("Header.OrderHeader.PurchaseOrderDate", "documents[].document.orderDate", "M/D/YYYY string vs ISO 8601 datetime"),
    ("Header.OrderHeader.TradingPartnerId", None, "SPS trading partner id — no API equivalent"),
    ("Header.Dates[].DateTimeQualifier", None, "Qualifier code ('010' = requested delivery)"),
    ("Header.Dates[].Date", "documents[].customFields.requestedDeliveryDate", "Also echoed in items[].lineDueDate"),
    ("Header.Address[].AddressTypeCode", None, "xerp uses array + type code; API uses keyed object (shipTo/billTo/shipFrom)"),
    ("Header.Address[].AddressName", "documents[].addresses.*.company", "ST↔shipTo, BT↔billTo, VN↔shipFrom"),
    ("Header.Address[].Address1", "documents[].addresses.*.street", ""),
    ("Header.Address[].City", "documents[].addresses.*.city", ""),
    ("Header.Address[].State", "documents[].addresses.*.state", ""),
    ("Header.Address[].PostalCode", "documents[].addresses.*.zipCode", ""),
    ("Header.Address[].Country", "documents[].addresses.*.country", ""),
    ("LineItem[].OrderLine.BuyerPartNumber", "documents[].items[].product.sku", "API also has product.externalSku"),
    ("LineItem[].OrderLine.OrderQty", "documents[].items[].quantity", "string '6.0' vs number 6"),
    ("LineItem[].OrderLine.OrderQtyUOM", None, "Unit of measure — no API equivalent"),
    ("LineItem[].OrderLine.PurchasePrice", "documents[].items[].unitPrice", "string vs number; API also has product.price.value"),
    ("LineItem[].OrderLine.ExtendedItemTotal", "documents[].items[].extendedLineTotal", "string vs number; API also has totalPrice.value"),
    ("LineItem[].ProductOrItemDescription[].ProductDescription", "documents[].items[].product.description", "xerp = full PDF text; API = catalog product name"),
    ("Summary.TotalAmount", "documents[].totals.total", "string vs number; API also has totals.subtotal"),
]


def compare_schemas(xerp_raw, api_raw):
    xerp_schema = schema_paths(xerp_raw)
    api_schema = schema_paths(api_raw)

    mapped = []
    used_api_paths = set()
    for xerp_path, api_path, note in SCHEMA_MAPPING:
        api_type = None
        if api_path:
            if "*" in api_path:
                prefix, suffix = api_path.split("*", 1)
                matches = {p: t for p, t in api_schema.items()
                           if p.startswith(prefix) and p.endswith(suffix)}
                used_api_paths.update(matches)
                api_type = " | ".join(sorted(set(matches.values()))) or None
            else:
                api_type = api_schema.get(api_path)
                used_api_paths.add(api_path)
        mapped.append({
            "xerp_path": xerp_path,
            "xerp_type": xerp_schema.get(xerp_path),
            "api_path": api_path,
            "api_type": api_type,
            "note": note,
        })

    mapped_xerp_paths = {m["xerp_path"] for m in mapped}
    xerp_only = [{"path": p, "type": t} for p, t in sorted(xerp_schema.items())
                 if p not in mapped_xerp_paths]
    api_only = [{"path": p, "type": t} for p, t in sorted(api_schema.items())
                if p not in used_api_paths]

    return {"mapped": mapped, "xerp_only": xerp_only, "api_only": api_only}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", orders=list_orders())


@app.route("/api/compare/<order_id>")
def api_compare(order_id):
    return _compare_impl(order_id, force=False)


@app.route("/api/compare/<order_id>/refresh", methods=["POST"])
def api_compare_refresh(order_id):
    return _compare_impl(order_id, force=True)


def _compare_impl(order_id, force):
    order = get_order(order_id)
    if not order:
        return jsonify({"error": f"Order '{order_id}' not found"}), 404
    try:
        with open(order["xerp"], encoding="utf-8-sig") as fh:
            xerp_raw = json.load(fh)
    except Exception as exc:
        return jsonify({"error": f"Failed to read xerp file: {exc}"}), 500
    try:
        api_raw, from_cache = extract_document(order["pdf"], order_id, force=force)
    except Exception as exc:
        return jsonify({"error": f"Extraction API call failed: {exc}"}), 502

    comparison = compare(normalize_xerp(xerp_raw), normalize_extraction(api_raw))
    return jsonify({
        "order_id": order_id,
        "pdf": os.path.basename(order["pdf"]) if order["pdf"] else None,
        "xerp_file": os.path.basename(order["xerp"]),
        "from_cache": from_cache,
        "comparison": comparison,
        "schema": compare_schemas(xerp_raw, api_raw),
        "raw": {"xerp": xerp_raw, "extraction": api_raw},
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
