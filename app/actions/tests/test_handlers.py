from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from shapely.geometry import Polygon

from app.actions.configurations import (
    FeatureGroup,
    Gear2GearAuthConfiguration,
    Gear2GearPullConfiguration,
)
from app.actions.handlers import action_auth, action_pull_gear


class TestActionAuth:
    """Tests for the action_auth handler."""

    @pytest.mark.asyncio
    async def test_auth_success(self, integration_v2_gear2gear, gear2gear_auth_config):
        """Test successful authentication to both source and destination."""
        with patch("app.actions.handlers.BuoyClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get_gears.return_value = []
            mock_client.__aenter__.return_value = mock_client
            mock_client_class.return_value = mock_client

            result = await action_auth(
                integration=integration_v2_gear2gear,
                action_config=gear2gear_auth_config,
            )

            assert result["valid_credentials"] is True
            assert result["source_valid"] is True
            assert result["destination_valid"] is True

            # Should have created two clients (source and destination)
            assert mock_client_class.call_count == 2

    @pytest.mark.asyncio
    async def test_auth_source_failure(
        self, integration_v2_gear2gear, gear2gear_auth_config
    ):
        """Test authentication failure on source."""
        with patch("app.actions.handlers.BuoyClient") as mock_client_class:
            mock_source = AsyncMock()
            mock_source.get_gears.side_effect = Exception("Source connection failed")
            mock_source.__aenter__.return_value = mock_source

            mock_dest = AsyncMock()
            mock_dest.get_gears.return_value = []
            mock_dest.__aenter__.return_value = mock_dest

            mock_client_class.side_effect = [mock_source, mock_dest]

            result = await action_auth(
                integration=integration_v2_gear2gear,
                action_config=gear2gear_auth_config,
            )

            assert result["valid_credentials"] is False
            assert result["source_valid"] is False
            assert result["destination_valid"] is True
            assert "Source connection failed" in result["source_error"]

    @pytest.mark.asyncio
    async def test_auth_destination_failure(
        self, integration_v2_gear2gear, gear2gear_auth_config
    ):
        """Test authentication failure on destination."""
        with patch("app.actions.handlers.BuoyClient") as mock_client_class:
            mock_source = AsyncMock()
            mock_source.get_gears.return_value = []
            mock_source.__aenter__.return_value = mock_source

            mock_dest = AsyncMock()
            mock_dest.get_gears.side_effect = Exception("Destination connection failed")
            mock_dest.__aenter__.return_value = mock_dest

            mock_client_class.side_effect = [mock_source, mock_dest]

            result = await action_auth(
                integration=integration_v2_gear2gear,
                action_config=gear2gear_auth_config,
            )

            assert result["valid_credentials"] is False
            assert result["source_valid"] is True
            assert result["destination_valid"] is False
            assert "Destination connection failed" in result["destination_error"]

    @pytest.mark.asyncio
    async def test_auth_both_failure(
        self, integration_v2_gear2gear, gear2gear_auth_config
    ):
        """Test authentication failure on both endpoints."""
        with patch("app.actions.handlers.BuoyClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get_gears.side_effect = Exception("Connection failed")
            mock_client.__aenter__.return_value = mock_client
            mock_client_class.return_value = mock_client

            result = await action_auth(
                integration=integration_v2_gear2gear,
                action_config=gear2gear_auth_config,
            )

            assert result["valid_credentials"] is False
            assert result["source_valid"] is False
            assert result["destination_valid"] is False


class TestActionPullGear:
    """Tests for the action_pull_gear handler."""

    @pytest.mark.asyncio
    async def test_pull_gear_success(
        self,
        integration_v2_gear2gear,
        gear2gear_pull_config,
        mock_log_action_activity,
        mock_publish_event,
    ):
        """Test successful gear sync."""
        with patch("app.actions.handlers.BuoyClient") as mock_client_class, patch(
            "app.actions.handlers.Gear2GearProcessor"
        ) as mock_processor_class:
            # Setup mock client
            mock_client = AsyncMock()
            mock_client.get_gears.return_value = []
            mock_client.send_gear.return_value = {
                "status": "success",
                "status_code": 201,
            }
            mock_client_class.return_value = mock_client

            # Setup mock processor
            mock_processor = AsyncMock()
            mock_processor.process.return_value = [
                {
                    "set_id": str(uuid4()),
                    "manufacturer_name": "Test",
                    "devices": [],
                }
            ]
            mock_processor_class.return_value = mock_processor

            result = await action_pull_gear(
                integration=integration_v2_gear2gear,
                action_config=gear2gear_pull_config,
            )

            assert result["total_payloads"] == 1
            assert result["success"] == 1
            assert result["failures"] == 0
            assert result["failed_payloads"] is None

            # Verify processor was called
            mock_processor.process.assert_called_once()

            # Verify gear was sent
            mock_client.send_gear.assert_called_once()

    @pytest.mark.asyncio
    async def test_pull_gear_with_failures(
        self,
        integration_v2_gear2gear,
        gear2gear_pull_config,
        mock_log_action_activity,
        mock_publish_event,
    ):
        """Test gear sync with some failures."""
        with patch("app.actions.handlers.BuoyClient") as mock_client_class, patch(
            "app.actions.handlers.Gear2GearProcessor"
        ) as mock_processor_class:
            mock_client = AsyncMock()
            mock_client.get_gears.return_value = []
            # First send succeeds, second fails
            mock_client.send_gear.side_effect = [
                {"status": "success", "status_code": 201},
                {"status": "error", "status_code": 400, "response": "Invalid payload"},
            ]
            mock_client_class.return_value = mock_client

            mock_processor = AsyncMock()
            mock_processor.process.return_value = [
                {"set_id": str(uuid4()), "manufacturer_name": "Test", "devices": []},
                {"set_id": str(uuid4()), "manufacturer_name": "Test", "devices": []},
            ]
            mock_processor_class.return_value = mock_processor

            result = await action_pull_gear(
                integration=integration_v2_gear2gear,
                action_config=gear2gear_pull_config,
            )

            assert result["total_payloads"] == 2
            assert result["success"] == 1
            assert result["failures"] == 1
            assert result["failed_payloads"] is not None
            assert len(result["failed_payloads"]) == 1

    @pytest.mark.asyncio
    async def test_pull_gear_no_payloads(
        self,
        integration_v2_gear2gear,
        gear2gear_pull_config,
        mock_log_action_activity,
        mock_publish_event,
    ):
        """Test gear sync with no payloads (nothing to sync)."""
        with patch("app.actions.handlers.BuoyClient") as mock_client_class, patch(
            "app.actions.handlers.Gear2GearProcessor"
        ) as mock_processor_class:
            mock_client = AsyncMock()
            mock_client.get_gears.return_value = []
            mock_client_class.return_value = mock_client

            mock_processor = AsyncMock()
            mock_processor.process.return_value = []
            mock_processor_class.return_value = mock_processor

            result = await action_pull_gear(
                integration=integration_v2_gear2gear,
                action_config=gear2gear_pull_config,
            )

            assert result["total_payloads"] == 0
            assert result["success"] == 0
            assert result["failures"] == 0

            # send_gear should not be called if no payloads
            mock_client.send_gear.assert_not_called()

    @pytest.mark.asyncio
    async def test_pull_gear_missing_auth_config(
        self,
        gear2gear_pull_config,
        mock_log_action_activity,
        mock_publish_event,
    ):
        """Test that missing auth config raises ValueError."""
        from gundi_core.schemas.v2 import Integration

        # Create integration without auth config
        integration = Integration(
            id=uuid4(),
            name="No Auth Config",
            base_url="https://test.pamdas.org",
            enabled=True,
            type={
                "id": str(uuid4()),
                "name": "Gear2Gear",
                "value": "gear2gear",
                "actions": [],
            },
            owner={"id": str(uuid4()), "name": "Test"},
            configurations=[],  # No configurations
        )

        with pytest.raises(ValueError) as exc_info:
            await action_pull_gear(
                integration=integration,
                action_config=gear2gear_pull_config,
            )

        assert "Missing auth configuration" in str(exc_info.value)


class TestConfigurationModels:
    """Tests for configuration models."""

    def test_auth_config_creation(self):
        """Test creating auth configuration."""
        config = Gear2GearAuthConfiguration(
            source_token="src-token",
            source_url="https://source.er.org/",
            destination_token="dst-token",
            destination_url="https://dest.er.org/",
        )

        assert config.source_token.get_secret_value() == "src-token"
        assert str(config.source_url) == "https://source.er.org/"
        assert config.destination_token.get_secret_value() == "dst-token"
        assert str(config.destination_url) == "https://dest.er.org/"

    def test_pull_config_defaults(self):
        """Test pull configuration defaults."""
        config = Gear2GearPullConfiguration()

        assert config.sync_interval_minutes == 5

    def test_pull_config_custom_interval(self):
        """Test pull configuration with custom interval."""
        config = Gear2GearPullConfiguration(sync_interval_minutes=10)

        assert config.sync_interval_minutes == 10

    def test_pull_config_validation(self):
        """Test pull configuration validation."""
        from pydantic import ValidationError

        # Interval too low
        with pytest.raises(ValidationError):
            Gear2GearPullConfiguration(sync_interval_minutes=0)

        # Interval too high
        with pytest.raises(ValidationError):
            Gear2GearPullConfiguration(sync_interval_minutes=100)

    def test_pull_config_with_feature_groups(self):
        """Test pull configuration with feature groups."""
        config = Gear2GearPullConfiguration(
            feature_groups=[
                FeatureGroup(id="fg-1"),
                FeatureGroup(id="fg-2"),
            ]
        )

        assert config.feature_groups is not None
        assert len(config.feature_groups) == 2
        assert config.feature_groups[0].id == "fg-1"


class TestActionPullGearWithPolygonFiltering:
    """Tests for polygon filtering in the action_pull_gear handler."""

    @pytest.mark.asyncio
    async def test_pull_gear_with_feature_groups(
        self,
        integration_v2_gear2gear,
        mock_log_action_activity,
        mock_publish_event,
    ):
        """Test gear sync loads polygons from feature groups."""
        pull_config = Gear2GearPullConfiguration(
            feature_groups=[FeatureGroup(id="fg-123")]
        )

        with patch("app.actions.handlers.BuoyClient") as mock_client_class, patch(
            "app.actions.handlers.Gear2GearProcessor"
        ) as mock_processor_class, patch(
            "app.actions.handlers.load_polygons_from_feature_groups"
        ) as mock_load_polygons:
            # Setup mock client
            mock_client = AsyncMock()
            mock_client.get_gears.return_value = []
            mock_client_class.return_value = mock_client

            # Setup mock polygon loader
            mock_polygon = Polygon([(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)])
            mock_load_polygons.return_value = [mock_polygon]

            # Setup mock processor
            mock_processor = AsyncMock()
            mock_processor.process.return_value = []
            mock_processor_class.return_value = mock_processor

            await action_pull_gear(
                integration=integration_v2_gear2gear,
                action_config=pull_config,
            )

            # Verify load_polygons_from_feature_groups was called
            mock_load_polygons.assert_called_once()
            call_kwargs = mock_load_polygons.call_args.kwargs
            assert call_kwargs["feature_group_ids"] == ["fg-123"]

            # Verify processor was created with containing_shapes
            mock_processor_class.assert_called_once()
            processor_kwargs = mock_processor_class.call_args.kwargs
            assert "containing_shapes" in processor_kwargs
            assert processor_kwargs["containing_shapes"] == [mock_polygon]

    @pytest.mark.asyncio
    async def test_pull_gear_without_feature_groups(
        self,
        integration_v2_gear2gear,
        gear2gear_pull_config,
        mock_log_action_activity,
        mock_publish_event,
    ):
        """Test gear sync without feature groups configured."""
        with patch("app.actions.handlers.BuoyClient") as mock_client_class, patch(
            "app.actions.handlers.Gear2GearProcessor"
        ) as mock_processor_class, patch(
            "app.actions.handlers.load_polygons_from_feature_groups"
        ) as mock_load_polygons:
            mock_client = AsyncMock()
            mock_client.get_gears.return_value = []
            mock_client_class.return_value = mock_client

            mock_processor = AsyncMock()
            mock_processor.process.return_value = []
            mock_processor_class.return_value = mock_processor

            await action_pull_gear(
                integration=integration_v2_gear2gear,
                action_config=gear2gear_pull_config,
            )

            # load_polygons should not be called when no feature groups
            mock_load_polygons.assert_not_called()

            # Processor should be created with empty containing_shapes
            mock_processor_class.assert_called_once()
            processor_kwargs = mock_processor_class.call_args.kwargs
            assert processor_kwargs["containing_shapes"] == []

    @pytest.mark.asyncio
    async def test_pull_gear_feature_group_not_found(
        self,
        integration_v2_gear2gear,
        mock_log_action_activity,
        mock_publish_event,
    ):
        """Test gear sync raises error when feature group not found."""
        from app.actions.buoy import FeatureGroupNotFoundError

        pull_config = Gear2GearPullConfiguration(
            feature_groups=[FeatureGroup(id="nonexistent-fg")]
        )

        with patch("app.actions.handlers.BuoyClient") as mock_client_class, patch(
            "app.actions.handlers.load_polygons_from_feature_groups"
        ) as mock_load_polygons:
            mock_client = AsyncMock()
            mock_client_class.return_value = mock_client

            mock_load_polygons.side_effect = FeatureGroupNotFoundError(
                "Feature group 'nonexistent-fg' not found"
            )

            with pytest.raises(FeatureGroupNotFoundError):
                await action_pull_gear(
                    integration=integration_v2_gear2gear,
                    action_config=pull_config,
                )
