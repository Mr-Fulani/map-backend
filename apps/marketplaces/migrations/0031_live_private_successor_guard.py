from importlib import import_module

from django.db import migrations


_previous = import_module(
    'apps.marketplaces.migrations.0030_private_feed_artifact_guards',
)
_function_start = _previous.FORWARD_SQL.index(
    'CREATE OR REPLACE FUNCTION mkt_feed_upload_guard_fn()',
)
_function_end = _previous.FORWARD_SQL.index(
    'DROP TRIGGER IF EXISTS mkt_feed_upload_guard_trg',
    _function_start,
)
ORIGINAL_UPLOAD_GUARD_SQL = _previous.FORWARD_SQL[
    _function_start:_function_end
]

_DARK_OR_LEGACY_ENDPOINT = '''OR NOT (
               (endpoint_row.storage_mode = 'private_generation'
                AND endpoint_row.serve_enabled IS FALSE)
               OR (endpoint_row.storage_mode = 'legacy_bridge'
                   AND endpoint_row.serve_enabled IS TRUE)
           )'''
_PRIVATE_OR_LIVE_LEGACY_ENDPOINT = '''OR NOT (
               endpoint_row.storage_mode = 'private_generation'
               OR (endpoint_row.storage_mode = 'legacy_bridge'
                   AND endpoint_row.serve_enabled IS TRUE)
           )'''

if ORIGINAL_UPLOAD_GUARD_SQL.count(_DARK_OR_LEGACY_ENDPOINT) != 2:
    raise RuntimeError(
        '0030 upload guard endpoint predicates changed unexpectedly.',
    )

UPDATED_UPLOAD_GUARD_SQL = ORIGINAL_UPLOAD_GUARD_SQL.replace(
    _DARK_OR_LEGACY_ENDPOINT,
    _PRIVATE_OR_LIVE_LEGACY_ENDPOINT,
)


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0030_private_feed_artifact_guards'),
    ]

    operations = [
        migrations.RunSQL(
            sql=UPDATED_UPLOAD_GUARD_SQL,
            reverse_sql=ORIGINAL_UPLOAD_GUARD_SQL,
        ),
    ]
