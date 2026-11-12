#!/usr/bin/env python3
"""Static checks on rendered dashboards and alert files (see render_templates.yml).

    python3 tests/check_dashboards.py <render_dir>

Checks: valid JSON; unique, positive panel ids; no overlapping panels; every
query target has a refId and an expression; every dashboard is tagged
"observability"; dashboard uids are unique; alert rule uids are unique and
every rule has a summary. Exits non-zero on the first file with problems.
"""
import glob
import json
import os
import sys


def panels(dashboard):
    for panel in dashboard.get("panels", []):
        yield panel
        for inner in panel.get("panels", []):
            yield inner


def check_dashboard(path, uids):
    problems = []
    with open(path) as handle:
        d = json.load(handle)
    if d["uid"] in uids:
        problems.append("duplicate uid " + d["uid"])
    uids.add(d["uid"])
    if "observability" not in d.get("tags", []):
        problems.append("not tagged observability")
    ids, cells = set(), {}
    for p in panels(d):
        pid = p.get("id")
        if not isinstance(pid, int) or pid <= 0 or pid in ids:
            problems.append("bad or duplicate panel id %r (%s)" % (pid, p.get("title")))
        ids.add(pid)
        g = p.get("gridPos", {})
        for x in range(g.get("x", 0), g.get("x", 0) + g.get("w", 0)):
            for y in range(g.get("y", 0), g.get("y", 0) + g.get("h", 0)):
                if (x, y) in cells:
                    problems.append("panel %s overlaps panel %s" % (pid, cells[(x, y)]))
                    break
                cells[(x, y)] = pid
            else:
                continue
            break
        if g.get("x", 0) + g.get("w", 0) > 24:
            problems.append("panel %s wider than the grid" % pid)
        for t in p.get("targets", []):
            if not t.get("refId") or not t.get("expr"):
                problems.append("panel %s has a target without refId or expr" % pid)
    return d, problems


def check_alerts(path, uids):
    problems = []
    with open(path) as handle:
        d = json.load(handle)
    for group in d.get("groups", []):
        for rule in group["rules"]:
            if rule["uid"] in uids:
                problems.append("duplicate alert uid " + rule["uid"])
            uids.add(rule["uid"])
            if not rule["annotations"].get("summary"):
                problems.append("rule %s has no summary" % rule["uid"])
    return d, problems


def main(directory):
    dash_uids, alert_uids, failed, total_panels = set(), set(), False, 0
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        d, problems = check_dashboard(path, dash_uids)
        total_panels += sum(1 for _ in panels(d))
        print("%-32s %-40s %3d panels %s" % (os.path.basename(path), d["title"], sum(1 for _ in panels(d)), "OK" if not problems else ""))
        for problem in problems:
            print("    " + problem)
        failed |= bool(problems)
    for path in sorted(glob.glob(os.path.join(directory, "*.yml"))):
        d, problems = check_alerts(path, alert_uids)
        n = sum(len(g["rules"]) for g in d.get("groups", []))
        print("%-32s %3d rules %s" % (os.path.basename(path), n, "OK" if not problems else ""))
        for problem in problems:
            print("    " + problem)
        failed |= bool(problems)
    print("%d dashboards, %d panels, %d alert rules" % (len(dash_uids), total_panels, len(alert_uids)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
