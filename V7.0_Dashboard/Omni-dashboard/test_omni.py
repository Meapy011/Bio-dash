"""Omni V7 with the stand-in devices (fake_omni.py): three people, each with an H10 and an O2 ring, on three radios.
Run:  python3 Omni-dashboard/test_omni.py      (about a minute; the page checks need Playwright + Chromium)

It runs in a scratch copy of this folder with its own HOME and data folder, so nothing here or in ~/Documents is
touched. KEEP=1 keeps the scratch folder (and the page screenshots) and prints where it is."""
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ok = 0


def check(name, cond, detail=""):
    global ok
    print(("  ok  " if cond else "  FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        raise SystemExit(1)
    ok += 1


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


tmp = tempfile.mkdtemp(prefix="omni-test-")
app_dir = os.path.join(tmp, "Omni-dashboard")
shutil.copytree(HERE, app_dir, ignore=shutil.ignore_patterns("__pycache__", "*.json"))
data = os.path.join(tmp, "data")
port = free_port()
# V6's remembered devices: the first V7 start should turn them into Person 1
for role, (name, addr) in {"polar": ("Polar H10 0FA7E001", "STAND-IN-H10-A"), "viatom": ("O2Ring 0FA1", "STAND-IN-O2-A")}.items():
    with open(os.path.join(app_dir, f"last_device_{role}.json"), "w") as f:
        json.dump({"address": addr, "name": name, "auto": True}, f)
env = {**os.environ, "HOME": tmp, "XDG_CONFIG_HOME": os.path.join(tmp, ".config"), "BIODASH_DATA_DIR": data,
       "BIODASH_OMNI_PORT": str(port), "OMNI_FAKE": "1", "PYTHONUNBUFFERED": "1"}
log = open(os.path.join(tmp, "log.txt"), "w")
base = f"http://127.0.0.1:{port}"


def start():
    return subprocess.Popen([sys.executable, "omni.py"], cwd=app_dir, env=env, stdout=log, stderr=subprocess.STDOUT)


def call(path, body=None, method=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait(cond, timeout, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


def people():
    return call("/api/people")[1]["people"]


def all_live(n):
    ppl = people()
    return ppl if len(ppl) == n and all(p["state"][r]["status"] == "Connected" for p in ppl for r in ("polar", "viatom")) else None


proc = start()
try:
    v = wait(lambda: call("/api/people")[1], 15)
    check("Omni answers", bool(v), open(os.path.join(tmp, "log.txt")).read()[-800:])
    p1 = v["people"][0]
    check("first start: Person 1 made from V6's remembered H10 and O2 ring",
          len(v["people"]) == 1 and p1["name"] == "Person 1" and p1["devices"]["polar"]["address"] == "STAND-IN-H10-A"
          and p1["devices"]["viatom"]["address"] == "STAND-IN-O2-A", v["people"])
    check("three radios found", v["radios"] == 3, v["radios"])
    found = wait(lambda: (lambda d: d if len(d["polar"]) >= 2 and len(d["viatom"]) >= 2 else None)(call("/api/people")[1]["devices"]), 15)
    check("the scanner lists the other H10s and O2 rings", bool(found), found)
    check("Person 1 reconnects to their remembered devices by themselves", bool(wait(lambda: all_live(1), 20)), people())

    code, out = call("/api/people", {"name": "Boss"})
    check("add a person by name", code == 200 and out["people"][1]["name"] == "Boss", out)
    boss = out["added"]
    code, out = call("/api/people", {})
    third = out["added"]
    check("a third person gets the next free name", out["people"][2]["name"] == "Person 2", [p["name"] for p in out["people"]])
    check("no fourth person (one H10 per radio)", call("/api/people", {})[0] == 400)
    for pid, k in ((boss, "B"), (third, "C")):
        call(f"/api/people/{pid}/device", {"role": "polar", "address": f"STAND-IN-H10-{k}"})
        call(f"/api/people/{pid}/device", {"role": "viatom", "address": f"STAND-IN-O2-{k}"})
    ppl = wait(lambda: all_live(3), 30)
    check("all six devices stream", bool(ppl), people())
    radios = [p["routing"]["polar"] for p in ppl]
    check(f"each H10 on its own radio, external radios first: {radios}", sorted(radios) == ["hci0", "hci1", "hci2"]
          and radios[:2] == ["hci1", "hci2"], radios)
    hrs = {p["name"]: p["state"]["polar"]["hr"] for p in ppl}
    check(f"each person their own heart: {hrs}", 55 <= hrs["Person 1"] <= 70 and 66 <= hrs["Boss"] <= 82 and 80 <= hrs["Person 2"] <= 96)
    spo2 = {p["name"]: p["state"]["viatom"]["spo2"] for p in ppl}
    check(f"and their own SpO2: {spo2}", abs(spo2["Person 1"] - 97) <= 1 and abs(spo2["Boss"] - 95) <= 1 and abs(spo2["Person 2"] - 92) <= 1)

    # the stream carries everyone
    with urllib.request.urlopen(base + "/api/stream", timeout=5) as r:
        got, t_end = {}, time.time() + 3
        while time.time() < t_end:
            line = r.readline().decode()
            if line.startswith("data: "):
                for pid, pk in json.loads(line[6:])["people"].items():
                    got.setdefault(pid, 0)
                    got[pid] += len(pk["polar"]["ecg_buffer"])
    check(f"the stream carries everyone's ECG: {sorted(got.values())} samples in 3 s",
          len(got) == 3 and all(n > 200 for n in got.values()), got)

    # recordings: per device, with whose they are
    time.sleep(3)
    pj = sorted(glob.glob(os.path.join(data, "Omni", "*", "*", "*_person.json")))
    owners = {json.load(open(f))["device"]: json.load(open(f))["person"] for f in pj}
    check(f"a session per device, saying whose: {len(pj)} sessions",
          owners.get("Polar H10 0FA7E002") == "Boss" and owners.get("O2Ring 0FA3") == "Person 2" and len(owners) == 6, owners)
    ecg = glob.glob(os.path.join(data, "Omni", "Polar H10 0FA7E002", "*", "*_ecg.csv"))
    check("Boss's ECG recorded in their H10's folder", ecg and sum(1 for _ in open(ecg[0])) > 500)
    h = call(f"/api/history?metric=spo2&minutes=5&person={boss}")[1]["spo2"]
    check(f"history per person: Boss's SpO2 {h[-1]['y'] if h else None}", h and abs(h[-1]["y"] - 95) <= 1)

    # moving, renaming, disconnecting, removing
    call(f"/api/people/{boss}", {"name": "The boss"})
    check("rename", [p["name"] for p in people()][1] == "The boss")
    call(f"/api/people/{boss}/device", {"role": "viatom", "disconnect": True})
    check("Disconnect: off and stays off", bool(wait(lambda: (lambda p: p["state"]["viatom"]["status"] == "Disconnected"
          and p["devices"]["viatom"]["auto"] is False)(people()[1]), 10)))
    time.sleep(6)
    check("still off a few sweeps later", people()[1]["state"]["viatom"]["status"] == "Disconnected")
    call(f"/api/people/{boss}/device", {"role": "viatom", "connect": True})
    check("Connect: back", bool(wait(lambda: people()[1]["state"]["viatom"]["status"] == "Connected", 20)))
    call(f"/api/people/{third}/device", {"role": "viatom", "address": "STAND-IN-O2-B"})

    def moved():
        dbg = call("/api/debug")[1]["people"][2]["viatom"]
        p = people()
        return (dbg["state"] == "Connected" and (dbg.get("device") or {}).get("address") == "STAND-IN-O2-B"
                and p[1]["devices"]["viatom"] is None and p[2]["devices"]["viatom"]["name"] == "O2Ring 0FA2")
    t0 = time.time()
    check("giving Boss's ring to Person 2 moves it (one owner per device), with its own name", bool(wait(moved, 25)), people())
    check(f"and quickly: {time.time() - t0:.1f} s (Person 2 retries while Boss lets go)", time.time() - t0 < 12)
    call(f"/api/people/{third}", {"move": -1})
    check("move a person left", [p["name"] for p in people()] == ["Person 1", "Person 2", "The boss"])

    # the page
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sync_playwright = None
        print("  (skipped the page: no Playwright)")
    if sync_playwright:
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page(viewport={"width": 1500, "height": 1300})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: m.type == "error" and "ERR_CONNECTION_REFUSED" not in m.text and errors.append(m.text))
            pg.goto(base + "/")
            pg.wait_for_function("document.querySelectorAll('#people .person').length === 3", timeout=15000)
            check("page: a card per person", True)
            pg.wait_for_function("[...document.querySelectorAll('#people [data-v=ecg_hr]')].every(e => /^\\d+$/.test(e.textContent))", timeout=15000)
            check("page: everyone's heart rate in their card", True)
            names = pg.eval_on_selector_all("#cmp-table tbody tr td:first-child", "els => els.map(e => e.textContent.trim())")
            check(f"page: the comparison table lists everyone: {names}", names == ["Person 1", "Person 2", "The boss"], names)
            pg.wait_for_function("OmniPeople.shared.hr.data.datasets.length === 3 && OmniPeople.shared.hr.data.datasets.every(d => d.data.length >= 3)", timeout=20000)
            dashes = pg.evaluate("OmniPeople.shared.hr.data.datasets.map(d => (d.borderDash || []).length)")
            check(f"page: shared heart-rate chart, a line per person, each its own style: {dashes}", sorted(dashes) == [0, 2, 2]
                  and len(set(map(str, pg.evaluate("OmniPeople.shared.hr.data.datasets.map(d => d.borderDash)")))) == 3)
            drawn = pg.evaluate("""() => [...document.querySelectorAll('#people .p-ecg canvas:nth-child(2)')].map(c => {
                const g = c.getContext('2d'), d = g.getImageData(0, 0, c.width, c.height).data; let n = 0;
                for (let i = 3; i < d.length; i += 4) if (d[i] > 60) n++; return n; })""")
            check(f"page: everyone's ECG drawn {drawn}", all(n > 300 for n in drawn), drawn)
            items = pg.inner_text("#found")
            check("page: devices nearby: assigned ones folded away", "assigned device" in items and "Person 1’s" not in items, items[:300])
            pg.click("#found [data-show-assigned]")
            items = pg.inner_text("#found")
            check("page: and shown on request, with whose they are", "Person 1’s" in items and "Move to The boss" in items, items[:300])
            pg.screenshot(path=os.path.join(tmp, "omni_three_people.png"), full_page=True)
            pg.keyboard.press("d")
            pg.wait_for_timeout(1500)
            dbg = pg.inner_text("#debug-panel")
            check("page: the radios panel shows whose H10 is on which radio", "Person 1 · H10" in dbg and "The boss · H10" in dbg, dbg[:400])
            pg.keyboard.press("d")
            pg.click("#people .person:nth-child(2) .p-name")
            pg.fill("#people .person:nth-child(2) input.p-name", "Lex")
            pg.keyboard.press("Enter")
            check("page: rename in place", bool(wait(lambda: people()[1]["name"] == "Lex", 5)))
            rm = "#people .person:nth-child(3) [data-act=remove]"
            pg.click(rm)
            check("page: Remove asks for a second click", len(people()) == 3 and "again" in pg.inner_text(rm))
            pg.click(rm)
            pg.wait_for_function("document.querySelectorAll('#people .person').length === 2", timeout=10000)
            check("page: removed", [p["name"] for p in people()] == ["Person 1", "Lex"])
            check("page: no script errors", not errors, errors)
            br.close()

    # people.json survives a restart
    proc.terminate()
    proc.wait(10)
    saved = json.load(open(os.path.join(app_dir, "people.json")))["people"]
    check("people.json keeps names, colours and devices", [p["name"] for p in saved] == ["Person 1", "Lex"]
          and saved[1]["devices"]["viatom"]["address"] == "STAND-IN-O2-B")
    proc = start()
    check("after a restart everyone reconnects", bool(wait(lambda: all_live(2), 30)), people())
    print(f"{ok} checks passed")
finally:
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
    if os.environ.get("KEEP"):
        print("kept", tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
