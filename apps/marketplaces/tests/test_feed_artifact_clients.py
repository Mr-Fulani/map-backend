from unittest.mock import Mock, patch

import pytest
from django.test import override_settings

from apps.marketplaces.feed_artifact_clients import (
    PrivateFeedClientConfigurationError,
    YandexPrivateVersionedObjectClient,
    presign_private_feed_exact_version,
    private_feed_bucket_preflight,
)


PRIVATE_SETTINGS = {
    'MARKETPLACE_FEED_ARTIFACT_BUCKET': 'private-feed-artifacts-1',
    'MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID': 'private-access',
    'MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY': 'private-secret',
    'MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER': 'folder-owner-1',
    'MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID': 'kms-key-1',
}


def _object_client():
    return YandexPrivateVersionedObjectClient(
        write_client=Mock(),
        read_client=Mock(),
        bucket='private-feed-artifacts-1',
        expected_bucket_owner='folder-owner-1',
        kms_key_id='kms-key-1',
    )


def test_put_is_one_shot_scoped_and_forces_kms():
    client = _object_client()
    client.write_client.put_object.return_value = {'VersionId': 'v1'}

    response = client.put_object_once(
        Bucket='private-feed-artifacts-1',
        Key='private-feeds/v1/endpoint/run/00001/feed.xml',
        Body=b'<Ads/>',
        ExpectedBucketOwner='folder-owner-1',
    )

    assert response == {'VersionId': 'v1'}
    assert client.put_total_max_attempts == 1
    client.write_client.put_object.assert_called_once_with(
        Bucket='private-feed-artifacts-1',
        Key='private-feeds/v1/endpoint/run/00001/feed.xml',
        Body=b'<Ads/>',
        ServerSideEncryption='aws:kms',
        SSEKMSKeyId='kms-key-1',
    )


@pytest.mark.parametrize(
    ('bucket', 'owner'),
    (
        ('another-bucket', 'folder-owner-1'),
        ('private-feed-artifacts-1', 'another-owner'),
    ),
)
def test_object_client_rejects_scope_mismatch(bucket, owner):
    client = _object_client()

    with pytest.raises(PrivateFeedClientConfigurationError):
        client.head_object(
            Bucket=bucket,
            Key='private-feeds/v1/e/r/00001/feed.xml',
            VersionId='v1',
            ExpectedBucketOwner=owner,
        )

    client.read_client.head_object.assert_not_called()


@override_settings(**PRIVATE_SETTINGS)
def test_presigner_is_exact_version_path_style_contract():
    raw_client = Mock()
    raw_client.generate_presigned_url.return_value = (
        'https://storage.yandexcloud.net/private-feed-artifacts-1/'
        'private-feeds/v1/e/r/00001/feed.xml?versionId=v1&X-Amz-Expires=120&'
        'X-Amz-Signature=sig'
    )

    with patch(
        'apps.marketplaces.feed_artifact_clients._client',
        return_value=raw_client,
    ):
        location = presign_private_feed_exact_version(
            bucket='private-feed-artifacts-1',
            object_key='private-feeds/v1/e/r/00001/feed.xml',
            object_version_id='v1',
            request_method='GET',
            expires_in=120,
        )

    assert 'versionId=v1' in location
    raw_client.generate_presigned_url.assert_called_once_with(
        'get_object',
        Params={
            'Bucket': 'private-feed-artifacts-1',
            'Key': 'private-feeds/v1/e/r/00001/feed.xml',
            'VersionId': 'v1',
        },
        ExpiresIn=120,
        HttpMethod='GET',
    )


@override_settings(**PRIVATE_SETTINGS)
def test_bucket_preflight_requires_versioning_and_exact_kms_key():
    raw_client = Mock()
    raw_client.get_bucket_acl.return_value = {
        'Owner': {'ID': 'folder-owner-1'},
    }
    raw_client.get_bucket_versioning.return_value = {'Status': 'Enabled'}
    raw_client.get_bucket_encryption.return_value = {
        'ServerSideEncryptionConfiguration': {
            'Rules': [{
                'ApplyServerSideEncryptionByDefault': {
                    'SSEAlgorithm': 'aws:kms',
                    'KMSMasterKeyID': 'kms-key-1',
                },
            }],
        },
    }

    with patch(
        'apps.marketplaces.feed_artifact_clients._client',
        return_value=raw_client,
    ):
        result = private_feed_bucket_preflight()

    assert result == {
        'bucket': 'private-feed-artifacts-1',
        'owner_id': 'folder-owner-1',
        'owner_check': 'verified',
        'versioning': 'Enabled',
        'kms_key_id': 'kms-key-1',
    }


@override_settings(**PRIVATE_SETTINGS)
def test_bucket_preflight_fails_closed_for_wrong_folder_owner():
    raw_client = Mock()
    raw_client.get_bucket_acl.return_value = {
        'Owner': {'ID': 'another-folder'},
    }

    with (
        patch(
            'apps.marketplaces.feed_artifact_clients._client',
            return_value=raw_client,
        ),
        pytest.raises(
            PrivateFeedClientConfigurationError,
            match='owner',
        ),
    ):
        private_feed_bucket_preflight()

    raw_client.get_bucket_versioning.assert_not_called()


@override_settings(**PRIVATE_SETTINGS)
def test_bucket_preflight_accepts_yandex_omitted_owner_fields():
    raw_client = Mock()
    raw_client.get_bucket_acl.return_value = {
        'Owner': {'ID': '', 'DisplayName': ''},
    }
    raw_client.get_bucket_versioning.return_value = {'Status': 'Enabled'}
    raw_client.get_bucket_encryption.return_value = {
        'ServerSideEncryptionConfiguration': {
            'Rules': [{
                'ApplyServerSideEncryptionByDefault': {
                    'SSEAlgorithm': 'aws:kms',
                    'KMSMasterKeyID': 'kms-key-1',
                },
            }],
        },
    }

    with patch(
        'apps.marketplaces.feed_artifact_clients._client',
        return_value=raw_client,
    ):
        result = private_feed_bucket_preflight()

    assert result == {
        'bucket': 'private-feed-artifacts-1',
        'owner_id': '',
        'owner_check': 'unavailable',
        'versioning': 'Enabled',
        'kms_key_id': 'kms-key-1',
    }


@override_settings(**PRIVATE_SETTINGS)
def test_bucket_preflight_fails_closed_for_partial_owner_response():
    raw_client = Mock()
    raw_client.get_bucket_acl.return_value = {
        'Owner': {'ID': '', 'DisplayName': 'unexpected-owner'},
    }

    with (
        patch(
            'apps.marketplaces.feed_artifact_clients._client',
            return_value=raw_client,
        ),
        pytest.raises(
            PrivateFeedClientConfigurationError,
            match='incomplete',
        ),
    ):
        private_feed_bucket_preflight()

    raw_client.get_bucket_versioning.assert_not_called()


@override_settings(**PRIVATE_SETTINGS)
def test_bucket_preflight_fails_closed_for_suspended_versioning():
    raw_client = Mock()
    raw_client.get_bucket_acl.return_value = {
        'Owner': {'ID': 'folder-owner-1'},
    }
    raw_client.get_bucket_versioning.return_value = {'Status': 'Suspended'}

    with (
        patch(
            'apps.marketplaces.feed_artifact_clients._client',
            return_value=raw_client,
        ),
        pytest.raises(
            PrivateFeedClientConfigurationError,
            match='versioning',
        ),
    ):
        private_feed_bucket_preflight()
