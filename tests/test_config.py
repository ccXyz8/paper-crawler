import json

from paper_crawler.config import load_config


def test_config_expands_environment_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "secret-value")
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "zotero": {"root_collection": "TFD", "api_key": "${TEST_SECRET}"},
                "conferences": [
                    {
                        "name": "CVPR",
                        "website": "https://example.test",
                        "adapter": "cvf",
                        "years": [2024],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.zotero.root_collection == "TFD"
    assert config.zotero.api_key == "secret-value"
    assert config.conferences[0].years == [2024]
