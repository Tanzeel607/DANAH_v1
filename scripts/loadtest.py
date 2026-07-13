"""Async burst load test — `make loadtest`.

Fires concurrent bursts at the two endpoints that matter operationally:

  * `GET  /api/dashboard/summary` — the read path every open browser tab polls. It fans out to a
    dozen aggregate queries, so it is where N+1s and missing indexes surface first.
  * `POST /api/agent/chat`        — the expensive path: embed → retrieve → LLM. Its latency is
    dominated by the provider, so what this measures is whether *DANAH* adds contention on top.

Reports p50/p95/p99 rather than a mean: a mean hides exactly the tail that makes a government user
say the system is slow.

    python -m scripts.loadtest --users 20 --requests 10
    python -m scripts.loadtest --endpoint dashboard --users 50 --requests 20

Chat costs real money against a live provider. `--endpoint dashboard` (the default) does not.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

from app.config import get_settings

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


@dataclass
class Results:
    latencies_ms: list[float] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def record(self, status: int, elapsed_ms: float) -> None:
        self.latencies_ms.append(elapsed_ms)
        self.statuses[status] = self.statuses.get(status, 0) + 1

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, int(len(ordered) * p))
        return ordered[index]

    @property
    def ok(self) -> int:
        return sum(count for status, count in self.statuses.items() if 200 <= status < 300)

    @property
    def failed(self) -> int:
        return sum(count for status, count in self.statuses.items() if status >= 400)


async def login(client: httpx.AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/auth/login", json={"email": email, "password": password})
    resp.raise_for_status()
    return str(resp.json()["access_token"])


async def hammer_dashboard(
    client: httpx.AsyncClient, token: str, count: int, results: Results
) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    for _ in range(count):
        started = time.perf_counter()
        try:
            resp = await client.get("/api/dashboard/summary", headers=headers)
            results.record(resp.status_code, (time.perf_counter() - started) * 1000)
        except httpx.HTTPError as exc:
            results.errors.append(type(exc).__name__)


async def hammer_chat(client: httpx.AsyncClient, token: str, count: int, results: Results) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    questions = [
        "What is the non-oil GDP target?",
        "What are the strategic input categories?",
        "What is the concentration threshold for a single country of origin?",
        "Who approves cost overruns above eight percent?",
    ]
    for i in range(count):
        started = time.perf_counter()
        try:
            resp = await client.post(
                "/api/agent/chat",
                headers=headers,
                json={"message": questions[i % len(questions)]},
            )
            results.record(resp.status_code, (time.perf_counter() - started) * 1000)
        except httpx.HTTPError as exc:
            results.errors.append(type(exc).__name__)


def report(name: str, results: Results, wall_s: float, users: int, per_user: int) -> int:
    total = len(results.latencies_ms)
    print(f"\n{BOLD}{name}{RESET}")
    print(f"{DIM}{'-' * 58}{RESET}")
    print(f"  concurrency:   {users} users x {per_user} requests")
    print(f"  completed:     {total}")
    print(f"  {GREEN}2xx:           {results.ok}{RESET}")
    if results.failed:
        print(f"  {RED}4xx/5xx:       {results.failed}  {results.statuses}{RESET}")
    if results.errors:
        print(f"  {RED}transport:     {len(results.errors)} {set(results.errors)}{RESET}")

    if not results.latencies_ms:
        print(f"  {RED}no successful responses{RESET}")
        return 1

    throughput = total / wall_s if wall_s else 0.0
    print(f"  wall clock:    {wall_s:.2f}s   ({throughput:.1f} req/s)")
    print(f"  latency  p50:  {results.percentile(0.50):7.1f} ms")
    print(f"           p95:  {results.percentile(0.95):7.1f} ms")
    print(f"           p99:  {results.percentile(0.99):7.1f} ms")
    print(f"           max:  {max(results.latencies_ms):7.1f} ms")
    print(f"          mean:  {statistics.mean(results.latencies_ms):7.1f} ms")

    # 429s under a burst are the rate limiter doing its job, not a failure.
    rate_limited = results.statuses.get(429, 0)
    if rate_limited:
        print(f"  {DIM}429s: {rate_limited} (the rate limiter is working as designed){RESET}")

    hard_failures = sum(c for s, c in results.statuses.items() if s >= 500)
    return 1 if hard_failures else 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="DANAH burst load test")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--users", type=int, default=20, help="Concurrent users")
    parser.add_argument("--requests", type=int, default=10, help="Requests per user")
    parser.add_argument(
        "--endpoint",
        choices=["dashboard", "chat", "both"],
        default="dashboard",
        help="chat calls a real LLM and costs money",
    )
    args = parser.parse_args()

    settings = get_settings()
    password = settings.admin_initial_password.get_secret_value()

    print(f"\n{BOLD}DANAH load test{RESET}")
    print(f"{DIM}target: {args.base_url}{RESET}")

    async with httpx.AsyncClient(base_url=args.base_url, timeout=120.0) as client:
        try:
            token = await login(client, settings.admin_email, password)
        except httpx.HTTPError as exc:
            print(f"\n{RED}Cannot log in: {exc}{RESET}")
            print(f"{DIM}Is the stack up? docker compose up -d && make seed{RESET}\n")
            return 2

        exit_code = 0

        if args.endpoint in ("dashboard", "both"):
            results = Results()
            started = time.perf_counter()
            await asyncio.gather(
                *(
                    hammer_dashboard(client, token, args.requests, results)
                    for _ in range(args.users)
                )
            )
            exit_code |= report(
                "GET /api/dashboard/summary",
                results,
                time.perf_counter() - started,
                args.users,
                args.requests,
            )

        if args.endpoint in ("chat", "both"):
            print(f"\n{DIM}chat burst: this calls the real LLM and costs money…{RESET}")
            results = Results()
            started = time.perf_counter()
            await asyncio.gather(
                *(hammer_chat(client, token, args.requests, results) for _ in range(args.users))
            )
            exit_code |= report(
                "POST /api/agent/chat",
                results,
                time.perf_counter() - started,
                args.users,
                args.requests,
            )

    print()
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
