# Contributing to FritzTrack4U

Thanks for your interest! This is a small, honest hobby project — contributions, ideas and bug reports are welcome.

## Quick dev setup

```bash
git clone https://github.com/Aslan4u73/FritzTrack4u.git
cd FritzTrack4u
pip install -r requirements.txt        # only needed for MQTT
cp config.example.json config.json     # fill in at least one box
python3 fritztrack4u.py --config ./config.json --once   # single test cycle
```

**You only need one FritzBox to test** — floor-level detection and presence work with a single box. Multi-box room detection needs 2+ boxes with their own TR-064 user.

## What's helpful

- **Bug reports** with the output of `--once` and your (anonymized) config.
- **New box/router support** — keep the TR-064 logic generic.
- **Fingerprint / matching improvements** — the core is `PositionEngine` in `fritztrack4u.py`.
- **Translations** of the docs.
- **A working demo screenshot or GIF** — the single most useful thing for the project.

## Ground rules

- Keep it **simple**: one file, one optional dependency. Don't add a framework.
- **Never commit secrets** — real passwords, IPs or MACs. `config.json`, `*.db` and fingerprints are gitignored; keep it that way.
- Anything that changes the `config.json` shape → **open an issue first**, so docs and code stay in sync.
- Be honest about accuracy. This is room-level, not centimeter-level. Don't oversell it.

## Honest status

FritzTrack4U reliably detects **floor + presence** today. **Room-level** accuracy depends on calibration and on whether a phone is seen by more than one box (Wi-Fi physics — a phone usually talks to only one box). It's a learning/DIY project, contributions to make it more accurate are very welcome.
