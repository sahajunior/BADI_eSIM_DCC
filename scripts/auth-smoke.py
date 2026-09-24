"""Exercise seeded customer isolation through the actual local HTTP proxy."""

import json
import os
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from app.seed import demo_uuid

base = os.environ.get("BASE_URL", "http://127.0.0.1:8080").rstrip("/")
if urlsplit(base).hostname not in {"127.0.0.1", "localhost"}:
    raise SystemExit("Authentication demo smoke is restricted to localhost.")
password = os.environ.get("BADI_DEMO_PASSWORD")
if not password:
    raise SystemExit("BADI_DEMO_PASSWORD is required; run through make auth-smoke.")


def client():
    return build_opener(HTTPCookieProcessor(CookieJar()))


def request(opener, path, *, method="GET", data=None, csrf=None):
    headers = {"Origin": base, "Accept": "application/json"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = Request(base + "/api/v1" + path, data=body, method=method, headers=headers)
    try:
        response = opener.open(req, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        content = response.read()
        return response.status, json.loads(content) if content else None


def login(opener, email):
    status, bootstrap = request(opener, "/auth/csrf")
    assert status == 200
    status, identity = request(
        opener,
        "/auth/login",
        method="POST",
        data={"email": email, "password": password},
        csrf=bootstrap["csrf_token"],
    )
    assert status == 200, f"Login failed with status {status} (check demo credentials/rate limits)."
    assert "password_hash" not in identity
    return identity["csrf_token"]


customer_a, customer_b, agent = client(), client(), client()
csrf_a = login(customer_a, "customer1@example.test")
login(customer_b, "customer2@example.test")
login(agent, "agent1@example.test")
ticket_a = str(demo_uuid("ticket", "customer1-connectivity"))
ticket_b = str(demo_uuid("ticket", "customer2-activation"))
assert request(customer_a, f"/tickets/{ticket_a}")[0] == 200
assert request(customer_b, f"/tickets/{ticket_b}")[0] == 200
assert request(customer_a, f"/tickets/{ticket_b}")[0] == 404
assert request(customer_b, f"/tickets/{ticket_a}")[0] == 404
assert request(customer_a, "/agents")[0] == 403
assert request(agent, f"/tickets/{ticket_a}")[0] == 200
assert request(agent, f"/tickets/{ticket_b}")[0] == 200
public = request(customer_a, f"/tickets/{ticket_a}")[1]
assert not {"version", "assigned_agent_id", "updated_at", "customer_id"} & public.keys()
assert request(customer_a, "/auth/logout", method="POST")[0] == 403
assert request(customer_a, "/auth/logout", method="POST", csrf=csrf_a)[0] == 204
assert request(customer_a, "/auth/me")[0] == 401
assert request(customer_b, "/auth/me")[0] == 200
print("PASS: real HTTP login, CSRF, customer isolation, staff permissions, and logout.")
