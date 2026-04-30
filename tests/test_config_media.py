from __future__ import annotations

from app.config import Settings
from app.tool.stock.media import build_harmonise_image_url


def test_settings_switches_harmonise_base_url_by_mode() -> None:
    local_settings = Settings(
        local_harmonise=True,
        local_harmonise_endpoint="http://local-harmonise:9000",
        cloud_harmonise_endpoint="https://cloud-harmonise.example.com",
        cloud_harmonise_api="cloud-key",
        harmonise_headers='{"x-trace-id":"abc"}',
    )
    assert local_settings.harmonise_base_url == "http://local-harmonise:9000"
    assert local_settings.harmonise_client_headers == {"x-trace-id": "abc"}

    cloud_settings = Settings(
        local_harmonise=False,
        local_harmonise_endpoint="http://local-harmonise:9000",
        cloud_harmonise_endpoint="https://cloud-harmonise.example.com",
        cloud_harmonise_api="cloud-key",
        harmonise_headers='{"x-trace-id":"abc"}',
    )
    assert cloud_settings.harmonise_base_url == "https://cloud-harmonise.example.com"
    assert cloud_settings.harmonise_client_headers == {
        "x-trace-id": "abc",
        "x-product-api-key": "cloud-key",
    }


def test_build_harmonise_image_url_handles_path_shapes() -> None:
    assert (
        build_harmonise_image_url(
            "https://cdn.example.com/",
            "/stock/product-images/item_thumb.png",
        )
        == "https://cdn.example.com/stock/product-images/item_thumb.png"
    )
    assert (
        build_harmonise_image_url(
            "https://cdn.example.com",
            "stock/product-images/item_thumb.png",
        )
        == "https://cdn.example.com/stock/product-images/item_thumb.png"
    )
    assert (
        build_harmonise_image_url(
            "https://cdn.example.com",
            "https://images.vendor.com/path/file.png",
        )
        == "https://images.vendor.com/path/file.png"
    )
    assert build_harmonise_image_url("https://cdn.example.com", None) is None
    assert build_harmonise_image_url(None, "/stock/product-images/item_thumb.png") is None


def test_settings_supports_unbounded_harmonise_timeout() -> None:
    settings = Settings(harmonise_timeout_ms=0)
    assert settings.harmonise_timeout_seconds is None

    bounded_settings = Settings(harmonise_timeout_ms=2500)
    assert bounded_settings.harmonise_timeout_seconds == 2.5


def test_settings_reads_max_cap_variant_from_environment(monkeypatch) -> None:  # noqa: ANN001
    assert Settings(_env_file=None).max_cap_variant == 20

    monkeypatch.setenv("MAX_CAP_VARIANT", "7")
    assert Settings(_env_file=None).max_cap_variant == 7
