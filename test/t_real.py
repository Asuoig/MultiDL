import json, time, urllib.request

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(p, d=None):
    r = urllib.request.Request("http://127.0.0.1:8765" + p)
    b = None
    if d is not None:
        b = json.dumps(d).encode()
        r.add_header("Content-Type", "application/json")
    return json.loads(OP.open(r, b, timeout=15).read())


r = call("/api/tasks", {
    "url": "https://speed.cloudflare.com/__down?bytes=25000000",
    "max_conn": 8,
    "filename": "cf25m.bin",
})
tid = r["task"]["id"]
peak = 0
t0 = time.time()
while time.time() - t0 < 70:
    t = next(x for x in call("/api/tasks")["tasks"] if x["id"] == tid)
    peak = max(peak, t["connections"])
    if t["state"] in ("done", "error"):
        break
    time.sleep(0.5)
print("state:", t["state"], "| peak conn:", peak, "| size:", t["size"],
      "| %.1fs" % (time.time() - t0), "|", t["message"][:50])
