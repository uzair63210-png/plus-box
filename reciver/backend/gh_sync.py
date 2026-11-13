"""Snapshot the local SQLite database to a PRIVATE GitHub repo (CSV files + git commit/push).

  1. git clone https://github.com/<you>/<private-repo>.git  C:\\Users\\you\\pulse-data
     (git must already be able to push: logged in via Git Credential Manager / gh auth login)
  2. python gh_sync.py --db sqlite:///pulse_rx.db --repo C:\\Users\\you\\pulse-data            (once)
     python gh_sync.py --db sqlite:///pulse_rx.db --repo C:\\Users\\you\\pulse-data --every 600 (every 10 min)

Why a snapshot and not a live database: GitHub is not a database (no transactions, slow, rate-limited,
concurrent pushes conflict). Keep SQLite as the live store; use the repo as an off-site, versioned backup /
audit trail (each commit = a timestamped copy of the alerts and temperature evidence).
Never put tokens in code; use your normal git login. --restore loads the CSVs back into an empty DB.
"""
import argparse, csv, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pulse_rx.db import Store

TABLES = ["tags", "packets", "alerts", "predictions"]


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def export(store, repo):
    for t in TABLES:
        rows = store.rows(f"SELECT * FROM {t} ORDER BY 1")
        with open(os.path.join(repo, f"{t}.csv"), "w", newline="") as f:
            if rows:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader(); w.writerows(rows)


def push(repo):
    git(repo, "add", "-A")
    if not git(repo, "status", "--porcelain").stdout.strip():
        return "no changes"
    r = git(repo, "commit", "-m", "pulse-box snapshot " + time.strftime("%Y-%m-%d %H:%M:%S"))
    if r.returncode:
        return "commit failed: " + r.stderr.strip()
    git(repo, "pull", "--rebase", "--autostash")
    r = git(repo, "push")
    return "pushed" if r.returncode == 0 else "push failed: " + r.stderr.strip()


def restore(store, repo):
    for t in TABLES:
        path = os.path.join(repo, f"{t}.csv")
        if not os.path.exists(path):
            continue
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                cols = list(row.keys())
                vals = [None if row[c] == "" else row[c] for c in cols]
                store.execute(f"INSERT OR REPLACE INTO {t}({','.join(cols)}) VALUES({','.join('?'*len(cols))})", vals)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sqlite:///pulse_rx.db"); ap.add_argument("--repo", required=True)
    ap.add_argument("--every", type=int, default=0, help="seconds between snapshots (0 = once)")
    ap.add_argument("--restore", action="store_true")
    a = ap.parse_args()
    st = Store(a.db)
    if a.restore:
        restore(st, a.repo); print("restored"); sys.exit()
    while True:
        export(st, a.repo); print(time.strftime("%H:%M:%S"), push(a.repo))
        if not a.every:
            break
        time.sleep(a.every)
