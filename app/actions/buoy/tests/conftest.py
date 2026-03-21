import asyncio
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.actions.buoy import BuoyClient, BuoyDevice, BuoyGear, DeviceLocation


def async_return(result):
    f = asyncio.Future()
    f.set_result(result)
    return f


# --- Sample Gear Data ---


@pytest.fixture
def sample_device_data() -> Dict[str, Any]:
    """Sample device data as returned by API."""
    return {
        "device_id": "dev-001",
        "mfr_device_id": "mfr-001",
        "label": "Device 1",
        "location": {"latitude": 45.0, "longitude": -120.0},
        "last_updated": "2024-01-15T10:00:00Z",
        "last_deployed": "2024-01-10T08:00:00Z",
    }


@pytest.fixture
def sample_gear_data(sample_device_data) -> Dict[str, Any]:
    """Sample gear data as returned by API."""
    return {
        "id": str(uuid4()),
        "display_id": "GEAR-001",
        "status": "deployed",
        "last_updated": "2024-01-15T10:00:00Z",
        "devices": [sample_device_data],
        "type": "single",
        "manufacturer": "TestManufacturer",
    }


@pytest.fixture
def sample_gear_api_response(sample_gear_data) -> Dict[str, Any]:
    """Sample API response for get gears."""
    return {
        "data": {
            "results": [sample_gear_data],
            "next": None,
        }
    }


@pytest.fixture
def sample_sources_api_response() -> Dict[str, Any]:
    """Sample API response for get sources."""
    return {
        "data": {
            "results": [
                {"id": "src-001", "manufacturer_id": "mfr-001"},
                {"id": "src-002", "manufacturer_id": "mfr-002"},
            ],
            "next": None,
        }
    }


# --- BuoyGear Fixtures ---


@pytest.fixture
def deployed_gear_source() -> BuoyGear:
    """A deployed gear from source ER."""
    return BuoyGear(
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
    )


@pytest.fixture
def hauled_gear_source() -> BuoyGear:
    """A hauled gear from source ER."""
    return BuoyGear(
        id=uuid4(),
        display_id="GEAR-002",
        status="hauled",
        last_updated=datetime(2024, 1, 16, 12, 0, 0, tzinfo=timezone.utc),
        devices=[
            BuoyDevice(
                device_id="dev-002",
                mfr_device_id="mfr-002",
                label="Device 2",
                location=DeviceLocation(latitude=46.0, longitude=-121.0),
                last_updated=datetime(2024, 1, 16, 12, 0, 0, tzinfo=timezone.utc),
                last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
            )
        ],
        type="single",
        manufacturer="TestManufacturer",
    )


@pytest.fixture
def trawl_gear_source() -> BuoyGear:
    """A trawl (two-device) gear from source ER."""
    gear_id = uuid4()
    return BuoyGear(
        id=gear_id,
        display_id="GEAR-003",
        status="deployed",
        last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        devices=[
            BuoyDevice(
                device_id="dev-003a",
                mfr_device_id="mfr-003_A",
                label="Device 3A",
                location=DeviceLocation(latitude=47.0, longitude=-122.0),
                last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
            ),
            BuoyDevice(
                device_id="dev-003b",
                mfr_device_id="mfr-003_B",
                label="Device 3B",
                location=DeviceLocation(latitude=47.1, longitude=-122.1),
                last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
            ),
        ],
        type="trawl",
        manufacturer="TestManufacturer",
    )


@pytest.fixture
def updated_gear_source(deployed_gear_source) -> BuoyGear:
    """A gear that has been updated (newer timestamp, different location)."""
    gear = deployed_gear_source.copy(deep=True)
    gear.last_updated = datetime(2024, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
    gear.devices[0].location = DeviceLocation(latitude=45.5, longitude=-120.5)
    gear.devices[0].last_updated = datetime(2024, 1, 20, 10, 0, 0, tzinfo=timezone.utc)
    return gear


# --- Mock Clients ---


@pytest.fixture
def mock_source_client(mocker) -> AsyncMock:
    """Mock BuoyClient for source ER."""
    client = AsyncMock(spec=BuoyClient)
    client.er_site = "https://source.pamdas.org"
    return client


@pytest.fixture
def mock_destination_client(mocker) -> AsyncMock:
    """Mock BuoyClient for destination ER."""
    client = AsyncMock(spec=BuoyClient)
    client.er_site = "https://destination.pamdas.org"
    return client
