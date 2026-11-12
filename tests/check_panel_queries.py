#!/usr/bin/env python3
"""Run every panel query of every provisioned dashboard (tag "observability")
through Grafana's datasource proxy against live data, and report errors and
empty results per panel.

    GRAFANA_PASSWORD=... python3 tests/check_panel_queries.py http://<monitoring-mac>:3000 <host_name>

<host_name> fills single-value host variables; multi-value variables are set
to All. An empty panel is not necessarily wrong (no errors, no MLX-VLM node,
no SSH sessions): read the list, do not just count it. Exits 1 on any query
error.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

G, HOST = sys.argv[1].rstrip("/"), sys.argv[2]
AUTH = "Basic " + base64.b64encode(("%s:%s" % (os.environ.get("GRAFANA_USER", "admin"),
                                                os.environ.get("GRAFANA_PASSWORD", "admin"))).encode()).decode()
def get(path):
    req = urllib.request.Request(G + path, headers={"Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)
def query(expr):
    url = G + "/api/datasources/proxy/uid/victoriametrics/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    req = urllib.request.Request(url, headers={"Authorization": AUTH})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        return d["status"], len(d["data"]["result"]), ""
    except urllib.error.HTTPError as e:
        return "error", 0, e.read().decode()[:200]
total = errors = empty = 0
for item in get("/api/search?type=dash-db&tag=observability"):
    d = get("/api/dashboards/uid/" + item["uid"])["dashboard"]
    single = {v["name"] for v in d.get("templating", {}).get("list", []) if not v.get("multi")}
    subs = {"$__rate_interval": "2m", "$__range": "1h", "$__interval": "1m", "${__range}": "1h"}
    for v in d.get("templating", {}).get("list", []):
        subs["$" + v["name"]] = HOST if v["name"] in ("host", "host_name") and v["name"] in single else ".*"
    def panels(ps):
        for p in ps:
            yield p
            yield from panels(p.get("panels", []))
    e_list = []
    n = 0
    for p in panels(d["panels"]):
        for t in p.get("targets", []):
            expr = t.get("expr")
            if not expr:
                continue
            for k in sorted(subs, key=len, reverse=True):
                expr = expr.replace(k, subs[k])
            status, count, err = query(expr)
            n += 1
            if status != "success":
                errors += 1; e_list.append(("ERROR", p["title"], err))
            elif count == 0:
                empty += 1; e_list.append(("empty", p["title"], ""))
    total += n
    print("%-40s %3d queries, %d errors, %d empty" % (d["title"], n, sum(1 for e in e_list if e[0] == "ERROR"), sum(1 for e in e_list if e[0] == "empty")))
    for e in e_list:
        print("     ", e[0], "|", e[1], "|", e[2])
print("TOTAL %d queries, %d errors, %d empty" % (total, errors, empty))
sys.exit(1 if errors else 0)
