import os
import sys
import requests
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from collections import defaultdict

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail


# =========================
# Konfiguracja (ENV)
# =========================
IDOSELL_API_KEY = os.environ.get("IDOSELL_API_KEY", "").strip()
IDOSELL_ENDPOINT = os.environ.get(
    "IDOSELL_ENDPOINT",
    "https://client5056.idosell.com/api/admin/v3/orders/orders/get"
).strip()

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "").strip()
MAIL_FROM = os.environ.get("MAIL_FROM", "").strip()
MAIL_TO = os.environ.get("MAIL_TO", "").strip()  # lista po przecinku
TZ_NAME = os.environ.get("TZ", "Europe/Warsaw").strip()

# Statusy zamówień — jak w Twoim kodzie
ORDER_STATUSES = [
    "new", "finished", "on_order", "packed", "ready",
    "payment_waiting", "delivery_waiting", "wait_for_dispatch"
]

RESULTS_LIMIT = int(os.environ.get("RESULTS_LIMIT", "100"))
TOP_N = int(os.environ.get("TOP_N", "10"))


def require_env(name: str, value: str) -> None:
    if not value:
        print(f"Brak zmiennej środowiskowej: {name}", file=sys.stderr)
        sys.exit(2)


def fmt_qty(x: float):
    return int(x) if x == int(x) else x


def get_report_range(days_back: int = 1):
    """
    Raport za 'wczoraj' w strefie TZ_NAME.
    Zwraca: (label_YYYY_MM_DD, start_str, end_str)
    """
    tz = ZoneInfo(TZ_NAME)
    now = datetime.now(tz)
    report_date = now.date() - timedelta(days=days_back)

    start_dt = datetime.combine(report_date, time(0, 0, 0), tzinfo=tz)
    end_dt = datetime.combine(report_date, time(23, 59, 59), tzinfo=tz)

    # Format jak w Twoim kodzie
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    label = report_date.strftime("%Y-%m-%d")
    return label, start_str, end_str


def fetch_orders_for_range(start_str: str, end_str: str) -> list[dict]:
    """
    Pobiera wszystkie zamówienia z IdoSell w zadanym zakresie dat, z paginacją.
    Uwaga: IdoSell może zwrócić HTTP 207 dla pustej strony ("zwrócono pusty wynik") — traktujemy to jako koniec.
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
        print(f"[IDOSELL] Pobieranie strony: {page}")

        resp = requests.post(
            IDOSELL_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=60
        )

        # IdoSell: 207 bywa używane jako sygnał "pusto" dla kolejnej strony
        if resp.status_code == 207:
            print(f"[IDOSELL] Koniec wyników (HTTP 207): {resp.text}")
            break

        if resp.status_code != 200:
            raise RuntimeError(f"Błąd API: {resp.status_code} – {resp.text}")

        data = resp.json()
        orders = data.get("Results", [])
        if not orders:
            print(f"[IDOSELL] Koniec wyników na stronie {page}.")
            break

        print(f"[IDOSELL] Zamówień na stronie {page}: {len(orders)}")
        all_orders.extend(orders)
        payload["params"]["resultsPage"] += 1

    return all_orders


def detect_order_source(order: dict) -> str:
    """
    'allegro' lub 'sklep' — jak w Twoim kodzie (auctionsServiceName).
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


def aggregate_report(orders: list[dict]) -> dict:
    """
    Zwraca metryki + top N osobno dla sklepu i Allegro.
    """
    orders_sklep_ids = set()
    orders_allegro_ids = set()
    daily_order_ids = set()

    product_qty_sklep = defaultdict(float)
    product_qty_allegro = defaultdict(float)

    for order in orders:
        order_id = order.get("orderId")
        if order_id:
            daily_order_ids.add(order_id)

        source = detect_order_source(order)
        if order_id:
            if source == "allegro":
                orders_allegro_ids.add(order_id)
            else:
                orders_sklep_ids.add(order_id)

        for product in order.get("orderDetails", {}).get("productsResults", []):
            product_name = str(product.get("productName") or "Nieznany Produkt").strip()
            qv = product.get("productQuantity")

            try:
                qty = float(qv) if qv is not None else 0.0
            except (ValueError, TypeError):
                qty = 0.0

            if source == "allegro":
                product_qty_allegro[product_name] += qty
            else:
                product_qty_sklep[product_name] += qty

    return {
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
    return f"""
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.4;">
      <h2 style="margin:0 0 10px;">Raport zamówień — {report_label}</h2>

      <h3 style="margin:16px 0 6px;">Podsumowanie</h3>
      <ul style="margin:6px 0 0 18px;">
        <li>Liczba zamówień (Sklep): <b>{agg['orders_sklep_count']}</b></li>
        <li>Liczba zamówień (Allegro): <b>{agg['orders_allegro_count']}</b></li>
        <li>Łączna liczba zamówień: <b>{agg['orders_total_count']}</b></li>
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

    message = Mail(
        from_email=MAIL_FROM,
        to_emails=recipients,
        subject=subject,
        html_content=html,
    )

    sg = SendGridAPIClient(SENDGRID_API_KEY)
    resp = sg.send(message)
    print(f"[SENDGRID] Status: {resp.status_code}")


def main():
    require_env("IDOSELL_API_KEY", IDOSELL_API_KEY)
    require_env("SENDGRID_API_KEY", SENDGRID_API_KEY)
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
