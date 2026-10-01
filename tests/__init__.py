"""Test suite for the Gecko Pool MQTT bridge.

The ``gecko-iot-client`` library is imported but not tested.
All Gecko calls use fakes from :mod:`tests.fakes`.

Ausfuehren::

    python -m pip install -r requirements.txt -r requirements-dev.txt
    pytest                     # unit tests only
    pytest -m integration      # zusaetzlich gegen den Testbroker
"""
