"""Unit tests. Run from the repo root:  python -m unittest discover -s tests -v
They use simulated plugs and temporary folders; no network, no real plug."""
import csv
import io
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import plug_server as ps  # noqa: E402

TWO_PLUGS = {"poll_seconds": 5, "plugs": [{"id": "1", "name": "Heater", "demo_w": 1400},
                                          {"id": "2", "name": "Fridge", "demo_w": 90}]}


class Base(unittest.TestCase):
    raw = TWO_PLUGS

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="plugtest-")
        self.sent = []
        self._notify = ps.notify
        ps.notify = lambda msg: self.sent.append(msg)   # never send anything for real
        self.prepare()
        ps.init(json.loads(json.dumps(self.raw)), demo=True, data_dir=self.dir)
        self.c = ps.app.test_client()

    def prepare(self):
        pass

    def tearDown(self):
        ps.notify = self._notify
        time.sleep(0.05)          # let alert threads finish
        shutil.rmtree(self.dir, ignore_errors=True)

    def post(self, url, **body):
        r = self.c.post(url, json=body)
        return r.status_code, r.get_json()

    def status(self, plug="1"):
        return self.c.get(f"/api/status?plug={plug}").get_json()

    def insert(self, rows):
        conn = ps.db()
        conn.executemany("INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()


# --------------------------------------------------------------------------

class TestCodecs(unittest.TestCase):
    def test_cycle_roundtrip(self):
        t = [{"enabled": True, "days": ["mon", "wed"], "start": "03:00", "end": "23:59", "on_min": 5, "off_min": 1439},
             {"enabled": False, "days": list(ps.DAYS), "start": "00:00", "end": "00:01", "on_min": 1, "off_min": 1}]
        self.assertEqual(ps.decode_cycle(ps.encode_cycle(t)), t)

    def test_random_roundtrip(self):
        t = [{"enabled": False, "days": ["sun"], "start": "02:00", "end": "02:15"}]
        self.assertEqual(ps.decode_random(ps.encode_random(t)), t)

    def test_inching_roundtrip(self):
        self.assertEqual(ps.decode_inching(ps.encode_inching(True, 65535)), {"enabled": True, "seconds": 65535})

    def test_garbage_decodes_to_empty(self):
        for bad in ("", "!!!", None, 5, "AAAA"):    # "AAAA" = 3 bytes: too short for a timer
            self.assertEqual(ps.decode_cycle(bad), [])
            self.assertEqual(ps.decode_random(bad), [])
        for bad in ("", "!!!", None, 5, "AA=="):
            self.assertEqual(ps.decode_inching(bad), {"enabled": False, "seconds": 5})


class TestParse(unittest.TestCase):
    dp = {k: str(v) for k, v in ps.DEFAULT_DPS.items()}

    def test_scaling(self):
        st = ps.parse({"1": True, "19": 12345, "20": 2318, "18": 5321, "26": 0b101}, self.dp)
        self.assertEqual((st["on"], st["power_w"], st["voltage_v"], st["current_a"]), (True, 1234.5, 231.8, 5.321))
        self.assertEqual(st["faults"], ["overcurrent", "overpower"])

    def test_other_dp_numbers(self):
        dp = dict(self.dp, power="22", voltage="23")
        self.assertEqual(ps.parse({"22": 100, "23": 2300}, dp)["power_w"], 10.0)

    def test_unexpected_types_never_raise(self):
        st = ps.parse({"1": "yes", "19": "abc", "20": None, "18": True, "26": "x", "9": [1],
                       "42": 7, "43": {}, "44": 1.5}, self.dp)
        self.assertEqual((st["on"], st["power_w"], st["voltage_v"], st["current_a"]), (False, None, None, None))
        self.assertEqual((st["faults"], st["countdown_s"], st["cycle"]), ([], 0, []))

    def test_missing_power_is_none(self):
        self.assertIsNone(ps.parse({"1": True, "20": 2300}, self.dp)["power_w"])


class TestChecks(unittest.TestCase):
    def test_hm(self):
        for ok in ("00:00", "23:59", "07:05"):
            self.assertEqual(ps.check_hm(ok), ok)
        for bad in ("24:00", "7:05", "07:60", None, 705, "07:05:00"):
            self.assertRaises(ps.ApiError, ps.check_hm, bad)

    def test_days(self):
        self.assertEqual(ps.check_days(["wed", "sun"]), ["sun", "wed"])
        for bad in (None, [], "mon", ["monday"], [1]):
            self.assertRaises(ps.ApiError, ps.check_days, bad)

    def test_numbers(self):
        self.assertEqual(ps.check_int("5", 1, 9, "x"), 5)
        for bad in (True, "5.5", None, 0, 10, float("nan")):
            self.assertRaises(ps.ApiError, ps.check_int, bad, 1, 9, "x")
        self.assertEqual(ps.check_num("0.32", "x"), 0.32)
        for bad in (float("nan"), float("inf"), -1, "abc", None, False):
            self.assertRaises(ps.ApiError, ps.check_num, bad, "x")

    def test_schedule(self):
        s = ps.clean_schedule({"time": "07:00", "action": "on", "days": ["mon"]})
        self.assertTrue(s["enabled"] and len(s["id"]) == 8)
        for bad in ({"time": "07:00", "action": "on", "days": None},
                    {"time": "7", "action": "on", "days": ["mon"]},
                    {"time": "07:00", "action": "toggle", "days": ["mon"]}, "x"):
            self.assertRaises(ps.ApiError, ps.clean_schedule, bad)


class TestMask(unittest.TestCase):
    def test_mask(self):
        m = ps.URL_MASK
        self.assertEqual(ps.mask_url(""), "")
        self.assertEqual(ps.mask_url("https://discord.com/api/webhooks/1/SECRET"), f"https://discord.com/{m}")
        self.assertEqual(ps.mask_url("https://ntfy.sh/topic"), f"https://ntfy.sh/{m}")
        self.assertEqual(ps.mask_url("https://user:pw@ntfy.example.com:8443/t"), f"https://ntfy.example.com:8443/{m}")
        self.assertEqual(ps.mask_url("garbage"), m)


class TestConfig(unittest.TestCase):
    def test_legacy_single_plug(self):
        p = ps.plug_configs({"device_id": "a", "local_key": "k", "ip": "1.2.3.4", "version": 3.5,
                             "name": "Old", "port": 8080})
        self.assertEqual(p, [{"id": "1", "device_id": "a", "local_key": "k", "ip": "1.2.3.4",
                              "version": 3.5, "name": "Old"}])

    def test_plug_list_default_ids(self):
        p = ps.plug_configs({"plugs": [{"device_id": "a", "local_key": "k", "ip": "x"},
                                       {"id": "garage", "device_id": "b", "local_key": "k", "ip": "y"}]})
        self.assertEqual([x["id"] for x in p], ["1", "garage"])

    def test_bad_configs(self):
        for raw in ({"plugs": []}, {"plugs": [{"id": "a b", "device_id": "a", "local_key": "k", "ip": "x"}]},
                    {"plugs": [{"id": "1", "device_id": "a", "local_key": "k", "ip": "x"},
                               {"id": "1", "device_id": "b", "local_key": "k", "ip": "y"}]},
                    {"plugs": [{"device_id": "a", "ip": "x"}]}, {"plugs": ["x"]}):
            self.assertRaises(ValueError, ps.plug_configs, raw)


# --------------------------------------------------------------------------

class TestUpgradeFromSinglePlug(Base):
    """A data folder of the single-plug version: old state.json and old energy.db."""
    raw = {"poll_seconds": 5, "price_per_kwh": 0.4, "plugs": [{"id": "1", "name": "AC plug"},
                                                              {"id": "2", "name": "New plug"}]}

    def prepare(self):
        old = {"schedules": [{"time": "07:00", "action": "on", "days": ["mon"], "enabled": True},
                             {"time": "08:00", "action": "on", "days": None}],
               "price_per_kwh": 0.3205, "device": "Heater",
               "alerts": {"power_above_w": 1500, "notify_url": "https://ntfy.sh/x", "budget_eur": 20,
                          "monthly_report": True}, "last_report": "2026-11"}
        with open(os.path.join(self.dir, "demo_state.json"), "w") as f:
            json.dump(old, f)
        conn = sqlite3.connect(os.path.join(self.dir, "demo_energy.db"))
        conn.execute("""CREATE TABLE readings (ts INTEGER PRIMARY KEY, on_state INTEGER, power_w REAL,
            voltage_v REAL, current_a REAL, wh REAL, device TEXT NOT NULL DEFAULT 'AC')""")
        conn.executemany("INSERT INTO readings VALUES (?,1,100,230,0.4,0.14,?)",
                         [(1_700_000_000 + 5 * i, "AC" if i < 50 else "Heater") for i in range(100)])
        conn.commit()
        conn.close()

    def test_database_moved_to_first_plug(self):
        conn = ps.db()
        rows = conn.execute("SELECT plug, device, COUNT(*) FROM readings GROUP BY plug, device ORDER BY device").fetchall()
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        self.assertEqual(rows, [("1", "AC", 50), ("1", "Heater", 50)])
        self.assertEqual(tables, ["readings"])

    def test_state_moved_to_first_plug(self):
        p1, p2 = ps.state["plugs"]["1"], ps.state["plugs"]["2"]
        self.assertEqual(p1["device"], "Heater")
        self.assertEqual(p1["power_above_w"], 1500)
        self.assertEqual([s["time"] for s in p1["schedules"]], ["07:00"])   # invalid one dropped
        self.assertEqual((p2["schedules"], p2["power_above_w"], p2["device"]), ([], 0, "New plug"))
        self.assertEqual(ps.state["alerts"], {"notify_url": "https://ntfy.sh/x", "budget_eur": 20,
                                              "monthly_report": True})
        self.assertEqual(ps.state["price_per_kwh"], 0.3205)          # state.json wins over config
        self.assertNotIn("schedules", ps.state)

    def test_upgrade_runs_once(self):
        ps.init(json.loads(json.dumps(self.raw)), demo=True, data_dir=self.dir)
        conn = ps.db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 100)
        conn.close()


class TestTwoPlugs(Base):
    def test_list(self):
        self.assertEqual([p["name"] for p in self.c.get("/api/plugs").get_json()["plugs"]], ["Heater", "Fridge"])

    def test_switch_one_plug(self):
        self.assertEqual(self.post("/api/switch", plug="2", on=True), (200, {"ok": True}))
        self.assertTrue(self.status("2")["status"]["on"])
        self.assertFalse(ps.PLUGS["1"].status()["on"] if ps.PLUGS["1"].status() else False)
        ps.PLUGS["1"].refresh()
        self.assertFalse(self.status("1")["status"]["on"])

    def test_switch_needs_bool(self):
        self.assertEqual(self.post("/api/switch", plug="1", on="yes")[0], 400)

    def test_unknown_plug(self):
        self.assertEqual(self.c.get("/api/status?plug=9").status_code, 404)
        self.assertEqual(self.post("/api/switch", plug="9", on=True)[0], 404)

    def test_empty_or_unknown_plug_never_means_first(self):
        for pid in ("", "9"):
            self.assertEqual(self.post("/api/switch", plug=pid, on=True)[0], 404)
            self.assertEqual(self.post("/api/device", plug=pid, setting="countdown", value=60)[0], 404)
        self.assertEqual(self.c.post("/api/switch?plug=", json={"on": True}).status_code, 404)
        ps.PLUGS["1"].refresh()
        self.assertFalse(self.status("1")["status"]["on"])

    def test_confirm_switch_null_keeps_asking(self):
        ps.state["plugs"]["1"]["confirm_switch"] = None
        ps.save_state()
        ps.init(json.loads(json.dumps(self.raw)), demo=True, data_dir=self.dir)
        self.assertTrue(self.status("1")["plug"]["confirm_switch"])

    def test_schedule_not_kept_when_save_fails(self):
        real = ps.save_state

        def broken():
            raise OSError("disk full")
        ps.save_state = broken
        try:
            code, j = self.post("/api/schedules", plug="1", time="07:00", action="on", days=["mon"])
        finally:
            ps.save_state = real
        self.assertEqual((code, j["ok"]), (500, False))
        self.assertEqual(ps.state["plugs"]["1"]["schedules"], [])

    def test_default_plug_is_first(self):
        self.assertEqual(self.c.get("/api/status").get_json()["plug"],
                         {"id": "1", "name": "Heater", "confirm_switch": True})

    def test_readings_in_same_second_kept_per_plug(self):
        now = time.time()
        for p in ps.PLUGS.values():
            ps.poll_once(p, now, 5)
        conn = ps.db()
        n = conn.execute("SELECT COUNT(DISTINCT plug) FROM readings").fetchone()[0]
        conn.close()
        self.assertEqual(n, 2)

    def test_rename(self):
        self.assertEqual(self.post("/api/settings", plug="2", plug_name="  Garage   freezer ")[0], 200)
        self.assertEqual(self.status("2")["plug"]["name"], "Garage freezer")
        self.assertEqual(ps.PLUGS["2"].name, "Garage freezer")
        with open(ps.STATE_PATH) as f:
            self.assertEqual(json.load(f)["plugs"]["2"]["name"], "Garage freezer")
        self.assertEqual(self.post("/api/settings", plug="2", plug_name="   ")[0], 400)
        self.assertEqual(ps.PLUGS["1"].name, "Heater")

    def test_settings_validated_before_applied(self):
        before = json.dumps(ps.state, sort_keys=True)
        for bad in ({"price_per_kwh": "abc"}, {"price_per_kwh": -1}, {"budget_eur": "x"},
                    {"price_per_kwh": 0.5, "device": "   "}, {"notify_url": "ftp://x"},
                    {"power_above_w": True}):
            code, j = self.post("/api/settings", plug="1", **bad)
            self.assertEqual((code, j["ok"]), (400, False), bad)
        self.assertEqual(json.dumps(ps.state, sort_keys=True), before)
        r = self.c.post("/api/settings", data='{"price_per_kwh": NaN}', content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(math.isfinite(ps.state["price_per_kwh"]))

    def test_confirm_switch_per_plug(self):
        # On by default for every plug; turned off for one plug only; survives a restart
        self.assertTrue(self.status("1")["plug"]["confirm_switch"])
        self.assertTrue(self.status("2")["plug"]["confirm_switch"])
        self.assertEqual(self.post("/api/settings", plug="2", confirm_switch=False), (200, {"ok": True}))
        self.assertFalse(self.status("2")["plug"]["confirm_switch"])
        self.assertTrue(self.status("1")["plug"]["confirm_switch"])
        ps.init(json.loads(json.dumps(self.raw)), demo=True, data_dir=self.dir)
        self.assertFalse(self.status("2")["plug"]["confirm_switch"])
        self.assertTrue(self.status("1")["plug"]["confirm_switch"])
        for bad in ("no", 0, None):
            self.assertEqual(self.post("/api/settings", plug="2", confirm_switch=bad)[0], 400)
        self.assertFalse(ps.state["plugs"]["2"]["confirm_switch"])

    def test_per_plug_settings(self):
        self.post("/api/settings", plug="2", device="Freezer", power_above_w=200)
        self.assertEqual(ps.state["plugs"]["2"]["device"], "Freezer")
        self.assertEqual(ps.state["plugs"]["1"]["power_above_w"], 0)
        self.assertEqual(self.status("2")["alerts_cfg"]["power_above_w"], 200)

    def test_notify_url_masked_and_kept(self):
        secret = "https://discord.com/api/webhooks/1/SECRET"
        self.post("/api/settings", notify_url=secret)
        body = self.c.get("/api/status").get_data(as_text=True)
        self.assertNotIn("SECRET", body)
        shown = self.status()["alerts_cfg"]["notify_url"]
        self.post("/api/settings", notify_url=shown, power_above_w=5)
        self.assertEqual(ps.state["alerts"]["notify_url"], secret)
        self.post("/api/settings", notify_url="")
        self.assertEqual(ps.state["alerts"]["notify_url"], "")

    def test_schedules(self):
        self.assertEqual(self.post("/api/schedules", plug="2", time="07:00", action="on", days=None)[0], 400)
        self.assertEqual(self.post("/api/schedules", plug="2", time="25:00", action="on", days=["mon"])[0], 400)
        self.assertEqual(self.post("/api/schedules", plug="2", time="07:00", action="on", days=["mon"])[0], 200)
        self.post("/api/schedules", plug="2", time="06:00", action="off", days=["tue"])
        s = self.status("2")["schedules"]
        self.assertEqual([x["time"] for x in s], ["06:00", "07:00"])
        self.assertEqual(self.status("1")["schedules"], [])
        self.post("/api/schedules", plug="2", toggle=s[1]["id"])
        self.assertFalse(self.status("2")["schedules"][1]["enabled"])
        self.post("/api/schedules", plug="2", delete=s[0]["id"])
        self.post("/api/schedules", plug="2", delete=0)           # old clients send the index
        self.assertEqual(self.status("2")["schedules"], [])
        self.assertEqual(self.post("/api/schedules", plug="2", delete="nope")[0], 404)

    def test_device_settings(self):
        for setting, value in (("led", "blink"), ("power_on", "maybe"), ("child_lock", "yes"),
                               ("countdown", -1), ("countdown", "x"), ("inching", {"enabled": True, "seconds": 0}),
                               ("inching", 5), ("cycle", [{"days": [], "start": "01:00", "end": "02:00",
                                                           "on_min": 1, "off_min": 1}]),
                               ("random", [{}] * 17), ("nope", 1)):
            self.assertEqual(self.post("/api/device", plug="1", setting=setting, value=value)[0], 400, setting)
        cyc = [{"enabled": True, "days": ["mon"], "start": "03:00", "end": "04:00", "on_min": 5, "off_min": 10}]
        self.assertEqual(self.post("/api/device", plug="2", setting="cycle", value=cyc)[0], 200)
        self.assertEqual(self.status("2")["status"]["cycle"], cyc)
        self.assertEqual(self.post("/api/device", plug="2", setting="led", value="none")[0], 200)
        self.assertEqual(self.status("2")["status"]["led"], "none")

    def test_switch_failure_reported(self):
        def boom(dp, v):
            raise RuntimeError("timeout")
        ps.PLUGS["1"].backend.set_dp = boom
        code, j = self.post("/api/switch", plug="1", on=True)
        self.assertEqual(code, 502)
        self.assertIn("timeout", j["error"])

    def test_info(self):
        i = self.c.get("/api/info?plug=2").get_json()
        self.assertEqual((i["id"], i["name"]), ("2", "Fridge"))

    def test_bad_json(self):
        r = self.c.post("/api/settings", data="nope", content_type="application/json")
        self.assertEqual(r.status_code, 400)


class TestEnergy(Base):
    def rows(self, plug, start, n, step=5, device="X", on=1, w=1000):
        return [(plug, start + i * step, on, w, 230, w / 230, w * step / 3600, device) for i in range(n)]

    def test_device_totals_incremental_matches_full_scan(self):
        t = int(time.time()) - 86400
        self.insert(self.rows("1", t, 500, device="A") + self.rows("2", t, 300, device="B", w=50))
        conn = ps.db()
        ps.device_totals(conn, "1", 5, 0.3)            # builds the cache
        self.insert(self.rows("1", t + 5000, 200, device="C") + self.rows("1", t + 10000, 10, device="A", on=0))
        cached = ps.device_totals(conn, "1", 5, 0.3)
        ps.invalidate_energy()
        full = ps.device_totals(conn, "1", 5, 0.3)
        conn.close()
        self.assertEqual(cached, full)
        self.assertEqual(sorted(d["device"] for d in full), ["A", "C"])     # plug 2 not mixed in

    def test_totals_per_plug_and_cache_invalidated_by_relabel(self):
        t = int(time.time()) - 600
        self.insert(self.rows("1", t, 100, device="A") + self.rows("2", t, 100, device="B", w=100))
        e1 = self.status("1")["energy"]
        e2 = self.status("2")["energy"]
        self.assertAlmostEqual(e1["total_kwh"], 100 * 1000 * 5 / 3600 / 1000, places=2)
        self.assertAlmostEqual(e2["total_kwh"], 100 * 100 * 5 / 3600 / 1000, places=2)
        frm = datetime.fromtimestamp(t - 60).strftime("%Y-%m-%dT%H:%M")
        to = datetime.fromtimestamp(t + 900).strftime("%Y-%m-%dT%H:%M")
        code, j = self.post("/api/relabel", plug="1", **{"from": frm, "to": to, "device": "Z"})
        self.assertEqual((code, j["changed"]), (200, 100))
        self.assertEqual([d["device"] for d in self.status("1")["energy"]["devices"]], ["Z"])
        self.assertEqual([d["device"] for d in self.status("2")["energy"]["devices"]], ["B"])

    def test_reset_one_plug(self):
        t = int(time.time()) - 600
        self.insert(self.rows("1", t, 10) + self.rows("2", t, 20))
        self.assertEqual(self.post("/api/reset", plug="1", confirm="nope")[0], 400)
        code, j = self.post("/api/reset", plug="1", confirm="RESET")
        self.assertEqual((code, j["deleted"]), (200, 10))
        self.assertEqual(self.status("1")["energy"]["total_kwh"], 0)
        self.assertGreater(self.status("2")["energy"]["total_kwh"], 0)

    def test_coverage_uses_measured_time(self):
        # readings every 6 s with poll 5 used to show ~83 %
        day0 = ps.midnight(datetime.now())
        secs = int(time.time()) - day0
        if secs < 600:
            self.skipTest("too close to midnight")
        self.insert(self.rows("1", day0 + 1, secs // 6, step=6))
        self.assertGreaterEqual(self.status("1")["energy"]["coverage_today_pct"], 98)

    def test_csv_export_streams_all_plugs(self):
        t = int(time.time()) - 600
        self.insert(self.rows("1", t, 3000) + self.rows("2", t, 5, device="B"))
        r = self.c.get("/api/export/readings.csv")
        rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
        self.assertEqual(rows[0], ["time", "plug", "device", "on", "power_w", "voltage_v", "current_a", "energy_wh"])
        self.assertEqual(len(rows) - 1, 3005)
        self.assertEqual({x[1] for x in rows[1:]}, {"Heater", "Fridge"})
        r = self.c.get("/api/export/readings.csv?plug=2&device=B")
        self.assertEqual(len(r.get_data(as_text=True).strip().splitlines()) - 1, 5)
        self.assertEqual(self.c.get("/api/export/readings.csv?plug=9").status_code, 404)

    def test_chart_per_plug(self):
        t = int(time.time()) - 600
        self.insert(self.rows("2", t, 10, w=3600))
        d = self.c.get("/api/chart?plug=2&range=day").get_json()
        self.assertAlmostEqual(d["total_kwh"], 0.05, places=3)
        self.assertEqual(self.c.get("/api/chart?plug=1&range=day").get_json()["total_kwh"], 0)
        self.assertEqual(self.c.get("/api/chart?plug=1&range=year").status_code, 400)
        self.assertEqual(self.c.get("/api/chart?plug=1&date=07.11.2026").status_code, 400)

    def test_midnight_is_dst_safe(self):
        d = datetime(2026, 3, 30, 12)      # day after the spring DST change in Europe
        self.assertEqual(datetime.fromtimestamp(ps.midnight(d, 6)).strftime("%H:%M"), "00:00")


class TestSchedulesAndAlerts(Base):
    def test_schedule_fires_and_catches_up(self):
        p = ps.PLUGS["1"]
        self.post("/api/schedules", plug="1", time="07:00", action="on", days=list(ps.DAYS))
        self.post("/api/schedules", plug="1", time="07:02", action="off", days=list(ps.DAYS))
        day = datetime(2026, 11, 9)
        ps.run_schedules(p, day.replace(hour=6, minute=59))
        p.refresh()
        self.assertFalse(p.status()["on"])
        ps.run_schedules(p, day.replace(hour=7, minute=0, second=3))
        p.refresh()
        self.assertTrue(p.status()["on"])
        ps.run_schedules(p, day.replace(hour=7, minute=0, second=40))     # same minute: no repeat
        p.set("switch", False)
        ps.run_schedules(p, day.replace(hour=7, minute=0, second=50))
        p.refresh()
        self.assertFalse(p.status()["on"])
        p.set("switch", True)
        ps.run_schedules(p, day.replace(hour=7, minute=5))      # 07:02 was missed by a slow pass
        p.refresh()
        self.assertFalse(p.status()["on"])

    def test_schedule_respects_days_and_pause(self):
        p = ps.PLUGS["2"]
        self.post("/api/schedules", plug="2", time="07:00", action="on", days=["tue"])
        mon = datetime(2026, 11, 9)                     # a Monday
        ps.run_schedules(p, mon.replace(hour=6, minute=59))
        ps.run_schedules(p, mon.replace(hour=7))
        p.refresh()
        self.assertFalse(p.status()["on"])

    def test_dst_fall_back_fires_once(self):
        p = ps.PLUGS["1"]
        self.post("/api/schedules", plug="1", time="02:30", action="on", days=list(ps.DAYS))
        calls = []
        p.switch = lambda on, reason="": calls.append(on)
        d = datetime(2026, 10, 25)
        for h, m in ((2, 29), (2, 30), (2, 59), (2, 0), (2, 30), (3, 1)):   # clock goes back 1 h
            ps.run_schedules(p, d.replace(hour=h, minute=m))
        self.assertEqual(calls, [True])

    def test_failed_schedule_retried_then_alerted(self):
        p = ps.PLUGS["1"]
        self.post("/api/schedules", plug="1", time="07:00", action="on", days=list(ps.DAYS))
        tries = []

        def fail(on, reason=""):
            tries.append(on)
            return "timeout"
        p.switch = fail
        day = datetime(2026, 11, 9)
        ps.run_schedules(p, day.replace(hour=6, minute=59))
        for s in range(0, 30, 5):
            ps.run_schedules(p, day.replace(hour=7, second=s))
        self.assertEqual(len(tries), ps.SCHED_TRIES)
        self.assertTrue(any("schedule 07:00" in a["msg"] for a in ps.alerts_log))

    def test_older_schedule_never_runs_after_newer(self):
        # "on" fails and waits for a retry; "off" five minutes later must win
        p = ps.PLUGS["1"]
        self.post("/api/schedules", plug="1", time="22:00", action="on", days=list(ps.DAYS))
        self.post("/api/schedules", plug="1", time="22:05", action="off", days=list(ps.DAYS))
        calls, fail = [], [True]

        def sw(on, reason=""):
            calls.append(on)
            return "timeout" if fail[0] else None
        p.switch = sw
        day = datetime(2026, 11, 9)
        ps.run_schedules(p, day.replace(hour=21, minute=59))
        ps.run_schedules(p, day.replace(hour=22, minute=0))        # on: fails
        fail[0] = False
        ps.run_schedules(p, day.replace(hour=22, minute=5))        # off is newer: retry of on dropped
        ps.run_schedules(p, day.replace(hour=22, minute=5, second=5))
        self.assertEqual(calls, [True, False])
        # Next day a slow pass catches up 22:00 and 22:05 together: only the newest runs
        calls.clear()
        ps.run_schedules(p, day.replace(hour=21, minute=59) + timedelta(days=1))
        ps.run_schedules(p, day.replace(hour=22, minute=10) + timedelta(days=1))
        self.assertEqual(calls, [False])

    def test_manual_switch_cancels_schedule_retry(self):
        p = ps.PLUGS["1"]
        p.sched_retry = [({"time": "07:00", "action": "on"}, datetime(2026, 11, 9, 7), 1)]
        self.post("/api/switch", plug="1", on=False)
        self.assertEqual(p.sched_retry, [])

    def test_power_alert_hysteresis(self):
        p = ps.PLUGS["1"]
        self.post("/api/settings", plug="1", power_above_w=1000)
        backend = p.backend
        for w in (1100, 1050, 990, 1100, 900, 1100):
            backend.watts = w
            backend.d[p.dp["switch"]] = True
            backend.t0 = time.time() - 9 * math.pi       # sin(...) == 0 -> exactly w
            ps.poll_once(p, time.time(), 5)
        power = [a for a in ps.alerts_log if "above your limit" in a["msg"]]
        self.assertEqual(len(power), 2)      # 1100 (on), 990 no clear, 900 clears, 1100 again

    def test_offline_and_back_online(self):
        p = ps.PLUGS["2"]
        real = p.backend.get_dps

        def down():
            raise RuntimeError("no response")
        p.backend.get_dps = down
        for _ in range(4):
            ps.poll_once(p, time.time(), 5)
        self.assertEqual(p.last["fail_count"], 4)
        p.backend.get_dps = real
        ps.poll_once(p, time.time(), 5)
        msgs = [a["msg"] for a in ps.alerts_log]
        self.assertEqual(msgs[:2], ["Fridge: plug is reachable again", "Fridge: plug is not reachable"])
        self.assertEqual(p.last["fail_count"], 0)

    def test_db_error_is_not_plug_failure(self):
        p = ps.PLUGS["1"]
        ps.DB_PATH = os.path.join(self.dir, "missing", "x.db")
        ps._db_ready = False
        ps.poll_once(p, time.time(), 5)
        self.assertEqual((p.last["fail_count"], p.last["error"]), (0, None))

    def test_budget_alert_once_per_month_across_restarts(self):
        n = datetime.now()
        self.post("/api/settings", budget_eur=1, price_per_kwh=1)
        self.insert([("2", int(time.time()) - 60, 1, 0, 0, 0, 2000, "B")])   # 2 kWh = 2 €
        ps.monthly_checks(n)
        ps.monthly_checks(n)
        ps.init(json.loads(json.dumps(self.raw)), demo=True, data_dir=self.dir)
        ps.monthly_checks(n)
        time.sleep(0.05)
        self.assertEqual(sum("over your budget" in a for a in self.sent), 1)

    def test_monthly_report_marked_only_after_send(self):
        n = datetime.now()
        first = datetime(n.year, n.month, 1)
        prev = first - timedelta(days=3)
        self.insert([("1", int(prev.timestamp()) + i * 5, 1, 1000, 230, 4, 1.4, "A") for i in range(10)] +
                    [("2", int(prev.timestamp()) + i * 5, 1, 100, 230, 0.4, 0.14, "B") for i in range(10)])
        self.post("/api/settings", monthly_report=True)
        ps.notify = lambda msg: "connection refused"
        ps.monthly_checks(n)
        self.assertNotEqual(ps.state["last_report"], n.strftime("%Y-%m"))
        texts = []
        ps.notify = lambda msg: texts.append(msg)
        ps.monthly_checks(n)
        self.assertEqual(ps.state["last_report"], n.strftime("%Y-%m"))
        self.assertIn("Per plug: Heater", texts[0])
        self.assertIn("Fridge", texts[0])


if __name__ == "__main__":
    unittest.main()
