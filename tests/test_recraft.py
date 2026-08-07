"""End-to-end tests against the local API stub in conftest.py.

No API token, no network, no credits spent.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import TOKEN, last_body, posts


class TestGenerate:
    def test_writes_png_and_prints_one_line(self, run, tmp_path):
        out_path = tmp_path / "bike.png"
        r = run("generate", "a red bicycle", "-o", str(out_path))

        assert r.code == 0, r.err
        assert len(r.out.strip().splitlines()) == 1
        assert r.out.startswith(str(out_path))
        assert "1024x1024" in r.out
        assert "35 credits ($0.035)" in r.out
        assert out_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_never_prints_base64_or_token(self, run, tmp_path):
        r = run("generate", "a red bicycle", "-o", str(tmp_path / "b.png"))
        assert "AAAA" not in r.out
        assert len(r.out) < 200
        assert TOKEN not in r.out + r.err

    def test_auto_name_is_slugified_and_absolute(self, run, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        r = run("generate", "A Red Bicycle!! On White")
        path = r.out.split("  ", 1)[0]
        assert path.startswith("/")
        assert "a-red-bicycle-on-white-" in path
        assert Path(path).exists()

    def test_output_directory_generates_name_inside(self, run, tmp_path):
        outdir = tmp_path / "out"
        outdir.mkdir()
        r = run("generate", "a red bicycle", "-o", str(outdir))
        assert str(outdir / "a-red-bicycle-") in r.out

    def test_multiple_images_numbered_and_credits_labelled_total(self, run, tmp_path):
        r = run("generate", "a red bicycle", "-n", "3", "-o", str(tmp_path / "multi.png"))
        lines = r.out.strip().splitlines()
        assert len(lines) == 3
        assert (tmp_path / "multi-2.png").exists()
        assert all("total" in line for line in lines)

    def test_vector_routes_to_vector_endpoint_and_model(self, run, stub, tmp_path):
        r = run("generate", "an icon", "--vector", "-o", str(tmp_path / "i.svg"))
        assert r.code == 0, r.err
        assert posts(stub)[-1]["path"].startswith("/v1/images/generations/vector")
        assert last_body(stub)["model"] == "recraftv4_1_vector"


class TestImageFormat:
    """Regressions for the API defaulting image_format to webp.

    Left unset, the server returns webp while the derived filename says png.
    """

    def test_defaults_to_png_explicitly(self, run, stub, tmp_path):
        run("generate", "x", "-o", str(tmp_path / "a.png"))
        assert last_body(stub)["image_format"] == "png"

    def test_output_extension_selects_webp(self, run, stub, tmp_path):
        run("generate", "x", "-o", str(tmp_path / "a.webp"))
        assert last_body(stub)["image_format"] == "webp"

    def test_explicit_flag_wins_over_extension(self, run, stub, tmp_path):
        run("generate", "x", "--format", "webp", "-o", str(tmp_path / "a.png"), "--force")
        assert last_body(stub)["image_format"] == "webp"

    def test_vector_omits_image_format(self, run, stub, tmp_path):
        run("generate", "x", "--vector", "-o", str(tmp_path / "a.svg"))
        assert "image_format" not in last_body(stub)

    def test_written_bytes_match_extension(self, run, tmp_path):
        out_path = tmp_path / "a.png"
        run("generate", "x", "-o", str(out_path))
        assert out_path.read_bytes()[:4] != b"RIFF"

    def test_content_mismatch_warns_on_stderr(self, run, tmp_path):
        # --format webp with a .png path: the stub honours webp, so bytes and
        # extension genuinely disagree and the sniff must catch it.
        r = run("generate", "x", "--format", "webp", "-o", str(tmp_path / "a.png"))
        assert "contents are webp, not png" in r.err

    def test_transform_endpoints_send_it_too(self, run, stub, image, tmp_path):
        run("remove-bg", image, "-o", str(tmp_path / "c.png"))
        assert last_body(stub)["image_format"] == "png"


class TestPositionalOrdering:
    """argparse cannot match two optional positionals split by an option."""

    def test_mask_between_image_and_prompt(self, run, stub, image, mask, tmp_path):
        r = run("generate-bg", image, "--mask", mask, "on wet asphalt",
                "-o", str(tmp_path / "o.png"))
        assert r.code == 0, r.err
        assert last_body(stub)["prompt"] == "on wet asphalt"

    def test_prompt_adjacent_to_image(self, run, stub, image, mask, tmp_path):
        r = run("inpaint", image, "a wooden table", "--mask", mask,
                "-o", str(tmp_path / "o.png"))
        assert r.code == 0, r.err
        assert last_body(stub)["prompt"] == "a wooden table"

    def test_image_url_replaces_the_positional(self, run, stub, tmp_path):
        r = run("edit", "--image-url", "https://example.com/a.png", "make it blue",
                "-o", str(tmp_path / "o.png"))
        assert r.code == 0, r.err
        body = last_body(stub)
        assert body["image_url"] == "https://example.com/a.png"
        assert body["prompt"] == "make it blue"

    def test_missing_image_is_a_usage_error(self, run):
        r = run("remove-bg")
        assert r.code == 2


class TestOverwrite:
    def test_refuses_existing_file_without_spending(self, run, stub, tmp_path):
        out_path = tmp_path / "bike.png"
        assert run("generate", "a red bicycle", "-o", str(out_path)).code == 0
        stub["requests"].clear()

        r = run("generate", "a red bicycle", "-o", str(out_path))
        assert r.code == 2
        assert not posts(stub), "refusal must happen before the billable request"

    def test_force_overwrites(self, run, tmp_path):
        out_path = tmp_path / "bike.png"
        run("generate", "a red bicycle", "-o", str(out_path))
        assert run("generate", "a red bicycle", "-o", str(out_path), "--force").code == 0


class TestJsonOutput:
    def test_single_valid_object(self, run, tmp_path):
        out_path = tmp_path / "bike.png"
        r = run("generate", "a red bicycle", "-o", str(out_path), "--json")
        doc = json.loads(r.out)
        assert doc["credits"] == 35
        assert doc["usd"] == 0.035
        assert doc["files"][0]["path"] == str(out_path)
        assert "b64" not in r.out

    def test_balance_shape(self, run):
        r = run("balance", "--json")
        assert json.loads(r.out) == {"credits": 12345, "usd": 12.345}
        assert "email" not in r.out


class TestUtilityCommands:
    def test_vectorize_writes_svg(self, run, image, tmp_path):
        out_path = tmp_path / "o.svg"
        r = run("vectorize", image, "-o", str(out_path))
        svg = out_path.read_bytes()
        assert r.code == 0, r.err
        assert b"<svg" in svg and svg.strip().endswith(b"</svg>")
        assert "10 credits ($0.010)" in r.out
        assert "1024x768" in r.out

    def test_remove_bg_writes_png_with_alpha(self, run, image, tmp_path):
        out_path = tmp_path / "cut.png"
        r = run("remove-bg", image, "-o", str(out_path))
        data = out_path.read_bytes()
        assert r.code == 0, r.err
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert data[25] == 6, "expected an RGBA colour type"

    def test_upscale_defaults_to_crisp(self, run, stub, image, tmp_path):
        r = run("upscale", image, "-o", str(tmp_path / "up.png"))
        assert "crispUpscale" in posts(stub)[-1]["path"]
        assert "4 credits ($0.004)" in r.out

    def test_balance_text(self, run):
        r = run("balance")
        assert "12345" in r.out and "$12.345" in r.out

    def test_enhance_prints_prompt_only(self, run):
        r = run("enhance", "a cat")
        assert r.out.strip() == "a vivid red bicycle, studio light"
        assert "2 credits" in r.err

    def test_create_style_prints_id(self, run, image):
        r = run("create-style", image, "--base-style", "digital_illustration")
        assert r.out.strip() == "style-abc-123"
        assert "--style-id style-abc-123" in r.err


class TestDiscoveryCache:
    def test_models_live_then_cached(self, run, stub):
        first = run("models", "--verbose")
        assert "recraftv4_1" in first.out
        assert any(r["path"] == "/v1/models" for r in stub["requests"])

        stub["requests"].clear()
        second = run("models", "--verbose")
        assert not stub["requests"], "second run must not hit the network"
        assert "cache hit" in second.err

    def test_models_refresh_bypasses_cache(self, run, stub):
        run("models")
        stub["requests"].clear()
        run("models", "--refresh")
        assert any(r["path"] == "/v1/models" for r in stub["requests"])

    def test_models_hides_third_party_by_default(self, run):
        assert "gpt_image_1_low" not in run("models").out
        assert "gpt_image_1_low" in run("models", "--all").out

    def test_styles_table_then_cached(self, run):
        first = run("styles", "--verbose")
        assert "realistic_image" in first.out and "STYLE_ID" in first.out
        assert "cache hit" in run("styles", "--verbose").err


class TestErrors:
    def test_bad_token_exits_1_without_echoing_it(self, run, monkeypatch):
        monkeypatch.setenv("RECRAFT_API_TOKEN", "BADTOKEN")
        r = run("balance")
        assert r.code == 1
        assert "authentication failed" in r.err
        assert "BADTOKEN" not in r.out + r.err

    def test_missing_token_exits_2(self, run, monkeypatch, tmp_path):
        monkeypatch.delenv("RECRAFT_API_TOKEN")
        monkeypatch.setenv("HOME", str(tmp_path))
        r = run("balance")
        assert r.code == 2
        assert "RECRAFT_API_TOKEN" in r.err

    def test_invalid_model_surfaces_api_message(self, run, tmp_path):
        r = run("generate", "x", "--model", "nosuchmodel", "-o", str(tmp_path / "z.png"))
        assert r.code == 1
        assert "model nosuchmodel is unknown" in r.err
        assert "Traceback" not in r.err

    def test_out_of_credits_exits_3_naming_api_credits(self, run, tmp_path):
        r = run("generate", "BROKE", "-o", str(tmp_path / "z.png"))
        assert r.code == 3
        assert "API credit" in r.err
        assert "recraft balance" in r.err
        assert "subscription" in r.err

    def test_retries_through_429(self, run, stub, tmp_path):
        stub["fail_429"] = 2
        r = run("generate", "retry me", "-o", str(tmp_path / "r.png"))
        assert r.code == 0, r.err

    def test_missing_url_fails_without_writing(self, run, tmp_path):
        out_path = tmp_path / "nourl.png"
        r = run("generate", "NOURL", "-o", str(out_path))
        assert r.code == 1
        assert not out_path.exists()

    def test_style_and_style_id_are_exclusive(self, run):
        r = run("generate", "x", "--style", "realistic_image", "--style-id", "abc")
        assert r.code == 2
        assert "mutually exclusive" in r.err

    def test_invalid_hex_colour(self, run):
        r = run("generate", "x", "--colors", "#zzzzzz")
        assert r.code == 2
        assert "invalid hex color" in r.err


class TestBilling:
    def test_every_billable_post_bills_api(self, run, stub, image, tmp_path):
        run("generate", "a", "-o", str(tmp_path / "b1.png"))
        run("vectorize", image, "-o", str(tmp_path / "b2.svg"))
        run("remove-bg", image, "-o", str(tmp_path / "b3.png"))
        run("upscale", image, "-o", str(tmp_path / "b4.png"))
        run("enhance", "hi")

        sent = posts(stub)
        assert sent
        assert all("billing=api" in r["path"] for r in sent)

    def test_dry_run_sends_nothing_and_exits_0(self, run, stub):
        r = run("generate", "a red bicycle", "--dry-run")
        assert r.code == 0
        assert not stub["requests"]
        assert "billing=api" in r.out
        assert '"prompt": "a red bicycle"' in r.out

    def test_dry_run_json_is_valid(self, run):
        r = run("generate", "a red bicycle", "--dry-run", "--json")
        doc = json.loads(r.out)
        assert doc["dry_run"] is True
        assert doc["url"].endswith("?billing=api")

    def test_dry_run_works_without_a_token(self, run, monkeypatch, tmp_path):
        monkeypatch.delenv("RECRAFT_API_TOKEN")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert run("generate", "x", "--dry-run").code == 0
