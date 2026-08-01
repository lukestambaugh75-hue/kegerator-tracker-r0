from tools.encrypted_dashboard_delivery import build_bundle


def test_scheduled_encrypted_delivery_keeps_full_dashboard_and_audience():
    dashboard, brief = build_bundle()
    assert len(dashboard["rows"]) == 24
    assert all(row["direct_url"].startswith("https://") for row in dashboard["rows"])
    assert brief["to"] == [
        "lukestambaugh75@gmail.com",
        "devin.mullen89@gmail.com",
    ]
    assert brief["cc"] == [] and brief["bcc"] == []
