#!/usr/bin/env python3
# NFL "fade the public" kickoff alert.
#
# Runs every 5 minutes via launchd (com.brett.nfl.fade.alert.plist). For each
# NFL game that kicks off within ALERT_WINDOW_MIN minutes, checks both teams
# against the screen and emails any that qualify:
#
#   1. Splits: spread bets% + money% < SPLIT_SUM_MAX  (SportsBettingDime)
#   2. Line:   over the last LOOKBACK_HOURS the spread either did not move,
#              or moved TOWARD the faded team (reverse line movement)
#              (EV Analytics line history)
#
# Each game is evaluated once (state in nfl_fade_alert_state.json). Games that
# kick off together go in one email.
#
# Env (from .env via run_nfl_fade_alert.sh):
#   SMTP_USER, SMTP_PASS  Gmail sender + app password
#   RECIPIENT             defaults to SMTP_USER
#
# Usage:
#   python3 nfl_fade_alert.py                 normal run
#   python3 nfl_fade_alert.py --dry-run       print instead of email, no state writes
#   python3 nfl_fade_alert.py --dry-run --now 2026-10-04T12:30:00-04:00
#                                             simulate a different current time

import os, re, sys, json, html, smtplib, argparse, datetime, traceback
import urllib.request
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

SPLIT_SUM_MAX    = 50
LOOKBACK_HOURS   = 48
ALERT_WINDOW_MIN = 35   # 5-min cadence -> first check lands ~30-35 min out

SBD_URL = ("https://www.sportsbettingdime.com/wp-json/adpt/v1/nfl-odds"
           "?books=sr%3Abook%3A17324%2Csr%3Abook%3A18149%2Csr%3Abook%3A18186&format=us")
EV_PAGE  = "https://evanalytics.com/nfl/odds"
EV_CHART = "https://evanalytics.com/modules/odds/data/chart.php?sport=nfl&gid={gid}&tid={tid}&cid=9&parent_cid=9"

UA  = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/129 Safari/537.36")
ET  = ZoneInfo("America/New_York")
LOCAL_TZ = ZoneInfo("America/Chicago")

DIR        = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR, "nfl_fade_alert_state.json")

SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
RECIPIENT = os.environ.get("RECIPIENT", SMTP_USER)


# ── fetch ───────────────────────────────────────────────────────────────────
def http(url, method="GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def sbd_games():
    """All NFL games SBD lists, with kickoff (UTC) and spread splits."""
    games = []
    for g in json.loads(http(SBD_URL))["data"]:
        home, away = g["competitors"]["home"], g["competitors"]["away"]
        splits = (g.get("bettingSplits") or {}).get("spread") or {}
        spread_books = ((g.get("markets") or {}).get("spread") or {}).get("books") or []
        games.append({
            "id":      g["id"],
            "status":  g.get("status"),
            "kickoff": datetime.datetime.fromisoformat(g["scheduled"]),
            "home":    home["abbreviation"],
            "away":    away["abbreviation"],
            "home_name": f'{home.get("market", "")} {home["name"]}'.strip(),
            "away_name": f'{away.get("market", "")} {away["name"]}'.strip(),
            "splits":  splits,
            "spread_books": spread_books,
        })
    return games


def ev_game_index():
    """Map frozenset({home, away}) -> (gid, tid, home_abbr) from the EV odds page.

    Each game row carries data-chart="0|nfl|gid|cid|parent_cid|tid|HOME|AWAY|...".
    tid is the home team, and chart.php returns the spread from its side."""
    idx = {}
    for m in re.finditer(r'data-chart="([^"]+)"', http(EV_PAGE)):
        p = html.unescape(m.group(1)).split("|")
        if len(p) < 8 or p[1] != "nfl" or p[3] != "9":   # cid 9 = full-game line
            continue
        gid, tid, hteam, ateam = p[2], p[5], p[6], p[7]
        idx[frozenset((hteam, ateam))] = (gid, tid, hteam)
    return idx


def ev_spread_history(gid, tid, now_et):
    """[(datetime ET, home spread float)] oldest first. EV omits the year."""
    data = json.loads(http(EV_CHART.format(gid=gid, tid=tid), method="POST"))
    out = []
    for row in data.get("spread") or []:
        dt = datetime.datetime.strptime(row["update_date"], "%m/%d %I:%M %p")
        dt = dt.replace(year=now_et.year, tzinfo=ET)
        if dt > now_et + datetime.timedelta(days=1):   # Dec -> Jan rollover
            dt = dt.replace(year=now_et.year - 1)
        out.append((dt, float(row["spread"])))
    out.sort(key=lambda x: x[0])
    return out


# ── screen ──────────────────────────────────────────────────────────────────
def spread_at(history, when):
    """Home spread in effect at `when` (last change at or before it)."""
    val = None
    for dt, s in history:
        if dt <= when:
            val = s
        else:
            break
    return val if val is not None else (history[0][1] if history else None)


def team_line(game, team):
    """Consensus-ish current spread for `team` from SBD (first book quoting it)."""
    side = "home" if team == game["home"] else "away"
    for b in game["spread_books"]:
        s = (b.get(side) or {}).get("spread")
        if s not in (None, ""):
            try:
                v = float(s)
                return f"{v:+g}"
            except ValueError:
                return str(s)
    return "?"


def evaluate(game, ev_idx, now_et):
    """Return (qualifiers, notes). qualifiers = list of dicts for the email."""
    splits = game["splits"]
    if not splits.get("home") or not splits.get("away"):
        return [], [f'{game["away"]} at {game["home"]}: no spread splits published']

    candidates = []
    for side in ("home", "away"):
        s = splits[side]
        bets, money = float(s.get("betsPercentage") or 0), float(s.get("stakePercentage") or 0)
        if bets + money < SPLIT_SUM_MAX:
            candidates.append((side, bets, money))
    if not candidates:
        return [], []

    key = frozenset((game["home"], game["away"]))
    if key not in ev_idx:
        return [], [f'{game["away"]} at {game["home"]}: passed splits but not found on EV Analytics']
    gid, tid, ev_home = ev_idx[key]
    hist = ev_spread_history(gid, tid, now_et)
    if not hist:
        return [], [f'{game["away"]} at {game["home"]}: no EV line history']

    # EV's tid is its home team; flip if it disagrees with SBD (neutral sites).
    sign = 1 if ev_home == game["home"] else -1
    cutoff  = now_et - datetime.timedelta(hours=LOOKBACK_HOURS)
    start_h = spread_at(hist, cutoff) * sign
    now_h   = hist[-1][1] * sign
    open_h  = hist[0][1] * sign

    out = []
    for side, bets, money in candidates:
        team  = game[side]
        opp   = game["away"] if side == "home" else game["home"]
        # Spread from the faded team's side. Falling = more favored = moved toward them.
        to_team = (lambda h: h) if side == "home" else (lambda h: -h)
        start, cur, opened = to_team(start_h), to_team(now_h), to_team(open_h)
        delta = cur - start
        if delta == 0:
            move = "No move"
        elif delta < 0:
            move = f"Reverse line movement ({start:+g} to {cur:+g})"
        else:
            continue   # moved with the public, fails the screen
        out.append({
            "team": team, "team_name": game[f"{side}_name"], "opp": opp,
            "venue": "vs" if side == "home" else "at",
            "line": team_line(game, team),
            "bets": bets, "money": money,
            "move": move,
            "opened": opened, "current": cur,
            "kickoff": game["kickoff"],
        })
    return out, []


# ── email ───────────────────────────────────────────────────────────────────
def fmt_kick(dt):
    loc = dt.astimezone(LOCAL_TZ)
    return loc.strftime("%a %b %-d, %-I:%M %p CT").replace(":00 ", " ")


def build_email(kickoff, quals, notes):
    names = ", ".join(q["team_name"] for q in quals)
    subject = f"NFL fade alert: {names} ({fmt_kick(kickoff)})"
    lines = [f"Kickoff {fmt_kick(kickoff)}. These teams pass your screen.", ""]
    for q in quals:
        lines += [
            f'{q["team_name"]} {q["line"]} {q["venue"]} {q["opp"]}',
            f'  Splits   {q["bets"]:.0f}% bets + {q["money"]:.0f}% money = {q["bets"] + q["money"]:.0f}',
            f'  Line     {q["move"]} in the last {LOOKBACK_HOURS}h',
            f'  History  opened {q["opened"]:+g}, now {q["current"]:+g}'
            + ("  (moved toward them since open)" if q["current"] < q["opened"] else ""),
            "",
        ]
    if notes:
        lines += ["Other notes"] + [f"  {n}" for n in notes] + [""]
    lines += [
        f"Screen: spread bets% + money% < {SPLIT_SUM_MAX}, and the line either held or moved toward the faded team over {LOOKBACK_HOURS}h.",
        "Sources: sportsbettingdime.com (splits), evanalytics.com (line history).",
    ]
    return subject, "\n".join(lines)


def send(subject, body, dry_run):
    if dry_run or not (SMTP_USER and SMTP_PASS):
        print(f"--- {'DRY RUN' if dry_run else 'NO SMTP CREDS'} ---\nSubject: {subject}\n\n{body}\n")
        return
    msg = MIMEText(body, "plain")
    msg["Subject"], msg["From"], msg["To"] = subject, SMTP_USER, RECIPIENT
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
    print(f"Sent: {subject}")


# ── state ───────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state, now_utc):
    # Keep two weeks of evaluated game ids.
    cutoff = (now_utc - datetime.timedelta(days=14)).isoformat()
    state = {k: v for k, v in state.items() if v >= cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


# ── main ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="ISO datetime with offset, to simulate")
    args = ap.parse_args()

    now_utc = (datetime.datetime.fromisoformat(args.now).astimezone(datetime.timezone.utc)
               if args.now else datetime.datetime.now(datetime.timezone.utc))
    now_et = now_utc.astimezone(ET)
    state = {} if args.dry_run else load_state()

    games = sbd_games()
    due = [g for g in games
           if g["id"] not in state
           and g["status"] == "not_started"
           and datetime.timedelta(0) < g["kickoff"] - now_utc <= datetime.timedelta(minutes=ALERT_WINDOW_MIN)]
    if not due:
        print(f"{now_et:%Y-%m-%d %H:%M} ET: nothing kicking off within {ALERT_WINDOW_MIN} min")
        return

    # One email per kickoff slot.
    slots = {}
    for g in due:
        slots.setdefault(g["kickoff"], []).append(g)

    ev_idx = None
    for kickoff, slot_games in sorted(slots.items()):
        quals, notes = [], []
        try:
            for g in slot_games:
                if ev_idx is None and any(
                        float((g["splits"].get(s) or {}).get("betsPercentage") or 0)
                        + float((g["splits"].get(s) or {}).get("stakePercentage") or 0) < SPLIT_SUM_MAX
                        for s in ("home", "away")):
                    ev_idx = ev_game_index()
                q, n = evaluate(g, ev_idx or {}, now_et)
                quals += q
                notes += n
        except Exception:
            # A failed check should not look like "no matches". Say so once per slot.
            err = traceback.format_exc()
            print(err, file=sys.stderr)
            send(f"NFL fade alert could not run ({fmt_kick(kickoff)})",
                 f"The check for games kicking off {fmt_kick(kickoff)} failed, so matches may have been missed.\n\n{err}",
                 args.dry_run)
        else:
            print(f"{now_et:%Y-%m-%d %H:%M} ET: slot {fmt_kick(kickoff)}, "
                  f"{len(slot_games)} games, {len(quals)} qualifiers")
            if quals:
                send(*build_email(kickoff, quals, notes), args.dry_run)
        for g in slot_games:
            state[g["id"]] = now_utc.isoformat()

    if not args.dry_run:
        save_state(state, now_utc)


if __name__ == "__main__":
    main()
