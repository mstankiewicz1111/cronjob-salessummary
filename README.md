# Raport zamówień (IdoSell) → e-mail (SendGrid) — Render Cron Job

Aplikacja uruchamiana raz dziennie (Cron Job na Render.com), pobiera zamówienia z IdoSell Admin API
dla zakresu "wczoraj 00:00–23:59" (Europe/Warsaw), liczy podsumowanie i wysyła maila przez SendGrid.

## Co jest w mailu
- liczba zamówień: Sklep
- liczba zamówień: Allegro
- łączna liczba zamówień
- Top 10 produktów osobno dla Sklepu i Allegro (ilość sztuk)

## Wymagane zmienne środowiskowe (Render)
- IDOSELL_API_KEY
- SENDGRID_API_KEY
- MAIL_FROM (np. raport@twojadomena.pl)
- MAIL_TO (np. ty@firma.pl,ksiegowosc@firma.pl)

Opcjonalne:
- IDOSELL_ENDPOINT (domyślnie ustawiony w kodzie)
- TZ (domyślnie Europe/Warsaw)
- RESULTS_LIMIT (domyślnie 100)
- TOP_N (domyślnie 10)

## Render.com — konfiguracja
1. Wrzuć pliki do repo na GitHub.
2. Render → New → Cron Job → wybierz repo.
3. Command: `python main.py`
4. Ustaw harmonogram (np. codziennie 07:05).
5. Dodaj Environment Variables (jak wyżej).
6. Deploy.

## Lokalnie (opcjonalnie)
1. `python -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. Ustaw zmienne środowiskowe i uruchom: `python main.py`
