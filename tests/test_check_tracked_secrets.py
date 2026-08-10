from pathlib import Path

from scripts.check_tracked_secrets import scan_text, scan_tracked_files


def _kinds(text: str) -> set[str]:
    return {finding.kind for finding in scan_text("fixture.txt", text)}


def test_allows_empty_placeholder_and_dynamic_values():
    text = "\n".join(
        [
            "QWEATHER_KEY=",
            "QWEATHER_KEY=your-qweather-key",
            "WX_MINIPROGRAM_SECRET=$WX_SECRET",
            "WXPUSHER_APP_TOKEN=AT_test-wxpusher-token",
            "https://unit-test.re.qweatherapi.com",
        ]
    )

    assert _kinds(text) == set()


def test_detects_wechat_appid_without_echoing_value():
    appid = "wx" + "0123456789abcdef"

    findings = scan_text("project.config.json", f'{{"appid": "{appid}"}}')

    assert findings == [
        findings[0].__class__("project.config.json", 1, "wechat_app_id")
    ]
    assert appid not in repr(findings)


def test_detects_literal_weather_key_and_dedicated_host():
    weather_key = "A1" + "B2C3D4E5F6G7H8J9K0L1M2N3P4Q5R6"
    api_host = "https://" + "a1b2c3d4e5f6" + ".re.qweatherapi.com"

    kinds = _kinds(
        f"- 历史记录 `QWEATHER_KEY={weather_key}`\nQWEATHER_API_BASE={api_host}\n"
    )

    assert kinds == {"literal_qweather_key", "qweather_api_host"}


def test_detects_appsecret_wxpusher_and_private_key():
    appsecret = "A1" + "B2C3D4E5F6G7H8J9K0L1M2N3P4Q5R6"
    wxpusher = "AT_" + "Ab12Cd34Ef56Gh78Ij90Kl12"
    private_key_marker = "-----BEGIN " + "PRIVATE KEY-----"

    kinds = _kinds(
        f"WX_MINIPROGRAM_SECRET={appsecret}\n{wxpusher}\n{private_key_marker}\n"
    )

    assert kinds == {
        "literal_wx_miniprogram_secret",
        "private_key",
        "wxpusher_token",
    }


def test_current_tracked_tree_has_no_secret_findings():
    root = Path(__file__).resolve().parents[1]

    assert scan_tracked_files(root) == []
