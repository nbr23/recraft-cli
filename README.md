# recraft-cli

CLI for the [Recraft AI](https://www.recraft.ai) image API, built to be driven by an LLM coding
agent. Writes image files to disk and prints their paths — image bytes never touch stdout.

Spends **API credits**, never subscription credits.

> Unofficial. Not affiliated with, endorsed by, or supported by Recraft AI.

## Install

Requires [uv](https://docs.astral.sh/uv/).

As a Claude Code plugin — adds `recraft` to the Bash tool's `PATH`, plus a skill telling Claude when
to use it:

```
/plugin marketplace add nbr23/recraft-cli
/plugin install recraft@nbr23
```

Then allow it once in `.claude/settings.json` to stop the per-call permission prompt:

```json
{ "permissions": { "allow": ["Bash(recraft:*)"] } }
```

As a standalone CLI:

```sh
uv tool install nbr23-recraft   # or: uvx nbr23-recraft --help
```

## Token

From `RECRAFT_API_TOKEN`, falling back to `~/.config/recraft/token` (mode 0600). Get one from the
[Recraft API console](https://www.recraft.ai/profile/api).

```sh
export RECRAFT_API_TOKEN='...'
```

## Examples

```sh
recraft balance
# 12345 API credits ($12.345)

recraft generate "a red bicycle on a white background" -o bike.png
# /abs/path/bike.png  1024x1024  recraftv4_1  35 credits ($0.035)

recraft generate "a minimal bicycle icon" --vector -o bike.svg
recraft vectorize bike.png -o bike.svg              # ~10 credits
recraft remove-bg bike.png -o bike-cutout.png       # ~10 credits
recraft upscale bike.png --mode crisp -o bike-2x.png   # ~4 credits; creative is ~250

recraft generate "a red bicycle" --dry-run          # print the request, spend nothing
recraft generate "a red bicycle" -o bike.png --json # one JSON object on stdout

recraft models      # ids and per-image price
recraft styles      # valid style / model combinations
```

`recraft --help` lists every command; `recraft COMMAND --help` documents its flags with an example.
