"""Cross-site request protection: browsers name the page that sent a request in Origin
(or Referer); a state-changing request from another site is refused."""


def test_cross_site_post_is_refused(client):
    resp = client.post("/pipelines/new", data={"name": "evil"},
                       headers={"Origin": "http://attacker.example"})
    assert resp.status_code == 403


def test_cross_site_referer_is_refused_when_origin_missing(client):
    resp = client.post("/pipelines/new", data={"name": "evil"},
                       headers={"Referer": "http://attacker.example/page"})
    assert resp.status_code == 403


def test_opaque_null_origin_is_refused(client):
    resp = client.post("/pipelines/new", data={"name": "evil"}, headers={"Origin": "null"})
    assert resp.status_code == 403


def test_same_origin_post_is_allowed(client):
    resp = client.post("/pipelines/new", data={"name": "ok-pipe"},
                       headers={"Origin": "http://localhost"})
    assert resp.status_code == 302


def test_request_without_origin_or_referer_is_allowed(client):
    # curl, the MCP server and other API clients send neither header.
    assert client.post("/api/v1/pipelines/nope/runs").status_code == 404


def test_cross_site_get_is_allowed(client):
    assert client.get("/", headers={"Origin": "http://attacker.example"}).status_code == 200


def test_session_cookie_is_samesite_lax(app):
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
