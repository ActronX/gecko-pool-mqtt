"""Integration tests against a real MQTT broker.

Prerequisite::

    docker compose -f docker-compose.test.yml up -d
    pytest -m integration

The Gecko cloud is not involved. ``app.pool_controller.GeckoIotClient`` and
``app.pool_controller.MqttTransporter`` are replaced with fakes so only the
bridge's MQTT interface is tested against the real broker.
"""
