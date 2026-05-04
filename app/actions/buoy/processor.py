import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple, Union

from shapely.geometry import MultiPolygon, Point, Polygon, shape

from .client import BuoyClient, FeatureGroupNotFoundError
from .types import BuoyGear

logger = logging.getLogger(__name__)

# Type alias for polygon shapes
PolygonShape = Union[Polygon, MultiPolygon]

GEAR_API_PAGE_SIZE = 500


class ProcessResult(NamedTuple):
    payloads: List[Dict[str, Any]]
    source_count: int
    filtered_count: int
    dest_count: int
    deploy_count: int
    update_count: int
    haul_count: int


class ProcessResult(NamedTuple):
    payloads: List[Dict[str, Any]]
    source_count: int
    filtered_count: int
    dest_count: int
    deploy_count: int
    update_count: int
    haul_count: int


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
        lookback_minutes: Optional[int] = None,
    ):
        """
        Initialize a Gear2GearProcessor instance.

        Args:
            source_client: BuoyClient configured for the source ER instance.
            destination_client: BuoyClient configured for the destination ER instance.
            containing_shapes: Optional list of shapely Polygon/MultiPolygon objects.
                If provided, only gears with at least one device inside these
                polygons will be synced.
            lookback_minutes: If set, only fetch source gears updated
                within this many minutes. If None, fetch all gears.
        """
        self._source_client = source_client
        self._destination_client = destination_client
        self._containing_shapes = containing_shapes or []
        self._lookback_minutes = lookback_minutes

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

    def _safe_recorded_at(self, source_gear: BuoyGear, dest_gear: BuoyGear) -> datetime:
        """
        Pick a recorded_at that won't invert ER's assigned_range.

        ER stores each device's deployment as a tstzrange whose lower bound is
        the device's last_deployed. An incoming observation closes the range at
        recorded_at, so recorded_at must be strictly greater than every
        last_deployed we know about for this gear — on either side of the sync.
        Otherwise Postgres raises "range lower bound must be less than or equal
        to range upper bound" and the request 500s.
        """
        deploys = [
            d.last_deployed or d.last_updated
            for d in list(source_gear.devices) + list(dest_gear.devices)
        ]
        deploys = [d for d in deploys if d is not None]
        latest_deploy = max(deploys) if deploys else None

        recorded_at = source_gear.last_updated
        if latest_deploy and recorded_at <= latest_deploy:
            recorded_at = latest_deploy + timedelta(seconds=1)
        return recorded_at

    @staticmethod
    def _deduplicate_gears(
        gears: List[BuoyGear],
    ) -> List[BuoyGear]:
        """
        Remove duplicate gears, keeping the last value per ID.

        Duplicates can occur if a gear appears in both the
        deployed and hauled API responses during a status
        transition. The last value for a given gear ID
        overwrites earlier ones, which preserves the hauled
        copy when deployed + hauled are concatenated in that
        order. The list order reflects the first time each
        gear ID was seen.

        Args:
            gears: List of gears, possibly with duplicates.

        Returns:
            Deduplicated list ordered by first occurrence of
            each gear ID, with last value retained.
        """
        seen: Dict[str, BuoyGear] = {}
        duplicate_count = 0
        for gear in gears:
            gear_id = str(gear.id)
            if gear_id in seen:
                duplicate_count += 1
                logger.debug(
                    "Duplicate gear %s (%s): " "keeping status=%s over %s",
                    gear.display_id,
                    gear_id,
                    gear.status,
                    seen[gear_id].status,
                )
            seen[gear_id] = gear
        if duplicate_count:
            logger.info(
                "Deduplicated %d gear(s) that appeared " "in multiple status responses",
                duplicate_count,
            )
        return list(seen.values())

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

    def create_update_payload(
        self, source_gear: BuoyGear, dest_gear: BuoyGear
    ) -> Dict[str, Any]:
        """
        Create an update payload for an existing gear.

        Uses the gear-level last_updated as recorded_at so the
        observation timestamp reflects the status/data change,
        avoiding 409 conflicts when device-level timestamps
        haven't changed since the previous sync. The timestamp
        is clamped past the latest known deployment on either
        side — see _safe_recorded_at.

        Args:
            source_gear: The gear from the source ER instance.
            dest_gear: The existing gear in the destination.

        Returns:
            Dict in the format expected by /api/v1.0/gear/ POST endpoint.
        """
        recorded_at = self._safe_recorded_at(source_gear, dest_gear)
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
                "recorded_at": self._remove_milliseconds(recorded_at).isoformat(),
                "device_status": source_gear.status,
                "location": {
                    "latitude": device.location.latitude,
                    "longitude": device.location.longitude,
                },
            }
            devices.append(device_payload)

        payload = {
            "set_id": str(dest_gear.id),
            "manufacturer_name": source_gear.manufacturer,
            "deployment_type": source_gear.type,
            "devices": devices,
        }

        return payload

    def _create_haul_payload(
        self, source_gear: BuoyGear, dest_gear: BuoyGear
    ) -> Dict[str, Any]:
        """
        Create a haul payload from a source gear that is hauled.

        Uses the gear-level last_updated as recorded_at so the
        observation timestamp reflects the haul event, avoiding
        409 conflicts when device-level timestamps haven't
        changed since the previous sync. The timestamp is
        clamped past the latest known deployment on either side
        — see _safe_recorded_at.

        Args:
            source_gear: The hauled gear from the source ER instance.
            dest_gear: The existing gear in the destination.

        Returns:
            Dict in the format expected by /api/v1.0/gear/ POST endpoint.
        """
        recorded_at = self._safe_recorded_at(source_gear, dest_gear)
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
                "recorded_at": self._remove_milliseconds(recorded_at).isoformat(),
                "device_status": "hauled",
                "location": {
                    "latitude": device.location.latitude,
                    "longitude": device.location.longitude,
                },
            }
            devices.append(haul_device)

        payload = {
            "set_id": str(dest_gear.id),
            "manufacturer_name": source_gear.manufacturer,
            "deployment_type": source_gear.type,
            "devices": devices,
        }

        return payload

    async def resolve_conflict(self, set_id: str) -> Optional[Dict[str, Any]]:
        """
        Resolve a 409 conflict for a gear that already exists in the destination.

        Fetches the gear from both source and destination, compares them, and
        returns an update payload if needed.

        Args:
            set_id: The gear set ID from the original deploy payload.

        Returns:
            An update payload dict if the gear needs updating, or None if
            the gear is already in sync or couldn't be fetched.
        """
        dest_gear = await self._destination_client.get_gear(set_id)
        source_gear = await self._source_client.get_gear(set_id)

        if dest_gear is None or source_gear is None:
            raise RuntimeError(
                f"409 for gear {set_id}: could not fetch for conflict resolution "
                f"(dest={dest_gear is not None}, source={source_gear is not None})"
            )

        if not self.needs_update(source_gear, dest_gear):
            logger.info(
                f"Gear set_id={set_id} already exists and is in sync; "
                "no update needed after 409"
            )
            return None

        return self.create_update_payload(source_gear, dest_gear)

    def needs_update(self, source_gear: BuoyGear, dest_gear: BuoyGear) -> bool:
        """
        Determine if a destination gear needs to be updated based on source gear.

        Args:
            source_gear: The gear from the source ER instance.
            dest_gear: The existing gear in the destination.

        Returns:
            True if the destination gear needs updating.
        """
        # A status change always needs to be synced, even backwards in time.
        if source_gear.status != dest_gear.status:
            return True

        # Hauled is terminal on the destination side: Buoy ER rejects
        # re-haul with 400 "Device X is already hauled". Once both sides
        # agree the gear is hauled, there's nothing left to push.
        if source_gear.status == "hauled":
            return False

        # Otherwise only push an update when the source has strictly newer
        # data. If the destination is newer (or equal), the source has
        # nothing to contribute and we'd risk overwriting fresher dest
        # state — or, when the dest has since been re-deployed with a
        # last_deployed past source.last_updated, triggering a 500 from
        # ER's assigned_range constructor ("range lower bound must be
        # less than or equal to range upper bound").
        return source_gear.last_updated > dest_gear.last_updated

    def _identify_sync_actions(
        self,
        source_gears: List[BuoyGear],
        dest_gears: List[BuoyGear],
        all_source_gears: Optional[List[BuoyGear]] = None,
    ) -> Tuple[
        List[BuoyGear], List[Tuple[BuoyGear, BuoyGear]], List[Tuple[BuoyGear, BuoyGear]]
    ]:
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
                - to_haul: Tuples of (source_gear, dest_gear) that are hauled or moved outside polygon
        """
        to_deploy: List[BuoyGear] = []
        to_update: List[Tuple[BuoyGear, BuoyGear]] = []
        to_haul: List[Tuple[BuoyGear, BuoyGear]] = []

        # IDs are preserved end-to-end: a gear's set_id and each device_id are
        # written through from source to destination on deploy and reused on
        # update/haul. Match strictly by set_id — falling back to
        # mfr_device_id lets a new source gear hijack an unrelated dest gear
        # that happens to share a physical device (e.g. a hauled gear that's
        # been redeployed in source under a new set_id).
        dest_gear_by_id: Dict[str, BuoyGear] = {
            str(gear.id): gear for gear in dest_gears
        }

        # Track which destination gears we've processed
        processed_dest_gear_ids: Set[str] = set()

        for source_gear in source_gears:
            source_id = str(source_gear.id)
            dest_gear = dest_gear_by_id.get(source_id)

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
                    to_haul.append((source_gear, dest_gear))
                    logger.info(
                        f"Gear {source_gear.display_id} marked for haul "
                        f"(hauled in source, deployed in destination)"
                    )
                elif self.needs_update(source_gear, dest_gear):
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
            all_source_by_id: Dict[str, BuoyGear] = {
                str(gear.id): gear for gear in all_source_gears
            }

            for dest_gear in dest_gears:
                dest_id = str(dest_gear.id)

                # Skip if already processed or not deployed
                if dest_id in processed_dest_gear_ids:
                    continue
                if dest_gear.status != "deployed":
                    continue

                matching_source = all_source_by_id.get(dest_id)
                if matching_source is not None:
                    # This gear exists in source but wasn't in filtered list
                    # It means the gear moved outside the polygon - haul it
                    to_haul.append((matching_source, dest_gear))
                    logger.info(
                        f"Gear {matching_source.display_id} marked for haul "
                        f"(moved outside polygon filter)"
                    )

        logger.info(
            f"Sync actions: {len(to_deploy)} to deploy, "
            f"{len(to_update)} to update, {len(to_haul)} to haul"
        )

        return to_deploy, to_update, to_haul

    async def process(self) -> ProcessResult:
        """
        Process gear sync from source to destination.

        This method:
            1. Fetches all gears from source ER instance.
            2. Optionally filters gears by polygon boundaries.
            3. Fetches all gears from destination ER instance.
            4. Compares and identifies gears to deploy, update, or haul.
            5. Creates payloads for each action.

        Returns:
            ProcessResult with gear payloads and sync counts.
        """
        # Build source query params with optional lookback window
        source_params: Dict[str, Any] = {"page_size": GEAR_API_PAGE_SIZE}
        if self._lookback_minutes:
            updated_since = datetime.now(timezone.utc) - timedelta(
                minutes=self._lookback_minutes
            )
            source_params["updated_since"] = updated_since.isoformat()
            logger.info(
                "Using lookback window: %d min " "(updated_since=%s)",
                self._lookback_minutes,
                source_params["updated_since"],
            )

        logger.info("Fetching gears from source ER instance...")
        deployed_source_gears = await self._source_client.get_gears(
            params=dict(source_params), status="deployed"
        )
        hauled_source_gears = await self._source_client.get_gears(
            params=dict(source_params), status="hauled"
        )
        all_source_gears = self._deduplicate_gears(
            deployed_source_gears + hauled_source_gears
        )
        logger.info(
            f"Found {len(all_source_gears)} gears in source "
            f"({len(deployed_source_gears)} deployed, "
            f"{len(hauled_source_gears)} hauled)"
        )

        # Apply polygon filter if configured
        filtered_source_gears = all_source_gears
        if self._containing_shapes:
            filtered_source_gears = self._filter_gears_by_polygon(all_source_gears)

        # Destination always fetches all gears for full comparison
        logger.info("Fetching gears from destination ER instance...")
        deployed_dest_gears = await self._destination_client.get_gears(
            params={"page_size": GEAR_API_PAGE_SIZE}, status="deployed"
        )
        hauled_dest_gears = await self._destination_client.get_gears(
            params={"page_size": GEAR_API_PAGE_SIZE}, status="hauled"
        )
        dest_gears = self._deduplicate_gears(deployed_dest_gears + hauled_dest_gears)
        logger.info(
            f"Found {len(dest_gears)} gears in destination "
            f"({len(deployed_dest_gears)} deployed, "
            f"{len(hauled_dest_gears)} hauled)"
        )

        # Pass both filtered and unfiltered source gears to detect gears that moved outside polygon
        to_deploy, to_update, to_haul = self._identify_sync_actions(
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
                payload = self.create_update_payload(source_gear, dest_gear)
                gear_payloads.append(payload)
                logger.info(f"Created update payload for {source_gear.display_id}")
            except Exception as e:
                logger.exception(
                    f"Failed to create update payload for {source_gear.display_id}: {e}"
                )

        # Process hauls
        for source_gear, dest_gear in to_haul:
            try:
                payload = self._create_haul_payload(source_gear, dest_gear)
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

        return ProcessResult(
            payloads=gear_payloads,
            source_count=len(all_source_gears),
            filtered_count=len(filtered_source_gears),
            dest_count=len(dest_gears),
            deploy_count=len(to_deploy),
            update_count=len(to_update),
            haul_count=len(to_haul),
        )


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
