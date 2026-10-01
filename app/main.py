from __future__ import annotations

import asyncio
import logging
import signal

from .config import settings
from .mqtt_bridge import MqttBridge
from .pool_controller import PoolController

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def run() -> None:
    loop = asyncio.get_running_loop()
    controller: PoolController | None = None
    bridge: MqttBridge | None = None
    stopped = asyncio.Event()

    def request_stop() -> None:
        stopped.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):
            pass

    # Callbacks are invoked by paho's network thread and marshalled by the controller.
    bridge = MqttBridge(
        on_command=lambda command_type, zone_id, payload: controller.handle_command(command_type, zone_id, payload) if controller else None,
        on_heat_pump=lambda payload: controller.handle_heat_pump_command(payload) if controller else None,
        on_auth_response=lambda payload: controller.handle_auth_response(payload) if controller else None,
        on_auth_login=lambda: controller.handle_auth_login() if controller else None,
    )
    controller = PoolController(loop, bridge)
    bridge.start()
    errors = settings.validate()
    if errors:
        logger.error("Configuration errors: %s", "; ".join(errors))
    try:
        await controller.try_start_from_tokens()
        await stopped.wait()
    finally:
        await controller.stop()
        bridge.stop()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
