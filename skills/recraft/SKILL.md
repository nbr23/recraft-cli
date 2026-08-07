---
description: Generate, edit, vectorize, upscale, or remove backgrounds from images using the Recraft AI API. Use when asked to create an image, icon, logo, illustration, or SVG, or to modify an existing image file. Spends real money per call.
---

# recraft

The `recraft` command is on your PATH. It calls the Recraft AI image API, writes the result to a
file, and prints the path.

**`recraft COMMAND --help` is the reference.** Every subcommand documents its flags and shows a
worked example. Read it before guessing — this file deliberately does not repeat the flag list.

```
recraft generate "a red bicycle on a white background" -o bike.png
recraft generate "a minimal bicycle icon" --vector -o bike.svg
recraft remove-bg photo.png -o cutout.png
recraft vectorize logo.png -o logo.svg
recraft balance
```

## Each call costs real money

Typical costs: a V4.1 image ~$0.035, vectorize or remove-bg ~$0.01, `--mode crisp` upscale ~$0.004.
`--mode creative` upscale is ~$0.25 — roughly 60x crisp, so use crisp unless asked otherwise.

- **Don't loop or batch speculatively.** Generate once, show the user, iterate only if asked.
- Use `-n` rather than repeat invocations when several variations are genuinely wanted.
- `--dry-run` prints the exact request and spends nothing. Use it to check a command is right.
- `recraft balance` shows remaining credits. Exit code 3 means the balance is exhausted.

## Reading the output

Every image command prints one line per file: `path  dimensions  model  credits`. `--json` gives a
single JSON object instead.

**The image bytes are never printed and you should not read them back.** Report the path to the
user. Don't open the file with the Read tool to "check" it — that burns a huge amount of context for
no benefit. Trust the printed dimensions and credit count.

## Other things worth knowing

- It refuses to overwrite an existing file. Pass `--force` deliberately, or write to a new path;
  a plain retry will not clobber a good asset.
- Styles are a large, changing list. Run `recraft styles` to see valid values instead of guessing;
  `recraft models` lists models and prices.
- The STYLE column in `recraft styles` (e.g. "Pixel art") is a preset name, not a valid `--style`
  value — passing it directly gets a 400 `invalid_request_parameter`. To use a preset, pass its
  STYLE_ID column value via `--style-id <UUID>`. `--style` itself only accepts the small set of
  base categories (`realistic_image`, `digital_illustration`, `icon`, etc.).
- When passing `--style-id`, also pass `--model` matching that row's MODEL column. The CLI's
  default model won't necessarily match the style's model, and a mismatch fails with
  `invalid_model: requested model '...' doesn't match the style's model '...'`.
- Exit codes: `0` ok, `1` API/network error, `2` bad arguments, `3` out of credits.
- Billing is already handled correctly and is not configurable. Nothing to set up per call.
- It needs `RECRAFT_API_TOKEN` set, or a token at `~/.config/recraft/token`. If it exits 2 saying
  the token is missing, tell the user rather than trying to work around it.
