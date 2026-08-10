import os
import sys
import time
import random
import requests
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from collections import defaultdict

import psycopg2
from psycopg2.extras import execute_values


# =========================
# Konfiguracja (ENV)
# =========================
IDOSELL_API_KEY = os.environ.get("IDOSELL_API_KEY", "").strip()
IDOSELL_ENDPOINT = os.environ.get(
    "IDOSELL_ENDPOINT",
    "https://client5056.idosell.com/api/admin/v3/orders/orders/get"
).strip()

IDOSELL_PRODUCTS_ENDPOINT = os.environ.get(
    "IDOSELL_PRODUCTS_ENDPOINT",
    "https://client5056.idosell.com/api/admin/v8/products/products"
).strip()

BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
MAIL_FROM = os.environ.get("MAIL_FROM", "").strip()
MAIL_TO = os.environ.get("MAIL_TO", "").strip() 
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

TZ_NAME = os.environ.get("TZ", "Europe/Warsaw").strip()

# Wartość priorytetu w IdoSell oznaczająca ręczną blokadę/przypięcie towaru
LOCKED_PRIORITY = 999

ORDER_STATUSES = [
    "new", "finished", "on_order", "packed", "ready",
    "payment_waiting", "delivery_waiting", "wait_for_dispatch"
]

RESULTS_LIMIT = int(os.environ.get("RESULTS_LIMIT", "100"))
TOP_N = int(os.environ.get("TOP_N", "10"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "2000"))
HTTP_TIMEOUT = (10, 60)


def require_env(name: str, value: str) -> None:
    if not value:
        print(f"Brak zmiennej środowiskowej: {name}", file=sys.stderr)
        sys.exit(2)


def fmt_qty(x: float):
    return int(x) if x == int(x) else x


def fmt_money_pln(x: float) -> str:
    return f"{x:.2f} zł"


def get_report_range(days_back: int = 1):
    tz = ZoneInfo(TZ_NAME)
    now = datetime.now(tz)
    report_date = now.date() - timedelta(days=days_back)

    start_dt = datetime.combine(report_date, dtime(0, 0, 0), tzinfo=tz)
    end_dt = datetime.combine(report_date, dtime(23, 59, 59), tzinfo=tz)

    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    label = report_date.strftime("%Y-%m-%d")
    return label, start_str, end_str


def _request_with_retry(method: str, url: str, payload: dict, headers: dict, *, max_attempts: int = 5) -> requests.Response:
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.request(method, url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            if attempt == max_attempts:
                raise RuntimeError(f"Błąd sieci po {attempt} próbach: {e}") from e
            sleep_s = (1.6 ** attempt) + random.random()
            print(f"[HTTP] Błąd sieci: {e} | retry za {sleep_s:.1f}s (próba {attempt}/{max_attempts})")
            time.sleep(sleep_s)
            continue

        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt == max_attempts:
                return resp
            sleep_s = (1.6 ** attempt) + random.random()
            print(f"[HTTP] Status {resp.status_code} | retry za {sleep_s:.1f}s (próba {attempt}/{max_attempts})")
            time.sleep(sleep_s)
            continue

        return resp

    raise RuntimeError("Nieoczekiwany błąd w _request_with_retry")


def fetch_orders_for_range(start_str: str, end_str: str) -> list[dict]:
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-KEY": IDOSELL_API_KEY,
    }

    payload = {
        "params": {
            "ordersStatuses": ORDER_STATUSES,
            "ordersRange": {
                "ordersDateRange": {
                    "ordersDateType": "add",
                    "ordersDateBegin": start_str,
                    "ordersDateEnd": end_str,
                }
            },
            "resultsLimit": RESULTS_LIMIT,
            "resultsPage": 0,
        }
    }

    all_orders: list[dict] = []

    while True:
        page = payload["params"]["resultsPage"]
        if page >= MAX_PAGES:
            raise RuntimeError(f"Osiągnięto MAX_PAGES={MAX_PAGES}.")

        print(f"[IDOSELL] Pobieranie strony zamówień: {page}")

        resp = _request_with_retry("POST", IDOSELL_ENDPOINT, payload, headers)

        if resp.status_code == 207:
            print(f"[IDOSELL] Koniec wyników (HTTP 207): {resp.text}")
            break

        if resp.status_code != 200:
            raise RuntimeError(f"Błąd API: {resp.status_code} – {resp.text}")

        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(f"HTTP 200, ale odpowiedź nie jest JSON.") from e

        orders = data.get("Results")
        if orders is None:
            orders = data.get("results", [])

        if not orders:
            print(f"[IDOSELL] Koniec wyników na stronie {page}.")
            break

        print(f"[IDOSELL] Zamówień na stronie {page}: {len(orders)}")
        all_orders.extend(orders)

        payload["params"]["resultsPage"] += 1

    return all_orders


def detect_order_source(order: dict) -> str:
    auctions_service_name = (
        order.get("orderDetails", {})
             .get("orderSourceResults", {})
             .get("auctionsServiceName")
    )
    if auctions_service_name and str(auctions_service_name).strip().lower() == "allegro":
        return "allegro"
    return "sklep"


def top_n_products(d: dict[tuple[str, str], float], n: int) -> list[tuple[tuple[str, str], int | float]]:
    items = sorted(d.items(), key=lambda x: x[1], reverse=True)[:n]
    return [((name, pid), fmt_qty(qty)) for (name, pid), qty in items]


def _safe_float(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def extract_order_gross_value(order: dict) -> tuple[float, str]:
    payments = order.get("orderDetails", {}).get("payments", {}) or {}
    oc = payments.get("orderCurrency", {}) or {}

    currency = str(oc.get("currencyId") or "").strip() or "PLN"

    products = _safe_float(oc.get("orderProductsCost"))
    delivery = _safe_float(oc.get("orderDeliveryCost"))
    payform = _safe_float(oc.get("orderPayformCost"))
    insurance = _safe_float(oc.get("orderInsuranceCost"))

    total = products + delivery + payform + insurance
    return total, currency


def aggregate_report(orders: list[dict]) -> dict:
    orders_sklep_ids = set()
    orders_allegro_ids = set()
    daily_order_ids = set()

    product_qty_sklep = defaultdict(float)
    product_qty_allegro = defaultdict(float)

    total_revenue = 0.0
    currencies_seen = set()
    revenue_counted_for = set()

    for order in orders:
        order_id = order.get("orderId")
        if order_id:
            daily_order_ids.add(order_id)

        if order_id and order_id not in revenue_counted_for:
            order_value, currency = extract_order_gross_value(order)
            total_revenue += order_value
            currencies_seen.add(currency)
            revenue_counted_for.add(order_id)

        source = detect_order_source(order)
        if order_id:
            if source == "allegro":
                orders_allegro_ids.add(order_id)
            else:
                orders_sklep_ids.add(order_id)

        for product in order.get("orderDetails", {}).get("productsResults", []):
            product_name = str(product.get("productName") or "Nieznany Produkt").strip()
            product_id = str(product.get("productId") or "0").strip()
            
            qv = product.get("productQuantity")
            qty = _safe_float(qv)

            if source == "allegro":
                product_qty_allegro[(product_name, product_id)] += qty
            else:
                product_qty_sklep[(product_name, product_id)] += qty

    total_orders = len(daily_order_ids)
    total_items_sold = sum(product_qty_sklep.values()) + sum(product_qty_allegro.values())

    avg_order_value = (total_revenue / total_orders) if total_orders > 0 else 0.0
    avg_items_per_order = (total_items_sold / total_orders) if total_orders > 0 else 0.0

    currency_note = ""
    if len(currencies_seen) > 1:
        currency_note = f" (uwaga: wiele walut: {', '.join(sorted(currencies_seen))})"
    elif len(currencies_seen) == 1 and "PLN" not in currencies_seen:
        currency_note = f" (waluta: {next(iter(currencies_seen))})"

    return {
        "total_revenue": round(total_revenue, 2),
        "currency_note": currency_note,
        "orders_sklep_count": len(orders_sklep_ids),
        "orders_allegro_count": len(orders_allegro_ids),
        "orders_total_count": total_orders,
        "total_items_sold": fmt_qty(round(total_items_sold, 2)),
        "avg_order_value": round(avg_order_value, 2),
        "avg_items_per_order": round(avg_items_per_order, 2),
        "top_sklep": top_n_products(product_qty_sklep, TOP_N),
        "top_allegro": top_n_products(product_qty_allegro, TOP_N),
        "raw_sklep": product_qty_sklep,
        "raw_allegro": product_qty_allegro
    }


def save_sales_to_postgres(report_label: str, agg: dict) -> None:
    if not DATABASE_URL:
        print("[POSTGRES] Brak DATABASE_URL. Pomijam zapis do bazy.", file=sys.stderr)
        return

    create_table_sql = """
    CREATE TABLE IF NOT EXISTS daily_sales_by_product (
        id SERIAL PRIMARY KEY,
        sale_date DATE NOT NULL,
        product_id VARCHAR(50) NOT NULL,
        product_name VARCHAR(255) NOT NULL,
        source VARCHAR(50) NOT NULL,
        quantity NUMERIC(10, 2) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT unique_date_pid_source UNIQUE (sale_date, product_id, source)
    );
    CREATE INDEX IF NOT EXISTS idx_sales_by_prod_date ON daily_sales_by_product(sale_date, product_id);
    """

    records = []
    for (name, pid), qty in agg["raw_sklep"].items():
        records.append((report_label, pid, name, "sklep", qty))
        
    for (name, pid), qty in agg["raw_allegro"].items():
        records.append((report_label, pid, name, "allegro", qty))

    if not records:
        print("[POSTGRES] Brak danych do zapisania za ten dzień.")
        return

    insert_sql = """
        INSERT INTO daily_sales_by_product (sale_date, product_id, product_name, source, quantity)
        VALUES %s
        ON CONFLICT (sale_date, product_id, source)
        DO UPDATE SET quantity = EXCLUDED.quantity, product_name = EXCLUDED.product_name;
    """

    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn:
            with conn.cursor() as cur:
                cur.execute(create_table_sql)
                execute_values(cur, insert_sql, records)
        conn.close()
        print("[POSTGRES] Sukces! Dane produktowe zostały zapisane.")
    except Exception as e:
        print(f"[POSTGRES] BŁĄD ZAPISU DO BAZY: {e}", file=sys.stderr)


def get_sales_trends_from_db(end_date_str: str, days_back: int, limit: int = 5) -> list:
    if not DATABASE_URL:
        return []

    end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    start_date = end_date - timedelta(days=days_back - 1)
    start_date_str = start_date.strftime("%Y-%m-%d")

    query = """
        SELECT product_id, MAX(product_name) as p_name, SUM(quantity) as total_qty
        FROM daily_sales_by_product
        WHERE sale_date BETWEEN %s AND %s
        GROUP BY product_id
        ORDER BY total_qty DESC
        LIMIT %s;
    """

    trends = []
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn:
            with conn.cursor() as cur:
                cur.execute(query, (start_date_str, end_date_str, limit))
                rows = cur.fetchall()
                trends = [((row[1], row[0]), fmt_qty(row[2])) for row in rows]
        conn.close()
    except Exception as e:
        print(f"[POSTGRES] Błąd pobierania trendów ({days_back} dni): {e}", file=sys.stderr)
    
    return trends


def get_current_product_priorities(product_ids: list[int]) -> dict[int, int]:
    """
    Pobiera z API IdoSell aktualne priorytety dla podanej listy ID produktów.
    Zwraca słownik: {product_id: current_priority}
    """
    if not product_ids:
        return {}

    get_endpoint = IDOSELL_ENDPOINT.replace("/orders/orders/get", "/products/products/get")
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-KEY": IDOSELL_API_KEY,
    }

    payload = {
        "params": {
            "productsIds": product_ids
        }
    }

    priorities_map = {}
    try:
        resp = _request_with_retry("POST", get_endpoint, payload, headers)
        if resp.status_code == 200:
            data = resp.json()
            products = data.get("Results") or data.get("results") or []
            for p in products:
                pid = p.get("productId")
                p_priority = p.get("productPriority", 1)
                if pid:
                    priorities_map[int(pid)] = int(p_priority)
        else:
            print(f"[IDOSELL-PRIORITY] Błąd pobierania obecnych priorytetów: HTTP {resp.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"[IDOSELL-PRIORITY] Błąd połączenia podczas odczytu priorytetów: {e}", file=sys.stderr)

    return priorities_map


def sync_top100_priorities_to_idosell(report_label: str) -> None:
    """
    Pobiera Top 100 sprzedanych sztuk z ostatnich 7 dni, obniża priorytet do 1
    dla produktów wypadających z zestawienia oraz ustawia priorytet równy wolumenowi
    sprzedaży (min. 2) dla aktualnego Top 100 w IdoSell.
    Pomija produkty posiadające zarezerwowany priorytet (LOCKED_PRIORITY = 999).
    """
    if not DATABASE_URL or not IDOSELL_API_KEY:
        print("[IDOSELL-PRIORITY] Brak DATABASE_URL lub IDOSELL_API_KEY. Pomijam synchronizację.")
        return

    # 1. Odczytujemy poprzedni stan z PostgreSQL
    init_tracker_sql = """
    CREATE TABLE IF NOT EXISTS top100_priority_tracker (
        product_id VARCHAR(50) PRIMARY KEY,
        last_priority INT NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    );
    SELECT product_id FROM top100_priority_tracker;
    """

    previous_pids = set()
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn:
            with conn.cursor() as cur:
                cur.execute(init_tracker_sql)
                rows = cur.fetchall()
                previous_pids = {str(r[0]) for r in rows}
        conn.close()
    except Exception as e:
        print(f"[IDOSELL-PRIORITY] Błąd odczytu stanu z bazy: {e}", file=sys.stderr)
        return

    # 2. Pobieramy aktualne Top 100 z ostatnich 7 dni
    top_100_current = get_sales_trends_from_db(report_label, days_back=7, limit=100)
    current_map = {
        str(pid): max(2, int(round(float(qty)))) 
        for (name, pid), qty in top_100_current 
        if pid and pid != "0"
    }
    current_pids = set(current_map.keys())

    # Towary wypadające z Top 100
    pids_to_reset = previous_pids - current_pids

    all_target_pids = [int(p) for p in (current_pids | pids_to_reset) if p.isdigit()]

    # 3. Pobieramy aktualne priorytety z IdoSell, żeby sprawdzić ewentualne blokady 999
    print(f"[IDOSELL-PRIORITY] Sprawdzanie obecnych priorytetów w panelu dla {len(all_target_pids)} produktów...")
    existing_priorities = get_current_product_priorities(all_target_pids)

    products_payload = []

    # A. Produkty wypadające z Top 100 -> reset priorytetu do 1 (o ile nie mają 999)
    for pid in pids_to_reset:
        pid_int = int(pid) if pid.isdigit() else pid
        if existing_priorities.get(pid_int) == LOCKED_PRIORITY:
            print(f"[IDOSELL-PRIORITY] Produkt {pid} ma priorytet {LOCKED_PRIORITY} (Ręczna blokada) – pomijam reset.")
            continue
            
        products_payload.append({
            "productId": pid_int,
            "productPriority": 1
        })

    # B. Produkty z aktualnego Top 100 -> priorytet = wolumen (min. 2, o ile nie mają 999)
    for pid, priority_val in current_map.items():
        pid_int = int(pid) if pid.isdigit() else pid
        if existing_priorities.get(pid_int) == LOCKED_PRIORITY:
            print(f"[IDOSELL-PRIORITY] Produkt {pid} ma priorytet {LOCKED_PRIORITY} (Ręczna blokada) – pomijam automatyczną zmianę.")
            continue

        products_payload.append({
            "productId": pid_int,
            "productPriority": priority_val
        })

    if not products_payload:
        print("[IDOSELL-PRIORITY] Brak zmian priorytetów do wysłania do IdoSell.")
        return

    def chunk_list(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-KEY": IDOSELL_API_KEY,
    }

    for batch in chunk_list(products_payload, 50):
        payload = {"params": {"products": batch}}
        try:
            resp = _request_with_retry("PUT", IDOSELL_PRODUCTS_ENDPOINT, payload, headers)
            if resp.status_code in (200, 207):
                print(f"[IDOSELL-PRIORITY] Zaktualizowano priorytety dla paczki {len(batch)} produktów w IdoSell.")
            else:
                print(f"[IDOSELL-PRIORITY] Błąd edycji w IdoSell: HTTP {resp.status_code} - {resp.text}", file=sys.stderr)
        except Exception as e:
            print(f"[IDOSELL-PRIORITY] Błąd połączenia przy aktualizacji priorytetów: {e}", file=sys.stderr)

    # 4. Zapisujemy nowy stan w PostgreSQL
    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE top100_priority_tracker;")
                if current_map:
                    insert_tracker = "INSERT INTO top100_priority_tracker (product_id, last_priority) VALUES %s;"
                    records = [(pid, val) for pid, val in current_map.items()]
                    execute_values(cur, insert_tracker, records)
        conn.close()
        print("[IDOSELL-PRIORITY] Stan w PostgreSQL został zaktualizowany.")
    except Exception as e:
        print(f"[IDOSELL-PRIORITY] Błąd zapisu stanu w bazie: {e}", file=sys.stderr)


def render_table(rows: list) -> str:
    if not rows:
        return '<p style="margin:6px 0; color:#666; font-size:13px; font-style:italic;">Brak sprzedaży w tym kanale w danym dniu.</p>'

    body = ""
    for i, ((name, pid), qty) in enumerate(rows, start=1):
        if pid and pid != "0":
            product_url = f"https://wassyl.pl/product-pol-{pid}"
            display_name = f"<a href='{product_url}' style='color:#0288d1; text-decoration:underline; font-weight:500;'>{name}</a> <span style='color:#888; font-size:12px; white-space:nowrap;'>| ID {pid}</span>"
        else:
            display_name = name

        bg_color = "#ffffff" if i % 2 != 0 else "#fafafa"
        body += f"""
          <tr style="background-color: {bg_color};">
            <td style="padding:8px 10px; border-bottom:1px solid #edeef0; font-size:13px; color:#666; width:25px; vertical-align:middle;">{i}.</td>
            <td style="padding:8px 10px; border-bottom:1px solid #edeef0; font-size:13px; color:#222; line-height:1.4;">{display_name}</td>
            <td style="padding:8px 10px; border-bottom:1px solid #edeef0; font-size:13px; color:#111; text-align:right; font-weight:bold; width:45px; vertical-align:middle;">{qty}</td>
          </tr>
        """

    return f"""
    <div style="border: 1px solid #edeef0; border-radius: 6px; overflow: hidden; margin-top: 8px; margin-bottom: 20px;">
      <table style="border-collapse:collapse; width:100%; background-color: #ffffff;">
        <thead>
          <tr style="background-color: #f8f9fa;">
            <th style="text-align:left; padding:10px; border-bottom:2px solid #edeef0; font-size:11px; color:#666; text-transform: uppercase; font-weight:600; width:25px;">#</th>
            <th style="text-align:left; padding:10px; border-bottom:2px solid #edeef0; font-size:11px; color:#666; text-transform: uppercase; font-weight:600;">Produkt</th>
            <th style="text-align:right; padding:10px; border-bottom:2px solid #edeef0; font-size:11px; color:#666; text-transform: uppercase; font-weight:600; width:45px;">Sztuk</th>
          </tr>
        </thead>
        <tbody>
          {body}
        </tbody>
      </table>
    </div>
    """


def build_email_html(report_label: str, agg: dict, trends_3d: list, trends_7d: list) -> str:
    total_value_str = fmt_money_pln(agg["total_revenue"]) + agg.get("currency_note", "")

    def render_trend_list(trends):
        if not trends:
            return '<p style="color:#666; margin:4px 0; font-size:13px; font-style:italic;">Zbieranie danych historycznych w toku...</p>'
        
        table_rows = ""
        for i, ((name, pid), qty) in enumerate(trends, start=1):
            if pid and pid != "0":
                product_url = f"https://wassyl.pl/product-pol-{pid}"
                display_name = f"<a href='{product_url}' style='color:#026aa7; text-decoration:underline; font-weight:500;'>{name}</a> <span style='color:#888; font-size:11px; white-space:nowrap;'>| ID {pid}</span>"
            else:
                display_name = name

            table_rows += f"""
              <tr style="border-bottom: 1px solid #eef2f5;">
                <td style="padding: 6px 0; font-size: 13px; color: #666; width: 20px; vertical-align: top;">{i}.</td>
                <td style="padding: 6px 8px; font-size: 13px; color: #222; line-height: 1.4;">{display_name}</td>
                <td style="padding: 6px 0; font-size: 13px; color: #0288d1; text-align: right; font-weight: bold; width: 60px; white-space: nowrap; vertical-align: top;">{qty} szt.</td>
              </tr>
            """
        return f'<table style="width:100%; border-collapse:collapse;">{table_rows}</table>'

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif; font-size:14px; line-height:1.5; color:#333333; max-width:600px; margin:0 auto; padding:10px;">
      
      <div style="background-color: #f8f9fa; padding: 16px 20px; border-left: 4px solid #0288d1; margin-bottom: 20px; border-radius: 4px;">
        <h2 style="margin: 0; font-size: 18px; color: #111111; font-weight: 700;">Raport zamówień — {report_label}</h2>
      </div>

      <div style="background-color: #ffffff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px 20px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.02);">
        <h3 style="margin-top: 0; margin-bottom: 14px; font-size: 15px; color: #111111; font-weight: 600; border-bottom: 1px solid #f0f0f0; padding-bottom: 6px;">📋 Podsumowanie dnia</h3>
        <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
          <tr>
            <td style="padding: 4px 0; color: #555;">Zamówienia (Sklep):</td>
            <td style="padding: 4px 0; text-align: right; font-weight: bold; color: #111;">{agg['orders_sklep_count']}</td>
          </tr>
          <tr>
            <td style="padding: 4px 0; color: #555;">Zamówienia (Allegro):</td>
            <td style="padding: 4px 0; text-align: right; font-weight: bold; color: #111;">{agg['orders_allegro_count']}</td>
          </tr>
          <tr>
            <td style="padding: 4px 0; color: #555;">Łączna liczba zamówień:</td>
            <td style="padding: 4px 0; text-align: right; font-weight: bold; color: #111;">{agg['orders_total_count']}</td>
          </tr>
          <tr style="border-bottom: 1px solid #f0f0f0;">
            <td style="padding: 4px 0 10px 0; color: #555;">Sprzedane towary (łącznie):</td>
            <td style="padding: 4px 0 10px 0; text-align: right; font-weight: bold; color: #111;">{agg['total_items_sold']} szt.</td>
          </tr>
          <tr>
            <td style="padding: 10px 0 4px 0; color: #555;">Średnia wartość koszyka:</td>
            <td style="padding: 10px 0 4px 0; text-align: right; font-weight: bold; color: #111;">{fmt_money_pln(agg['avg_order_value'])}</td>
          </tr>
          <tr>
            <td style="padding: 4px 0 10px 0; color: #555;">Średnio produktów w koszyku:</td>
            <td style="padding: 4px 0 10px 0; text-align: right; font-weight: bold; color: #111;">{agg['avg_items_per_order']:.2f} szt.</td>
          </tr>
          <tr style="border-top: 1px solid #f0f0f0;">
            <td style="padding: 10px 0 4px 0; color: #111; font-weight: 600;">Łączna wartość (brutto):</td>
            <td style="padding: 10px 0 4px 0; text-align: right; font-weight: bold; color: #d32f2f; font-size: 16px;">{total_value_str}</td>
          </tr>
        </table>
      </div>

      <h3 style="margin-top: 0; margin-bottom: 12px; font-size: 15px; color: #0288d1; font-weight: 600;">📈 Trendy sprzedażowe (Sklep + Allegro)</h3>
      
      <div style="background-color: #f4f9fc; border: 1px solid #d0e3f0; border-radius: 8px; padding: 16px 20px; margin-bottom: 14px;">
        <h4 style="margin-top: 0; margin-bottom: 10px; font-size: 13px; color: #026aa7; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">🔥 Top 5 produktów (Ostatnie 3 dni)</h4>
        {render_trend_list(trends_3d)}
      </div>

      <div style="background-color: #f4f9fc; border: 1px solid #d0e3f0; border-radius: 8px; padding: 16px 20px; margin-bottom: 28px;">
        <h4 style="margin-top: 0; margin-bottom: 10px; font-size: 13px; color: #026aa7; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">⭐️ Top 5 produktów (Ostatnie 7 dni)</h4>
        {render_trend_list(trends_7d)}
      </div>

      <h3 style="margin-top: 0; margin-bottom: 4px; font-size: 15px; color: #111111; font-weight: 600;">🛒 Top {TOP_N} dnia — Sklep</h3>
      {render_table(agg['top_sklep'])}

      <h3 style="margin-top: 10px; margin-bottom: 4px; font-size: 15px; color: #111111; font-weight: 600;">🦅 Top {TOP_N} dnia — Allegro</h3>
      {render_table(agg['top_allegro'])}

      <p style="margin-top: 30px; font-size: 11px; color: #999999; text-align: center; border-top: 1px solid #edeef0; padding-top: 12px;">
        Raport wygenerowany automatycznie przez system analityczny.<br>
        Strefa czasowa: {TZ_NAME} | Dane historyczne przechowywane w PostgreSQL.
      </p>
    </div>
    """


def send_email(subject: str, html: str, max_attempts: int = 3) -> None:
    recipients = [x.strip() for x in MAIL_TO.split(",") if x.strip()]
    if not recipients:
        raise RuntimeError("MAIL_TO jest puste albo w złym formacie.")

    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json",
    }

    sender_name = os.environ.get("MAIL_FROM_NAME", "WASSYL | raport sprzedaży").strip()

    payload = {
        "sender": {
            "name": sender_name,
            "email": MAIL_FROM
        },
        "to": [{"email": r} for r in recipients],
        "subject": subject,
        "htmlContent": html,
    }

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"[BREVO] Próba wysyłki maila ({attempt}/{max_attempts})...")
            resp = requests.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
            
            if resp.status_code in (200, 201, 202):
                print(f"[BREVO] Sukces! Status: {resp.status_code}")
                return
            
            print(f"[BREVO] HTTP {resp.status_code} – {resp.text} | próba {attempt}/{max_attempts}")
            if resp.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"[BREVO] Błąd krytyczny API: HTTP {resp.status_code} – {resp.text}")
                
        except requests.RequestException as e:
            print(f"[BREVO] Błąd sieci/połączenia: {e} | próba {attempt}/{max_attempts}")
            if attempt == max_attempts:
                raise RuntimeError(f"[BREVO] Nie udało się wysłać raportu po {max_attempts} próbach.") from e
        
        sleep_s = (2 ** attempt) + random.random()
        print(f"[BREVO] Ponowienie za {sleep_s:.1f}s...")
        time.sleep(sleep_s)


def main():
    require_env("IDOSELL_API_KEY", IDOSELL_API_KEY)
    require_env("BREVO_API_KEY", BREVO_API_KEY)
    require_env("MAIL_FROM", MAIL_FROM)
    require_env("MAIL_TO", MAIL_TO)

    report_label, start_str, end_str = get_report_range(days_back=1)
    print(f"[RANGE] {start_str} -> {end_str}")

    orders = fetch_orders_for_range(start_str, end_str)
    agg = aggregate_report(orders)

    # 1. Zapis bieżącej sprzedaży do bazy PostgreSQL
    save_sales_to_postgres(report_label, agg)

    # 2. Synchronizacja priorytetów towarów w IdoSell na podstawie Top 100 z 7 dni
    print("[IDOSELL-PRIORITY] Rozpoczynam synchronizację priorytetów do IdoSell...")
    sync_top100_priorities_to_idosell(report_label)

    # 3. Pobranie trendów i wysyłka maila
    print("[POSTGRES] Pobieranie trendów produktowych do maila...")
    trends_3d = get_sales_trends_from_db(report_label, days_back=3, limit=5)
    trends_7d = get_sales_trends_from_db(report_label, days_back=7, limit=5)

    subject = f"Raport zamówień — {report_label}"
    html = build_email_html(report_label, agg, trends_3d, trends_7d)

    send_email(subject, html)


if __name__ == "__main__":
    main()
