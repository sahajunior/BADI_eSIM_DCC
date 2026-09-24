"""Local real-proxy message/SSE smoke; optional scoped worker/API restart checks."""

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.seed import demo_uuid

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8080").rstrip("/")
if urlsplit(BASE).hostname not in {"127.0.0.1", "localhost"}:
    raise SystemExit("Real-time smoke is restricted to localhost.")
PASSWORD = os.environ.get("BADI_DEMO_PASSWORD")
if not PASSWORD:
    raise SystemExit("BADI_DEMO_PASSWORD is required; use make realtime-smoke.")


async def login(client, email):
    bootstrap = await client.get("/auth/csrf")
    bootstrap.raise_for_status()
    response = await client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
        headers={
            "Origin": BASE,
            "X-CSRF-Token": bootstrap.json()["csrf_token"],
        },
    )
    response.raise_for_status()
    return response.json()["csrf_token"]


async def message(client, csrf, body, *, visibility="PUBLIC", key=None):
    ticket_id = demo_uuid("ticket", "customer1-connectivity")
    response = await client.post(
        f"/tickets/{ticket_id}/messages",
        json={
            "body": body,
            "visibility": visibility,
        },
        headers={"Origin": BASE, "X-CSRF-Token": csrf, "Idempotency-Key": key or str(uuid4())},
    )
    assert response.status_code == 201, f"Message creation returned {response.status_code}"
    return response.json()


class Feed:
    def __init__(self, client):
        self.client = client
        self.queue = asyncio.Queue()
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.consume())
        await self.wait("resync.required")
        return self

    async def __aexit__(self, *_args):
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task

    async def consume(self):
        try:
            async with self.client.stream("GET", "/events") as response:
                response.raise_for_status()
                assert response.headers["content-type"].startswith("text/event-stream")
                event = ""
                async for line in response.aiter_lines():
                    assert not line.startswith("id:"), "SSE must not promise event replay IDs"
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        await self.queue.put((event, json.loads(line[6:])))
        except Exception as error:
            await self.queue.put(("stream.error", type(error).__name__))

    async def wait(self, kind, timeout=8):
        async with asyncio.timeout(timeout):
            while True:
                event, data = await self.queue.get()
                assert event != "stream.error", f"Stream failed: {data}"
                if event == kind:
                    return data

    async def expect_no_ticket_change(self):
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            try:
                event, _data = await asyncio.wait_for(self.queue.get(), deadline - time.monotonic())
            except TimeoutError:
                return
            assert event not in {"ticket.changed", "queue.changed", "stream.error"}, event


def compose(*args):
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)


async def main(restarts):
    async with (
        httpx.AsyncClient(base_url=BASE + "/api/v1", timeout=25) as customer,
        httpx.AsyncClient(base_url=BASE + "/api/v1", timeout=25) as agent,
    ):
        customer_csrf = await login(customer, "customer1@example.test")
        agent_csrf = await login(agent, "agent1@example.test")
        ticket_id = str(demo_uuid("ticket", "customer1-connectivity"))
        async with Feed(customer) as public, Feed(agent) as staff:
            start = time.monotonic()
            result = await message(agent, agent_csrf, "Synthetic live smoke: support response")
            assert await public.wait("ticket.changed") == {"ticket_id": ticket_id}
            await public.wait("queue.changed")
            await staff.wait("ticket.changed")
            await staff.wait("queue.changed")
            latency = time.monotonic() - start
            assert latency < 2, f"Local live delivery exceeded 2s target: {latency:.3f}s"
            await message(
                agent,
                agent_csrf,
                "Synthetic private smoke: provider reference",
                visibility="INTERNAL",
            )
            await staff.wait("ticket.changed")
            await staff.wait("queue.changed")
            await public.expect_no_ticket_change()
            key = str(uuid4())
            reply = await message(
                customer, customer_csrf, "Synthetic live smoke: customer reply", key=key
            )
            replay = await message(
                customer, customer_csrf, "Synthetic live smoke: customer reply", key=key
            )
            assert replay["id"] == reply["id"]
            await staff.wait("ticket.changed")
            await staff.wait("queue.changed")
        if restarts:
            # Only this repository's local services; always bring the worker back.
            await asyncio.to_thread(compose, "stop", "worker")
            try:
                async with Feed(customer) as recovery:
                    pending = await message(agent, agent_csrf, "Synthetic worker-outage recovery")
                    await recovery.expect_no_ticket_change()
                    await asyncio.to_thread(compose, "up", "--detach", "worker")
                    await recovery.wait("ticket.changed")
            finally:
                await asyncio.to_thread(compose, "up", "--detach", "worker")
            await asyncio.to_thread(
                compose, "up", "--detach", "--no-deps", "--force-recreate", "--wait", "api"
            )
            async with asyncio.timeout(20):
                while True:
                    ready = await customer.get("/health/ready")
                    if ready.status_code == 200:
                        break
                    await asyncio.sleep(0.25)
            async with Feed(customer):
                page = await customer.get(f"/tickets/{ticket_id}/messages")
                page.raise_for_status()
                ids = [item["id"] for item in page.json()["items"]]
                assert pending["id"] in ids and result["id"] in ids
                assert len(ids) == len(set(ids))
                assert "provider reference" not in page.text
        print(
            "PASS: Nginx SSE, two-way replies, private-note isolation and retry safety; "
            f"delivery {latency:.3f}s."
        )
        if restarts:
            print(
                "PASS: worker stop/restart drains pending events; "
                "API process restart resyncs durable messages."
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--restarts",
        action="store_true",
        help="Exercise only this local Compose worker/API restart.",
    )
    asyncio.run(main(parser.parse_args().restarts))
