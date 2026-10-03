"""
Overseerr/Jellyseerr companion client implementation.
"""

import requests

from app.models import Connection

from .base import CompanionClient

TEST_TIMEOUT_SECONDS = 10


class OverseerrClient(CompanionClient):
    """Client for integrating with Overseerr/Jellyseerr (info-only)."""

    @property
    def requires_api_call(self) -> bool:
        return False

    @property
    def display_name(self) -> str:
        return "Overseerr/Jellyseerr"

    def invite_user(
        self,
        username: str,  # noqa: ARG002
        email: str,  # noqa: ARG002
        connection: Connection,  # noqa: ARG002
        password: str = "",  # noqa: ARG002
    ) -> dict[str, str]:
        """
        Overseerr connections are info-only, no actual API calls needed.

        Args:
            username: Username to invite (unused - info-only)
            email: Email address (unused - info-only)
            connection: Connection object with URL and API key (unused - info-only)
            password: Password for the user (unused - info-only)

        Returns:
            Dict with 'status' and 'message' keys
        """
        return {
            "status": "info_only",
            "message": "Overseerr auto-imports users automatically",
        }

    def delete_user(self, username: str, connection: Connection) -> dict[str, str]:  # noqa: ARG002
        """
        Overseerr connections are info-only, no deletion needed.

        Args:
            username: Username to delete (unused - info-only)
            connection: Connection object with URL and API key (unused - info-only)

        Returns:
            Dict with 'status' and 'message' keys
        """
        return {
            "status": "info_only",
            "message": "Overseerr users managed automatically",
        }

    def test_connection(self, connection: Connection) -> dict[str, str]:
        """Check the URL and API key when given; info-only otherwise.

        The URL and API key are optional. With them, sauron can take Seerr
        requests away from members on the renewal screen
        (app/services/seerr_access.py), so they must actually work.
        """
        if not (connection.url and connection.api_key):
            return {
                "status": "info_only",
                "message": "Overseerr connections are informational only - no API testing required",
            }
        try:
            response = requests.get(
                f"{connection.url.rstrip('/')}/api/v1/settings/main",
                headers={"X-Api-Key": connection.api_key},
                timeout=TEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return {"status": "error", "message": f"Could not reach Seerr: {exc}"}
        if response.status_code != 200:
            return {
                "status": "error",
                "message": f"Seerr refused the API key (HTTP {response.status_code})",
            }
        return {"status": "success", "message": "Connected to Seerr"}
