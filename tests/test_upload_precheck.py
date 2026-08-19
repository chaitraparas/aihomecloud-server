"""
The pre-upload hash check — "which of these do you already have?"

Borrowed in shape from LocalSend's `prepare-upload` handshake (public protocol spec; this is a
re-implementation, not their code). The property that matters most here is not speed but **scope
isolation**: answering for another member's library would turn a private dedup lookup into a hash
oracle, letting anyone test whether a given file sits in someone else's personal folder.
"""

import hashlib
import io

import pytest
from httpx import AsyncClient


async def _upload(client, token, name, content, scope="personal"):
    return await client.post(
        f"/api/v1/files/upload?syncScope={scope}&sourceFolder=Camera",
        files={"file": (name, io.BytesIO(content), "image/jpeg")},
        headers={"Authorization": f"Bearer {token}"},
    )


async def _precheck(client, token, hashes, scope="personal"):
    return await client.post(
        "/api/v1/files/upload/precheck",
        json={"hashes": hashes, "syncScope": scope},
        headers={"Authorization": f"Bearer {token}"},
    )


def _jpeg(marker: bytes) -> bytes:
    return b"\xff\xd8\xff" + marker * 512


class TestPrecheckBasics:
    async def test_unknown_hashes_are_all_needed(self, client: AsyncClient, member_token: str):
        r = await _precheck(client, member_token, ["a" * 64, "b" * 64])

        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body["needed"]) == {"a" * 64, "b" * 64}
        assert body["haveCount"] == 0
        assert body["checked"] == 2

    async def test_an_uploaded_file_is_reported_as_not_needed(
        self, client: AsyncClient, member_token: str
    ):
        content = _jpeg(b"\x01")
        digest = hashlib.sha256(content).hexdigest()
        assert (await _upload(client, member_token, "pc.jpg", content)).status_code == 201

        body = (await _precheck(client, member_token, [digest])).json()

        assert body["needed"] == []
        assert body["haveCount"] == 1

    async def test_duplicate_hashes_in_the_request_are_collapsed(
        self, client: AsyncClient, member_token: str
    ):
        h = "c" * 64

        body = (await _precheck(client, member_token, [h, h, h])).json()

        assert body["checked"] == 1
        assert body["needed"] == [h]

    async def test_empty_request_is_valid(self, client: AsyncClient, member_token: str):
        body = (await _precheck(client, member_token, [])).json()

        assert body == {"needed": [], "haveCount": 0, "checked": 0}

    async def test_hashes_are_matched_case_insensitively(
        self, client: AsyncClient, member_token: str
    ):
        content = _jpeg(b"\x02")
        digest = hashlib.sha256(content).hexdigest()
        await _upload(client, member_token, "case.jpg", content)

        body = (await _precheck(client, member_token, [digest.upper()])).json()

        assert body["needed"] == [], "an uppercase hex digest is the same hash"

    async def test_authentication_is_required(self, client: AsyncClient):
        r = await client.post("/api/v1/files/upload/precheck", json={"hashes": ["a" * 64]})

        assert r.status_code in (401, 403)


class TestScopeIsolation:
    """The disclosure risk: a precheck must never answer for content the caller cannot see."""

    async def test_another_members_personal_file_is_still_reported_as_needed(
        self, client: AsyncClient, member_token: str
    ):
        content = _jpeg(b"\x03")
        digest = hashlib.sha256(content).hexdigest()
        assert (await _upload(client, member_token, "private.jpg", content)).status_code == 201

        # The other account the member_token fixture creates.
        resp = await client.post("/api/v1/auth/login", json={"name": "admin", "pin": "0000"})
        other = resp.json()["accessToken"]

        body = (await _precheck(client, other, [digest])).json()

        assert body["needed"] == [digest], (
            "answering 'already have it' would let one member probe another's library by hash"
        )
        assert body["haveCount"] == 0

    async def test_a_family_scope_file_is_shared(self, client: AsyncClient, member_token: str):
        content = _jpeg(b"\x04")
        digest = hashlib.sha256(content).hexdigest()
        assert (
            await _upload(client, member_token, "shared.jpg", content, scope="family")
        ).status_code == 201

        resp = await client.post("/api/v1/auth/login", json={"name": "admin", "pin": "0000"})
        other = resp.json()["accessToken"]

        body = (await _precheck(client, other, [digest], scope="family")).json()

        assert body["needed"] == [], "family scope is genuinely shared"

    async def test_personal_and_family_are_separate_namespaces(
        self, client: AsyncClient, member_token: str
    ):
        content = _jpeg(b"\x05")
        digest = hashlib.sha256(content).hexdigest()
        await _upload(client, member_token, "p.jpg", content, scope="personal")

        family = (await _precheck(client, member_token, [digest], scope="family")).json()

        assert family["needed"] == [digest], "a personal copy must not satisfy a family upload"
