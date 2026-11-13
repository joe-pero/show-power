"""Turn a Config into a Home Assistant package (helpers, scripts, automations, one switch).

Design rules carried over from a real season of running an outdoor show:
  * ON is checked: every switch must report 'on'; it's re-sent, then you're told which didn't come on.
  * OFF never hard-cuts a hot projector lamp: standby first, a cool-down, then the power, and only if
    any confirm sensor agrees the lamp is off. If it can't confirm, the circuit stays ON and you're told.
  * Sound goes off first (bedtime); lights can stay on later.
  * A manual OFF cancels a pending ON, and a manual ON cancels a pending OFF.
  * Rain: the ON automation checks the forecast; if it can't, it runs the show and tells you why.
  * Nothing fails silently: every check that fails sends a notification and a logbook entry.
"""
from __future__ import annotations

import json

from .config import Config


def ids(cfg: Config) -> dict:
    s = cfg.slug
    return {
        "on": f"script.{s}_power_on", "off": f"script.{s}_power_off",
        "skip": f"input_text.{s}_skip_dates", "ignore_rain": f"input_boolean.{s}_ignore_rain",
        "switch": f"switch.{s}_power",
    }


# --------------------------------------------------------------------------- building blocks
def notify(cfg: Config, title: str, message: str) -> list:
    out = [{"action": "persistent_notification.create", "data": {"title": title, "message": message}}]
    out += [{"action": n, "data": {"title": title, "message": message}, "continue_on_error": True} for n in cfg.notify]
    return out


def log(cfg: Config, message: str) -> dict:
    return {"action": "logbook.log", "data": {"name": cfg.name + " power", "message": message}}


def domain(e: str) -> str:
    return "homeassistant" if e.split(".")[0] in ("input_boolean",) else e.split(".")[0]


def turn(state: str, entities: list, gap_s: int) -> list:
    seq = []
    for e in entities:
        seq += [{"action": f"{domain(e)}.turn_{state}", "target": {"entity_id": e}, "continue_on_error": True},
                {"delay": {"seconds": gap_s}}]
    return seq


def turn_on_verified(cfg: Config, sw) -> list:
    """turn_on, wait up to verify_wait_s for 'on', re-send up to verify_tries times."""
    e = sw.entity
    seq = []
    if sw.delay_before_s:
        seq.append({"delay": {"seconds": sw.delay_before_s}})
    if sw.wait_online_s:
        seq.append({"wait_template": "{{ states('" + e + "') not in ['unavailable', 'unknown'] }}",
                    "timeout": {"seconds": sw.wait_online_s}, "continue_on_timeout": True})
    seq.append({"repeat": {
        "sequence": [
            {"if": [{"condition": "template", "value_template": "{{ repeat.index > 1 }}"}],
             "then": [log(cfg, sw.name + " not on, retry {{ repeat.index }}")]},
            {"action": f"{domain(e)}.turn_on", "target": {"entity_id": e}, "continue_on_error": True},
            {"wait_template": "{{ is_state('" + e + "', 'on') }}",
             "timeout": {"seconds": cfg.verify_wait_s}, "continue_on_timeout": True}],
        "until": [{"condition": "template", "value_template":
                   "{{ is_state('" + e + "', 'on') or repeat.index >= " + str(cfg.verify_tries) + " }}"}]}})
    seq.append({"delay": {"seconds": cfg.gap_s}})
    return seq


def names_not(cfg: Config, state: str, switches: list) -> str:
    """Template -> list of names of `switches` NOT in `state`."""
    pairs = json.dumps({s.entity: s.name for s in switches})
    return ("{% set ns = namespace(b=[]) %}{% for e, name in (" + pairs + ").items() %}"
            "{% if not is_state(e, '" + state + "') %}{% set ns.b = ns.b + [name] %}{% endif %}{% endfor %}"
            "{{ ns.b }}")


def confirm_template(cfg: Config) -> str | None:
    c = cfg.projectors.confirm
    if not c:
        return None
    return "{{ " + " and ".join("is_state('%s', '%s')" % (x["entity"], x["off_state"]) for x in c) + " }}"


def rain_template(cfg: Config) -> str:
    """Highest chance of precipitation in the next window_hours, from the weather.get_forecasts response.
    'unknown' if the forecast couldn't be read (the show then runs and you're told)."""
    w, hrs = cfg.rain.weather_entity, cfg.rain.window_hours
    return ("{% set f = (fc[" + json.dumps(w) + "].forecast if fc is defined and fc and " + json.dumps(w)
            + " in fc else none) %}"
            "{% if f is none %}unknown{% else %}"
            "{% set end = now() + timedelta(hours=" + str(hrs) + ") %}"
            "{% set ns = namespace(p=[]) %}"
            "{% for h in f %}{% set t = as_datetime(h.datetime) %}"
            "{% if t is not none and t <= end and h.precipitation_probability is not none %}"
            "{% set ns.p = ns.p + [h.precipitation_probability | int(0)] %}{% endif %}{% endfor %}"
            "{{ (ns.p + [0]) | max if ns.p else 'unknown' }}{% endif %}")


def date_cond(cfg: Config) -> list:
    """In season, not a skip date."""
    s, e = cfg.season_start, cfg.season_end
    if s <= e:
        season = "{{ '" + s + "' <= now().strftime('%m-%d') <= '" + e + "' }}"
    else:   # season crosses New Year (e.g. 11-25 .. 01-06)
        season = "{{ now().strftime('%m-%d') >= '" + s + "' or now().strftime('%m-%d') <= '" + e + "' }}"
    return [{"condition": "template", "value_template": season},
            {"condition": "template", "value_template":
             "{{ now().strftime('%Y-%m-%d') not in states('" + ids(cfg)["skip"] + "') }}"}]


def not_manual(cfg: Config) -> list:
    if not cfg.manual_off_dates:
        return []
    return [{"condition": "template", "value_template":
             "{{ now().strftime('%m-%d') not in " + json.dumps(cfg.manual_off_dates) + " }}"}]


# --------------------------------------------------------------------------- scripts
def script_on(cfg: Config) -> dict:
    I = ids(cfg)
    seq = [{"action": "script.turn_off", "target": {"entity_id": I["off"]}}, log(cfg, "power ON started")]
    order = cfg.by_role("projector") + [s for s in cfg.switches if s.role != "projector"]   # lamps warm up longest
    order = [s for s in order if not s.delay_before_s and not s.wait_online_s] + \
            [s for s in order if s.delay_before_s or s.wait_online_s]                      # delayed ones last
    for s in order:
        seq += turn_on_verified(cfg, s)
    seq += [{"variables": {"bad": names_not(cfg, "on", cfg.switches)}},
            {"if": [{"condition": "template", "value_template": "{{ bad | count > 0 }}"}],
             "then": notify(cfg, cfg.name + ": NOT all on", "Did not come on: {{ bad | join(', ') }}"),
             "else": [log(cfg, "all %d switches on" % len(cfg.switches))]}]
    return {"alias": cfg.name + " power ON", "mode": "single", "icon": "mdi:power-plug", "sequence": seq}


def script_off(cfg: Config) -> dict:
    I = ids(cfg)
    P = cfg.projectors
    sound = [s for s in cfg.by_role("sound", "other") if not s.cut_by]
    lights = [s for s in cfg.by_role("lights") if not s.cut_by]
    proj = [s for s in cfg.by_role("projector") if not s.cut_by]
    seq = [{"action": "script.turn_off", "target": {"entity_id": I["on"]}}, log(cfg, "power OFF started")]
    for e in P.standby:                                            # lamps start cooling now
        act = {"button": "button.press", "input_button": "input_button.press", "script": "script.turn_on"}.get(
            e.split(".")[0], e.split(".")[0] + ".turn_off")
        seq.append({"action": act, "target": {"entity_id": e}, "continue_on_error": True})
    seq += turn("off", [s.entity for s in sound], cfg.gap_s)
    if lights:
        seq.append({"if": [{"condition": "template", "value_template": "{{ not (scheduled | default(false)) }}"}],
                    "then": turn("off", [s.entity for s in lights], cfg.gap_s)})
    seq += [{"variables": {"loud": names_not(cfg, "off", sound)}},
            {"if": [{"condition": "template", "value_template": "{{ loud | count > 0 }}"}],
             "then": notify(cfg, cfg.name + ": sound NOT all off", "Still on: {{ loud | join(', ') }}")}]
    if proj:
        seq += [log(cfg, "projector power off in %d min (lamp cool-down)" % P.cool_down_min),
                {"delay": {"minutes": P.cool_down_min}}]
        conf = confirm_template(cfg)
        if conf:
            seq += [{"wait_template": conf, "timeout": {"minutes": 5}, "continue_on_timeout": True},
                    {"if": [{"condition": "template", "value_template": conf}],
                     "then": turn("off", [s.entity for s in proj], cfg.gap_s),
                     "else": notify(cfg, cfg.name + ": projectors NOT confirmed off",
                                    "Projector power LEFT ON so no lamp is hard-cut. Put the projectors in "
                                    "standby, wait for the fans to stop, then switch the power off.")}]
        else:
            seq += turn("off", [s.entity for s in proj], cfg.gap_s)
        seq += [{"delay": {"seconds": 5}},
                {"variables": {"left": names_not(cfg, "off", proj)}},
                {"if": [{"condition": "template", "value_template": "{{ left | count > 0 }}"}],
                 "then": notify(cfg, cfg.name + ": projector power still on", "Still on: {{ left | join(', ') }}"),
                 "else": [log(cfg, "projector power off")]}]
    fields = {}
    if lights:
        fields["scheduled"] = {"description": "true = nightly schedule: lights stay on until the lights-off time",
                               "default": False}
    return {"alias": cfg.name + " power OFF", "mode": "single", "icon": "mdi:power-plug-off",
            "fields": fields, "sequence": seq}


# --------------------------------------------------------------------------- automations
def automations(cfg: Config) -> list:
    I = ids(cfg)
    s = cfg.slug
    if cfg.on.startswith("sunset"):
        off = cfg.on[len("sunset"):] or "+0"
        mins = int(off)
        trig = {"trigger": "sun", "event": "sunset",
                "offset": "%s00:%02d:00" % ("-" if mins < 0 else "", abs(mins))}
    else:
        trig = {"trigger": "time", "at": cfg.on + ":00"}

    on_actions = []
    if cfg.rain:
        r = cfg.rain
        on_actions += [
            {"action": "weather.get_forecasts", "target": {"entity_id": r.weather_entity},
             "data": {"type": "hourly"}, "response_variable": "fc", "continue_on_error": True},
            {"variables": {"rain_pct": rain_template(cfg)}},
            {"if": [{"condition": "template", "value_template": "{{ rain_pct == 'unknown' }}"}],
             "then": notify(cfg, cfg.name + ": rain check failed",
                            "Couldn't read the forecast from " + r.weather_entity + ". Running the show anyway.")},
            {"if": [{"condition": "template", "value_template":
                     "{{ rain_pct != 'unknown' and rain_pct | int(0) >= " + str(r.threshold) + " }}"},
                    {"condition": "state", "entity_id": I["ignore_rain"], "state": "off"}],
             "then": notify(cfg, cfg.name + ": skipped tonight (rain)",
                            "{{ rain_pct }}% chance of rain in the next " + str(r.window_hours) + " h (limit "
                            + str(r.threshold) + "%). Turn on '" + cfg.name + " ignore rain' to run anyway.")
             + [log(cfg, "skipped: {{ rain_pct }}% chance of rain"), {"stop": "rain"}]},
        ]
    on_actions.append({"action": "script.turn_on", "target": {"entity_id": I["on"]}})

    def sched_off(at: str, days: list) -> dict:
        return {"id": f"{s}_power_off_{days[0]}", "alias": f"{cfg.name} power OFF {at} ({days[0]}-{days[-1]})",
                "triggers": [{"trigger": "time", "at": at + ":00"}],
                "conditions": [{"condition": "time", "weekday": days}] + date_cond(cfg) + not_manual(cfg),
                "actions": [{"action": "script.turn_on", "target": {"entity_id": I["off"]},
                             "data": {"variables": {"scheduled": True}}}],
                "mode": "single"}

    out = [{"id": f"{s}_power_on", "alias": f"{cfg.name} power ON ({cfg.on})",
            "description": "Generated by show-power. Skips skip dates" + (" and rainy nights." if cfg.rain else "."),
            "triggers": [trig], "conditions": date_cond(cfg), "actions": on_actions, "mode": "single"},
           sched_off(cfg.off_weeknight, ["sun", "mon", "tue", "wed", "thu"]),
           sched_off(cfg.off_weekend, ["fri", "sat"])]
    lights = [x.entity for x in cfg.by_role("lights") if not x.cut_by]
    if cfg.lights_off and lights:
        out.append({"id": f"{s}_lights_off", "alias": f"{cfg.name} lights OFF {cfg.lights_off}",
                    "triggers": [{"trigger": "time", "at": cfg.lights_off + ":00"}],
                    "conditions": date_cond(cfg) + not_manual(cfg),
                    "actions": turn("off", lights, cfg.gap_s), "mode": "single"})
    if cfg.rain:
        out.append({"id": f"{s}_ignore_rain_reset", "alias": f"{cfg.name} ignore-rain resets at noon",
                    "triggers": [{"trigger": "time", "at": "12:00:00"}],
                    "actions": [{"action": "input_boolean.turn_off", "target": {"entity_id": I["ignore_rain"]}}],
                    "mode": "single"})
    return out


# --------------------------------------------------------------------------- package
def package(cfg: Config) -> dict:
    I = ids(cfg)
    any_on = " or ".join("is_state('%s', 'on')" % s.entity for s in cfg.switches)
    pkg = {
        "input_text": {I["skip"].split(".")[1]: {
            "name": cfg.name + " skip dates", "max": 255, "icon": "mdi:calendar-remove"}},   # no 'initial': HA then remembers the dates across restarts
        "script": {I["on"].split(".")[1]: script_on(cfg), I["off"].split(".")[1]: script_off(cfg)},
        "automation": automations(cfg),
        "template": [{"switch": [{
            "name": cfg.name + " power", "unique_id": cfg.slug + "_power_switch", "icon": "mdi:power",
            "state": "{{ " + any_on + " }}",
            "turn_on": [{"action": "script.turn_on", "target": {"entity_id": I["on"]}}],
            "turn_off": [{"action": "script.turn_on", "target": {"entity_id": I["off"]}}]}]}],
    }
    if cfg.rain:
        pkg["input_boolean"] = {I["ignore_rain"].split(".")[1]: {
            "name": cfg.name + " ignore rain", "icon": "mdi:weather-pouring"}}
    return pkg


HEADER = """# Generated by show-power from {src}. Edit the config and regenerate rather than editing this file.
# Install: save as <config>/packages/{slug}_power.yaml, make sure configuration.yaml has
#   homeassistant:
#     packages: !include_dir_named packages
# then restart Home Assistant (or check configuration first: Developer tools -> YAML).
#
# What you get:
#   {switch:<36} one button: ON = checked power-up, OFF = safe shutdown
#   {on:<36} staggered ON, every switch verified, re-sent, alert if not on
#   {off:<36} sound off, projectors cool before power is cut
#   {skip:<36} comma-separated YYYY-MM-DD nights to leave alone
"""


def render(cfg: Config, src: str = "config") -> str:
    import yaml

    class Dumper(yaml.SafeDumper):
        pass

    def str_rep(d, data):
        if "\n" in data or len(data) > 90:
            return d.represent_scalar("tag:yaml.org,2002:str", data, style=">" if "\n" not in data else "|")
        return d.represent_scalar("tag:yaml.org,2002:str", data)

    Dumper.add_representer(str, str_rep)
    I = ids(cfg)
    head = HEADER.format(src=src, slug=cfg.slug, **I)
    if cfg.rain:
        head += "#   %-36s run tonight even if rain is forecast (resets at noon)\n" % I["ignore_rain"]
    return head + "\n" + yaml.dump(package(cfg), Dumper=Dumper, sort_keys=False, width=110, allow_unicode=True)
