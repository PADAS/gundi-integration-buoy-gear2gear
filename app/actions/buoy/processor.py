import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from shapely.geometry import MultiPolygon, Point, Polygon, shape

from .client import BuoyClient, FeatureGroupNotFoundError
from .types import BuoyGear

logger = logging.getLogger(__name__)

# Type alias for polygon shapes
PolygonShape = Union[Polygon, MultiPolygon]


class Gear2GearProcessor:
    """
    Processor for syncing gear data between two EarthRanger instances.

    This processor:
    - Reads all gears from a source ER instance
    - Compares with destination ER instance state
    - Creates deploy/update/haul payloads to sync destination with source
    - Preserves all IDs (device_id, set_id, mfr_device_id)
    - Optionally filters gears by polygon boundaries
    """

    def __init__(
        self,
        source_client: BuoyClient,
        destination_client: BuoyClient,
        containing_shapes: Optional[List[PolygonShape]] = None,
    ):
        """
        Initialize a Gear2GearProcessor instance.

        Args:
            source_client: BuoyClient configured for the source ER instance.
            destination_client: BuoyClient configured for the destination ER instance.
            containing_shapes: Optional list of shapely Polygon/MultiPolygon objects.
                If provided, only gears with at least one device inside these
                polygons will be synced.
        """
        self._source_client = source_client
        self._destination_client = destination_client
        self._containing_shapes = containing_shapes or []

    def _gear_is_inside_polygons(self, gear: BuoyGear) -> bool:
        """
        Check if any device in the gear is located inside the containing polygons.

        A gear is considered "inside" if at least one of its devices has a
        location that falls within any of the configured polygons.

        Args:
            gear: The gear to check.

        Returns:
            True if any device is inside any polygon, False otherwise.
        """
        if not self._containing_shapes:
            # No polygon filter configured, all gears pass
            return True

        for device in gear.devices:
            if device.location.latitude is None or device.location.longitude is None:
                continue

            point = Point(device.location.longitude, device.location.latitude)

            for polygon in self._containing_shapes:
                if polygon.covers(point):
                    logger.debug(
                        f"Device {device.mfr_device_id} at "
                        f"({device.location.latitude}, {device.location.longitude}) "
                        f"is inside a configured polygon"
                    )
                    return True

        return False

    def _filter_gears_by_polygon(self, gears: List[BuoyGear]) -> List[BuoyGear]:
        """
        Filter gears to only include those with devices inside the configured polygons.

        Args:
            gears: List of gears to filter.

        Returns:
            Filtered list of gears.
        """
        if not self._containing_shapes:
            return gears

        filtered_gears = []
        for gear in gears:
            if self._gear_is_inside_polygons(gear):
                filtered_gears.append(gear)
            else:
                logger.debug(
                    f"Gear {gear.display_id} filtered out - "
                    f"no devices inside configured polygons"
                )

        logger.info(
            f"Polygon filter: {len(filtered_gears)} of {len(gears)} gears "
            f"have devices inside configured polygons"
        )

        return filtered_gears

    def _remove_milliseconds(self, dt: datetime) -> datetime:
        """Remove milliseconds from a datetime object."""
        return dt.replace(microsecond=0)

    def _create_deploy_payload(self, source_gear: BuoyGear) -> Dict[str, Any]:
        """
        Create a deployment payload from a source gear.

        Preserves all IDs from the source gear.

        Args:
            source_gear: The gear from the source ER instance.

        Returns:
            Dict in the format expected by /api/v1.0/gear/ POST endpoint.
        """
        devices = []

        for device in source_gear.devices:
            device_payload = {
                "device_id": device.device_id,
                "mfr_device_id": device.mfr_device_id,
                "last_deployed": (
                    self._remove_milliseconds(device.last_deployed).isoformat()
                    if device.last_deployed
                    else self._remove_milliseconds(device.last_updated).isoformat()
                ),
                "last_updated": self._remove_milliseconds(
                    device.last_updated
                ).isoformat(),
                "recorded_at": self._remove_milliseconds(
                    device.last_deployed or device.last_updated
                ).isoformat(),
                "device_status": source_gear.status,
                "location": {
                    "latitude": device.location.latitude,
                    "longitude": device.location.longitude,
                },
            }
            devices.append(device_payload)

        # Find the earliest deployment date for initial_deployment_date
        deployment_dates = [
            d.last_deployed or d.last_updated for d in source_gear.devices
        ]
        initial_deployment = min(deployment_dates) if deployment_dates else None

        payload = {
            "set_id": str(source_gear.id),
            "manufacturer_name": source_gear.manufacturer,
            "deployment_type": source_gear.type,
            "devices_in_set": len(devices),
            "devices": devices,
        }

        if initial_deployment:
            payload["initial_deployment_date"] = self._remove_milliseconds(
                initial_deployment
            ).isoformat()

        return payload

    def _create_update_payload(
        self, source_gear: BuoyGear, dest_gear: BuoyGear
    ) -> Dict[str, Any]:
        """
        Create an update payload for an existing gear.

        Args:
            source_gear: The gear from the source ER instance.
            dest_gear: The existing gear in the destination.

        Returns:
            Dict in the format expected by /api/v1.0/gear/ POST endpoint.
        """
        devices = []

        for device in source_gear.devices:
            device_payload = {
                "device_id": device.device_id,
                "mfr_device_id": device.mfr_device_id,
                "last_deployed": (
                    self._remove_milliseconds(device.last_deployed).isoformat()
                    if device.last_deployed
                    else self._remove_milliseconds(device.last_updated).isoformat()
                ),
                "last_updated": self._remove_milliseconds(
                    device.last_updated
                ).isoformat(),
                "recorded_at": self._remove_milliseconds(
                    device.last_updated
                ).isoformat(),
                "device_status": source_gear.status,
                "location": {
                    "latitude": device.location.latitude,
                    "longitude": device.location.longitude,
                },
            }
            devices.append(device_payload)

        payload = {
            "set_id": str(source_gear.id),
            "manufacturer_name": source_gear.manufacturer,
            "deployment_type": source_gear.type,
            "devices": devices,
        }

        return payload

    def _create_haul_payload(self, source_gear: BuoyGear) -> Dict[str, Any]:
        """
        Create a haul payload from a source gear that is hauled.

        Args:
            source_gear: The hauled gear from the source ER instance.

        Returns:
            Dict in the format expected by /api/v1.0/gear/ POST endpoint.
        """
        devices = []

        for device in source_gear.devices:
            haul_device = {
                "device_id": device.device_id,
                "mfr_device_id": device.mfr_device_id,
                "last_deployed": (
                    self._remove_milliseconds(device.last_deployed).isoformat()
                    if device.last_deployed
                    else self._remove_milliseconds(device.last_updated).isoformat()
                ),
                "last_updated": self._remove_milliseconds(
                    device.last_updated
                ).isoformat(),
                "recorded_at": self._remove_milliseconds(
                    device.last_updated
                ).isoformat(),
                "device_status": "hauled",
                "location": {
                    "latitude": device.location.latitude,
                    "longitude": device.location.longitude,
                },
            }
            devices.append(haul_device)

        payload = {
            "set_id": str(source_gear.id),
            "manufacturer_name": source_gear.manufacturer,
            "deployment_type": source_gear.type,
            "devices": devices,
        }

        return payload

    def _needs_update(self, source_gear: BuoyGear, dest_gear: BuoyGear) -> bool:
        """
        Determine if a destination gear needs to be updated based on source gear.

        Args:
            source_gear: The gear from the source ER instance.
            dest_gear: The existing gear in the destination.

        Returns:
            True if the destination gear needs updating.
        """
        # Check if status changed
        if source_gear.status != dest_gear.status:
            return True

        # Check if source has newer data
        if source_gear.last_updated > dest_gear.last_updated:
            return True

        # Check if any device locations changed
        source_locations = {
            d.mfr_device_id: (d.location.latitude, d.location.longitude)
            for d in source_gear.devices
        }
        dest_locations = {
            d.mfr_device_id: (d.location.latitude, d.location.longitude)
            for d in dest_gear.devices
        }

        if source_locations != dest_locations:
            return True

        return False

    async def _identify_sync_actions(
        self,
        source_gears: List[BuoyGear],
        dest_gears: List[BuoyGear],
        all_source_gears: Optional[List[BuoyGear]] = None,
    ) -> Tuple[List[BuoyGear], List[Tuple[BuoyGear, BuoyGear]], List[BuoyGear]]:
        """
        Identify which gears need to be deployed, updated, or hauled.

        Args:
            source_gears: Gears from source ER instance (possibly filtered by polygon).
            dest_gears: All gears from the destination ER instance.
            all_source_gears: All gears from source before polygon filtering. Used to
                detect gears that moved outside the polygon and should be hauled.

        Returns:
            Tuple of (to_deploy, to_update, to_haul) where:
                - to_deploy: Source gears not in destination
                - to_update: Tuples of (source_gear, dest_gear) needing update
                - to_haul: Source gears that are hauled or moved outside polygon
        """
        to_deploy: List[BuoyGear] = []
        to_update: List[Tuple[BuoyGear, BuoyGear]] = []
        to_haul: List[BuoyGear] = []

        # Build lookup by gear ID
        dest_gear_by_id: Dict[str, BuoyGear] = {
            str(gear.id): gear for gear in dest_gears
        }

        # Also build lookup by mfr_device_id for matching
        dest_gear_by_mfr_id: Dict[str, BuoyGear] = {}
        for gear in dest_gears:
            for device in gear.devices:
                dest_gear_by_mfr_id[device.mfr_device_id] = gear

        # Track which destination gears we've processed
        processed_dest_gear_ids: Set[str] = set()

        for source_gear in source_gears:
            source_id = str(source_gear.id)

            # Try to find matching destination gear by ID first
            dest_gear = dest_gear_by_id.get(source_id)

            # If not found by ID, try by mfr_device_id
            if dest_gear is None:
                for device in source_gear.devices:
                    dest_gear = dest_gear_by_mfr_id.get(device.mfr_device_id)
                    if dest_gear:
                        break

            if dest_gear is None:
                # Gear doesn't exist in destination
                if source_gear.status == "deployed":
                    to_deploy.append(source_gear)
                    logger.info(
                        f"Gear {source_gear.display_id} ({source_id}) marked for deployment "
                        f"(not in destination, deployed in source)"
                    )
                else:
                    logger.info(
                        f"Gear {source_gear.display_id} ({source_id}) skipped "
                        f"(not in destination, status={source_gear.status} in source)"
                    )
            else:
                # Mark this destination gear as processed
                processed_dest_gear_ids.add(str(dest_gear.id))

                # Gear exists in destination
                if source_gear.status == "hauled" and dest_gear.status == "deployed":
                    # Source is hauled but destination still shows deployed
                    to_haul.append(source_gear)
                    logger.info(
                        f"Gear {source_gear.display_id} marked for haul "
                        f"(hauled in source, deployed in destination)"
                    )
                elif self._needs_update(source_gear, dest_gear):
                    to_update.append((source_gear, dest_gear))
                    logger.info(
                        f"Gear {source_gear.display_id} marked for update "
                        f"(newer data or location change in source)"
                    )
                else:
                    logger.debug(
                        f"Gear {source_gear.display_id} skipped (no changes detected)"
                    )

        # Check for destination gears that moved outside the polygon
        # Only do this if we have polygon filtering (all_source_gears provided)
        if all_source_gears is not None:
            # Build lookup for all source gears (unfiltered)
            all_source_by_id: Dict[str, BuoyGear] = {
                str(gear.id): gear for gear in all_source_gears
            }
            all_source_by_mfr_id: Dict[str, BuoyGear] = {}
            for gear in all_source_gears:
                for device in gear.devices:
                    all_source_by_mfr_id[device.mfr_device_id] = gear

            for dest_gear in dest_gears:
                dest_id = str(dest_gear.id)

                # Skip if already processed or not deployed
                if dest_id in processed_dest_gear_ids:
                    continue
                if dest_gear.status != "deployed":
                    continue

                # Check if this destination gear matches any source gear
                matching_source = all_source_by_id.get(dest_id)
                if matching_source is None:
                    for device in dest_gear.devices:
                        matching_source = all_source_by_mfr_id.get(device.mfr_device_id)
                        if matching_source:
                            break

                if matching_source is not None:
                    # This gear exists in source but wasn't in filtered list
                    # It means the gear moved outside the polygon - haul it
                    to_haul.append(matching_source)
                    logger.info(
                        f"Gear {matching_source.display_id} marked for haul "
                        f"(moved outside polygon filter)"
                    )

        logger.info(
            f"Sync actions: {len(to_deploy)} to deploy, "
            f"{len(to_update)} to update, {len(to_haul)} to haul"
        )

        return to_deploy, to_update, to_haul

    async def process(self) -> List[Dict[str, Any]]:
        """
        Process gear sync from source to destination.

        This method:
            1. Fetches all gears from source ER instance.
            2. Optionally filters gears by polygon boundaries.
            3. Fetches all gears from destination ER instance.
            4. Compares and identifies gears to deploy, update, or haul.
            5. Creates payloads for each action.

        Returns:
            List of gear payloads ready to be sent to the destination Buoy API.
        """
        logger.info("Fetching gears from source ER instance...")
        all_source_gears = await self._source_client.get_gears(
            params={"page_size": 10000}
        )
        logger.info(f"Found {len(all_source_gears)} gears in source")

        # Apply polygon filter if configured
        filtered_source_gears = all_source_gears
        if self._containing_shapes:
            filtered_source_gears = self._filter_gears_by_polygon(all_source_gears)

        logger.info("Fetching gears from destination ER instance...")
        dest_gears = await self._destination_client.get_gears(
            params={"page_size": 10000}
        )
        logger.info(f"Found {len(dest_gears)} gears in destination")

        # Pass both filtered and unfiltered source gears to detect gears that moved outside polygon
        to_deploy, to_update, to_haul = await self._identify_sync_actions(
            source_gears=filtered_source_gears,
            dest_gears=dest_gears,
            all_source_gears=all_source_gears if self._containing_shapes else None,
        )

        gear_payloads = []

        # Process deployments
        for source_gear in to_deploy:
            try:
                payload = self._create_deploy_payload(source_gear)
                gear_payloads.append(payload)
                logger.info(f"Created deployment payload for {source_gear.display_id}")
            except Exception as e:
                logger.exception(
                    f"Failed to create deployment payload for {source_gear.display_id}: {e}"
                )

        # Process updates
        for source_gear, dest_gear in to_update:
            try:
                payload = self._create_update_payload(source_gear, dest_gear)
                gear_payloads.append(payload)
                logger.info(f"Created update payload for {source_gear.display_id}")
            except Exception as e:
                logger.exception(
                    f"Failed to create update payload for {source_gear.display_id}: {e}"
                )

        # Process hauls
        for source_gear in to_haul:
            try:
                payload = self._create_haul_payload(source_gear)
                gear_payloads.append(payload)
                logger.info(f"Created haul payload for {source_gear.display_id}")
            except Exception as e:
                logger.exception(
                    f"Failed to create haul payload for {source_gear.display_id}: {e}"
                )

        logger.info(f"Generated {len(gear_payloads)} gear payload(s)")
        logger.debug(
            "Full gear payloads: %s",
            json.dumps(gear_payloads, indent=2, default=str),
        )

        return gear_payloads


async def load_polygons_from_feature_groups(
    client: BuoyClient,
    feature_group_ids: List[str],
) -> List[PolygonShape]:
    """
    Load polygon shapes from EarthRanger feature groups.

    Fetches each feature group and extracts all Polygon and MultiPolygon
    geometries from their features.

    Args:
        client: BuoyClient configured for the ER instance with the feature groups.
        feature_group_ids: List of feature group UUIDs to load.

    Returns:
        List of shapely Polygon/MultiPolygon objects.

    Raises:
        FeatureGroupNotFoundError: If a feature group doesn't exist.
        RuntimeError: For other API errors.
    """
    containing_shapes: List[PolygonShape] = []

    for feature_group_id in feature_group_ids:
        logger.info(f"Loading feature group: {feature_group_id}")

        try:
            fg_data = await client.get_feature_group(feature_group_id)
        except FeatureGroupNotFoundError:
            logger.error(f"Feature group '{feature_group_id}' not found")
            raise

        # Extract polygons from feature group
        # Structure: fg_data["features"] contains ER features, each with "features" (GeoJSON)
        for er_feature in fg_data.get("features", []):
            for geojson_feature in er_feature.get("features", []):
                if "geometry" in geojson_feature:
                    try:
                        polygon = shape(geojson_feature["geometry"])
                        # Only accept Polygon and MultiPolygon geometries
                        if isinstance(polygon, (Polygon, MultiPolygon)):
                            containing_shapes.append(polygon)
                            logger.debug(
                                f"Added polygon from feature group {feature_group_id}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to parse geometry in feature group "
                            f"{feature_group_id}: {e}"
                        )

    if not containing_shapes and feature_group_ids:
        logger.warning(
            f"No valid polygon features found in the configured feature groups: "
            f"{feature_group_ids}"
        )

    logger.info(
        f"Loaded {len(containing_shapes)} polygons from "
        f"{len(feature_group_ids)} feature groups"
    )

    return containing_shapes
