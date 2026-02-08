import os
import sys
import time
import random
import requests
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from collections import defaultdict


# =========================
# Konfiguracja (ENV)
# =========================
IDOSELL_API_KEY = os.environ.get("IDOSELL_API_KEY", "").strip()
IDOSELL_ENDPOINT = os.environ.get(
    "IDOSELL_ENDPOINT",
    "https://client5056.idosell.com/api/admin/v3/orders/orders/get"
).strip()

BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
MAIL_FROM = os.environ.get("MAIL_FROM", "").strip()
MAIL_TO = os.environ.get("MAIL_TO", "").strip()  # lista po przecinku
TZ_NAME = os.environ.get("TZ", "Europe/Warsaw").strip()

# Statusy zamówień
ORDER_STATUSES = [
    "new", "finished", "on_order", "packed", "ready",
    "payment_waiting", "delivery_waiting", "wait_for_dispatch"
]

RESULTS_LIMIT = int(os.environ.get("RESULTS_LIMIT", "100"))
TOP_N = int(os.environ.get("TOP_N", "10"))

# Bezpiecznik na wypadek zapętlenia/paginacji “nigdy nie kończy”
MAX_PAGES = int(os.environ.get("MAX_PAGES", "2000"))

# Timeouty: (connect, read)
HTTP_TIMEOUT = (10, 60)


def require_env(name: str, value: str) -> None:
    if not value:
        print(f"Brak zmiennej środowiskowej: {name}", file=sys.stderr)
        sys.exit(2)


def fmt_qty(x: float):
    return int(x) if x == int(x) else x


def fmt_money_pln(x: float) -> str:
    # proste formatowanie 2 miejsca po przecinku
    return f"{x:.2f} zł"


def get_report_range(days_back: int = 1):
    """
    Raport za 'wczoraj' w strefie TZ_NAME.
    Zwraca: (label_YYYY_MM_DD, start_str, end_str)
    """
    tz = ZoneInfo(TZ_NAME)
    now = datetime.now(tz)
    report_date = now.date() - timedelta(days=days_back)

    start_dt = datetime.combine(report_date, dtime(0, 0, 0), tzinfo=tz)
    end_dt = datetime.combine(report_date, dtime(23, 59, 59), tzinfo=tz)

    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    label = report_date.strftime("%Y-%m-%d")
    return label, start_str, end_str


def _post_with_retry(url: str, payload: dict, headers: dict, *, max_attempts: int = 5) -> requests.Response:
    """
    POST z retry na problemy sieciowe + 429 + 5xx.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            if attempt == max_attempts:
                raise RuntimeError(f"Błąd sieci po {attempt} próbach: {e}") from e
            sleep_s = (1.6 ** attempt) + random.random()
            print(f"[IDOSELL] Błąd sieci: {e} | retry za {sleep_s:.1f}s (próba {attempt}/{max_attempts})")
            time.sleep(sleep_s)
            continue

        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt == max_attempts:
                return resp
            sleep_s = (1.6 ** attempt) + random.random()
            print(f"[IDOSELL] HTTP {resp.status_code} | retry za {sleep_s:.1f}s (próba {attempt}/{max_attempts})")
            time.sleep(sleep_s)
            continue

        return resp

    raise RuntimeError("Nieoczekiwany błąd w _post_with_retry")


def fetch_orders_for_range(start_str: str, end_str: str) -> list[dict]:
    """
    Pobiera wszystkie zamówienia z IdoSell w zadanym zakresie dat, z paginacją.
    IdoSell może zwrócić HTTP 207 dla pustej strony ("zwrócono pusty wynik") — traktujemy to jako koniec.
    """
    headers = {
        "Content-Type": "application/json",
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
            raise RuntimeError(f"Osiągnięto MAX_PAGES={MAX_PAGES}. Coś nie tak z paginacją / filtrem.")

        print(f"[IDOSELL] Pobieranie strony: {page}")

        resp = _post_with_retry(IDOSELL_ENDPOINT, payload, headers)

        # IdoSell: 207 = “pusto / koniec”
        if resp.status_code == 207:
            print(f"[IDOSELL] Koniec wyników (HTTP 207): {resp.text}")
            break

        if resp.status_code != 200:
            raise RuntimeError(f"Błąd API: {resp.status_code} – {resp.text}")

        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(f"HTTP 200, ale odpowiedź nie jest JSON. Body (pierwsze 500): {resp.text[:500]}") from e

        orders = data.get("Results")
        if orders is None:
            orders = data.get("results", [])

        if not orders:
            print(f"[IDOSELL] Koniec wyników na stronie {page} (pusta lista przy HTTP 200).")
            break

        print(f"[IDOSELL] Zamówień na stronie {page}: {len(orders)}")
        all_orders.extend(orders)

        payload["params"]["resultsPage"] += 1

    return all_orders


def detect_order_source(order: dict) -> str:
    """
    'allegro' lub 'sklep' — na podstawie auctionsServiceName.
    """
    auctions_service_name = (
        order.get("orderDetails", {})
             .get("orderSourceResults", {})
             .get("auctionsServiceName")
    )
    if auctions_service_name and str(auctions_service_name).strip().lower() == "allegro":
        return "allegro"
    return "sklep"


def top_n_products(d: dict[str, float], n: int) -> list[tuple[str, int | float]]:
    items = sorted(d.items(), key=lambda x: x[1], reverse=True)[:n]
    return [(name, fmt_qty(qty)) for name, qty in items]


def _safe_float(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def extract_order_gross_value(order: dict) -> tuple[float, str]:
    """
    Liczy wartość zamówienia na podstawie:
      orderDetails.payments.orderCurrency:
        - orderProductsCost
        - orderDeliveryCost
        - orderPayformCost
        - orderInsuranceCost

    Zwraca: (wartość, waluta)
    """
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
    """
    Zwraca metryki + top N osobno dla sklepu i Allegro
    oraz łączną wartość zamówień (brutto) wg orderCurrency.
    """
    orders_sklep_ids = set()
    orders_allegro_ids = set()
    daily_order_ids = set()

    product_qty_sklep = defaultdict(float)
    product_qty_allegro = defaultdict(float)

    total_revenue = 0.0
    currencies_seen = set()

    # Na wszelki wypadek: nie licz dwa razy tego samego orderId
    revenue_counted_for = set()

    for order in orders:
        order_id = order.get("orderId")
        if order_id:
            daily_order_ids.add(order_id)

        # SUMA WARTOŚCI ZAMÓWIEŃ (BRUTTO)
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
            qv = product.get("productQuantity")

            qty = _safe_float(qv)

            if source == "allegro":
                product_qty_allegro[product_name] += qty
            else:
                product_qty_sklep[product_name] += qty

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
        "orders_total_count": len(daily_order_ids),
        "top_sklep": top_n_products(product_qty_sklep, TOP_N),
        "top_allegro": top_n_products(product_qty_allegro, TOP_N),
    }


def render_table(rows: list[tuple[str, int | float]]) -> str:
    if not rows:
        return """
        <p style="margin:6px 0;color:#666;">Brak sprzedaży w tym kanale w danym dniu.</p>
        """

    body = ""
    for i, (name, qty) in enumerate(rows, start=1):
        body += f"""
          <tr>
            <td style="padding:6px 8px;border-bottom:1px solid #eee;">{i}.</td>
            <td style="padding:6px 8px;border-bottom:1px solid #eee;">{name}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:right;">{qty}</td>
          </tr>
        """

    return f"""
    <table style="border-collapse:collapse;width:100%;max-width:900px;">
      <thead>
        <tr>
          <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #ddd;">#</th>
          <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #ddd;">Produkt</th>
          <th style="text-align:right;padding:6px 8px;border-bottom:2px solid #ddd;">Sztuk</th>
        </tr>
      </thead>
      <tbody>
        {body}
      </tbody>
    </table>
    """


def build_email_html(report_label: str, agg: dict) -> str:
    total_value_str = fmt_money_pln(agg["total_revenue"]) + agg.get("currency_note", "")

    return f"""
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.4;">
      <h2 style="margin:0 0 10px;">Raport zamówień — {report_label}</h2>

      <h3 style="margin:16px 0 6px;">Podsumowanie</h3>
      <ul style="margin:6px 0 0 18px;">
        <li>Liczba zamówień (Sklep): <b>{agg['orders_sklep_count']}</b></li>
        <li>Liczba zamówień (Allegro): <b>{agg['orders_allegro_count']}</b></li>
        <li>Łączna liczba zamówień: <b>{agg['orders_total_count']}</b></li>
        <li><b>Łączna wartość zamówień:</b> <b>{total_value_str}</b></li>
      </ul>

      <h3 style="margin:16px 0 6px;">Top {TOP_N} sprzedanych towarów — Sklep</h3>
      {render_table(agg['top_sklep'])}

      <h3 style="margin:16px 0 6px;">Top {TOP_N} sprzedanych towarów — Allegro</h3>
      {render_table(agg['top_allegro'])}

      <p style="margin-top:16px;color:#666;">
        Wygenerowano automatycznie (strefa czasowa: {TZ_NAME}).
      </p>
    </div>
    """


def send_email(subject: str, html: str) -> None:
    recipients = [x.strip() for x in MAIL_TO.split(",") if x.strip()]
    if not recipients:
        raise RuntimeError("MAIL_TO jest puste albo w złym formacie (użyj przecinków).")

    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json",
    }

    payload = {
        "sender": {"email": MAIL_FROM},
        "to": [{"email": r} for r in recipients],
        "subject": subject,
        "htmlContent": html,
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)

    # Brevo zwykle zwraca 201 Created dla poprawnej wysyłki
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"[BREVO] Błąd wysyłki: HTTP {resp.status_code} – {resp.text}")

    print(f"[BREVO] Status: {resp.status_code}")


def main():
    require_env("IDOSELL_API_KEY", IDOSELL_API_KEY)
    require_env("BREVO_API_KEY", BREVO_API_KEY)
    require_env("MAIL_FROM", MAIL_FROM)
    require_env("MAIL_TO", MAIL_TO)

    report_label, start_str, end_str = get_report_range(days_back=1)
    print(f"[RANGE] {start_str} -> {end_str}")

    orders = fetch_orders_for_range(start_str, end_str)
    agg = aggregate_report(orders)

    subject = f"Raport zamówień — {report_label}"
    html = build_email_html(report_label, agg)

    send_email(subject, html)


if __name__ == "__main__":
    main()
