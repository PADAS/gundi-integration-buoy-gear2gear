import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from gundi_core.schemas.v2 import Integration, IntegrationActionConfiguration

from app.actions.buoy import BuoyDevice, BuoyGear, DeviceLocation
from app.actions.configurations import (
    Gear2GearAuthConfiguration,
    Gear2GearPullConfiguration,
)


def async_return(result):
    f = asyncio.Future()
    f.set_result(result)
    return f


@pytest.fixture
def gear2gear_auth_config():
    """Sample auth configuration for gear2gear."""
    return Gear2GearAuthConfiguration(
        source_token="source-token-123",
        source_url="https://source.pamdas.org/",
        destination_token="dest-token-456",
        destination_url="https://dest.pamdas.org/",
    )


@pytest.fixture
def gear2gear_pull_config():
    """Sample pull configuration for gear2gear."""
    return Gear2GearPullConfiguration(
        sync_interval_minutes=5,
    )


@pytest.fixture
def integration_v2_gear2gear(gear2gear_auth_config, gear2gear_pull_config):
    """Sample integration for gear2gear."""
    return Integration(
        id=uuid4(),
        name="Gear2Gear Test",
        base_url="https://test.pamdas.org",
        enabled=True,
        type={
            "id": str(uuid4()),
            "name": "Gear2Gear",
            "value": "gear2gear",
            "description": "Gear sync between ER instances",
            "actions": [
                {
                    "id": str(uuid4()),
                    "type": "auth",
                    "name": "Authenticate",
                    "value": "auth",
                },
                {
                    "id": str(uuid4()),
                    "type": "pull",
                    "name": "Pull Gear",
                    "value": "pull_gear",
                },
            ],
        },
        owner={
            "id": str(uuid4()),
            "name": "Test Org",
        },
        configurations=[
            IntegrationActionConfiguration(
                id=str(uuid4()),
                integration=str(uuid4()),
                action={
                    "id": str(uuid4()),
                    "type": "auth",
                    "name": "Authenticate",
                    "value": "auth",
                },
                data=gear2gear_auth_config.dict(),
            ),
            IntegrationActionConfiguration(
                id=str(uuid4()),
                integration=str(uuid4()),
                action={
                    "id": str(uuid4()),
                    "type": "pull",
                    "name": "Pull Gear",
                    "value": "pull_gear",
                },
                data=gear2gear_pull_config.dict(),
            ),
        ],
    )


@pytest.fixture
def sample_source_gears():
    """Sample gears from source ER."""
    return [
        BuoyGear(
            id=uuid4(),
            display_id="GEAR-001",
            status="deployed",
            last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            devices=[
                BuoyDevice(
                    device_id="dev-001",
                    mfr_device_id="mfr-001",
                    label="Device 1",
                    location=DeviceLocation(latitude=45.0, longitude=-120.0),
                    last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                    last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
                )
            ],
            type="single",
            manufacturer="TestManufacturer",
        ),
    ]


@pytest.fixture
def mock_buoy_client_class(mocker, sample_source_gears):
    """Mock the BuoyClient class."""
    mock_client = AsyncMock()
    mock_client.get_gears.return_value = sample_source_gears
    mock_client.send_gear.return_value = {"status": "success", "status_code": 201}

    mock_class = mocker.patch("app.actions.handlers.BuoyClient")
    mock_class.return_value = mock_client

    return mock_class, mock_client


@pytest.fixture
def mock_processor_class(mocker):
    """Mock the Gear2GearProcessor class."""
    mock_processor = AsyncMock()
    mock_processor.process.return_value = [
        {
            "set_id": str(uuid4()),
            "manufacturer_name": "TestManufacturer",
            "deployment_type": "single",
            "devices": [
                {
                    "device_id": "dev-001",
                    "mfr_device_id": "mfr-001",
                    "device_status": "deployed",
                    "location": {"latitude": 45.0, "longitude": -120.0},
                }
            ],
        }
    ]

    mock_class = mocker.patch("app.actions.handlers.Gear2GearProcessor")
    mock_class.return_value = mock_processor

    return mock_class, mock_processor


@pytest.fixture
def mock_log_action_activity(mocker):
    """Mock the log_action_activity function."""
    return mocker.patch(
        "app.actions.handlers.log_action_activity",
        return_value=async_return(None),
    )


@pytest.fixture
def mock_publish_event(mocker):
    """Mock the publish_event function used by activity_logger decorator."""
    return mocker.patch(
        "app.services.activity_logger.publish_event",
        return_value=async_return(None),
    )
