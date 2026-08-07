#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.27",
# ]
# ///
"""Command line client for the Recraft AI image API."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import stat
import string
import sys
import time
from pathlib import Path
from typing import Any

import httpx

PROG = "recraft"
BASE_URL = "https://external.api.recraft.ai"
CREDITS_PER_USD = 1000
MAX_ATTEMPTS = 4
# Published limits are ~5 req/s and ~100 images/min; stay under the request ceiling.
MIN_REQUEST_INTERVAL = 0.21
CACHE_TTL = 24 * 3600

# Fallback for --dry-run estimates only. Real charges come from the response's
# `credits` field, which is what every command reports.
PRICE_ESTIMATES = {
    "recraftv4_1": 35,
    "recraftv4_1_pro": 210,
    "recraftv4": 40,
    "recraftv4_pro": 250,
    "recraftv3": 40,
    "recraftv2": 40,
    "recraftv4_1_vector": 80,
    "recraftv4_vector": 80,
    "recraftv3_vector": 80,
    "recraftv2_vector": 80,
    "recraftv4_1_pro_vector": 300,
    "recraftv4_pro_vector": 300,
}
OP_ESTIMATES = {
    "imageToImage": 40,
    "inpaint": 40,
    "outpaint": 40,
    "variateImage": 40,
    "replaceBackground": 80,
    "generateBackground": 80,
    "eraseRegion": 10,
    "removeBackground": 10,
    "vectorize": 10,
    "crispUpscale": 4,
    "creativeUpscale": 250,
    "clarityUpscale": 250,
    "generativeUpscale": 250,
}

MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".svg": "image/svg+xml",
}


class UsageError(Exception):
    """Bad arguments or a refused local action. Exit 2."""


class ApiError(Exception):
    """API or network failure. Exit 1."""


class CreditsError(Exception):
    """API credit pool exhausted. Exit 3."""


class DryRunComplete(Exception):
    """Raised instead of issuing a billable request under --dry-run."""


def warn(message: str) -> None:
    print(f"{PROG}: {message}", file=sys.stderr)


class Context:
    """Global flags plus the shared HTTP client."""

    def __init__(self, args: argparse.Namespace):
        self.json_out: bool = getattr(args, "json_out", False)
        self.verbose: bool = getattr(args, "verbose", False)
        self.timeout: float = getattr(args, "timeout", 180.0)
        self.force: bool = getattr(args, "force", False)
        self.dry_run: bool = getattr(args, "dry_run", False)
        self._token: str | None = None
        self._client: httpx.Client | None = None
        self._last_request = 0.0

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = load_token()
        return self._token

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        return self._client

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"{PROG}: {message}", file=sys.stderr)

    def throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if 0 < elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


def load_token() -> str:
    token = os.environ.get("RECRAFT_API_TOKEN", "").strip()
    if token:
        return token

    path = Path.home() / ".config" / "recraft" / "token"
    if path.is_file():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            warn(f"{path} is mode {mode:04o}; tighten it with: chmod 600 {path}")
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token

    raise UsageError(
        "no API token. Set RECRAFT_API_TOKEN in the environment, or write the "
        f"token to {path} (mode 0600)."
    )


def api_request(
    ctx: Context,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    files: list[tuple[str, Path]] | None = None,
    billable: bool = True,
) -> Any:
    """Issue one API call, retrying 429/5xx. Billable calls always bill `api`.

    The billing query parameter is the whole reason this tool exists: Recraft
    draws from two disjoint pools, and anything but `api` spends subscription
    credits this account does not have. It is never configurable.
    """
    url = f"{BASE_URL}{path}"
    params = {"billing": "api"} if billable else None

    if ctx.dry_run and billable:
        emit_dry_run(ctx, method, url, params, json_body, form, files)
        raise DryRunComplete

    file_parts = None
    if files:
        file_parts = []
        for field, file_path in files:
            data = read_image(file_path)
            mime = MIME_TYPES.get(file_path.suffix.lower(), "application/octet-stream")
            # Bytes rather than handles, so a retry can resend the same body.
            file_parts.append((field, (file_path.name, data, mime)))

    headers = {"Authorization": f"Bearer {ctx.token}"}
    display_url = url + ("?billing=api" if params else "")

    last_error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        ctx.throttle()
        ctx.log(f"--> {method} {display_url} (attempt {attempt}/{MAX_ATTEMPTS})")
        if json_body is not None:
            ctx.log(f"    body {json.dumps(json_body, sort_keys=True)}")
        elif form is not None:
            ctx.log(f"    form {json.dumps(redact_form(form, files), sort_keys=True)}")

        started = time.monotonic()
        try:
            response = ctx.client.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json_body,
                data=form,
                files=file_parts,
            )
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            ctx.log(f"<-- transport error: {last_error}")
            if attempt == MAX_ATTEMPTS:
                raise ApiError(f"request to {display_url} failed: {last_error}") from exc
            sleep_for_retry(ctx, attempt, None)
            continue

        elapsed = time.monotonic() - started
        ctx.log(f"<-- {response.status_code} in {elapsed:.2f}s")

        if response.status_code < 400:
            if ctx.verbose:
                ctx.log(f"    {summarize_body(response.text)}")
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise ApiError(
                    f"{display_url} returned non-JSON body ({response.status_code})"
                ) from exc

        retryable = response.status_code == 429 or response.status_code >= 500
        if retryable and attempt < MAX_ATTEMPTS:
            sleep_for_retry(ctx, attempt, response.headers.get("Retry-After"))
            continue

        raise_api_error(response, display_url)

    raise ApiError(f"request to {display_url} failed: {last_error}")


def sleep_for_retry(ctx: Context, attempt: int, retry_after: str | None) -> None:
    delay = None
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            delay = None
    if delay is None:
        delay = 2.0 ** (attempt - 1)
    delay = min(delay, 60.0) + random.uniform(0, 0.5)
    ctx.log(f"    retrying in {delay:.1f}s")
    time.sleep(delay)


def raise_api_error(response: httpx.Response, display_url: str) -> None:
    code, message = parse_api_error(response)
    status = response.status_code

    if code == "not_enough_credits" or (status == 402 and not code):
        raise CreditsError(
            "out of API credits. This is your API credit balance, not a "
            "subscription plan limit - Recraft bills the two pools separately "
            "and this tool only ever spends API credits. "
            f"Check the balance with `{PROG} balance`."
            + (f" (server: {message})" if message else "")
        )

    if status in (401, 403):
        raise ApiError(
            f"authentication failed ({status}) for {display_url}. Check the token "
            "in RECRAFT_API_TOKEN or ~/.config/recraft/token."
            + (f" (server: {message})" if message else "")
        )

    hints = {
        "invalid_model": f"run `{PROG} models` for the list of available models.",
        "deprecated_model": f"run `{PROG} models` and pick a current model.",
        "prompt_is_improper": "the prompt was rejected by content moderation.",
        "invalid_prompt": "the prompt was rejected by content moderation.",
        "rate_limit_exceeded": f"rate limited after {MAX_ATTEMPTS} attempts; retry later.",
        "image_too_big": "downscale the input image and retry.",
        "invalid_image_format": "supported inputs are png, jpeg and webp.",
        "style_is_private": f"run `{PROG} my-styles` to list styles you own.",
    }

    parts = [f"API error {status}"]
    if code:
        parts.append(f"[{code}]")
    parts.append(message or response.text.strip()[:400] or "(empty response body)")
    text = " ".join(parts)
    if code in hints:
        text += f"\n{hints[code]}"
    raise ApiError(text)


def parse_api_error(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    error = payload.get("error")
    if isinstance(error, dict):
        payload = error
    elif isinstance(error, str) and "message" not in payload:
        return str(payload.get("code", "")), error
    code = payload.get("code") or payload.get("error_code") or ""
    message = payload.get("message") or payload.get("detail") or ""
    return str(code), str(message)


def summarize_body(text: str) -> str:
    """Log response bodies without ever spilling base64 payloads."""
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:400]
    scrubbed = scrub_b64(payload)
    dumped = json.dumps(scrubbed, sort_keys=True)
    return dumped[:800] + ("..." if len(dumped) > 800 else "")


def scrub_b64(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: (f"<{len(value)} bytes elided>" if key == "b64_json" and isinstance(value, str) else scrub_b64(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [scrub_b64(item) for item in payload]
    return payload


def redact_form(form: dict[str, Any], files: list[tuple[str, Path]] | None) -> dict[str, Any]:
    shown = dict(form)
    for field, file_path in files or []:
        shown[field] = f"<file {file_path}>"
    return shown


def emit_dry_run(
    ctx: Context,
    method: str,
    url: str,
    params: dict[str, str] | None,
    json_body: dict[str, Any] | None,
    form: dict[str, Any] | None,
    files: list[tuple[str, Path]] | None,
) -> None:
    full_url = url + ("?billing=api" if params else "")
    if json_body is not None:
        body: Any = json_body
        content_type = "application/json"
    else:
        body = redact_form(form or {}, files)
        content_type = "multipart/form-data"

    if ctx.json_out:
        print(json.dumps(
            {
                "dry_run": True,
                "method": method,
                "url": full_url,
                "content_type": content_type,
                "body": body,
            },
            indent=2,
            sort_keys=True,
        ))
    else:
        print(f"{method} {full_url}")
        print(f"content-type: {content_type}")
        print(json.dumps(body, indent=2, sort_keys=True))
    warn("dry run: no request sent, no credits spent")


def read_image(path: Path) -> bytes:
    if not path.is_file():
        raise UsageError(f"input image not found: {path}")
    return path.read_bytes()


def download(ctx: Context, url: str) -> bytes:
    """Fetch a generated asset. Recraft's URLs are short-lived, so callers must
    do this in the same process, immediately after generation."""
    ctx.log(f"--> GET {url.split('?', 1)[0]} (asset download)")
    response = ctx.client.get(url)
    response.raise_for_status()
    ctx.log(f"<-- {response.status_code}, {len(response.content)} bytes")
    return response.content


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > limit:
        slug = slug[:limit].rstrip("-")
    return slug or "image"


def short_id(image_id: str | None) -> str:
    if image_id:
        return image_id.replace("-", "")[:8]
    return secrets.token_hex(4)


def resolve_paths(
    output: str | None,
    count: int,
    ext: str,
    slug: str,
    ids: list[str | None] | None = None,
) -> list[Path]:
    """Map -o plus artifact count onto concrete absolute paths."""
    ids = ids or [None] * count
    if output:
        target = Path(output).expanduser()
        is_dir = target.is_dir() or output.endswith(os.sep)
        if is_dir:
            return [
                (target / f"{slug}-{short_id(ids[i])}{ext}").resolve()
                for i in range(count)
            ]
        if count == 1:
            return [target.resolve()]
        stem = target.stem
        parent = target.parent
        suffix = target.suffix or ext
        return [(parent / f"{stem}-{i + 1}{suffix}").resolve() for i in range(count)]
    return [
        (Path.cwd() / f"{slug}-{short_id(ids[i])}{ext}").resolve() for i in range(count)
    ]


def preflight_paths(ctx: Context, output: str | None, count: int, ext: str) -> None:
    """Reject an existing target before spending credits, not after."""
    if not output:
        return
    target = Path(output).expanduser()
    if target.is_dir() or output.endswith(os.sep):
        return
    for path in resolve_paths(output, count, ext, "preflight"):
        check_writable(ctx, path)


def check_writable(ctx: Context, path: Path) -> None:
    if path.exists() and not ctx.force:
        raise UsageError(f"{path} exists; pass --force to overwrite")
    if not path.parent.is_dir():
        raise UsageError(f"output directory does not exist: {path.parent}")


def write_asset(ctx: Context, path: Path, data: bytes) -> None:
    check_writable(ctx, path)
    path.write_bytes(data)


def image_dims(data: bytes) -> str:
    """Best-effort dimensions for the summary line, without a codec dependency."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return f"{int.from_bytes(data[16:20], 'big')}x{int.from_bytes(data[20:24], 'big')}"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                width = int.from_bytes(data[24:27], "little") + 1
                height = int.from_bytes(data[27:30], "little") + 1
                return f"{width}x{height}"
            if chunk == b"VP8 ":
                width = int.from_bytes(data[26:28], "little") & 0x3FFF
                height = int.from_bytes(data[28:30], "little") & 0x3FFF
                return f"{width}x{height}"
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return f"{(bits & 0x3FFF) + 1}x{((bits >> 14) & 0x3FFF) + 1}"
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = int.from_bytes(data[i + 2:i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height = int.from_bytes(data[i + 5:i + 7], "big")
                    width = int.from_bytes(data[i + 7:i + 9], "big")
                    return f"{width}x{height}"
                i += 2 + length
        head = data[:2048].decode("utf-8", "ignore")
        if "<svg" in head:
            width = re.search(r'\bwidth="([\d.]+)', head)
            height = re.search(r'\bheight="([\d.]+)', head)
            if width and height:
                return f"{int(float(width.group(1)))}x{int(float(height.group(1)))}"
            box = re.search(r'viewBox="[\d.\-]+ [\d.\-]+ ([\d.]+) ([\d.]+)', head)
            if box:
                return f"{int(float(box.group(1)))}x{int(float(box.group(2)))}"
    except (ValueError, IndexError):
        pass
    return "-"


def usd(credits: int) -> float:
    return round(credits / CREDITS_PER_USD, 6)


def resolve_format(args: argparse.Namespace) -> str:
    """Decide the raster format: explicit --format, else the -o extension, else png.

    The API defaults image_format to webp, so this is always sent explicitly. Left
    unset, the server returns webp while the filename derived here says png, and
    the extension silently disagrees with the bytes.
    """
    if getattr(args, "format", None):
        return args.format
    output = getattr(args, "output", None)
    if output:
        suffix = Path(output).suffix.lower()
        if suffix in (".png", ".webp"):
            return suffix[1:]
    return "png"


def detect_format(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if "<svg" in data[:2048].decode("utf-8", "ignore"):
        return "svg"
    return None


def emit_artifacts(
    ctx: Context,
    command: str,
    model: str | None,
    credits: int,
    artifacts: list[dict[str, Any]],
) -> None:
    """One terse line per artifact, or a single JSON object. Never image bytes."""
    if ctx.json_out:
        print(json.dumps(
            {
                "command": command,
                "model": model,
                "credits": credits,
                "usd": usd(credits),
                "files": artifacts,
            },
            sort_keys=True,
        ))
        return

    # `credits` is the charge for the whole call; label it when it covers
    # more than one file so the column is never mistaken for a per-file price.
    suffix = " total" if len(artifacts) > 1 else ""
    for artifact in artifacts:
        print(
            f"{artifact['path']}  {artifact['size']}  {model or '-'}  "
            f"{credits} credits (${usd(credits):.3f}){suffix}"
        )


def save_results(
    ctx: Context,
    command: str,
    payload: dict[str, Any],
    output: str | None,
    ext: str,
    slug: str,
    model: str | None,
) -> None:
    """Download every image in a response, write it, and print the summary."""
    if "data" in payload:
        images = payload.get("data") or []
    elif "image" in payload:
        images = [payload["image"]]
    else:
        raise ApiError(f"unexpected response shape: {list(payload)[:8]}")

    credits = int(payload.get("credits", 0))
    ids = [image.get("image_id") for image in images]
    paths = resolve_paths(output, len(images), ext, slug, ids)

    artifacts = []
    failures = []
    for image, path in zip(images, paths, strict=True):
        url = image.get("url")
        if not url:
            failures.append("response contained no url (b64 is never requested)")
            continue
        try:
            data = download(ctx, url)
            write_asset(ctx, path, data)
        except UsageError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            # The URL expires shortly; surface it so the asset is recoverable.
            warn(f"failed to save {path}: {exc}")
            warn(f"asset URL (expires soon): {url}")
            failures.append(str(exc))
            continue
        actual = detect_format(data)
        expected = path.suffix.lower().lstrip(".")
        if actual and expected and actual != expected and {actual, expected} != {"jpeg", "jpg"}:
            warn(f"{path.name}: contents are {actual}, not {expected}")

        artifact = {
            "path": str(path),
            "size": image_dims(data),
            "bytes": len(data),
            "image_id": image.get("image_id"),
        }
        if image.get("revised_prompt"):
            artifact["revised_prompt"] = image["revised_prompt"]
        artifacts.append(artifact)

    if not artifacts:
        raise ApiError("no artifacts saved: " + "; ".join(failures or ["empty response"]))

    emit_artifacts(ctx, command, model, credits, artifacts)
    if failures:
        raise ApiError(f"{len(failures)} of {len(images)} artifacts could not be saved")


def hex_to_rgb(value: str) -> list[int]:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6 or any(char not in string.hexdigits for char in text):
        raise UsageError(f"invalid hex color: {value!r} (expected #rgb or #rrggbb)")
    return [int(text[i:i + 2], 16) for i in (0, 2, 4)]


def build_controls(args: argparse.Namespace) -> dict[str, Any] | None:
    controls: dict[str, Any] = {}
    if getattr(args, "artistic_level", None) is not None:
        controls["artistic_level"] = args.artistic_level
    if getattr(args, "no_text", False):
        controls["no_text"] = True
    if getattr(args, "colors", None):
        controls["colors"] = [
            {"rgb": hex_to_rgb(color)} for color in args.colors.split(",") if color.strip()
        ]
    if getattr(args, "background_color", None):
        controls["background_color"] = {"rgb": hex_to_rgb(args.background_color)}
    if getattr(args, "controls", None):
        try:
            extra = json.loads(args.controls)
        except ValueError as exc:
            raise UsageError(f"--controls is not valid JSON: {exc}") from None
        if not isinstance(extra, dict):
            raise UsageError("--controls must be a JSON object")
        controls.update(extra)
    return controls or None


def styling_body(args: argparse.Namespace) -> dict[str, Any]:
    """Fields shared by generation and the transform endpoints. Unset stays absent."""
    if getattr(args, "style", None) and getattr(args, "style_id", None):
        raise UsageError("--style and --style-id are mutually exclusive")

    body: dict[str, Any] = {"response_format": "url"}
    for flag, field in (
        ("model", "model"),
        ("style", "style"),
        ("substyle", "substyle"),
        ("style_id", "style_id"),
        ("size", "size"),
        ("negative_prompt", "negative_prompt"),
        ("creativity", "creativity"),
        ("format", "image_format"),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            body[field] = value
    if getattr(args, "seed", None) is not None:
        body["random_seed"] = args.seed
    if getattr(args, "n", None) is not None and args.n != 1:
        body["n"] = args.n
    controls = build_controls(args)
    if controls:
        body["controls"] = controls
    return body


def form_fields(body: dict[str, Any]) -> dict[str, str]:
    """Flatten a JSON body for multipart transport."""
    fields = {}
    for key, value in body.items():
        if isinstance(value, bool):
            fields[key] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            fields[key] = json.dumps(value)
        else:
            fields[key] = str(value)
    return fields


def image_transport(
    args: argparse.Namespace,
    body: dict[str, Any],
    fields: tuple[str, ...] = ("image",),
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[tuple[str, Path]] | None]:
    """Choose JSON+URL or multipart+upload, per the API's dual input transport.

    Returns (json_body, form, files) with exactly one transport populated.
    """
    urls = {
        "image": getattr(args, "image_url", None),
        "mask": getattr(args, "mask_url", None),
    }
    paths = {
        "image": getattr(args, "image", None),
        "mask": getattr(args, "mask", None),
    }

    using_urls = any(urls[field] for field in fields)
    using_paths = any(paths[field] for field in fields)
    if using_urls and using_paths:
        raise UsageError("pass either a local image path or --image-url, not both")
    if not using_urls and not using_paths:
        raise UsageError("an input image is required (local path or --image-url)")

    if using_urls:
        for field in fields:
            if not urls[field]:
                raise UsageError(f"--{field}-url is required when using URL input")
            body[f"{field}_url"] = urls[field]
        return body, None, None

    files = []
    for field in fields:
        if not paths[field]:
            raise UsageError(f"--{field} is required" if field == "mask" else "IMAGE is required")
        files.append((field, Path(paths[field]).expanduser()))
    return None, form_fields(body), files


def cache_path(name: str) -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(root) / PROG / f"{name}.json"


def cached_get(ctx: Context, name: str, path: str, refresh: bool) -> Any:
    target = cache_path(name)
    if not refresh and target.is_file():
        age = time.time() - target.stat().st_mtime
        if age < CACHE_TTL:
            ctx.log(f"cache hit {target} (age {int(age)}s, no HTTP request)")
            try:
                return json.loads(target.read_text(encoding="utf-8"))
            except ValueError:
                ctx.log("cache corrupt, refetching")

    payload = api_request(ctx, "GET", path, billable=False)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        ctx.log(f"cached {target}")
    except OSError as exc:
        warn(f"could not write cache {target}: {exc}")
    return payload


def print_table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)).rstrip())
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


def cmd_generate(ctx: Context, args: argparse.Namespace) -> None:
    model = args.model
    if args.vector and not model.endswith("_vector"):
        model = f"{model}_vector"
    args.model = model

    body = styling_body(args)
    body["prompt"] = args.prompt
    if args.upscale:
        body["upscale"] = args.upscale
    if args.vector:
        body.pop("image_format", None)  # meaningless for SVG output

    ext = ".svg" if args.vector else f".{args.format}"
    if args.output and not Path(args.output).is_dir():
        given = Path(args.output).suffix.lower()
        if given and given != ext:
            warn(f"output extension {given} does not match the {ext} the API will return")
    preflight_paths(ctx, args.output, args.n, ext)

    path = "/v1/images/generations/vector" if args.vector else "/v1/images/generations"
    if ctx.dry_run:
        estimate = PRICE_ESTIMATES.get(model)
        if estimate:
            warn(f"estimated cost: {estimate * args.n} credits (${usd(estimate * args.n):.3f})")
    payload = api_request(ctx, "POST", path, json_body=body)
    save_results(ctx, "generate", payload, args.output, ext, slugify(args.prompt), model)


def cmd_edit(ctx: Context, args: argparse.Namespace) -> None:
    body = styling_body(args)
    body["prompt"] = args.prompt
    body["strength"] = args.strength  # required by the API
    run_transform(ctx, "edit", "/v1/images/imageToImage", args, body)


def cmd_inpaint(ctx: Context, args: argparse.Namespace) -> None:
    body = styling_body(args)
    body["prompt"] = args.prompt
    run_transform(ctx, "inpaint", "/v1/images/inpaint", args, body, fields=("image", "mask"))


def cmd_outpaint(ctx: Context, args: argparse.Namespace) -> None:
    expansions = {
        "expand_top": args.expand_top,
        "expand_bottom": args.expand_bottom,
        "expand_left": args.expand_left,
        "expand_right": args.expand_right,
        "zoom_out_percentage": args.zoom_out,
    }
    if not args.size and not any(v is not None for v in expansions.values()):
        raise UsageError("outpaint needs --size, or at least one --expand-* / --zoom-out")

    body = styling_body(args)
    # The API marks prompt as required on this endpoint even though it is
    # meaningful only when describing the new surroundings.
    body["prompt"] = args.prompt or ""
    for field, value in expansions.items():
        if value is not None:
            body[field] = value
    run_transform(ctx, "outpaint", "/v1/images/outpaint", args, body)


def cmd_variate(ctx: Context, args: argparse.Namespace) -> None:
    body: dict[str, Any] = {
        "response_format": "url",
        "size": args.size,
        "image_format": args.format,
    }
    if args.model:
        body["model"] = args.model
    if args.seed is not None:
        body["random_seed"] = args.seed
    if args.n != 1:
        body["n"] = args.n
    run_transform(ctx, "variate", "/v1/images/variateImage", args, body)


def cmd_erase(ctx: Context, args: argparse.Namespace) -> None:
    body: dict[str, Any] = {"response_format": "url", "image_format": args.format}
    run_transform(ctx, "erase", "/v1/images/eraseRegion", args, body, fields=("image", "mask"))


def cmd_replace_bg(ctx: Context, args: argparse.Namespace) -> None:
    body = styling_body(args)
    body["prompt"] = args.prompt
    run_transform(ctx, "replace-bg", "/v1/images/replaceBackground", args, body)


def cmd_generate_bg(ctx: Context, args: argparse.Namespace) -> None:
    body = styling_body(args)
    body["prompt"] = args.prompt
    run_transform(
        ctx, "generate-bg", "/v1/images/generateBackground", args, body,
        fields=("image", "mask"),
    )


def cmd_remove_bg(ctx: Context, args: argparse.Namespace) -> None:
    body: dict[str, Any] = {"response_format": "url", "image_format": args.format}
    run_transform(ctx, "remove-bg", "/v1/images/removeBackground", args, body)


def cmd_vectorize(ctx: Context, args: argparse.Namespace) -> None:
    body: dict[str, Any] = {"response_format": "url"}
    for flag, field in (
        ("color_reduction", "color_reduction"),
        ("max_shapes", "max_num_shapes"),
        ("limit_num_shapes", "limit_num_shapes"),
        ("shape_stacking", "shape_stacking"),
        ("small_shape_filter", "small_shape_filter"),
        ("svg_compression", "svg_compression"),
        ("return_gradients", "return_gradients"),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            body[field] = value
    run_transform(ctx, "vectorize", "/v1/images/vectorize", args, body, ext=".svg")


UPSCALE_ENDPOINTS = {
    "crisp": ("/v1/images/crispUpscale", False),
    "creative": ("/v1/images/creativeUpscale", False),
    "clarity": ("/v1/images/clarityUpscale", True),
    "generative": ("/v1/images/generativeUpscale", True),
}


def cmd_upscale(ctx: Context, args: argparse.Namespace) -> None:
    path, deprecated = UPSCALE_ENDPOINTS[args.mode]
    if deprecated:
        warn(f"the {args.mode} upscale endpoint is deprecated upstream; prefer crisp or creative")
    body: dict[str, Any] = {"response_format": "url", "image_format": args.format}
    run_transform(ctx, "upscale", path, args, body)


def run_transform(
    ctx: Context,
    command: str,
    path: str,
    args: argparse.Namespace,
    body: dict[str, Any],
    fields: tuple[str, ...] = ("image",),
    ext: str | None = None,
) -> None:
    if ext is None:
        ext = f".{args.format}"
    count = getattr(args, "n", 1) or 1

    json_body, form, files = image_transport(args, body, fields)
    preflight_paths(ctx, args.output, count, ext)

    if ctx.dry_run:
        estimate = OP_ESTIMATES.get(path.rsplit("/", 1)[-1])
        if estimate:
            warn(f"estimated cost: {estimate * count} credits (${usd(estimate * count):.3f})")

    source = args.image or args.image_url or command
    slug = slugify(getattr(args, "prompt", None) or Path(str(source)).stem or command)
    payload = api_request(ctx, "POST", path, json_body=json_body, form=form, files=files)
    save_results(ctx, command, payload, args.output, ext, slug, body.get("model"))


def cmd_balance(ctx: Context, args: argparse.Namespace) -> None:
    payload = api_request(ctx, "GET", "/v1/users/me", billable=False)
    credits = int(payload.get("credits", 0))
    if ctx.json_out:
        print(json.dumps({"credits": credits, "usd": usd(credits)}))
    else:
        print(f"{credits} API credits (${usd(credits):.3f})")


def cmd_models(ctx: Context, args: argparse.Namespace) -> None:
    payload = cached_get(ctx, "models", "/v1/models", args.refresh)
    models = payload.get("data", [])
    if not args.all:
        models = [m for m in models if str(m.get("id", "")).startswith("recraft")]
    if ctx.json_out:
        print(json.dumps({"data": models}))
        return
    rows = [
        [
            str(model.get("id", "")),
            str(model.get("name", "")),
            str((model.get("pricing") or {}).get("image", "")),
        ]
        for model in sorted(models, key=lambda m: str(m.get("id", "")))
    ]
    print_table(rows, ["ID", "NAME", "USD/IMAGE"])


def cmd_styles(ctx: Context, args: argparse.Namespace) -> None:
    payload = cached_get(ctx, "styles", "/v1/styles/basic", args.refresh)
    styles = payload.get("styles", [])
    if args.model:
        styles = [s for s in styles if s.get("model") == args.model]
    if ctx.json_out:
        print(json.dumps({"styles": styles}))
        return
    rows = [
        [str(s.get("style", "")), str(s.get("model", "")), str(s.get("style_id", ""))]
        for s in sorted(styles, key=lambda s: (str(s.get("style", "")), str(s.get("model", ""))))
    ]
    print_table(rows, ["STYLE", "MODEL", "STYLE_ID"])


def cmd_my_styles(ctx: Context, args: argparse.Namespace) -> None:
    payload = api_request(ctx, "GET", "/v1/styles", billable=False)
    styles = payload.get("styles", [])
    if ctx.json_out:
        print(json.dumps({"styles": styles}))
        return
    if not styles:
        warn("no user-created styles")
        return
    rows = [
        [
            str(s.get("id", "")),
            str(s.get("style", "")),
            str(s.get("substyle", "") or "-"),
            str(s.get("creation_time", "")),
        ]
        for s in styles
    ]
    print_table(rows, ["STYLE_ID", "STYLE", "SUBSTYLE", "CREATED"])


def cmd_enhance(ctx: Context, args: argparse.Namespace) -> None:
    payload = api_request(
        ctx, "POST", "/v1/prompts/enhance", json_body={"prompt": args.prompt}
    )
    enhanced = payload.get("enhanced_prompt", "")
    credits = int(payload.get("credits", 0))
    if ctx.json_out:
        print(json.dumps({
            "command": "enhance",
            "prompt": args.prompt,
            "enhanced_prompt": enhanced,
            "credits": credits,
            "usd": usd(credits),
        }, sort_keys=True))
    else:
        print(enhanced)
        warn(f"{credits} credits (${usd(credits):.3f})")


def cmd_create_style(ctx: Context, args: argparse.Namespace) -> None:
    body: dict[str, Any] = {"style": args.base_style}
    if args.model:
        body["model"] = args.model
    if args.prompt:
        body["prompt"] = args.prompt
    if args.private:
        body["private"] = True

    files = [("images", Path(image).expanduser()) for image in args.images]
    payload = api_request(
        ctx, "POST", "/v1/styles", form=form_fields(body), files=files
    )
    style_id = payload.get("id") or payload.get("style_id", "")
    credits = int(payload.get("credits", 0))
    if ctx.json_out:
        print(json.dumps({
            "command": "create-style",
            "style_id": style_id,
            "style": payload.get("style"),
            "credits": credits,
            "usd": usd(credits),
        }, sort_keys=True))
    else:
        print(style_id)
        warn(f"use it with: {PROG} generate PROMPT --style-id {style_id}")
        warn(f"{credits} credits (${usd(credits):.3f})")


def cmd_delete_style(ctx: Context, args: argparse.Namespace) -> None:
    api_request(ctx, "DELETE", f"/v1/styles/{args.style_id}", billable=False)
    if ctx.json_out:
        print(json.dumps({"command": "delete-style", "style_id": args.style_id, "deleted": True}))
    else:
        print(args.style_id)


TOP_EPILOG = f"""\
commands:
  generate       prompt -> image, raster or --vector SVG
  edit           image + prompt -> image (imageToImage)
  inpaint        regenerate the masked region from a prompt
  outpaint       extend an image beyond its current bounds
  variate        generate variations of an image
  erase          remove the masked region, no prompt
  replace-bg     keep the subject, replace the background
  generate-bg    generate a background behind a masked subject
  remove-bg      cut the subject out to transparent PNG
  vectorize      raster -> SVG
  upscale        crisp | creative | clarity | generative
  balance        remaining API credits
  models         available models and per-image price
  styles         valid style / model / style_id combinations
  my-styles      styles you created
  enhance        rewrite a prompt with an LLM
  create-style   reference images -> reusable style_id
  delete-style   delete one of your styles

Every billable call spends API credits, never subscription credits.
Run `{PROG} COMMAND --help` for flags and an example.
"""


def add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o", "--output", metavar="PATH",
        help="output file, or a directory to name the file inside; default is ./",
    )


def add_image_input(
    parser: argparse.ArgumentParser, mask: bool = False, optional_image: bool = False
) -> None:
    parser.add_argument(
        "image",
        nargs="?" if optional_image else None,
        help="local image path (uploaded as multipart)",
    )
    parser.add_argument("--image-url", metavar="URL", help="use a remote URL or data URL instead")
    if mask:
        parser.add_argument("--mask", metavar="PATH", help="local mask image path")
        parser.add_argument("--mask-url", metavar="URL", help="remote mask URL or data URL")


def add_styling(parser: argparse.ArgumentParser, size: bool = True, count: bool = True) -> None:
    parser.add_argument(
        "--model", metavar="ID", default="recraftv4_1",
        help=f"see `{PROG} models`; default recraftv4_1, the latest general-purpose model",
    )
    parser.add_argument("--style", metavar="STYLE", help=f"see `{PROG} styles`")
    parser.add_argument("--substyle", metavar="SUBSTYLE")
    parser.add_argument("--style-id", metavar="UUID", help="custom style; excludes --style")
    if size:
        parser.add_argument("--size", metavar="SIZE", help="e.g. 1024x1024 or 16:9")
    if count:
        parser.add_argument("-n", type=int, default=1, metavar="COUNT", help="number of images")
    parser.add_argument("--negative-prompt", metavar="TEXT")
    parser.add_argument("--seed", type=int, metavar="INT")
    parser.add_argument("--format", choices=("png", "webp"), help="raster format (default png)")
    parser.add_argument("--artistic-level", type=int, choices=range(6), metavar="0-5")
    parser.add_argument("--no-text", action="store_true", help="discourage rendered text")
    parser.add_argument("--colors", metavar="HEX,HEX", help='e.g. "#ff0000,#00ff00"')
    parser.add_argument("--background-color", metavar="HEX")
    parser.add_argument("--creativity", choices=("simple", "standard", "eccentric"))
    parser.add_argument("--controls", metavar="JSON", help="raw controls object, merged last")


def build_parser(argv: list[str] | None = None) -> argparse.ArgumentParser:
    # argparse cannot match two optional positionals separated by an option, so
    # `inpaint IMG --mask M PROMPT` would lose PROMPT. Keeping IMAGE required
    # fixes that ordering; it only relaxes when --image-url replaces it.
    tokens = sys.argv[1:] if argv is None else argv
    url_input = any(t == "--image-url" or t.startswith("--image-url=") for t in tokens)

    globals_parser = argparse.ArgumentParser(add_help=False)
    globals_parser.add_argument(
        "--json", dest="json_out", action="store_true", default=argparse.SUPPRESS,
        help="emit one JSON object on stdout",
    )
    globals_parser.add_argument(
        "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="log requests to stderr (token redacted)",
    )
    globals_parser.add_argument(
        "--timeout", type=float, metavar="SECONDS", default=argparse.SUPPRESS,
        help="HTTP timeout, default 180",
    )
    globals_parser.add_argument(
        "--force", action="store_true", default=argparse.SUPPRESS,
        help="overwrite existing output files",
    )
    globals_parser.add_argument(
        "--dry-run", action="store_true", default=argparse.SUPPRESS,
        help="print the request and exit without spending credits",
    )

    parser = argparse.ArgumentParser(
        prog=PROG,
        parents=[globals_parser],
        description="Recraft AI image API client. Writes files, prints paths.",
        epilog=TOP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(json_out=False, verbose=False, timeout=180.0, force=False, dry_run=False)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def add(name: str, help_text: str, example: str) -> argparse.ArgumentParser:
        # Omitting `help` keeps the command out of argparse's auto-generated
        # list; the epilog lists them compactly so top-level --help stays short.
        return subparsers.add_parser(
            name,
            parents=[globals_parser],
            description=help_text,
            epilog=f"example:\n  {example}",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

    p = add(
        "generate",
        "Generate an image from a text prompt.",
        f'{PROG} generate "a red bicycle on a white background" --style realistic_image -o bike.png',
    )
    p.add_argument("prompt")
    p.add_argument("--vector", action="store_true", help="vector model, writes SVG")
    p.add_argument("--upscale", choices=("upscale4mp", "upscale16mp"), help="upscale during generation")
    add_styling(p)
    add_output(p)
    p.set_defaults(func=cmd_generate)

    p = add(
        "edit",
        "Transform an existing image with a prompt (image-to-image).",
        f'{PROG} edit photo.png "make it a watercolor painting" --strength 0.4 -o painted.png',
    )
    add_image_input(p, optional_image=url_input)
    p.add_argument("prompt")
    p.add_argument(
        "--strength", type=float, default=0.5, metavar="0.0-1.0",
        help="how far to move from the input; required by the API, default 0.5",
    )
    add_styling(p, size=False)
    add_output(p)
    p.set_defaults(func=cmd_edit)

    p = add(
        "inpaint",
        "Regenerate the masked region of an image from a prompt.",
        f'{PROG} inpaint room.png "a wooden table" --mask mask.png -o room-table.png',
    )
    add_image_input(p, mask=True, optional_image=url_input)
    p.add_argument("prompt")
    add_styling(p, size=False)
    add_output(p)
    p.set_defaults(func=cmd_inpaint)

    p = add(
        "outpaint",
        "Extend an image past its current edges.",
        f'{PROG} outpaint portrait.png "more of the same forest" --size 1820x1024 -o wide.png',
    )
    add_image_input(p, optional_image=url_input)
    p.add_argument("prompt", nargs="?", help="describes the new surroundings")
    p.add_argument("--expand-top", type=int, metavar="PX")
    p.add_argument("--expand-bottom", type=int, metavar="PX")
    p.add_argument("--expand-left", type=int, metavar="PX")
    p.add_argument("--expand-right", type=int, metavar="PX")
    p.add_argument("--zoom-out", type=float, metavar="PCT")
    add_styling(p, size=False)
    p.add_argument("--size", metavar="SIZE", help="target canvas, e.g. 1820x1024 or 16:9")
    add_output(p)
    p.set_defaults(func=cmd_outpaint)

    p = add(
        "variate",
        "Generate variations of an image. --size is required by the API.",
        f"{PROG} variate logo.png --size 1024x1024 -n 2 -o variants/",
    )
    add_image_input(p, optional_image=url_input)
    p.add_argument("--size", required=True, metavar="SIZE", help="required, e.g. 1024x1024")
    p.add_argument(
        "--model", metavar="ID", default="recraftv4_1",
        help=f"see `{PROG} models`; default recraftv4_1, the latest general-purpose model",
    )
    p.add_argument("-n", type=int, default=1, metavar="COUNT")
    p.add_argument("--seed", type=int, metavar="INT")
    p.add_argument("--format", choices=("png", "webp"))
    add_output(p)
    p.set_defaults(func=cmd_variate)

    p = add(
        "erase",
        "Erase the masked region and fill it in. No prompt.",
        f"{PROG} erase photo.png --mask blemish.png -o clean.png",
    )
    add_image_input(p, mask=True, optional_image=url_input)
    p.add_argument("--format", choices=("png", "webp"))
    add_output(p)
    p.set_defaults(func=cmd_erase)

    p = add(
        "replace-bg",
        "Keep the subject and replace everything behind it.",
        f'{PROG} replace-bg product.png "on a marble kitchen counter" -o product-scene.png',
    )
    add_image_input(p, optional_image=url_input)
    p.add_argument("prompt")
    add_styling(p, size=False)
    add_output(p)
    p.set_defaults(func=cmd_replace_bg)

    p = add(
        "generate-bg",
        "Generate a background behind a masked subject.",
        f'{PROG} generate-bg shoe.png --mask shoe-mask.png "on wet asphalt at night" -o shoe-bg.png',
    )
    add_image_input(p, mask=True, optional_image=url_input)
    p.add_argument("prompt")
    add_styling(p, size=False)
    add_output(p)
    p.set_defaults(func=cmd_generate_bg)

    p = add(
        "remove-bg",
        "Cut the subject out onto transparency (~10 credits).",
        f"{PROG} remove-bg product.png -o product-cutout.png",
    )
    add_image_input(p, optional_image=url_input)
    p.add_argument("--format", choices=("png", "webp"), help="default png, for alpha")
    add_output(p)
    p.set_defaults(func=cmd_remove_bg)

    p = add(
        "vectorize",
        "Trace a raster image into an SVG (~10 credits).",
        f"{PROG} vectorize logo.png -o logo.svg --color-reduction on",
    )
    add_image_input(p, optional_image=url_input)
    p.add_argument("--color-reduction", choices=("on", "off"))
    p.add_argument("--max-shapes", type=int, metavar="N")
    p.add_argument("--limit-num-shapes", choices=("on", "off"))
    p.add_argument("--shape-stacking", choices=("cut_out", "hierarchical"))
    p.add_argument("--small-shape-filter", choices=("on", "off"))
    p.add_argument("--svg-compression", choices=("on", "off"))
    p.add_argument("--return-gradients", choices=("on", "off"))
    add_output(p)
    p.set_defaults(func=cmd_vectorize)

    p = add(
        "upscale",
        "Upscale an image. Modes differ in cost by ~60x:\n"
        "  crisp       ~4 credits   ($0.004)  default\n"
        "  creative    ~250 credits ($0.25)\n"
        "  clarity     ~250 credits ($0.25)   deprecated upstream\n"
        "  generative  ~250 credits ($0.25)   deprecated upstream",
        f"{PROG} upscale photo.png --mode crisp -o photo-2x.png",
    )
    add_image_input(p, optional_image=url_input)
    p.add_argument(
        "--mode", choices=tuple(UPSCALE_ENDPOINTS), default="crisp",
        help="default crisp, by far the cheapest",
    )
    p.add_argument("--format", choices=("png", "webp"))
    add_output(p)
    p.set_defaults(func=cmd_upscale)

    p = add("balance", "Show remaining API credits.", f"{PROG} balance --json")
    p.set_defaults(func=cmd_balance)

    p = add("models", "List models and per-image price (cached 24h).", f"{PROG} models")
    p.add_argument("--refresh", action="store_true", help="bypass the cache")
    p.add_argument("--all", action="store_true", help="include third-party passthrough models")
    p.set_defaults(func=cmd_models)

    p = add("styles", "List valid style/model combinations (cached 24h).", f"{PROG} styles --model recraftv4_1")
    p.add_argument("--refresh", action="store_true", help="bypass the cache")
    p.add_argument("--model", metavar="ID", help="filter to one model")
    p.set_defaults(func=cmd_styles)

    p = add("my-styles", "List styles you created.", f"{PROG} my-styles")
    p.set_defaults(func=cmd_my_styles)

    p = add("enhance", "Rewrite a prompt with an LLM. Prints only the new prompt.", f'{PROG} enhance "a cat"')
    p.add_argument("prompt")
    p.set_defaults(func=cmd_enhance)

    p = add(
        "create-style",
        "Create a reusable style from reference images. Prints the style_id.",
        f"{PROG} create-style ref1.png ref2.png --base-style digital_illustration",
    )
    p.add_argument("images", nargs="+", metavar="REF_IMAGE")
    p.add_argument(
        "--base-style", required=True,
        choices=("any", "digital_illustration", "icon", "realistic_image",
                 "vector_illustration", "logo_raster"),
    )
    p.add_argument("--model", metavar="ID")
    p.add_argument("--prompt", metavar="TEXT")
    p.add_argument("--private", action="store_true")
    p.set_defaults(func=cmd_create_style)

    p = add("delete-style", "Delete one of your styles.", f"{PROG} delete-style 3d1e...")
    p.add_argument("style_id")
    p.set_defaults(func=cmd_delete_style)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser(argv)
    args = parser.parse_args(argv)
    # Commands that emit raster bytes must send image_format explicitly; the API
    # would otherwise default to webp behind a .png filename.
    if hasattr(args, "format"):
        args.format = resolve_format(args)
    ctx = Context(args)
    try:
        args.func(ctx, args)
        return 0
    except DryRunComplete:
        return 0
    except UsageError as exc:
        warn(str(exc))
        return 2
    except CreditsError as exc:
        warn(str(exc))
        return 3
    except ApiError as exc:
        warn(str(exc))
        return 1
    except httpx.HTTPError as exc:
        warn(f"network error: {type(exc).__name__}: {exc}")
        return 1
    except OSError as exc:
        warn(str(exc))
        return 1
    except KeyboardInterrupt:
        warn("interrupted")
        return 1
    finally:
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
