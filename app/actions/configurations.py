from typing import List, Optional

import pydantic

from .core import (
    AuthActionConfiguration,
    ExecutableActionMixin,
    PullActionConfiguration,
)


class FeatureGroup(pydantic.BaseModel):
    """Reference to an EarthRanger feature group for polygon filtering."""

    id: str = pydantic.Field(
        ...,
        title="Feature Group ID",
        description="The UUID of the feature group in the source EarthRanger instance.",
    )


class Gear2GearAuthConfiguration(AuthActionConfiguration, ExecutableActionMixin):
    """Authentication configuration for gear2gear sync.

    Contains credentials for both source and destination ER instances.
    """

    source_token: pydantic.SecretStr = pydantic.Field(
        ...,
        title="Source ER API Token",
        description="API token for the source EarthRanger instance.",
        format="password",
    )
    source_url: pydantic.AnyHttpUrl = pydantic.Field(
        ...,
        title="Source ER URL",
        description="Base URL for the source EarthRanger instance (e.g., https://source.pamdas.org/).",
    )
    destination_token: pydantic.SecretStr = pydantic.Field(
        ...,
        title="Destination ER API Token",
        description="API token for the destination EarthRanger instance.",
        format="password",
    )
    destination_url: pydantic.AnyHttpUrl = pydantic.Field(
        ...,
        title="Destination ER URL",
        description="Base URL for the destination EarthRanger instance (e.g., https://dest.pamdas.org/).",
    )


class Gear2GearPullConfiguration(PullActionConfiguration):
    """Configuration for the gear2gear pull action."""

    sync_interval_minutes: int = pydantic.Field(
        default=5,
        title="Sync Interval (minutes)",
        description="How often to sync gear data between instances.",
        ge=1,
        le=60,
    )
    feature_groups: Optional[List[FeatureGroup]] = pydantic.Field(
        default=None,
        title="Feature Groups",
        description=(
            "Optional list of feature group IDs from the source ER instance. "
            "If specified, only gears with devices located inside these polygon "
            "features will be synced. Leave empty to sync all gears."
        ),
    )
