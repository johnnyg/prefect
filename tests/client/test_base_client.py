from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, List, Tuple
from unittest import mock

import httpx
import pytest
from httpx import AsyncClient, Request, Response
from starlette import status

import prefect
import prefect.client
import prefect.client.constants
from prefect.client.base import (
    PrefectHttpxAsyncClient,
    PrefectResponse,
    ServerType,
    determine_server_type,
)
from prefect.client.schemas.objects import CsrfToken
from prefect.exceptions import PrefectHTTPStatusError
from prefect.settings import (
    PREFECT_API_URL,
    PREFECT_CLIENT_CUSTOM_HEADERS,
    PREFECT_CLIENT_MAX_RETRIES,
    PREFECT_CLIENT_RETRY_EXTRA_CODES,
    PREFECT_CLIENT_RETRY_JITTER_FACTOR,
    PREFECT_CLOUD_API_URL,
    PREFECT_SERVER_ALLOW_EPHEMERAL_MODE,
    temporary_settings,
)
from prefect.testing.utilities import AsyncMock

now = datetime.now(timezone.utc)

RESPONSE_429_RETRY_AFTER_0 = Response(
    status.HTTP_429_TOO_MANY_REQUESTS,
    headers={"Retry-After": "0"},
    request=Request("a test request", "fake.url/fake/route"),
)

RESPONSE_429_RETRY_AFTER_MISSING = Response(
    status.HTTP_429_TOO_MANY_REQUESTS,
    request=Request("a test request", "fake.url/fake/route"),
)


RESPONSE_200 = Response(
    status.HTTP_200_OK,
    request=Request("a test request", "fake.url/fake/route"),
)

RESPONSE_CSRF = Response(
    status.HTTP_200_OK,
    json=CsrfToken(
        client="test_client", token="test_token", expiration=now + timedelta(days=1)
    ).model_dump(mode="json", exclude_unset=True),
    request=Request("a test request", "fake.url/fake/route"),
)

RESPONSE_400 = Response(
    status.HTTP_400_BAD_REQUEST,
    json={"detail": "You done bad things"},
    request=Request("a test request", "fake.url/fake/route"),
)

RESPONSE_404 = Response(
    status.HTTP_404_NOT_FOUND,
    request=Request("a test request", "fake.url/fake/route"),
)

RESPONSE_CSRF_DISABLED = Response(
    status.HTTP_422_UNPROCESSABLE_ENTITY,
    json={"detail": "CSRF protection is disabled."},
    request=Request("a test request", "fake.url/fake/route"),
)

RESPONSE_INVALID_TOKEN = Response(
    status_code=status.HTTP_403_FORBIDDEN,
    json={"detail": "Invalid CSRF token or client identifier."},
    request=Request("a test request", "fake.url/fake/route"),
)


@pytest.fixture
def disable_jitter():
    with temporary_settings({PREFECT_CLIENT_RETRY_JITTER_FACTOR: 0}):
        yield


class TestPrefectHttpxAsyncClient:
    @pytest.mark.usefixtures("mock_anyio_sleep", "disable_jitter")
    @pytest.mark.parametrize(
        "error_code",
        [
            status.HTTP_408_REQUEST_TIMEOUT,
            status.HTTP_429_TOO_MANY_REQUESTS,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            status.HTTP_502_BAD_GATEWAY,
        ],
    )
    async def test_prefect_httpx_client_retries_on_designated_error_codes(
        self, monkeypatch, error_code, caplog
    ):
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)
        client = PrefectHttpxAsyncClient()
        retry_response = Response(
            error_code,
            request=Request("a test request", "fake.url/fake/route"),
        )
        base_client_send.side_effect = [
            retry_response,
            retry_response,
            retry_response,
            RESPONSE_200,
        ]
        async with client:
            response = await client.post(
                url="fake.url/fake/route", data={"evenmorefake": "data"}
            )
        assert response.status_code == status.HTTP_200_OK
        assert base_client_send.call_count == 4

        for code, delay, attempt in [
            (error_code, 2, 1),
            (error_code, 4, 2),
            (error_code, 8, 3),
        ]:
            assert f"Received response with retryable status code {code}" in caplog.text
            assert f"Another attempt will be made in {delay}s" in caplog.text
            assert f"This is attempt {attempt}/6" in caplog.text

    @pytest.mark.usefixtures("mock_anyio_sleep", "disable_jitter")
    @pytest.mark.parametrize(
        "error_code,extra_codes",
        [
            (status.HTTP_508_LOOP_DETECTED, "508"),
            (status.HTTP_409_CONFLICT, "508,409"),
        ],
    )
    async def test_prefect_httpx_client_retries_on_extra_error_codes(
        self, monkeypatch, error_code, extra_codes, caplog
    ):
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)
        client = PrefectHttpxAsyncClient()
        retry_response = Response(
            error_code,
            request=Request("a test request", "fake.url/fake/route"),
        )
        base_client_send.side_effect = [
            retry_response,
            retry_response,
            retry_response,
            RESPONSE_200,
        ]
        with temporary_settings({PREFECT_CLIENT_RETRY_EXTRA_CODES: extra_codes}):
            async with client:
                response = await client.post(
                    url="fake.url/fake/route", data={"evenmorefake": "data"}
                )
        assert response.status_code == status.HTTP_200_OK
        assert base_client_send.call_count == 4

        for code, delay, attempt in [
            (error_code, 2, 1),
            (error_code, 4, 2),
            (error_code, 8, 3),
        ]:
            assert f"Received response with retryable status code {code}" in caplog.text
            assert f"Another attempt will be made in {delay}s" in caplog.text
            assert f"This is attempt {attempt}/6" in caplog.text

    @pytest.mark.usefixtures("mock_anyio_sleep", "disable_jitter")
    async def test_prefect_httpx_client_raises_on_non_extra_error_codes(
        self, monkeypatch, caplog
    ):
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)
        client = PrefectHttpxAsyncClient()
        retry_response = Response(
            status.HTTP_508_LOOP_DETECTED,
            request=Request("a test request", "fake.url/fake/route"),
        )
        base_client_send.side_effect = [
            retry_response,
            retry_response,
            retry_response,
            RESPONSE_200,
        ]
        with temporary_settings({PREFECT_CLIENT_RETRY_EXTRA_CODES: "409"}):
            with pytest.raises(PrefectHTTPStatusError):
                async with client:
                    await client.post(
                        url="fake.url/fake/route", data={"evenmorefake": "data"}
                    )

    @pytest.mark.usefixtures("mock_anyio_sleep", "disable_jitter")
    @pytest.mark.parametrize(
        "exception_type",
        [
            httpx.RemoteProtocolError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.LocalProtocolError,
            httpx.PoolTimeout,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
        ],
    )
    async def test_prefect_httpx_client_retries_on_designated_exceptions(
        self,
        monkeypatch,
        exception_type,
        caplog,
    ):
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)
        client = PrefectHttpxAsyncClient()

        base_client_send.side_effect = [
            exception_type("test"),
            exception_type("test"),
            exception_type("test"),
            RESPONSE_200,
        ]
        async with client:
            response = await client.post(
                url="fake.url/fake/route", data={"evenmorefake": "data"}
            )
        assert response.status_code == status.HTTP_200_OK
        assert base_client_send.call_count == 4

        # We log on retry
        assert "Encountered retryable exception during request" in caplog.text
        assert "Another attempt will be made in 2s" in caplog.text
        assert "This is attempt 1/6" in caplog.text

        # The traceback should be included
        assert "Traceback" in caplog.text

        # Ensure the messaging changes
        assert "Another attempt will be made in 4s" in caplog.text
        assert "This is attempt 2/6" in caplog.text

    @pytest.mark.usefixtures("mock_anyio_sleep")
    @pytest.mark.parametrize(
        "response_or_exc",
        [RESPONSE_429_RETRY_AFTER_0, httpx.RemoteProtocolError("test")],
    )
    async def test_prefect_httpx_client_retries_up_to_five_times(
        self,
        monkeypatch,
        response_or_exc,
    ):
        client = PrefectHttpxAsyncClient()
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        # Return more than 6 retryable responses
        base_client_send.side_effect = [response_or_exc] * 10

        with pytest.raises(Exception):
            async with client:
                await client.post(
                    url="fake.url/fake/route",
                    data={"evenmorefake": "data"},
                )

        # 5 retries + 1 first attempt
        assert base_client_send.call_count == 6

    @pytest.mark.usefixtures("mock_anyio_sleep")
    @pytest.mark.parametrize(
        "response_or_exc",
        [RESPONSE_429_RETRY_AFTER_0, httpx.RemoteProtocolError("test")],
    )
    async def test_prefect_httpx_client_respects_max_retry_setting(
        self,
        monkeypatch,
        response_or_exc,
    ):
        client = PrefectHttpxAsyncClient()
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        # Return more than 10 retryable responses
        base_client_send.side_effect = [response_or_exc] * 20

        with pytest.raises(Exception):
            with temporary_settings({PREFECT_CLIENT_MAX_RETRIES: 10}):
                async with client:
                    await client.post(
                        url="fake.url/fake/route",
                        data={"evenmorefake": "data"},
                    )

        # 10 retries + 1 first attempt
        assert base_client_send.call_count == 11

    @pytest.mark.usefixtures("mock_anyio_sleep")
    @pytest.mark.parametrize(
        "final_response,expected_error_type",
        [
            (
                RESPONSE_429_RETRY_AFTER_0,
                httpx.HTTPStatusError,
            ),
            (httpx.RemoteProtocolError("test"), httpx.RemoteProtocolError),
        ],
    )
    async def test_prefect_httpx_client_raises_final_error_after_retries(
        self, monkeypatch, final_response, expected_error_type
    ):
        client = PrefectHttpxAsyncClient()
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        # First throw a bunch of retryable errors, then the final one
        base_client_send.side_effect = [httpx.ReadError("test")] * 5 + [final_response]

        with pytest.raises(expected_error_type):
            async with client:
                await client.post(
                    url="fake.url/fake/route",
                    data={"evenmorefake": "data"},
                )

        # 5 retries + 1 first attempt
        assert base_client_send.call_count == 6

    @pytest.mark.parametrize(
        "error_code",
        [status.HTTP_429_TOO_MANY_REQUESTS, status.HTTP_503_SERVICE_UNAVAILABLE],
    )
    @pytest.mark.usefixtures("disable_jitter")
    async def test_prefect_httpx_client_respects_retry_header(
        self, monkeypatch, mock_anyio_sleep, error_code
    ):
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        client = PrefectHttpxAsyncClient()
        retry_response = Response(
            error_code,
            headers={"Retry-After": "5"},
            request=Request("a test request", "fake.url/fake/route"),
        )

        base_client_send.side_effect = [
            retry_response,
            RESPONSE_200,
        ]

        with mock_anyio_sleep.assert_sleeps_for(5):
            async with client:
                response = await client.post(
                    url="fake.url/fake/route", data={"evenmorefake": "data"}
                )
        assert response.status_code == status.HTTP_200_OK

    @pytest.mark.usefixtures("disable_jitter")
    @pytest.mark.parametrize(
        "response_or_exc",
        [RESPONSE_429_RETRY_AFTER_MISSING, httpx.RemoteProtocolError("test")],
    )
    async def test_prefect_httpx_client_uses_exponential_backoff_without_retry_after_header(
        self, mock_anyio_sleep, response_or_exc, monkeypatch
    ):
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        client = PrefectHttpxAsyncClient()

        base_client_send.side_effect = [
            response_or_exc,
            response_or_exc,
            response_or_exc,
            RESPONSE_200,
        ]

        with mock_anyio_sleep.assert_sleeps_for(2 + 4 + 8):
            async with client:
                response = await client.post(
                    url="fake.url/fake/route", data={"evenmorefake": "data"}
                )
        assert response.status_code == status.HTTP_200_OK
        mock_anyio_sleep.assert_has_awaits([mock.call(2), mock.call(4), mock.call(8)])

    @pytest.mark.usefixtures("disable_jitter")
    async def test_prefect_httpx_client_respects_retry_header_per_response(
        self, mock_anyio_sleep, monkeypatch
    ):
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        client = PrefectHttpxAsyncClient()

        base_client_send.side_effect = [
            # Generate responses with retry after headers
            Response(
                status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(retry_after)},
                request=Request("a test request", "fake.url/fake/route"),
            )
            for retry_after in [5, 0, 10, 2.0]
        ] + [RESPONSE_200]  # Then succeed

        with mock_anyio_sleep.assert_sleeps_for(5 + 10 + 2):
            async with client:
                response = await client.post(
                    url="fake.url/fake/route", data={"evenmorefake": "data"}
                )
        assert response.status_code == status.HTTP_200_OK
        mock_anyio_sleep.assert_has_awaits(
            [mock.call(5), mock.call(0), mock.call(10), mock.call(2.0)]
        )

    async def test_prefect_httpx_client_adds_jitter_with_retry_header(
        self, monkeypatch, mock_anyio_sleep
    ):
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        client = PrefectHttpxAsyncClient()
        retry_response = Response(
            status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "5"},
            request=Request("a test request", "fake.url/fake/route"),
        )

        base_client_send.side_effect = [
            retry_response,
            retry_response,
            retry_response,
            retry_response,
            retry_response,
            RESPONSE_200,
        ]

        async with client:
            response = await client.post(
                url="fake.url/fake/route", data={"evenmorefake": "data"}
            )
        assert response.status_code == status.HTTP_200_OK

        for mock_call in mock_anyio_sleep.mock_calls:
            sleep_time = mock_call.args[0]
            assert sleep_time > 5 and sleep_time < (5 * 1.2)

    @pytest.mark.parametrize(
        "response_or_exc",
        [RESPONSE_429_RETRY_AFTER_MISSING, httpx.RemoteProtocolError("test")],
    )
    async def test_prefect_httpx_client_adds_jitter_with_exponential_backoff(
        self, mock_anyio_sleep, response_or_exc, monkeypatch
    ):
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        client = PrefectHttpxAsyncClient()

        base_client_send.side_effect = [
            response_or_exc,
            response_or_exc,
            response_or_exc,
            RESPONSE_200,
        ]

        with mock_anyio_sleep.assert_sleeps_for(
            2 + 4 + 8,
            extra_tolerance=0.2 * 14,  # Add tolerance for jitter
        ):
            async with client:
                response = await client.post(
                    url="fake.url/fake/route", data={"evenmorefake": "data"}
                )
        assert response.status_code == status.HTTP_200_OK
        mock_anyio_sleep.assert_has_awaits(
            [mock.call(pytest.approx(n, rel=0.2)) for n in [2, 4, 8]]
        )

    async def test_prefect_httpx_client_does_not_retry_other_exceptions(
        self, mock_anyio_sleep, monkeypatch
    ):
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        client = PrefectHttpxAsyncClient()

        base_client_send.side_effect = [TypeError("This error should not be retried")]

        with pytest.raises(TypeError, match="This error should not be retried"):
            async with client:
                await client.post(
                    url="fake.url/fake/route", data={"evenmorefake": "data"}
                )

        mock_anyio_sleep.assert_not_called()

    async def test_prefect_httpx_client_returns_prefect_response(self, monkeypatch):
        """Test that the PrefectHttpxAsyncClient returns a PrefectResponse"""
        client = PrefectHttpxAsyncClient()
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        base_client_send.return_value = RESPONSE_200

        async with client:
            response = await client.post(
                url="fake.url/fake/route", data={"evenmorefake": "data"}
            )
        assert isinstance(response, PrefectResponse)

    async def test_prefect_httpx_client_raises_prefect_http_status_error(
        self, monkeypatch
    ):
        RESPONSE_400 = Response(
            status.HTTP_400_BAD_REQUEST,
            json={"extra_info": [{"message": "a test error message"}]},
            request=Request("a test request", "fake.url/fake/route"),
        )

        client = PrefectHttpxAsyncClient()
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        base_client_send.return_value = RESPONSE_400
        with pytest.raises(PrefectHTTPStatusError) as exc:
            async with client:
                await client.post(
                    url="fake.url/fake/route", data={"evenmorefake": "data"}
                )
        expected = "Response: {'extra_info': [{'message': 'a test error message'}]}"
        assert expected in str(exc.exconly())

    async def test_prefect_httpx_client_retries_indefinitely_during_maintenance(
        self, monkeypatch, mock_anyio_sleep
    ):
        base_client_send = AsyncMock()
        monkeypatch.setattr(AsyncClient, "send", base_client_send)

        client = PrefectHttpxAsyncClient()
        retry_response = Response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Prefect-Maintenance": "true"},
            request=Request("a test request", "fake.url/fake/route"),
        )

        # Return more than max retries worth of maintenance responses
        retry_count = PREFECT_CLIENT_MAX_RETRIES.value() * 2
        base_client_send.side_effect = [retry_response] * retry_count

        with pytest.raises(Exception):
            async with client:
                await client.post(
                    url="fake.url/fake/route",
                    data={"evenmorefake": "data"},
                )

        # Should have tried more times than the normal retry limit
        assert base_client_send.call_count == retry_count + 1


@asynccontextmanager
async def mocked_client(
    responses: List[Response],
    **client_kwargs: Dict[str, Any],
) -> AsyncGenerator[Tuple[PrefectHttpxAsyncClient, mock.AsyncMock], None]:
    with mock.patch("httpx.AsyncClient.send", autospec=True) as send:
        send.side_effect = responses
        client = PrefectHttpxAsyncClient(**client_kwargs)
        async with client:
            try:
                yield client, send
            finally:
                pass


@asynccontextmanager
async def mocked_csrf_client(
    responses: List[Response],
) -> AsyncGenerator[Tuple[PrefectHttpxAsyncClient, mock.AsyncMock], None]:
    async with mocked_client(responses, enable_csrf_support=True) as (client, send):
        yield client, send


class TestCsrfSupport:
    async def test_no_csrf_headers_not_change_request(self):
        async with mocked_csrf_client(responses=[RESPONSE_200]) as (client, send):
            await client.get(url="fake.url/fake/route")

        request = send.call_args[0][1]
        assert isinstance(request, httpx.Request)

        assert "Prefect-Csrf-Token" not in request.headers
        assert "Prefect-Csrf-Client" not in request.headers

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    async def test_csrf_headers_on_change_request(self, method: str):
        async with mocked_csrf_client(responses=[RESPONSE_CSRF, RESPONSE_200]) as (
            client,
            send,
        ):
            await getattr(client, method)(url="fake.url/fake/route")

        assert send.await_count == 2

        # The first call should be for the CSRF token
        request = send.call_args_list[0][0][1]
        assert isinstance(request, httpx.Request)
        assert request.method == "GET"
        assert request.url == httpx.URL(
            f"/csrf-token?client={str(client.csrf_client_id)}"
        )

        # The second call should be for the actual request
        request = send.call_args_list[1][0][1]
        assert isinstance(request, httpx.Request)
        assert request.method == method.upper()
        assert request.url == httpx.URL("/fake.url/fake/route")
        assert request.headers["Prefect-Csrf-Token"] == "test_token"
        assert request.headers["Prefect-Csrf-Client"] == str(client.csrf_client_id)

    @pytest.mark.xfail(
        reason="Very brittle for some reason, see https://github.com/PrefectHQ/prefect/issues/13963"
    )
    async def test_refreshes_token_on_csrf_403(self):
        async with mocked_csrf_client(
            responses=[
                RESPONSE_CSRF,
                RESPONSE_INVALID_TOKEN,
                RESPONSE_CSRF,
                RESPONSE_200,
            ]
        ) as (
            client,
            send,
        ):
            await client.post(url="fake.url/fake/route")

        assert send.await_count == 4

        # The first call should be for the CSRF token
        request = send.call_args_list[0][0][1]
        assert isinstance(request, httpx.Request)
        assert request.method == "GET"
        assert request.url == httpx.URL(
            f"/csrf-token?client={str(client.csrf_client_id)}"
        )

        # The second call should be for the actual request
        request = send.call_args_list[1][0][1]
        assert isinstance(request, httpx.Request)
        assert request.url == httpx.URL("/fake.url/fake/route")
        assert request.headers["Prefect-Csrf-Token"] == "test_token"
        assert request.headers["Prefect-Csrf-Client"] == str(client.csrf_client_id)

        # The third call should be a refresh of the CSRF token
        request = send.call_args_list[0][0][1]
        assert isinstance(request, httpx.Request)
        assert request.method == "GET"
        assert request.url == httpx.URL(
            f"/csrf-token?client={str(client.csrf_client_id)}"
        )

        # The fourth call should be for the actual request
        request = send.call_args_list[1][0][1]
        assert isinstance(request, httpx.Request)
        assert request.url == httpx.URL("/fake.url/fake/route")
        assert request.headers["Prefect-Csrf-Token"] == "test_token"
        assert request.headers["Prefect-Csrf-Client"] == str(client.csrf_client_id)

    async def test_does_not_refresh_csrf_token_not_expired(self):
        async with mocked_csrf_client(responses=[RESPONSE_200]) as (
            client,
            send,
        ):
            client.csrf_token = "fresh_token"
            client.csrf_token_expiration = now + timedelta(days=1)
            await client.post(url="fake.url/fake/route")

        assert send.await_count == 1

        request = send.call_args_list[0][0][1]
        assert isinstance(request, httpx.Request)
        assert request.url == httpx.URL("/fake.url/fake/route")
        assert request.headers["Prefect-Csrf-Token"] == "fresh_token"
        assert request.headers["Prefect-Csrf-Client"] == str(client.csrf_client_id)

    async def test_does_refresh_csrf_token_when_expired(self):
        async with mocked_csrf_client(responses=[RESPONSE_CSRF, RESPONSE_200]) as (
            client,
            send,
        ):
            client.csrf_token = "old_token"
            client.csrf_token_expiration = now - timedelta(days=1)
            await client.post(url="fake.url/fake/route")

        assert send.await_count == 2

        # The first call should be for the CSRF token
        request = send.call_args_list[0][0][1]
        assert isinstance(request, httpx.Request)
        assert request.method == "GET"
        assert request.url == httpx.URL(
            f"/csrf-token?client={str(client.csrf_client_id)}"
        )

        # The second call should be for the actual request
        request = send.call_args_list[1][0][1]
        assert isinstance(request, httpx.Request)
        assert request.url == httpx.URL("/fake.url/fake/route")
        assert request.headers["Prefect-Csrf-Token"] == "test_token"
        assert request.headers["Prefect-Csrf-Client"] == str(client.csrf_client_id)

    async def test_raises_exception_bad_csrf_token_response(self):
        async with mocked_csrf_client(responses=[RESPONSE_400]) as (
            client,
            _,
        ):
            with pytest.raises(PrefectHTTPStatusError):
                await client.post(url="fake.url/fake/route")

    async def test_disables_csrf_support_404_token_endpoint(self):
        async with mocked_csrf_client(responses=[RESPONSE_404, RESPONSE_200]) as (
            client,
            send,
        ):
            assert client.enable_csrf_support is True
            await client.post(url="fake.url/fake/route")
            assert client.enable_csrf_support is False

    async def test_disables_csrf_support_422_csrf_disabled(self):
        async with mocked_csrf_client(
            responses=[RESPONSE_CSRF_DISABLED, RESPONSE_200]
        ) as (
            client,
            send,
        ):
            assert client.enable_csrf_support is True
            await client.post(url="fake.url/fake/route")
            assert client.enable_csrf_support is False


class TestUserAgent:
    @pytest.fixture
    def prefect_version(self, monkeypatch: pytest.MonkeyPatch) -> str:
        v = "42.43.44"
        monkeypatch.setattr(prefect, "__version__", v)
        return v

    @pytest.fixture
    def prefect_api_version(self, monkeypatch: pytest.MonkeyPatch) -> str:
        v = "45.46.47"
        monkeypatch.setattr(prefect.client.constants, "SERVER_API_VERSION", v)
        return v

    async def test_passes_informative_user_agent(
        self,
        prefect_version: str,
        prefect_api_version: str,
    ):
        async with mocked_client(responses=[RESPONSE_200]) as (client, send):
            await client.get(url="fake.url/fake/route")

        request = send.call_args[0][1]
        assert isinstance(request, httpx.Request)

        assert request.headers["User-Agent"] == "prefect/42.43.44 (API 45.46.47)"


class TestCustomHeaders:
    """Test custom headers functionality in HTTP clients."""

    async def test_default_no_custom_headers(self):
        """Test that no custom headers are added by default."""
        async with PrefectHttpxAsyncClient(base_url="http://localhost:4200") as client:
            # Should only have standard headers, no custom ones
            headers = dict(client.headers)
            # httpx normalizes header names to lowercase
            assert "user-agent" in headers
            # Verify no unexpected custom headers
            custom_header_prefixes = ["x-", "authorization", "api-key"]
            custom_headers = [
                k
                for k in headers.keys()
                if any(
                    k.lower().startswith(prefix) for prefix in custom_header_prefixes
                )
            ]
            assert len(custom_headers) == 0

    async def test_custom_headers_from_settings(self):
        """Test that custom headers are added from settings."""
        custom_headers = {
            "X-Test-Header": "test-value",
            "X-Custom-Auth": "Bearer token123",
            "Api-Version": "v1",
        }

        with temporary_settings({PREFECT_CLIENT_CUSTOM_HEADERS: custom_headers}):
            async with PrefectHttpxAsyncClient(
                base_url="http://localhost:4200"
            ) as client:
                for header_name, expected_value in custom_headers.items():
                    assert client.headers[header_name] == expected_value

    async def test_custom_headers_json_env_var(self, monkeypatch: pytest.MonkeyPatch):
        """Test custom headers from JSON environment variable."""
        json_value = (
            '{"X-Json-Header": "json-value", "Authorization": "Bearer env-token"}'
        )
        monkeypatch.setenv("PREFECT_CLIENT_CUSTOM_HEADERS", json_value)

        # Create a new settings instance to pick up the env var
        from prefect.settings.models.root import Settings

        settings = Settings()

        expected_headers = {
            "X-Json-Header": "json-value",
            "Authorization": "Bearer env-token",
        }
        assert settings.client.custom_headers == expected_headers

        # Test that it works with the client (using the setting directly)
        with temporary_settings({PREFECT_CLIENT_CUSTOM_HEADERS: expected_headers}):
            async with PrefectHttpxAsyncClient(
                base_url="http://localhost:4200"
            ) as client:
                assert client.headers["X-Json-Header"] == "json-value"
                assert client.headers["Authorization"] == "Bearer env-token"

    async def test_protected_headers_not_overridden(self):
        """Test that critical headers cannot be overridden by custom headers."""
        malicious_headers = {
            "User-Agent": "malicious-agent",
            "user-agent": "another-malicious-agent",  # Test case insensitive
            "Prefect-Csrf-Token": "fake-token",
            "prefect-csrf-client": "fake-client",
            "X-Safe-Header": "this-should-work",
        }

        with temporary_settings({PREFECT_CLIENT_CUSTOM_HEADERS: malicious_headers}):
            async with PrefectHttpxAsyncClient(
                base_url="http://localhost:4200"
            ) as client:
                # User-Agent should still be the Prefect one (httpx normalizes to lowercase)
                assert "prefect/" in client.headers["user-agent"]
                assert "malicious-agent" not in client.headers["user-agent"]

                # CSRF headers should not be set (they're set later during requests)
                assert "prefect-csrf-token" not in client.headers
                assert "prefect-csrf-client" not in client.headers

                # Safe header should be added
                assert client.headers["X-Safe-Header"] == "this-should-work"

    async def test_sync_client_custom_headers(self):
        """Test custom headers work with sync client."""
        from prefect.client.base import PrefectHttpxSyncClient

        custom_headers = {"X-Sync-Test": "sync-value", "Custom-Header": "sync-custom"}

        with temporary_settings({PREFECT_CLIENT_CUSTOM_HEADERS: custom_headers}):
            with PrefectHttpxSyncClient(base_url="http://localhost:4200") as client:
                for header_name, expected_value in custom_headers.items():
                    assert client.headers[header_name] == expected_value

    async def test_custom_headers_case_preserved(self):
        """Test that custom header names preserve their case."""
        custom_headers = {
            "X-CamelCase-Header": "value1",
            "lowercase-header": "value2",
            "UPPERCASE-HEADER": "value3",
        }

        with temporary_settings({PREFECT_CLIENT_CUSTOM_HEADERS: custom_headers}):
            async with PrefectHttpxAsyncClient(
                base_url="http://localhost:4200"
            ) as client:
                # Headers should be accessible with original case
                assert client.headers["X-CamelCase-Header"] == "value1"
                assert client.headers["lowercase-header"] == "value2"
                assert client.headers["UPPERCASE-HEADER"] == "value3"

    async def test_empty_custom_headers(self):
        """Test that empty custom headers dict works correctly."""
        with temporary_settings({PREFECT_CLIENT_CUSTOM_HEADERS: {}}):
            async with PrefectHttpxAsyncClient(
                base_url="http://localhost:4200"
            ) as client:
                # Should behave same as default (no custom headers)
                assert "user-agent" in client.headers
                # No unexpected headers should be added
                expected_headers = {
                    "accept",
                    "accept-encoding",
                    "connection",
                    "user-agent",
                }
                actual_headers = {k.lower() for k in client.headers.keys()}
                assert actual_headers == expected_headers

    @pytest.mark.parametrize(
        "protected_header",
        [
            "User-Agent",
            "user-agent",
            "USER-AGENT",
            "Prefect-Csrf-Token",
            "prefect-csrf-token",
            "PREFECT-CSRF-TOKEN",
            "Prefect-Csrf-Client",
            "prefect-csrf-client",
        ],
    )
    async def test_protected_headers_case_insensitive(self, protected_header):
        """Test that protected headers are blocked regardless of case."""
        custom_headers = {protected_header: "should-be-blocked"}

        with temporary_settings({PREFECT_CLIENT_CUSTOM_HEADERS: custom_headers}):
            async with PrefectHttpxAsyncClient(
                base_url="http://localhost:4200"
            ) as client:
                if protected_header.lower() == "user-agent":
                    # User-Agent should still be the Prefect one (httpx normalizes to lowercase)
                    assert "prefect/" in client.headers["user-agent"]
                    assert "should-be-blocked" not in client.headers["user-agent"]
                else:
                    # Other protected headers should not be in headers at all
                    # (they get added later in the request lifecycle)
                    assert protected_header.lower() not in client.headers

    async def test_protected_headers_warning_logged(self, caplog):
        """Test that warning is logged when protected headers are attempted."""
        import logging

        malicious_headers = {
            "User-Agent": "malicious-agent",
            "Prefect-Csrf-Token": "fake-token",
        }

        with temporary_settings({PREFECT_CLIENT_CUSTOM_HEADERS: malicious_headers}):
            with caplog.at_level(logging.WARNING):
                async with PrefectHttpxAsyncClient(base_url="http://localhost:4200"):
                    pass

            # Should have logged warnings for both protected headers
            warning_messages = [
                record.message
                for record in caplog.records
                if record.levelname == "WARNING"
            ]

            # Check that warnings were logged for protected headers
            user_agent_warning = any(
                "User-Agent" in msg and "ignored because it conflicts" in msg
                for msg in warning_messages
            )
            csrf_warning = any(
                "Prefect-Csrf-Token" in msg and "ignored because it conflicts" in msg
                for msg in warning_messages
            )

            assert user_agent_warning, (
                f"Expected User-Agent warning not found in: {warning_messages}"
            )
            assert csrf_warning, (
                f"Expected Prefect-Csrf-Token warning not found in: {warning_messages}"
            )


class TestDetermineServerType:
    @pytest.mark.parametrize(
        "temp_settings, expected_type",
        [
            (
                {
                    PREFECT_API_URL: "http://localhost:4200/api",
                },
                ServerType.SERVER,
            ),
            (
                {
                    PREFECT_API_URL: None,
                    PREFECT_SERVER_ALLOW_EPHEMERAL_MODE: True,
                },
                ServerType.EPHEMERAL,
            ),
            (
                {
                    PREFECT_API_URL: None,
                    PREFECT_SERVER_ALLOW_EPHEMERAL_MODE: False,
                },
                ServerType.UNCONFIGURED,
            ),
            (
                {
                    PREFECT_CLOUD_API_URL: "https://api.prefect.cloud/api/",
                    PREFECT_API_URL: "https://api.prefect.cloud/api/accounts/foo/workspaces/bar",
                },
                ServerType.CLOUD,
            ),
        ],
    )
    def test_with_settings_variations(self, temp_settings, expected_type):
        with temporary_settings(temp_settings):
            assert determine_server_type() == expected_type
