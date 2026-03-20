import json
import logging
from datetime import timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .types import BuoyGear

logger = logging.getLogger(__name__)


class BuoyClient:
    """Client for interacting with EarthRanger's Gear API."""

    DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)

    def __init__(self, er_token: str, er_site: str):
        self.er_token = er_token
        self.er_site = er_site.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {self.er_token}",
        }
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=self.DEFAULT_TIMEOUT,
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def get_gears(
        self,
        params: Optional[dict] = None,
        status: Optional[str] = None,
    ) -> List[BuoyGear]:
        """
        Fetch all gears from the Buoy Gear API.

        Args:
            params: Optional query parameters.
            status: Optional status filter (e.g., 'deployed', 'hauled').

        Returns:
            List of BuoyGear objects.
        """
        url = f"{self.er_site}/api/v1.0/gear/?include_empty_location=true"
        if status:
            url += f"&status={status}"

        items = []
        session = await self._get_session()

        while url:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    body = await response.text()
                    raise RuntimeError(
                        f"Failed to fetch gear from Buoy Gear API. "
                        f"Status code: {response.status} Body: {body}"
                    )

                data = await response.json()

                if "data" not in data:
                    raise RuntimeError(
                        f"Unexpected response structure from Buoy Gear API: "
                        f"missing 'data' field. Response: {data}"
                    )

                page_data = data["data"]

                if "results" not in page_data:
                    raise RuntimeError(
                        f"Unexpected response structure from Buoy Gear API: "
                        f"missing 'results' field. Response: {page_data}"
                    )

                results = page_data["results"]
                items.extend(results)
                url = page_data.get("next")
                params = None  # Clear params for subsequent pages

        if len(items) == 0:
            logger.info("No gears found in Buoy API")

        gears = []
        for item in items:
            try:
                gear = BuoyGear.parse_obj(item)
                gear.last_updated = gear.last_updated.astimezone(timezone.utc)
                gears.append(gear)
            except Exception as e:
                logger.error(f"Error parsing gear item: {e} (item: {json.dumps(item)})")
                raise

        return gears

    async def get_gear(self, set_id: str) -> Optional[BuoyGear]:
        """
        Fetch a single gear by set ID from the Buoy Gear API.

        Use this to check if a gearset already exists in the destination
        before sending, or to compare source vs destination and decide
        if an update payload is needed.

        Args:
            set_id: The gear set ID (UUID string).

        Returns:
            BuoyGear if found, None if the gear does not exist (404).
        """
        url = f"{self.er_site}/api/v1.0/gear/{set_id}/"
        session = await self._get_session()

        async with session.get(url) as response:
            if response.status == 404:
                logger.debug(f"Gear set_id={set_id} not found in API")
                return None
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(
                    f"Failed to fetch gear set_id={set_id}. "
                    f"Status code: {response.status} Body: {body}"
                )

            data = await response.json()
            if "data" in data:
                item = data["data"]
            else:
                item = data

            gear = BuoyGear.parse_obj(item)
            gear.last_updated = gear.last_updated.astimezone(timezone.utc)
            return gear

    async def send_gear(self, gear_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send gear payload to the Buoy API POST endpoint.

        Args:
            gear_payload: The gear payload in the format expected by /api/v1.0/gear/

        Returns:
            Dict containing the API response.
        """
        url = f"{self.er_site}/api/v1.0/gear/"
        session = await self._get_session()

        try:
            async with session.post(url, json=gear_payload) as response:
                response_text = await response.text()
                if response.status in [200, 201]:
                    logger.info(
                        f"Successfully sent gear to Buoy API: {response.status}"
                    )
                    return {
                        "status": "success",
                        "status_code": response.status,
                        "response": response_text,
                    }
                else:
                    logger.error(
                        f"Failed to send gear to Buoy API. "
                        f"Status: {response.status}, Response: {response_text}"
                    )
                    return {
                        "status": "error",
                        "status_code": response.status,
                        "response": response_text,
                    }
        except Exception as e:
            logger.exception("Exception while sending gear to Buoy API")
            return {"status": "error", "error": str(e)}

    async def get_sources(self, params: Optional[dict] = None) -> List[Dict[str, Any]]:
        """
        Get all sources from the Buoy API with pagination support.

        Args:
            params: Optional query parameters for the request.

        Returns:
            List of source dictionaries.
        """
        url = f"{self.er_site}/api/v1.0/sources/"
        sources = []
        session = await self._get_session()

        while url:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    body = await response.text()
                    raise RuntimeError(
                        f"Failed to fetch sources. "
                        f"Status code: {response.status} Body: {body}"
                    )

                data = await response.json()

                if "data" not in data:
                    raise RuntimeError(
                        f"Unexpected response structure from sources API: "
                        f"missing 'data' field. Response: {data}"
                    )

                page_data = data["data"]

                if "results" not in page_data:
                    raise RuntimeError(
                        f"Unexpected response structure from sources API: "
                        f"missing 'results' field. Response: {page_data}"
                    )

                results = page_data["results"]
                sources.extend(results)
                url = page_data.get("next")
                params = None

        if len(sources) == 0:
            logger.warning("No sources found")

        return sources

    async def get_feature_group(self, feature_group_id: str) -> Dict[str, Any]:
        """
        Fetch a feature group by ID from the EarthRanger API.

        Args:
            feature_group_id: The UUID of the feature group.

        Returns:
            Dict containing the feature group data with its features.

        Raises:
            FeatureGroupNotFoundError: If the feature group doesn't exist.
            RuntimeError: For other API errors.
        """
        url = f"{self.er_site}/api/v1.0/spatialfeaturegroup/{feature_group_id}/"
        session = await self._get_session()

        async with session.get(url) as response:
            if response.status == 404:
                raise FeatureGroupNotFoundError(
                    f"Feature group '{feature_group_id}' not found"
                )
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(
                    f"Failed to fetch feature group. "
                    f"Status code: {response.status} Body: {body}"
                )

            data = await response.json()

            # ER API wraps response in 'data' field
            if "data" in data:
                return data["data"]
            return data


class FeatureGroupNotFoundError(Exception):
    """Raised when a feature group is not found in EarthRanger."""
