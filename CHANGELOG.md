# Changelog

## 1.0.0 (2026-11-12)

First stable release.

- `showpower check`: validates a yard config and prints the plan in plain words.
- `showpower build`: writes a Home Assistant package with one power button, a checked power-up
  script, a safe shutdown script, the schedule automations, and helpers.
- Checked power-up: switches come on one at a time; each must report "on", is re-sent if not,
  and any that still aren't on are named in a notification.
- Safe shutdown: sound off first, lights optionally later, projectors to standby, a cool-down,
  then projector power cut only if a confirm sensor agrees (otherwise left on, with a notification).
- Chained and delayed power: plugs that boot OFF behind another switch are waited for and turned
  on; they're never switched off directly.
- Rain skip from the forecast (`weather.get_forecasts`), with an "ignore rain" toggle that resets
  at noon; if the forecast can't be read, the show runs and you're told.
- Skip dates that survive Home Assistant restarts, seasons that cross New Year, and hands-off
  dates the schedule never turns off.
- Tests for config validation, step order, the projector gate, and the generated templates
  (run through a Home Assistant-style Jinja environment), on every push (Python 3.10 and 3.12).

Not yet tested against a running Home Assistant instance; please open an issue if anything in a
generated package doesn't load.
