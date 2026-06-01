"""Discord Bot entry point.

Starts three concurrent tasks in one asyncio event loop:
  1. Discord gateway client (discord.py)
  2. Temporal Activity Worker polling the `discord-bot` task queue
  3. HTTP health server on HEALTH_PORT for Kubernetes probes
"""

import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from temporalio.client import Client
from temporalio.worker import Worker

from activities import DiscordActivities
from discord_client import BotClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "temporal-frontend.agents.svc:7233")
TASK_QUEUE = os.getenv("TASK_QUEUE", "discord-bot")
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        pass


def _start_health_server() -> None:
    HTTPServer(("", HEALTH_PORT), _HealthHandler).serve_forever()


async def main() -> None:
    threading.Thread(target=_start_health_server, daemon=True).start()
    log.info("health server listening on port %d", HEALTH_PORT)

    temporal_client = await Client.connect(TEMPORAL_HOST)
    log.info("connected to temporal at %s", TEMPORAL_HOST)

    bot = BotClient(temporal_client)
    activities = DiscordActivities(bot)

    worker = Worker(
        temporal_client,
        task_queue=TASK_QUEUE,
        activities=[
            activities.send_message,
            activities.send_notification,
            activities.archive_thread,
        ],
    )
    log.info("temporal activity worker polling queue '%s'", TASK_QUEUE)

    # Run the Discord gateway and Temporal worker concurrently.
    await asyncio.gather(
        bot.start(DISCORD_TOKEN),
        worker.run(),
    )


if __name__ == "__main__":
    asyncio.run(main())
