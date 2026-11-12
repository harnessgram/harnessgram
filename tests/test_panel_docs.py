"""docs/panel.md names every route the panel server answers, and no route that does not exist."""
import re
from pathlib import Path

from ctb import panel

DOC = (Path(__file__).resolve().parent.parent / "docs" / "panel.md").read_text(encoding="utf-8")


def test_every_route_is_documented():
    missing = sorted(r for r in panel.ROUTES if f"`{r}`" not in DOC)
    assert not missing, f"docs/panel.md does not describe: {missing}"


def test_the_docs_name_no_route_that_is_gone():
    named = set(re.findall(r"/api/[a-z-]+", DOC))
    assert named <= set(panel.ROUTES), sorted(named - set(panel.ROUTES))


def test_every_action_route_has_a_handler():
    assert set(panel.ACTIONS) <= set(panel.ROUTES)


def test_the_static_page_is_documented_as_served_without_a_login():
    assert "`GET /`" in DOC and "served without a login" in DOC
    assert "Every route except the static `GET /` page" in DOC


def test_the_app_routes_are_documented_with_their_logins():
    for pattern in ("GET /app/<name>/", "GET /app/<name>/media/<path>", "POST /app/<name>/api"):
        assert f"`{pattern}`" in DOC, pattern
    # the documented shapes are the ones the server matches
    assert panel.APP_GET.fullmatch("/app/sample-app/") and panel.APP_GET.fullmatch("/app/sample-app/media/a/b.png")
    assert panel.APP_API.fullmatch("/app/sample-app/api")
    row = {line.split("|")[1].strip(): line for line in DOC.splitlines() if line.startswith("| `GET /app/") or line.startswith("| `POST /app/")}
    assert "?auth=" in row["`GET /app/<name>/`"] and "1 hour" in row["`GET /app/<name>/`"]
    assert "?auth=" in row["`GET /app/<name>/media/<path>`"]
    assert "header only" in row["`POST /app/<name>/api`"] and "15 minutes" in row["`POST /app/<name>/api`"]
    # the freshness windows quoted are the server's
    assert panel.MAX_AGE == 3600 and panel.DECIDE_MAX_AGE == 900


def test_the_deployment_text_matches_the_installer():
    from ctb import install
    assert install.PANEL_JOB in DOC and "never started" in DOC
    assert "does not install its\n> service" in DOC or "does not install its service" in DOC.replace("\n> ", " ")
    assert "tailscale serve --https" not in DOC  # a private deployment's command is not repository behaviour


def _route_table(title_marker):
    """{route: limit column} of the markdown table that holds the `/api/` routes with a numeric limit."""
    out = {}
    for line in DOC.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| `/api/") and len(cells) >= 3 and cells[1].isdigit():
            out[cells[0].strip("`")] = int(cells[1])
    return out


def test_every_ordinary_route_is_in_the_route_table_with_the_servers_body_limit():
    table = _route_table("Routes")
    assert set(table) == set(panel.ROUTES), sorted(set(table) ^ set(panel.ROUTES))
    wrong = {r: (n, panel.ROUTES[r]) for r, n in table.items() if n != panel.ROUTES[r]}
    assert not wrong, f"documented limit differs from panel.ROUTES: {wrong}"


def test_app_routes_stay_out_of_the_ordinary_table_and_the_other_way_round():
    app_rows = [l for l in DOC.splitlines() if l.startswith("| `GET /app/") or l.startswith("| `POST /app/")]
    assert len(app_rows) == 3 and not any("/api/radio" in l or "/api/bank" in l for l in app_rows)
    assert not any(l.startswith("| `/api/") and "Login" in l for l in DOC.splitlines())


def test_the_auth_query_is_documented_with_its_lifetime_and_the_log_advice():
    assert "`?auth=`" in DOC and "strip the `auth` query parameter" in DOC
    assert "one hour" in DOC and panel.MAX_AGE == 3600
    assert "`auth`" in DOC and "access logs" in DOC
