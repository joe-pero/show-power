"""Tests: config checks, the generated package's structure, and its Jinja templates run against a
small Home Assistant-like template environment (states, is_state, now, as_datetime, timedelta).

Run: python3 -m unittest -v
"""
import copy
import datetime as dt
import os
import sys
import unittest

import jinja2
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from showpower.config import ConfigError, parse  # noqa: E402
from showpower.generate import (confirm_template, date_cond, ids, names_not, package, rain_template,  # noqa: E402
                                render)

EXAMPLE = os.path.join(ROOT, "examples", "config.example.yaml")
TZ = dt.timezone(dt.timedelta(hours=-7))
NOW = dt.datetime(2026, 10, 17, 18, 0, tzinfo=TZ)


def raw():
    with open(EXAMPLE) as f:
        return yaml.safe_load(f)


class HA:
    """Just enough of Home Assistant's template environment to run the generated templates."""

    def __init__(self, states=None, now=NOW):
        self.states = states or {}
        self.env = jinja2.Environment(undefined=jinja2.Undefined)
        self.env.globals.update(
            states=lambda e: self.states.get(e, "unknown"),
            is_state=lambda e, s: self.states.get(e) == s,
            now=lambda: now,
            timedelta=dt.timedelta,
            as_datetime=lambda s: dt.datetime.fromisoformat(s) if isinstance(s, str) else None,
            namespace=jinja2.utils.Namespace,
        )
        self.env.filters["int"] = lambda v, d=0: int(v) if str(v).lstrip("-").isdigit() else d

    def render(self, tpl, **vars):
        return self.env.from_string(tpl).render(**vars).strip()


def hours(*pcts, start=NOW):
    return [{"datetime": (start + dt.timedelta(hours=i)).isoformat(), "precipitation_probability": p}
            for i, p in enumerate(pcts)]


class ConfigTests(unittest.TestCase):
    def test_example_is_valid(self):
        cfg = parse(raw())
        self.assertEqual(cfg.slug, "yard_show")
        self.assertEqual(len(cfg.switches), 6)

    def test_rejects_bad_input(self):
        cases = [
            (lambda r: r["switches"][0].update(entity="porch plug"), "not an entity id"),
            (lambda r: r["switches"][0].update(role="speaker"), "role must be"),
            (lambda r: r["switches"].append(dict(r["switches"][0])), "listed twice"),
            (lambda r: r["switches"][5].update(cut_by="switch.nope"), "cut_by"),
            (lambda r: r["schedule"].update(on="dusk"), "schedule.on"),
            (lambda r: r["schedule"].update(off_weeknight="8:30pm"), "HH:MM"),
            (lambda r: r["rain"].update(threshold=0), "threshold"),
            (lambda r: r["rain"].update(weather_entity="sensor.rain"), "must be one of weather"),
            (lambda r: r.update(switches=[]), "at least one switch"),
        ]
        for mutate, msg in cases:
            r = raw()
            mutate(r)
            with self.assertRaises(ConfigError) as cm:
                parse(r)
            self.assertIn(msg, str(cm.exception))


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.cfg = parse(raw())
        self.pkg = package(self.cfg)
        self.I = ids(self.cfg)

    def actions(self, seq):
        """Flatten a script sequence into (action, entity) pairs, in order, looking inside repeat/if."""
        out = []
        for step in seq:
            if "action" in step:
                out.append((step["action"], (step.get("target") or {}).get("entity_id")))
            if "repeat" in step:
                out += self.actions(step["repeat"]["sequence"])
            if "if" in step:
                out += self.actions(step.get("then", [])) + self.actions(step.get("else", []))
        return out

    def test_yaml_round_trip(self):
        text = render(self.cfg, "x.yaml")
        self.assertEqual(yaml.safe_load(text), self.pkg)
        self.assertNotIn("!!python", text)

    def test_skip_dates_survive_restart(self):
        helper = self.pkg["input_text"]["yard_show_skip_dates"]
        self.assertNotIn("initial", helper)          # an initial value would wipe the dates on every restart

    def test_on_order_projectors_first_delayed_last_all_verified(self):
        seq = self.pkg["script"]["yard_show_power_on"]["sequence"]
        ons = [e for a, e in self.actions(seq) if a.endswith(".turn_on") and e.startswith("switch.")]
        self.assertEqual(ons[0], "switch.porch_projectors_plug")
        self.assertEqual(ons[-2:], ["switch.extension_cord_plug", "switch.fog_machine_plug"])
        self.assertEqual(sorted(ons), sorted(s.entity for s in self.cfg.switches))
        self.assertEqual(sum("repeat" in s for s in seq), len(self.cfg.switches))   # every one verified
        self.assertEqual(seq[0]["target"]["entity_id"], self.I["off"])              # cancels a pending OFF

    def test_off_never_hard_cuts_and_sound_goes_first(self):
        seq = self.pkg["script"]["yard_show_power_off"]["sequence"]
        acts = self.actions(seq)
        names = [a for a, _ in acts]
        offs = [e for a, e in acts if a.endswith(".turn_off")]
        self.assertEqual(acts[0], ("script.turn_off", self.I["on"]))                # cancels a pending ON
        self.assertIn("button.press", names)                                        # projector standby
        self.assertLess(names.index("button.press"), offs.index("switch.porch_projectors_plug"))
        self.assertLess(offs.index("switch.yard_speakers_plug"), offs.index("switch.porch_projectors_plug"))
        self.assertNotIn("switch.fog_machine_plug", offs)                           # cut_by: never switched
        delays = [s for s in seq if "delay" in s and "minutes" in s["delay"]]
        self.assertEqual(delays[0]["delay"]["minutes"], 15)
        gate = [s for s in seq if "if" in s and "projectors NOT confirmed off" in str(s.get("else"))]
        self.assertEqual(len(gate), 1, "projector power must be cut only behind the confirm gate")

    def test_no_confirm_sensor_cuts_after_cool_down(self):
        r = raw()
        r["projectors"].pop("confirm")
        seq = package(parse(r))["script"]["yard_show_power_off"]["sequence"]
        self.assertNotIn("NOT confirmed", str(seq))
        self.assertIn("switch.porch_projectors_plug", str(seq))

    def test_lights_stay_on_when_scheduled(self):
        seq = self.pkg["script"]["yard_show_power_off"]["sequence"]
        guarded = [s for s in seq if "if" in s and "scheduled" in str(s["if"])]
        self.assertEqual(len(guarded), 1)
        self.assertIn("switch.pathway_lights", str(guarded[0]["then"]))

    def test_automations(self):
        autos = {a["id"]: a for a in self.pkg["automation"]}
        self.assertEqual(set(autos), {"yard_show_power_on", "yard_show_power_off_sun", "yard_show_power_off_fri",
                                      "yard_show_lights_off", "yard_show_ignore_rain_reset"})
        self.assertEqual(autos["yard_show_power_on"]["triggers"][0]["offset"], "-00:15:00")
        manual = 'not in ["10-31"]'                                                # the manual-OFF rule
        self.assertIn(manual, str(autos["yard_show_power_off_fri"]["conditions"]))
        self.assertIn(manual, str(autos["yard_show_lights_off"]["conditions"]))
        self.assertNotIn(manual, str(autos["yard_show_power_on"]["conditions"]))     # ON still runs on Halloween

    def test_no_rain_section_means_no_forecast_call(self):
        r = raw()
        r.pop("rain")
        pkg = package(parse(r))
        self.assertNotIn("weather.get_forecasts", str(pkg))
        self.assertNotIn("input_boolean", pkg)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = parse(raw())

    def rain(self, fc):
        return HA().render(rain_template(self.cfg), **({"fc": fc} if fc is not None else {}))

    def test_rain_highest_chance_inside_window(self):
        self.assertEqual(self.rain({"weather.home": {"forecast": hours(10, 20, 70, 30)}}), "70")

    def test_rain_ignores_hours_beyond_window(self):
        self.assertEqual(self.rain({"weather.home": {"forecast": hours(10, 10, 10, 10, 10, 10, 95)}}), "10")

    def test_rain_handles_missing_values(self):
        self.assertEqual(self.rain({"weather.home": {"forecast": hours(None, 35, None)}}), "35")

    def test_rain_unknown_when_forecast_unreadable(self):
        self.assertEqual(self.rain(None), "unknown")                             # call failed
        self.assertEqual(self.rain({"weather.other": {"forecast": []}}), "unknown")
        self.assertEqual(self.rain({"weather.home": {"forecast": hours(None, None)}}), "unknown")

    def test_names_not(self):
        st = {s.entity: "on" for s in self.cfg.switches}
        st["switch.yard_speakers_plug"] = "off"
        st["switch.fog_machine_plug"] = "unavailable"
        out = HA(st).render(names_not(self.cfg, "on", self.cfg.switches))
        self.assertEqual(out, "['Yard speakers', 'Fog machine (plugged into the extension cord)']")

    def test_confirm(self):
        tpl = confirm_template(self.cfg)
        self.assertEqual(HA({"sensor.porch_projector_state": "standby"}).render(tpl), "True")
        self.assertEqual(HA({"sensor.porch_projector_state": "on"}).render(tpl), "False")

    def test_season_and_skip_dates(self):
        season, skip = (c["value_template"] for c in date_cond(self.cfg))
        I = ids(self.cfg)
        self.assertEqual(HA().render(season), "True")
        self.assertEqual(HA(now=NOW.replace(month=11, day=2)).render(season), "False")
        self.assertEqual(HA({I["skip"]: "2026-10-16,2026-10-18"}).render(skip), "True")
        self.assertEqual(HA({I["skip"]: "2026-10-16,2026-10-17"}).render(skip), "False")

    def test_season_across_new_year(self):
        r = raw()
        r["schedule"]["season"] = {"start": "11-25", "end": "01-06"}
        season = date_cond(parse(r))[0]["value_template"]
        for (m, d), want in (((12, 20), "True"), ((1, 3), "True"), ((2, 1), "False"), ((11, 1), "False")):
            self.assertEqual(HA(now=NOW.replace(month=m, day=d)).render(season), want, (m, d))


if __name__ == "__main__":
    unittest.main()
