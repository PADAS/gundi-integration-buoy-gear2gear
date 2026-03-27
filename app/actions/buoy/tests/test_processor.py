from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from shapely.geometry import MultiPolygon, Polygon

from app.actions.buoy import (
    BuoyClient,
    BuoyDevice,
    BuoyGear,
    DeviceLocation,
    FeatureGroupNotFoundError,
)
from app.actions.buoy.processor import (
    Gear2GearProcessor,
    load_polygons_from_feature_groups,
)


class TestGear2GearProcessorPayloadCreation:
    """Tests for payload creation methods."""

    @pytest.fixture
    def processor(self, mock_source_client, mock_destination_client):
        """Create a processor with mock clients."""
        return Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
        )

    def test_create_deploy_payload(self, processor, deployed_gear_source):
        """Test creating a deployment payload."""
        payload = processor._create_deploy_payload(deployed_gear_source)

        assert payload["set_id"] == str(deployed_gear_source.id)
        assert payload["manufacturer_name"] == deployed_gear_source.manufacturer
        assert payload["deployment_type"] == deployed_gear_source.type
        assert payload["devices_in_set"] == len(deployed_gear_source.devices)
        assert len(payload["devices"]) == 1

        device = payload["devices"][0]
        assert device["device_id"] == "dev-001"
        assert device["mfr_device_id"] == "mfr-001"
        assert device["device_status"] == "deployed"
        assert device["location"]["latitude"] == 45.0
        assert device["location"]["longitude"] == -120.0

        # Should have initial_deployment_date for deployed gear
        assert "initial_deployment_date" in payload

    def test_create_deploy_payload_trawl(self, processor, trawl_gear_source):
        """Test creating a deployment payload for trawl gear."""
        payload = processor._create_deploy_payload(trawl_gear_source)

        assert payload["deployment_type"] == "trawl"
        assert payload["devices_in_set"] == 2
        assert len(payload["devices"]) == 2

        device_ids = [d["mfr_device_id"] for d in payload["devices"]]
        assert "mfr-003_A" in device_ids
        assert "mfr-003_B" in device_ids

    def test_create_update_payload(
        self, processor, updated_gear_source, deployed_gear_source
    ):
        """Test creating an update payload."""
        payload = processor.create_update_payload(
            updated_gear_source, deployed_gear_source
        )

        # set_id should come from the destination gear (the one being updated)
        assert payload["set_id"] == str(deployed_gear_source.id)
        assert payload["manufacturer_name"] == updated_gear_source.manufacturer

        # Update payload should NOT have initial_deployment_date
        assert "initial_deployment_date" not in payload
        assert "devices_in_set" not in payload

        device = payload["devices"][0]
        # Should have new location
        assert device["location"]["latitude"] == 45.5
        assert device["location"]["longitude"] == -120.5

    def test_create_haul_payload(
        self, processor, hauled_gear_source, deployed_gear_source
    ):
        """Test creating a haul payload."""
        payload = processor._create_haul_payload(
            hauled_gear_source, deployed_gear_source
        )

        # set_id should come from the destination gear (the one being hauled)
        assert payload["set_id"] == str(deployed_gear_source.id)
        assert payload["manufacturer_name"] == hauled_gear_source.manufacturer

        device = payload["devices"][0]
        assert device["device_status"] == "hauled"


class TestGear2GearProcessorNeedsUpdate:
    """Tests for the _needs_update method."""

    @pytest.fixture
    def processor(self, mock_source_client, mock_destination_client):
        return Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
        )

    def test_needs_update_status_changed(self, processor, deployed_gear_source):
        """Test that status change triggers update."""
        dest_gear = deployed_gear_source.copy(deep=True)
        source_gear = deployed_gear_source.copy(deep=True)
        source_gear.status = "hauled"

        assert processor.needs_update(source_gear, dest_gear) is True

    def test_needs_update_newer_timestamp(self, processor, deployed_gear_source):
        """Test that newer timestamp triggers update."""
        dest_gear = deployed_gear_source.copy(deep=True)
        source_gear = deployed_gear_source.copy(deep=True)
        source_gear.last_updated = datetime(2024, 2, 1, 10, 0, 0, tzinfo=timezone.utc)

        assert processor.needs_update(source_gear, dest_gear) is True

    def test_needs_update_location_changed(self, processor, deployed_gear_source):
        """Test that location change triggers update."""
        dest_gear = deployed_gear_source.copy(deep=True)
        source_gear = deployed_gear_source.copy(deep=True)
        source_gear.devices[0].location = DeviceLocation(
            latitude=46.0, longitude=-121.0
        )

        assert processor.needs_update(source_gear, dest_gear) is True

    def test_needs_update_no_change(self, processor, deployed_gear_source):
        """Test that no change doesn't trigger update."""
        dest_gear = deployed_gear_source.copy(deep=True)
        source_gear = deployed_gear_source.copy(deep=True)

        assert processor.needs_update(source_gear, dest_gear) is False


class TestGear2GearProcessorIdentifySyncActions:
    """Tests for the _identify_sync_actions method."""

    @pytest.fixture
    def processor(self, mock_source_client, mock_destination_client):
        return Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
        )

    @pytest.mark.asyncio
    async def test_identify_new_gear_for_deployment(
        self, processor, deployed_gear_source
    ):
        """Test that new gear in source is marked for deployment."""
        source_gears = [deployed_gear_source]
        dest_gears = []

        to_deploy, to_update, to_haul = processor._identify_sync_actions(
            source_gears, dest_gears
        )

        assert len(to_deploy) == 1
        assert to_deploy[0] == deployed_gear_source
        assert len(to_update) == 0
        assert len(to_haul) == 0

    @pytest.mark.asyncio
    async def test_identify_gear_for_update(self, processor, deployed_gear_source):
        """Test that changed gear is marked for update."""
        source_gear = deployed_gear_source.copy(deep=True)
        source_gear.last_updated = datetime(2024, 2, 1, 10, 0, 0, tzinfo=timezone.utc)
        source_gear.devices[0].location = DeviceLocation(
            latitude=46.0, longitude=-121.0
        )

        dest_gear = deployed_gear_source.copy(deep=True)

        to_deploy, to_update, to_haul = processor._identify_sync_actions(
            [source_gear], [dest_gear]
        )

        assert len(to_deploy) == 0
        assert len(to_update) == 1
        assert to_update[0][0] == source_gear
        assert len(to_haul) == 0

    @pytest.mark.asyncio
    async def test_identify_gear_for_haul(self, processor, deployed_gear_source):
        """Test that hauled gear in source triggers haul in destination."""
        source_gear = deployed_gear_source.copy(deep=True)
        source_gear.status = "hauled"

        dest_gear = deployed_gear_source.copy(deep=True)
        dest_gear.status = "deployed"

        to_deploy, to_update, to_haul = processor._identify_sync_actions(
            [source_gear], [dest_gear]
        )

        assert len(to_deploy) == 0
        assert len(to_update) == 0
        assert len(to_haul) == 1
        assert to_haul[0] == (source_gear, dest_gear)

    @pytest.mark.asyncio
    async def test_skip_hauled_gear_not_in_destination(
        self, processor, hauled_gear_source
    ):
        """Test that hauled gear not in destination is skipped (not deployed)."""
        source_gears = [hauled_gear_source]
        dest_gears = []

        to_deploy, to_update, to_haul = processor._identify_sync_actions(
            source_gears, dest_gears
        )

        # Hauled gear shouldn't be deployed
        assert len(to_deploy) == 0
        assert len(to_update) == 0
        assert len(to_haul) == 0

    @pytest.mark.asyncio
    async def test_match_by_mfr_device_id(self, processor, deployed_gear_source):
        """Test that gears are matched by mfr_device_id when IDs differ."""
        source_gear = deployed_gear_source.copy(deep=True)
        source_gear.last_updated = datetime(2024, 2, 1, 10, 0, 0, tzinfo=timezone.utc)

        # Destination has same mfr_device_id but different gear ID
        dest_gear = deployed_gear_source.copy(deep=True)
        dest_gear.id = uuid4()  # Different gear ID

        to_deploy, to_update, to_haul = processor._identify_sync_actions(
            [source_gear], [dest_gear]
        )

        # Should match by mfr_device_id and mark for update
        assert len(to_deploy) == 0
        assert len(to_update) == 1
        assert len(to_haul) == 0

    @pytest.mark.asyncio
    async def test_no_change_skipped(self, processor, deployed_gear_source):
        """Test that gear with no changes is skipped."""
        source_gear = deployed_gear_source.copy(deep=True)
        dest_gear = deployed_gear_source.copy(deep=True)

        to_deploy, to_update, to_haul = processor._identify_sync_actions(
            [source_gear], [dest_gear]
        )

        assert len(to_deploy) == 0
        assert len(to_update) == 0
        assert len(to_haul) == 0


class TestGear2GearProcessorProcess:
    """Tests for the main process method."""

    @pytest.fixture
    def processor(self, mock_source_client, mock_destination_client):
        return Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
        )

    @pytest.mark.asyncio
    async def test_process_empty_source(
        self, processor, mock_source_client, mock_destination_client
    ):
        """Test processing with no gears in source."""
        mock_source_client.get_gears.side_effect = [[], []]  # deployed, hauled
        mock_destination_client.get_gears.side_effect = [[], []]  # deployed, hauled

        payloads = await processor.process()

        assert payloads == []
        assert mock_source_client.get_gears.call_count == 2
        assert mock_destination_client.get_gears.call_count == 2

    @pytest.mark.asyncio
    async def test_process_new_deployment(
        self,
        processor,
        mock_source_client,
        mock_destination_client,
        deployed_gear_source,
    ):
        """Test processing a new gear deployment."""
        mock_source_client.get_gears.side_effect = [
            [deployed_gear_source],
            [],
        ]  # deployed, hauled
        mock_destination_client.get_gears.side_effect = [[], []]  # deployed, hauled

        payloads = await processor.process()

        assert len(payloads) == 1
        assert payloads[0]["set_id"] == str(deployed_gear_source.id)
        assert payloads[0]["devices"][0]["device_status"] == "deployed"

    @pytest.mark.asyncio
    async def test_process_haul(
        self,
        processor,
        mock_source_client,
        mock_destination_client,
        deployed_gear_source,
    ):
        """Test processing a gear haul."""
        source_gear = deployed_gear_source.copy(deep=True)
        source_gear.status = "hauled"

        dest_gear = deployed_gear_source.copy(deep=True)
        dest_gear.status = "deployed"

        mock_source_client.get_gears.side_effect = [
            [],
            [source_gear],
        ]  # deployed, hauled
        mock_destination_client.get_gears.side_effect = [
            [dest_gear],
            [],
        ]  # deployed, hauled

        payloads = await processor.process()

        assert len(payloads) == 1
        assert payloads[0]["devices"][0]["device_status"] == "hauled"

    @pytest.mark.asyncio
    async def test_process_mixed_actions(
        self,
        processor,
        mock_source_client,
        mock_destination_client,
        deployed_gear_source,
        trawl_gear_source,
    ):
        """Test processing with multiple action types."""
        # New gear (not in destination) - should be deployed
        new_gear = deployed_gear_source.copy(deep=True)
        new_gear.id = uuid4()
        new_gear.display_id = "NEW-GEAR"
        new_gear.devices[0].mfr_device_id = "new-mfr-id"

        # Existing gear with update
        updated_source = trawl_gear_source.copy(deep=True)
        updated_source.last_updated = datetime(
            2024, 2, 1, 10, 0, 0, tzinfo=timezone.utc
        )

        existing_dest = trawl_gear_source.copy(deep=True)

        mock_source_client.get_gears.side_effect = [
            [new_gear, updated_source],
            [],
        ]  # deployed, hauled
        mock_destination_client.get_gears.side_effect = [
            [existing_dest],
            [],
        ]  # deployed, hauled

        payloads = await processor.process()

        assert len(payloads) == 2

        # Verify we have both a deployment and an update
        set_ids = [p["set_id"] for p in payloads]
        assert str(new_gear.id) in set_ids
        assert str(updated_source.id) in set_ids

    @pytest.mark.asyncio
    async def test_process_deployed_and_hauled_from_source(
        self,
        processor,
        mock_source_client,
        mock_destination_client,
        deployed_gear_source,
        trawl_gear_source,
    ):
        """Test processing with both deployed and hauled gears from source."""
        # A new deployed gear
        new_gear = deployed_gear_source.copy(deep=True)
        new_gear.id = uuid4()
        new_gear.display_id = "NEW-GEAR"
        new_gear.devices[0].mfr_device_id = "new-mfr-id"

        # A hauled gear that exists deployed in destination
        hauled_gear = trawl_gear_source.copy(deep=True)
        hauled_gear.status = "hauled"

        dest_gear = trawl_gear_source.copy(deep=True)
        dest_gear.status = "deployed"

        mock_source_client.get_gears.side_effect = [
            [new_gear],  # deployed
            [hauled_gear],  # hauled
        ]
        mock_destination_client.get_gears.side_effect = [
            [dest_gear],  # deployed
            [],  # hauled
        ]

        payloads = await processor.process()

        assert len(payloads) == 2
        set_ids = [p["set_id"] for p in payloads]
        assert str(new_gear.id) in set_ids
        assert str(trawl_gear_source.id) in set_ids

        # Verify correct actions
        for p in payloads:
            if p["set_id"] == str(new_gear.id):
                assert p["devices"][0]["device_status"] == "deployed"
            else:
                assert p["devices"][0]["device_status"] == "hauled"

    @pytest.mark.asyncio
    async def test_process_passes_correct_status_params(
        self,
        processor,
        mock_source_client,
        mock_destination_client,
    ):
        """Test that get_gears is called with correct status params."""
        mock_source_client.get_gears.side_effect = [[], []]
        mock_destination_client.get_gears.side_effect = [[], []]

        await processor.process()

        source_calls = mock_source_client.get_gears.call_args_list
        assert len(source_calls) == 2
        assert source_calls[0].kwargs["status"] == "deployed"
        assert source_calls[1].kwargs["status"] == "hauled"

        dest_calls = mock_destination_client.get_gears.call_args_list
        assert len(dest_calls) == 2
        assert dest_calls[0].kwargs["status"] == "deployed"
        assert dest_calls[1].kwargs["status"] == "hauled"

    @pytest.mark.asyncio
    async def test_process_with_lookback_sends_updated_since(
        self,
        mock_source_client,
        mock_destination_client,
    ):
        """Test that lookback_minutes adds updated_since to source params."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            lookback_minutes=5,
        )
        mock_source_client.get_gears.side_effect = [[], []]
        mock_destination_client.get_gears.side_effect = [[], []]

        await processor.process()

        # Source calls should have updated_since
        source_calls = mock_source_client.get_gears.call_args_list
        for call in source_calls:
            assert "updated_since" in call.kwargs["params"]

        # Destination calls should NOT have updated_since
        dest_calls = mock_destination_client.get_gears.call_args_list
        for call in dest_calls:
            assert "updated_since" not in call.kwargs["params"]

    @pytest.mark.asyncio
    async def test_process_without_lookback_no_updated_since(
        self,
        processor,
        mock_source_client,
        mock_destination_client,
    ):
        """Test that without lookback, no updated_since is sent."""
        mock_source_client.get_gears.side_effect = [[], []]
        mock_destination_client.get_gears.side_effect = [[], []]

        await processor.process()

        source_calls = mock_source_client.get_gears.call_args_list
        for call in source_calls:
            assert "updated_since" not in call.kwargs["params"]


class TestGear2GearProcessorHelpers:
    """Tests for helper methods."""

    @pytest.fixture
    def processor(self, mock_source_client, mock_destination_client):
        return Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
        )

    def test_remove_milliseconds(self, processor):
        """Test milliseconds removal from datetime."""
        dt = datetime(2024, 1, 15, 10, 30, 45, 123456, tzinfo=timezone.utc)
        result = processor._remove_milliseconds(dt)

        assert result.microsecond == 0
        assert result.second == 45
        assert result.minute == 30

    def test_deduplicate_gears_no_duplicates(
        self, deployed_gear_source, trawl_gear_source
    ):
        """Test deduplication with no duplicates is a no-op."""
        gears = [deployed_gear_source, trawl_gear_source]
        result = Gear2GearProcessor._deduplicate_gears(gears)
        assert len(result) == 2

    def test_deduplicate_gears_keeps_last(self, deployed_gear_source):
        """Test that deduplication keeps the last occurrence."""
        deployed = deployed_gear_source.copy(deep=True)
        deployed.status = "deployed"

        hauled = deployed_gear_source.copy(deep=True)
        hauled.status = "hauled"

        # deployed + hauled order: hauled wins
        result = Gear2GearProcessor._deduplicate_gears([deployed, hauled])
        assert len(result) == 1
        assert result[0].status == "hauled"

    @pytest.mark.asyncio
    async def test_process_deduplicates_source_gears(
        self,
        processor,
        mock_source_client,
        mock_destination_client,
        deployed_gear_source,
    ):
        """Test that duplicate gears across deployed/hauled are deduplicated."""
        deployed = deployed_gear_source.copy(deep=True)
        deployed.status = "deployed"

        hauled = deployed_gear_source.copy(deep=True)
        hauled.status = "hauled"

        dest_gear = deployed_gear_source.copy(deep=True)
        dest_gear.status = "deployed"

        # Same gear appears in both deployed and hauled responses
        mock_source_client.get_gears.side_effect = [
            [deployed],  # deployed
            [hauled],  # hauled
        ]
        mock_destination_client.get_gears.side_effect = [
            [dest_gear],  # deployed
            [],  # hauled
        ]

        payloads = await processor.process()

        # Should produce exactly one haul payload, not two actions
        assert len(payloads) == 1
        assert payloads[0]["devices"][0]["device_status"] == "hauled"


class TestGear2GearProcessorPolygonFiltering:
    """Tests for polygon-based gear filtering."""

    @pytest.fixture
    def test_polygon(self):
        """A polygon around coordinates (44-46, -121 to -119)."""
        return Polygon([(-121, 44), (-119, 44), (-119, 46), (-121, 46), (-121, 44)])

    @pytest.fixture
    def gear_inside_polygon(self):
        """A gear with device inside the test polygon."""
        return BuoyGear(
            id=uuid4(),
            display_id="GEAR-INSIDE",
            status="deployed",
            last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            devices=[
                BuoyDevice(
                    device_id="dev-inside",
                    mfr_device_id="mfr-inside",
                    label="Device Inside",
                    location=DeviceLocation(latitude=45.0, longitude=-120.0),
                    last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                    last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
                )
            ],
            type="single",
            manufacturer="TestManufacturer",
        )

    @pytest.fixture
    def gear_outside_polygon(self):
        """A gear with device outside the test polygon."""
        return BuoyGear(
            id=uuid4(),
            display_id="GEAR-OUTSIDE",
            status="deployed",
            last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            devices=[
                BuoyDevice(
                    device_id="dev-outside",
                    mfr_device_id="mfr-outside",
                    label="Device Outside",
                    location=DeviceLocation(latitude=50.0, longitude=-130.0),
                    last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                    last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
                )
            ],
            type="single",
            manufacturer="TestManufacturer",
        )

    @pytest.fixture
    def gear_with_null_location(self):
        """A gear with null location coordinates."""
        return BuoyGear(
            id=uuid4(),
            display_id="GEAR-NULL",
            status="deployed",
            last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            devices=[
                BuoyDevice(
                    device_id="dev-null",
                    mfr_device_id="mfr-null",
                    label="Device Null",
                    location=DeviceLocation(latitude=None, longitude=None),
                    last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                    last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
                )
            ],
            type="single",
            manufacturer="TestManufacturer",
        )

    def test_gear_is_inside_polygons_with_no_shapes(
        self, mock_source_client, mock_destination_client, gear_inside_polygon
    ):
        """Test that all gears pass when no polygons configured."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[],
        )

        assert processor._gear_is_inside_polygons(gear_inside_polygon) is True

    def test_gear_is_inside_polygons_inside(
        self,
        mock_source_client,
        mock_destination_client,
        test_polygon,
        gear_inside_polygon,
    ):
        """Test gear with device inside polygon returns True."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[test_polygon],
        )

        assert processor._gear_is_inside_polygons(gear_inside_polygon) is True

    def test_gear_is_inside_polygons_outside(
        self,
        mock_source_client,
        mock_destination_client,
        test_polygon,
        gear_outside_polygon,
    ):
        """Test gear with device outside polygon returns False."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[test_polygon],
        )

        assert processor._gear_is_inside_polygons(gear_outside_polygon) is False

    def test_gear_is_inside_polygons_null_location(
        self,
        mock_source_client,
        mock_destination_client,
        test_polygon,
        gear_with_null_location,
    ):
        """Test gear with null location coordinates returns False."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[test_polygon],
        )

        assert processor._gear_is_inside_polygons(gear_with_null_location) is False

    def test_gear_is_inside_polygons_multiple_polygons(
        self, mock_source_client, mock_destination_client, gear_outside_polygon
    ):
        """Test with multiple polygons, gear inside second one."""
        polygon1 = Polygon([(-100, 40), (-98, 40), (-98, 42), (-100, 42), (-100, 40)])
        # This polygon contains the gear_outside_polygon location (50, -130)
        polygon2 = Polygon([(-135, 48), (-125, 48), (-125, 52), (-135, 52), (-135, 48)])

        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[polygon1, polygon2],
        )

        assert processor._gear_is_inside_polygons(gear_outside_polygon) is True

    def test_filter_gears_by_polygon_no_shapes(
        self,
        mock_source_client,
        mock_destination_client,
        gear_inside_polygon,
        gear_outside_polygon,
    ):
        """Test that filter returns all gears when no polygons configured."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[],
        )

        gears = [gear_inside_polygon, gear_outside_polygon]
        filtered = processor._filter_gears_by_polygon(gears)

        assert len(filtered) == 2

    def test_filter_gears_by_polygon(
        self,
        mock_source_client,
        mock_destination_client,
        test_polygon,
        gear_inside_polygon,
        gear_outside_polygon,
    ):
        """Test filtering gears by polygon."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[test_polygon],
        )

        gears = [gear_inside_polygon, gear_outside_polygon]
        filtered = processor._filter_gears_by_polygon(gears)

        assert len(filtered) == 1
        assert filtered[0].display_id == "GEAR-INSIDE"

    def test_filter_gears_by_polygon_trawl_one_device_inside(
        self, mock_source_client, mock_destination_client, test_polygon
    ):
        """Test trawl gear where only one device is inside polygon still passes."""
        trawl_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR-TRAWL",
            status="deployed",
            last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            devices=[
                BuoyDevice(
                    device_id="dev-1",
                    mfr_device_id="mfr-1",
                    label="Device 1",
                    location=DeviceLocation(latitude=45.0, longitude=-120.0),  # Inside
                    last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                    last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
                ),
                BuoyDevice(
                    device_id="dev-2",
                    mfr_device_id="mfr-2",
                    label="Device 2",
                    location=DeviceLocation(latitude=50.0, longitude=-130.0),  # Outside
                    last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                    last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
                ),
            ],
            type="trawl",
            manufacturer="TestManufacturer",
        )

        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[test_polygon],
        )

        assert processor._gear_is_inside_polygons(trawl_gear) is True

    @pytest.mark.asyncio
    async def test_process_with_polygon_filter(
        self,
        mock_source_client,
        mock_destination_client,
        test_polygon,
        gear_inside_polygon,
        gear_outside_polygon,
    ):
        """Test process method applies polygon filter."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[test_polygon],
        )

        mock_source_client.get_gears.side_effect = [
            [gear_inside_polygon, gear_outside_polygon],  # deployed
            [],  # hauled
        ]
        mock_destination_client.get_gears.side_effect = [[], []]  # deployed, hauled

        payloads = await processor.process()

        # Only gear inside polygon should be processed
        assert len(payloads) == 1
        assert payloads[0]["set_id"] == str(gear_inside_polygon.id)

    @pytest.mark.asyncio
    async def test_process_hauls_gear_moved_outside_polygon(
        self,
        mock_source_client,
        mock_destination_client,
        test_polygon,
        gear_inside_polygon,
        gear_outside_polygon,
    ):
        """Test that gear moving outside polygon gets hauled from destination."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[test_polygon],
        )

        # Gear is outside polygon in source, but exists deployed in destination
        mock_source_client.get_gears.side_effect = [
            [gear_outside_polygon],  # deployed
            [],  # hauled
        ]
        mock_destination_client.get_gears.side_effect = [
            [gear_outside_polygon.copy(deep=True)],  # deployed
            [],  # hauled
        ]

        payloads = await processor.process()

        # Should create haul payload for the gear that moved outside
        assert len(payloads) == 1
        assert payloads[0]["set_id"] == str(gear_outside_polygon.id)
        assert payloads[0]["devices"][0]["device_status"] == "hauled"

    @pytest.mark.asyncio
    async def test_process_hauls_gear_moved_outside_polygon_by_mfr_id(
        self, mock_source_client, mock_destination_client, test_polygon
    ):
        """Test gear matched by mfr_device_id is hauled when moved outside polygon."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[test_polygon],
        )

        # Source gear is outside polygon
        source_gear = BuoyGear(
            id=uuid4(),
            display_id="GEAR-SOURCE",
            status="deployed",
            last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            devices=[
                BuoyDevice(
                    device_id="dev-1",
                    mfr_device_id="shared-mfr-id",
                    label="Device 1",
                    location=DeviceLocation(latitude=50.0, longitude=-130.0),  # Outside
                    last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                    last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
                )
            ],
            type="single",
            manufacturer="TestManufacturer",
        )

        # Destination gear has different ID but same mfr_device_id
        dest_gear = BuoyGear(
            id=uuid4(),  # Different ID
            display_id="GEAR-DEST",
            status="deployed",
            last_updated=datetime(2024, 1, 14, 10, 0, 0, tzinfo=timezone.utc),
            devices=[
                BuoyDevice(
                    device_id="dev-dest",
                    mfr_device_id="shared-mfr-id",  # Same mfr_device_id
                    label="Device Dest",
                    location=DeviceLocation(
                        latitude=45.0, longitude=-120.0
                    ),  # Was inside
                    last_updated=datetime(2024, 1, 14, 10, 0, 0, tzinfo=timezone.utc),
                    last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
                )
            ],
            type="single",
            manufacturer="TestManufacturer",
        )

        mock_source_client.get_gears.side_effect = [
            [source_gear],
            [],
        ]  # deployed, hauled
        mock_destination_client.get_gears.side_effect = [
            [dest_gear],
            [],
        ]  # deployed, hauled

        payloads = await processor.process()

        # Should create haul payload targeting the destination gear's ID
        assert len(payloads) == 1
        assert payloads[0]["set_id"] == str(dest_gear.id)
        assert payloads[0]["devices"][0]["device_status"] == "hauled"

    @pytest.mark.asyncio
    async def test_process_does_not_haul_unrelated_dest_gear(
        self,
        mock_source_client,
        mock_destination_client,
        test_polygon,
        gear_inside_polygon,
    ):
        """Test that destination gears not matching any source gear are not hauled."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[test_polygon],
        )

        # Unrelated gear in destination (different ID and mfr_device_id)
        unrelated_dest_gear = BuoyGear(
            id=uuid4(),
            display_id="UNRELATED-GEAR",
            status="deployed",
            last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            devices=[
                BuoyDevice(
                    device_id="unrelated-dev",
                    mfr_device_id="unrelated-mfr-id",
                    label="Unrelated Device",
                    location=DeviceLocation(latitude=45.0, longitude=-120.0),
                    last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                    last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
                )
            ],
            type="single",
            manufacturer="OtherManufacturer",
        )

        mock_source_client.get_gears.side_effect = [
            [gear_inside_polygon],
            [],
        ]  # deployed, hauled
        mock_destination_client.get_gears.side_effect = [
            [unrelated_dest_gear],
            [],
        ]  # deployed, hauled

        payloads = await processor.process()

        # Should only deploy the gear inside polygon, not haul the unrelated one
        assert len(payloads) == 1
        assert payloads[0]["set_id"] == str(gear_inside_polygon.id)
        assert payloads[0]["devices"][0]["device_status"] == "deployed"

    @pytest.mark.asyncio
    async def test_process_no_haul_without_polygon_filter(
        self, mock_source_client, mock_destination_client
    ):
        """Test that without polygon filter, no haul happens for unmatched dest gears."""
        processor = Gear2GearProcessor(
            source_client=mock_source_client,
            destination_client=mock_destination_client,
            containing_shapes=[],  # No polygon filter
        )

        # Source has one gear
        source_gear = BuoyGear(
            id=uuid4(),
            display_id="SOURCE-GEAR",
            status="deployed",
            last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            devices=[
                BuoyDevice(
                    device_id="dev-1",
                    mfr_device_id="mfr-1",
                    label="Device 1",
                    location=DeviceLocation(latitude=45.0, longitude=-120.0),
                    last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                    last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
                )
            ],
            type="single",
            manufacturer="TestManufacturer",
        )

        # Destination has a different gear (not from source)
        dest_gear = BuoyGear(
            id=uuid4(),
            display_id="DEST-ONLY-GEAR",
            status="deployed",
            last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
            devices=[
                BuoyDevice(
                    device_id="dev-dest",
                    mfr_device_id="mfr-dest",
                    label="Dest Device",
                    location=DeviceLocation(latitude=46.0, longitude=-121.0),
                    last_updated=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
                    last_deployed=datetime(2024, 1, 10, 8, 0, 0, tzinfo=timezone.utc),
                )
            ],
            type="single",
            manufacturer="TestManufacturer",
        )

        mock_source_client.get_gears.side_effect = [
            [source_gear],
            [],
        ]  # deployed, hauled
        mock_destination_client.get_gears.side_effect = [
            [dest_gear],
            [],
        ]  # deployed, hauled

        payloads = await processor.process()

        # Should only deploy source gear, dest gear is unrelated and not hauled
        assert len(payloads) == 1
        assert payloads[0]["set_id"] == str(source_gear.id)
        assert payloads[0]["devices"][0]["device_status"] == "deployed"


class TestLoadPolygonsFromFeatureGroups:
    """Tests for the load_polygons_from_feature_groups helper function."""

    @pytest.fixture
    def mock_client(self, mocker) -> AsyncMock:
        """Mock BuoyClient."""
        return AsyncMock(spec=BuoyClient)

    @pytest.mark.asyncio
    async def test_load_empty_feature_group_ids(self, mock_client):
        """Test with empty feature group IDs list."""
        polygons = await load_polygons_from_feature_groups(
            client=mock_client,
            feature_group_ids=[],
        )

        assert polygons == []
        mock_client.get_feature_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_load_single_polygon(self, mock_client):
        """Test loading a single polygon from a feature group."""
        mock_client.get_feature_group.return_value = {
            "id": "fg-1",
            "features": [
                {
                    "id": "er-feature-1",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        polygons = await load_polygons_from_feature_groups(
            client=mock_client,
            feature_group_ids=["fg-1"],
        )

        assert len(polygons) == 1
        assert isinstance(polygons[0], Polygon)

    @pytest.mark.asyncio
    async def test_load_multipolygon(self, mock_client):
        """Test loading a MultiPolygon geometry."""
        mock_client.get_feature_group.return_value = {
            "id": "fg-1",
            "features": [
                {
                    "id": "er-feature-1",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "MultiPolygon",
                                "coordinates": [
                                    [[[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]],
                                    [
                                        [
                                            [20, 20],
                                            [20, 30],
                                            [30, 30],
                                            [30, 20],
                                            [20, 20],
                                        ]
                                    ],
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        polygons = await load_polygons_from_feature_groups(
            client=mock_client,
            feature_group_ids=["fg-1"],
        )

        assert len(polygons) == 1
        assert isinstance(polygons[0], MultiPolygon)

    @pytest.mark.asyncio
    async def test_load_ignores_non_polygon_geometries(self, mock_client):
        """Test that non-polygon geometries are ignored."""
        mock_client.get_feature_group.return_value = {
            "id": "fg-1",
            "features": [
                {
                    "id": "er-feature-1",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Point",
                                "coordinates": [10, 10],
                            },
                        },
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[0, 0], [10, 10]],
                            },
                        },
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]
                                ],
                            },
                        },
                    ],
                }
            ],
        }

        polygons = await load_polygons_from_feature_groups(
            client=mock_client,
            feature_group_ids=["fg-1"],
        )

        # Only the polygon should be included
        assert len(polygons) == 1
        assert isinstance(polygons[0], Polygon)

    @pytest.mark.asyncio
    async def test_load_multiple_feature_groups(self, mock_client):
        """Test loading from multiple feature groups."""
        fg1_data = {
            "id": "fg-1",
            "features": [
                {
                    "id": "er-feature-1",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        fg2_data = {
            "id": "fg-2",
            "features": [
                {
                    "id": "er-feature-2",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [[20, 20], [20, 30], [30, 30], [30, 20], [20, 20]]
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        mock_client.get_feature_group.side_effect = [fg1_data, fg2_data]

        polygons = await load_polygons_from_feature_groups(
            client=mock_client,
            feature_group_ids=["fg-1", "fg-2"],
        )

        assert len(polygons) == 2
        assert mock_client.get_feature_group.call_count == 2

    @pytest.mark.asyncio
    async def test_load_feature_group_not_found(self, mock_client):
        """Test that FeatureGroupNotFoundError is raised for missing groups."""
        mock_client.get_feature_group.side_effect = FeatureGroupNotFoundError(
            "Feature group 'missing-fg' not found"
        )

        with pytest.raises(FeatureGroupNotFoundError):
            await load_polygons_from_feature_groups(
                client=mock_client,
                feature_group_ids=["missing-fg"],
            )

    @pytest.mark.asyncio
    async def test_load_empty_features(self, mock_client):
        """Test loading from feature group with no features."""
        mock_client.get_feature_group.return_value = {
            "id": "fg-1",
            "features": [],
        }

        polygons = await load_polygons_from_feature_groups(
            client=mock_client,
            feature_group_ids=["fg-1"],
        )

        assert polygons == []

    @pytest.mark.asyncio
    async def test_load_handles_invalid_geometry(self, mock_client):
        """Test that invalid geometries are gracefully skipped."""
        mock_client.get_feature_group.return_value = {
            "id": "fg-1",
            "features": [
                {
                    "id": "er-feature-1",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": "invalid",  # Invalid coordinates
                            },
                        },
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [
                                    [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]
                                ],
                            },
                        },
                    ],
                }
            ],
        }

        # Should not raise, should skip invalid and keep valid
        polygons = await load_polygons_from_feature_groups(
            client=mock_client,
            feature_group_ids=["fg-1"],
        )

        assert len(polygons) == 1
