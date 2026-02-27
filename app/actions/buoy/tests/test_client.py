from unittest.mock import patch

import pytest

from app.actions.buoy import BuoyClient


class MockResponse:
    """Mock aiohttp response."""

    def __init__(self, status, json_data=None, text_data=None):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data or ""

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockSession:
    """Mock aiohttp ClientSession."""

    def __init__(self, responses=None):
        self.responses = responses or []
        self._call_index = 0
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self._call_index < len(self.responses):
            response = self.responses[self._call_index]
            self._call_index += 1
            return response
        raise Exception("No more responses configured")

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if self._call_index < len(self.responses):
            response = self.responses[self._call_index]
            self._call_index += 1
            return response
        raise Exception("No more responses configured")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestBuoyClient:
    """Tests for the BuoyClient class."""

    def test_init(self):
        """Test client initialization."""
        client = BuoyClient(er_token="test-token", er_site="https://test.pamdas.org/")

        assert client.er_token == "test-token"
        assert client.er_site == "https://test.pamdas.org"  # trailing slash stripped
        assert client.headers["Authorization"] == "Bearer test-token"

    def test_init_strips_trailing_slash(self):
        """Test that trailing slash is stripped from site URL."""
        client = BuoyClient(er_token="token", er_site="https://test.pamdas.org///")
        assert client.er_site == "https://test.pamdas.org"


class TestBuoyClientGetGears:
    """Tests for BuoyClient.get_gears method."""

    @pytest.mark.asyncio
    async def test_get_gears_success(self, sample_gear_api_response, sample_gear_data):
        """Test successful gear fetch."""
        mock_response = MockResponse(200, json_data=sample_gear_api_response)
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            gears = await client.get_gears()

            assert len(gears) == 1
            assert gears[0].display_id == sample_gear_data["display_id"]
            assert gears[0].status == "deployed"
            assert gears[0].manufacturer == "TestManufacturer"

    @pytest.mark.asyncio
    async def test_get_gears_with_status_filter(self, sample_gear_api_response):
        """Test gear fetch with status filter."""
        mock_response = MockResponse(200, json_data=sample_gear_api_response)
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            await client.get_gears(status="deployed")

            # Verify URL includes status parameter
            assert len(mock_session.get_calls) == 1
            url = mock_session.get_calls[0][0]
            assert "status=deployed" in url

    @pytest.mark.asyncio
    async def test_get_gears_empty_response(self):
        """Test gear fetch with empty results."""
        mock_response = MockResponse(
            200, json_data={"data": {"results": [], "next": None}}
        )
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            gears = await client.get_gears()

            assert gears == []

    @pytest.mark.asyncio
    async def test_get_gears_api_error(self):
        """Test gear fetch with API error."""
        mock_response = MockResponse(500, text_data="Internal Server Error")
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")

            with pytest.raises(RuntimeError) as exc_info:
                await client.get_gears()

            assert "Failed to fetch gear" in str(exc_info.value)
            assert "500" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_gears_pagination(self, sample_gear_data):
        """Test gear fetch with pagination."""
        page1_response = MockResponse(
            200,
            json_data={
                "data": {
                    "results": [sample_gear_data],
                    "next": "https://test.pamdas.org/api/v1.0/gear/?page=2",
                }
            },
        )

        gear2 = sample_gear_data.copy()
        gear2["id"] = "22222222-2222-2222-2222-222222222222"
        gear2["display_id"] = "GEAR-002"
        page2_response = MockResponse(
            200, json_data={"data": {"results": [gear2], "next": None}}
        )

        mock_session = MockSession(responses=[page1_response, page2_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            gears = await client.get_gears()

            assert len(gears) == 2
            assert gears[0].display_id == "GEAR-001"
            assert gears[1].display_id == "GEAR-002"


class TestBuoyClientGetGear:
    """Tests for BuoyClient.get_gear method (single gear by set_id)."""

    @pytest.mark.asyncio
    async def test_get_gear_success(self, sample_gear_data):
        """Test fetching a single gear by set_id."""
        set_id = sample_gear_data["id"]
        mock_response = MockResponse(200, json_data={"data": sample_gear_data})
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            gear = await client.get_gear(set_id)

            assert gear is not None
            assert gear.display_id == sample_gear_data["display_id"]
            assert str(gear.id) == set_id
            assert gear.status == "deployed"
            assert len(mock_session.get_calls) == 1
            url = mock_session.get_calls[0][0]
            assert f"/api/v1.0/gear/{set_id}/" in url

    @pytest.mark.asyncio
    async def test_get_gear_not_found(self):
        """Test get_gear returns None when gear does not exist (404)."""
        mock_response = MockResponse(404, text_data="Not found")
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            gear = await client.get_gear("00000000-0000-0000-0000-000000000000")

            assert gear is None

    @pytest.mark.asyncio
    async def test_get_gear_unwraps_data(self, sample_gear_data):
        """Test that response without 'data' wrapper is still parsed."""
        set_id = sample_gear_data["id"]
        mock_response = MockResponse(200, json_data=sample_gear_data)
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            gear = await client.get_gear(set_id)

            assert gear is not None
            assert str(gear.id) == set_id

    @pytest.mark.asyncio
    async def test_get_gear_api_error(self):
        """Test get_gear raises on non-404 API error."""
        mock_response = MockResponse(500, text_data="Internal Server Error")
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")

            with pytest.raises(RuntimeError) as exc_info:
                await client.get_gear("some-set-id")

            assert "Failed to fetch gear" in str(exc_info.value)
            assert "500" in str(exc_info.value)


class TestBuoyClientSendGear:
    """Tests for BuoyClient.send_gear method."""

    @pytest.mark.asyncio
    async def test_send_gear_success(self):
        """Test successful gear send."""
        mock_response = MockResponse(201, text_data='{"id": "new-gear-id"}')
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            result = await client.send_gear({"set_id": "test-set"})

            assert result["status"] == "success"
            assert result["status_code"] == 201

    @pytest.mark.asyncio
    async def test_send_gear_error(self):
        """Test gear send with error response."""
        mock_response = MockResponse(400, text_data='{"error": "Invalid payload"}')
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            result = await client.send_gear({"set_id": "test-set"})

            assert result["status"] == "error"
            assert result["status_code"] == 400

    @pytest.mark.asyncio
    async def test_send_gear_exception(self):
        """Test gear send with exception."""

        class FailingSession:
            def post(self, url, **kwargs):
                raise Exception("Connection failed")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("aiohttp.ClientSession", return_value=FailingSession()):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            result = await client.send_gear({"set_id": "test-set"})

            assert result["status"] == "error"
            assert "Connection failed" in result["error"]


class TestBuoyClientGetSources:
    """Tests for BuoyClient.get_sources method."""

    @pytest.mark.asyncio
    async def test_get_sources_success(self, sample_sources_api_response):
        """Test successful sources fetch."""
        mock_response = MockResponse(200, json_data=sample_sources_api_response)
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            sources = await client.get_sources()

            assert len(sources) == 2
            assert sources[0]["manufacturer_id"] == "mfr-001"

    @pytest.mark.asyncio
    async def test_get_sources_empty(self):
        """Test sources fetch with empty results."""
        mock_response = MockResponse(
            200, json_data={"data": {"results": [], "next": None}}
        )
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            sources = await client.get_sources()

            assert sources == []


class TestBuoyClientGetFeatureGroup:
    """Tests for BuoyClient.get_feature_group method."""

    @pytest.mark.asyncio
    async def test_get_feature_group_success(self):
        """Test successful feature group fetch."""
        feature_group_data = {
            "data": {
                "id": "fg-123",
                "name": "Test Feature Group",
                "features": [
                    {
                        "id": "feature-1",
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
        }
        mock_response = MockResponse(200, json_data=feature_group_data)
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            result = await client.get_feature_group("fg-123")

            assert result["id"] == "fg-123"
            assert result["name"] == "Test Feature Group"
            assert len(result["features"]) == 1

    @pytest.mark.asyncio
    async def test_get_feature_group_not_found(self):
        """Test feature group fetch returns 404."""
        from app.actions.buoy import FeatureGroupNotFoundError

        mock_response = MockResponse(404, text_data="Not found")
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")

            with pytest.raises(FeatureGroupNotFoundError) as exc_info:
                await client.get_feature_group("nonexistent-fg")

            assert "nonexistent-fg" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_feature_group_api_error(self):
        """Test feature group fetch with API error."""
        mock_response = MockResponse(500, text_data="Internal Server Error")
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")

            with pytest.raises(RuntimeError) as exc_info:
                await client.get_feature_group("fg-123")

            assert "Failed to fetch feature group" in str(exc_info.value)
            assert "500" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_feature_group_unwraps_data(self):
        """Test that the response data is unwrapped correctly."""
        # Response without 'data' wrapper
        feature_group_raw = {
            "id": "fg-456",
            "name": "Direct Response",
            "features": [],
        }
        mock_response = MockResponse(200, json_data=feature_group_raw)
        mock_session = MockSession(responses=[mock_response])

        with patch("aiohttp.ClientSession", return_value=mock_session):
            client = BuoyClient(er_token="token", er_site="https://test.pamdas.org")
            result = await client.get_feature_group("fg-456")

            assert result["id"] == "fg-456"
