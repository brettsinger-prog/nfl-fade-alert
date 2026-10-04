# NFL fade alert

Emails a heads-up ~30 minutes before kickoff for any NFL team that passes the fade screen:

1. Spread bets% + money% < 50 (SportsBettingDime splits)
2. Over the last 48h the spread either held or moved toward that team (EV Analytics line history)

Runs on GitHub Actions. A schedule starts it every 4h, and on game days the run stays up, checks ~30 min before each kickoff, and starts a fresh run of itself before the 6h job limit.
Secrets: `SMTP_USER`, `SMTP_PASS` (Gmail app password), `RECIPIENT`.

Local test: `python3 nfl_fade_alert.py --dry-run --now 2026-10-04T12:28:00-04:00`

## College football

`cfb_fade_alert.py` runs the same screen for college games, but only games where at least one team is in the current AP Top 25 (ESPN rankings feed). Either team in the game can qualify. Workflow `cfb_alert.yml`, state in `cfb_fade_alert_state.json`, same secrets.

Local test: `python3 cfb_fade_alert.py --dry-run --now 2026-10-10T15:00:00-04:00`
