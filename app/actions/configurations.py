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

    Contains credentials for the source ER instance. Destination credentials
    are retrieved from the Gundi connection's destination integration.
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


class Gear2GearPullConfiguration(PullActionConfiguration):
    """Configuration for the gear2gear pull action."""

    lookback_minutes: int = pydantic.Field(
        default=60,
        title="Lookback Window (minutes)",
        description=(
            "Only fetch gears updated in the last N minutes from the "
            "source. The action runs on a fixed 3-minute cron schedule, "
            "so this should exceed 3 minutes to provide a safety margin "
            "for missed or delayed runs."
        ),
        ge=1,
        le=43200,
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
