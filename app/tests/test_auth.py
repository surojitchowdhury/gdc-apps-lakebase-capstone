from lib import auth


def test_obo_client_pins_token_auth_despite_ambient_oauth(monkeypatch):
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "ambient-app-client")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "ambient-app-secret")
    monkeypatch.setattr(auth, "_header", lambda _name: "fake-user-obo-token")
    monkeypatch.setattr(auth, "_host", lambda: "https://example.cloud.databricks.com")
    # Recent SDKs probe host metadata during Config construction; avoid network
    # while retaining the real Config validation and credentials selection.
    monkeypatch.setattr(auth.Config, "_resolve_host_metadata", lambda _self: None)

    client = auth.obo_client()

    assert client is not None
    assert client.config.auth_type == "pat"
    assert client.config.token == "fake-user-obo-token"
