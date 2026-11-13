"""show-power: generate a Home Assistant package that runs outdoor display power safely.

    showpower check config.yaml                 # validate, print the plan
    showpower build config.yaml -o show_power.yaml
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .config import ConfigError, load
from .generate import ids, render


def plan(cfg) -> str:
    I = ids(cfg)
    L = [f"{cfg.name}: {len(cfg.switches)} switches, season {cfg.season_start} to {cfg.season_end}"]
    L.append(f"  ON   {cfg.on}" + (f" unless >= {cfg.rain.threshold}% rain in the next {cfg.rain.window_hours} h "
                                    f"({cfg.rain.weather_entity})" if cfg.rain else ""))
    L.append(f"  OFF  {cfg.off_weeknight} Sun-Thu, {cfg.off_weekend} Fri-Sat"
             + (f", never on {', '.join(cfg.manual_off_dates)}" if cfg.manual_off_dates else ""))
    if cfg.lights_off:
        L.append(f"  lights stay on until {cfg.lights_off}")
    if cfg.by_role("projector"):
        P = cfg.projectors
        L.append(f"  projectors: standby {len(P.standby)} device(s), cool {P.cool_down_min} min, "
                 + ("cut only when confirmed off" if P.confirm else "then cut (no confirm sensor)"))
    L.append("  power-on order:")
    for s in cfg.by_role("projector") + [x for x in cfg.switches if x.role != "projector"]:
        extra = []
        if s.delay_before_s:
            extra.append(f"+{s.delay_before_s}s delay")
        if s.wait_online_s:
            extra.append(f"waits {s.wait_online_s}s to come online")
        if s.cut_by:
            extra.append(f"powered via {s.cut_by}")
        L.append(f"    {s.role:<9} {s.name}" + (f"  ({', '.join(extra)})" if extra else ""))
    L.append(f"  button: {I['switch']}   skip dates: {I['skip']}")
    return "\n".join(L)


def main(argv=None):
    p = argparse.ArgumentParser(prog="showpower", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version="show-power " + __version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate a config and print the plan")
    c.add_argument("config")
    b = sub.add_parser("build", help="write the Home Assistant package")
    b.add_argument("config")
    b.add_argument("-o", "--out", help="output file (default: <name>_power.yaml)")
    a = p.parse_args(argv)
    try:
        cfg = load(a.config)
    except (ConfigError, OSError) as e:
        sys.exit(f"error: {e}")
    print(plan(cfg))
    if a.cmd == "build":
        out = a.out or f"{cfg.slug}_power.yaml"
        with open(out, "w") as f:
            f.write(render(cfg, os.path.basename(a.config)))
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
