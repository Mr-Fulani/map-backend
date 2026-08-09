#!/usr/bin/env python3
import logging
import os

from backup.common import (
    BackupConfigError,
    S3Settings,
    load_json_body,
    notify_monitor_safely,
    parse_utc,
    required_env,
    utc_now,
    validate_manifest,
    verify_manifest_signature,
)


logger = logging.getLogger('backup-freshness')


def _max_age_seconds() -> int:
    raw_value = os.environ.get('BACKUP_MAX_AGE_SECONDS', '93600')
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise BackupConfigError('BACKUP_MAX_AGE_SECONDS must be an integer') from exc
    if value <= 0:
        raise BackupConfigError('BACKUP_MAX_AGE_SECONDS must be positive')
    return value


def check_freshness(
    s3: S3Settings,
    public_key_value: str,
    *,
    now=None,
    max_age_seconds=None,
) -> dict:
    now = now or utc_now()
    max_age_seconds = max_age_seconds or _max_age_seconds()
    client = s3.client()
    response = client.get_object(
        Bucket=s3.bucket,
        Key=s3.key('latest.json'),
    )
    manifest = validate_manifest(load_json_body(response['Body'], label='latest.json'))
    verify_manifest_signature(manifest, public_key_value)

    companion_response = client.get_object(
        Bucket=s3.bucket,
        Key=manifest['manifest_key'],
    )
    companion = validate_manifest(
        load_json_body(companion_response['Body'], label='companion manifest'),
        expected_object_key=manifest['object_key'],
    )
    verify_manifest_signature(companion, public_key_value)
    if companion != manifest:
        raise BackupConfigError('latest.json differs from the companion manifest')

    archive_head = client.head_object(
        Bucket=s3.bucket,
        Key=manifest['object_key'],
        VersionId=manifest['archive_version_id'],
    )
    if archive_head.get('ContentLength') != manifest['encrypted_size_bytes']:
        raise BackupConfigError('Encrypted archive size differs from the manifest')
    metadata_checksum = archive_head.get('Metadata', {}).get('sha256')
    if metadata_checksum != manifest['sha256']:
        raise BackupConfigError('Encrypted archive metadata checksum differs from manifest')

    created_at = parse_utc(manifest.get('created_at', ''))
    age_seconds = (now - created_at).total_seconds()
    if age_seconds < -300:
        raise BackupConfigError('Latest backup timestamp is in the future')
    if age_seconds > max_age_seconds:
        raise BackupConfigError(
            f'Latest backup is stale: {int(age_seconds)}s > {max_age_seconds}s'
        )
    return {
        'object_key': manifest.get('object_key'),
        'created_at': manifest['created_at'],
        'age_seconds': max(0, int(age_seconds)),
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        result = check_freshness(
            S3Settings.from_env(),
            required_env('BACKUP_SIGNING_PUBLIC_KEY'),
        )
        logger.info(
            'Latest backup is fresh: key=%s age=%ss',
            result['object_key'],
            result['age_seconds'],
        )
        notify_monitor_safely(
            os.environ.get('BACKUP_FRESHNESS_MONITOR_SUCCESS_URL'),
            {'status': 'ok', **result},
            logger,
        )
        return 0
    except Exception as exc:
        logger.exception('Backup freshness check failed')
        notify_monitor_safely(
            os.environ.get('BACKUP_FRESHNESS_MONITOR_FAILURE_URL'),
            {'status': 'error', 'error_type': type(exc).__name__},
            logger,
        )
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
