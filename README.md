# NFL fade alert

Emails a heads-up ~30 minutes before kickoff for any NFL team that passes the fade screen:

1. Spread bets% + money% < 50 (SportsBettingDime splits)
2. Over the last 48h the spread either held or moved toward that team (EV Analytics line history)

Runs on GitHub Actions via `workflow_dispatch`, triggered every 5 minutes during game windows by cron-job.org.
Secrets: `SMTP_USER`, `SMTP_PASS` (Gmail app password), `RECIPIENT`.

Local test: `python3 nfl_fade_alert.py --dry-run --now 2026-10-04T12:28:00-04:00`
