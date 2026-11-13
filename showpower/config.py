"""Load and check a show-power config file (YAML). See examples/config.example.yaml."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

ROLES = ("projector", "sound", "lights", "other")
ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MMDD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
SUN = re.compile(r"^sunset([+-]\d{1,3})?$")


class ConfigError(ValueError):
    pass


@dataclass
class Switch:
    entity: str
    name: str
    role: str = "other"
    delay_before_s: int = 0        # wait this long before turning it on (a load that must come up last)
    wait_online_s: int = 0         # wait for it to come online first (a smart plug powered by another switch)
    cut_by: str | None = None      # another switch that cuts its power: never switched off directly


@dataclass
class Projectors:
    standby: list = field(default_factory=list)    # switch/button/script entities that put lamps in standby
    confirm: list = field(default_factory=list)    # [{entity, off_state}] that prove a lamp is off
    cool_down_min: int = 15                         # wait after standby before cutting projector power


@dataclass
class Rain:
    weather_entity: str
    threshold: int = 50            # % chance of precipitation that skips the night
    window_hours: int = 4          # look this far ahead from the ON time


@dataclass
class Config:
    name: str
    switches: list
    on: str = "sunset-15"
    off_weeknight: str = "20:30"
    off_weekend: str = "21:30"
    lights_off: str | None = None
    season_start: str = "10-01"
    season_end: str = "10-31"
    manual_off_dates: list = field(default_factory=list)   # e.g. ["10-31"]: the schedule never turns these off
    notify: list = field(default_factory=list)              # notify.* services (persistent notification is always on)
    verify_tries: int = 3
    verify_wait_s: int = 10
    gap_s: int = 3
    projectors: Projectors = field(default_factory=Projectors)
    rain: Rain | None = None

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_")

    def by_role(self, *roles) -> list:
        return [s for s in self.switches if s.role in roles]


def _req(d: dict, key: str, where: str):
    if key not in d:
        raise ConfigError(f"{where}: missing '{key}'")
    return d[key]


def _entity(e: str, where: str, domains=None) -> str:
    if not isinstance(e, str) or not ENTITY.match(e):
        raise ConfigError(f"{where}: '{e}' is not an entity id like switch.porch_lights")
    if domains and e.split(".")[0] not in domains:
        raise ConfigError(f"{where}: '{e}' must be one of {', '.join(domains)}")
    return e


def parse(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping")
    name = str(_req(raw, "name", "config"))
    sw = []
    seen = set()
    for i, s in enumerate(_req(raw, "switches", "config") or []):
        where = f"switches[{i}]"
        e = _entity(_req(s, "entity", where), where, ("switch", "light", "input_boolean"))
        if e in seen:
            raise ConfigError(f"{where}: {e} listed twice")
        seen.add(e)
        role = s.get("role", "other")
        if role not in ROLES:
            raise ConfigError(f"{where}: role must be one of {', '.join(ROLES)}")
        sw.append(Switch(e, str(s.get("name", e)), role, int(s.get("delay_before_s", 0)),
                         int(s.get("wait_online_s", 0)), s.get("cut_by")))
    if not sw:
        raise ConfigError("config: at least one switch is needed")
    for s in sw:
        if s.cut_by and s.cut_by not in seen:
            raise ConfigError(f"{s.entity}: cut_by '{s.cut_by}' is not one of the listed switches")

    sched = raw.get("schedule", {}) or {}
    on = str(sched.get("on", "sunset-15"))
    if not (SUN.match(on) or HHMM.match(on)):
        raise ConfigError("schedule.on must be HH:MM or sunset / sunset-15 / sunset+10")
    times = {}
    for k, d in (("off_weeknight", "20:30"), ("off_weekend", "21:30"), ("lights_off", None)):
        v = sched.get(k, d)
        if v is not None and not HHMM.match(str(v)):
            raise ConfigError(f"schedule.{k} must be HH:MM")
        times[k] = None if v is None else str(v)
    season = sched.get("season", {}) or {}
    ss, se = str(season.get("start", "10-01")), str(season.get("end", "10-31"))
    for v in (ss, se):
        if not MMDD.match(v):
            raise ConfigError("schedule.season dates must be MM-DD")
    manual = [str(d) for d in sched.get("manual_off_dates", []) or []]
    for d in manual:
        if not MMDD.match(d):
            raise ConfigError("schedule.manual_off_dates must be MM-DD")
    if times["lights_off"] and not [s for s in sw if s.role == "lights"]:
        raise ConfigError("schedule.lights_off is set but no switch has role: lights")

    p = raw.get("projectors", {}) or {}
    proj = Projectors(
        standby=[_entity(e, "projectors.standby", ("switch", "button", "script", "input_button")) for e in p.get("standby", []) or []],
        confirm=[{"entity": _entity(_req(c, "entity", "projectors.confirm"), "projectors.confirm"),
                  "off_state": str(c.get("off_state", "off"))} for c in p.get("confirm", []) or []],
        cool_down_min=int(p.get("cool_down_min", 15)),
    )
    if (proj.standby or proj.confirm) and not [s for s in sw if s.role == "projector"]:
        raise ConfigError("projectors: settings given but no switch has role: projector")

    r = raw.get("rain")
    rain = None
    if r:
        rain = Rain(_entity(_req(r, "weather_entity", "rain"), "rain", ("weather",)),
                    int(r.get("threshold", 50)), int(r.get("window_hours", 4)))
        if not 0 < rain.threshold <= 100:
            raise ConfigError("rain.threshold must be 1-100 (percent)")

    v = raw.get("verify", {}) or {}
    notify = [_entity(n, "notify", ("notify",)) for n in raw.get("notify", []) or []]
    cfg = Config(name=name, switches=sw, on=on, off_weeknight=times["off_weeknight"],
                 off_weekend=times["off_weekend"], lights_off=times["lights_off"], season_start=ss,
                 season_end=se, manual_off_dates=manual, notify=notify,
                 verify_tries=int(v.get("tries", 3)), verify_wait_s=int(v.get("wait_s", 10)),
                 gap_s=int(v.get("gap_s", 3)), projectors=proj, rain=rain)
    if not cfg.slug:
        raise ConfigError("name must contain letters or numbers")
    return cfg


def load(path: str) -> Config:
    with open(path) as f:
        return parse(yaml.safe_load(f))
