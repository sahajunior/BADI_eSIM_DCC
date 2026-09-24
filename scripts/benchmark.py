#!/usr/bin/env python3
"""Read-only latency/throughput measurement against a running local stack.

Measures customer and staff read endpoints through the proxy with bounded
concurrency. It never mutates data. Results are engineering measurements on a
local Docker stack, not external SLAs.

Usage:
    cd backend && uv run --locked --env-file ../.env python ../scripts/benchmark.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

ORIGIN = "http://127.0.0.1:8080"


def load_demo_password() -> str:
    password = os.environ.get("BADI_DEMO_PASSWORD")
    if password:
        return password
    for candidate in (".env", "../.env"):
        if os.path.exists(candidate):
            for line in open(candidate, encoding="utf-8"):
                if line.startswith("BADI_DEMO_PASSWORD="):
                    return line.split("=", 1)[1].strip()
    raise SystemExit("BADI_DEMO_PASSWORD is required (run make env)")


@dataclass
class Result:
    endpoint: str
    count: int
    errors: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    rps: float


async def login(client: httpx.AsyncClient, base_url: str, email: str, password: str) -> None:
    csrf = await client.get(f"{base_url}/api/v1/auth/csrf")
    csrf.raise_for_status()
    response = await client.post(
        f"{base_url}/api/v1/auth/login",
        json={"email": email, "password": password},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf.json()["csrf_token"]},
    )
    response.raise_for_status()


async def first_ticket_id(client: httpx.AsyncClient, base_url: str) -> str:
    page = await client.get(f"{base_url}/api/v1/tickets", params={"limit": 1})
    page.raise_for_status()
    items = page.json()["items"]
    if not items:
        raise SystemExit("No tickets available; run `make seed` first")
    return items[0]["id"]


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


async def measure(
    client: httpx.AsyncClient,
    base_url: str,
    endpoint: str,
    path: str,
    total: int,
    concurrency: int,
) -> Result:
    latencies: list[float] = []
    errors = 0
    semaphore = asyncio.Semaphore(concurrency)

    async def one() -> None:
        nonlocal errors
        async with semaphore:
            started = time.perf_counter()
            try:
                response = await client.get(f"{base_url}{path}")
                if response.status_code != 200:
                    errors += 1
            except httpx.HTTPError:
                errors += 1
            latencies.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(total)))
    elapsed = time.perf_counter() - started
    return Result(
        endpoint=endpoint,
        count=total,
        errors=errors,
        p50_ms=round(percentile(latencies, 0.50), 2),
        p95_ms=round(percentile(latencies, 0.95), 2),
        p99_ms=round(percentile(latencies, 0.99), 2),
        max_ms=round(max(latencies), 2),
        rps=round(total / elapsed, 1),
    )


async def run(base_url: str, password: str, total: int, concurrency: int) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=15) as customer, httpx.AsyncClient(timeout=15) as agent:
        await login(customer, base_url, "customer1@example.test", password)
        await login(agent, base_url, "agent1@example.test", password)
        ticket_id = await first_ticket_id(customer, base_url)

        # Warm up each endpoint once before measuring.
        for client, path in (
            (customer, "/api/v1/tickets?limit=25"),
            (customer, f"/api/v1/tickets/{ticket_id}"),
            (customer, f"/api/v1/tickets/{ticket_id}/messages?limit=50"),
            (agent, "/api/v1/tickets?limit=25"),
            (agent, "/api/v1/dashboard/summary"),
            (agent, f"/api/v1/tickets/{ticket_id}/sla"),
        ):
            await client.get(f"{base_url}{path}")

        plan = [
            (customer, "customer list", "/api/v1/tickets?limit=25"),
            (customer, "customer detail", f"/api/v1/tickets/{ticket_id}"),
            (customer, "customer messages", f"/api/v1/tickets/{ticket_id}/messages?limit=50"),
            (agent, "staff list", "/api/v1/tickets?limit=25"),
            (agent, "staff dashboard", "/api/v1/dashboard/summary"),
            (agent, "staff sla", f"/api/v1/tickets/{ticket_id}/sla"),
        ]
        results = [
            await measure(client, base_url, name, path, total, concurrency)
            for client, name, path in plan
        ]

    return {
        "base_url": base_url,
        "concurrency": concurrency,
        "requests_per_endpoint": total,
        "results": [result.__dict__ for result in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args()

    host = urlsplit(args.base_url).hostname or "127.0.0.1"
    if host not in {"127.0.0.1", "localhost"}:
        print("Refusing to benchmark a non-local target.", file=sys.stderr)
        return 2

    report = asyncio.run(run(args.base_url, load_demo_password(), args.requests, args.concurrency))
    print(json.dumps(report, indent=2))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
