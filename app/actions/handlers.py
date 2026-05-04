import asyncio
import json
import logging
from typing import Dict

import aiohttp
import httpx
import stamina
from gundi_client_v2 import GundiClient
from gundi_core.events import LogLevel
from gundi_core.schemas.v2 import Integration

from app.actions.buoy import BuoyClient, FeatureGroupNotFoundError
from app.actions.buoy.processor import (
    Gear2GearProcessor,
    load_polygons_from_feature_groups,
)
from app.actions.configurations import (
    Gear2GearAuthConfiguration,
    Gear2GearPullConfiguration,
)
from app.services.action_scheduler import crontab_schedule
from app.services.activity_logger import activity_logger, log_action_activity
from app.services.utils import find_config_for_action

logger = logging.getLogger(__name__)


async def _get_destination_client(integration_id: str) -> BuoyClient:
    async with GundiClient() as gundi:
        async for attempt in stamina.retry_context(on=httpx.HTTPError, wait_initial=1.0, wait_jitter=5.0, wait_max=32.0):
            with attempt:
                connection = await gundi.get_connection_details(integration_id)

    if not connection.destinations:
        raise ValueError(f"No destinations configured for integration {integration_id}")

    if len(connection.destinations) > 1:
        logger.warning(f"Integration {integration_id} has multiple destinations, using first one")

    destination = connection.destinations[0]

    async with GundiClient() as gundi:
        async for attempt in stamina.retry_context(on=httpx.HTTPError, wait_initial=1.0, wait_jitter=5.0, wait_max=32.0):
            with attempt:
                dest_integration = await gundi.get_integration_details(str(destination.id))

    dest_auth = dest_integration.get_action_config("auth")
    if not dest_auth:
        raise ValueError(f"No auth config on destination integration {destination.id}")
    dest_token = dest_auth.data.get("token")
    if not dest_token:
        raise ValueError(f"No token in auth config of destination integration {destination.id}")
    dest_base_url = str(destination.base_url)

    return BuoyClient(er_token=dest_token, er_site=dest_base_url)


@activity_logger()
async def action_auth(
    integration: Integration, action_config: Gear2GearAuthConfiguration
) -> Dict:
    """
    Validate authentication credentials for the source ER instance.
    """
    logger.info(f"Executing auth action for integration {integration.id}...")

    results = {
        "source_valid": False,
        "valid_credentials": False,
    }

    try:
        async with BuoyClient(
            er_token=action_config.source_token.get_secret_value(),
            er_site=str(action_config.source_url),
        ) as source_client:
            await source_client.get_gears(params={"page_size": 1})
        results["source_valid"] = True
        logger.info(f"Source ER connection successful")
    except Exception as e:
        logger.error(f"Source ER connection failed: {e}")
        results["source_error"] = str(e)

    results["valid_credentials"] = results["source_valid"]

    return results


@activity_logger()
@crontab_schedule(
    "*/3 * * * *"
)  # Fixed every 3 min; lookback window is configurable via action_config.lookback_minutes
async def action_pull_gear(
    integration: Integration, action_config: Gear2GearPullConfiguration
) -> Dict:
    """
    Sync gear data from source ER instance to destination ER instance.

    This action:
    - Fetches all gears from the source ER instance
    - Compares with the destination ER instance state
    - Deploys new gears, updates changed gears, and hauls removed gears
    - Preserves all IDs (device_id, set_id, mfr_device_id)
    """
    logger.info(f"Executing gear2gear sync for integration {integration.id}...")

    # Get auth config from integration configurations
    auth_config_data = find_config_for_action(
        configurations=integration.configurations, action_id="auth"
    )
    if not auth_config_data:
        raise ValueError("Missing auth configuration for gear2gear integration")

    auth_config = Gear2GearAuthConfiguration.parse_obj(auth_config_data.data)

    source_client = BuoyClient(
        er_token=auth_config.source_token.get_secret_value(),
        er_site=str(auth_config.source_url),
    )
    destination_client = await _get_destination_client(str(integration.id))
    try:
        return await _pull_gear(
            integration, action_config, source_client, destination_client
        )
    finally:
        await source_client.close()
        await destination_client.close()


async def _pull_gear(
    integration: Integration,
    action_config: Gear2GearPullConfiguration,
    source_client: BuoyClient,
    destination_client: BuoyClient,
) -> Dict:
    # Load polygon filters from feature groups if configured
    containing_shapes = []
    if action_config.feature_groups:
        feature_group_ids = [fg.id for fg in action_config.feature_groups]
        logger.info(f"Loading polygons from {len(feature_group_ids)} feature groups")
        try:
            containing_shapes = await load_polygons_from_feature_groups(
                client=source_client,
                feature_group_ids=feature_group_ids,
            )
            if not containing_shapes:
                raise ValueError(
                    f"Feature groups {feature_group_ids} "
                    "returned no polygons; aborting to avoid "
                    "unfiltered full sync"
                )
        except FeatureGroupNotFoundError as e:
            logger.error(f"Feature group not found: {e}")
            raise

    # Process the sync
    processor = Gear2GearProcessor(
        source_client=source_client,
        destination_client=destination_client,
        containing_shapes=containing_shapes,
        lookback_minutes=action_config.lookback_minutes,
    )
    integration_id = str(integration.id)
    process_result = await processor.process()
    gear_payloads = process_result.payloads
    discovery_title = (
        f"Evaluated {process_result.filtered_count} gears: "
        f"{process_result.source_count} in source, "
        f"{process_result.filtered_count} inside polygon, "
        f"{process_result.dest_count} already in destination"
    )
    logger.info(f"[{integration_id}] {discovery_title}")
    try:
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_gear",
            level=LogLevel.INFO,
            title=discovery_title,
            data={
                "source_count": process_result.source_count,
                "filtered_count": process_result.filtered_count,
                "dest_count": process_result.dest_count,
                "to_deploy": process_result.deploy_count,
                "to_update": process_result.update_count,
                "to_haul": process_result.haul_count,
            },
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"[{integration_id}] Failed to publish discovery activity log: {e}")

    # Send gear payloads to destination
    success_count = 0
    failure_count = 0
    failed_payloads = []

    for idx, payload in enumerate(gear_payloads):
        send_result = await destination_client.send_gear(payload)
        if send_result.get("status") == "success":
            success_count += 1
            logger.info(
                f"Successfully sent gear {idx + 1}/{len(gear_payloads)} to destination"
            )
        elif send_result.get("status_code") == 409:
            # Gear already exists (e.g. duplicate observation); retry as update
            set_id = payload.get("set_id")
            if not set_id:
                failure_count += 1
                failed_payloads.append(
                    {"index": idx, "error": "409 without set_id in payload"}
                )
                continue
            try:
                update_payload = await processor.resolve_conflict(set_id)
                if update_payload is None:
                    success_count += 1
                    logger.info(
                        f"Gear {idx + 1}/{len(gear_payloads)} (set_id={set_id}) "
                        "already exists and is in sync after 409"
                    )
                    continue
                retry_result = await destination_client.send_gear(update_payload)
                if retry_result.get("status") == "success":
                    success_count += 1
                    logger.info(
                        f"Gear {idx + 1}/{len(gear_payloads)} (set_id={set_id}) "
                        "already existed; sent as update after 409"
                    )
                else:
                    failure_count += 1
                    error_info = retry_result.get("error") or retry_result.get(
                        "response", "Unknown error"
                    )
                    failed_payloads.append({"index": idx, "error": error_info})
            except Exception as e:
                logger.exception(f"Failed to retry gear {set_id} as update after 409")
                failure_count += 1
                failed_payloads.append({"index": idx, "error": str(e)})
        else:
            failure_count += 1
            error_info = send_result.get("error") or send_result.get("response", "Unknown error")
            logger.error(
                f"Failed to send gear {idx + 1}/{len(gear_payloads)} "
                f"(set_id={payload.get('set_id')}) "
                f"to destination: {error_info}"
            )
            logger.debug(
                f"Full payload for failed gear: {json.dumps(payload, default=str)}"
            )
            failed_payloads.append({"index": idx, "error": error_info})

    # Log activity
    log_level = LogLevel.INFO if failure_count == 0 else LogLevel.WARNING
    title = (
        f"Sync complete: {success_count} sent "
        f"({process_result.deploy_count} to deploy, "
        f"{process_result.update_count} to update, "
        f"{process_result.haul_count} to haul)"
    )
    if failure_count > 0:
        title += f" ({failure_count} failed)"
    logger.info(f"[{integration_id}] {title}")
    try:
        await log_action_activity(
            integration_id=integration_id,
            action_id="pull_gear",
            level=log_level,
            title=title,
            data={
                "total": len(gear_payloads),
                "success": success_count,
                "failures": failure_count,
            },
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"[{integration_id}] Failed to publish sync activity log: {e}")

    return {
        "total_payloads": len(gear_payloads),
        "success": success_count,
        "failures": failure_count,
        "failed_payloads": failed_payloads if failed_payloads else None,
    }
