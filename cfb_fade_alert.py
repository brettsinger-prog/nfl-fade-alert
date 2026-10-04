#!/usr/bin/env python3
# College football "fade the public" kickoff alert.
#
# Same screen as nfl_fade_alert.py, limited to games where at least one team
# is in the current AP Top 25 (smaller games draw too little betting volume
# for the splits to mean much). For each such game that kicks off within
# ALERT_WINDOW_MIN minutes, checks both teams and emails any that qualify:
#
#   1. Splits: spread bets% + money% < SPLIT_SUM_MAX  (SportsBettingDime)
#   2. Line:   over the last LOOKBACK_HOURS the spread either did not move,
#              or moved TOWARD the faded team (reverse line movement)
#              (EV Analytics line history)
#
# Either team can qualify, ranked or not, as long as the game has a ranked
# team in it. Each game is evaluated once (state in cfb_fade_alert_state.json).
# Games that kick off together go in one email.
#
# Env:
#   SMTP_USER, SMTP_PASS  Gmail sender + app password
#   RECIPIENT             defaults to SMTP_USER
#
# Usage:
#   python3 cfb_fade_alert.py                 one check, then exit
#   python3 cfb_fade_alert.py --loop          stay up, checking ~30 min before each
#                                             kickoff (GitHub Actions mode)
#   python3 cfb_fade_alert.py --dry-run       print instead of email, no state writes
#   python3 cfb_fade_alert.py --dry-run --now 2026-10-10T15:00:00-04:00
#                                             simulate a different current time

import os, re, sys, json, html, time, smtplib, argparse, datetime, traceback, subprocess
import urllib.request
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

SPLIT_SUM_MAX    = 50
LOOKBACK_HOURS   = 48
ALERT_WINDOW_MIN = 35
TOP_N            = 25

# --loop mode. GitHub Actions jobs die at 6h, so a run works for at most
# RUN_BUDGET_MIN, then starts a fresh run of itself if a check is due within
# CHAIN_HOURS. Further out than that, it exits and waits for the next scheduled
# trigger (every 4h).
CHECK_LEAD_MIN = 32
RUN_BUDGET_MIN = 330
CHAIN_HOURS    = 12
MAX_SLEEP_MIN  = 15
WORKFLOW       = "cfb_alert.yml"

SBD_URL = ("https://www.sportsbettingdime.com/wp-json/adpt/v1/ncaafb-odds"
           "?books=sr%3Abook%3A17324%2Csr%3Abook%3A18149%2Csr%3Abook%3A18186&format=us")
EV_PAGE  = "https://evanalytics.com/ncaaf/odds"
EV_CHART = "https://evanalytics.com/modules/odds/data/chart.php?sport=ncaaf&gid={gid}&tid={tid}&cid=6&parent_cid=6"
EV_CID   = "6"   # full-game line for college
RANKINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"

UA  = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/129 Safari/537.36")
ET  = ZoneInfo("America/New_York")
LOCAL_TZ = ZoneInfo("America/Chicago")

DIR        = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DIR, "cfb_fade_alert_state.json")

SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
RECIPIENT = os.environ.get("RECIPIENT", SMTP_USER)


# ── fetch ───────────────────────────────────────────────────────────────────
def http(url, method="GET"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def norm(s):
    """'Miami (FL)' -> 'miami', 'Texas A&M' -> 'texas a&m'."""
    return re.sub(r"\s*\(.*?\)", "", s or "").strip().lower()


def sbd_games():
    """All college games SBD lists, with kickoff (UTC) and spread splits."""
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
            "home_name": home.get("market") or home["name"],
            "away_name": away.get("market") or away["name"],
            "home_key": (norm(home.get("market")), home["name"].lower()),
            "away_key": (norm(away.get("market")), away["name"].lower()),
            "splits":  splits,
            "spread_books": spread_books,
        })
    return games


def ap_top25():
    """{(school, mascot): (rank, espn_abbr)} from the current AP poll."""
    polls = json.loads(http(RANKINGS_URL)).get("rankings") or []
    ap = next((p for p in polls if p.get("type") == "ap"), None)
    if not ap or not ap.get("ranks"):
        raise RuntimeError("AP Top 25 not found in the ESPN rankings feed")
    out = {}
    for r in ap["ranks"]:
        if r.get("current") and r["current"] <= TOP_N:
            t = r["team"]
            out[(norm(t.get("location")), (t.get("name") or "").lower())] = (r["current"], t.get("abbreviation"))
    return out


def tag_ranks(games, ranks):
    """Attach home_rank / away_rank and ESPN abbreviations (EV uses ESPN's)."""
    for g in games:
        for side in ("home", "away"):
            rank, espn = ranks.get(g[f"{side}_key"], (None, None))
            g[f"{side}_rank"] = rank
            g[f"{side}_alias"] = {g[side]} | ({espn} if espn else set())
    return games


def ev_game_index():
    """List of (gid, tid, home_abbr, away_abbr) from the EV odds page.

    Each game row carries data-chart="0|ncaaf|gid|cid|parent_cid|tid|HOME|AWAY|...".
    tid is the home team, and chart.php returns the spread from its side."""
    rows = []
    for m in re.finditer(r'data-chart="([^"]+)"', http(EV_PAGE)):
        p = html.unescape(m.group(1)).split("|")
        if len(p) < 8 or p[1] != "ncaaf" or p[3] != EV_CID:
            continue
        rows.append((p[2], p[5], p[6], p[7]))
    return rows


def ev_find(game, ev_rows):
    """EV row for this game, plus whether EV's home matches SBD's home.

    Abbreviations differ between sites for some schools (IND vs IU), so
    match on any known alias of either team. The ranked team always has its
    ESPN abbreviation, which is what EV uses."""
    home, away = game["home_alias"], game["away_alias"]
    for gid, tid, eh, ea in ev_rows:
        if eh in home or ea in away:
            return gid, tid, True
        if eh in away or ea in home:
            return gid, tid, False
    return None


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


def team_line(game, side):
    """Current spread for one side from SBD (first book quoting it)."""
    for b in game["spread_books"]:
        s = (b.get(side) or {}).get("spread")
        if s not in (None, ""):
            try:
                return f"{float(s):+g}"
            except ValueError:
                return str(s)
    return "?"


def label(game, side):
    rank = game[f"{side}_rank"]
    return f"#{rank} {game[side + '_name']}" if rank else game[f"{side}_name"]


def matchup(game):
    return f'{label(game, "away")} at {label(game, "home")}'


def has_splits(game):
    """SBD sends blank ('') percentages for games it has no splits for yet."""
    return any(str((game["splits"].get(side) or {}).get("betsPercentage") or "").strip()
               for side in ("home", "away"))


def split_candidates(game):
    splits = game["splits"]
    if not has_splits(game):
        return []
    out = []
    for side in ("home", "away"):
        s = splits.get(side) or {}
        if not s:
            continue
        bets, money = float(s.get("betsPercentage") or 0), float(s.get("stakePercentage") or 0)
        if bets + money < SPLIT_SUM_MAX:
            out.append((side, bets, money))
    return out


def evaluate(game, ev_rows, now_et):
    """Return (qualifiers, notes). qualifiers = list of dicts for the email."""
    if not has_splits(game):
        return [], [f"{matchup(game)}: no spread splits published"]

    candidates = split_candidates(game)
    if not candidates:
        return [], []

    found = ev_find(game, ev_rows)
    if not found:
        return [], [f"{matchup(game)}: passed splits but not found on EV Analytics"]
    gid, tid, same_home = found
    hist = ev_spread_history(gid, tid, now_et)
    if not hist:
        return [], [f"{matchup(game)}: no EV line history"]

    # EV's tid is its home team; flip if it disagrees with SBD (neutral sites).
    sign = 1 if same_home else -1
    cutoff  = now_et - datetime.timedelta(hours=LOOKBACK_HOURS)
    start_h = spread_at(hist, cutoff) * sign
    now_h   = hist[-1][1] * sign
    open_h  = hist[0][1] * sign

    out = []
    for side, bets, money in candidates:
        opp = "away" if side == "home" else "home"
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
            "team_name": label(game, side), "opp": label(game, opp),
            "venue": "vs" if side == "home" else "at",
            "line": team_line(game, side),
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
    subject = f"CFB fade alert: {names} ({fmt_kick(kickoff)})"
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
        f"Screen: games with an AP Top {TOP_N} team only. Spread bets% + money% < {SPLIT_SUM_MAX}, "
        f"and the line either held or moved toward the faded team over {LOOKBACK_HOURS}h.",
        "Sources: sportsbettingdime.com (splits), evanalytics.com (line history), ESPN (AP poll).",
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
def is_pending(g, state, now_utc):
    return g["id"] not in state and g["status"] == "not_started" and g["kickoff"] > now_utc


def check_once(now_utc, state, dry_run, games=None):
    """Evaluate every game kicking off within ALERT_WINDOW_MIN. Mutates state.
    Returns True if any game was evaluated."""
    now_et = now_utc.astimezone(ET)
    games = games if games is not None else sbd_games()
    due = [g for g in games
           if is_pending(g, state, now_utc)
           and g["kickoff"] - now_utc <= datetime.timedelta(minutes=ALERT_WINDOW_MIN)]
    if not due:
        print(f"{now_et:%Y-%m-%d %H:%M} ET: nothing kicking off within {ALERT_WINDOW_MIN} min", flush=True)
        return False

    # One email per kickoff slot.
    slots = {}
    for g in due:
        slots.setdefault(g["kickoff"], []).append(g)

    ranks = ev_rows = None
    for kickoff, slot_games in sorted(slots.items()):
        quals, notes = [], []
        ranked = []
        try:
            if ranks is None:
                ranks = ap_top25()
            ranked = [g for g in tag_ranks(slot_games, ranks) if g["home_rank"] or g["away_rank"]]
            for g in ranked:
                if ev_rows is None and split_candidates(g):
                    ev_rows = ev_game_index()
                q, n = evaluate(g, ev_rows or [], now_et)
                quals += q
                notes += n
        except Exception:
            # A failed check should not look like "no matches". Say so once per slot.
            err = traceback.format_exc()
            print(err, file=sys.stderr, flush=True)
            send(f"CFB fade alert could not run ({fmt_kick(kickoff)})",
                 f"The check for games kicking off {fmt_kick(kickoff)} failed, so matches may have been missed.\n\n{err}",
                 dry_run)
        else:
            print(f"{now_et:%Y-%m-%d %H:%M} ET: slot {fmt_kick(kickoff)}, {len(slot_games)} games, "
                  f"{len(ranked)} with a ranked team, {len(quals)} qualifiers", flush=True)
            for g in ranked:
                print(f"  {matchup(g)}", flush=True)
            if quals:
                send(*build_email(kickoff, quals, notes), dry_run)
        for g in slot_games:
            state[g["id"]] = now_utc.isoformat()
    return True


def next_check_time(games, state, now_utc):
    """When the next unevaluated game enters the alert window.

    Uses every game, ranked or not, so the AP poll is only fetched when a
    slot is actually checked. Unranked-only slots are skipped quickly."""
    times = [g["kickoff"] - datetime.timedelta(minutes=CHECK_LEAD_MIN)
             for g in games if is_pending(g, state, now_utc)]
    return min(times) if times else None


def commit_state():
    """Persist state to the repo so later runs skip evaluated games."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    if not subprocess.run(["git", "status", "--porcelain", STATE_FILE],
                          capture_output=True, text=True).stdout.strip():
        return
    for cmd in (["git", "add", STATE_FILE],
                ["git", "commit", "-q", "-m", "Update evaluated CFB games"],
                ["git", "pull", "-q", "--rebase"],
                ["git", "push", "-q"]):
        subprocess.run(cmd, check=True)


def start_next_run():
    """Kick off a fresh workflow run (it queues behind this one)."""
    subprocess.run(["gh", "workflow", "run", WORKFLOW, "--ref", "main"], check=True)
    print("Started the next run to keep watching.", flush=True)


def loop(dry_run):
    started = datetime.datetime.now(datetime.timezone.utc)
    deadline = started + datetime.timedelta(minutes=RUN_BUDGET_MIN)
    state = load_state()
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            games = sbd_games()
            if check_once(now, state, dry_run, games):
                save_state(state, now)
                commit_state()
            nxt = next_check_time(games, state, now)
        except Exception:
            # Network blip etc. Retry shortly rather than end the watch.
            traceback.print_exc()
            nxt = now + datetime.timedelta(minutes=5)

        if nxt is None or nxt - now > datetime.timedelta(hours=CHAIN_HOURS):
            print(f"No check due in the next {CHAIN_HOURS}h. Exiting until the next scheduled start.", flush=True)
            return
        if nxt > deadline:
            if not dry_run:
                start_next_run()
            return
        wake = min(max(nxt, now + datetime.timedelta(seconds=30)),
                   now + datetime.timedelta(minutes=MAX_SLEEP_MIN))
        print(f"Next check {nxt.astimezone(ET):%a %H:%M} ET. Sleeping until {wake.astimezone(ET):%H:%M} ET.", flush=True)
        time.sleep((wake - now).total_seconds())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--now", help="ISO datetime with offset, to simulate (single check only)")
    args = ap.parse_args()

    if args.loop:
        loop(args.dry_run)
        return

    now_utc = (datetime.datetime.fromisoformat(args.now).astimezone(datetime.timezone.utc)
               if args.now else datetime.datetime.now(datetime.timezone.utc))
    state = {} if args.dry_run else load_state()
    if check_once(now_utc, state, args.dry_run) and not args.dry_run:
        save_state(state, now_utc)


if __name__ == "__main__":
    main()
